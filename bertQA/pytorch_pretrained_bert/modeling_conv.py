# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch BERT model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import copy
import json
import math
import logging
import tarfile
import tempfile
import shutil

import torch
from torch import nn
from torch.nn import CrossEntropyLoss

from .file_utils import cached_path

logger = logging.getLogger(__name__)

PRETRAINED_MODEL_ARCHIVE_MAP = {
    'bert-base-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-uncased.tar.gz",
    'bert-large-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased.tar.gz",
    'bert-base-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-cased.tar.gz",
    'bert-large-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased.tar.gz",
    'bert-base-multilingual-uncased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-multilingual-uncased.tar.gz",
    'bert-base-multilingual-cased': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-multilingual-cased.tar.gz",
    'bert-base-chinese': "https://s3.amazonaws.com/models.huggingface.co/bert/bert-base-chinese.tar.gz",
}
CONFIG_NAME = 'bert_config.json'
WEIGHTS_NAME = 'pytorch_model.bin'

def gelu(x):
    """Implementation of the gelu activation function.
        For information: OpenAI GPT's gelu is slightly different (and gives slightly different results):
        0.5 * x * (1 + torch.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * torch.pow(x, 3))))
    """
    return x * 0.5 * (1.0 + torch.erf(x / math.sqrt(2.0)))


def swish(x):
    return x * torch.sigmoid(x)


ACT2FN = {"gelu": gelu, "relu": torch.nn.functional.relu, "swish": swish}


class BertConfig(object):
    """Configuration class to store the configuration of a `BertModel`.
    """
    def __init__(self,
                 vocab_size_or_config_json_file,
                 hidden_size=768,
                 num_hidden_layers=12,
                 num_attention_heads=12,
                 intermediate_size=3072,
                 hidden_act="gelu",
                 hidden_dropout_prob=0.1,
                 attention_probs_dropout_prob=0.1,
                 max_position_embeddings=512,
                 type_vocab_size=2,
                 initializer_range=0.02):
        """Constructs BertConfig.

        Args:
            vocab_size_or_config_json_file: Vocabulary size of `inputs_ids` in `BertModel`.
            hidden_size: Size of the encoder layers and the pooler layer.
            num_hidden_layers: Number of hidden layers in the Transformer encoder.
            num_attention_heads: Number of attention heads for each attention layer in
                the Transformer encoder.
            intermediate_size: The size of the "intermediate" (i.e., feed-forward)
                layer in the Transformer encoder.
            hidden_act: The non-linear activation function (function or string) in the
                encoder and pooler. If string, "gelu", "relu" and "swish" are supported.
            hidden_dropout_prob: The dropout probabilitiy for all fully connected
                layers in the embeddings, encoder, and pooler.
            attention_probs_dropout_prob: The dropout ratio for the attention
                probabilities.
            max_position_embeddings: The maximum sequence length that this model might
                ever be used with. Typically set this to something large just in case
                (e.g., 512 or 1024 or 2048).
            type_vocab_size: The vocabulary size of the `token_type_ids` passed into
                `BertModel`.
            initializer_range: The sttdev of the truncated_normal_initializer for
                initializing all weight matrices.
        """
        if isinstance(vocab_size_or_config_json_file, str):
            with open(vocab_size_or_config_json_file, "r", encoding='utf-8') as reader:
                json_config = json.loads(reader.read())
            for key, value in json_config.items():
                self.__dict__[key] = value
        elif isinstance(vocab_size_or_config_json_file, int):
            self.vocab_size = vocab_size_or_config_json_file
            self.hidden_size = hidden_size
            self.num_hidden_layers = num_hidden_layers
            self.num_attention_heads = num_attention_heads
            self.hidden_act = hidden_act
            self.intermediate_size = intermediate_size
            self.hidden_dropout_prob = hidden_dropout_prob
            self.attention_probs_dropout_prob = attention_probs_dropout_prob
            self.max_position_embeddings = max_position_embeddings
            self.type_vocab_size = type_vocab_size
            self.initializer_range = initializer_range
        else:
            raise ValueError("First argument must be either a vocabulary size (int)"
                             "or the path to a pretrained model config file (str)")

    @classmethod
    def from_dict(cls, json_object):
        """Constructs a `BertConfig` from a Python dictionary of parameters."""
        config = BertConfig(vocab_size_or_config_json_file=-1)
        for key, value in json_object.items():
            config.__dict__[key] = value
        return config

    @classmethod
    def from_json_file(cls, json_file):
        """Constructs a `BertConfig` from a json file of parameters."""
        with open(json_file, "r", encoding='utf-8') as reader:
            text = reader.read()
        return cls.from_dict(json.loads(text))

    def __repr__(self):
        return str(self.to_json_string())

    def to_dict(self):
        """Serializes this instance to a Python dictionary."""
        output = copy.deepcopy(self.__dict__)
        return output

    def to_json_string(self):
        """Serializes this instance to a JSON string."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

try:
    from apex.normalization.fused_layer_norm import FusedLayerNorm as BertLayerNorm
except ImportError:
    print("Better speed can be achieved with apex installed from https://www.github.com/nvidia/apex.")
    class BertLayerNorm(nn.Module):
        def __init__(self, hidden_size, eps=1e-12):
            """Construct a layernorm module in the TF style (epsilon inside the square root).
            """
            super(BertLayerNorm, self).__init__()
            self.weight = nn.Parameter(torch.ones(hidden_size))
            self.bias = nn.Parameter(torch.zeros(hidden_size))
            self.variance_epsilon = eps

        def forward(self, x):
            u = x.mean(-1, keepdim=True)
            s = (x - u).pow(2).mean(-1, keepdim=True)
            x = (x - u) / torch.sqrt(s + self.variance_epsilon)
            return self.weight * x + self.bias

class BertEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings.
    """
    def __init__(self, config):
        super(BertEmbeddings, self).__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)

        # self.LayerNorm is not snake-cased to stick with TensorFlow model variable name and be able to load
        # any TensorFlow checkpoint file
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, input_ids, token_type_ids=None):
        seq_length = input_ids.size(1)
        position_ids = torch.arange(seq_length, dtype=torch.long, device=input_ids.device)
        position_ids = position_ids.unsqueeze(0).expand_as(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        words_embeddings = self.word_embeddings(input_ids)
        position_embeddings = self.position_embeddings(position_ids)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = words_embeddings + position_embeddings + token_type_embeddings
        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class BertSelfAttention(nn.Module):
    def __init__(self, config):
        super(BertSelfAttention, self).__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads))
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)

        self.dropout = nn.Dropout(config.attention_probs_dropout_prob)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, hidden_states, attention_mask):
        mixed_query_layer = self.query(hidden_states)
        mixed_key_layer = self.key(hidden_states)
        mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)
        return context_layer

class BertDeSelfOutput(nn.Module):
    def __init__(self, config):
        super(BertDeSelfOutput, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        self.qdense = nn.Linear(config.hidden_size, config.hidden_size)
        self.qLayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.qdropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, chidden_states,qhidden_states, cinput_tensor,qinput_tensor):
        chidden_states = self.dense(chidden_states)
        chidden_states = self.dropout(chidden_states)
        chidden_states = self.LayerNorm(chidden_states + cinput_tensor)
        qhidden_states = self.dense(qhidden_states)
        qhidden_states = self.dropout(qhidden_states)
        qhidden_states = self.LayerNorm(qhidden_states + cinput_tensor)

        return chidden_states,qhidden_states

class BertDeAttention(nn.Module):
    def __init__(self, config):
        super(BertDeAttention, self).__init__()
        self.self = BertMultiAttention(config)
        self.output = BertDeSelfOutput(config) #Can use De here

    def forward(self, cinput_tensor,qinput_tensor, attention_mask, qattention_mask):
        cself_output,qself_output = self.self(cinput_tensor,qinput_tensor, attention_mask,qattention_mask)
        cattention_output,qattention_output = self.output(cself_output,qself_output, cinput_tensor,qinput_tensor)

        return cattention_output,qattention_output

class BertDeIntermediate(nn.Module):
    def __init__(self, config):
        super(BertDeIntermediate, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.intermediate_size)
        self.qdense = nn.Linear(config.hidden_size, config.intermediate_size)

        self.intermediate_act_fn = ACT2FN[config.hidden_act] \
            if isinstance(config.hidden_act, str) else config.hidden_act

    def forward(self, chidden_states,qhidden_states):
        #print('In DeIntermediate -dim of chidden_states is',chidden_states.size())
        chidden_states = self.dense(chidden_states)
        chidden_states = self.intermediate_act_fn(chidden_states)

        qhidden_states = self.qdense(qhidden_states)
        qhidden_states = self.intermediate_act_fn(qhidden_states)

        return chidden_states,qhidden_states

class BertDeOutput(nn.Module):
    def __init__(self, config):
        super(BertDeOutput, self).__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        self.qdense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.qLayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.qdropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, chidden_states,qhidden_states, cinput_tensor,qinput_tensor):

        #print('In BertDeOutput - size of chidden_states is',chidden_states.size())
        chidden_states = self.dense(chidden_states)
        chidden_states = self.dropout(chidden_states)
        chidden_states = self.LayerNorm(chidden_states + cinput_tensor)

        qhidden_states = self.qdense(qhidden_states)
        qhidden_states = self.qdropout(qhidden_states)
        qhidden_states = self.qLayerNorm(qhidden_states + qinput_tensor)
        return chidden_states,qhidden_states


class BertDeLayer(nn.Module):
    def __init__(self, config):
        super(BertDeLayer, self).__init__()
        self.attention = BertDeAttention(config)
        self.intermediate = BertDeIntermediate(config)
        self.output = BertDeOutput(config)

    def forward(self, chidden_states,qhidden_states, attention_mask,qattention_mask):
        cattention_output,qattention_output = self.attention(chidden_states,qhidden_states, attention_mask,qattention_mask)
        #Call this one more time to calculaye qattention_output^
        #print('In DeLayer - dim of cattention_output',cattention_output.size())
        cintermediate_output,qintermediate_output = self.intermediate(cattention_output,qattention_output)
        clayer_output,qlayer_output = self.output(cintermediate_output,qintermediate_output,cattention_output,qattention_output)
        return clayer_output,qlayer_output

class BertDecoder(nn.Module):
    def __init__(self, config):
        super(BertDecoder, self).__init__()
        layer = BertDeLayer(config)
        self.layer = nn.ModuleList([copy.deepcopy(layer) for _ in range(6)])

    def forward(self, hidden_states, cattention_mask, qattention_mask, output_all_deencoded_layers=True):
        call_deencoder_layers = []
        qall_deencoder_layers = []
        chidden_states = hidden_states
        qhidden_states = hidden_states
        for layer_module in self.layer:
            chidden_states,qhidden_states = layer_module(chidden_states,qhidden_states, cattention_mask,qattention_mask)
            if output_all_deencoded_layers:
                call_deencoder_layers.append(chidden_states)
                qall_deencoder_layers.append(qhidden_states)

        if not output_all_deencoded_layers:
            call_deencoder_layers.append(chidden_states)
            qall_deencoder_layers.append(qhidden_states)

        return call_deencoder_layers,qall_deencoder_layers


class BertSelfOutput(nn.Module):
    def __init__(self, config):
        super(BertSelfOutput, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states

class BertMultiAttention(nn.Module):
    def __init__(self, config):
        super(BertMultiAttention, self).__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads))
        self.num_attention_heads = config.num_attention_heads
        self.attention_head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size


        #print('config.hidden_size is',config.hidden_size)

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        torch.nn.init.xavier_uniform_(self.query.weight)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        torch.nn.init.xavier_uniform_(self.key.weight)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)
        torch.nn.init.xavier_uniform_(self.value.weight)


        self.qquery = nn.Linear(config.hidden_size, self.all_head_size)
        torch.nn.init.xavier_uniform_(self.qquery.weight)
        self.qkey = nn.Linear(config.hidden_size, self.all_head_size)
        torch.nn.init.xavier_uniform_(self.qkey.weight)
        self.qvalue = nn.Linear(config.hidden_size, self.all_head_size)
        torch.nn.init.xavier_uniform_(self.qvalue.weight)


        self.cdropout = nn.Dropout(config.attention_probs_dropout_prob)
        self.qdropout = nn.Dropout(config.attention_probs_dropout_prob)
    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

   #NOTE -
    # 1. enc_hidden_states is context embeddings
    # 2. dec_hidden_states is question embeddings
    # start expt with query from dec_hidden_states
    # key and value from context

    def forward(self, enc_hidden_states,dec_hidden_states, attention_mask,qattention_mask):
        #print('forward of decoder')
        #print('shape of dec_hidden_states is',dec_hidden_states.shape)
        #print('size of self.all_head_size is',self.all_head_size)
        mixed_query_layer = self.query(dec_hidden_states)
        mixed_key_layer = self.key(enc_hidden_states)
        mixed_value_layer = self.value(enc_hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.cdropout(attention_probs)
        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        #--- Q2C

        qmixed_query_layer = self.qquery(enc_hidden_states)
        qmixed_key_layer = self.qkey(dec_hidden_states)
        qmixed_value_layer = self.qvalue(dec_hidden_states)

        qquery_layer = self.transpose_for_scores(qmixed_query_layer)
        qkey_layer = self.transpose_for_scores(qmixed_key_layer)
        qvalue_layer = self.transpose_for_scores(qmixed_value_layer)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        qattention_scores = torch.matmul(qquery_layer, qkey_layer.transpose(-1, -2))
        qattention_scores = qattention_scores / math.sqrt(self.attention_head_size)
        # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
        qattention_scores = qattention_scores + qattention_mask

        # Normalize the attention scores to probabilities.
        qattention_probs = nn.Softmax(dim=-1)(qattention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        qattention_probs = self.qdropout(qattention_probs)

        q_layer = torch.matmul(qattention_probs, qvalue_layer)
        q_layer = q_layer.permute(0, 2, 1, 3).contiguous()
        new_q_layer_shape = q_layer.size()[:-2] + (self.all_head_size,)
        q_layer = q_layer.view(*new_q_layer_shape)

        return context_layer,q_layer



class BertAttention(nn.Module):
    def __init__(self, config):
        super(BertAttention, self).__init__()
        self.self = BertSelfAttention(config)
        self.output = BertSelfOutput(config)

    def forward(self, input_tensor, attention_mask):
        self_output = self.self(input_tensor, attention_mask)
        attention_output = self.output(self_output, input_tensor)
        return attention_output


class BertIntermediate(nn.Module):
    def __init__(self, config):
        super(BertIntermediate, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.intermediate_size)
        self.intermediate_act_fn = ACT2FN[config.hidden_act] \
            if isinstance(config.hidden_act, str) else config.hidden_act

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class BertOutput(nn.Module):
    def __init__(self, config):
        super(BertOutput, self).__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertLayer(nn.Module):
    def __init__(self, config):
        super(BertLayer, self).__init__()
        self.attention = BertAttention(config)
        self.intermediate = BertIntermediate(config)
        self.output = BertOutput(config)

    def forward(self, hidden_states, attention_mask):
        attention_output = self.attention(hidden_states, attention_mask)
        #print('attention_output is',attention_output.shape)
        intermediate_output = self.intermediate(attention_output)
        #print('intermediate_output is',intermediate_output.shape)
        layer_output = self.output(intermediate_output, attention_output)
        #print('layer_output is',layer_output.shape)
        return layer_output


class BertEncoder(nn.Module):
    def __init__(self, config):
        super(BertEncoder, self).__init__()
        layer = BertLayer(config)
        self.layer = nn.ModuleList([copy.deepcopy(layer) for _ in range(config.num_hidden_layers)])

    def forward(self, hidden_states, attention_mask, output_all_encoded_layers=True):
        all_encoder_layers = []
        for layer_module in self.layer:
            hidden_states = layer_module(hidden_states, attention_mask)
            #print('size of hidden_states in BertEncoder is',hidden_states.shape)
            if output_all_encoded_layers:
                all_encoder_layers.append(hidden_states)
        if not output_all_encoded_layers:
            all_encoder_layers.append(hidden_states)
        return all_encoder_layers


class BertPooler(nn.Module):
    def __init__(self, config):
        super(BertPooler, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class BertPredictionHeadTransform(nn.Module):
    def __init__(self, config):
        super(BertPredictionHeadTransform, self).__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.transform_act_fn = ACT2FN[config.hidden_act] \
            if isinstance(config.hidden_act, str) else config.hidden_act
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.transform_act_fn(hidden_states)
        hidden_states = self.LayerNorm(hidden_states)
        return hidden_states


class BertLMPredictionHead(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertLMPredictionHead, self).__init__()
        self.transform = BertPredictionHeadTransform(config)

        # The output weights are the same as the input embeddings, but there is
        # an output-only bias for each token.
        self.decoder = nn.Linear(bert_model_embedding_weights.size(1),
                                 bert_model_embedding_weights.size(0),
                                 bias=False)
        self.decoder.weight = bert_model_embedding_weights
        self.bias = nn.Parameter(torch.zeros(bert_model_embedding_weights.size(0)))

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        hidden_states = self.decoder(hidden_states) + self.bias
        return hidden_states


class BertOnlyMLMHead(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertOnlyMLMHead, self).__init__()
        self.predictions = BertLMPredictionHead(config, bert_model_embedding_weights)

    def forward(self, sequence_output):
        prediction_scores = self.predictions(sequence_output)
        return prediction_scores


class BertOnlyNSPHead(nn.Module):
    def __init__(self, config):
        super(BertOnlyNSPHead, self).__init__()
        self.seq_relationship = nn.Linear(config.hidden_size, 2)

    def forward(self, pooled_output):
        seq_relationship_score = self.seq_relationship(pooled_output)
        return seq_relationship_score


class BertPreTrainingHeads(nn.Module):
    def __init__(self, config, bert_model_embedding_weights):
        super(BertPreTrainingHeads, self).__init__()
        self.predictions = BertLMPredictionHead(config, bert_model_embedding_weights)
        self.seq_relationship = nn.Linear(config.hidden_size, 2)

    def forward(self, sequence_output, pooled_output):
        prediction_scores = self.predictions(sequence_output)
        seq_relationship_score = self.seq_relationship(pooled_output)
        return prediction_scores, seq_relationship_score


class PreTrainedBertModel(nn.Module):
    """ An abstract class to handle weights initialization and
        a simple interface for dowloading and loading pretrained models.
    """
    def __init__(self, config, *inputs, **kwargs):
        super(PreTrainedBertModel, self).__init__()
        if not isinstance(config, BertConfig):
            raise ValueError(
                "Parameter config in `{}(config)` should be an instance of class `BertConfig`. "
                "To create a model from a Google pretrained model use "
                "`model = {}.from_pretrained(PRETRAINED_MODEL_NAME)`".format(
                    self.__class__.__name__, self.__class__.__name__
                ))
        self.config = config

    def init_bert_weights(self, module):
        """ Initialize the weights.
        """
        if isinstance(module, (nn.Linear, nn.Embedding)):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
        elif isinstance(module, BertLayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)
        if isinstance(module, nn.Linear) and module.bias is not None:
            module.bias.data.zero_()

    @classmethod
    def from_pretrained(cls, pretrained_model_name, state_dict=None, cache_dir=None, *inputs, **kwargs):
        """
        Instantiate a PreTrainedBertModel from a pre-trained model file or a pytorch state dict.
        Download and cache the pre-trained model file if needed.

        Params:
            pretrained_model_name: either:
                - a str with the name of a pre-trained model to load selected in the list of:
                    . `bert-base-uncased`
                    . `bert-large-uncased`
                    . `bert-base-cased`
                    . `bert-large-cased`
                    . `bert-base-multilingual-uncased`
                    . `bert-base-multilingual-cased`
                    . `bert-base-chinese`
                - a path or url to a pretrained model archive containing:
                    . `bert_config.json` a configuration file for the model
                    . `pytorch_model.bin` a PyTorch dump of a BertForPreTraining instance
            cache_dir: an optional path to a folder in which the pre-trained models will be cached.
            state_dict: an optional state dictionnary (collections.OrderedDict object) to use instead of Google pre-trained models
            *inputs, **kwargs: additional input for the specific Bert class
                (ex: num_labels for BertForSequenceClassification)
        """
        if pretrained_model_name in PRETRAINED_MODEL_ARCHIVE_MAP:
            archive_file = PRETRAINED_MODEL_ARCHIVE_MAP[pretrained_model_name]
        else:
            archive_file = pretrained_model_name
        # redirect to the cache, if necessary
        try:
            resolved_archive_file = cached_path(archive_file, cache_dir=cache_dir)
        except FileNotFoundError:
            logger.error(
                "Model name '{}' was not found in model name list ({}). "
                "We assumed '{}' was a path or url but couldn't find any file "
                "associated to this path or url.".format(
                    pretrained_model_name,
                    ', '.join(PRETRAINED_MODEL_ARCHIVE_MAP.keys()),
                    archive_file))
            return None
        if resolved_archive_file == archive_file:
            logger.info("loading archive file {}".format(archive_file))
        else:
            logger.info("loading archive file {} from cache at {}".format(
                archive_file, resolved_archive_file))
        tempdir = None
        if os.path.isdir(resolved_archive_file):
            serialization_dir = resolved_archive_file
        else:
            # Extract archive to temp dir
            tempdir = tempfile.mkdtemp()
            logger.info("extracting archive file {} to temp dir {}".format(
                resolved_archive_file, tempdir))
            with tarfile.open(resolved_archive_file, 'r:gz') as archive:
                def is_within_directory(directory, target):
                    
                    abs_directory = os.path.abspath(directory)
                    abs_target = os.path.abspath(target)
                
                    prefix = os.path.commonprefix([abs_directory, abs_target])
                    
                    return prefix == abs_directory
                
                def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
                
                    for member in tar.getmembers():
                        member_path = os.path.join(path, member.name)
                        if not is_within_directory(path, member_path):
                            raise Exception("Attempted Path Traversal in Tar File")
                
                    tar.extractall(path, members, numeric_owner=numeric_owner) 
                    
                
                safe_extract(archive, tempdir)
            serialization_dir = tempdir
        # Load config
        config_file = os.path.join(serialization_dir, CONFIG_NAME)
        config = BertConfig.from_json_file(config_file)
        logger.info("Model config {}".format(config))
        # Instantiate model.
        model = cls(config, *inputs, **kwargs)
        if state_dict is None:
            weights_path = os.path.join(serialization_dir, WEIGHTS_NAME)
            state_dict = torch.load(weights_path)

        old_keys = []
        new_keys = []
        for key in state_dict.keys():
            new_key = None
            if 'gamma' in key:
                new_key = key.replace('gamma', 'weight')
            if 'beta' in key:
                new_key = key.replace('beta', 'bias')
            if new_key:
                old_keys.append(key)
                new_keys.append(new_key)
        for old_key, new_key in zip(old_keys, new_keys):
            state_dict[new_key] = state_dict.pop(old_key)

        missing_keys = []
        unexpected_keys = []
        error_msgs = []
        # copy state_dict so _load_from_state_dict can modify it
        metadata = getattr(state_dict, '_metadata', None)
        state_dict = state_dict.copy()
        if metadata is not None:
            state_dict._metadata = metadata

        def load(module, prefix=''):
            local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
            module._load_from_state_dict(
                state_dict, prefix, local_metadata, True, missing_keys, unexpected_keys, error_msgs)
            for name, child in module._modules.items():
                if child is not None:
                    load(child, prefix + name + '.')
        load(model, prefix='' if hasattr(model, 'bert') else 'bert.')
        if len(missing_keys) > 0:
            logger.info("Weights of {} not initialized from pretrained model: {}".format(
                model.__class__.__name__, missing_keys))
        if len(unexpected_keys) > 0:
            logger.info("Weights from pretrained model not used in {}: {}".format(
                model.__class__.__name__, unexpected_keys))
        if tempdir:
            # Clean up temp dir
            shutil.rmtree(tempdir)
        return model


class BertModel(PreTrainedBertModel):
    """BERT model ("Bidirectional Embedding Representations from a Transformer").

    Params:
        config: a BertConfig class instance with the configuration to build a new model

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `output_all_encoded_layers`: boolean which controls the content of the `encoded_layers` output as described below. Default: `True`.

    Outputs: Tuple of (encoded_layers, pooled_output)
        `encoded_layers`: controled by `output_all_encoded_layers` argument:
            - `output_all_encoded_layers=True`: outputs a list of the full sequences of encoded-hidden-states at the end
                of each attention block (i.e. 12 full sequences for BERT-base, 24 for BERT-large), each
                encoded-hidden-state is a torch.FloatTensor of size [batch_size, sequence_length, hidden_size],
            - `output_all_encoded_layers=False`: outputs only the full sequence of hidden-states corresponding
                to the last attention block of shape [batch_size, sequence_length, hidden_size],
        `pooled_output`: a torch.FloatTensor of size [batch_size, hidden_size] which is the output of a
            classifier pretrained on top of the hidden state associated to the first character of the
            input (`CLF`) to train on the Next-Sentence task (see BERT's paper).

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = modeling.BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = modeling.BertModel(config=config)
    all_encoder_layers, pooled_output = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertModel, self).__init__(config)
        self.embeddings = BertEmbeddings(config)
        self.encoder = BertEncoder(config)
        self.pooler = BertPooler(config)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, output_all_encoded_layers=True):
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        # We create a 3D attention mask from a 2D tensor mask.
        # Sizes are [batch_size, 1, 1, to_seq_length]
        # So we can broadcast to [batch_size, num_heads, from_seq_length, to_seq_length]
        # this attention mask is more simple than the triangular masking of causal attention
        # used in OpenAI GPT, we just need to prepare the broadcast dimension here.
        extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)

        # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
        # masked positions, this operation will create a tensor which is 0.0 for
        # positions we want to attend and -10000.0 for masked positions.
        # Since we are adding it to the raw scores before the softmax, this is
        # effectively the same as removing these entirely.
        extended_attention_mask = extended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
        extended_attention_mask = (1.0 - extended_attention_mask) * -10000.0
        #print('extended_attention_mask',extended_attention_mask)
        #print('attention_mask',attention_mask)
        #print('token_type_ids',token_type_ids)
        qattention_mask = attention_mask - token_type_ids
        cattention_mask = attention_mask - qattention_mask
        #print('*************************')
        #print('cattention_mask',cattention_mask)
        #print('qattention_mask',qattention_mask)

        cextended_attention_mask = cattention_mask.unsqueeze(1).unsqueeze(2)
        cextended_attention_mask = cextended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
        cextended_attention_mask = (1.0 - cextended_attention_mask) * -10000.0

        qextended_attention_mask = qattention_mask.unsqueeze(1).unsqueeze(2)
        qextended_attention_mask = qextended_attention_mask.to(dtype=next(self.parameters()).dtype) # fp16 compatibility
        qextended_attention_mask = (1.0 - qextended_attention_mask) * -10000.0


        #raise SystemExit
        embedding_output = self.embeddings(input_ids, token_type_ids)
        encoded_layers = self.encoder(embedding_output,
                                      extended_attention_mask,
                                      output_all_encoded_layers=output_all_encoded_layers)
        #print('*** encoded_layers is',encoded_layers.shape)
        sequence_output = encoded_layers[-1]

        #print('*** sequence_output is',sequence_output.shape)

        pooled_output = self.pooler(sequence_output)
        if not output_all_encoded_layers:
            encoded_layers = encoded_layers[-1]
        return cextended_attention_mask,qextended_attention_mask,sequence_output#encoded_layers, pooled_output


class BertForPreTraining(PreTrainedBertModel):
    """BERT model with pre-training heads.
    This module comprises the BERT model followed by the two pre-training heads:
        - the masked language modeling head, and
        - the next sentence classification head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `masked_lm_labels`: masked language modeling labels: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [-1, 0, ..., vocab_size]. All labels set to -1 are ignored (masked), the loss
            is only computed for the labels set in [0, ..., vocab_size]
        `next_sentence_label`: next sentence classification loss: torch.LongTensor of shape [batch_size]
            with indices selected in [0, 1].
            0 => next sentence is the continuation, 1 => next sentence is a random sentence.

    Outputs:
        if `masked_lm_labels` and `next_sentence_label` are not `None`:
            Outputs the total_loss which is the sum of the masked language modeling loss and the next
            sentence classification loss.
        if `masked_lm_labels` or `next_sentence_label` is `None`:
            Outputs a tuple comprising
            - the masked language modeling logits of shape [batch_size, sequence_length, vocab_size], and
            - the next sentence classification logits of shape [batch_size, 2].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForPreTraining(config)
    masked_lm_logits_scores, seq_relationship_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForPreTraining, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertPreTrainingHeads(config, self.bert.embeddings.word_embeddings.weight)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None, next_sentence_label=None):
        sequence_output, pooled_output = self.bert(input_ids, token_type_ids, attention_mask,
                                                   output_all_encoded_layers=False)
        prediction_scores, seq_relationship_score = self.cls(sequence_output, pooled_output)

        if masked_lm_labels is not None and next_sentence_label is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            total_loss = masked_lm_loss + next_sentence_loss
            return total_loss
        else:
            return prediction_scores, seq_relationship_score


class BertForMaskedLM(PreTrainedBertModel):
    """BERT model with the masked language modeling head.
    This module comprises the BERT model followed by the masked language modeling head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `masked_lm_labels`: masked language modeling labels: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [-1, 0, ..., vocab_size]. All labels set to -1 are ignored (masked), the loss
            is only computed for the labels set in [0, ..., vocab_size]

    Outputs:
        if `masked_lm_labels` is  not `None`:
            Outputs the masked language modeling loss.
        if `masked_lm_labels` is `None`:
            Outputs the masked language modeling logits of shape [batch_size, sequence_length, vocab_size].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForMaskedLM(config)
    masked_lm_logits_scores = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForMaskedLM, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertOnlyMLMHead(config, self.bert.embeddings.word_embeddings.weight)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask,
                                       output_all_encoded_layers=False)
        prediction_scores = self.cls(sequence_output)

        if masked_lm_labels is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            masked_lm_loss = loss_fct(prediction_scores.view(-1, self.config.vocab_size), masked_lm_labels.view(-1))
            return masked_lm_loss
        else:
            return prediction_scores


class BertForNextSentencePrediction(PreTrainedBertModel):
    """BERT model with next sentence prediction head.
    This module comprises the BERT model followed by the next sentence classification head.

    Params:
        config: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `next_sentence_label`: next sentence classification loss: torch.LongTensor of shape [batch_size]
            with indices selected in [0, 1].
            0 => next sentence is the continuation, 1 => next sentence is a random sentence.

    Outputs:
        if `next_sentence_label` is not `None`:
            Outputs the total_loss which is the sum of the masked language modeling loss and the next
            sentence classification loss.
        if `next_sentence_label` is `None`:
            Outputs the next sentence classification logits of shape [batch_size, 2].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForNextSentencePrediction(config)
    seq_relationship_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForNextSentencePrediction, self).__init__(config)
        self.bert = BertModel(config)
        self.cls = BertOnlyNSPHead(config)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, next_sentence_label=None):
        _, pooled_output = self.bert(input_ids, token_type_ids, attention_mask,
                                     output_all_encoded_layers=False)
        seq_relationship_score = self.cls( pooled_output)

        if next_sentence_label is not None:
            loss_fct = CrossEntropyLoss(ignore_index=-1)
            next_sentence_loss = loss_fct(seq_relationship_score.view(-1, 2), next_sentence_label.view(-1))
            return next_sentence_loss
        else:
            return seq_relationship_score


class BertForSequenceClassification(PreTrainedBertModel):
    """BERT model for classification.
    This module is composed of the BERT model with a linear layer on top of
    the pooled output.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_labels`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size]
            with indices selected in [0, ..., num_labels].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_labels = 2

    model = BertForSequenceClassification(config, num_labels)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_labels=2):
        super(BertForSequenceClassification, self).__init__(config)
        self.num_labels = num_labels
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, num_labels)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None):
        _, pooled_output = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            return loss
        else:
            return logits


class BertForMultipleChoice(PreTrainedBertModel):
    """BERT model for multiple choice tasks.
    This module is composed of the BERT model with a linear layer on top of
    the pooled output.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_choices`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, num_choices, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, num_choices, sequence_length]
            with the token types indices selected in [0, 1]. Type 0 corresponds to a `sentence A`
            and type 1 corresponds to a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, num_choices, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size]
            with indices selected in [0, ..., num_choices].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[[31, 51, 99], [15, 5, 0]], [[12, 16, 42], [14, 28, 57]]])
    input_mask = torch.LongTensor([[[1, 1, 1], [1, 1, 0]],[[1,1,0], [1, 0, 0]]])
    token_type_ids = torch.LongTensor([[[0, 0, 1], [0, 1, 0]],[[0, 1, 1], [0, 0, 1]]])
    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_choices = 2

    model = BertForMultipleChoice(config, num_choices)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_choices=2):
        super(BertForMultipleChoice, self).__init__(config)
        self.num_choices = num_choices
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, 1)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None):
        flat_input_ids = input_ids.view(-1, input_ids.size(-1))
        flat_token_type_ids = token_type_ids.view(-1, token_type_ids.size(-1))
        flat_attention_mask = attention_mask.view(-1, attention_mask.size(-1))
        _, pooled_output = self.bert(flat_input_ids, flat_token_type_ids, flat_attention_mask, output_all_encoded_layers=False)
        pooled_output = self.dropout(pooled_output)
        logits = self.classifier(pooled_output)
        reshaped_logits = logits.view(-1, self.num_choices)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(reshaped_logits, labels)
            return loss
        else:
            return reshaped_logits


class BertForTokenClassification(PreTrainedBertModel):
    """BERT model for token-level classification.
    This module is composed of the BERT model with a linear layer on top of
    the full hidden state of the last layer.

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.
        `num_labels`: the number of classes for the classifier. Default = 2.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `labels`: labels for the classification output: torch.LongTensor of shape [batch_size, sequence_length]
            with indices selected in [0, ..., num_labels].

    Outputs:
        if `labels` is not `None`:
            Outputs the CrossEntropy classification loss of the output with the labels.
        if `labels` is `None`:
            Outputs the classification logits of shape [batch_size, sequence_length, num_labels].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    num_labels = 2

    model = BertForTokenClassification(config, num_labels)
    logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config, num_labels=2):
        super(BertForTokenClassification, self).__init__(config)
        self.num_labels = num_labels
        self.bert = BertModel(config)
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(config.hidden_size, num_labels)
        self.apply(self.init_bert_weights)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None, labels=None):
        sequence_output, _ = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        sequence_output = self.dropout(sequence_output)
        logits = self.classifier(sequence_output)

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            # Only keep active parts of the loss
            if attention_mask is not None:
                active_loss = attention_mask.view(-1) == 1
                active_logits = logits.view(-1, self.num_labels)[active_loss]
                active_labels = labels.view(-1)[active_loss]
                loss = loss_fct(active_logits, active_labels)
            else:
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            return loss
        else:
            return logits


class BertForQuestionAnswering(PreTrainedBertModel):
    """BERT model for Question Answering (span extraction).
    This module is composed of the BERT model with a linear layer on top of
    the sequence output that computes start_logits and end_logits

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `start_positions`: position of the first token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.
        `end_positions`: position of the last token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.

    Outputs:
        if `start_positions` and `end_positions` are not `None`:
            Outputs the total_loss which is the sum of the CrossEntropy loss for the start and end token positions.
        if `start_positions` or `end_positions` is `None`:
            Outputs a tuple of start_logits, end_logits which are the logits respectively for the start and end
            position tokens of shape [batch_size, sequence_length].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForQuestionAnswering(config)
    start_logits, end_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForQuestionAnswering, self).__init__(config)
        self.bert = BertModel(config)
        self.enc_trans = nn.Linear(2*config.max_position_embeddings,config.max_position_embeddings)
        # TODO check with Google if it's normal there is no dropout on the token classifier of SQuAD in the TF version
        # self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.qa_outputs = nn.Linear(config.hidden_size, 2)
        #print('hidden size in QA is',config.hidden_size)
        self.apply(self.init_bert_weights)
        self.conv1d = nn.Conv1d(in_channels=2,out_channels=1,kernel_size=3,padding=1)
        self.decoder = BertDecoder(config)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        ks=3
        #convs
        self.conv_512_1= nn.Conv1d(in_channels=768, out_channels=512, kernel_size=ks, padding=1)
        self.conv_512_2= nn.Conv1d(in_channels=512, out_channels=512, kernel_size=ks, padding=1)
        self.conv_256_1= nn.Conv1d(in_channels=512, out_channels=256, kernel_size=ks, padding=1)
        self.conv_256_2= nn.Conv1d(in_channels=256, out_channels=256, kernel_size=ks, padding=1)
        self.conv_128_1= nn.Conv1d(in_channels=256, out_channels=128, kernel_size=ks, padding=1)
        self.conv_128_2= nn.Conv1d(in_channels=128, out_channels=128, kernel_size=ks, padding=1)

        self.conv_64_1= nn.Conv1d(in_channels=128, out_channels=64, kernel_size=ks, padding=1)
        self.conv_64_2= nn.Conv1d(in_channels=64, out_channels=64, kernel_size=ks, padding=1)
        self.conv_32_1= nn.Conv1d(in_channels=64, out_channels=32, kernel_size=ks, padding=1)
        self.conv_32_2= nn.Conv1d(in_channels=32, out_channels=32, kernel_size=ks, padding=1)
        self.conv_16_1= nn.Conv1d(in_channels=32, out_channels=16, kernel_size=ks, padding=1)
        self.conv_16_2= nn.Conv1d(in_channels=16, out_channels=16, kernel_size=ks, padding=1)

        self.conv_8_1= nn.Conv1d(in_channels=16, out_channels=8, kernel_size=ks, padding=1)
        self.conv_8_2= nn.Conv1d(in_channels=8, out_channels=8, kernel_size=ks, padding=1)
        self.conv_4_1= nn.Conv1d(in_channels=8, out_channels=4, kernel_size=ks, padding=1)
        self.conv_4_2= nn.Conv1d(in_channels=4, out_channels=4, kernel_size=ks, padding=1)

        self.conv_out=nn.Conv1d(in_channels=4, out_channels=2, kernel_size=ks, padding=1)
        #Freeze embedding layers of Bert
        #for param in self.bert.parameters():
        #  param.requires_grad = False
        #  print(param)



    def forward(self, input_ids, token_type_ids=None, attention_mask=None, start_positions=None, end_positions=None):
        c_attention_mask,q_attention_mask,sequence_output = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        #print('shape of sequence_output',sequence_output.shape)
        #Ankit addition - Decoder
        cdeencoded_layers,qdeencoded_layers = self.decoder(sequence_output, #2d --> 1d translated
                                      c_attention_mask,q_attention_mask,
                                      output_all_deencoded_layers=False)#output_all_encoded_layers)

        cdeencoded_layers = cdeencoded_layers[-1]
        qdeencoded_layers = qdeencoded_layers[-1]

        cdeencoded_layers = cdeencoded_layers.unsqueeze(-1)
        qdeencoded_layers = qdeencoded_layers.unsqueeze(-1)
        enc_cat = torch.cat((cdeencoded_layers,qdeencoded_layers), dim=-1)
        #print('enc_cat size is',enc_cat.size())
        #enc_cat = enc_cat.permute(0,2,1)


        encshape = enc_cat.shape

        #print('AFTERPERMUTE - enc_cat size is',enc_cat.size())
        #sequence_output1d = self.enc_trans(enc_cat)
        #print('Dim of sequence_output is',sequence_output1d.size())
        enc_cat = enc_cat.reshape(-1,enc_cat.shape[2],enc_cat.shape[3]).contiguous()

        #print('AFTER : enc_cat size is',enc_cat.size())
        enc_cat = enc_cat.permute(0,2,1).contiguous()
        sequence_output1d = self.conv1d(enc_cat)
        #print('shape of sequence_output1d',sequence_output1d.shape)
        sequence_output1d = sequence_output1d.squeeze(1).contiguous()
        sequence_output1d =  sequence_output1d.reshape(encshape[0],encshape[1],encshape[2])

        #Skip connection with bert embeddings
        sequence_output1d = self.LayerNorm(sequence_output + sequence_output1d)

        sequence_output1d = sequence_output1d.permute(0,2,1).contiguous()
        #print('seq after perm: ', sequence_output.shape)
        out_512_1=self.conv_512_1(sequence_output1d)
        #print('out 512 1 shape', out_512_1.shape)
        out_512_2=self.conv_512_2(out_512_1)
        #print('out 512 2 shape', out_512_2.shape)
        #elem_1=self.LayerNorm(sequence_output+out_512_2)
        #print('512 size: ', elem_1.shape)
        out_256_1=self.conv_256_1(out_512_2)
        out_256_2=self.conv_256_2(out_256_1)
        #elem_2=self.LayerNorm(sequence_output+out_256_2)
        #print('256 size: ', elem_2.shape)
        out_128_1=self.conv_128_1(out_256_2)
        out_128_2=self.conv_128_2(out_128_1)
        #elem_3=self.LayerNorm(sequence_output+out_128_2)
        #print('128 size: ', elem_3.shape)
        out_64_1=self.conv_64_1(out_128_2)
        out_64_2=self.conv_64_2(out_64_1)
        #elem_4=self.LayerNorm(sequence_output+out_64_2)
        #print('64 size: ', elem_4.shape)
        out_32_1=self.conv_32_1(out_64_2)
        out_32_2=self.conv_32_2(out_32_1)
        #elem_5=self.LayerNorm(sequence_output+out_32_2)
        #print('32 size: ', elem_5.shape)
        out_16_1=self.conv_16_1(out_32_2)
        out_16_2=self.conv_16_2(out_16_1)
        #elem_6=self.LayerNorm(sequence_output+out_16_2)
        #print('16 size: ', elem_6.shape)
        out_8_1=self.conv_8_1(out_16_2)
        out_8_2=self.conv_8_2(out_8_1)
        #elem_7=self.LayerNorm(sequence_output+out_8_2)
        #print('8 size: ', elem_7.shape)
        out_4_1=self.conv_4_1(out_8_2)
        out_4_2=self.conv_4_2(out_4_1)
        #elem_8=self.LayerNorm(sequence_output+out_4_2)
        #print('4 size: ', elem_8.shape)
        out=self.conv_out(out_4_2)
        #print('out before perm: ', out.shape)
        out = out.permute(0,2,1).contiguous()
        #out = self.LayerNorm2(out)
        #print('out after perm: ', out.shape)

        logits=out
        #logits = self.qa_outputs(sequence_output1d)
        #print('Dim of logits is',logits.size())

        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            return total_loss
        else:
            return start_logits, end_logits


class BertForCMRC2019Conv(PreTrainedBertModel):
    """BERT model for Question Answering (span extraction).
    This module is composed of the BERT model with a linear layer on top of
    the sequence output that computes start_logits and end_logits

    Params:
        `config`: a BertConfig class instance with the configuration to build a new model.

    Inputs:
        `input_ids`: a torch.LongTensor of shape [batch_size, sequence_length]
            with the word token indices in the vocabulary(see the tokens preprocessing logic in the scripts
            `extract_features.py`, `run_classifier.py` and `run_squad.py`)
        `token_type_ids`: an optional torch.LongTensor of shape [batch_size, sequence_length] with the token
            types indices selected in [0, 1]. Type 0 corresponds to a `sentence A` and type 1 corresponds to
            a `sentence B` token (see BERT paper for more details).
        `attention_mask`: an optional torch.LongTensor of shape [batch_size, sequence_length] with indices
            selected in [0, 1]. It's a mask to be used if the input sequence length is smaller than the max
            input sequence length in the current batch. It's the mask that we typically use for attention when
            a batch has varying length sentences.
        `start_positions`: position of the first token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.
        `end_positions`: position of the last token for the labeled span: torch.LongTensor of shape [batch_size].
            Positions are clamped to the length of the sequence and position outside of the sequence are not taken
            into account for computing the loss.

    Outputs:
        if `start_positions` and `end_positions` are not `None`:
            Outputs the total_loss which is the sum of the CrossEntropy loss for the start and end token positions.
        if `start_positions` or `end_positions` is `None`:
            Outputs a tuple of start_logits, end_logits which are the logits respectively for the start and end
            position tokens of shape [batch_size, sequence_length].

    Example usage:
    ```python
    # Already been converted into WordPiece token ids
    input_ids = torch.LongTensor([[31, 51, 99], [15, 5, 0]])
    input_mask = torch.LongTensor([[1, 1, 1], [1, 1, 0]])
    token_type_ids = torch.LongTensor([[0, 0, 1], [0, 1, 0]])

    config = BertConfig(vocab_size_or_config_json_file=32000, hidden_size=768,
        num_hidden_layers=12, num_attention_heads=12, intermediate_size=3072)

    model = BertForQuestionAnswering(config)
    start_logits, end_logits = model(input_ids, token_type_ids, input_mask)
    ```
    """
    def __init__(self, config):
        super(BertForCMRC2019Conv, self).__init__(config)
        self.bert = BertModel(config)
        self.enc_trans = nn.Linear(2*config.max_position_embeddings,config.max_position_embeddings)
        # TODO check with Google if it's normal there is no dropout on the token classifier of SQuAD in the TF version
        # self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.qa_outputs = nn.Linear(config.hidden_size, 1)
        #print('hidden size in QA is',config.hidden_size)
        self.apply(self.init_bert_weights)
        self.conv1d = nn.Conv1d(in_channels=2,out_channels=1,kernel_size=3,padding=1)
        self.decoder = BertDecoder(config)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=1e-12)
        ks=3
        #convs
        self.conv_512_1= nn.Conv1d(in_channels=768, out_channels=512, kernel_size=ks, padding=1)
        self.conv_512_2= nn.Conv1d(in_channels=512, out_channels=512, kernel_size=ks, padding=1)
        self.conv_256_1= nn.Conv1d(in_channels=512, out_channels=256, kernel_size=ks, padding=1)
        self.conv_256_2= nn.Conv1d(in_channels=256, out_channels=256, kernel_size=ks, padding=1)
        self.conv_128_1= nn.Conv1d(in_channels=256, out_channels=128, kernel_size=ks, padding=1)
        self.conv_128_2= nn.Conv1d(in_channels=128, out_channels=128, kernel_size=ks, padding=1)

        self.conv_64_1= nn.Conv1d(in_channels=128, out_channels=64, kernel_size=ks, padding=1)
        self.conv_64_2= nn.Conv1d(in_channels=64, out_channels=64, kernel_size=ks, padding=1)
        self.conv_32_1= nn.Conv1d(in_channels=64, out_channels=32, kernel_size=ks, padding=1)
        self.conv_32_2= nn.Conv1d(in_channels=32, out_channels=32, kernel_size=ks, padding=1)
        self.conv_16_1= nn.Conv1d(in_channels=32, out_channels=16, kernel_size=ks, padding=1)
        self.conv_16_2= nn.Conv1d(in_channels=16, out_channels=16, kernel_size=ks, padding=1)

        self.conv_8_1= nn.Conv1d(in_channels=16, out_channels=8, kernel_size=ks, padding=1)
        self.conv_8_2= nn.Conv1d(in_channels=8, out_channels=8, kernel_size=ks, padding=1)
        self.conv_4_1= nn.Conv1d(in_channels=8, out_channels=4, kernel_size=ks, padding=1)
        self.conv_4_2= nn.Conv1d(in_channels=4, out_channels=4, kernel_size=ks, padding=1)

        self.conv_out=nn.Conv1d(in_channels=4, out_channels=2, kernel_size=ks, padding=1)
        #Freeze embedding layers of Bert
        #for param in self.bert.parameters():
        #  param.requires_grad = False
        #  print(param)



    def forward(self, input_ids, token_type_ids=None, attention_mask=None, start_positions=None, end_positions=None):
        c_attention_mask,q_attention_mask,sequence_output = self.bert(input_ids, token_type_ids, attention_mask, output_all_encoded_layers=False)
        #print('shape of sequence_output',sequence_output.shape)
        #Ankit addition - Decoder
        cdeencoded_layers,qdeencoded_layers = self.decoder(sequence_output, #2d --> 1d translated
                                      c_attention_mask,q_attention_mask,
                                      output_all_deencoded_layers=False)#output_all_encoded_layers)

        cdeencoded_layers = cdeencoded_layers[-1]
        qdeencoded_layers = qdeencoded_layers[-1]

        cdeencoded_layers = cdeencoded_layers.unsqueeze(-1)
        qdeencoded_layers = qdeencoded_layers.unsqueeze(-1)
        enc_cat = torch.cat((cdeencoded_layers,qdeencoded_layers), dim=-1)
        #print('enc_cat size is',enc_cat.size())
        #enc_cat = enc_cat.permute(0,2,1)


        encshape = enc_cat.shape

        #print('AFTERPERMUTE - enc_cat size is',enc_cat.size())
        #sequence_output1d = self.enc_trans(enc_cat)
        #print('Dim of sequence_output is',sequence_output1d.size())
        enc_cat = enc_cat.reshape(-1,enc_cat.shape[2],enc_cat.shape[3]).contiguous()

        #print('AFTER : enc_cat size is',enc_cat.size())
        enc_cat = enc_cat.permute(0,2,1).contiguous()
        sequence_output1d = self.conv1d(enc_cat)
        #print('shape of sequence_output1d',sequence_output1d.shape)
        sequence_output1d = sequence_output1d.squeeze(1).contiguous()
        sequence_output1d =  sequence_output1d.reshape(encshape[0],encshape[1],encshape[2])

        #Skip connection with bert embeddings
        sequence_output1d = self.LayerNorm(sequence_output + sequence_output1d)

        sequence_output1d = sequence_output1d.permute(0,2,1).contiguous()
        #print('seq after perm: ', sequence_output.shape)
        out_512_1=self.conv_512_1(sequence_output1d)
        #print('out 512 1 shape', out_512_1.shape)
        out_512_2=self.conv_512_2(out_512_1)
        #print('out 512 2 shape', out_512_2.shape)
        #elem_1=self.LayerNorm(sequence_output+out_512_2)
        #print('512 size: ', elem_1.shape)
        out_256_1=self.conv_256_1(out_512_2)
        out_256_2=self.conv_256_2(out_256_1)
        #elem_2=self.LayerNorm(sequence_output+out_256_2)
        #print('256 size: ', elem_2.shape)
        out_128_1=self.conv_128_1(out_256_2)
        out_128_2=self.conv_128_2(out_128_1)
        #elem_3=self.LayerNorm(sequence_output+out_128_2)
        #print('128 size: ', elem_3.shape)
        out_64_1=self.conv_64_1(out_128_2)
        out_64_2=self.conv_64_2(out_64_1)
        #elem_4=self.LayerNorm(sequence_output+out_64_2)
        #print('64 size: ', elem_4.shape)
        out_32_1=self.conv_32_1(out_64_2)
        out_32_2=self.conv_32_2(out_32_1)
        #elem_5=self.LayerNorm(sequence_output+out_32_2)
        #print('32 size: ', elem_5.shape)
        out_16_1=self.conv_16_1(out_32_2)
        out_16_2=self.conv_16_2(out_16_1)
        #elem_6=self.LayerNorm(sequence_output+out_16_2)
        #print('16 size: ', elem_6.shape)
        out_8_1=self.conv_8_1(out_16_2)
        out_8_2=self.conv_8_2(out_8_1)
        #elem_7=self.LayerNorm(sequence_output+out_8_2)
        #print('8 size: ', elem_7.shape)
        out_4_1=self.conv_4_1(out_8_2)
        out_4_2=self.conv_4_2(out_4_1)
        #elem_8=self.LayerNorm(sequence_output+out_4_2)
        #print('4 size: ', elem_8.shape)
        out=self.conv_out(out_4_2)
        #print('out before perm: ', out.shape)
        out = out.permute(0,2,1).contiguous()
        #out = self.LayerNorm2(out)
        #print('out after perm: ', out.shape)

        logits=out
        #logits = self.qa_outputs(sequence_output1d)
        #print('Dim of logits is',logits.size())

        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions.clamp_(0, ignored_index)
            end_positions.clamp_(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
            return total_loss
        else:
            return start_logits, end_logits
