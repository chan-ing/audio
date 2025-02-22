import numpy as np
import torch
import os
import torch.nn as nn
import bisect
import torch.optim as optim
import torch.distributed as dist
import pytorch_lightning as pl
from sklearn.metrics import average_precision_score, roc_auc_score, accuracy_score
from utils import get_loss_func, get_mix_lambda, d_prime

class SEDWrapper(pl.LightningModule):
    def __init__(self, sed_model, config, dataset):
        super().__init__()
        self.sed_model = sed_model
        self.config = config
        self.dataset = dataset
        self.loss_func = get_loss_func(config.loss_type)
        self.training_step_outputs = []
        self.validation_step_outputs = []

    def evaluate_metric(self, pred, ans):
        if self.config.dataset_type == "audioset":
            mAP = np.mean(average_precision_score(ans, pred, average=None))
            mAUC = np.mean(roc_auc_score(ans, pred, average=None))
            dprime = d_prime(mAUC)
            return {"mAP": mAP, "mAUC": mAUC, "dprime": dprime}
        else:
            acc = accuracy_score(ans, np.argmax(pred, 1))
            return {"acc": acc}

    def forward(self, x, mix_lambda=None):
        output_dict = self.sed_model(x, mix_lambda)
        return output_dict["clipwise_output"], output_dict["framewise_output"]

    def inference(self, x):
        self.device_type = next(self.parameters()).device
        self.eval()
        x = torch.from_numpy(x).float().to(self.device_type)
        output_dict = self.sed_model(x, None, True)
        for key in output_dict.keys():
            output_dict[key] = output_dict[key].detach().cpu().numpy()
        return output_dict

    def training_step(self, batch, batch_idx):
        self.device_type = next(self.parameters()).device
        if self.config.dataset_type == "audioset":
            mix_lambda = torch.from_numpy(get_mix_lambda(0.5, len(batch["waveform"]))).to(self.device_type)
        else:
            mix_lambda = None

        pred, _ = self(batch["waveform"], mix_lambda)
        loss = self.loss_func(pred, batch["target"])
        self.training_step_outputs.append(loss)
        self.log("loss", loss, on_epoch=True, prog_bar=True)
        return loss

    def on_train_epoch_end(self):
        epoch_average = torch.stack(self.training_step_outputs).mean()
        self.log("training_epoch_average", epoch_average)
        self.training_step_outputs.clear()  # free memory
        self.dataset.generate_queue()

    def validation_step(self, batch, batch_idx):
        pred, _ = self(batch["waveform"])
        return [pred.detach(), batch["target"].detach()]

    def validation_epoch_end(self, validation_step_outputs):
        self.device_type = next(self.parameters()).device
        pred = torch.cat([d[0] for d in validation_step_outputs], dim=0)
        target = torch.cat([d[1] for d in validation_step_outputs], dim=0)

        if torch.cuda.device_count() > 1:
            gather_pred = [torch.zeros_like(pred) for _ in range(dist.get_world_size())]
            gather_target = [torch.zeros_like(target) for _ in range(dist.get_world_size())]
            dist.all_gather(gather_pred, pred)
            dist.all_gather(gather_target, target)
            gather_pred = torch.cat(gather_pred, dim=0).cpu().numpy()
            gather_target = torch.cat(gather_target, dim=0).cpu().numpy()
        else:
            gather_pred = pred.cpu().numpy()
            gather_target = target.cpu().numpy()

        if self.config.dataset_type == "scv2":
            gather_target = np.argmax(gather_target, 1)
        metric_dict = self.evaluate_metric(gather_pred, gather_target)
        print(self.device_type, metric_dict, flush=True)

        if self.config.dataset_type == "audioset":
            self.log("mAP", metric_dict["mAP"], on_epoch=True, prog_bar=True, sync_dist=True)
            self.log("mAUC", metric_dict["mAUC"], on_epoch=True, prog_bar=True, sync_dist=True)
            self.log("dprime", metric_dict["dprime"], on_epoch=True, prog_bar=True, sync_dist=True)
        else:
            self.log("acc", metric_dict["acc"], on_epoch=True, prog_bar=True, sync_dist=True)

    def test_step(self, batch, batch_idx):
        self.device_type = next(self.parameters()).device
        preds = []
        shift_num = 1 if self.config.fl_local or self.config.dataset_type != "audioset" else 10
        for i in range(shift_num):
            pred, pred_map = self(batch["waveform"])
            preds.append(pred.unsqueeze(0))
            batch["waveform"] = self.time_shifting(batch["waveform"], shift_len=100 * (i + 1))
        preds = torch.cat(preds, dim=0)
        pred = preds.mean(dim=0)
        if self.config.fl_local:
            return [pred.detach().cpu().numpy(), pred_map.detach().cpu().numpy(), batch["audio_name"], batch["real_len"].cpu().numpy()]
        else:
            return [pred.detach(), batch["target"].detach()]

    def test_epoch_end(self, test_step_outputs):
        self.device_type = next(self.parameters()).device
        if self.config.fl_local:
            pred = np.concatenate([d[0] for d in test_step_outputs], axis=0)
            pred_map = np.concatenate([d[1] for d in test_step_outputs], axis=0)
            audio_name = np.concatenate([d[2] for d in test_step_outputs], axis=0)
            real_len = np.concatenate([d[3] for d in test_step_outputs], axis=0)
            heatmap_file = os.path.join(self.config.heatmap_dir, self.config.test_file + "_" + str(self.device_type) + ".npy")
            save_npy = [{"audio_name": audio_name[i], "heatmap": pred_map[i], "pred": pred[i], "real_len": real_len[i]} for i in range(len(pred))]
            np.save(heatmap_file, save_npy)
        else:
            pred = torch.cat([d[0] for d in test_step_outputs], dim=0)
            target = torch.cat([d[1] for d in test_step_outputs], dim=0)
            gather_pred = [torch.zeros_like(pred) for _ in range(dist.get_world_size())]
            gather_target = [torch.zeros_like(target) for _ in range(dist.get_world_size())]
            dist.all_gather(gather_pred, pred)
            dist.all_gather(gather_target, target)
            gather_pred = torch.cat(gather_pred, dim=0).cpu().numpy()
            gather_target = torch.cat(gather_target, dim=0).cpu().numpy()
            if self.config.dataset_type == "scv2":
                gather_target = np.argmax(gather_target, 1)
            metric_dict = self.evaluate_metric(gather_pred, gather_target)
            print(self.device_type, dist.get_world_size(), metric_dict, flush=True)
            if self.config.dataset_type == "audioset":
                self.log("mAP", metric_dict["mAP"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)
                self.log("mAUC", metric_dict["mAUC"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)
                self.log("dprime", metric_dict["dprime"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)
            else:
                self.log("acc", metric_dict["acc"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)

    def time_shifting(self, x, shift_len):
        shift_len = int(shift_len)
        new_sample = torch.cat([x[:, shift_len:], x[:, :shift_len]], axis=1)
        return new_sample

    def configure_optimizers(self):
        optimizer = optim.AdamW(
            filter(lambda p: p.requires_grad, self.parameters()),
            lr=self.config.learning_rate, 
            betas=(0.9, 0.999), eps=1e-08, weight_decay=0.05
        )
        def lr_foo(epoch):       
            if epoch < 3:
                lr_scale = self.config.lr_rate[epoch]
            else:
                lr_pos = int(-1 - bisect.bisect_left(self.config.lr_scheduler_epoch, epoch))
                if lr_pos < -3:
                    lr_scale = max(self.config.lr_rate[0] * (0.98 ** epoch), 0.03)
                else:
                    lr_scale = self.config.lr_rate[lr_pos]
            return lr_scale
        scheduler = optim.lr_scheduler.LambdaLR(
            optimizer,
            lr_lambda=lr_foo
        )
        return [optimizer], [scheduler]

class Ensemble_SEDWrapper(pl.LightningModule):
    def __init__(self, sed_models, config, dataset):
        super().__init__()
        self.sed_models = nn.ModuleList(sed_models)
        self.config = config
        self.dataset = dataset

    def evaluate_metric(self, pred, ans):
        if self.config.dataset_type == "audioset":
            mAP = np.mean(average_precision_score(ans, pred, average=None))
            mAUC = np.mean(roc_auc_score(ans, pred, average=None))
            dprime = d_prime(mAUC)
            return {"mAP": mAP, "mAUC": mAUC, "dprime": dprime}
        else:
            acc = accuracy_score(ans, np.argmax(pred, 1))
            return {"acc": acc}
        
    def forward(self, x, sed_index, mix_lambda=None):
        self.sed_models[sed_index].eval()
        preds = [] 
        pred_maps = []
        shift_num = 1 if self.config.fl_local or self.config.dataset_type != "audioset" else 10
        for i in range(shift_num):
            pred, pred_map = self.sed_models[sed_index](x)
            pred_maps.append(pred_map.unsqueeze(0))
            preds.append(pred.unsqueeze(0))
            x = self.time_shifting(x, shift_len=100 * (i + 1))
        preds = torch.cat(preds, dim=0)
        pred_maps = torch.cat(pred_maps, dim=0)
        pred = preds.mean(dim=0)
        pred_map = pred_maps.mean(dim=0)
        return pred, pred_map

    def time_shifting(self, x, shift_len):
        shift_len = int(shift_len)
        new_sample = torch.cat([x[:, shift_len:], x[:, :shift_len]], axis=1)
        return new_sample

    def test_step(self, batch, batch_idx):
        self.device_type = next(self.parameters()).device
        if self.config.fl_local:
            pred = torch.zeros(len(batch["waveform"]), self.config.classes_num).float().to(self.device_type)
            pred_map = torch.zeros(len(batch["waveform"]), 1024, self.config.classes_num).float().to(self.device_type)
            for j in range(len(self.sed_models)):
                temp_pred, temp_pred_map = self(batch["waveform"], j)
                pred = pred + temp_pred
                pred_map = pred_map + temp_pred_map 
            pred = pred / len(self.sed_models)
            pred_map = pred_map / len(self.sed_models)
            return [pred.detach().cpu().numpy(), pred_map.detach().cpu().numpy(), batch["audio_name"], batch["real_len"].cpu().numpy()]
        else:
            pred = torch.zeros(len(batch["waveform"]), self.config.classes_num).float().to(self.device_type)
            for j in range(len(self.sed_models)):
                temp_pred, _ = self(batch["waveform"], j)
                pred = pred + temp_pred
            pred = pred / len(self.sed_models)
            return [pred.detach(), batch["target"].detach()]

    def test_epoch_end(self, test_step_outputs):
        self.device_type = next(self.parameters()).device
        if self.config.fl_local:
            pred = np.concatenate([d[0] for d in test_step_outputs], axis=0)
            pred_map = np.concatenate([d[1] for d in test_step_outputs], axis=0)
            audio_name = np.concatenate([d[2] for d in test_step_outputs], axis=0)
            real_len = np.concatenate([d[3] for d in test_step_outputs], axis=0)
            heatmap_file = os.path.join(self.config.heatmap_dir, self.config.test_file + "_" + str(self.device_type) + ".npy")
            save_npy = [{"audio_name": audio_name[i], "heatmap": pred_map[i], "pred": pred[i], "real_len": real_len[i]} for i in range(len(pred))]
            np.save(heatmap_file, save_npy)
        else:
            pred = torch.cat([d[0] for d in test_step_outputs], dim=0)
            target = torch.cat([d[1] for d in test_step_outputs], dim=0)
            gather_pred = [torch.zeros_like(pred) for _ in range(dist.get_world_size())]
            gather_target = [torch.zeros_like(target) for _ in range(dist.get_world_size())]
            dist.all_gather(gather_pred, pred)
            dist.all_gather(gather_target, target)
            gather_pred = torch.cat(gather_pred, dim=0).cpu().numpy()
            gather_target = torch.cat(gather_target, dim=0).cpu().numpy()
            if self.config.dataset_type == "scv2":
                gather_target = np.argmax(gather_target, 1)
            metric_dict = self.evaluate_metric(gather_pred, gather_target)
            print(self.device_type, dist.get_world_size(), metric_dict, flush=True)
            if self.config.dataset_type == "audioset":
                self.log("mAP", metric_dict["mAP"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)
                self.log("mAUC", metric_dict["mAUC"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)
                self.log("dprime", metric_dict["dprime"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)
            else:
                self.log("acc", metric_dict["acc"] * float(dist.get_world_size()), on_epoch=True, prog_bar=True, sync_dist=True)

    def configure_optimizers(self):
        optimizer = optim.AdamW(
            filter(lambda p: p.requires_grad, self.parameters()),
            lr=self.config.learning_rate, 
            betas=(0.9, 0.999), eps=1e-08, weight_decay=0.05
        )
        def lr_foo(epoch):       
            if epoch < 3:
                lr_scale = self.config.lr_rate[epoch]
            else:
                lr_pos = int(-1 - bisect.bisect_left(self.config.lr_scheduler_epoch, epoch))
                if lr_pos < -3:
                    lr_scale = max(self.config.lr_rate[0] * (0.98 ** epoch), 0.03)
                else:
                    lr_scale = self.config.lr_rate[lr_pos]
            return lr_scale
        scheduler = optim.lr_scheduler.LambdaLR(
            optimizer,
            lr_lambda=lr_foo
        )
        return [optimizer], [scheduler]
