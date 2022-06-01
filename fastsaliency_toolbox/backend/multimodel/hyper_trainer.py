import os
import numpy as np
import torch
from torch.utils.data import DataLoader

import wandb

from backend.utils import print_pretty_header
from backend.datasets import TrainDataManager, RunDataManager
from backend.parameters import ParameterMap
from backend.image_processing import process

class HyperTrainer(object):
    '''
    Class that trains a hypernetwork & main network architecture and reports to wandb
    '''
    def __init__(self, conf, hyper_model):
        self._hyper_model = hyper_model

        train_conf = conf["train"]
        model_conf = conf["model"]
        wandb_conf = train_conf["wandb"]
        preprocess_conf = conf["preprocess"]
        postprocess_conf = conf["postprocess"]

        # params
        batch_size = train_conf["batch_size"]
        imgs_per_task_train = train_conf["imgs_per_task_train"]
        imgs_per_task_val = train_conf["imgs_per_task_val"]

        self._device = f"cuda:{conf['gpu']}" if torch.cuda.is_available() else "cpu"
        self._logging_dir = train_conf["logging_dir"]
        self._verbose = train_conf["verbose"]
        self._export_path = train_conf["export_path"]
        self._pretrained_model_path = train_conf["pretrained_model_path"]
        self._auto_checkpoint_steps = train_conf["auto_checkpoint_steps"]

        self._tasks = train_conf["tasks"]
        self._task_cnt = model_conf["hnet"]["task_cnt"]
        self._batches_per_task_train = imgs_per_task_train // batch_size
        self._batches_per_task_val = imgs_per_task_val // batch_size

        self._loss_fn = train_conf["loss"]
        self._epochs = train_conf["epochs"]
        self._consecutive_batches_per_task = train_conf["consecutive_batches_per_task"]
        self._lr = train_conf["lr"]
        self._lr_decay = train_conf["lr_decay"]
        self._freeze_encoder_steps = train_conf["freeze_encoder_steps"]
        self._decay_epochs = train_conf["decay_epochs"]

        self.wandb_watch_log = wandb_conf["watch"]["log"]
        self.wandb_watch_log_freq = wandb_conf["watch"]["log_freq"]

        # convert to preprocess params
        preprocess_parameter_map = ParameterMap()
        preprocess_parameter_map.set_from_dict(preprocess_conf)

        self._postprocess_parameter_map = ParameterMap()
        self._postprocess_parameter_map.set_from_dict(postprocess_conf)

        # data loading
        input_saliencies = train_conf["input_saliencies"]
        train_img_path = train_conf["input_images_train"]
        val_img_path = train_conf["input_images_val"]
        sal_folders = [os.path.join(input_saliencies, task) for task in self._tasks] # path to saliency folder for all models

        train_datasets = [TrainDataManager(train_img_path, sal_path, self._verbose, preprocess_parameter_map) for sal_path in sal_folders]
        val_datasets = [TrainDataManager(val_img_path, sal_path, self._verbose, preprocess_parameter_map) for sal_path in sal_folders]

        self._dataloaders = {
            "train": {task:DataLoader(ds, batch_size=batch_size, shuffle=True, num_workers=4) for (task,ds) in zip(self._tasks, train_datasets)},
            "val": {task:DataLoader(ds, batch_size=batch_size, shuffle=True, num_workers=4) for (task,ds) in zip(self._tasks, val_datasets)},
        }

        self._run_dataloader = DataLoader(RunDataManager(train_conf["input_images_run"], "", verbose=False, recursive=False), batch_size=1)

        # sanity checks
        assert self._task_cnt == len(self._tasks)
        assert train_conf["imgs_per_task_train"] <= min([len(ds) for ds in train_datasets])
        assert train_conf["imgs_per_task_val"] <= min([len(ds) for ds in val_datasets])
    
    # train or evaluate one epoch for all models (mode in [train, val])
    # return loss, model
    def train_one(self, model, dataloaders, criterion, optimizer, mode):
        if mode == "train": model.train()
        elif mode == "val": model.eval()

        all_loss = []

        # defines which batch will be loaded from which task/model
        if mode == "train":
            limit = self._batches_per_task_train // self._consecutive_batches_per_task
            all_batches = np.concatenate([np.repeat(model.task_to_id(task), limit) for task in self._tasks])
            np.random.shuffle(all_batches)
            all_batches = np.repeat(all_batches, self._consecutive_batches_per_task)
        else:
            all_batches = np.concatenate([np.repeat(model.task_to_id(task), self._batches_per_task_val) for task in self._tasks])

        # for each model
        data_iters = [iter(d) for d in dataloaders[mode].values()] # Note: DataLoader shuffles when iterator is created
        for (i,task_id) in enumerate(all_batches):
            X,y = next(data_iters[task_id])

            optimizer.zero_grad()

            # put data on GPU (if cuda)
            X = X.to(self._device)
            y = y.to(self._device)

            pred = model(task_id.item(), X)
            loss = criterion(pred, y)

            # training
            if mode == "train":
                loss.backward()
                optimizer.step()

                all_loss.append(loss.item())

            # validation
            elif mode == "val":
                with torch.no_grad():
                    all_loss.append(loss.item())

            # logging
            if i%100 == 0:
                print(f"Batch {i}/{len(all_batches)}: current accumulated loss {np.mean(all_loss)}", flush=True)
            
            # remove batch from gpu (if cuda)
            if torch.cuda.is_available():
                del X
                del y
                torch.cuda.empty_cache()
                
        return np.mean(all_loss)

    # run the entire training
    def start_train(self):
        # build folder structure
        export_dir = os.path.join(self._logging_dir, self._export_path)
        export_path_best = os.path.join(export_dir, "best.pth")
        export_path_final = os.path.join(export_dir, "final.pth")
        os.makedirs(self._logging_dir, exist_ok=True)
        os.makedirs(export_dir, exist_ok=True)

        # initialize networks
        model = self._hyper_model
        model.build()
        if os.path.exists(self._pretrained_model_path):
            print(f"Load pretrained model from {self._pretrained_model_path}")
            model.load(self._pretrained_model_path, self._device)
        else:
            print(f"Not using a pretrained model")
        model.to(self._device)

        epochs = self._epochs
        lr = self._lr
        lr_decay = self._lr_decay
        optimizer = torch.optim.Adam(model.parameters(), lr=lr)

        losses = {
            "BCELoss": torch.nn.BCELoss(),
            "L1Loss": torch.nn.L1Loss()
        }
        loss = losses[self._loss_fn]
        
        # report to wandb
        wandb.watch((model.hnet, model.mnet), loss, log=self.wandb_watch_log, log_freq=self.wandb_watch_log_freq)
        
        all_epochs = []
        smallest_loss = None

        model.mnet.freeze_encoder()
        if self._verbose: print("Encoder frozen...")

        # evaluate how the model performs initially
        self.track_progress(-1, model)

        # training loop
        for epoch in range(0, epochs):
            # decrease learning rate over time
            if epoch in self._decay_epochs:
                for param_group in optimizer.param_groups:
                    param_group["lr"] *= lr_decay
                lr = lr * lr_decay

            # unfreeze the encoder after given amount of epochs
            if epoch == self._freeze_encoder_steps:
                if self._verbose: print("Encoder unfrozen")
                model.mnet.unfreeze_encoder()

            # train the networks
            loss_train = self.train_one(model, self._dataloaders, loss, optimizer, "train")
            if self._verbose: self.pretty_print_epoch(epoch, "train", loss_train, lr)

            # validate the networks
            loss_val = self.train_one(model, self._dataloaders, loss, optimizer, "val")
            if self._verbose: self.pretty_print_epoch(epoch, "val", loss_val, lr)

            ### REPORTING / STATS ###
            all_epochs.append([epoch, loss_train, loss_val]) 

            # if better performance than all previous => save weights as checkpoint
            is_best_model = smallest_loss is None or loss_val < smallest_loss
            if epoch % self._auto_checkpoint_steps == 0 or is_best_model:
                checkpoint_dir = os.path.join(self._logging_dir, f"checkpoint_in_epoch_{epoch}/")
                os.makedirs(checkpoint_dir, exist_ok=True)
                path = f"{checkpoint_dir}/{epoch}_{loss_val:f}.pth"

                self.save(path, model)
                
                # overwrite the best model
                if is_best_model:
                    smallest_loss = loss_val
                    self.save(export_path_best, model)
                
                self.track_progress(epoch, model)
            
            # save/overwrite results at the end of each epoch
            stats_file = os.path.join(os.path.relpath(self._logging_dir, wandb.run.dir), "all_results").replace("\\", "/")
            table = wandb.Table(data=all_epochs, columns=["Epoch", "Loss-Train", "Loss-Val"])
            wandb.log({
                    "epoch": epoch,
                    "loss train": loss_train,
                    "loss val": loss_val,
                    "learning rate": lr,
                    "encoder_frozen": float(epoch < self._freeze_encoder_steps),
                    stats_file:table
                })
        
        # save the final model
        self.save(export_path_final, model)

    # tracks and reports some metrics of the model that is being trained
    def track_progress(self, epoch, model):
        cols = ["Model"]
        cols.extend([os.path.basename(output_path[0]) for (_, _, output_path) in self._run_dataloader])
        data = []
        for task in self._tasks:
            row = [task]
            task_id = model.task_to_id(task)
            for (image, _, _) in self._run_dataloader:
                image = image.to(self._device)
                saliency_map = model.compute_saliency(image, task_id)
                post_processed_image = np.clip((process(saliency_map.cpu().detach().numpy()[0, 0], self._postprocess_parameter_map)*255).astype(np.uint8), 0, 255)
                img = wandb.Image(post_processed_image)
                row.append(img)
            data.append(row)

        table = wandb.Table(data=data, columns=cols)
        wandb.log({f"Progress Epoch {epoch}": table})

    # setup & run the entire training
    def execute(self):
        if self._verbose: print_pretty_header("TRAINING")
        if self._verbose: print("Trainer started...")

        self.start_train()
    
        if self._verbose: print("Done with training!")

    # saves the model to disk & wandb
    def save(self, path, model):
        model.save(path)
        wandb.save(path, base_path=wandb.run.dir)

    def pretty_print_epoch(self, epoch, mode, loss, lr):
        print("--------------------------------------------->>>>>>")
        print(f"Epoch {epoch}: loss {mode} {loss}, lr {lr}", flush=True)
        print("--------------------------------------------->>>>>>")

    def delete(self):
        del self._dataloaders
