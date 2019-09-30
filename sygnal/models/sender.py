import torch
import torch.nn as nn
from torch.distributions.categorical import Categorical
from torch.nn import functional as F


from .vector_quantization import (
    to_one_hot,
    VectorQuantization,
    EmbeddingtableDistances,
    HardMax,
)

from ..helpers.utils_helper import UtilsHelper


class Sender(nn.Module):
    def __init__(
        self,
        vocab_size,  # Specifies number of words in baseline setting. In VQ-VAE Setting:
        # Dimension of embedding space.
        output_len,  # called max_length in other files
        sos_id,
        device,
        eos_id=None,
        input_size=64,
        embedding_size=256,
        hidden_size=512,
        greedy=False,
        cell_type="lstm",
        reset_params=True,
        tau=1.2,
        vqvae=False,  # If True, use VQ instead of Gumbel Softmax
        discrete_latent_number=25,  # Number of embedding vectors e_i in embedding table in vqvae setting
        discrete_latent_dimension=25,  # dimension of embedding vectors
        beta=0.25,  # Weighting of loss terms 2 and 3 in VQ-VAE
        discrete_communication=False,
        gumbel_softmax=False,
        rl=False,
    ):
        super().__init__()
        if vqvae and not rl and not discrete_communication:
            assert vocab_size == discrete_latent_dimension, (
                "When using continuous communication, "
                "vocab_size = discrete_latent_dimension"
            )
        else:
            assert vocab_size == discrete_latent_number, (
                "When using discrete communication, "
                "vocab_size = discrete_latent_number"
            )
        self.vocab_size = vocab_size
        self.cell_type = cell_type
        self.output_len = output_len
        self.sos_id = sos_id
        self.utils_helper = UtilsHelper()
        self.device = device

        if eos_id is None:
            self.eos_id = sos_id
        else:
            self.eos_id = eos_id

        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.input_size = input_size
        self.greedy = greedy

        if cell_type == "lstm":
            self.rnn = nn.LSTMCell(embedding_size, hidden_size)
        else:
            raise ValueError(
                "Sender case with cell_type '{}' is undefined".format(cell_type)
            )

        self.embedding = nn.Parameter(
            torch.empty((vocab_size, embedding_size), dtype=torch.float32)
        )

        if not vqvae:
            self.linear_out = nn.Linear(
                hidden_size, vocab_size
            )  # from a hidden state to the vocab
        else:
            self.linear_out = nn.Linear(hidden_size, discrete_latent_dimension)
        self.tau = tau
        self.vqvae = vqvae
        self.discrete_latent_number = discrete_latent_number
        self.discrete_latent_dimension = discrete_latent_dimension
        self.discrete_communication = discrete_communication
        self.beta = beta
        self.gumbel_softmax = gumbel_softmax

        self.input_module = nn.Identity()
        if self.input_size != self.hidden_size:
            self.input_module = nn.Linear(input_size, hidden_size)

        if self.vqvae:
            self.e = nn.Parameter(
                torch.empty(
                    (self.discrete_latent_number, self.discrete_latent_dimension),
                    dtype=torch.float32,
                )
            )  # The discrete embedding table
            print("the shape of e is {}".format(self.e.shape))

        self.rl = rl

        if reset_params:
            self.reset_parameters()

    def reset_parameters(self):
        nn.init.normal_(self.embedding, 0.0, 0.1)
        if not self.vqvae and not self.rl:
            nn.init.constant_(self.linear_out.weight, 0)
            nn.init.constant_(self.linear_out.bias, 0)
        if self.vqvae:
            nn.init.normal_(self.e, 0.0, 0.1)

        if type(self.rnn) is nn.LSTMCell:
            nn.init.xavier_uniform_(self.rnn.weight_ih)
            nn.init.orthogonal_(self.rnn.weight_hh)
            nn.init.constant_(self.rnn.bias_ih, val=0)
            # # cuDNN bias order: https://docs.nvidia.com/deeplearning/sdk/cudnn-developer-guide/index.html#cudnnRNNMode_t
            # # add some positive bias for the forget gates [b_i, b_f, b_o, b_g] = [0, 1, 0, 0]
            nn.init.constant_(self.rnn.bias_hh, val=0)
            nn.init.constant_(
                self.rnn.bias_hh[self.hidden_size : 2 * self.hidden_size], val=1
            )

    def _init_state(self, hidden_state, rnn_type):
        """
            Handles the initialization of the first hidden state of the decoder.
            Hidden state + cell state in the case of an LSTM cell or
            only hidden state in the case of a GRU cell.
            Args:
                hidden_state (torch.tensor): The state to initialize the decoding with.
                rnn_type (type): Type of the rnn cell.
            Returns:
                state: (h, c) if LSTM cell, h if GRU cell
                batch_size: Based on the given hidden_state if not None, 1 otherwise
        """

        # h0
        if hidden_state is None:
            batch_size = 1
            h = torch.zeros([batch_size, self.hidden_size], device=self.device)
        else:
            batch_size = hidden_state.shape[0]
            h = hidden_state  # batch_size, hidden_size

        # c0
        if rnn_type is nn.LSTMCell:
            c = torch.zeros([batch_size, self.hidden_size], device=self.device)
            state = (h, c)
        else:
            state = h

        return state, batch_size

    def _calculate_seq_len(self, seq_lengths, token, initial_length, seq_pos):
        """
            Calculates the lengths of each sequence in the batch in-place.
            The length goes from the start of the sequece up until the eos_id is predicted.
            If it is not predicted, then the length is output_len + n_sos_symbols.
            Args:
                seq_lengths (torch.tensor): To keep track of the sequence lengths.
                token (torch.tensor): Batch of predicted tokens at this timestep.
                initial_length (int): The max possible sequence length (output_len + n_sos_symbols).
                seq_pos (int): The current timestep.
        """
        max_predicted, vocab_index = torch.max(token, dim=1)
        mask = (vocab_index == self.eos_id) * (
            max_predicted == 1.0
        )  # all words in batch that are "already done"
        mask = mask.to(self.device)
        mask *= seq_lengths == initial_length
        seq_lengths[mask.nonzero()] = (
            seq_pos + 1
        )  # start always token appended. This tells the sequence
        # to be smaller at the positions where the sentence already ended.

    def calculate_token_gumbel_softmax(self, p, tau, sentence_probability, batch_size):
        if self.training:
            token = self.utils_helper.calculate_gumbel_softmax(p, tau, hard=True)
        else:
            sentence_probability += p.detach()

            if self.greedy:
                _, token = torch.max(p, -1)
            else:
                token = Categorical(p).sample()
            token = to_one_hot(token, n_dims=self.vocab_size)

            if batch_size == 1:
                token = token.unsqueeze(0)
        return token, sentence_probability

    def forward(self, hidden_state=None):
        """
        Performs a forward pass. If training, use Gumbel Softmax (hard) for sampling, else use
        discrete sampling.
        Hidden state here represents the encoded image/metadata - initializes the RNN from it.
        """

        hidden_state = self.input_module(hidden_state)
        state, batch_size = self._init_state(hidden_state, type(self.rnn))

        # Init output
        if not (self.vqvae and not self.discrete_communication and not self.rl):
            output = [
                torch.zeros(
                    (batch_size, self.vocab_size),
                    dtype=torch.float32,
                    device=self.device,
                )
            ]
            output[0][:, self.sos_id] = 1.0
        else:
            # In vqvae case with continuous communication, there is no sos symbol, since all words come from the unordered embedding table.
            # It is not possible to index code words by sos or eos symbols, since the number of codewords
            # is not necessarily the vocab size!
            output = [
                torch.zeros(
                    (batch_size, self.vocab_size),
                    dtype=torch.float32,
                    device=self.device,
                )
            ]

        # Keep track of sequence lengths
        initial_length = self.output_len + 1  # add the sos token
        seq_lengths = (
            torch.ones([batch_size], dtype=torch.int64, device=self.device)
            * initial_length
        )  # [initial_length, initial_length, ..., initial_length]. This gets reduced whenever it ends somewhere.

        embeds = []  # keep track of the embedded sequence
        sentence_probability = torch.zeros(
            (batch_size, self.vocab_size), device=self.device
        )
        losses_2_3 = torch.empty(self.output_len, device=self.device)
        entropy = torch.empty((batch_size, self.output_len), device=self.device)
        message_logits = torch.empty((batch_size, self.output_len), device=self.device)

        if self.vqvae:
            distance_computer = EmbeddingtableDistances(self.e)
            vq = VectorQuantization()
            hard_max = HardMax()

        for i in range(self.output_len):

            emb = torch.matmul(output[-1], self.embedding)

            embeds.append(emb)

            state = self.rnn(emb, state)

            if type(self.rnn) is nn.LSTMCell:
                h, _ = state
            else:
                h = state

            indices = [None] * batch_size

            if not self.rl:
                if not self.vqvae:
                    # That's the original baseline setting
                    p = F.softmax(self.linear_out(h), dim=1)
                    token, sentence_probability = self.calculate_token_gumbel_softmax(
                        p, self.tau, sentence_probability, batch_size
                    )
                else:
                    pre_quant = self.linear_out(h)

                    if not self.discrete_communication:
                        token = vq.apply(pre_quant, self.e, indices)
                    else:
                        distances = distance_computer(pre_quant)
                        softmin = F.softmax(-distances, dim=1)
                        if not self.gumbel_softmax:
                            token = hard_max.apply(
                                softmin, indices, self.discrete_latent_number
                            )  # This also updates the indices
                        else:
                            token, _ = self.calculate_token_gumbel_softmax(
                                softmin, self.tau, 0, batch_size
                            )
                            _, indices[:] = torch.max(token, dim=1)

            else:
                if not self.vqvae:
                    all_logits = F.log_softmax(self.linear_out(h) / self.tau, dim=1)
                else:
                    pre_quant = self.linear_out(h)
                    distances = distance_computer(pre_quant)
                    all_logits = F.log_softmax(-distances / self.tau, dim=1)

                distr = Categorical(logits=all_logits)
                entropy[:, i] = distr.entropy()

                if self.training:
                    token_index = distr.sample()
                    token = to_one_hot(token_index, n_dims=self.vocab_size)
                else:
                    token_index = all_logits.argmax(dim=1)
                    token = to_one_hot(token_index, n_dims=self.vocab_size)
                _, indices[:] = torch.max(token, dim=1)
                message_logits[:, i] = distr.log_prob(token_index)

            if not (self.vqvae and not self.discrete_communication and not self.rl):
                # Whenever we have a meaningful eos symbol, we prune the messages in the end
                self._calculate_seq_len(
                    seq_lengths, token, initial_length, seq_pos=i + 1
                )

            if self.vqvae:
                loss_2 = torch.mean(
                    torch.norm(pre_quant.detach() - self.e[indices], dim=1) ** 2
                )
                loss_3 = torch.mean(
                    torch.norm(pre_quant - self.e[indices].detach(), dim=1) ** 2
                )
                loss_2_3 = (
                    loss_2 + self.beta * loss_3
                )  # This corresponds to the second and third loss term in VQ-VAE
                losses_2_3[i] = loss_2_3

            token = token.to(self.device)
            output.append(token)

        messages = torch.stack(output, dim=1)
        loss_2_3_out = torch.mean(losses_2_3)

        return (
            messages,
            seq_lengths,
            entropy,
            torch.stack(embeds, dim=1),
            sentence_probability,
            loss_2_3_out,
            message_logits,
        )
