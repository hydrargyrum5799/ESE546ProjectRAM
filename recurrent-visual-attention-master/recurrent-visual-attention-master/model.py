import torch.nn as nn

import modules


class RecurrentAttention(nn.Module):
    """A Recurrent Model of Visual Attention (RAM) [1]

    References:
      [1]: Minh et. al., https://arxiv.org/abs/1406.6247
    """

    def __init__(
        self, g, k, s, c, h_g, h_l, std, hidden_size, num_classes,corenet_type,pVAE=False
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
        self.classifier = modules.ActionNetwork(hidden_size, num_classes)
        self.critic = modules.Critic(hidden_size, 1)
        #Decoder to understadnreconstruction to original image
        if pVAE:
            self.decoder = modules.PartialDecoder(hidden_size, 16, 784)
        else:
            self.decoder = modules.Decoder(hidden_size, 16, 784)

    def forward(self, x, l_t_prev, h_t_prev, last=False,pVAE= False,masks=None):
        """Run RAM for one timestep on a minibatch of images.
        """
        g_t,masks = self.sensor(x, l_t_prev,masks)
        h_t = self.rnn(g_t, h_t_prev)

        log_pi, l_t = self.locator(h_t)
        b_t = self.critic(h_t).squeeze()


        log_probas = self.classifier(h_t)

        if pVAE:
            mu,logvar,decoded_output = self.decoder(h_t,masks)
        else:
            mu, logvar, decoded_output = self.decoder(h_t)

        return h_t, l_t, b_t,log_pi, log_probas,decoded_output,mu,logvar,masks
