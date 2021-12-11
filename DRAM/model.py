import torch.nn as nn
import torch

import modules


class RecurrentAttention(nn.Module):
    """A Recurrent Model of Visual Attention (RAM) [1]

    References:
      [1]: Minh et. al., https://arxiv.org/abs/1406.6247
    """

    def __init__(
        self, g, k, s, c, h_g, h_l, std, hidden_size, num_classes,corenet_type
    ):
        """
        """
        super().__init__()

        self.std = std

        self.sensor = modules.GlimpseNetwork(h_g, h_l, g, k, s, c)
        if corenet_type=="Linear":
            self.rnn = modules.CoreNetwork(hidden_size, hidden_size)
        elif corenet_type=="LSTM":
            self.rnn = modules.CoreNetworkLSTM(hidden_size, hidden_size)
        self.locator = modules.LocationNetwork(hidden_size, 2, std)
        self.classifier1 = modules.ActionNetwork(hidden_size, num_classes)
        self.classifier2 = modules.ActionNetwork(hidden_size, num_classes)
        self.classifier3 = modules.ActionNetwork(hidden_size, num_classes)
        self.critic = modules.Critic(hidden_size, 1)

    def forward(self, x, l_t_prev, h_t_prev, last=False):
        """Run RAM for one timestep on a minibatch of images.
        """
        g_t = self.sensor(x, l_t_prev)
        h_t = self.rnn(g_t, h_t_prev)

        log_pi, l_t = self.locator(h_t[1])
        b_t = self.critic(h_t[1]).squeeze()


        log_probas_1 = self.classifier1(h_t[0])
        log_probas_2 = self.classifier2(h_t[0])
        log_probas_3 = self.classifier3(h_t[0])
        log_probas = torch.stack((log_probas_1,log_probas_2,log_probas_3))
        return h_t, l_t, b_t,log_pi, log_probas
