"""
gmain.py
--------

Provides the CLI to the generalization component of the fastsaliency_toolbox.
Note: This part is currently work in progress but the interested user is encouraged to play around with the provided framework.

Available commands are:
    - gridsearch: Runs a gridsearch over a set of configurations specified by a .json file 
        (see the command for a list of possible arguments)

"""

import os
import json
import click
import wandb

from backend.multitask.hnet.hyper_model import HyperModel
from backend.multitask.hnet.runner import Runner
from backend.multitask.hnet.tester import Tester
from backend.multitask.hnet.trainer import Trainer
from backend.multitask.hnet.pretrainer import PreTrainer
from backend.multitask.pipeline.pipeline import Pipeline
from backend.multitask.pipeline.stages import ExportStage

@click.group()
def cli():
    pass

@cli.command()
def foo():
    from backend.multitask.hnet.full.model import hnet_mnet_from_config

    with open("C:/Users/yanic/dev/ethz/ba/kdsalbox-generalization/fastsaliency_toolbox/backend/multitask/hnet/full/config.json") as f:
        conf = json.load(f)
        hnet,mnet = hnet_mnet_from_config(conf)

        hnet(2)

    

@cli.command()
def version():
    """Displays version information."""
    click.echo("Fast Saliency Toolbox: Generalization Implementation")

def run_with_conf(conf, group=None):
    run_name = conf["name"]
    run_description = conf["description"]

    wandb.login()
    start_method = "spawn" if os.name == "nt" else "fork" # check if windows or linux
    run = wandb.init(project="kdsalbox-generalization", entity="ba-yanickz", 
        name=run_name, notes=run_description, group=group, reinit=True, 
        config=conf, settings=wandb.Settings(start_method=start_method))

    # build & update paths relative to wandb run dir
    run_dir = os.path.abspath(os.path.join(wandb.run.dir, run_name))

    # DO NOT FURTHER ADJUST THE CONF FROM THIS POINT ON

    # save the current config file
    config_dump = os.path.join(run_dir, "used_config.json")
    print(config_dump)
    os.makedirs(os.path.dirname(config_dump), exist_ok=True)
    with open(config_dump, "w") as f:
        json.dump(conf, f, indent=4)
    wandb.save(config_dump, base_path=wandb.run.dir)
        
    # construct and run the experiment pipeline
    try:
        from backend.multitask.hnet.contextmod.model import hnet_mnet_from_config as hmfc_contextmod
        from backend.multitask.hnet.full_chunked.model import hnet_mnet_from_config as hmfc_full_chunked
        from backend.multitask.hnet.full.model import hnet_mnet_from_config as hmfc_full
        net_factory = {
            "contextmod": hmfc_contextmod,
            "full_chunked": hmfc_full_chunked,
            "full": hmfc_full,
        }
        hnet_mnet_from_config = net_factory[conf["type"]]

        verbose = conf["verbose"]
        print(f"Running {conf['type']}")

        pipeline = Pipeline(
            input = HyperModel(lambda: hnet_mnet_from_config(conf), conf["tasks"]).build(),
            work_dir_path=run_dir,
            stages = [
                PreTrainer(conf, "pretrain", verbose=verbose),
                ExportStage("export - pretrained", path=f"{os.path.join(run_dir, 'pretrain', 'best.pth')}", verbose=verbose),
                Trainer(conf, "train", verbose=verbose), 
                ExportStage("export - trained", path=f"{os.path.join(run_dir, 'train', 'best.pth')}", verbose=verbose),
                Tester(conf, "test", verbose=verbose), 
                Runner(conf, "run", verbose=verbose)
            ]
        )
        
        pipeline.execute(exclude=conf["skip"])

    except ValueError as e:
        print(str(e))
        exit(64)

    run.finish()

########################################
# Generalization - Gridsearch
@cli.command()
@click.option("--skip", help="Comma-separated list of experiment stages that should be skipped {train, test, run}")
@click.option("-n", "--name", help="The name of the experiment.")
@click.option("-c", "--conf_file", help="The path to the configuration file.")
@click.option("-p", "--param_grid_file", help="The path to the gridsearch params file.")

@click.option("-i", "--input_images", help="The images used for experimenting. Should contain three folders inside: train, val and run.")
@click.option("-s", "--input_saliencies", help="Specify the directory to the saliency images. Should contain a separated folder for each model/task name.")

@click.option("--wdb", is_flag=True, help="Do you want to report to wandb?")

@click.option("--description", help="A description of what makes this run special")
def gridsearch(skip, name, conf_file, param_grid_file, input_images, input_saliencies, wdb, description):
    # load param_grid
    with open(param_grid_file) as f:
        param_grid = json.load(f)
    
    # load config file
    with open(conf_file) as f:
        conf = json.load(f)

    train_conf = conf["train"]
    test_conf = conf["test"]
    run_conf = conf["run"]

    # overwrite params given as args
    if skip:
        conf["skip"] = skip.split(",")
    if name:
        conf["name"] = name
    if description:
        conf["description"] = description
    if input_images:
        train_conf["input_images_train"] = os.path.join(input_images, "train")
        train_conf["input_images_val"] = os.path.join(input_images, "val")
        train_conf["input_images_run"] = os.path.join(input_images, "run")
        test_conf["input_images_test"] = os.path.join(input_images, "val") # TODO: change to /test once testdata available
        run_conf["input_images_run"] = os.path.join(input_images, "run")
    if input_saliencies:
        train_conf["input_saliencies"] = input_saliencies
        test_conf["input_saliencies"] = input_saliencies
    
    os.environ["WANDB_MODE"] = "online" if wdb else "offline"

    base_name = conf["name"]

    def _gridsearch(conf, param_grid, indices=[]):
        param_grid = param_grid.copy()
        if len(param_grid) == 0:
            print("#############################")
            print(f"NOW RUNNING {indices}")
            print("#############################")

            run_with_conf(conf, group=base_name)
        else:
            path,values = param_grid.pop(0)
            paths = path.split("/")
            c = conf.copy()
            bn = c["name"]
            bd = c["description"] 
            d = c
            for p in paths[0:-1]:
                d = d[p]
            
            for i,v in enumerate(values):
                c["name"] = f"{bn} - {v}"
                c["description"] = f"{bd}\n{path} = {v}"
                d[paths[-1]] = v
                _gridsearch(c, param_grid, indices+[i])
        
    _gridsearch(conf, param_grid)

if __name__ == "__main__":
    cli()