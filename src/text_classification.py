"""IBP text classification model."""
import itertools
import glob
import numpy as np
import os
import pickle
import random
import pandas
import csv

from nltk import word_tokenize
import torch
from torch import nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from tqdm import tqdm

import attacks
import data_util
import ibp
import vocabulary
import shared
import copy

LOSS_FUNC = nn.BCEWithLogitsLoss()
IMDB_DIR = 'data/aclImdb'
AGNEWS_DIR = 'data/agnews'
LM_FILE = 'data/lm_scores/imdb_all.txt'
COUNTER_FITTED_FILE = 'data/counter-fitted-vectors.txt'


class AdversarialModel(nn.Module):
    def __init__(self):
        super(AdversarialModel, self).__init__()

    def query(self, x, vocab, device, return_bounds=False, attack_surface=None):
        """Query the model on a Dataset.

        Args:
          x: a string
          vocab: vocabulary
          device: torch device.

        Returns: list of logits of same length as |examples|.
        """
        if not isinstance(x, tuple):
            raw_data = [(x, 0)]
        else:
            raw_data = [x]
        dataset = TextClassificationDataset.from_raw_data(
            raw_data, vocab, attack_surface=attack_surface)
        data = dataset.get_loader(1)
        with torch.no_grad():
            batch = data_util.dict_batch_to_device(next(iter(data)), device)
            logits = self.forward(batch, compute_bounds=return_bounds)
            if shared.opts.use_agnews_data:
                if isinstance(x, tuple):
                    return logits, batch['y']
                else:
                    return logits
            else:
                if return_bounds:
                    return logits.val[0].item(), (logits.lb[0].item(), logits.ub[0].item())
                else:
                    return logits[0].item()


def attention_pool(x, mask, layer):
    """Attention pooling

    Args:
      x: batch of inputs, shape (B, n, h)
      mask: binary mask, shape (B, n)
      layer: Linear layer mapping h -> 1
    Returns:
      pooled version of x, shape (B, h)
    """
    attn_raw = layer(x).squeeze(2)  # B, n, 1 -> B, n
    attn_raw = ibp.add(attn_raw, (1 - mask) * -1e20)
    attn_logsoftmax = ibp.log_softmax(attn_raw, 1)
    attn_probs = ibp.activation(torch.exp, attn_logsoftmax)  # B, n
    # B, 1, n x B, n, h -> B, h
    return ibp.bmm(attn_probs.unsqueeze(1), x).squeeze(1)


class BOWModel(AdversarialModel):
    """Bag of word vectors + MLP."""

    def __init__(self, word_vec_size, hidden_size, word_mat,
                 pool='max', dropout=0.2, no_wordvec_layer=False, num_labels=1):
        super(BOWModel, self).__init__()
        self.pool = pool
        self.no_wordvec_layer = no_wordvec_layer
        self.embs = ibp.Embedding.from_pretrained(word_mat)
        if no_wordvec_layer:
            self.linear_hidden = ibp.Linear(word_vec_size, hidden_size)
        else:
            self.linear_input = ibp.Linear(word_vec_size, hidden_size)
            self.linear_hidden = ibp.Linear(hidden_size, hidden_size)
        self.linear_output = ibp.Linear(hidden_size, num_labels)
        self.dropout = ibp.Dropout(dropout)
        if self.pool == 'attn':
            self.attn_pool = ibp.Linear(hidden_size, num_labels)
        if num_labels > 1:
            self.log_softmax = ibp.LogSoftmax(dim=1)
        else:
            self.log_softmax = None


    def forward(self, batch, compute_bounds=True, cert_eps=1.0):
        """Forward pass of BOWModel.

        Args:
          batch: A batch dict from a TextClassificationDataset with the following keys:
            - x: tensor of word vector indices, size (B, n, 1)
            - mask: binary mask over words (1 for real, 0 for pad), size (B, n)
            - lengths: lengths of sequences, size (B,)
          compute_bounds: If True compute the interval bounds and reutrn an IntervalBoundedTensor as logits. Otherwise just use the values
          cert_eps: Scaling factor for interval bounds of the input
        """
        if compute_bounds:
            x = batch['x']
        else:
            x = batch['x'].val
        mask = batch['mask']
        lengths = batch['lengths']

        x_vecs = self.embs(x)  # B, n, d
        if not self.no_wordvec_layer:
            x_vecs = self.linear_input(x_vecs)  # B, n, h
        if isinstance(x_vecs, ibp.DiscreteChoiceTensor):
            x_vecs = x_vecs.to_interval_bounded(eps=cert_eps)
        if self.no_wordvec_layer:
            z1 = x_vecs
        else:
            z1 = ibp.activation(F.relu, x_vecs)
        z1_masked = z1 * mask.unsqueeze(-1)  # B, n, h
        if self.pool == 'mean':
            z1_pooled = ibp.sum(
                z1_masked / lengths.to(dtype=torch.float).view(-1, 1, 1), 1)  # B, h
        elif self.pool == 'attn':
            z1_pooled = attention_pool(z1_masked, mask, self.attn_pool)
        else:  # max
            # zero-masking works b/c ReLU guarantees that everything is >= 0
            z1_pooled = ibp.pool(torch.max, z1_masked, 1)  # B, h
        z1_pooled = self.dropout(z1_pooled)
        z2 = ibp.activation(F.relu, self.linear_hidden(z1_pooled))  # B, h
        z2 = self.dropout(z2)
        output = self.linear_output(z2)  # B, 1
        if self.log_softmax:
            output = self.log_softmax(output)
        return output


class CNNModel(AdversarialModel):
    """Convolutional neural network.

    Here is the overall architecture:
      1) Rotate word vectors
      2) One convolutional layer
      3) Max/mean pool across all time
      4) Predict with MLP

    """

    def __init__(self, word_vec_size, hidden_size, kernel_size, word_mat,
                 pool='max', dropout=0.2, no_wordvec_layer=False,
                 early_ibp=False, relu_wordvec=True, unfreeze_wordvec=False, num_labels=1):
        super(CNNModel, self).__init__()
        cnn_padding = (kernel_size - 1) // 2  # preserves size
        self.pool = pool
        # Ablations
        self.no_wordvec_layer = no_wordvec_layer
        self.early_ibp = early_ibp
        self.relu_wordvec = relu_wordvec
        self.unfreeze_wordvec = False
        # End ablations
        self.embs = ibp.Embedding.from_pretrained(
            word_mat, freeze=not self.unfreeze_wordvec)
        if no_wordvec_layer:
            self.conv1 = ibp.Conv1d(word_vec_size, hidden_size, kernel_size,
                                    padding=cnn_padding)
        else:
            self.linear_input = ibp.Linear(word_vec_size, hidden_size)
            self.conv1 = ibp.Conv1d(hidden_size, hidden_size, kernel_size,
                                    padding=cnn_padding)
        if self.pool == 'attn':
            self.attn_pool = ibp.Linear(hidden_size, 1)
        self.dropout = ibp.Dropout(dropout)
        self.fc_hidden = ibp.Linear(hidden_size, hidden_size)
        self.fc_output = ibp.Linear(hidden_size, num_labels)
        if num_labels > 1:
            self.log_softmax = ibp.LogSoftmax(dim=1)
        else:
            self.log_softmax = None

    def forward(self, batch, compute_bounds=True, cert_eps=1.0):
        """
        Args:
          batch: A batch dict from a TextClassificationDataset with the following keys:
            - x: tensor of word vector indices, size (B, n, 1)
            - mask: binary mask over words (1 for real, 0 for pad), size (B, n)
            - lengths: lengths of sequences, size (B,)
          compute_bounds: If True compute the interval bounds and reutrn an IntervalBoundedTensor as logits. Otherwise just use the values
          cert_eps: Scaling factor for interval bounds of the input
        """
        if compute_bounds:
            x = batch['x']
        else:
            x = batch['x'].val
        mask = batch['mask']
        lengths = batch['lengths']

        x_vecs = self.embs(x)  # B, n, d
        if self.early_ibp and isinstance(x_vecs, ibp.DiscreteChoiceTensor):
            x_vecs = x_vecs.to_interval_bounded(eps=cert_eps)
        if not self.no_wordvec_layer:
            x_vecs = self.linear_input(x_vecs)  # B, n, h
        if isinstance(x_vecs, ibp.DiscreteChoiceTensor):
            x_vecs = x_vecs.to_interval_bounded(eps=cert_eps)
        if self.no_wordvec_layer or not self.relu_wordvec:
            z = x_vecs
        else:
            z = ibp.activation(F.relu, x_vecs)  # B, n, h
        z_masked = z * mask.unsqueeze(-1)  # B, n, h
        z_cnn_in = z_masked.permute(0, 2, 1)  # B, h, n
        c1 = ibp.activation(F.relu, self.conv1(z_cnn_in))  # B, h, n
        c1_masked = c1 * mask.unsqueeze(1)  # B, h, n
        if self.pool == 'mean':
            fc_in = ibp.sum(
                c1_masked / lengths.to(dtype=torch.float).view(-1, 1, 1), 2)  # B, h
        elif self.pool == 'attn':
            fc_in = attention_pool(c1_masked.permute(
                0, 2, 1), mask, self.attn_pool)  # B, h
        else:
            # zero-masking works b/c ReLU guarantees that everything is >= 0
            fc_in = ibp.pool(torch.max, c1_masked, 2)  # B, h
        fc_in = self.dropout(fc_in)
        fc_hidden = ibp.activation(F.relu, self.fc_hidden(fc_in))  # B, h
        fc_hidden = self.dropout(fc_hidden)
        output = self.fc_output(fc_hidden)  # B, 1
        if self.log_softmax:
            output = self.log_softmax(output)
        return output


class LSTMModel(AdversarialModel):
    """LSTM text classification model.

    Here is the overall architecture:
      1) Rotate word vectors
      2) Feed to bi-LSTM
      3) Max/mean pool across all time
      4) Predict with MLP

    """

    def __init__(self, word_vec_size, hidden_size, word_mat, device, pool='max', dropout=0.2,
                 no_wordvec_layer=False, num_labels=1):
        super(LSTMModel, self).__init__()
        self.hidden_size = hidden_size
        self.pool = pool
        self.no_wordvec_layer = no_wordvec_layer
        self.device = device
        self.embs = ibp.Embedding.from_pretrained(word_mat)
        if no_wordvec_layer:
            self.lstm = ibp.LSTM(
                word_vec_size, hidden_size, bidirectional=True)
        else:
            self.linear_input = ibp.Linear(word_vec_size, hidden_size)
            self.lstm = ibp.LSTM(hidden_size, hidden_size, bidirectional=True)
        self.dropout = ibp.Dropout(dropout)
        self.fc_hidden = ibp.Linear(2 * hidden_size, hidden_size)
        self.fc_output = ibp.Linear(hidden_size, num_labels)
        if num_labels > 1:
            self.log_softmax = ibp.LogSoftmax(dim=1)
        else:
            self.log_softmax = None


    def forward(self, batch, compute_bounds=True, cert_eps=1.0, analysis_mode=False):
        """
        Args:
          batch: A batch dict from a TextClassificationDataset with the following keys:
            - x: tensor of word vector indices, size (B, n, 1)
            - mask: binary mask over words (1 for real, 0 for pad), size (B, n)
            - lengths: lengths of sequences, size (B,)
          compute_bounds: If True compute the interval bounds and reutrn an IntervalBoundedTensor as logits. Otherwise just use the values
          cert_eps: Scaling factor for interval bounds of the input
        """
        if compute_bounds:
            x = batch['x']
        else:
            x = batch['x'].val
        mask = batch['mask']
        lengths = batch['lengths']

        B = x.shape[0]
        x_vecs = self.embs(x)  # B, n, d
        if not self.no_wordvec_layer:
            x_vecs = self.linear_input(x_vecs)  # B, n, h
        if isinstance(x_vecs, ibp.DiscreteChoiceTensor):
            x_vecs = x_vecs.to_interval_bounded(eps=cert_eps)
        if self.no_wordvec_layer:
            z = x_vecs
        else:
            z = ibp.activation(F.relu, x_vecs)  # B, n, h
        h0 = torch.zeros((B, 2 * self.hidden_size),
                         device=self.device)  # B, 2*h
        c0 = torch.zeros((B, 2 * self.hidden_size),
                         device=self.device)  # B, 2*h
        if analysis_mode:
            h_mat, c_mat, lstm_analysis = self.lstm(
                z, (h0, c0), mask=mask, analysis_mode=True)  # B, n, 2*h each
        else:
            h_mat, c_mat = self.lstm(z, (h0, c0), mask=mask)  # B, n, 2*h each
        h_masked = h_mat * mask.unsqueeze(2)
        if self.pool == 'mean':
            fc_in = ibp.sum(
                h_masked / lengths.to(dtype=torch.float).view(-1, 1, 1), 1)  # B, 2*h
        else:
            raise NotImplementedError()
        fc_in = self.dropout(fc_in)
        fc_hidden = ibp.activation(F.relu, self.fc_hidden(fc_in))  # B, h
        fc_hidden = self.dropout(fc_hidden)
        output = self.fc_output(fc_hidden)  # B, 1
        if analysis_mode:
            return output, h_mat, c_mat, lstm_analysis
        if self.log_softmax:
            output = self.log_softmax(output)
        return output


class LSTMFinalStateModel(AdversarialModel):
    """LSTM text classification model that uses final hidden state."""

    def __init__(self, word_vec_size, hidden_size, word_mat, device, dropout=0.2,
                 no_wordvec_layer=False, num_labels=1):
        super(LSTMFinalStateModel, self).__init__()
        self.hidden_size = hidden_size
        self.no_wordvec_layer = no_wordvec_layer
        self.device = device
        self.embs = ibp.Embedding.from_pretrained(word_mat)
        if no_wordvec_layer:
            self.lstm = ibp.LSTM(
                word_vec_size, hidden_size, bidirectional=True)
        else:
            self.linear_input = ibp.Linear(word_vec_size, hidden_size)
            self.lstm = ibp.LSTM(hidden_size, hidden_size)
        self.dropout = ibp.Dropout(dropout)
        self.fc_hidden = ibp.Linear(hidden_size, hidden_size)
        self.fc_output = ibp.Linear(hidden_size, num_labels)
        if num_labels > 1:
            self.log_softmax = ibp.LogSoftmax(dim=1)
        else:
            self.log_sogtmax = None

    def forward(self, batch, compute_bounds=True, cert_eps=1.0, analysis_mode=False):
        """
        Args:
          batch: A batch dict from a TextClassificationDataset with the following keys:
            - x: tensor of word vector indices, size (B, n, 1)
            - mask: binary mask over words (1 for real, 0 for pad), size (B, n)
            - lengths: lengths of sequences, size (B,)
          compute_bounds: If True compute the interval bounds and reutrn an IntervalBoundedTensor as logits. Otherwise just use the values
          cert_eps: Scaling factor for interval bounds of the input
        """
        if compute_bounds:
            x = batch['x']
        else:
            x = batch['x'].val
        mask = batch['mask']
        lengths = batch['lengths']

        B = x.shape[0]
        x_vecs = self.embs(x)  # B, n, d
        if not self.no_wordvec_layer:
            x_vecs = self.linear_input(x_vecs)  # B, n, h
        if isinstance(x_vecs, ibp.DiscreteChoiceTensor):
            x_vecs = x_vecs.to_interval_bounded(eps=cert_eps)
        if self.no_wordvec_layer:
            z = x_vecs
        else:
            z = ibp.activation(F.relu, x_vecs)  # B, n, h
        h0 = torch.zeros((B, self.hidden_size), device=self.device)  # B, h
        c0 = torch.zeros((B, self.hidden_size), device=self.device)  # B, h
        if analysis_mode:
            h_mat, c_mat, lstm_analysis = self.lstm(
                z, (h0, c0), mask=mask, analysis_mode=True)  # B, n, h each
        else:
            h_mat, c_mat = self.lstm(z, (h0, c0), mask=mask)  # B, n, h each
        h_final = h_mat[:, -1, :]  # B, h
        fc_in = self.dropout(h_final)
        fc_hidden = ibp.activation(F.relu, self.fc_hidden(fc_in))  # B, h
        fc_hidden = self.dropout(fc_hidden)
        output = self.fc_output(fc_hidden)  # B, 1
        if analysis_mode:
            return output, h_mat, c_mat, lstm_analysis
        if self.log_softmax:
            output = self.log_softmax(output)
        return output


class Adversary(object):
    """An Adversary tries to fool a model on a given example."""

    def __init__(self, attack_surface):
        self.attack_surface = attack_surface

    def run(self, model, dataset, device, opts=None):
        """Run adversary on a dataset.

        Args:
          model: a TextClassificationModel.
          dataset: a TextClassificationDataset.
          device: torch device.
        Returns: pair of
          - list of 0-1 adversarial loss of same length as |dataset|
          - list of list of adversarial examples (each is just a text string)
        """
        raise NotImplementedError


class ExhaustiveAdversary(Adversary):
    """An Adversary that exhaustively tries all allowed perturbations.

    Only practical for short sentences.
    """

    def run(self, model, dataset, device, opts=None):
        is_correct = []
        adv_exs = []
        for x, y in dataset.raw_data:
            words = x.split()
            swaps = self.attack_surface.get_swaps(words)
            choices = [[w] + cur_swaps for w, cur_swaps in zip(words, swaps)]
            prod = 1
            for c in choices:
                prod *= len(c)
            print('ExhaustiveAdversary: "%s" -> %d options' % (x, prod))
            all_raw = [(' '.join(x_new), y)
                       for x_new in itertools.product(*choices)]
            cur_dataset = TextClassificationDataset.from_raw_data(
                all_raw, dataset.vocab)
            preds = model.query(cur_dataset, device)
            cur_adv_exs = [all_raw[i][0] for i, p in enumerate(preds)
                           if p * (2 * y - 1) <= 0]
            print(cur_adv_exs)
            adv_exs.append(cur_adv_exs)
            is_correct.append(int(len(cur_adv_exs) > 0))
        return is_correct, adv_exs


class GreedyAdversary(Adversary):
    """An adversary that picks a random word and greedily tries perturbations."""

    def __init__(self, attack_surface, num_epochs=10, num_tries=2, margin_goal=0.0):
        super(GreedyAdversary, self).__init__(attack_surface)
        self.num_epochs = num_epochs
        self.num_tries = num_tries
        self.margin_goal = margin_goal

    def run(self, model, dataset, device, opts=None):
        is_correct = []
        raw_correct= []
        adv_exs = []
        for x, y in tqdm(dataset.raw_data):
            # First query the example itself
            if shared.opts.use_agnews_data:
                orig_pred, orig_gold = model.query((x, y), dataset.vocab, device, return_bounds=True, attack_surface=self.attack_surface)
                model_correct, model_cert_correct = compute_is_correct(orig_pred, orig_gold)
                cert_correct = model_cert_correct.sum().item()
                value_margins, worst_case_margins = get_margins(
                    orig_pred, orig_gold)
                print('Margin: %.6f, lower bound: %.6f, cert_correct=%s' %
                      (value_margins[0].item(), worst_case_margins[0].item(),
                       cert_correct))
                if model_correct.sum().item() <= 0:
                    print('ORIGINAL PREDICTION WAS WRONG')
                    raw_correct.append(0)
                    is_correct.append(0)
                    adv_exs.append(x)
                    continue
            else:
                orig_pred, (orig_lb, orig_ub) = model.query(
                    x, dataset.vocab, device, return_bounds=True,
                    attack_surface=self.attack_surface)
                cert_correct = (orig_lb * (2 * y - 1) >
                                0) and (orig_ub * (2 * y - 1) > 0)
                print('Logit bounds: %.6f <= %.6f <= %.6f, cert_correct=%s' % (
                    orig_lb, orig_pred, orig_ub, cert_correct))
                if orig_pred * (2 * y - 1) <= 0:
                    print('ORIGINAL PREDICTION WAS WRONG')
                    raw_correct.append(0)
                    is_correct.append(0)
                    adv_exs.append(x)
                    continue

            # Now run adversarial search
            words = x.split()
            swaps = self.attack_surface.get_swaps(words)
            choices = [[w] + cur_swaps for w, cur_swaps in zip(words, swaps)]
            max_can_change = len(list(filter(lambda xxx: len(xxx) != 0, swaps)))
            max_change_num = min(int(0.15 * len(words)), max_can_change)
            max_change_num = 1
            
            found = False
            for try_idx in range(self.num_tries):
                cur_words = list(words)
                for epoch in range(self.num_epochs):
                    word_idxs = list(range(len(choices)))
                    random.shuffle(word_idxs)
                    for i in word_idxs[:max_change_num]:
                        cur_raw = []
                        for w_new in choices[i]:
                            cur_raw.append(' '.join(cur_words[:i] + [w_new] + cur_words[i+1:]))
                        probs = query(cur_raw, model, dataset, device)
                        best_idx = torch.argmin(probs[:, y])
#                         import pdb; pdb.set_trace()
                        
#                         cur_dataset = TextClassificationDataset.from_raw_data(
#                             cur_raw, dataset.vocab)
#                         preds = model.query(cur_dataset, dataset.vocab, device)
#                         margins = [p * (2 * y - 1) for p in preds]
#                         best_idx = min(enumerate(margins),
#                                        key=lambda x: x[1])[0]
                        cur_words[i] = choices[i][best_idx]
#                         if margins[best_idx] < self.margin_goal:
                        if torch.argmax(probs[best_idx]).item() != y:
                            found = True
                            is_correct.append(0)
                            raw_correct.append(1)
                            adv_exs.append([' '.join(cur_words)])
#                             print('ADVERSARY SUCCESS on ("%s", %d): Found "%s" with margin %.2f' % (
#                                 x, y, adv_exs[-1], margins[best_idx]))
                            print('adversary success')
                            if cert_correct:
                                print('^^ CERT CORRECT THOUGH')
                            break
                    if found:
                        break
                if found:
                    break
            else:
                is_correct.append(1)
                raw_correct.append(1)
                adv_exs.append([])
                print('ADVERSARY FAILURE on ("%s", %d)' % (x, y))
            raw_cnt = np.array(raw_correct)
            cnt = np.array(is_correct)
            print(">>> Clean accuracy", round(100 * raw_cnt.sum() / raw_cnt.shape[0], 2))
            print(">>> Adv accuracy", round(100 * cnt.sum() / cnt.shape[0], 2))
                
            
        return is_correct, adv_exs
    

def query(sents, model, dataset, device):
    if hasattr(dataset, "vocab"):
        cur_dataset = TextClassificationDataset.from_raw_data(
                zip(sents, [0] * len(sents)), dataset.vocab)
    else:
        cur_dataset = TextClassificationDataset.from_raw_data(
                zip(sents, [0] * len(sents)), dataset)
    _logits = []
    with torch.no_grad():
        for data in cur_dataset.get_loader(63):
            batch = data_util.dict_batch_to_device(data, device)
            logits = model.forward(batch, compute_bounds=False)
            _logits.extend(logits.tolist())
    _logits = torch.tensor(_logits)
    if shared.opts.use_agnews_data:
        _probs = torch.nn.functional.softmax(_logits, dim=1)
    else:
        _probs = torch.sigmoid(_logits)
        _probs_0 = 1 - _probs
        _probs = torch.cat([_probs_0, _probs], dim=1)
    return _probs

class PWWSAdversary(Adversary):
    def __init__(self, attack_surface):
        super(PWWSAdversary, self).__init__(attack_surface)
    
    def run(self, model, dataset, device, opts=None):
        raw_correct = []
        is_correct = []
        adv_exs = []
        for x, y in tqdm(dataset.raw_data[1:]):
            if torch.argmax(query([x], model, dataset, device)[0]).item() != y:
                print('Skip sentence since it is wrong')
                raw_correct.append(0)
                is_correct.append(0)
                continue
            
            words = x.split()
            swaps = self.attack_surface.get_swaps([ele.lower() for ele in words])
            
#             import pdb; pdb.set_trace()
            
            _sents = []
            _offsets = {}
            for sid, word in enumerate(words):
                if len(swaps[sid]) == 0:
                    continue
                tmp_sents = []
                # first element is the raw sentence
                tmp_sents.append(" ".join(words))
                # second element is the UNK sentence
                tmp_words = copy.copy(words)
                tmp_words[sid] = '<UNK>'
                tmp_sents.append(" ".join(tmp_words))
                # starting from the third one are modified sentences
                for nbr in swaps[sid]:
                    tmp_words = copy.copy(words)
                    tmp_words[sid] = nbr
                    tmp_sents.append(" ".join(tmp_words))

                _offsets[sid] = (len(_sents), len(tmp_sents))
                _sents.extend(tmp_sents)
            
            if len(_sents) == 0:
                is_correct.append(1)
                raw_correct.append(1)
                continue 
                
            _probs = query(_sents, model, dataset, device)
                
            repl_dct = {}  # {idx: "the replaced word"}
            pwws_dct = {}
            for sid, word in enumerate(words):
                if len(swaps[sid]) == 0:
                    continue
                _start, _num = _offsets[sid]
                probs = np.array(_probs[_start:_start + _num])
                true_probs = probs[:, y]
                raw_prob = true_probs[0]
                oov_prob = true_probs[1]
                other_probs = true_probs[2:]
                repl_dct[sid] = swaps[sid][np.argmin(other_probs)]
                pwws_dct[sid] = np.max(raw_prob - other_probs) * np.exp(raw_prob - oov_prob)
    
            max_change_num = len(list(filter(lambda xxx: len(xxx) != 0, swaps)))
            
        
            sorted_pwws = sorted(pwws_dct.items(), key=lambda x: x[1], reverse=True)
            final_words = copy.copy(words)
            successful = False
            for i in range(max_change_num):
                sid = sorted_pwws[i][0]
                final_words[sid] = repl_dct[sid]
                pro = query([" ".join(final_words)], model, dataset, device)[0]
#                 print(f"replace {words[sid]} with {repl_dct[sid]}: {pro}")
#                 print(" ".join(final_words))
                if torch.argmax(pro).item() != y:
                    successful = True
                    break

            adv_exs.append(final_words)
            is_correct.append(0 if successful else 1)
            raw_correct.append(1)
                
            raw_cnt = np.array(raw_correct)
            cnt = np.array(is_correct)
            print(">>> Clean accuracy", round(100 * raw_cnt.sum() / raw_cnt.shape[0], 2))
            print(">>> Adv accuracy", round(100 * cnt.sum() / cnt.shape[0], 2))
        return is_correct, adv_exs


class GeneticAdversary(Adversary):
    """An adversary that runs a genetic attack."""

    def __init__(self, attack_surface, num_iters=20, pop_size=60, margin_goal=0.0):
        super(GeneticAdversary, self).__init__(attack_surface)
        self.num_iters = num_iters
        self.pop_size = pop_size
        self.margin_goal = margin_goal

    def perturb(self, words, choices, model, y, vocab, device):
        if all(len(c) == 1 for c in choices):
            return words, None
        good_idxs = [i for i, c in enumerate(choices) if len(c) > 1]
        idx = random.sample(good_idxs, 1)[0]
        x_list = [' '.join(words[:idx] + [w_new] + words[idx+1:])
                  for w_new in choices[idx]]
        if shared.opts.use_agnews_data:
            margins = []
            for x in x_list:
                model_output, gold_labels = model.query((x, y), vocab, device)
                margin, _ = get_margins(model_output, gold_labels)
                margins.append(margin.item())
        else:
            probs = query(x_list, model, vocab, device)
            preds = (- (1 / probs[:, 1] - 1).log()).tolist()
#             import pdb; pdb.set_trace()
#             preds = [model.query(x, vocab, device) for x in x_list]
            margins = [p * (2 * y - 1) for p in preds]
        best_idx = min(enumerate(margins), key=lambda x: x[1])[0]
        cur_words = list(words)
        cur_words[idx] = choices[idx][best_idx]
        return cur_words, margins[best_idx]

    def run(self, model, dataset, device, opts=None):
        raw_correct = []
        is_correct = []
        adv_exs = []
        for x, y in tqdm(dataset.raw_data):
            # First query the example itself
            if shared.opts.use_agnews_data:
                orig_pred, orig_gold = model.query((x, y), dataset.vocab, device, return_bounds=True, attack_surface=self.attack_surface)
                model_correct, model_cert_correct = compute_is_correct(orig_pred, orig_gold)
                cert_correct = model_cert_correct.sum().item()
                value_margins, worst_case_margins = get_margins(
                    orig_pred, orig_gold)
                print('Margin: %.6f, lower bound: %.6f, cert_correct=%s' %
                      (value_margins[0].item(), worst_case_margins[0].item(),
                       cert_correct))
                if model_correct.sum().item() <= 0:
                    print('ORIGINAL PREDICTION WAS WRONG')
                    raw_correct.append(0)
                    is_correct.append(0)
                    adv_exs.append(x)
                    continue
            else:
                orig_pred, (orig_lb, orig_ub) = model.query(
                    x, dataset.vocab, device, return_bounds=True,
                    attack_surface=self.attack_surface)
                cert_correct = (orig_lb * (2 * y - 1) >
                                0) and (orig_ub * (2 * y - 1) > 0)
                print('Logit bounds: %.6f <= %.6f <= %.6f, cert_correct=%s' % (
                    orig_lb, orig_pred, orig_ub, cert_correct))
                if orig_pred * (2 * y - 1) <= 0:
                    print('ORIGINAL PREDICTION WAS WRONG')
                    raw_correct.append(0)
                    is_correct.append(0)
                    adv_exs.append(x)
                    continue
            # Now run adversarial search
            words = x.split()
            swaps = self.attack_surface.get_swaps(words)
            choices = [[w] + cur_swaps for w, cur_swaps in zip(words, swaps)]
            found = False
            population = [self.perturb(words, choices, model, y, dataset.vocab, device)
                          for i in range(self.pop_size)]
            if population[0][1] is None:
                print('NO REPLACEMENT FOUND')
                raw_correct.append(1)
                is_correct.append(1)
                adv_exs.append(x)
                continue
            for g in range(self.num_iters):
                best_idx = min(enumerate(population), key=lambda x: x[1][1])[0]
                print('Iteration %d: %.6f' % (g, population[best_idx][1]))
                if population[best_idx][1] < self.margin_goal:
                    found = True
                    raw_correct.append(1)
                    is_correct.append(0)
                    adv_exs.append(' '.join(population[best_idx][0]))
                    print('ADVERSARY SUCCESS on ("%s", %d): Found "%s" with margin %.2f' % (
                        x, y, adv_exs[-1], population[best_idx][1]))
                    if cert_correct:
                        print('^^ CERT CORRECT THOUGH')
                    break
                new_population = [population[best_idx]]
                margins = np.array([m for c, m in population])
                adv_probs = 1 / (1 + np.exp(margins)) + 1e-6
                # Sigmoid of negative margin, for probabilty of wrong class
                # Add 1e-6 for numerical stability
                sample_probs = adv_probs / np.sum(adv_probs)
                for i in range(1, self.pop_size):
                    parent1 = population[np.random.choice(
                        range(len(population)), p=sample_probs)][0]
                    parent2 = population[np.random.choice(
                        range(len(population)), p=sample_probs)][0]
                    child = [random.sample([w1, w2], 1)[0]
                             for (w1, w2) in zip(parent1, parent2)]
                    child_mut, new_margin = self.perturb(child, choices, model, y,
                                                         dataset.vocab, device)
                    new_population.append((child_mut, new_margin))
                population = new_population
            else:
                raw_correct.append(1)
                is_correct.append(1)
                adv_exs.append([])
                print('ADVERSARY FAILURE on ("%s", %d)' % (x, y))
            raw_cnt = np.array(raw_correct)
            cnt = np.array(is_correct)
            print(">>> Clean accuracy", round(100 * raw_cnt.sum() / raw_cnt.shape[0], 2))
            print(">>> Adv accuracy", round(100 * cnt.sum() / cnt.shape[0], 2))
        return is_correct, adv_exs

    
def get_margins(model_output, gold_labels):
    if isinstance(model_output, ibp.IntervalBoundedTensor):
        logits = model_output.val
        w_true_class_pred = (model_output.lb * gold_labels).sum(dim=1)
        w_highest_false_pred = (model_output.ub +
                                (gold_labels * -1e20)).max(dim=1)[0]
        w_value_margin = w_true_class_pred - w_highest_false_pred
    else:
        logits = model_output
        w_value_margin = None
    true_class_pred = (logits * gold_labels).sum(dim=1)
    highest_false_pred = (logits + (gold_labels * -1e20)).max(dim=1)[0]
    value_margin = true_class_pred - highest_false_pred
    return value_margin, w_value_margin

def load_datasets(device, opts):
    """
    Loads text classification datasets given opts on the device and returns the dataset.
    If a data cache is specified in opts and the cached data there is of the same class
      as the one specified in opts, uses the cache. Otherwise reads from the raw dataset
      files specified in OPTS.
    Returns:
      - train_data:  EntailmentDataset - Processed training dataset
      - dev_data: Optional[EntailmentDataset] - Processed dev dataset if raw dev data was found or
          dev_frac was specified in opts
      - word_mat: torch.Tensor
      - attack_surface: AttackSurface - defines the adversarial attack surface
    """
    data_class = ToyClassificationDataset if opts.use_toy_data else IMDBDataset
    if opts.use_agnews_data:
        data_class = AGNEWSDataset
    try:
        with open(os.path.join(opts.data_cache_dir, 'train_data.pkl'), 'rb') as infile:
            train_data = pickle.load(infile)
            if not isinstance(train_data, data_class):
                raise Exception(
                    "Cached dataset of wrong class: {}".format(type(train_data)))
        with open(os.path.join(opts.data_cache_dir, 'dev_data.pkl'), 'rb') as infile:
            dev_data = pickle.load(infile)
            if not isinstance(dev_data, data_class):
                raise Exception(
                    "Cached dataset of wrong class: {}".format(type(train_data)))
        with open(os.path.join(opts.data_cache_dir, 'word_mat.pkl'), 'rb') as infile:
            word_mat = pickle.load(infile)
        with open(os.path.join(opts.data_cache_dir, 'attack_surface.pkl'), 'rb') as infile:
            attack_surface = pickle.load(infile)
        print("Loaded data from {}.".format(opts.data_cache_dir))
    except Exception:
        if opts.use_toy_data:
            attack_surface = ToyClassificationAttackSurface(
                ToyClassificationDataset.VOCAB_LIST)
        elif opts.use_lm:
            if shared.opts.use_agnews_data:
                attack_surface = attacks.LMConstrainedAttackSurface.from_files(
                    opts.neighbor_file, opts.agnews_lm_file)
            else:
                attack_surface = attacks.LMConstrainedAttackSurface.from_files(
                    opts.neighbor_file, opts.imdb_lm_file)
        else:
            attack_surface = attacks.WordSubstitutionAttackSurface.from_file(
                opts.neighbor_file)
        print('Reading dataset.')
        if opts.use_agnews_data:
            raw_data = data_class.get_raw_data(opts.agnews_dir, test=opts.test)
        else:
            raw_data = data_class.get_raw_data(opts.imdb_dir, test=opts.test)
        word_set = raw_data.get_word_set(attack_surface)
        vocab, word_mat = vocabulary.Vocabulary.read_word_vecs(
            word_set, opts.glove_dir, opts.glove, device)
        train_data = data_class.from_raw_data(raw_data.train_data, vocab, attack_surface,
                                              downsample_to=opts.downsample_to,
                                              downsample_shard=opts.downsample_shard,
                                              truncate_to=opts.truncate_to)
        dev_data = data_class.from_raw_data(raw_data.dev_data, vocab, attack_surface,
                                            downsample_to=opts.downsample_to,
                                            downsample_shard=opts.downsample_shard,
                                            truncate_to=opts.truncate_to)
        if opts.data_cache_dir:
            with open(os.path.join(opts.data_cache_dir, 'train_data.pkl'), 'wb') as outfile:
                pickle.dump(train_data, outfile)
            with open(os.path.join(opts.data_cache_dir, 'dev_data.pkl'), 'wb') as outfile:
                pickle.dump(dev_data, outfile)
            with open(os.path.join(opts.data_cache_dir, 'word_mat.pkl'), 'wb') as outfile:
                pickle.dump(word_mat, outfile)
            with open(os.path.join(opts.data_cache_dir, 'attack_surface.pkl'), 'wb') as outfile:
                pickle.dump(attack_surface, outfile)
    return train_data, dev_data, word_mat, attack_surface


def num_correct(model_output, gold_labels):
    """
    Given the output of model and gold labels returns number of correct and certified correct
    predictions
    Args:
      - model_output: output of the model, could be ibp.IntervalBoundedTensor or torch.Tensor
      - gold_labels: torch.Tensor, should be of size 1 per sample, 1 for positive 0 for negative
    Returns:
      - num_correct: int - number of correct predictions from the actual model output
      - num_cert_correct - number of bounds-certified correct predictions if the model_output was an
          IntervalBoundedTensor, 0 otherwise.
    """
    if shared.opts.use_agnews_data:
        return num_correct_multi(model_output, gold_labels)
    if isinstance(model_output, ibp.IntervalBoundedTensor):
        logits = model_output.val
        num_cert_correct = sum(
            all((b * (2 * y - 1)).item() >
                0 for b in (model_output.lb[i], model_output.ub[i]))
            for i, y in enumerate(gold_labels)
        )
    else:
        logits = model_output
        num_cert_correct = 0
    num_correct = sum(
        (logits[i] * (2 * y - 1)).item() > 0 for i, y in enumerate(gold_labels)
    )
    return num_correct, num_cert_correct

def compute_is_correct(model_output, gold_labels):
    if isinstance(model_output, ibp.IntervalBoundedTensor):
        logits = model_output.val
        # Worst case pred. is the LB of correct class
        # combined with the UBs of the other classes
        worst_case_pred = (gold_labels * model_output.lb + (1 - gold_labels) * model_output.ub).argmax(dim=1)
        gold_labels = gold_labels.argmax(dim=1)
        cert_correct = ((worst_case_pred - gold_labels) == 0)
    else:
        gold_labels = gold_labels.argmax(dim=1)
        logits = model_output
        cert_correct = None
    predictions = logits.argmax(dim=1)
    correct = ((predictions - gold_labels) == 0)
    return correct, cert_correct


def num_correct_multi(model_output, gold_labels):
    """
  Given the output of model and gold labels returns number of correct and certified correct
  predictions
  Args:
    - model_output: output of the model, could be ibp.IntervalBoundedTensor or torch.Tensor
    - gold_labels: torch.Tensor
  Returns:
    - num_correct: int - number of correct predictions from the actual model output
    - num_cert_correct - number of bounds-certified correct predictions if the model_output was an
        IntervalBoundedTensor, 0 otherwise.
    """
    is_correct, is_cert_correct = compute_is_correct(model_output, gold_labels)
    num_correct = is_correct.sum().item()
    if is_cert_correct is not None:
        num_cert_correct = is_cert_correct.sum().item()
    else:
        num_cert_correct = 0
    return num_correct, num_cert_correct


def load_model(word_mat, device, opts):
    """
    Try to load a model on the device given the word_mat and opts.
    Tries to load a model from the given or latest checkpoint if specified in the opts.
    Otherwise instantiates a new model on the device.
    """
    if opts.use_agnews_data:
        num_labels = 4
    else:
        num_labels = 1
    if opts.model == 'bow':
        model = BOWModel(
            vocabulary.GLOVE_CONFIGS[opts.glove]['size'], opts.hidden_size, word_mat,
            pool=opts.pool, dropout=opts.dropout_prob, no_wordvec_layer=opts.no_wordvec_layer, num_labels=num_labels).to(device)
    elif opts.model == 'cnn':
        model = CNNModel(
            vocabulary.GLOVE_CONFIGS[opts.glove]['size'], opts.hidden_size, opts.kernel_size,
            word_mat, pool=opts.pool, dropout=opts.dropout_prob, no_wordvec_layer=opts.no_wordvec_layer,
            early_ibp=opts.early_ibp, relu_wordvec=not opts.no_relu_wordvec, unfreeze_wordvec=opts.unfreeze_wordvec, num_labels=num_labels).to(device)
    elif opts.model == 'lstm':
        model = LSTMModel(
            vocabulary.GLOVE_CONFIGS[opts.glove]['size'], opts.hidden_size,
            word_mat, device, pool=opts.pool, dropout=opts.dropout_prob, no_wordvec_layer=opts.no_wordvec_layer, num_labels=num_labels).to(device)
    elif opts.model == 'lstm-final-state':
        model = LSTMFinalStateModel(
            vocabulary.GLOVE_CONFIGS[opts.glove]['size'], opts.hidden_size,
            word_mat, device, dropout=opts.dropout_prob, no_wordvec_layer=opts.no_wordvec_layer, num_labels=num_labels).to(device)
    if opts.load_dir:
        try:
            if opts.load_ckpt is None:
                load_fn = sorted(glob.glob(os.path.join(
                    opts.load_dir, 'model-checkpoint-[0-9]+.pth')))[-1]
            else:
                load_fn = os.path.join(
                    opts.load_dir, 'model-checkpoint-%d.pth' % opts.load_ckpt)
            print('Loading model from %s.' % load_fn)
            state_dict = dict(torch.load(load_fn))
            state_dict['embs.weight'] = model.embs.weight
            model.load_state_dict(state_dict)
            print('Finished loading model.')
        except Exception as ex:
            print("Couldn't load model, starting anew: {}".format(ex))
    return model


class RawClassificationDataset(data_util.RawDataset):
    """
    Dataset that only holds x,y as (str, str) tuples
    """

    def get_word_set(self, attack_surface):
        with open(COUNTER_FITTED_FILE) as f:
            counter_vocab = set([line.split(' ')[0] for line in f])
        word_set = set()
        for x, y in self.data:
            words = [w.lower() for w in x.split(' ')]
            for w in words:
                word_set.add(w)

        newly_add = 0
        for x, y in self.data:
            words = [w.lower() for w in x.split(' ')]
            try:
                swaps = attack_surface.get_swaps(words)
                for cur_swaps in swaps:
                    for w in cur_swaps:
                        if w not in word_set:
                            newly_add += 1
                        word_set.add(w)
            except KeyError:
                # For now, ignore things not in attack surface
                # If we really need them, later code will throw an error
                pass
        print(newly_add)
        return word_set & counter_vocab


class TextClassificationDataset(data_util.ProcessedDataset):
    """
    Dataset that holds processed example dicts
    """
    @classmethod
    def from_raw_data(cls, raw_data, vocab, attack_surface=None, truncate_to=None,
                      downsample_to=None, downsample_shard=0):
        if downsample_to:
            raw_data = raw_data[downsample_shard *
                                downsample_to:(downsample_shard+1) * downsample_to]
        examples = []
        for x, y in raw_data:
            all_words = [w.lower() for w in x.split()]
            if attack_surface:
                all_swaps = attack_surface.get_swaps(all_words)
                words = [w for w in all_words if w in vocab]
                swaps = [s for w, s in zip(all_words, all_swaps) if w in vocab]
                choices = [[w] + cur_swaps for w,
                           cur_swaps in zip(words, swaps)]
            else:
                # Delete UNK words
                words = [w for w in all_words if w in vocab]
            if truncate_to:
                words = words[:truncate_to]
            word_idxs = [vocab.get_index(w) for w in words]
            x_torch = torch.tensor(word_idxs).view(1, -1, 1)  # (1, T, d)
            if attack_surface:
                if shared.opts.use_agnews_data:
                    choices_word_idxs =  [
                        torch.tensor(list(filter(lambda x: x!=0, [vocab.get_index(c) for c in c_list])), dtype=torch.long) for c_list in choices
                    ]
                else:
                    choices_word_idxs = [
                        torch.tensor([vocab.get_index(c) for c in c_list], dtype=torch.long) for c_list in choices
                    ]
                if any(0 in c.view(-1).tolist() for c in choices_word_idxs):
                    raise ValueError("UNK tokens found")
                choices_torch = pad_sequence(choices_word_idxs, batch_first=True).unsqueeze(
                    2).unsqueeze(0)  # (1, T, C, 1)
                choices_mask = (choices_torch.squeeze(-1)
                                != 0).long()  # (1, T, C)
            else:
                choices_torch = x_torch.view(1, -1, 1, 1)  # (1, T, 1, 1)
                choices_mask = torch.ones_like(x_torch.view(1, -1, 1))
            mask_torch = torch.ones((1, len(word_idxs)))
            x_bounded = ibp.DiscreteChoiceTensor(
                x_torch, choices_torch, choices_mask, mask_torch)
            if shared.opts.use_agnews_data:
                y_torch = torch.zeros(1, 4)
                y_torch[0, y] = 1
            else:
                y_torch = torch.tensor(y, dtype=torch.float).view(1, 1)
            lengths_torch = torch.tensor(len(word_idxs)).view(1)
            examples.append(dict(x=x_bounded, y=y_torch,
                                 mask=mask_torch, lengths=lengths_torch))
        return cls(raw_data, vocab, examples)

    @staticmethod
    def example_len(example):
        return example['x'].shape[1]

    @staticmethod
    def collate_examples(examples):
        """
        Turns a list of examples into a workable batch:
        """
        if len(examples) == 1:
            return examples[0]
        B = len(examples)
        max_len = max(ex['x'].shape[1] for ex in examples)
        x_vals = []
        choice_mats = []
        choice_masks = []
        if shared.opts.use_agnews_data:
            y = torch.zeros((B, 4))
        else:
            y = torch.zeros((B, 1))
        lengths = torch.zeros((B, ), dtype=torch.long)
        masks = torch.zeros((B, max_len))
        for i, ex in enumerate(examples):
            x_vals.append(ex['x'].val)
            choice_mats.append(ex['x'].choice_mat)
            choice_masks.append(ex['x'].choice_mask)
            cur_len = ex['x'].shape[1]
            masks[i, :cur_len] = 1
            if shared.opts.use_agnews_data:
                y[i] = ex['y']
            else:
                y[i, 0] = ex['y']
            lengths[i] = ex['lengths'][0]
        x_vals = data_util.multi_dim_padded_cat(x_vals, 0).long()
        choice_mats = data_util.multi_dim_padded_cat(choice_mats, 0).long()
        choice_masks = data_util.multi_dim_padded_cat(choice_masks, 0).long()
        return {'x': ibp.DiscreteChoiceTensor(x_vals, choice_mats, choice_masks, masks),
                'y': y, 'mask': masks, 'lengths': lengths}


class ToyClassificationDataset(TextClassificationDataset):
    """
    Dataset that holds a toy sentiment classification data
    """
    VOCAB_LIST = [
        'cat', 'dog', 'fish', 'tiger', 'chicken',
        'hamster', 'bear', 'lion', 'dragon', 'horse',
        'monkey', 'goat', 'sheep', 'goose', 'duck']

    @classmethod
    def get_raw_data(cls, ignore_dir, data_size=5000, max_len=10, *args, **kwargs):
        data = []
        from tqdm import tqdm
        for t in tqdm(range(data_size)):
            seq_len = random.randint(3, max_len)
            words = [random.sample(cls.VOCAB_LIST, 1)[0]
                     for i in range(seq_len - 1)]
            if random.random() > 0.5:
                words.append(words[0])
                y = 1
            else:
                other_words = list(cls.VOCAB_LIST)
                other_words.remove(words[0])
                words.append(random.sample(other_words, 1)[0])
                y = 0
            data.append((' '.join(words), y))
        num_train = int(round(data_size * 0.8))
        train_data = data[:num_train]
        dev_data = data[num_train:]
        print(dev_data[:10])
        return RawClassificationDataset(train_data, dev_data)


class ToyClassificationAttackSurface(attacks.AttackSurface):
    """Attack surface for ToyClassificationDataset."""

    def __init__(self, vocab_list):
        self.vocab_list = vocab_list

    def get_swaps(self, words):
        swaps = []
        s = ' '.join(words)
        for i in range(len(words)):
            if i == 0 or i == len(words) - 1:
                swaps.append([])
            else:
                swaps.append(self.vocab_list)
        return swaps


class AGNEWSDataset(TextClassificationDataset):
    num_labels = 4
    """
    Dataset that holds the IMDB sentiment classification data
    """
    @classmethod
    def read_text(cls, agnews_dir, split):
        data = []
        with open(f"{agnews_dir}/{split}.tsv", "r") as data_file:
            # without the quoting arg, errors will occur with line having quoting characters "/'
            df = pandas.read_csv(data_file, sep='\t', quoting=csv.QUOTE_NONE)
            for rid in range(0, df.shape[0]):
                sent = df.iloc[rid]['sentence']
#                 sent = sent.lower()
                label = df.iloc[rid]['label']
                data.append((sent, label))
        return data

    @classmethod
    def get_raw_data(cls, agnews_dir, test=False):
        train_data = cls.read_text(agnews_dir, 'train')
        if test:
            dev_data = cls.read_text(agnews_dir, 'test')
        else:
            dev_data = cls.read_text(agnews_dir, 'dev')
        return RawClassificationDataset(train_data, dev_data)


class IMDBDataset(TextClassificationDataset):
    num_labels = 2
    """
    Dataset that holds the IMDB sentiment classification data
    """
    @classmethod
    def read_text(cls, imdb_dir, split):
        if split == 'test':
            subdir = 'test'
        else:
            subdir = 'train'
        with open(os.path.join(imdb_dir, subdir, 'imdb_%s_files.txt' % split)) as f:
            filenames = [line.strip() for line in f]
        data = []
        num_words = 0
        for fn in tqdm(filenames):
            label = 1 if fn.startswith('pos') else 0
            with open(os.path.join(imdb_dir, subdir, fn)) as f:
                x_raw = f.readlines()[0].strip().replace('<br />', ' ')
                x_toks = word_tokenize(x_raw)
                num_words += len(x_toks)
                data.append((' '.join(x_toks), label))
        num_pos = sum(y for x, y in data)
        num_neg = sum(1 - y for x, y in data)
        avg_words = num_words / len(data)
        print('Read %d examples (+%d, -%d), average length %d words' % (
            len(data), num_pos, num_neg, avg_words))
        return data

    @classmethod
    def get_raw_data(cls, imdb_dir, test=False):
        train_data = cls.read_text(imdb_dir, 'train')
        if test:
            dev_data = cls.read_text(imdb_dir, 'test')
        else:
            dev_data = cls.read_text(imdb_dir, 'dev')
        return RawClassificationDataset(train_data, dev_data)


class DataAugmenter(data_util.DataAugmenter):
    def augment(self, dataset):
        new_examples = []
        for ex in tqdm(dataset.examples):
            new_examples.append(ex)
            x_orig = ex['x']  # (1, T, 1)
            choices = []
            for i in range(x_orig.shape[1]):
                cur_choices = torch.masked_select(
                    x_orig.choice_mat[0, i, :, 0], x_orig.choice_mask[0, i, :].type(torch.uint8))
                choices.append(cur_choices)
            for t in range(self.augment_by):
                x_new = torch.stack([choices[i][random.choice(range(len(choices[i])))]
                                     for i in range(len(choices))]).view(1, -1, 1)
                x_bounded = ibp.DiscreteChoiceTensor(
                    x_new, x_orig.choice_mat, x_orig.choice_mask, x_orig.sequence_mask)
                ex_new = dict(ex)
                ex_new['x'] = x_bounded
                new_examples.append(ex_new)
        return TextClassificationDataset(None, dataset.vocab, new_examples)
