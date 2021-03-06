import torch
from torch._C import device
import torch.nn as nn
import torch.nn.functional as F

from torch.distributions import Normal


class PatchExtractor:
    """A visual retina.

    Extracts a k scaled glimpses `phi` around location `l`
    from an image `x`.

    """


    def __init__(self, g, k, s):
        self.g = g
        self.k = k
        self.s = s
        if torch.cuda.is_available():
            self.device = "cuda"

        else:
            self.device = "cpu"
    def extract_scaledpatches(self, x, l,masks=None):

        phi = []
        size = self.g
        mask = []
        # extract k patches of increasing size
        for i in range(self.k):
            phi_,mask_ = self.extract_patch(x, l, size)
            phi.append(phi_)
            mask.append(mask_)
            size = int(self.s * size)

        # resize the patches to squares of size g
        for i in range(1, len(phi)):
            k = phi[i].shape[-1] // self.g
            phi[i] = F.avg_pool2d(phi[i], k)

        # concatenate into a single tensor and flatten
        phi = torch.cat(phi, 1)
        mask = torch.cat(mask,1)
        mask = mask.sum(dim=1).clamp(0,1)
        phi = phi.view(phi.shape[0], -1)
        mask = mask.view(mask.shape[0],-1)
        if masks is not None:
            masks+=mask
            masks = masks.clamp(0, 1)
            return phi,masks
        return phi,mask

    def extract_patch(self, x, l, size):
        """Extract a single patch for each image in `x`.
        """
        B, C, H, W = x.shape

        start = (0.5 * ((l + 1.0) * H)).long()
        end = start + size

        # pad with zeros
        x = F.pad(x, (size // 2, size // 2, size // 2, size // 2))

        # loop through mini-batch and extract patches
        patch = []

        masks  = torch.zeros(x.shape)
        for i in range(B):
            patch.append(x[i, :, start[i, 1] : end[i, 1], start[i, 0] : end[i, 0]])
            masks[i, :, start[i, 1] : end[i, 1], start[i, 0] : end[i, 0]]+=1
        return torch.stack(patch),masks[:,:,size//2:size//2+H,size//2:size//2+W]

    def denormalize(self, T, coords):
        """Convert coordinates in the range [-1, 1] to
        coordinates in the range [0, T] where `T` is
        the size of the image.
        """
        return (0.5 * ((coords + 1.0) * T)).long()

    def exceeds(self, from_x, to_x, from_y, to_y, T):
        """Check whether the extracted patch will exceed
        the boundaries of the image of size `T`.
        """
        if (from_x < 0) or (from_y < 0) or (to_x > T) or (to_y > T):
            return True
        return False


class GlimpseNetwork(nn.Module):
    """The glimpse network.

    """

    def __init__(self, h_g, h_l, g, k, s, c):
        super().__init__()

        self.retina = PatchExtractor(g, k, s)

        # glimpse layer
        D_in = k * g * g * c
        self.fc1 = nn.Linear(D_in, h_g)

        # location layer
        D_in = 2
        self.fc2 = nn.Linear(D_in, h_l)

        self.fc3 = nn.Linear(h_g, h_g + h_l)
        self.fc4 = nn.Linear(h_l, h_g + h_l)

    def forward(self, x, l_t_prev,masks = None):
        # generate glimpse phi from image x
        phi,masks = self.retina.extract_scaledpatches(x, l_t_prev,masks)

        # flatten location vector
        l_t_prev = l_t_prev.view(l_t_prev.size(0), -1)

        # feed phi and l to respective fc layers
        phi_out = F.relu(self.fc1(phi))
        l_out = F.relu(self.fc2(l_t_prev))

        what = self.fc3(phi_out)
        where = self.fc4(l_out)

        # feed to fc layer
        g_t = F.relu(what + where)

        return g_t,masks

class CoreNetworkLSTM(nn.Module):
    """The core network in LSTM.

     """

    def __init__(self, input_size, hidden_size):
        super().__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size
        self.i2h = nn.Linear(input_size,hidden_size)
        self.LSTM = torch.nn.LSTM(self.hidden_size,self.hidden_size, batch_first = False)

    def forward(self, g_t, h_t_prev,c_t_prev,):
        h1 = self.i2h(g_t).unsqueeze(dim =0)
        _,(h_t,c_t) = self.LSTM(h1,(h_t_prev,c_t_prev))
        return h_t,c_t


class CoreNetwork(nn.Module):
    """The core network.
    RNN layer that process glimpse input and hidden state at prior step
    """

    def __init__(self, input_size, hidden_size):
        super().__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size

        self.i2h = nn.Linear(input_size, hidden_size)
        self.h2h = nn.Linear(hidden_size, hidden_size)

    def forward(self, g_t, h_t_prev):
        h1 = self.i2h(g_t)
        h2 = self.h2h(h_t_prev)
        h_t = F.relu(h1 + h2)
        return h_t


class ActionNetwork(nn.Module):
    """The action network.
    Prints the claass output
    """

    def __init__(self, input_size, output_size):
        super().__init__()
        self.fc = nn.Linear(input_size, output_size)

    def forward(self, h_t):
        a_t = F.log_softmax(self.fc(h_t), dim=1)
        return a_t


class LocationNetwork(nn.Module):
    """The location network.
    Outputs a policy over where to see next
    """

    def __init__(self, input_size, output_size, std):
        super().__init__()

        self.std = std

        hid_size = input_size // 2
        self.fc = nn.Linear(input_size, hid_size)
        self.fc_lt = nn.Linear(hid_size, output_size)

    def forward(self, h_t):
        # compute mean
        feat = F.relu(self.fc(h_t.detach()))
        mu = torch.tanh(self.fc_lt(feat))

        # reparametrization trick
        l_t = torch.distributions.Normal(mu, self.std).rsample()
        l_t = l_t.detach()
        log_pi = Normal(mu, self.std).log_prob(l_t)

        # we assume both dimensions are independent
        # 1. pdf of the joint is the product of the pdfs
        # 2. log of the product is the sum of the logs
        log_pi = torch.sum(log_pi, dim=1)

        # bound between [-1, 1]
        l_t = torch.clamp(l_t, -1, 1)

        return log_pi, l_t


class Critic(nn.Module):
    """The baseline network.
    The critic of the network
    """

    def __init__(self, input_size, output_size):
        super().__init__()

        self.fc = nn.Linear(input_size, output_size)

    def forward(self, h_t):
        b_t = self.fc(h_t.detach())
        return b_t


class Decoder(nn.Module):
    """The Decoder of the network to see whether image is reconstructed
        """

    def __init__(self, input_size, latent_dim,output_size):
        super().__init__()

        self.mu_fc = nn.Linear(input_size, latent_dim)
        self.var_fc = nn.Linear(input_size,latent_dim)
        self.decode = nn.Sequential(
                    nn.Linear(latent_dim,output_size//8),
                    nn.Tanh(),
                    nn.Linear(output_size//8,output_size)
        )
        self.relu = nn.ReLU()
        self.sigmoid = nn.Sigmoid()
        if torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"

    def forward(self, h_t):
        mu = self.relu(self.mu_fc(h_t.detach()))
        logvar = self.relu(self.var_fc(h_t.detach()))
        z = self.reparameterization(mu,logvar)
        out = self.sigmoid(self.relu(self.decode(z)))
        return mu,logvar,out

    def reparameterization(self, mean, log_var):
        std = torch.exp(0.5 * log_var)
        eps = torch.normal(0, 0.1, size=(std.size())).to(self.device)
        z = mean + std * eps
        return z

    # Reconstruction error module
    def reconstruction_error(self,x,x_recons):
        '''
        Argms:
        Input:
            model: VAE model
            test_loader: Fashion-MNIST test_loader
        Output:
            avg_err: MSE
        '''
        # set model to eval
        ##################
        ##################
        # Initialize MSE Loss(use reduction='sum')
        ##################
        # TODO:
        x_recons = x_recons.view(x.shape)
        criterion = nn.MSELoss(reduction='mean')(x_recons,x)
        return criterion

    def loss_function(self,recon_x, x, mu, log_var):
        '''
        Compute reconstruction loss and KL divergence loss mentioned in pdf handout
        '''
        recon_x = recon_x.reshape(x.shape)
        bce_loss = nn.BCELoss(reduction='sum')
        BCE = bce_loss(recon_x.to(self.device), x.to(self.device))
        KLD = -0.5 * torch.sum(-torch.exp(log_var) + log_var + 1 - mu**2)
        totalloss = BCE + KLD

        return totalloss, KLD.detach().item(), BCE.detach().item()

class PartialDecoder(nn.Module):
    """The Decoder of the network to see whether image is reconstructed
        """

    def __init__(self, input_size, latent_dim,output_size):
        super().__init__()

        self.mu_fc = nn.Linear(input_size, latent_dim)
        self.var_fc = nn.Linear(input_size,latent_dim)
        self.decode = nn.Sequential(
                    nn.Linear(latent_dim,output_size//8),
                    nn.Tanh(),
                    nn.Linear(output_size//8,output_size)
        )
        self.relu = nn.ReLU()
        self.sigmoid = nn.Sigmoid()
        if torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"

    def forward(self, h_t,masks = None):
        mu = self.relu(self.mu_fc(h_t.detach()))
        logvar = self.relu(self.var_fc(h_t.detach()))
        z = self.reparameterization(mu,logvar)
        out = self.sigmoid(self.relu(self.decode(z)))
        self.masks = masks
        return mu,logvar,out

    def reparameterization(self, mean, log_var):
        std = torch.exp(0.5 * log_var)
        eps = torch.normal(0, 0.1, size=(std.size())).to(self.device)
        z = mean + std * eps
        return z

    # Reconstruction error module
    def reconstruction_error(self,x,x_recons):
        '''
        Argms:
        Input:
            model: VAE model
            test_loader: Fashion-MNIST test_loader
        Output:
            avg_err: MSE
        '''
        # set model to eval
        ##################
        ##################
        # Initialize MSE Loss(use reduction='sum')
        ##################
        # TODO:
        x_recons = x_recons.view(x.shape)
        criterion = nn.MSELoss(reduction='mean')(x_recons,x)
        return criterion

    def loss_function(self,recon_x, x, mu, log_var):
        '''
        Compute reconstruction loss and KL divergence loss mentioned in pdf handout
        '''
        recon_x = recon_x.reshape(x.shape)
        bce_loss = nn.BCELoss(reduction='sum')
        BCE = bce_loss(self.masks.to(self.device)*recon_x.to(self.device), self.masks.to(self.device)*x.to(self.device))
        KLD = -0.5 * torch.sum(-torch.exp(log_var) + log_var + 1 - mu**2)
        totalloss = BCE + KLD
        #print(BCE,KLD)

        return totalloss, KLD.detach().item(), BCE.detach().item()