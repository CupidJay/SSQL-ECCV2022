import torch
import torch.nn as nn
from copy import deepcopy
import quant_tools
import utils
import random
import numpy as np


class SSQL_BYOL(nn.Module):
    def __init__(self, base_encoder, config):
        """
        update the quantized branch, add float prediction loss
        dim: feature dimension (default: 2048)
        pred_dim: hidden dimension of the predictor (default: 512)
        """
        super(SSQL_BYOL, self).__init__()
        self.config = config
        
        dim = config.SSL.SETTING.DIM
        hidden_dim = config.SSL.SETTING.HIDDEN_DIM
        self.m = config.SSL.SETTING.MOMENTUM

        # create the encoder
        # num_classes is the output fc dimension, zero-initialize last BNs
        self.encoder_q = base_encoder(num_classes=dim, zero_init_residual=True)

        # build a 3-layer projector
        prev_dim = self.encoder_q.fc.weight.shape[1]
        fc_dim = hidden_dim
        self.encoder_q.fc = nn.Sequential(nn.Linear(prev_dim, fc_dim, bias=False),
                                        nn.BatchNorm1d(fc_dim),
                                        nn.ReLU(inplace=True), # first layer
                                        nn.Linear(fc_dim, fc_dim, bias=False),
                                        nn.BatchNorm1d(fc_dim),
                                        nn.ReLU(inplace=True), # second layer
                                        nn.Linear(fc_dim,  dim),
                                        nn.BatchNorm1d(dim, affine=False)) # output layer

        self.encoder_q.fc[6].bias.requires_grad = False #hack: not use bias as it is followed by BN

        print(self.encoder_q.state_dict().keys())

        self.encoder_k = deepcopy(self.encoder_q)

        self.encoder_q =  quant_tools.QuantModel(self.encoder_q, self.config)
        #self.encoder_q.allocate_zero_bit(self.config, None)
        self.encoder_q.allocate_bit(self.config, None)

        # build a 2-layer predictor
        self.predictor = nn.Sequential(nn.Linear(dim, hidden_dim, bias=False),
                                        nn.BatchNorm1d(hidden_dim),
                                        nn.ReLU(inplace=True), # hidden layer
                                        nn.Linear(hidden_dim, dim)) # output layer
        
        self.w_range = self.config.QUANT.W.BIT_RANGE
        self.a_range = self.config.QUANT.A.BIT_RANGE
        print('weight bit range {}, activation bit range {}'.format(self.w_range, self.a_range))

    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        """
        Momentum update of the key encoder
        """
        #print(len(self.encoder_q.parameters()), len(self.encoder_k.parameters()))
        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            #print(param_q.size(), param_k.size())
            param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)

    def forward(self, x1, x2):
        """
        Input:
            x1: first views of images
            x2: second views of images
        Output:
            p1, p2, z1, z2: predictors and targets of the network
        """
        # For the float branch, we want it to generate target z (also prediction op) and do calibration during training
        self.encoder_q.set_quant_state(False, False, w_init=False, a_init=False)
        self.encoder_q.reset_minmax()
        self.encoder_q._register_hook_update()
        z1, z2 = self.encoder_q(x1), self.encoder_q(x2)
        self.encoder_q._unregister_hook()
        p1, p2 = self.predictor(z1), self.predictor(z2)

        #'''
        # if you want to set random bit per step
        #random_w_bit = random.choice(np.arange(2,9))
        #random_a_bit = random.choice(np.arange(4,9))
        random_w_bit = random.choice(np.arange(self.w_range[0], self.w_range[1]))
        random_a_bit = random.choice(np.arange(self.a_range[0], self.a_range[1]))
        self.config.defrost()
        self.config.QUANT.W.BIT = int(random_w_bit)
        self.config.QUANT.A.BIT = int(random_a_bit)
        #'''

        self.encoder_q.allocate_bit(self.config, None)
        self.encoder_q.set_quant_state(True, True, w_init=True, a_init=True)
        z1_q = self.encoder_q(x1)
        z2_q = self.encoder_q(x2)
        p1_q = self.predictor(z1_q)
        p2_q = self.predictor(z2_q)

        with torch.no_grad():
            self._momentum_update_key_encoder()  # update the key encoder
            z1_k = self.encoder_k(x1)
            z2_k = self.encoder_k(x2)

        return [p1, p2, p1_q, p2_q] , [z1_k.detach(), z2_k.detach()]