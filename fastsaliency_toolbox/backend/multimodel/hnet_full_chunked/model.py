import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import mobilenet_v2

from hypnettorch.hnets.chunked_mlp_hnet import ChunkedHMLP
from hypnettorch.mnets.mnet_interface import MainNetInterface

class Decoder(nn.Module):
    def __init__(self, model_conf, has_bias):
        super(Decoder, self).__init__()
        self._has_bias = has_bias

        self.bn7_3 = nn.BatchNorm2d(num_features=512)
        self.bn8_1 = nn.BatchNorm2d(num_features=256)
        self.bn8_2 = nn.BatchNorm2d(num_features=256)
        self.bn9_1 = nn.BatchNorm2d(num_features=128)
        self.bn9_2 = nn.BatchNorm2d(num_features=128)
        self.bn10_1 = nn.BatchNorm2d(num_features=64)
        self.bn10_2 = nn.BatchNorm2d(num_features=64)

        non_lins = {
            "LeakyReLU": nn.LeakyReLU(),
            "ReLU": nn.ReLU()
        }
        self.non_lin = non_lins[model_conf["decoder_non_linearity"]]

        # layers not actually used in forward but providing the param shapes
        conv7_3 = nn.Conv2d(1280, 512, kernel_size=3, padding=1)
        conv8_1 = nn.Conv2d(512, 256, kernel_size=3, padding=1)
        conv8_2 = nn.Conv2d(256, 256, kernel_size=3, padding=1)
        conv9_1 = nn.Conv2d(256, 128, kernel_size=3, padding=1)
        conv9_2 = nn.Conv2d(128, 128, kernel_size=3, padding=1)
        conv10_1 = nn.Conv2d(128, 64, kernel_size=3, padding=1)
        conv10_2 = nn.Conv2d(64, 64, kernel_size=3, padding=1)
        output = nn.Conv2d(64, 1, kernel_size=1, padding=0)

        # generate the shapes of the params that are learned by the hypernetwork
        self._external_param_shapes = []
        self._external_param_shapes.extend([p.size() for p in conv7_3.parameters()])
        self._external_param_shapes.extend([p.size() for p in conv8_1.parameters()])
        self._external_param_shapes.extend([p.size() for p in conv8_2.parameters()])
        self._external_param_shapes.extend([p.size() for p in conv9_1.parameters()])
        self._external_param_shapes.extend([p.size() for p in conv9_2.parameters()])
        self._external_param_shapes.extend([p.size() for p in conv10_1.parameters()])
        self._external_param_shapes.extend([p.size() for p in conv10_2.parameters()])
        self._external_param_shapes.extend([p.size() for p in output.parameters()])

        
    def forward(self, xb, weights):
        w_weights = []
        b_weights = []
        for i, p in enumerate(weights):
            if i % 2 == 1:
                b_weights.append(p)
            else:
                w_weights.append(p)

        xb = self.non_lin(self.bn7_3(F.conv2d(xb, w_weights[0], b_weights[0], padding=1)))
        xb = F.interpolate(xb, scale_factor=2, mode="bilinear", align_corners=False)
        xb = self.non_lin(self.bn8_1(F.conv2d(xb, w_weights[1], b_weights[1], padding=1)))
        xb = F.interpolate(xb, scale_factor=2, mode="bilinear", align_corners=False)
        xb = self.non_lin(self.bn8_2(F.conv2d(xb, w_weights[2], b_weights[2], padding=1)))
        xb = F.interpolate(xb, scale_factor=2, mode="bilinear", align_corners=False)

        xb = self.non_lin(self.bn9_1(F.conv2d(xb, w_weights[3], b_weights[3], padding=1)))
        xb = F.interpolate(xb, scale_factor=2, mode="bilinear", align_corners=False)
        xb = self.non_lin(self.bn9_2(F.conv2d(xb, w_weights[4], b_weights[4], padding=1)))
        xb = F.interpolate(xb, scale_factor=2, mode="bilinear", align_corners=False)

        xb = self.non_lin(self.bn10_1(F.conv2d(xb, w_weights[5], b_weights[5], padding=1)))
        xb = self.non_lin(self.bn10_2(F.conv2d(xb, w_weights[6], b_weights[6], padding=1)))

        xb = F.conv2d(xb, w_weights[7], b_weights[7], padding=0)
        return xb

    # gets the shape of the parameters of which we expect the weights to be generated by the hypernetwork
    def get_external_param_shapes(self):
        return self._external_param_shapes


class Student(nn.Module, MainNetInterface):
    def __init__(self, model_conf):
        nn.Module.__init__(self)
        MainNetInterface.__init__(self)

        # MNET setup
        self._has_fc_out = False 
        self._mask_fc_out = False
        self._has_linear_out = False
        self._has_bias = True

        self._layer_weight_tensors = nn.ParameterList()
        self._layer_bias_vectors = nn.ParameterList()

        # build model
        self.encoder = self.mobilenetv2_pretrain()
        self.decoder = Decoder(model_conf, has_bias=self._has_bias)
        self.sigmoid = nn.Sigmoid()

        # params that will be trained by the hypernetwork
        self._external_param_shapes = self.decoder.get_external_param_shapes()

        # params that will be trained by the model itself
        self._internal_params = list(self.parameters())
        self._param_shapes = []
        for param in self._internal_params:
            self._param_shapes.append(list(param.size()))


        self._is_properly_setup()

    def forward(self, xb, weights):
        enc = self.encoder(xb)
        dec = self.decoder(enc, weights)
        prob = self.sigmoid(dec)
        return prob


    def freeze_encoder(self):
        for param in self.encoder.parameters():
            param.requires_grad_(False)

    def unfreeze_encoder(self):
        for param in self.encoder.parameters():
            param.requires_grad_(True)
    
    def external_param_shapes(self):
        return self._external_param_shapes

    ####################
    # MainNetInterface #
    ####################

    def distillation_targets(self):
        return None # we do not have any distillation targets

    #########
    # Other #
    #########

    def mobilenetv2_pretrain(self, pretrained=True):
        model = mobilenet_v2(pretrained=pretrained, progress=False)
        features = nn.Sequential(*list(model.features))
        return features


######################
#  LOAD & SAVE MODEL #
######################

# builds a hypernetwork and mainnetwork
def hnet_mnet_from_config(conf):
    model_conf = conf["model"]

    mnet = Student(model_conf)
    hnet = ChunkedHMLP(
        target_shapes=mnet.external_param_shapes(), 
        chunk_size=model_conf["hnet_chunk_size"],
        layers=model_conf["hnet_hidden_layers"], # the sizes of the hidden layers (excluding the last layer that generates the weights)
        cond_in_size=model_conf["hnet_embedding_size"], # the size of the embeddings
        num_cond_embs=model_conf["task_cnt"], # the number of embeddings we want to learn
        cond_chunk_embs = model_conf["hnet_chunk_emb_per_task"], # chunk embeddings depend on task id
        chunk_emb_size=model_conf["hnet_chunk_emb_size"] # size of the chunk embeddings
    )

    return hnet,mnet