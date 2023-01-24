import os
import time
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from collections import OrderedDict
from einops import rearrange
import tqdm

from .helper import *
from .models import HybridModel
from warmup_scheduler import GradualWarmupScheduler
# pip install git+https://github.com/ildoonet/pytorch-gradual-warmup-lr.git
import shutil
import logging
import math
from .losses import ContrasiveLoss
import pickle


def setup_logger(name, log_file, file_mode, to_console=False, override=False):
    """
            https://stackoverflow.com/questions/11232230/logging-to-two-files-with-different-settings
        To setup as many loggers as you want
    """

    formatter = logging.Formatter('%(message)s')
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if override:
        pass
    elif os.path.isfile(log_file) and file_mode == 'w':
        raise Exception('log file already exists! Use --override-logs to ignore. %s' % (log_file, ))
    handler = logging.FileHandler(log_file, mode=file_mode)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    if to_console:
        logger.addHandler(logging.StreamHandler())

    return logger


def compose_logging(file_mode, model_name, to_console=False, override=False):
    writer = {}
    writer["meta"] = setup_logger("meta",
                                  os.path.join("logs",
                                               "%s_meta.log" % model_name),
                                  file_mode,
                                  to_console=to_console,
                                  override=override)
    writer["data"] = setup_logger("data",
                                  os.path.join("logs",
                                               "%s_data.csv" % model_name),
                                  file_mode,
                                  to_console=to_console,
                                  override=override)
    return writer


def format_results(res):
    line = ""
    for key in sorted(res):
        val = res[key]
        if isinstance(val, str) or isinstance(val, int):
            fmt = "%s: %s\t"
        else:
            fmt = "%s: %8.6f\t"
        line += (fmt % (key, val))
    return line


def reshape_img_batch(img_batch, crop_size, use_features):
    if use_features:
        return img_batch.float()
    else:
        b = img_batch.size(0)
    return img_batch.\
        view(img_batch.size(0), 3, -1, crop_size, crop_size).\
        permute(0, 2, 1, 3, 4).\
        contiguous().\
        view(b, -1, 3, crop_size, crop_size)


def unpack_sample(sample, device):
    imgs, ids, targets, pos, pos_tile, tiles, clusters, pct_valid = list(
        map(lambda x: sample[x], [0, 1, 2, 3, 4, 5, 6, 7]))
    imgs, ids, targets, pos, pos_tile, tiles, clusters, pct_valid = \
        imgs.to(device, non_blocking=True), \
        ids.to(device, non_blocking=True), \
        targets.to(device, non_blocking=True), \
        pos.to(device, non_blocking=True), \
        pos_tile.to(device, non_blocking=True), \
        tiles.to(device, non_blocking=True), \
        clusters.to(device, non_blocking=True), \
        pct_valid.to(device, non_blocking=True)

    return imgs, ids, targets, pos, pos_tile, tiles, clusters, pct_valid


def sample_wsi(df):
    df_id = df.drop_duplicates('id_svs')[['id_patient', 'id_svs']]
    df_id = df_id.groupby('id_patient').sample(1).reset_index(drop=True)
    return df.merge(df_id[['id_svs']], on='id_svs', how='inner')


# helper functions
class _CustomDistributedDataParallel(nn.Module):
    '''
    https://stackoverflow.com/a/56225540
    '''

    def __init__(self, model, device_ids):
        super(_CustomDistributedDataParallel, self).__init__()
        self.model = nn.parallel.DistributedDataParallel(model, device_ids)

    def forward(self, *input):
        return self.model(*input)

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.model.module, name)


# helper functions
class _CustomDataParallel(nn.Module):
    '''
    https://stackoverflow.com/a/56225540
    '''

    def __init__(self, model):
        super(_CustomDataParallel, self).__init__()
        self.model = nn.DataParallel(model)

    def forward(self, *input):
        return self.model(*input)

    def __getattr__(self, name):
        try:
            return super().__getattr__(name)
        except AttributeError:
            return getattr(self.model.module, name)


def get_wd_params(model: nn.Module):
    decay = list()
    no_decay = list()
    for name, param in model.named_parameters():
        if hasattr(param, 'requires_grad') and not param.requires_grad:
            # print(f"{name} skipped")
            continue
        if 'weight' in name and 'norm' not in name and 'bn' not in name:
            decay.append(param)
            # print(f"{name} needs decay")
        else:
            no_decay.append(param)
            # print(f"{name} does not need decay")
    return decay, no_decay


class NullWriter:

    def __init__(self):
        pass

    def info(self, meesage):
        pass


scaler = torch.cuda.amp.GradScaler()


class HybridFitter:

    def __init__(
        self,
        writer=None,
        args=None,
        timestr='',
        num_classes=1,
        model_name='model',
        checkpoint_to_resume='',
        checkpoints_folder='',
        loss_function=None,
    ):
        self.writer = writer
        self.args = args
        self.criterion = loss_function
        self.dataloaders = {}
        self.meta_df = {}
        self.timestr = timestr
        self.meta_ds = {}
        self.model_name = model_name
        self.best_metric = -100 if args.outcome_type == 'mlm' else 0
        self.checkpoints_folder = checkpoints_folder
        self.checkpoint_to_resume = checkpoint_to_resume
        self.num_classes = num_classes
        self.loss_fn = ContrasiveLoss(args)
        self.epoch = 0

    def resume_checkpoint(self):
        if not os.path.isfile(self.checkpoint_to_resume):
            return

        ckp = torch.load(
            self.checkpoint_to_resume,
            map_location='cuda:{}'.format(0 if self.gpu is None else self.gpu))
        self.writer['meta'].info("Loading model checkpoints ... Epoch is %s" %
                                 ckp['epoch'])
        self.epoch = ckp['epoch']

        try:
            del ckp['state_dict_model']['attn.loss_fn.enc_train']
            del ckp['state_dict_model']['attn.loss_fn.enc_val']
        except:
            pass

        try:
            del ckp['state_dict_model']['model.module.attn.loss_fn.enc_train']
            del ckp['state_dict_model']['model.module.attn.loss_fn.enc_val']
        except:
            pass

        if self.args.mode == 'train' and not self.args.resume_train:
            try:
                del ckp['state_dict_model']['model.module.pred.fc.weight']
                del ckp['state_dict_model']['model.module.pred.fc.bias']
            except Exception as e:
                print(e)

        new_state_model = OrderedDict()
        for key, value in ckp['state_dict_model'].items():
            new_state_model[
                f"model.module.{key.replace('model.module.','')}"] = value

        self.model.load_state_dict(new_state_model,
                                   strict=not self.args.resume_fuzzy)
        if 'state_dict_loss' in ckp.keys():
            new_state_loss = ckp['state_dict_loss']
        else:
            new_state_loss = OrderedDict()
            for key, value in new_state_model.items():
                if 'loss_fn' in key:
                    new_state_loss[key.replace('model.module.loss_fn.',
                                               '')] = value
        self.loss_fn.load_state_dict(new_state_loss,
                                     strict=not self.args.resume_fuzzy)

        if self.args.resume_train:
            self.current_epoch = ckp['epoch'] + 1
            self.best_metric = ckp.get('best_metric', -100)
        if self.args.resume_optim:
            self.optimizer.load_state_dict(ckp['state_dict_optimizer'])
            if self.current_epoch <= self.args.warmup_epochs:
                self.scheduler.load_state_dict(ckp['state_dict_scheduler'])
                self.scheduler.step()
            else:
                if 'after_scheduler' in ckp['state_dict_scheduler']:
                    scheduler_ckp = ckp['state_dict_scheduler'][
                        'after_scheduler'].state_dict()
                else:
                    scheduler_ckp = ckp['state_dict_scheduler']
                self.scheduler.load_state_dict(scheduler_ckp)

    def prepare_datasets(self, pickle_file, mode='train', batch_size=256):
        self.batch_size = batch_size
        if isinstance(pickle_file, str):
            _df = pd.read_pickle(pickle_file)
        else:
            _df = pickle_file

        if self.args.visualization:
            self.meta_df[mode] = _df

        elif self.args.sample_patient or self.args.sample_svs:
            n_repeats = 1 if mode == 'val' else self.args.repeats_per_epoch
            res = []
            group_var = 'id_svs' if self.args.sample_svs else 'id_patient'
            for r_i in range(n_repeats):
                # random sample n svs for each patient during each iteration
                # shuffle by group
                _dfi = _df.groupby(group_var, as_index=False).sample(
                    self.args.num_svs,
                    weights=_df.sampling_weights,
                    replace=True).reset_index(drop=True)
                _dfi['repeat'] = r_i
                _dfi['group'] = _dfi.index // self.args.num_svs
                grps = _dfi['group'].unique()
                np.random.shuffle(grps)
                _dfg = pd.DataFrame(grps, columns=['group'])
                res.append(_dfg.merge(_dfi, on='group'))
            self.meta_df[mode] = pd.concat(res).reset_index(drop=True)

            if mode == 'predict':
                self.meta_df[mode] = self.meta_df[mode].sort_values(
                    group_var).reset_index(drop=True)
                if self.args.visualization:
                    self.meta_df[mode] = self.meta_df[mode].iloc[:20]
        else:
            n_repeats = self.args.num_val if mode == 'val' else self.args.repeats_per_epoch
            self.meta_df[mode] = _df.loc[_df.index.repeat(n_repeats)].sample(
                frac=1.0).reset_index(drop=True)

    def get_datasets(
        self,
        pickle_file,
        mode='train',
        epoch=1,
        batch_size=1
    ):
        transform = get_data_transforms()[mode]
        ds = SlidesDataset(data_file=pickle_file,
                           outcome=self.args.outcome,
                           writer=self.writer,
                           mode=mode,
                           args=self.args,
                           n_tiles=self.args.repeats_per_svs *
                           (1 if mode == 'train' else self.args.num_val),
                           transforms=transform)

        train_sampler = None

        dl = torch.utils.data.DataLoader(
            ds,
            shuffle=False,
            batch_size=batch_size,
            num_workers=self.args.num_workers,
            # pin_memory=True,
            drop_last=False,
            sampler=train_sampler)
        self.dataloaders[mode] = dl

    def reset_optimizer(self):

        assert self.args.optimizer in ['adam', 'adamw', 'sgd']

        if self.args.optimizer == 'adam':
            optimizer = optim.Adam
        elif self.args.optimizer == 'adamw':
            optimizer = optim.AdamW
        elif self.args.optimizer == 'sgd':
            optimizer = optim.SGD

        backbone_decay, backbone_no_decay = get_wd_params(self.model.backbone)
        attn_decay, attn_no_decay = get_wd_params(self.model.attn)
        fuse_decay, fuse_no_decay = get_wd_params(self.model.fuse)
        loss_decay, loss_no_decay = get_wd_params(self.loss_fn)
        pred_decay, pred_no_decay = get_wd_params(self.model.pred)

        self.optimizer = optimizer([{
            'params': attn_decay + backbone_decay,
            'lr': self.args.lr_attn,
            'weight_decay': self.args.wd_attn
        }, {
            'params': attn_no_decay + backbone_no_decay,
            'lr': self.args.lr_attn,
            'weight_decay': 0
        }, {
            'params': pred_decay,
            'lr': self.args.lr_pred,
            'weight_decay': self.args.wd_pred
        }, {
            'params': pred_no_decay,
            'lr': self.args.lr_pred,
            'weight_decay': 0
        }, {
            'params': fuse_decay,
            'lr': self.args.lr_fuse,
            'weight_decay': self.args.wd_fuse
        }, {
            'params': fuse_no_decay,
            'lr': self.args.lr_fuse,
            'weight_decay': 0
        }, {
            'params': loss_decay,
            'lr': self.args.lr_loss,
            'weight_decay': self.args.wd_loss
        }, {
            'params': loss_no_decay,
            'lr': self.args.lr_loss,
            'weight_decay': 0
        }])
        base_scheduler = lr_scheduler.CosineAnnealingWarmRestarts(
            self.optimizer,
            T_0=self.args.anneal_freq,
            T_mult=self.args.t_mult,
            eta_min=0)

        if self.args.warmup_epochs == 0:
            self.scheduler = base_scheduler
        else:
            self.scheduler = GradualWarmupScheduler(
                self.optimizer,
                multiplier=1,
                total_epoch=self.args.warmup_epochs,
                after_scheduler=base_scheduler)

    def train(self, df_train=None, epoch=0):
        tiles_per_sample = self.args.num_svs
        # print("Preparing training data ...")
        self.prepare_datasets(df_train,
                              'train',
                              batch_size=self.args.batch_size *
                              self.args.num_patches)

        self.get_datasets(self.meta_df['train'], mode='train', epoch=1, batch_size=self.args.batch_size)

        self.writer['meta'].info('Training from step %s' % epoch)
        # training phase
        self.model.train()
        self.loss_fn.train()
        # self.model.backbone.eval()

        batch_time = AverageMeter('Time', ':6.3f')
        perfs = AverageMeter(self.metric, ':5.4f')
        data_time = AverageMeter('Data', ':6.3f')
        losses0 = AverageMeter('Loss All', ':6.3f')
        losses1 = AverageMeter('Loss 1', ':6.3f')
        losses2 = AverageMeter('Loss 2', ':6.3f')

        progress = ProgressMeter(
            len(self.dataloaders['train']),
            [batch_time, data_time, losses1, losses2, losses0, perfs],
            prefix="Epoch: [{}]".format(epoch),
            writer=self.writer['meta'],
            verbose=True)
        end = time.time()

        eval_t = ModelEvaluation(outcome_type=self.args.outcome_type,
                                 loss_function=self.criterion,
                                 mode='train',
                                 device=self.device,
                                 timestr=self.timestr)

        for group in self.optimizer.param_groups:
            current_lr = group['lr']
            # self.writer['meta'].info("Learning rate is %s" % (current_lr))
        self.writer['meta'].info('-' * 30)

        # print("Start training loop ...")
        # train over all training data
        for i, sample in enumerate(self.dataloaders['train']):
            imgs, ids, targets, pos, pos_tile, tiles, clusters, pct_valid = unpack_sample(
                sample, self.device)
            targets = rearrange(targets,
                                '(b n) d -> b n d',
                                n=tiles_per_sample)[:, 0, :]

            inputs = {
                'x': imgs,
                'ids': ids,
                'pos': pos,
                'pos_tile': pos_tile,
                'tiles': tiles,
                'clusters': clusters,
                'pct_valid': pct_valid,
                'n_tiles': self.args.repeats_per_svs
            }

            if self.args.outcome_type == 'survival':
                if targets[:, 1].sum() == 0:
                    continue

            data_time.update(time.time() - end)

            # forward and backprop
            self.optimizer.zero_grad()
            with torch.set_grad_enabled(True):
                outputs = self.model(inputs)
                preds = outputs['out']
                attn_loss_seq, attn_loss_cls = self.loss_fn(outputs)
                loss = self.criterion.calculate(outputs['out'], targets)
                attn_loss_seq = attn_loss_seq.mean()
                attn_loss_cls = attn_loss_cls.mean()
                loss += attn_loss_seq
                loss += attn_loss_cls
                if torch.isnan(loss):
                    print('null loss', attn_loss_cls, attn_loss_seq)
                    continue

            ids = rearrange(ids, '(b n) -> b n',
                            n=tiles_per_sample)[:, 0]
            nbatches = imgs.size(0) // tiles_per_sample
            loss.backward()
            self.optimizer.step()

            if self.args.outcome_type == 'mlm':
                pass
            else:
                eval_t.update({
                    "ids": ids.view(-1, 1),
                    "preds": preds,
                    "targets": targets
                })

            # update metrics
            losses1.update(attn_loss_seq.item(), nbatches)
            losses2.update(attn_loss_cls.item(), nbatches)
            losses0.update(loss.item(), nbatches)

            if self.args.outcome_type == 'mlm':
                metrics = {'loss': 0}
            else:
                metrics = calculate_metrics(
                    ids=ids.view(nbatches, -1).cpu().numpy(),
                    preds=preds.data.cpu().numpy(),
                    targets=targets.data.cpu().numpy(),
                    outcome_type=self.args.outcome_type)
            perfs.update(metrics[self.metric], nbatches)

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if (i + 1) % self.args.log_freq == 0:
                progress.display(i + 1)

        if self.args.outcome_type == 'mlm':
            res = {
                'loss': losses0.get_avg(),
                'loss_pt1': losses1.get_avg(),
                'loss_pt2': losses2.get_avg(),
            }
        else:
            res = eval_t.evaluate()
        res['epoch'] = epoch
        res['mode'] = 'train'
        return res

    def evaluate(self, df_val, epoch=0):
        tiles_per_sample = self.args.num_svs
        # print("Preparing val data ...")
        self.prepare_datasets(df_val, 'val', batch_size=self.args.batch_size)
        self.get_datasets(self.meta_df['val'], 'val', batch_size=self.args.batch_size // self.args.num_val)

        # validation phase
        self.model.eval()
        self.loss_fn.eval()
        # self.model.train()
        eval_v = ModelEvaluation(outcome_type=self.args.outcome_type,
                                 loss_function=self.criterion,
                                 mode='val',
                                 device=self.device,
                                 timestr=self.timestr)

        # forward prop over all validation data
        losses0 = 0
        losses1 = 0
        losses2 = 0

        counts = 0
        # print("Start val loop ...")
        for i, sample in enumerate(tqdm.tqdm(self.dataloaders['val'])):
            imgs, ids, targets, pos, pos_tile, tiles, clusters, pct_valid = unpack_sample(
                sample, self.device)
            inputs = {
                'x': imgs,
                'ids': ids,
                'pos': pos,
                'pos_tile': pos_tile,
                'tiles': tiles,
                'clusters': clusters,
                'pct_valid': pct_valid,
                'n_tiles': self.args.repeats_per_svs * self.args.num_val
            }

            targets = rearrange(targets,
                                '(b n) d -> b n d',
                                n=tiles_per_sample)[:, 0, :]
            # forward
            with torch.set_grad_enabled(False):
                outputs = self.model(inputs)
                preds = outputs['out']
                attn_loss_seq, attn_loss_cls = self.loss_fn(outputs)
                attn_loss_seq = attn_loss_seq.mean()
                attn_loss_cls = attn_loss_cls.mean()

            ids = rearrange(ids, '(b n) -> b n',
                            n=tiles_per_sample)[:, 0]
            nbatches = imgs.size(0) // tiles_per_sample

            if self.args.outcome_type == 'mlm':
                counts += 1
                loss = self.criterion.calculate(outputs['out'], targets)
                losses0 += (attn_loss_seq + attn_loss_cls + loss).item()
                losses1 += attn_loss_seq.mean().item()
                losses2 += attn_loss_cls.mean().item()
            else:
                eval_v.update({
                    "ids": ids.view(-1, 1),
                    "preds": preds,
                    "targets": targets
                })

        if self.args.outcome_type == 'mlm':
            res = {
                'loss': losses0 / counts,
                'loss_pt1': losses1 / counts,
                'loss_pt2': losses2 / counts
            }
        else:
            res = eval_v.evaluate()
            pred_file = f"predictions/{self.model_name}-predictions.csv"
            os.makedirs(os.path.dirname(pred_file), exist_ok=True)
            eval_v.save(pred_file)

        res['epoch'] = epoch
        res['mode'] = 'val'
        return res

    def fit_epoch(self, data_dict, epoch=0):
        print(self.model_name)
        if epoch == 1:
            self.scheduler.step()

        # print("Start the training epoch ....")
        train_res = self.train(data_dict['train'], epoch=epoch)
        # print("Start the evaluation epoch ....")
        val_res = self.evaluate(data_dict['val'], epoch=epoch)

        self.writer['data'].info(format_results(train_res))
        self.writer['data'].info(format_results(val_res))

        ########################################
        # schedule step
        ########################################

        if self.args.outcome_type == 'survival':
            performance_measure = torch.tensor(val_res['c-index'])
        elif self.args.outcome_type == 'classification':
            performance_measure = torch.tensor(val_res['auc'])
        elif self.args.outcome_type == 'regression':
            performance_measure = torch.tensor(val_res['r2'])
        elif self.args.outcome_type == 'mlm':
            performance_measure = -torch.tensor(val_res['loss'])
        performance_measure = performance_measure.item()

        self.scheduler.step()

        is_best = False
        epoch_start_monitoring = 0
        if performance_measure > self.best_metric and epoch >= epoch_start_monitoring:
            self.best_metric = performance_measure
            is_best = True

        if not self.args.not_save:
            self.save_checkpoint(epoch=epoch,
                                 is_best=is_best,
                                 save_freq=self.args.save_interval,
                                 checkpoints_folder=self.checkpoints_folder)

        if epoch >= epoch_start_monitoring and not math.isnan(
                performance_measure):
            if self.es is not None and self.es.step(performance_measure):
                return 1  # early stop criterion is met, we can stop now

        return 0

    def extract_features(self, df_val, epoch=0):
        tiles_per_sample = self.args.num_svs
        # print("Preparing val data ...")
        self.prepare_datasets(df_val,
                              'predict',
                              batch_size=self.args.batch_size // self.args.num_val)
        self.get_datasets(self.meta_df['predict'], 'predict', batch_size=self.args.batch_size // self.args.num_val)

        # validation phase
        self.model.eval()
        self.loss_fn.eval()

        # forward prop over all validation data
        counts = 0
        # print("Start extraction loop ...")
        for i, sample in enumerate(tqdm.tqdm(self.dataloaders['predict'])):
            imgs, ids, targets, pos, pos_tile, tiles, clusters, pct_valid = unpack_sample(
                sample, self.device)
            inputs = {
                'x': imgs,
                'ids': ids,
                'pos': pos,
                'pos_tile': pos_tile,
                'tiles': tiles,
                'clusters': clusters,
                'pct_valid': pct_valid,
                'n_tiles': self.args.repeats_per_svs * self.args.num_val
            }

            # forward
            with torch.set_grad_enabled(False):
                outputs = self.model(inputs)

            ids = rearrange(ids, '(b n) -> b n',
                            n=tiles_per_sample)[:, 0]

            res = {
                'ids': ids.cpu().detach(),
                'cls': enc_cls.cpu().detach(),
                'pos_tile': pos_tile.cpu().detach(),
                'pos': pos.cpu().detach(),
                'attn': info['attn'].cpu().detach(),
                'dots': info['dots'],
                'enc_seq': enc_seq.cpu().detach(),
                'org_seq': org_seq.cpu().detach()
            }

            file_id = res['ids'].item()
            pos_x, pos_y = res['pos_tile'].squeeze().tolist()

            subdir = f"{self.args.timestr}-{self.args.resume}/{self.args.offset}"
            save_loc = f"features/{subdir}/{self.args.cancer}/{self.epoch:04d}/{file_id}-{pos_x}-{pos_y}.pickle"
            os.makedirs(os.path.dirname(save_loc), exist_ok=True)
            with open(save_loc, 'wb') as f:
                pickle.dump(res, f)

    def save_checkpoint(self, epoch, is_best, save_freq, checkpoints_folder):

        state_dict = {
            'epoch': epoch,
            'state_dict_model': self.model.state_dict(),
            'state_dict_loss': self.loss_fn.state_dict(),
            'state_dict_optimizer': self.optimizer.state_dict(),
            'state_dict_scheduler': self.scheduler.state_dict(),
            'best_metric': self.best_metric,
        }
        # remaining things related to training
        os.makedirs(checkpoints_folder, exist_ok=True)
        epoch_output_path = os.path.join(checkpoints_folder, "LAST.pt")

        if os.path.isfile(
                epoch_output_path) and self.args.outcome_type == 'mlm':
            shutil.copyfile(epoch_output_path,
                            epoch_output_path.replace('LAST.pt', 'LAST-1.pt'))
        torch.save(state_dict, epoch_output_path)

        if is_best:
            print("Saving new best result!")
            fname_best = os.path.join(checkpoints_folder, "BEST.pt")
            if os.path.isfile(fname_best):
                os.remove(fname_best)
            shutil.copy(epoch_output_path, fname_best)

        if epoch % save_freq == 0:
            # print("Saving new checkpoints!")
            shutil.copy(epoch_output_path,
                        os.path.join(checkpoints_folder, "%04d.pt" % epoch))

    def get_logger(self, primary_worker):
        file_mode = 'a' if self.args.resume_train or self.args.mode == 'extract' else 'w'
        if primary_worker:
            # print("This is primary worker")
            self.writer = compose_logging(file_mode,
                                          self.model_name,
                                          to_console=True,
                                          override=self.args.override_logs)
            for arg, value in sorted(vars(self.args).items()):
                self.writer['meta'].info("Argument %s: %r", arg, value)
        else:
            self.writer = {}
            self.writer['meta'] = NullWriter()
            self.writer['data'] = NullWriter()

    def fit(self, gpu, data_dict, procedure='train'):
        self.gpu = gpu
        if torch.cuda.is_available():
            self.device = torch.device(
                'cuda:0' if gpu is None else f'cuda:{gpu}')
        else:
            self.device = torch.device('cpu')

        self.current_epoch = 1
        metrics = {
            'classification': 'auc',
            'survival': 'c-index',
            'regression': 'r2',
            'mlm': 'loss'
        }
        self.metric = metrics[self.args.outcome_type]

        if gpu is not None:
            print("Use GPU: {} for training".format(gpu))

        self.es = EarlyStopping(patience=self.args.patience, mode='max')

        self.get_logger(primary_worker=True)

        model = HybridModel(in_dim=self.args.num_features,
                            out_dim=self.num_classes,
                            dropout=self.args.dropout,
                            args=self.args,
                            model_name=self.model_name,
                            outcome_type=self.args.outcome_type)

        if not torch.cuda.is_available():
            print('using CPU, this will be slow')
        elif gpu is not None:
            # print("training on single gpu")
            torch.cuda.set_device(gpu)
            # model = model.cuda(gpu)
            model = _CustomDataParallel(model).cuda()
        else:
            model = _CustomDataParallel(model).cuda()

        if not torch.cuda.is_available():
            pass
        else:
            self.loss_fn.cuda()

        self.model = model
        self.reset_optimizer()
        self.resume_checkpoint()

        if procedure == 'train':
            # print("start training ... ")
            for epoch in range(self.current_epoch, self.args.epochs + 1):
                return_code = self.fit_epoch(data_dict, epoch=epoch)
                if return_code:
                    break
        elif procedure == 'test':
            res = []
            for i in range(self.args.num_repeats):
                info_str = self.evaluate(data_dict['val'], epoch=0)
                self.writer['meta'].info(info_str)
                res.append(info_str)

            df_sum = pd.DataFrame(res)
            df_sum.drop(['mode'], axis=1, inplace=True)
            self.writer['meta'].info("Average prediction")
            self.writer['meta'].info(df_sum.mean().to_dict())
            self.writer['meta'].info("Standard deviation")
            self.writer['meta'].info(df_sum.std().to_dict())
            self.writer['data'].info(df_sum.to_csv())

        elif procedure == 'extract':
            subdir = f"{self.args.timestr}-{self.args.resume}/{self.args.offset}"
            save_loc = f"features/{subdir}/{self.args.cancer}/{self.epoch:04d}/meta.pickle"
            os.makedirs(os.path.dirname(save_loc), exist_ok=True)
            data_dict['val'].to_pickle(save_loc)
            print('=' * 30)
            self.extract_features(data_dict['val'])