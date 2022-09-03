"""
Runner
------

DESCRIPTION:
    For each task in conf["tasks"], 
    this runner will go over all images in conf["run"]["input_images_run"]
    and compute & store the saliency map for each of them.

RETURN VALUE:
    Same as input

CONFIG:
run
    tasks                   (List[str]) : all the tasks that will be trained
    input_images_run        (str)       : path to images for running (folder/img.jpg)
    overwrite               (bool)      : overwrite output?

    save_to_wandb           (bool)      : save output to wandb
    save_to_disk            (bool)      : save output to disk

"""

import torch
from torch.utils.data import DataLoader

from backend.parameters import ParameterMap
from backend.datasets import RunDataManager
from backend.multitask.pipeline.pipeline import AStage
from backend.multitask.hnet.models.hyper_model import HyperModel
from backend.multitask.hnet.train_impl_wandb.progress_tracking import RunProgressTrackerWandb
from backend.multitask.hnet.train_impl.progress_tracking import RunProgressTracker

class Runner(AStage):
    def __init__(self, conf, name, verbose):
        super().__init__(name=name, verbose=verbose)
        self._model : HyperModel = None

        run_conf = conf[name]
        self._tasks = run_conf["tasks"]
        self._device = f"cuda:{conf['gpu']}" if torch.cuda.is_available() else "cpu"
        self._input_dir = run_conf["input_images_run"]
        self._overwrite = run_conf["overwrite"]
        self._save_to_wandb = run_conf["save_to_wandb"]
        self._save_to_disk = run_conf["save_to_disk"]

        # convert params
        self._postprocess_parameter_map = ParameterMap().set_from_dict(conf["postprocess"])

    def setup(self, work_dir_path: str = None, input=None):
        super().setup(work_dir_path, input)
        assert input is not None and isinstance(input, HyperModel), "Runner expects a HyperModel to be passed as an input."
        assert work_dir_path is not None, "Working directory path cannot be None."

        self._model = input
        self._model.build()
        self._logging_dir = work_dir_path

        # prepare dataloader
        self._dataloader = DataLoader(RunDataManager(self._input_dir, self._logging_dir, self._verbose, recursive=False), batch_size=1)

        if self._save_to_wandb:
            self._runner_wandb = RunProgressTrackerWandb(self._dataloader, self._tasks, self._postprocess_parameter_map, self._name)
        if self._save_to_disk:
            self._runner_disk = RunProgressTracker(self._dataloader, self._tasks, self._postprocess_parameter_map, self._name)


    def execute(self):
        super().execute()
        
        self._model.to(self._device)

        if self._save_to_wandb:
            self._runner_wandb.track_progress_core(self._model, self._name)
            
        if self._save_to_disk:
            self._runner_disk.track_progress_core(self._model, self._name)

        return self._model

    def cleanup(self):
        super().cleanup()

        del self._dataloader