import numpy as np
import cupy
import chainer
from chainer import cuda
import glob
import struct
import code
import chainer.serializers as serializers
import matplotlib
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
'''
Data utils
'''
DATA_PATH = '/home/evan/Data/nbody_simulations/N_{0}/DM*/{1}_dm.z=0{2}000'


#=============================================================================
# Data utils
#=============================================================================
def read_sim(file_list, n_P):
    """ reads simulation data from disk and returns

    Args:
        file_list: (list<str>) paths to files
        n_P: (int) number of particles base (n_P**3 particles)
    """
    num_particles = n_P**3
    dataset = []
    for file_name in file_list:
        this_set = []
        with open(file_name, "rb") as f:
            for i in range(num_particles*6):
                s = struct.unpack('=f',f.read(4))
                this_set.append(s[0])
        dataset.append(this_set)
    dataset = np.array(dataset).reshape([len(file_list),num_particles,6])  
    return dataset

def load_datum(n_P, redshift, normalize_data=False):
    """ loads two redshift datasets from proper data directory

    Args:
        redshift: (float) redshift
        n_P: (int) base of number of particles (n_P**3 particles)
    """
    N_P = 10000 if n_P == 32 else 1000
    glob_paths = glob.glob(DATA_PATH.format(N_P, 'xv', redshift))
    X = read_sim(glob_paths, n_P)
    if normalize_data:
        X = normalize(X)
    return X

def load_data(n_P, *args, **kwargs):
    """ loads datasets from proper data directory
    # Note: is this function necessary? may add unnecessary coupling,
    can probably do everything with a single load data fun.

    Args:
        n_P: (int) base of number of particles (n_P**3 particles)
    """
    data = []
    for redshift in args:
        x = load_datum(n_P, redshift, **kwargs)
        data.append(x)
    return data

def normalize(X_in):
    """ Normalize features
    coordinates are rescaled to (0,1)
    velocities normalized by mean/std    
    """
    x_r = np.reshape(X_in, [-1,6])
    coo, vel = np.split(x_r, [3], axis=-1)
    coo_min = np.min(coo, axis=0)
    coo_max = np.max(coo, axis=0)
    #coo_mean, coo_std = np.mean(coo,axis=0), np.std(coo,axis=0)
    #x_r[:,:3] = (x_r[:,:3] - coo_mean) / (coo_std)
    vel_mean = np.mean(vel, axis=0)
    vel_std  = np.std( vel, axis=0)
    x_r[:,:3] = (x_r[:,:3] - coo_min) / (coo_max - coo_min)
    x_r[:,3:] = (x_r[:,3:] - vel_mean) / vel_std
    X_out = np.reshape(x_r,X_in.shape).astype(np.float32) # just convert to float32 here
    return X_out

class nBodyDataset():
    def __init__(self, num_particles, zX, zY, normalize_data=True, validation=True, use_GPU=True):
        # do stuff with validation
        self.num_particles = num_particles
        self.zX, self.zY = zX, zY
        self.validation = validation
        self.use_GPU = use_GPU
        self.xp = cupy if use_GPU else np
        self.data = {}

        X, Y = load_data(num_particles, zX, zY, normalize_data=normalize_data)
        if use_GPU:
            X, Y = cuda.to_gpu(X), cuda.to_gpu(Y)
        if validation:
            X_sets, Y_sets = split_data_validation(X,Y)
            self.X_train = X_sets[0]
            self.X_val   = X_sets[1]
            #self.data['X_train'] = X_sets[0]
            #self.data['X_val']   = X_sets[1]
            
            self.Y_train = Y_sets[0]
            self.Y_val   = Y_sets[1]
            #self.data['Y_train'] = Y_sets[0]
            #self.data['Y_val']   = Y_sets[1]
        else:
            #self.data['X_train'] = X
            #self.data['Y_train'] = Y
            self.X_train = X
            self.Y_train = Y

    def shift_data(self, x, y):
        batch_size, N, D = x.shape
        rands = self.xp.random.rand(D) # 6
        shift = self.xp.random.rand(batch_size, 3) # for loc only
        out = []
        for tmp in [x,y]:
            if rands[0] < .5:
                tmp = tmp[:,:,[1,0,2,4,3,5]]
            if rands[1] < .5:
                tmp = tmp[:,:, [0,2,1,3,5,4]]
            if rands[2] < .5:
                tmp = tmp[:,:, [2,1,0,5,4,3]]
            if rands[3] < .5:
                tmp[:,:,0] = 1 - tmp[:,:,0]
                tmp[:,:,3] = -tmp[:,:,3]
            if rands[4] < .5:
                tmp[:,:,1] = 1 - tmp[:,:,1]
                tmp[:,:,4] = -tmp[:,:,4]
            if rands[5] < .5:
                tmp[:,:,2] = 1 - tmp[:,:,2]
                tmp[:,:,5] = -tmp[:,:,5]            
            tmploc = tmp[:,:,:3]
            tmploc += shift[:,None,:]
            gt1 = tmploc > 1
            tmploc[gt1] = tmploc[gt1] - 1
            tmp[:,:,:3] = tmploc
            out.append(tmp)
        return out

    def next_minibatch(self, batch_size, shift=True):
        N,M,D = self.X_train.shape
        index_list = self.xp.random.choice(N, batch_size)
        x = self.X_train[index_list]#self.xp.copy(self.X_train[index_list])
        y = self.Y_train[index_list]#self.xp.copy(self.Y_train[index_list])
        if shift:
            x,y = self.shift_data(x,y)
        return x,y

    def __call__(self, batch_size, val_idx=None):
        to_var = lambda arr: chainer.Variable(arr)
        if val_idx is not None:
            val_start, val_stop = val_idx
            x_val = self.X_val[val_start:val_stop]
            y_val = self.Y_val[val_start:val_stop]
            return x_val, y_val
            
        else:
            x_train, y_train = self.next_minibatch(batch_size)
            return x_train, y_train


def split_data_validation(X, Y, num_val_samples=200):
    """ split dataset into training and validation sets
    
    Args:        
        X, Y (ndarray): data arrays of shape (num_samples, num_particles, 6)
        num_val_samples (int): size of validation set
    """
    num_samples = X.shape[0]
    idx_list = np.random.permutation(num_samples)
    X, Y = X[idx_list], Y[idx_list]
    X_train, X_val = X[:-num_val_samples], X[-num_val_samples:]#np.split(X, [-num_val_samples])
    Y_train, Y_val = Y[:-num_val_samples], Y[-num_val_samples:]#np.split(Y, [-num_val_samples])
    return [(X_train, X_val), (Y_train, Y_val)]



def next_minibatch(in_list,batch_size):
    if all(len(i) == len(in_list[0]) for i in in_list) == False:   
        raise ValueError('Inputs do not have the same dimension')
    index_list = np.random.permutation(len(in_list[0]))[:batch_size]#np.random.randint(len(in_list[0]), size=batch_size)
    out = []
    rands = np.random.rand(6)
    shift = np.random.rand(batch_size,3)
    for k in range(len(in_list)):
        tmp = in_list[k][index_list]
        if rands[0] < .5:
            tmp = tmp[:,:,[1,0,2,4,3,5]]
        if rands[1] < .5:
            tmp = tmp[:,:, [0,2,1,3,5,4]]
        if rands[2] < .5:
            tmp = tmp[:,:, [2,1,0,5,4,3]]
        if rands[3] < .5:
            tmp[:,:,0] = 1 - tmp[:,:,0]
            tmp[:,:,3] = -tmp[:,:,3]
        if rands[4] < .5:
            tmp[:,:,1] = 1 - tmp[:,:,1]
            tmp[:,:,4] = -tmp[:,:,4]
        if rands[5] < .5:
            tmp[:,:,2] = 1 - tmp[:,:,2]
            tmp[:,:,5] = -tmp[:,:,5]
            
        tmploc = tmp[:,:,:3]
        tmploc += shift[:,None,:]
        gt1 = tmploc > 1
        tmploc[gt1] = tmploc[gt1] - 1
        tmp[:,:,:3] = tmploc
        out.append(tmp)
    return out

def gpunext_minibatch(in_list, batch_size, xp=cupy):
    assert len(set([a.shape for a in in_list])) == 1
    M, N, D = in_list[0].shape
    index_list = xp.random.choice(M, batch_size)
    out = []
    rands = xp.random.rand(6)
    shift = xp.random.rand(batch_size, 3)
    for k in range(len(in_list)):
        tmp = in_list[k][index_list]
        if rands[0] < .5:
            tmp = tmp[:,:,[1,0,2,4,3,5]]
        if rands[1] < .5:
            tmp = tmp[:,:, [0,2,1,3,5,4]]
        if rands[2] < .5:
            tmp = tmp[:,:, [2,1,0,5,4,3]]
        if rands[3] < .5:
            tmp[:,:,0] = 1 - tmp[:,:,0]
            tmp[:,:,3] = -tmp[:,:,3]
        if rands[4] < .5:
            tmp[:,:,1] = 1 - tmp[:,:,1]
            tmp[:,:,4] = -tmp[:,:,4]
        if rands[5] < .5:
            tmp[:,:,2] = 1 - tmp[:,:,2]
            tmp[:,:,5] = -tmp[:,:,5]
            
        tmploc = tmp[:,:,:3]
        tmploc += shift[:,None,:]
        gt1 = tmploc > 1
        tmploc[gt1] = tmploc[gt1] - 1
        tmp[:,:,:3] = tmploc
        out.append(tmp)
    return out

def to_var_xp(lst_data):
    return [chainer.Variable(data) for data in lst_data]

def to_variable(lst_data, use_gpu=True):
    xp = cupy if use_gpu else np
    chainer_vars = []
    for data in lst_data:
        data = data.astype(xp.float32)
        if use_gpu: data = cuda.to_gpu(data)
        data_var = chainer.Variable(data)
        chainer_vars.append(data_var)
    return chainer_vars

def save_data_batches(batch_tuple, save_name):
    x_in, xt, xh = batch_tuple
    assert x_in.shape[0] == xt.shape[0] == xh.shape[0]
    np.save(save_name + 'input', x_in)
    np.save(save_name + 'truth', xt)
    np.save(save_name + 'hat'  , xh)
    print('data saved')

def save_model(mopt_tuple, save_name):
    model, opt = mopt_tuple
    serializers.save_npz(save_name + '.model', model)
    serializers.save_npz(save_name + '.state', opt)

#=============================================================================
# Plotting utils
#=============================================================================
def plot_3D_pointcloud(xt, xh, j, pt_size=(.9,.9), colors=('b','r'), fsize=(18,18), xin=None):
    xt_x, xt_y, xt_z = np.split(xt[...,:3], 3, axis=-1)
    xh_x, xh_y, xh_z = np.split(xh[...,:3], 3, axis=-1)
    
    fig = plt.figure(figsize=fsize)
    ax = fig.add_subplot(111, projection='3d')
    ax.scatter(xt_x[j], xt_y[j], xt_z[j], s=pt_size[0], c=colors[0])
    ax.scatter(xh_x[j], xh_y[j], xh_z[j], s=pt_size[1], c=colors[1])

    ax.set_xlabel('X')
    ax.set_ylabel('Y')
    ax.set_zlabel('Z')
    return fig

def plot_training_curve(y, cur_iter, yclip=.0004, c='b', poly=None, fsize=(16,10), title=None):
    fig = plt.figure(figsize=fsize)
    ax = fig.add_subplot(111)
    ax.set_xlabel('Iterations')
    ax.set_ylabel('MSE')

    cur = str(cur_iter)
    yclipped = np.clip(y, 0, yclip) if yclip > 0 else y
    plt.grid(True)
    ax.plot(yclipped,c=c)
    if poly is not None:
        xvals = np.arange(cur_iter)
        pfit = np.poly1d(np.polyfit(xvals, yclipped, poly))
        ax.plot(poly(xvals), c='orange', linewidth=3)
    if title is None:
        title = 'Iteration: {0}, loss: {1:.4}'.format(cur_iter, y[-1])
    ax.set_title(title)
    return fig