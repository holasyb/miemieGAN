#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# Copyright (c) Megvii, Inc. and its affiliates.

import datetime
import os
import cv2
import math
import time
import numpy as np
from loguru import logger

import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter

from mmgan.data import DataPrefetcher, StyleGANv2ADADataPrefetcher
from mmgan.data.data_prefetcher import FCOSDataPrefetcher
from mmgan.utils import (
    MeterBuffer,
    ModelEMA,
    all_reduce_norm,
    get_local_rank,
    get_model_info,
    get_rank,
    get_world_size,
    gpu_mem_usage,
    is_parallel,
    load_ckpt,
    occupy_mem,
    save_checkpoint,
    setup_logger,
    synchronize
)


class Trainer:
    def __init__(self, exp, args):
        # init function only defines some basic attr, other attrs like model, optimizer are built in
        # before_train methods.
        self.exp = exp
        # 算法名字
        self.archi_name = self.exp.archi_name
        self.args = args

        # training related attr
        self.amp_training = args.fp16
        self.scaler = torch.cuda.amp.GradScaler(enabled=args.fp16)
        self.is_distributed = get_world_size() > 1
        self.rank = get_rank()
        self.local_rank = get_local_rank()
        self.device = "cuda:{}".format(self.local_rank)

        # data/dataloader related attr
        self.data_type = torch.float16 if args.fp16 else torch.float32

        # metric record
        self.meter = MeterBuffer(window_size=exp.print_interval)
        self.file_name = os.path.join(exp.output_dir, args.experiment_name)

        if self.rank == 0:
            os.makedirs(self.file_name, exist_ok=True)

        setup_logger(
            self.file_name,
            distributed_rank=self.rank,
            filename="train_log.txt",
            mode="a",
        )

    def train(self):
        self.before_train()
        try:
            self.train_in_epoch()
        except Exception:
            raise
        finally:
            self.after_train()

    def train_in_epoch(self):
        for self.epoch in range(self.start_epoch, self.max_epoch):
            self.before_epoch()
            self.train_in_iter()
            self.after_epoch()

    def train_in_iter(self):
        for self.iter in range(self.max_iter):
            self.before_iter()
            self.train_one_iter()
            self.after_iter()

    def train_one_iter(self):
        iter_start_time = time.time()
        if self.archi_name == 'StyleGANv2ADA' or self.archi_name == 'StyleGANv3':
            # StyleGANv2ADA不使用混合精度训练，所以训练代码只写了FP32的情况。
            phase_real_img, phase_real_c, phases_all_gen_c = self.prefetcher.next()
            phase_real_img = phase_real_img.to(self.data_type)
            phase_real_c = phase_real_c.to(self.data_type)
            phases_all_gen_c = [x.to(self.data_type) for x in phases_all_gen_c]
            phase_real_img.requires_grad = False
            phase_real_c.requires_grad = False
            for x in phases_all_gen_c:
                x.requires_grad = False
            data_end_time = time.time()

            data = [phase_real_img, phase_real_c, phases_all_gen_c]
            self.model.setup_input(data)
            outputs = self.model.train_iter(self.optimizers)

            iter_end_time = time.time()
            # 删去所有loss的键值对，避免打印loss时出现None错误。
            if (self.iter + 1) % self.exp.print_interval == 0:
                loss_meter = self.meter.get_filtered_meter("loss")
                for key in loss_meter.keys():
                    del self.meter[key]
            self.meter.update(
                iter_time=iter_end_time - iter_start_time,
                data_time=data_end_time - iter_start_time,
                lr=self.base_lr_G,
                **outputs,
            )
        else:
            raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))


    def before_train(self):
        logger.info("args: {}".format(self.args))
        logger.info("exp value:\n{}".format(self.exp))

        # model related init
        torch.cuda.set_device(self.local_rank)
        if self.archi_name == 'StyleGANv2ADA':
            model = self.exp.get_model()
        elif self.archi_name == 'StyleGANv3':
            model = self.exp.get_model(self.args.batch_size)
        else:
            raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))
        # logger.info("Model Summary: {}".format(get_model_info(self.archi_name, model, self.exp.test_size)))
        model.to(self.device)

        # 是否进行梯度裁剪
        self.need_clip = False

        if self.archi_name == 'StyleGANv2ADA':
            learning_rate = self.exp.basic_lr_per_img * self.args.batch_size
            beta1 = self.exp.optimizer_cfg['generator']['beta1']
            beta2 = self.exp.optimizer_cfg['generator']['beta2']

            G_reg_interval = self.exp.G_reg_interval
            D_reg_interval = self.exp.D_reg_interval

            for name, reg_interval in [('G', G_reg_interval), ('D', D_reg_interval)]:
                if reg_interval is None:
                    pass
                    # opt = dnnlib.util.construct_class_by_name(params=module.parameters(),
                    #                                           **opt_kwargs)  # subclass of torch.optim.Optimizer
                    # phases += [dnnlib.EasyDict(name=name + 'both', module=module, opt=opt, interval=1)]
                else:  # Lazy regularization.
                    mb_ratio = reg_interval / (reg_interval + 1)
                    new_lr = learning_rate * mb_ratio
                    new_beta1 = beta1 ** mb_ratio
                    new_beta2 = beta2 ** mb_ratio
                if name == 'G':
                    self.base_lr_G = new_lr
                    self.exp.optimizer_cfg['generator']['beta1'] = new_beta1
                    self.exp.optimizer_cfg['generator']['beta2'] = new_beta2
                elif name == 'D':
                    self.base_lr_D = new_lr
                    self.exp.optimizer_cfg['discriminator']['beta1'] = new_beta1
                    self.exp.optimizer_cfg['discriminator']['beta2'] = new_beta2

            # solver related init
            self.optimizers = {}
            self.optimizer_G = self.exp.get_optimizer(self.base_lr_G, 'G')
            self.optimizer_D = self.exp.get_optimizer(self.base_lr_D, 'D')
            self.optimizers['optimizer_G'] = self.optimizer_G
            self.optimizers['optimizer_D'] = self.optimizer_D

            # value of epoch will be set in `resume_train`
            model = self.resume_train(model)


            self.train_loader = self.exp.get_data_loader(
                batch_size=self.args.batch_size,
                is_distributed=self.is_distributed,
                cache_img=self.args.cache,
            )
            # 一轮的步数。
            train_steps = self.exp.dataset.train_steps
            # 一轮的图片数。
            one_epoch_imgs = train_steps * self.args.batch_size
            # 算出需要的训练轮数并写入。
            self.exp.max_epoch = self.exp.kimgs * 1000 // one_epoch_imgs
            if self.exp.kimgs * 1000 % one_epoch_imgs != 0:
                self.exp.max_epoch += 1
            self.max_epoch = self.exp.max_epoch

            logger.info("init prefetcher, this might take one minute or less...")
            self.prefetcher = StyleGANv2ADADataPrefetcher(self.train_loader)

            self.test_loader = self.exp.get_eval_loader(
                batch_size=self.args.eval_batch_size,
                is_distributed=self.is_distributed,
            )

            # max_iter means iters per epoch
            self.max_iter = len(self.train_loader)
        elif self.archi_name == 'StyleGANv3':
            learning_rate_g = self.exp.basic_glr_per_img * self.args.batch_size
            learning_rate_d = self.exp.basic_dlr_per_img * self.args.batch_size
            beta1 = self.exp.optimizer_cfg['generator']['beta1']
            beta2 = self.exp.optimizer_cfg['generator']['beta2']

            G_reg_interval = self.exp.G_reg_interval
            D_reg_interval = self.exp.D_reg_interval

            for name, reg_interval in [('G', G_reg_interval), ('D', D_reg_interval)]:
                if reg_interval is None:
                    if name == 'G':
                        self.base_lr_G = learning_rate_g
                    elif name == 'D':
                        self.base_lr_D = learning_rate_d
                else:  # Lazy regularization.
                    if name == 'G':
                        mb_ratio = reg_interval / (reg_interval + 1)
                        new_lr = learning_rate_g * mb_ratio
                        new_beta1 = beta1 ** mb_ratio
                        new_beta2 = beta2 ** mb_ratio

                        self.base_lr_G = new_lr
                        self.exp.optimizer_cfg['generator']['beta1'] = new_beta1
                        self.exp.optimizer_cfg['generator']['beta2'] = new_beta2
                    elif name == 'D':
                        mb_ratio = reg_interval / (reg_interval + 1)
                        new_lr = learning_rate_d * mb_ratio
                        new_beta1 = beta1 ** mb_ratio
                        new_beta2 = beta2 ** mb_ratio

                        self.base_lr_D = new_lr
                        self.exp.optimizer_cfg['discriminator']['beta1'] = new_beta1
                        self.exp.optimizer_cfg['discriminator']['beta2'] = new_beta2

            # solver related init
            self.optimizers = {}
            self.optimizer_G = self.exp.get_optimizer(self.base_lr_G, 'G')
            self.optimizer_D = self.exp.get_optimizer(self.base_lr_D, 'D')
            self.optimizers['optimizer_G'] = self.optimizer_G
            self.optimizers['optimizer_D'] = self.optimizer_D

            # value of epoch will be set in `resume_train`
            model = self.resume_train(model)
            resume = False
            if self.args.resume:
                resume = True
            else:
                if self.args.ckpt is not None:
                    resume = True
            if resume:
                # 需要修改配置
                model.ada_kimg = 100        # Make ADA react faster at the beginning.
                model.ema_rampup = None     # Disable EMA rampup.
                model.blur_init_sigma = 0   # Disable blur rampup.


            self.train_loader = self.exp.get_data_loader(
                batch_size=self.args.batch_size,
                is_distributed=self.is_distributed,
                cache_img=self.args.cache,
            )
            # 一轮的步数。
            train_steps = self.exp.dataset.train_steps
            # 一轮的图片数。
            one_epoch_imgs = train_steps * self.args.batch_size
            # 算出需要的训练轮数并写入。
            self.exp.max_epoch = self.exp.kimgs * 1000 // one_epoch_imgs
            if self.exp.kimgs * 1000 % one_epoch_imgs != 0:
                self.exp.max_epoch += 1
            self.max_epoch = self.exp.max_epoch

            logger.info("init prefetcher, this might take one minute or less...")
            self.prefetcher = StyleGANv2ADADataPrefetcher(self.train_loader)

            self.test_loader = self.exp.get_eval_loader(
                batch_size=self.args.eval_batch_size,
                is_distributed=self.is_distributed,
            )

            # max_iter means iters per epoch
            self.max_iter = len(self.train_loader)
        else:
            raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))

        if self.args.occupy:
            occupy_mem(self.local_rank)

        if self.is_distributed:
            model = DDP(model, device_ids=[self.local_rank], broadcast_buffers=False)

        self.model = model
        self.model.train()

        self.evaluator = self.exp.get_evaluator(
            batch_size=self.args.eval_batch_size, is_distributed=self.is_distributed
        )

        # Tensorboard logger
        if self.rank == 0:
            self.tblogger = SummaryWriter(self.file_name)

        logger.info("Training start...")
        if self.archi_name == 'StyleGANv2ADA' or self.archi_name == 'StyleGANv3':
            for name, module in [('synthesis', model.synthesis), ('mapping', model.mapping), ('discriminator', model.discriminator)]:
                trainable_params = 0
                nontrainable_params = 0
                for name_, param_ in module.named_parameters():
                    mul = np.prod(param_.shape)
                    if name == 'synthesis':
                        freeze = False
                        for freeze_name in self.exp.synthesis_freeze_at:
                            if freeze_name in name_:
                                freeze = True
                                break
                        if not freeze:
                            trainable_params += mul
                        else:
                            nontrainable_params += mul
                    elif name == 'discriminator':
                        freeze = False
                        for freeze_name in self.exp.discriminator_freeze_at:
                            if freeze_name in name_:
                                freeze = True
                                break
                        if not freeze:
                            trainable_params += mul
                        else:
                            nontrainable_params += mul
                    else:
                        if param_.requires_grad is True:
                            trainable_params += mul
                        else:
                            nontrainable_params += mul
                trainable_params = int(trainable_params)
                nontrainable_params = int(nontrainable_params)
                total_params = trainable_params + nontrainable_params
                logger.info('StyleGANv2ADA(v3) %s Total params: %s' % (name, format(total_params, ",")))
                logger.info('StyleGANv2ADA(v3) %s Trainable params: %s' % (name, format(trainable_params, ",")))
                logger.info('StyleGANv2ADA(v3) %s Non-trainable params: %s' % (name, format(nontrainable_params, ",")))
        else:
            raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))

    def after_train(self):
        logger.info(
            "Training of experiment is done."
        )

    def before_epoch(self):
        logger.info("---> start train epoch{}".format(self.epoch + 1))
        if self.archi_name == 'StyleGANv2ADA':
            self.train_loader.dataset.set_epoch(self.epoch)
        elif self.archi_name == 'StyleGANv3':
            self.train_loader.dataset.set_epoch(self.epoch)
        else:
            raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))

    def after_epoch(self):
        self.save_ckpt(ckpt_name="%d" % (self.epoch + 1))

        if (self.epoch + 1) % self.exp.eval_interval == 0:
            self.model.eval()
            self.stylegan_generate_imgs()
            self.model.train()

    def stylegan_generate_imgs(self):
        if self.archi_name == 'StyleGANv2ADA' or self.archi_name == 'StyleGANv3':
            for seed_idx, data in enumerate(self.test_loader):
                for k, v in data.items():
                    data[k] = v.cuda()
                self.model.setup_input(data)
                with torch.no_grad():
                    img_bgr = self.model.test_iter()
                    save_folder = os.path.join(self.file_name, 'snapshot_imgs')
                    os.makedirs(save_folder, exist_ok=True)
                    save_file_name = os.path.join(save_folder, f'epoch{self.epoch + 1:08d}_seedidx{seed_idx:08d}.png')
                    logger.info("Saving generation result in {}".format(save_file_name))
                    cv2.imwrite(save_file_name, img_bgr)
        else:
            raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))

    def before_iter(self):
        pass

    def after_iter(self):
        """
        `after_iter` contains two parts of logic:
            * log information
            * reset setting of resize
        """
        # log needed information
        if (self.iter + 1) % self.exp.print_interval == 0:
            # TODO check ETA logic
            left_iters = self.max_iter * self.max_epoch - (self.progress_in_iter + 1)
            eta_seconds = self.meter["iter_time"].global_avg * left_iters
            eta_str = "ETA: {}".format(datetime.timedelta(seconds=int(eta_seconds)))

            progress_str = "epoch: {}/{}, iter: {}/{}".format(
                self.epoch + 1, self.max_epoch, self.iter + 1, self.max_iter
            )
            loss_meter = self.meter.get_filtered_meter("loss")
            loss_str = ", ".join(
                ["{}: {:.1f}".format(k, v.latest) for k, v in loss_meter.items()]
            )

            time_meter = self.meter.get_filtered_meter("time")
            time_str = ", ".join(
                ["{}: {:.3f}s".format(k, v.avg) for k, v in time_meter.items()]
            )

            log_msg = "{}, mem: {:.0f}Mb, {}, {}, lr: {:.6f}".format(progress_str, gpu_mem_usage(), time_str, loss_str, self.meter["lr"].latest, )
            log_msg += (", {}".format(eta_str))
            logger.info(log_msg)
            self.meter.clear_meters()
        if (self.iter + 1) % self.exp.temp_img_interval == 0:
            self.model.eval()
            self.stylegan_generate_imgs()
            self.model.train()

    @property
    def progress_in_iter(self):
        return self.epoch * self.max_iter + self.iter

    def resume_train(self, model):
        if self.args.resume:
            logger.info("resume training")
            if self.args.ckpt is None:
                ckpt_file = os.path.join(self.file_name, "latest" + "_ckpt.pth")
            else:
                ckpt_file = self.args.ckpt

            ckpt = torch.load(ckpt_file, map_location=self.device)
            # resume the model/optimizer state dict
            model.load_state_dict(ckpt["model"])
            if self.archi_name == 'StyleGANv2ADA' or self.archi_name == 'StyleGANv3':
                self.optimizer_G.load_state_dict(ckpt["optimizer_G"])
                self.optimizer_D.load_state_dict(ckpt["optimizer_D"])
            else:
                raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))
            # resume the training states variables
            start_epoch = ckpt["start_epoch"]
            self.start_epoch = start_epoch
            logger.info(
                "loaded checkpoint '{}' (epoch {})".format(
                    self.args.resume, self.start_epoch
                )
            )  # noqa
        else:
            if self.args.ckpt is not None:
                logger.info("loading checkpoint for fine tuning")
                ckpt_file = self.args.ckpt
                ckpt = torch.load(ckpt_file, map_location=self.device)["model"]
                model = load_ckpt(model, ckpt)
            self.start_epoch = 0

        return model

    def evaluate_and_save_model(self):
        evalmodel = self.model
        if is_parallel(evalmodel):
            evalmodel = evalmodel.module

        ap50_95, ap50, summary = self.exp.eval(
            evalmodel, self.evaluator, self.is_distributed
        )
        self.model.train()
        if self.rank == 0:
            self.tblogger.add_scalar("val/COCOAP50", ap50, self.epoch + 1)
            self.tblogger.add_scalar("val/COCOAP50_95", ap50_95, self.epoch + 1)
            logger.info("\n" + summary)
        synchronize()

        self.save_ckpt("last_epoch", ap50_95 > self.best_ap)
        self.best_ap = max(self.best_ap, ap50_95)

    def save_ckpt(self, ckpt_name, update_best_ckpt=False):
        if self.rank == 0:
            save_model = self.model
            logger.info("Save weights to {}".format(self.file_name))
            if self.archi_name == 'StyleGANv2ADA' or self.archi_name == 'StyleGANv3':
                ckpt_state = {
                    "start_epoch": self.epoch + 1,
                    "model": save_model.state_dict(),
                    "optimizer_G": self.optimizer_G.state_dict(),
                    "optimizer_D": self.optimizer_D.state_dict(),
                }
            else:
                raise NotImplementedError("Architectures \'{}\' is not implemented.".format(self.archi_name))
            save_checkpoint(
                ckpt_state,
                update_best_ckpt,
                self.file_name,
                ckpt_name,
            )
