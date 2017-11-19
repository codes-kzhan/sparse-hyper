import hyper, gaussian
import torch, random, sys
from torch.autograd import Variable
from torch.nn import Parameter
from torch import nn, optim
from tqdm import trange
from tensorboardX import SummaryWriter

import matplotlib.pyplot as plt
import util, logging, time, gc

import psutil, os

logging.basicConfig(filename='run.log',level=logging.INFO)
LOG = logging.getLogger()

"""
Simple experiment: learn the identity function from one tensor to another
"""
w = SummaryWriter()

BATCH = 64
SHAPE = (16, )
CUDA = False
MARGIN = 0.1

torch.manual_seed(2)

nzs = hyper.prod(SHAPE)

N = 300000 // BATCH

plt.figure(figsize=(5,5))
util.makedirs('./spread/')

params = None

model = gaussian.WeightSharingASHLayer(SHAPE, SHAPE, additional=64, k=nzs, sigma_scale=0.2, num_values=2)
# model.initialize(SHAPE, batch_size=64, iterations=100, lr=0.05)

if CUDA:
    model.cuda()

criterion = nn.MSELoss()
optimizer = optim.Adam(model.parameters(), lr=0.01)

for i in trange(N):

    x = torch.rand((BATCH,) + SHAPE)
    if CUDA:
        x = x.cuda()
    x = Variable(x)

    optimizer.zero_grad()

    y = model(x)
    loss = criterion(y, x) # compute the loss

    t0 = time.time()
    loss.backward()        # compute the gradients
    logging.info('backward: {} seconds'.format(time.time() - t0))

    optimizer.step()

    w.add_scalar('identity32/loss', loss.data[0], i*BATCH)

    if i % (N//2500) == 0:
        means, sigmas, values = model.hyper(x)

        plt.clf()
        util.plot(means, sigmas, values, shape=(SHAPE[0], SHAPE[0]))
        plt.xlim((-MARGIN*(SHAPE[0]-1), (SHAPE[0]-1) * (1.0+MARGIN)))
        plt.ylim((-MARGIN*(SHAPE[0]-1), (SHAPE[0]-1) * (1.0+MARGIN)))
        plt.savefig('./spread/means{:04}.png'.format(i))

        print('LOSS', torch.sqrt(loss))