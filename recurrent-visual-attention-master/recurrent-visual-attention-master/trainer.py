import os
import time
import shutil
import pickle
import matplotlib.pyplot as plt
import torch
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.tensorboard import SummaryWriter

from model import RecurrentAttention
from utils import AverageMeter
from modules import Decoder

class Trainer:
    """A Recurrent Attention Model trainer.

    All hyperparameters are provided by the user in the
    config file.
    """

    def __init__(self, config, data_loader):
        """
        Construct a new Trainer instance.

        Args:
            config: object containing command line arguments.
            data_loader: A data iterator.
        """
        self.config = config

        if config.use_gpu and torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")

        # Architecture Params
        self.mode = config.mode

        # glimpse network params
        self.patch_size = config.patch_size
        self.glimpse_scale = config.glimpse_scale
        self.num_patches = config.num_patches
        self.loc_hidden = config.loc_hidden
        self.glimpse_hidden = config.glimpse_hidden

        # core network params
        self.num_glimpses = config.num_glimpses
        self.hidden_size = config.hidden_size
        self.core_net_type = config.core_net_type

        # reinforce params
        self.std = config.std
        self.M = config.M

        # data params
        if config.is_train:
            self.train_loader = data_loader[0]
            self.valid_loader = data_loader[1]
            self.num_train = len(self.train_loader.dataset)
            self.num_valid = len(self.valid_loader.dataset)
        else:
            self.test_loader = data_loader
            self.num_test = len(self.test_loader.dataset)
        self.num_classes = 10
        self.num_channels = 1

        # training params
        self.epochs = config.epochs
        self.start_epoch = 0
        self.momentum = config.momentum
        self.lr = config.init_lr
        self.training_mode = config.training_mode
        self.reward = config.reward
        self.critic_weight = config.critic_weight
        self.actor_weight = config.actor_weight
        self.partial_vae = config.partial_vae
        self.vae_patience = config.vae_patience

        # misc params
        self.best = config.best
        self.ckpt_dir = config.ckpt_dir
        self.logs_dir = config.logs_dir
        self.best_valid_acc = 0.0
        self.counter = 0
        self.lr_patience = config.lr_patience
        self.train_patience = config.train_patience
        self.use_tensorboard = config.use_tensorboard
        self.resume = config.resume
        self.print_freq = config.print_freq
        self.plot_freq = config.plot_freq
        self.model_name = config.model_name
        self.data_type = config.data_type

        self.plot_dir = "./plots/" + self.model_name + "/"
        if not os.path.exists(self.plot_dir):
            os.makedirs(self.plot_dir)

        # configure tensorboard logging
        if self.use_tensorboard:
            tensorboard_dir = self.logs_dir + self.model_name
            print("[*] Saving tensorboard logs to {}".format(tensorboard_dir))
            self.writer = SummaryWriter(log_dir=tensorboard_dir)

        # build RAM model
        self.model = RecurrentAttention(
            self.patch_size,
            self.num_patches,
            self.glimpse_scale,
            self.num_channels,
            self.loc_hidden,
            self.glimpse_hidden,
            self.std,
            self.hidden_size,
            self.num_classes,
            self.core_net_type
        )
        self.model.to(self.device)


        # initialize optimizer and scheduler
        self.optimizer = torch.optim.Adam(
            self.model.parameters(), lr=self.config.init_lr
        )
        
        self.scheduler = ReduceLROnPlateau(
            self.optimizer, "min", patience=self.lr_patience
        )

    def reset(self):
        h_t = torch.zeros(
            self.batch_size,
            self.hidden_size,
            dtype=torch.float,
            device=self.device,
            requires_grad=True,
        )
        l_t = torch.FloatTensor(self.batch_size, 2).uniform_(-1, 1).to(self.device)
        l_t.requires_grad = True

        return h_t, l_t

    def train(self):
        """Train the model on the training set.

        A checkpoint of the model is saved after each epoch
        and if the validation accuracy is improved upon,
        a separate ckpt is created for use on the test set.
        """
        # load the most recent checkpoint
        if self.resume:
            self.load_checkpoint(best=False)

        print(
            "\n[*] Train on {} samples, validate on {} samples".format(
                self.num_train, self.num_valid
            )
        )

        for epoch in range(self.start_epoch, self.epochs):

            print(
                "\nEpoch: {}/{} - LR: {:.6f}".format(
                    epoch + 1, self.epochs, self.optimizer.param_groups[0]["lr"]
                )
            )

            # train for 1 epoch
            train_loss, train_acc = self.train_one_epoch(epoch)

            # evaluate on validation set
            valid_loss, valid_acc = self.validate(epoch)

            # # reduce lr if validation loss plateaus
            self.scheduler.step(-valid_acc)

            is_best = valid_acc > self.best_valid_acc
            msg1 = "train loss: {:.3f} - train acc: {:.3f} "
            msg2 = "- val loss: {:.3f} - val acc: {:.3f} - val err: {:.3f}"
            if is_best:
                self.counter = 0
                msg2 += " [*]"
            msg = msg1 + msg2
            print(
                msg.format(
                    train_loss, train_acc, valid_loss, valid_acc, 100 - valid_acc
                )
            )

            # check for improvement
            if not is_best:
                self.counter += 1
            if self.counter > self.train_patience:
                print("[!] No improvement in a while, stopping training.")
                return
            self.best_valid_acc = max(valid_acc, self.best_valid_acc)
            self.save_checkpoint(
                {
                    "epoch": epoch + 1,
                    "model_state": self.model.state_dict(),
                    "optim_state": self.optimizer.state_dict(),
                    "best_valid_acc": self.best_valid_acc,
                },
                is_best,
            )

    def train_one_epoch(self, epoch):
        """
        Train the model for 1 epoch of the training set.

        An epoch corresponds to one full pass through the entire
        training set in successive mini-batches.

        This is used by train() and should not be called manually.
        """
        self.model.train()
        batch_time = AverageMeter()
        losses = AverageMeter()
        accs = AverageMeter()
        vaelosses = AverageMeter()

        tic = time.time()
        with tqdm(total=self.num_train) as pbar:
            for i, (x, y) in enumerate(self.train_loader):
                self.optimizer.zero_grad()
                if(self.data_type=="mnist-clut"):
                    x_orig = x[1]
                    x = x[0]
                    x_orig = x_orig.to(self.device)
                x, y = x.to(self.device), y.to(self.device)

                plot = False
                if (epoch % self.plot_freq == 0) and (i == 0):
                    plot = True

                # initialize location vector and hidden state
                self.batch_size = x.shape[0]
                h_t, l_t = self.reset()

                # save images
                imgs = []
                imgs.append(x[0:9])

                # extract the glimpses
                locs = []
                log_pi = []
                baselines = []
                class_probs = []
                rc_images = []
                muList = []
                logvarList = []
                masks = None
                masks_glimpse =[]
                for t in range(self.num_glimpses - 1):
                    # forward pass through model
                    h_t, l_t, b_t, p, class_prob,rc_image,mu,logvar,masks = self.model(x, l_t, h_t,masks=masks)

                    # store
                    locs.append(l_t[0:9])
                    baselines.append(b_t)
                    log_pi.append(p)
                    class_probs.append(class_prob.detach())
                    rc_images.append(rc_image)
                    muList.append(mu)
                    logvarList.append(logvar)
                    masks_glimpse.append(masks.clone())
                # last iteration
                h_t, l_t, b_t, p,log_probas,rc_image,mu,logvar,masks = self.model(x, l_t, h_t, last=True,masks=masks)

                muList.append(mu)
                logvarList.append(logvar)
                log_pi.append(p)
                baselines.append(b_t)
                locs.append(l_t[0:9])
                class_probs.append(log_probas.detach())
                rc_images.append(rc_image)
                masks_glimpse.append(masks.clone())
                # convert list to tensors and reshape
                baselines = torch.stack(baselines).transpose(1, 0)
                log_pi = torch.stack(log_pi).transpose(1, 0)
                class_probs = torch.stack(class_probs).transpose(1,0)
                muList = torch.stack(muList).transpose(1,0)
                logvarList = torch.stack(logvarList).transpose(1,0)
                rc_images = torch.stack(rc_images).transpose(1,0)
                masks_glimpse = torch.stack(masks_glimpse).transpose(1,0)
                # calculate the reward for correct classification
                predicted = torch.max(log_probas, 1)[1]
                R = (predicted.detach() == y).float()
                R = R.unsqueeze(1).repeat(1, self.num_glimpses)
                R[:,0:self.num_glimpses-1] = 0

                if self.reward=="logprob":
                    class_probs_reward = torch.sum(-torch.exp(class_probs)*class_probs,dim = 2)
                    class_probs_reward[:, 1:] = class_probs_reward[:, 0:-1] - class_probs_reward[:, 1:]
                    class_probs_reward[:,0] = 2.3 - class_probs_reward[:,0]
                    R += 0.5*class_probs_reward.detach()

                # Discounting with  gamma = 1
                discR = torch.sum(R, dim=1).unsqueeze(1).repeat(1,self.num_glimpses)
                disccumSum = torch.cumsum(R,dim=1)
                discR[:,1:] = discR[:,1:] - disccumSum[:,0:-1]
                # compute losses for differentiable modules
                loss_action = F.nll_loss(log_probas, y)

                # compute reinforce loss
                # summed over timesteps and averaged across batch
                if self.training_mode=="default":
                    loss_baseline = F.mse_loss(baselines, discR)
                    adjusted_reward = discR - baselines.detach()
                    loss_reinforce = torch.sum(-log_pi * adjusted_reward, dim=1)
                    loss_reinforce = torch.mean(loss_reinforce, dim=0)
                elif self.training_mode=="AC2":#TODO
                    advantage = torch.zeros(R.shape).to(self.device)
                    loss_baseline = F.mse_loss(baselines, discR)
                    advantage[:,:(self.num_glimpses-1)] = R[:,:(self.num_glimpses-1)] + baselines.detach()[:,1:self.num_glimpses] - baselines.detach()[:,0:(self.num_glimpses-1)]
                    advantage[:,self.num_glimpses-1] = R[:,self.num_glimpses-1] - baselines.detach()[:,self.num_glimpses-1]
                    loss_reinforce = torch.sum(-log_pi*advantage,dim=1)
                    loss_reinforce = torch.mean(loss_reinforce,dim=0)

                # sum up into a hybrid loss
                if self.data_type=="mnist-clut":
                    x_orig = x_orig.unsqueeze(dim=1).repeat((1, self.num_glimpses, 1, 1, 1))
                else:
                    x = x.unsqueeze(dim=1).repeat((1, self.num_glimpses, 1, 1, 1))
                loss = loss_action + loss_baseline*self.critic_weight+ loss_reinforce * self.actor_weight


                if self.vae_patience<=epoch:
                    if (self.data_type == "mnist-clut"):
                        vae_loss = self.model.decoder.loss_function(rc_images, x_orig, muList, logvarList)[0]
                    else:
                        vae_loss = self.model.decoder.loss_function(rc_images, x, muList, logvarList)[0]


                    vaelosses.update(vae_loss.item(),x.size()[0])
                    vae_loss.backward(retain_graph=True)

                # compute accuracy
                correct = (predicted == y).float()
                acc = 100 * (correct.sum() / len(y))

                # store
                losses.update(loss.item(), x.size()[0])
                accs.update(acc.item(), x.size()[0])

                # compute gradients and update SGD
                loss.backward()
                self.optimizer.step()

                # measure elapsed time
                toc = time.time()
                batch_time.update(toc - tic)

                pbar.set_description(
                    (
                        "{:.1f}s - loss: {:.3f} - acc: {:.3f}".format(
                            (toc - tic), loss.item(), acc.item()
                        )
                    )
                )
                pbar.update(self.batch_size)

                # dump the glimpses and locs
                if plot:
                    imgs = [g.cpu().data.numpy().squeeze() for g in imgs]
                    locs = [l.cpu().data.numpy() for l in locs]
                    pickle.dump(
                        imgs, open(self.plot_dir + "g_{}.p".format(epoch + 1), "wb")
                    )
                    pickle.dump(
                        locs, open(self.plot_dir + "l_{}.p".format(epoch + 1), "wb")
                    )
                
                # log to tensorboard
                if self.use_tensorboard:
                    iteration = epoch * len(self.train_loader) + i
                    self.writer.add_scalar("train_loss", losses.avg, iteration)
                    self.writer.add_scalar("train_acc", accs.avg, iteration)
                    if (self.data_type == "mnist-clut"):
                        self.writer.add_scalar("reconstrunction_loss",
                                               self.model.decoder.reconstruction_error(rc_images, x_orig), iteration)
                    else:
                        self.writer.add_scalar("reconstrunction_loss",self.model.decoder.reconstruction_error(rc_images,x),iteration)
                    self.writer.add_scalar("vae loss",vaelosses.avg,iteration)

            return losses.avg, accs.avg

    @torch.no_grad()
    def validate(self, epoch):
        """Evaluate the RAM model on the validation set.
        """
        losses = AverageMeter()
        accs = AverageMeter()

        for i, (x, y) in enumerate(self.valid_loader):
            if (self.data_type == "mnist-clut"):
                x_orig = x[1]
                x = x[0]
                x_orig = x_orig.to(self.device)
            x, y = x.to(self.device), y.to(self.device)

            # duplicate M times
            x = x.repeat(self.M, 1, 1, 1)

            # initialize location vector and hidden state
            self.batch_size = x.shape[0]
            h_t, l_t = self.reset()

            # extract the glimpses
            log_pi = []
            baselines = []
            class_probs = []
            for t in range(self.num_glimpses - 1):
                # forward pass through model
                h_t, l_t, b_t, p, class_prob,_,_,_,_ = self.model(x, l_t, h_t)

                baselines.append(b_t)
                log_pi.append(p)
                class_probs.append(class_prob)

            # last iteration
            h_t, l_t, b_t, p, log_probas,_ ,_,_,_= self.model(x, l_t, h_t, last=True)
            log_pi.append(p)
            baselines.append(b_t)
            class_probs.append(log_probas)

            # convert list to tensors and reshape
            baselines = torch.stack(baselines).transpose(1, 0)
            log_pi = torch.stack(log_pi).transpose(1, 0)
            class_probs = torch.stack(class_probs).transpose(1, 0)

            # average
            log_probas = log_probas.view(self.M, -1, log_probas.shape[-1])
            log_probas = torch.mean(log_probas, dim=0)

            baselines = baselines.contiguous().view(self.M, -1, baselines.shape[-1])
            baselines = torch.mean(baselines, dim=0)

            log_pi = log_pi.contiguous().view(self.M, -1, log_pi.shape[-1])
            log_pi = torch.mean(log_pi, dim=0)

            # calculate reward
            predicted = torch.max(log_probas, 1)[1]
            R = (predicted.detach() == y).float()
            R = R.unsqueeze(1).repeat(1, self.num_glimpses)

            # compute losses for differentiable modules
            loss_action = F.nll_loss(log_probas, y)
            loss_baseline = F.mse_loss(baselines, R)

            # compute reinforce loss
            adjusted_reward = R - baselines.detach()
            loss_reinforce = torch.sum(-log_pi * adjusted_reward, dim=1)
            loss_reinforce = torch.mean(loss_reinforce, dim=0)

            # sum up into a hybrid loss
            loss = loss_action + loss_baseline + loss_reinforce * 0.01

            # compute accuracy
            correct = (predicted == y).float()
            acc = 100 * (correct.sum() / len(y))

            # store
            losses.update(loss.item(), x.size()[0])
            accs.update(acc.item(), x.size()[0])

            # log to tensorboard
            if self.use_tensorboard:
                iteration = epoch * len(self.valid_loader) + i
                self.writer.add_scalar("val_loss", losses.avg, iteration)
                self.writer.add_scalar("val_acc", accs.avg, iteration)


        return losses.avg, accs.avg

    @torch.no_grad()
    def test(self):
        """Test the RAM model.

        This function should only be called at the very
        end once the model has finished training.
        """
        correct = 0

        # load the best checkpoint
        self.load_checkpoint(best=self.best)
        runningRecError = torch.zeros(self.num_glimpses)
        pltimg = torch.zeros((28,28))
        pltrecs = torch.zeros((28,28))
        glimpses = torch.zeros((6,784))
        for i, (x, y) in enumerate(self.test_loader):
            x, y = x.to(self.device), y.to(self.device)
            if (self.data_type == "mnist-clut"):
                x_orig = x[1]
                x = x[0]
                x_orig = x_orig.to(self.device)
            err = torch.zeros(self.num_glimpses)
            # duplicate M times
            x = x.repeat(self.M, 1, 1, 1)
            if (self.data_type == "mnist-clut"):
                x_orig = x_orig.repeat(self.M, 1, 1, 1)
            # initialize location vector and hidden state
            self.batch_size = x.shape[0]
            h_t, l_t = self.reset()
            testmu = []
            testlogvar = []
            testrecx= []
            # extract the glimpses
            for t in range(self.num_glimpses - 1):
                # forward pass through model
                h_t, l_t, b_t, p,log_probas,rec_x,mu,logvar,_ = self.model(x, l_t, h_t)
                testmu.append(mu)
                testlogvar.append(logvar)
                testrecx.append(rec_x)
            # last iteration
            h_t, l_t, b_t,  p ,log_probas,rec_x,mu,logvar,_ = self.model(x, l_t, h_t, last=True)

            if (self.data_type == "mnist-clut"):
                loss = self.model.decoder.reconstruction_error(x_orig, rec_x)
            else:
                loss = self.model.decoder.reconstruction_error(x, rec_x)
            testrecx.append(rec_x)
            testrecx = torch.stack(testrecx).transpose(1,0)

            if (self.data_type == "mnist-clut"):
                x_orig = x_orig.unsqueeze(dim=1).repeat((1, self.num_glimpses, 1, 1, 1))
            else:
                x = x.unsqueeze(dim=1).repeat((1, self.num_glimpses, 1, 1, 1))

            # runningRecError = self.model.decoder.reconstruction_error(x,rec_x)
            testrecx = testrecx.view((testrecx.shape[0],testrecx.shape[1],-1))
            x = x.view((x.shape[0],x.shape[1],-1))

            if (self.data_type == "mnist-clut"):
                err = torch.mean(torch.norm((testrecx - x_orig),dim=-1)**2,dim=0)
            else:
                err = torch.mean(torch.norm((testrecx - x),dim=-1)**2,dim=0)

            runningRecError += err
            log_probas = log_probas.view(self.M, -1, log_probas.shape[-1])
            log_probas = torch.mean(log_probas, dim=0)

            pred = log_probas.data.max(1, keepdim=True)[1]
            correct += pred.eq(y.data.view_as(pred)).cpu().sum()
            #Randomly generating image to take glimpses about
            if(i==len(self.test_loader)-1):
                pltimg = x
                glimpses = testrecx
        
        self.save_recs(2,pltimg,glimpses,"HardAttwShapingMNIST") #Saves the glimses in ./report

        #For reconstruction error, change file name if runnign another model
        np.save("./report/Reconstruction_hardAttwReshaping",runningRecError/len(self.test_loader))
        plt.plot(runningRecError/len(self.test_loader))
        plt.xlabel("Number of glimpses")
        plt.ylabel("Reconstruction error")
        plt.show()
        perc = (100.0 * correct) / (self.num_test)
        error = 100 - perc
        print(
            "[*] Test Acc: {}/{} ({:.2f}% - {:.2f}%)".format(
                correct, self.num_test, perc, error
            )
        )

    def save_checkpoint(self, state, is_best):
        """Saves a checkpoint of the model.

        If this model has reached the best validation accuracy thus
        far, a seperate file with the suffix `best` is created.
        """
        filename = self.model_name + "_ckpt.pth.tar"
        ckpt_path = os.path.join(self.ckpt_dir, filename)
        torch.save(state, ckpt_path)
        if is_best:
            filename = self.model_name + "_model_best.pth.tar"
            shutil.copyfile(ckpt_path, os.path.join(self.ckpt_dir, filename))

    def load_checkpoint(self, best=False):
        """Load the best copy of a model.

        This is useful for 2 cases:
        - Resuming training with the most recent model checkpoint.
        - Loading the best validation model to evaluate on the test data.

        Args:
            best: if set to True, loads the best model.
                Use this if you want to evaluate your model
                on the test data. Else, set to False in which
                case the most recent version of the checkpoint
                is used.
        """
        print("[*] Loading model from {}".format(self.ckpt_dir))

        filename = self.model_name + "_ckpt.pth.tar"
        if best:
            filename = self.model_name + "_model_best.pth.tar"
        ckpt_path = os.path.join(self.ckpt_dir, filename)
        ckpt = torch.load(ckpt_path)

        # load variables from checkpoint
        self.start_epoch = ckpt["epoch"]
        self.best_valid_acc = ckpt["best_valid_acc"]
        self.model.load_state_dict(ckpt["model_state"])
        self.optimizer.load_state_dict(ckpt["optim_state"])

        if best:
            print(
                "[*] Loaded {} checkpoint @ epoch {} "
                "with best valid acc of {:.3f}".format(
                    filename, ckpt["epoch"], ckpt["best_valid_acc"]
                )
            )
        else:
            print("[*] Loaded {} checkpoint @ epoch {}".format(filename, ckpt["epoch"]))
    
    def save_recs(self,n,img,glimpses_data,mode,saveLast=False,show=False):
        #Plotting glimpses, if show is true, it will print all samples
        for i in range(n):
            idx = np.random.randint(0,len(glimpses_data))
            org = img[idx][-1]
            glimpses = glimpses_data[idx]
            if(saveLast):
                ax1 = plt.subplot(121)
                ax2 = plt.subplot(122)
                ax1.imshow(org.view(28,28),cmap='gray')
                ax2.imshow(glimpses[-1].view(28,28),cmap='gray')

            else:
                ax0g = plt.subplot(171)
                ax1g = plt.subplot(172)
                ax2g = plt.subplot(173)
                ax3g = plt.subplot(174)
                ax4g = plt.subplot(175)
                ax5g = plt.subplot(176)
                ax6g = plt.subplot(177)

                ax0g.imshow(org.view(28,28),cmap='gray')
                ax1g.imshow(glimpses[0].view(28,28),cmap='gray')
                ax2g.imshow(glimpses[1].view(28,28),cmap='gray')
                ax3g.imshow(glimpses[2].view(28,28),cmap='gray')
                ax4g.imshow(glimpses[3].view(28,28),cmap='gray')
                ax5g.imshow(glimpses[4].view(28,28),cmap='gray')
                ax6g.imshow(glimpses[5].view(28,28),cmap='gray')
            plt.savefig('./report/'+mode+'_glimpses_{0}.jpg'.format(i)) #CHANGE NAME IF PLOTTING SOME OTHER MODE
            
            if(show):
                plt.show()
            plt.close()



