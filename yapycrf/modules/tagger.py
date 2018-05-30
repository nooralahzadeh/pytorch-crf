"""Defines a Bi-LSMT CRF model."""

from typing import List

import torch
import torch.nn as nn

from allennlp.modules.conditional_random_field import ConditionalRandomField
from yapycrf.io import Vocab
from .utils import sequence_mask
from .char_lstm import CharLSTM


class Tagger(nn.Module):
    """
    Bi-LSTM CRF model.

    Parameters
    ----------
    vocab : :obj:`yapycrf.io.Vocab`
        The vocab object which contains a dict of known characters and word
        embeddings.

    char_lstm : :obj:`yapycrf.model.CharLSTM`
        The character-level LSTM layer.

    crf : :obj:`yapycrf.model.crf`
        The CRF model.

    hidden_dim : int
        The hidden dimension of the recurrent layer.

    layers : int
        The number of layers of cells in the recurrent layer.

    dropout : float
        The dropout probability for the recurrent layer.

    bidirectional : bool
        If True, bidirectional recurrent layer is used, otherwise single
        direction.

    Attributes
    ----------
    vocab : :obj:`yapycrf.io.Vocab`
        The vocab object which contains a dict of known characters and word
        embeddings.

    char_lstm : :obj:`yapycrf.model.CharLSTM`
        The character-level LSTM layer.

    crf : :obj:`yapycrf.model.crf`
        The CRF model.

    rnn_output_size : int
        The output dimension of the recurrent layer.

    rnn : :obj:`nn.Module`
        The recurrent layer of the network.

    rnn_to_crf : :obj:`nn.Module`
        The linear layer that maps the hidden states from the recurrent layer
        to the label space.

    """

    def __init__(self,
                 vocab: Vocab,
                 char_lstm: CharLSTM,
                 crf: ConditionalRandomField,
                 hidden_dim: int = 100,
                 layers: int = 1,
                 dropout: float = 0.,
                 bidirectional: bool = True) -> None:
        super(Tagger, self).__init__()

        assert vocab.n_chars == char_lstm.n_chars
        assert vocab.n_labels == crf.num_tags

        self.vocab = vocab
        self.char_lstm = char_lstm
        self.crf = crf

        # Recurrent layer. Takes as input the concatenation of the char_lstm
        # final hidden state and pre-trained embedding for each word.
        # The dimension of the output is given by self.rnn_output_size (see
        # below).
        self.rnn = nn.LSTM(
            input_size=vocab.word_vec_dim + char_lstm.output_size,
            hidden_size=hidden_dim,
            num_layers=layers,
            bidirectional=bidirectional,
            dropout=dropout,
            batch_first=True,
        )

        # This is the size of the recurrent layer's output (see self.rnn).
        self.rnn_output_size = hidden_dim
        if bidirectional:
            self.rnn_output_size *= 2

        # Linear layer that takes the output from the recurrent layer and each
        # time step and transforms into scores for each label.
        self.rnn_to_crf = nn.Linear(self.rnn_output_size, self.vocab.n_labels)

    def _feats(self,
               chars: List[torch.Tensor],
               words: torch.Tensor) -> torch.Tensor:
        """
        Generate features for the CRF from input.

        First we generate a vector for each word by running each word
        char-by-char through the char_lstm and then concatenating those final
        hidden states for each word with the pre-trained embedding of the word.
        That word vector is than ran through the RNN and then the linear layer.

        Parameters
        ----------
        chars : list of :obj:`torch.Tensor`
            List of tensors with shape `[word_lenth x n_chars]`.

        words : :obj:`Tensor`
            Pretrained word embeddings with shape
            `[sent_length x word_emb_dim]`.

        Returns
        -------
        :obj:`Tensor`
            `[batch_size x sent_length x crf.n_labels]`

        """
        # Run each word character-by-character through the CharLSTM to generate
        # character-level word features.
        # char_feats: `[sent_length x char_lstm.output_size]`
        char_feats = self.char_lstm(chars)

        # Concatenate the character-level word features and word embeddings.
        # word_feats: `[sent_length x
        #               (char_lstm.output_size + vocab.word_vec_dim)]`
        word_feats = torch.cat([char_feats, words], dim=-1)

        # Add a fake batch dimension.
        # word_feats: `[1 x sent_length x
        #               (char_lstm.output_size + vocab.word_vec_dim)]`
        word_feats = word_feats.unsqueeze(0)

        # Run word features through the LSTM.
        # lstm_feats: `[1 x sent_length x rnn_output_size]`
        lstm_feats, _ = self.rnn(word_feats)

        # Run recurrent output through linear layer to generate the by-label
        # features.
        # feats: `[1 x sent_length x crf.n_labels]`
        feats = self.rnn_to_crf(lstm_feats)

        return feats

    def _score(self,
               feats: torch.Tensor,
               labs: torch.Tensor,
               lens: torch.Tensor) -> torch.Tensor:
        # Gather the score for each actual label.
        # scores: `[batch_size x sent_length]`
        scores = torch.gather(feats, 2, labs.unsqueeze(-1)).squeeze(-1)

        # Apply mask.
        mask = sequence_mask(lens).float()
        scores = scores * mask

        # Take sum over each sent.
        # score: `[batch_size]`
        score = scores.sum(1).squeeze(-1)

        # Now add the transition score.
        score = score + self.crf.transition_score(labs, lens)

        return score

    def predict(self,
                chars: List[torch.Tensor],
                words: torch.Tensor,
                lens: torch.Tensor = None) -> List[List[int]]:
        """
        Outputs the best tag sequence.

        Parameters
        ----------
        chars : list of :obj:`Tensor`
            List of tensors with shape `[word_lenth x n_chars]`.

        words : :obj:`Tensor`
            Pretrained word embeddings with shape
            `[sent_length x word_emb_dim]`.

        Returns
        -------
        List[List[int]]
            The best path for each sentence in the batch.

        """
        # pylint: disable=not-callable
        if lens is None:
            lens = torch.tensor([words.size(0)])
        mask = sequence_mask(lens)

        # Gather word feats.
        # feats: `[1 x sent_length x n_labels]`
        feats = self._feats(chars, words)

        # Run features through Viterbi decode algorithm.
        preds = self.crf.viterbi_tags(feats, mask)

        return preds

    def forward(self,
                chars: List[torch.Tensor],
                words: torch.Tensor,
                labs: torch.Tensor,
                lens: torch.Tensor = None) -> torch.Tensor:
        """
        Computes the negative of the log-likelihood.

        Parameters
        ----------
        chars : list of :obj:`Tensor`
            List of tensors with shape `[word_lenth x n_chars]`.

        words : :obj:`Tensor`
            Pretrained word embeddings with shape
            `[sent_length x word_emb_dim]`.

        labs : :obj:`Tensor`
            Corresponding target label sequence with shape `[sent_length]`.

        Returns
        -------
        :obj:`torch.Tensor`
            The negative log-likelihood evaluated at the inputs.

        """
        # pylint: disable=arguments-differ,not-callable
        if lens is None:
            lens = torch.tensor([words.size(0)])
        mask = sequence_mask(lens)

        # Fake batch dimension for labs.
        # labs: `[1 x sent_length]`
        labs = labs.unsqueeze(0)

        # Gather word feats.
        # feats: `[1 x sent_length x n_labels]`
        feats = self._feats(chars, words)

        loglik = self.crf(feats, labs, mask=mask)

        return -1. * loglik