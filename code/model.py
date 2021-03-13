# -*- coding: utf-8 -*-

import sys

import numpy as np

import torch

import torch as T

import torch.nn as nn

from torch.autograd import Variable

import torch.nn.functional as F

from utils_pg import *

from transformer import TransformerLayer, Embedding, LearnedPositionalEmbedding, gelu, LayerNorm, SelfAttentionMask

from word_prob_layer import *

from label_smoothing import LabelSmoothing


class Model(nn.Module):

    def __init__(self, modules, consts, options):

        super(Model, self).__init__()

        self.has_learnable_w2v = options["has_learnable_w2v"]

        self.is_predicting = options["is_predicting"]

        self.is_bidirectional = options["is_bidirectional"]

        self.beam_decoding = options["beam_decoding"]

        self.cell = options["cell"]

        self.device = options["device"]

        self.copy = options["copy"]

        self.coverage = options["coverage"]

        self.avg_nll = options["avg_nll"]

        self.dim_x = consts["dim_x"]

        self.dim_y = consts["dim_y"]

        self.len_x = consts["len_x"]

        self.len_y = consts["len_y"]

        self.hidden_size = consts["hidden_size"]

        self.dict_size = consts["dict_size"]

        self.pad_token_idx = consts["pad_token_idx"]

        self.ctx_size = self.hidden_size * 2 if self.is_bidirectional else self.hidden_size

        self.num_layers = consts["num_layers"]

        self.d_ff = consts["d_ff"]

        self.num_heads = consts["num_heads"]

        self.dropout = consts["dropout"]

        self.smoothing_factor = consts["label_smoothing"]

        self.tok_embed = nn.Embedding(self.dict_size, self.dim_x, self.pad_token_idx) #pad单独进行embedding

        self.pos_embed = LearnedPositionalEmbedding(self.dim_x, device=self.device)

        self.additional_enc_layers_up = nn.ModuleList()

        for i in range(self.num_layers):
            self.additional_enc_layers_up.append(TransformerLayer(self.dim_x, self.d_ff, self.num_heads, self.dropout))

        self.additional_enc_layers_below = nn.ModuleList()

        for i in range(self.num_layers):
            self.additional_enc_layers_below.append(TransformerLayer(self.dim_x, self.d_ff, self.num_heads, self.dropout))

        self.enc_layers = nn.ModuleList()

        for i in range(self.num_layers):
            self.enc_layers.append(TransformerLayer(self.dim_x, self.d_ff, self.num_heads, self.dropout))

        self.dec_layers = nn.ModuleList()

        for i in range(self.num_layers):
            self.dec_layers.append(
                TransformerLayer(self.dim_x, self.d_ff, self.num_heads, self.dropout, with_external=True))

        self.attn_mask = SelfAttentionMask(device=self.device)

        self.emb_layer_norm = LayerNorm(self.dim_x)

        self.word_prob = WordProbLayer(self.hidden_size, self.dict_size, self.device, self.copy, self.coverage,
                                       self.dropout)

        self.smoothing = LabelSmoothing(self.device, self.dict_size, self.pad_token_idx, self.smoothing_factor)

        self.to_weights = nn.Linear(self.dim_x, 1)

        self.init_weights()



    def init_weights(self):

        init_uniform_weight(self.tok_embed.weight)

    def label_smoothing_loss(self, y_pred, y, y_mask, avg=True):

        seq_len, bsz = y.size()

        y_pred = T.log(y_pred.clamp(min=1e-8))

        loss = self.smoothing(y_pred.view(seq_len * bsz, -1), y.view(seq_len * bsz, -1))

        if avg:

            return loss / T.sum(y_mask)

        else:

            return loss / bsz

    def nll_loss(self, y_pred, y, y_mask, avg=True):

        cost = -T.log(T.gather(y_pred, 2, y.view(y.size(0), y.size(1), 1)))

        cost = cost.view(y.shape)

        y_mask = y_mask.view(y.shape)

        if avg:

            cost = T.sum(cost * y_mask, 0) / T.sum(y_mask, 0)

        else:

            cost = T.sum(cost * y_mask, 0)

        cost = cost.view((y.size(1), -1))

        return T.mean(cost)

    def encode(self, input, dx=None, d_padding_mask=None):

        x = self.tok_embed(input) + self.pos_embed(input)

        x = self.emb_layer_norm(x)

        x = F.dropout(x, p=self.dropout, training=self.training)

        padding_mask = torch.eq(input, self.pad_token_idx)

        if not padding_mask.any():
            padding_mask = None

        for layer_id, layer in enumerate(self.enc_layers):
            x, _, _ = layer(x, self_padding_mask=padding_mask, dx=dx, d_padding_mask=d_padding_mask)

        return x, padding_mask

    def decode(self, input, mask_x, mask_y, src, src_padding_mask, x_ext=None, max_ext_len=None, dx=None, d_padding_mask=None):

        seq_len, bsz = input.size()

        x = self.tok_embed(input) + self.pos_embed(input)

        x = self.emb_layer_norm(x)

        x = F.dropout(x, p=self.dropout, training=self.training)

        h = x

        if not self.is_predicting:

            mask_y = mask_y.view((seq_len, bsz))

            padding_mask = torch.eq(mask_y, self.pad_token_idx)

            if not padding_mask.any():
                padding_mask = None

        else:

            padding_mask = None

        self_attn_mask = self.attn_mask(seq_len)

        for layer_id, layer in enumerate(self.dec_layers):
            x, _, _ = layer(x, self_padding_mask=padding_mask, \
 \
                            self_attn_mask=self_attn_mask, \
 \
                            external_memories=src, \
 \
                            external_padding_mask=src_padding_mask, need_weights=False, dx=dx, d_padding_mask=d_padding_mask)

        if self.copy:
            
            y_dec, attn_dist = self.word_prob(x, h, src, src_padding_mask, x_ext, max_ext_len)

        else:

            y_dec, attn_dist = self.word_prob(x)

        return y_dec, attn_dist

    def additional_encoder_up(self, input):

        x = self.tok_embed(input) + self.pos_embed(input)

        x = self.emb_layer_norm(x) # normalization

        x = F.dropout(x, p=self.dropout, training=self.training)

        padding_mask = torch.eq(input, self.pad_token_idx) # pad mask if sentence is not long enough

        if not padding_mask.any():
            padding_mask = None

        for layer_id, layer in enumerate(self.additional_enc_layers_up):
            x, _, _ = layer(x, self_padding_mask=padding_mask)

        v_dx = self.to_weights(x)

        to_weights = nn.Linear(x.size(0), 1)

        v_dx = v_dx.transpose(0, 2)

        v_dx = v_dx.to("cpu")

        v_dx = to_weights(v_dx)

        v_dx = torch.sigmoid(v_dx)

        v_dx = v_dx.expand(x.size(0), -1, x.size(2))

        v_dx = v_dx.cuda()

        x = torch.mul(x, v_dx)

        return x, padding_mask

    def additional_encoder_below(self, input):

        x = self.tok_embed(input) + self.pos_embed(input)

        x = self.emb_layer_norm(x)

        x = F.dropout(x, p=self.dropout, training=self.training)

        padding_mask = torch.eq(input, self.pad_token_idx)

        if not padding_mask.any():
            padding_mask = None

        for layer_id, layer in enumerate(self.additional_enc_layers_below):
            x, _, _ = layer(x, self_padding_mask=padding_mask)

        v_dx = self.to_weights(x)

        to_weights = nn.Linear(x.size(0), 1)

        v_dx = v_dx.transpose(0, 2)

        v_dx = v_dx.to("cpu")

        v_dx = to_weights(v_dx)

        v_dx = torch.sigmoid(v_dx)

        v_dx = v_dx.expand(x.size(0), -1, x.size(2))

        v_dx = v_dx.cuda()

        x = torch.mul(x, v_dx)

        return x, padding_mask

    def forward(self, x, y_inp, y_tgt, mask_x, mask_y, x_ext, y_ext, max_ext_len, dx, dy, signal=True):
    # The dx is corresponding to the preceding sentencesand the dd is corresponding to the following sentences.
        if signal:

            dx, dc_padding_mask = self.additional_encoder_up(dx)

            dy, dy_padding_mask = self.additional_encoder_below(dy)

            dx = torch.cat((dx, dy), dim=0)

            d_padding_mask = torch.cat((dc_padding_mask, dy_padding_mask), dim=0)

            hs, src_padding_mask = self.encode(x, dx, d_padding_mask) # The hs is the output of the encoder layer.

            if self.copy:

                y_pred, _ = self.decode(y_inp, mask_x, mask_y, hs, src_padding_mask, x_ext, max_ext_len, dx, d_padding_mask)

                cost = self.label_smoothing_loss(y_pred, y_ext, mask_y, self.avg_nll)

            else:

                y_pred, _ = self.decode(y_inp, mask_x, mask_y, hs, src_padding_mask)

                cost = self.nll_loss(y_pred, y_tgt, mask_y, self.avg_nll)

            return y_pred, cost

        else:

            hs, src_padding_mask = self.encode(x)

            if self.copy:

                y_pred, _ = self.decode(y_inp, mask_x, mask_y, hs, src_padding_mask, x_ext, max_ext_len)

                cost = self.label_smoothing_loss(y_pred, y_ext, mask_y, self.avg_nll)

            else:

                y_pred, _ = self.decode(y_inp, mask_x, mask_y, hs, src_padding_mask)

                cost = self.nll_loss(y_pred, y_tgt, mask_y, self.avg_nll)

            return y_pred, cost
