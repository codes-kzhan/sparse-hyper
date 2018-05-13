import hyper, gaussian, util, time, pretrain, os, math, sys
import torch, random
from torch.autograd import Variable
from torch import nn, optim
from torch.nn import Parameter
from tqdm import trange, tqdm
from tensorboardX import SummaryWriter
from util import Lambda, Debug

from torch.utils.data import TensorDataset, DataLoader

from torchsample.metrics import CategoricalAccuracy

import torch.optim as optim

import torchvision
import torchvision.transforms as transforms

from util import od, prod, logit

from argparse import ArgumentParser

import logging

import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

LOG = logging.getLogger('ash')
LOG.setLevel(logging.INFO)
fh = logging.FileHandler('ash.log')
fh.setLevel(logging.INFO)
LOG.addHandler(fh)

def inv(i):
    sc = (i/27) * 0.9999 + 0.00005
    return logit(sc)

def sigmoid(x):
  return 1 / (1 + math.exp(-x))

class MNISTLayer(gaussian.HyperLayer):
    """
    Simple hyperlayer for the 1D MNIST experiment

    NB: k is the number of tuples _per hidden node_.
    """

    def __init__(self, k, adaptive=True, pre=28, out=28, additional=0, sigma_scale=0.1, num_values=-1, min_sigma=0.0, subsample=None):

        super().__init__(in_rank=1, out_shape=(out,), additional=additional, bias_type=gaussian.Bias.DENSE, subsample=subsample)

        self.k = k
        self.sigma_scale = sigma_scale
        self.num_values = num_values
        self.min_sigma = min_sigma
        self.out=out
        self.adaptive = adaptive
        self.pre = pre

        out_indices = torch.FloatTensor(range(out)).unsqueeze(1).expand(out, k).contiguous().view(-1)

        if self.adaptive:

            # one-hot matrix for the inputs to the hypernetwork
            one_hots = torch.zeros(out_indices.size()[0], 28 + k)
            for r in range(out_indices.size()[0]):

                one_hots[r, int(out_indices[r])] = 1

                one_hots[r, 28 + r % k] = 1

                # print(out_indices[r, :], out_size)
                #  print(one_hots[r, :].view(1, -1))

            # convert out_indices to float values that return the correct indices when sigmoided.
            self.register_buffer('one_hots', one_hots)
            self.register_buffer('out_indices', inv(out_indices))

            if pre > 0:
                self.preprocess = nn.Sequential(
                    nn.Linear(28, pre)
                )

            activation = nn.ReLU()

            hidden = 32

            self.source = nn.Sequential(
                nn.Linear(pre + 28 + k, 3),
                # activation,
                # nn.Linear(hidden, hidden),
                # activation,
                # nn.Linear(hidden, hidden),
                # activation,
                # nn.Linear(hidden, hidden),
                # nn.Linear(hidden, 3),
            )

        else:
            self.nas = Parameter(torch.randn(self.k * out, 3))

        self.bias = Parameter(torch.zeros(out))

        if num_values > 0:
            self.values = Parameter(torch.randn((num_values,)))

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """
        b, d = input.size()
        l, = self.out_indices.size()
        l, dh = self.one_hots.size()

        outs = Variable(self.out_indices.unsqueeze(0).expand(b, l).unsqueeze(2))
        hots = Variable(self.one_hots.unsqueeze(0).expand(b, l, dh))

        if self.adaptive:
            if self.pre > 0:
                input = self.preprocess(input)

            b, d = input.size()

            input = input.view(b, 1, -1).expand(b, l, d)

            if self.pre == 0:
                input = hots
            else:
                input = torch.cat([input, hots], dim=2)

            input = input.contiguous().view(b*l, -1)

            res = self.source(input).unsqueeze(2).view(b, l , 3)
        else:
            res = self.nas.unsqueeze(0).expand(b, l, 3)

        res = torch.cat([outs, res], dim=2)

        means, sigmas, values = self.split_out(res, (28,), (self.out,))

        sigmas = sigmas * self.sigma_scale + self.min_sigma

        if self.num_values > 0:
            mult = self.k // self.num_values

            values = self.values.unsqueeze(0).expand(mult, self.num_values)
            values = values.contiguous().view(-1)[:self.k]

            values = values.unsqueeze(0).expand(b, self.k)

        self.last_values = values.data

        return means, sigmas, values, self.bias

class ConvLayer(gaussian.HyperLayer):
    """
   Simple hyperlayer for the 1D MNIST experiment

    """
    def __init__(self, kernel_size=3, out=28, additional=0, sigma_scale=0.1, min_sigma=0.0, subsample=None):

        super().__init__(in_rank=1, out_shape=(out,), additional=additional, bias_type=gaussian.Bias.DENSE, subsample=subsample)

        # ignored for now
        self.kernel_size = kernel_size

        self.sigma_scale = sigma_scale
        self.min_sigma = min_sigma
        self.out=out

        outsize = (3 * 28 - 2) * 4

        self.nasind = torch.zeros(3 * 28 - 2, 2)

        index = 0
        for i in range(28):
            if  i > 0:
                self.nasind[index, :] = torch.FloatTensor([inv(i), inv(i-1)])
                index += 1

            self.nasind[index, :]     = torch.FloatTensor([inv(i), inv(i)])
            index += 1

            if i < 27:
                self.nasind[index, :] = torch.FloatTensor([inv(i), inv(i+1)])
                index += 1

        self.nasind = Parameter(self.nasind)

        self.values = Parameter(torch.randn((3,)))

        self.nassig = Parameter(torch.ones(3 * 28 - 2, 1))

        self.bias = Parameter(torch.zeros(out))

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """

        b, _ = input.size()
        k, _ = self.nasind.size()

        nasval = self.values.unsqueeze(0).expand(28, 3).contiguous().view(-1, 1)[1:-1, :]

        nas = torch.cat([self.nasind, self.nassig, nasval], dim=1)
        res = nas.unsqueeze(0).expand(b, k, 4)

        means, sigmas, values = self.split_out(res, (28,), (self.out,))

        sigmas = sigmas * self.sigma_scale + self.min_sigma

        self.last_values = values.data

        return means, sigmas, values, self.bias

PLOT = True
COLUMN = 13

def go(batch=64, epochs=350, k=750, additional=64, modelname='baseline', cuda=False,
       seed=1, lr=0.001, subsample=None, num_values=-1, min_sigma=0.0,
       tb_dir=None, data='./data', hidden=28):

    FT = torch.cuda.FloatTensor if cuda else torch.FloatTensor

    torch.manual_seed(seed)

    w = SummaryWriter(log_dir=tb_dir)

    normalize = transforms.Compose([transforms.ToTensor()])
    train = torchvision.datasets.MNIST(root=data, train=True, download=True, transform=normalize)
    trainloader = torch.utils.data.DataLoader(train, batch_size=batch, shuffle=True, num_workers=2)
    test = torchvision.datasets.MNIST(root=data, train=False, download=True, transform=normalize)
    testloader = torch.utils.data.DataLoader(test, batch_size=batch, shuffle=False, num_workers=2)

    activation = nn.ReLU()

    hyperlayer = None

    if modelname == 'baseline':

        model = nn.Sequential(
            nn.Linear(28, hidden),
            activation,
            nn.Linear(hidden, 10),
            nn.Softmax())

    elif modelname == 'baseline-conv':

        model = nn.Sequential(
            util.Lambda(lambda x : x.unsqueeze(1)),
            nn.Conv1d(1, 1, kernel_size=3, padding=1),
            util.Lambda(lambda x : x.squeeze(1)),
            activation,
            nn.Linear(hidden, 10),
            nn.Softmax())

    elif modelname == 'baseline-conv3':

        model = nn.Sequential(
            util.Lambda(lambda x : x.unsqueeze(1)),
            nn.Conv1d(1, 1, kernel_size=3, padding=1),
            nn.Conv1d(1, 1, kernel_size=3, padding=1),
            util.Lambda(lambda x : x.squeeze(1)),
            activation,
            nn.Linear(hidden, 10),
            nn.Softmax())

    elif modelname == 'conv3':

        hyperlayer = ConvLayer(additional=additional, min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            util.Lambda(lambda x: x.unsqueeze(1)),
            nn.Conv1d(1, 1, kernel_size=3, padding=1),
            # nn.Conv1d(1, 1, kernel_size=3, padding=1),
            util.Lambda(lambda x: x.squeeze(1)),
            activation,
            nn.Linear(28, 10),
            nn.Softmax())

    elif modelname == 'conv':

        hyperlayer = ConvLayer(additional=additional, min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            activation,
            nn.Linear(28, 10),
            nn.Softmax())

    elif modelname == 'ash':

        hyperlayer = MNISTLayer(k, out=hidden, adaptive=True, pre=0, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            activation,
            nn.Linear(28, 10),
            nn.Softmax())

    elif modelname == 'nas':

        hyperlayer = MNISTLayer(k, out=hidden, adaptive=False, additional=additional, num_values=num_values,
                                min_sigma=min_sigma, subsample=subsample)

        model = nn.Sequential(
            hyperlayer,
            activation,
            nn.Linear(28, 10),
            nn.Softmax())
    else:
        raise Exception('Model name {} not recognized'.format(modelname))

    if cuda:
        model.cuda()
        if hyperlayer is not None:
            hyperlayer.apply(lambda t: t.cuda())

    optimizer = optim.Adam(model.parameters(), lr=lr)

    xent = nn.CrossEntropyLoss()
    acc = CategoricalAccuracy()

    step = 0

    sigs, vals = [], []

    normalize = transforms.Compose([transforms.ToTensor()])
    train = torchvision.datasets.MNIST(root=data, train=True, download=True, transform=normalize)
    trainloader = torch.utils.data.DataLoader(train, batch_size=batch, shuffle=True, num_workers=2)
    test = torchvision.datasets.MNIST(root=data, train=False, download=True, transform=normalize)
    testloader = torch.utils.data.DataLoader(test, batch_size=batch, shuffle=False, num_workers=2)

    util.makedirs('./mnist1d/')


    for epoch in range(epochs):

        for i, data in tqdm(enumerate(trainloader, 0)):

            # get the inputs
            inputs, labels = data

            inputs = inputs.squeeze(1) # rm channel dim
            inputs = inputs[:, :, COLUMN].contiguous()

            if cuda:
                inputs, labels = inputs.cuda(), labels.cuda()

            # wrap them in Variables
            inputs, labels = Variable(inputs), Variable(labels)

            optimizer.zero_grad()

            outputs = model(inputs)

            loss = xent(outputs, labels)

            t0 = time.time()
            loss.backward()  # compute the gradients
            logging.info('backward: {} seconds'.format(time.time() - t0))

            # print(hyperlayer.values, hyperlayer.values.grad)

            optimizer.step()

            w.add_scalar('mnist1d/train-loss', loss.data[0], step)

            step += inputs.size()[0]

            if PLOT and i == 0 and hyperlayer is not None:
                plt.figure(figsize=(7, 7))

                means, sigmas, values, _ = hyperlayer.hyper(inputs)

                plt.cla()
                util.plot(means, sigmas, values, shape=(28, hidden))
                plt.xlim((-0.1 * 27, 27 * 1.1))
                plt.ylim((-0.1 * 27, 27 * 1.1))

                plt.savefig('./mnist1d/means{:04}.png'.format(epoch))

                sigmas = list(hyperlayer.last_sigmas[0, :])
                values = list(hyperlayer.last_values[0, :])

                sigs.append(sigmas)
                vals.append(values)

                ax = plt.figure().add_subplot(111)

                for j, (s, v) in enumerate(zip(sigs, vals)):
                    ax.scatter([j] * len(s), s, c=v, linewidth=0,  alpha=0.2, cmap='RdYlBu', vmin=-1.0, vmax=1.0)

                ax.set_aspect('auto')
                plt.ylim(ymin=0)
                util.clean()

                plt.savefig('sigmas.pdf')
                plt.savefig('sigmas.png')

        total = 0.0
        num = 0

        for i, data in enumerate(testloader, 0):

            # get the inputs
            inputs, labels = data

            inputs = inputs.squeeze(1)  # rm channel dim
            inputs = inputs[:, :, COLUMN].contiguous()

            if cuda:
                inputs, labels = inputs.cuda(), labels.cuda()

            # wrap them in Variables
            inputs, labels = Variable(inputs), Variable(labels)

            outputs = model(inputs)

            total += acc(outputs, labels)
            num += 1

        accuracy = total / num

        w.add_scalar('mnist1d/per-epoch-test-acc', accuracy, epoch)
        print('EPOCH {}: {} accuracy '.format(epoch, accuracy))

    LOG.info('Finished Training.')

if __name__ == "__main__":

    ## Parse the command line options
    parser = ArgumentParser()

    parser.add_argument("-e", "--epochs",
                        dest="epochs",
                        help="Number of epochs over thegenerated data.",
                        default=350, type=int)

    parser.add_argument("-m", "--model",
                        dest="model",
                        help="Which model to train.",
                        default='baseline')

    parser.add_argument("-b", "--batch-size",
                        dest="batch_size",
                        help="The batch size.",
                        default=64, type=int)

    parser.add_argument("-k", "--num-points",
                        dest="k",
                        help="Number of index tuples in the decoder layer",
                        default=3, type=int)

    parser.add_argument("-a", "--additional",
                        dest="additional",
                        help="Number of additional points sampled",
                        default=64, type=int)

    parser.add_argument("-c", "--cuda", dest="cuda",
                        help="Whether to use cuda.",
                        action="store_true")

    parser.add_argument("-D", "--data", dest="data",
                        help="Data directory",
                        default='./data')

    parser.add_argument("-l", "--learn-rate",
                        dest="lr",
                        help="Learning rate",
                        default=0.001, type=float)

    parser.add_argument("-S", "--subsample",
                        dest="subsample",
                        help="Sample a subset of the indices to estimate gradients for",
                        default=None, type=float)

    parser.add_argument("-F", "--num-values", dest="num_values",
                        help="How many fixed values to allow the network",
                        default=-1, type=int)

    parser.add_argument("-M", "--min-sigma",
                        dest="min_sigma",
                        help="Minimum value of sigma.",
                        default=0.0, type=float)

    parser.add_argument("-T", "--tb_dir", dest="tb_dir",
                        help="Data directory",
                        default=None)

    options = parser.parse_args()

    print('OPTIONS ', options)
    LOG.info('OPTIONS ' + str(options))

    go(epochs=options.epochs, batch=options.batch_size, k=options.k,
        additional=options.additional, modelname=options.model, cuda=options.cuda,
        lr=options.lr, subsample=options.subsample,
        num_values=options.num_values, min_sigma=options.min_sigma,
        tb_dir=options.tb_dir, data=options.data)
