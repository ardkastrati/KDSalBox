"""
Provides an interface that exposes a most general hypernetwork
and a concrete implementation that selects a hypernetwork type 
specified in the config.

Also provides the actual hypernetwork architectures.

TUTORIAL:
    To add new hypernetwork architecture:
    Add a class extending from AHNET and add an if case in HNET.
    You can now set type=<your new type name> in the config.

"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from abc import ABC, abstractmethod
from typing import Dict, List, Union

from hypnettorch.hnets.chunked_mlp_hnet import ChunkedHMLP
from hypnettorch.hnets.mlp_hnet import HMLP

class AHNET(nn.Module, ABC):
    """ Abstract hypernetwork class that exposes a default interface
        that can be used by trainers for example. """
    def __init__(self, target_shapes : List[torch.Size]):
        nn.Module.__init__(self)
        ABC.__init__(self)

        self._target_shapes = target_shapes

    @property
    def target_shapes(self):
        return self._target_shapes

    @abstractmethod
    def get_gradients_on_outputs(self) -> Dict[int, List[torch.Tensor]]:
        """ Returns a dictionary that for each task_id contains a list of gradients (representing the different targets = MNET parameters) """
        pass

    @abstractmethod
    def task_parameters(self, task_ids : List[int]) -> List[torch.nn.parameter.Parameter]:
        """ Gets all the parameters that are unique to a list of tasks
            (e.g. the embedding vectors)
        """
        pass

class HNET(AHNET):
    """ General HNET that can take different forms 
        depending on the specified type in the config. """
    def __init__(self, target_shapes : List[torch.Size], hnet_conf : dict):
        super().__init__(target_shapes)

        hnet_type = hnet_conf["type"]
        if hnet_type == "simple":
            self.hnet = SimpleHNET(target_shapes, hnet_conf)
        elif hnet_type == "chunked":
            self.hnet = ChunkedHNET(target_shapes, hnet_conf)
        elif hnet_type == "single_layer":
            self.hnet = SingleLayerHNET(target_shapes, hnet_conf)
        else:
            raise ValueError(f"Hypernetwork Configuration: Does not support type {hnet_type}")

    def forward(self, task_id : Union[int, List[int]]):
        return self.hnet.forward(task_id)

    def get_gradients_on_outputs(self) -> List[torch.Tensor]:
        return self.hnet.get_gradients_on_outputs()
    
    def task_parameters(self, task_ids : List[int]) -> List[torch.nn.parameter.Parameter]:
        return self.hnet.task_parameters(task_ids)






########## ACTUAL HYPERNETWORK ARCHITECTURES ##########

class ChunkedHNET(AHNET):
    """
    ChunkedHNET
    -----------

    Hypernetwork that uses chunking of the outputs to be more compact.
    In essence the outputs are split into equally sized chunks that are generated
    in a for loop. Each iteration gets additionally fed a chunk embedding (analog to the index of the iteration).

    """
    def __init__(self, target_shapes : List[torch.Size], hnet_conf : dict):
        super().__init__(target_shapes)

        self.hnet = ChunkedHMLP(
            target_shapes=target_shapes, 
            chunk_size=hnet_conf["chunk_size"],
            layers=hnet_conf["hidden_layers"], # the sizes of the hidden layers (excluding the last layer that generates the weights)
            cond_in_size=hnet_conf["embedding_size"], # the size of the embeddings
            num_cond_embs=hnet_conf["task_cnt"], # the number of embeddings we want to learn
            cond_chunk_embs = hnet_conf["chunk_emb_per_task"], # chunk embeddings depend on task id
            chunk_emb_size=hnet_conf["chunk_emb_size"] # size of the chunk embeddings
        )

        self._did_log = False

    def forward(self, task_id : Union[int, List[int]]):
        return self.hnet.forward(cond_id = task_id)

    def get_gradients_on_outputs(self) -> Dict[int, List[torch.Tensor]]:
        if self._did_log: return

        print("Warning: observing gradients of ChunkedHLMP is currently not yet supported!")
        self._did_log = True
        
        return {}
    
    def task_parameters(self, task_ids : List[int]) -> List[torch.nn.parameter.Parameter]:
        selection = [f"_hnet._internal_params.{i}" for i in task_ids]
        params = [p for n,p in self.hnet.named_parameters() if n in selection]
        return params


class SimpleHNET(AHNET):
    """
    SimpleHNET
    ----------

    HNET that outputs all the weights at once.
    Note that the the network will use the conditional task_embedding as an input
    (which will be learned too).

    """
    def __init__(self, target_shapes : List[torch.Size], hnet_conf : dict):
        super().__init__(target_shapes)

        self.hnet = HMLP(
            target_shapes=target_shapes, 
            layers=hnet_conf["hidden_layers"], # the sizes of the hidden layers (excluding the last layer that generates the weights)
            cond_in_size=hnet_conf["embedding_size"], # the size of the embeddings
            num_cond_embs=hnet_conf["task_cnt"], # the number of embeddings we want to learn
        )

    def forward(self, task_id : Union[int, List[int]]):
        return self.hnet.forward(cond_id = task_id)

    def get_gradients_on_outputs(self) -> Dict[int, List[torch.Tensor]]:
        print("Warning: observing gradients of SimpleHNET is currently not yet supported!")
        return {}

    def task_parameters(self, task_ids : List[int]) -> List[torch.nn.parameter.Parameter]:
        selection = [f"_internal_params.{i}" for i in task_ids]
        params = [p for n,p in self.hnet.named_parameters() if n in selection]
        return params






class SingleLayerHNET(AHNET):
    """
    SingleLayerHNET
    ---------------

    Hypernetwork that uses a one-hot vector representing the task_id and one linear layer
    that goes from <task_cnt> to <amount of MNET weights>

    """
    def __init__(self, target_shapes : List[torch.Size], hnet_conf : dict):
        super().__init__(target_shapes)

        self._task_cnt = hnet_conf["task_cnt"]
        self._target_shapes = target_shapes
        self._target_numels = [shape.numel() for shape in target_shapes]

        total_weights = 0
        for n in self._target_numels:
            total_weights += n

        self._l1 = nn.Linear(self._task_cnt, total_weights, bias=False)

    def forward(self, task_id : Union[int, List[int]]):
        device = list(self.parameters())[0].device

        if isinstance(task_id, int):
            return self._generate_weights_for_task_id(task_id, device)
        elif isinstance(task_id, list):
            return [self._generate_weights_for_task_id(tid, device) for tid in task_id]
        else:
            raise TypeError(task_id)
    
    def _generate_weights_for_task_id(self, task_id : int, device):
        x = F.one_hot(torch.LongTensor(np.array([task_id])), self._task_cnt)[0].float().to(device) # one hot encoding of cond_id/task_id
        x = self._l1(x)

        # make the HNET output the specified target shapes
        weights = list(torch.split(x, split_size_or_sections=self._target_numels))
        for i,s in enumerate(self._target_shapes):
            weights[i] = weights[i].view(s)

        return weights

    def get_gradients_on_outputs(self) -> Dict[int, List[torch.Tensor]]:
        weight_param = self.get_parameter("_l1.weight")
        grads = weight_param.grad.detach() # has shape (total_weights, task_cnt)

        grads_per_task = {}
        for task_id in range(self._task_cnt):
            grad = grads[:,task_id] # has shape (total_weights)
            grad_per_target = list(grad.split(self._target_numels))
            grads_per_task[task_id] = grad_per_target

        return grads_per_task
    
    def task_parameters(self, task_ids : List[int]) -> List[torch.nn.parameter.Parameter]:
        raise NotImplementedError("SingleLayerHNET.task_parameters() not implemented!")