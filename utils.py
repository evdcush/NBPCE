import os, glob, struct, shutil, code, sys, time
import numpy as np
import tensorflow as tf
import matplotlib
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D

#=============================================================================
# Globals
#=============================================================================
#------------------------------------------------------------------------------
# Data and pathing
#------------------------------------------------------------------------------
# Redshifts available in dataset
# ========================================
REDSHIFTS = [9.0000, 4.7897, 3.2985, 2.4950, 1.9792, 1.6141, 1.3385,
             1.1212, 0.9438, 0.7955, 0.6688, 0.5588, 0.4620, 0.3758,
             0.2983, 0.2280, 0.1639, 0.1049, 0.0505, 0.0000]

# Data load paths (must be changed for your machine!)
# ========================================
DATA_ROOT_PATH = '/home/evan/Data/nbody_simulations/N_uniform/{}'
DATA_PATH_BINARIES = DATA_ROOT_PATH.format('binaries/run*/xv_dm.z=0{:.4f}') # not in use
DATA_PATH_NPY      = DATA_ROOT_PATH.format('npy_data/X_{:.4f}_.npy')


# Data write paths
# ========================================
# All model save paths
BASE_SAVE_PATH = '../Models/{}/'
MODEL_SAVE_PATH   = BASE_SAVE_PATH  + 'Session/'
FILE_SAVE_PATH    = MODEL_SAVE_PATH + 'original_files/'
RESULTS_SAVE_PATH = BASE_SAVE_PATH  + 'Results/'
FILE_SAVE_NAMES = ['utils.py', 'nn.py', 'train_ShiftInv.py', 'train_multi_ShiftInv.py']

# Model data names
CUBE_BASE_NAME = 'X_{}-{}_'
CUBE_NAME_TRUTH = CUBE_BASE_NAME + 'truth'
CUBE_NAME_PRED  = CUBE_BASE_NAME + 'prediction'


#------------------------------------------------------------------------------
# Model naming
#------------------------------------------------------------------------------
# Model and layer names
# ========================================
# Model names
MODEL_TYPES = ['multi-step', 'single-step']
MODEL_BASENAME = '{}_{}_{}' # {model-type}_{layer-type}_{rs1-...-rsN}_{extra-naming}

# Layer names
VANILLA   = 'vanilla'
SHIFT_INV = 'shift-inv'
ROT_INV   = 'rot-inv'
LAYER_TYPES = [VANILLA, SHIFT_INV, ROT_IN]
LAYER_INIT_FUNCS = {VANILLA:   initialize_vanilla_params,
                    SHIFT_INV: initialize_ShiftInv_params,
                    ROT_INV:   initialize_RotInv_params}

# Variable names
# ========================================
# Scope naming
VARIABLE_SCOPE = 'params_{}-{}' # eg. 'params_0-7' for rs 9.0000 --> 1.1212

# Model variable names
WEIGHT_TAG = 'W{}_{}'
BIAS_TAG   = 'B_{}'
SCALAR_TAG = 'T_{}'


#------------------------------------------------------------------------------
# Model variables
#------------------------------------------------------------------------------
# Model params
# ========================================
# RNG seeds
PARAMS_SEED  = 77743196 # Randomly generated seed selected by cross-validation
DATASET_SEED = 12345    # for train/validation data splits

# Network channels
CHANNELS = [6, 8, 16, 32, 16, 8, 3, 8, 16, 32, 16, 8, 3]
CHANNELS_SHALLOW = [9, 32, 16, 8, 6]


# Layer variables
# ========================================
# Shift-invariant
SHIFT_INV_W_IDX = [1,2,3,4]

# Rotation-invariant
ROTATION_INV_SEGMENTS = ['no-pooling', 'col-depth', 'row-depth',
                         'row-col', 'depth', 'col', 'row', 'all']


# Training variables
# ========================================
LEARNING_RATE = 0.01
NUM_VAL_SAMPLES = 200



#=============================================================================
# TensorFlow Variable inits and gets
#=============================================================================
#------------------------------------------------------------------------------
# Parameter initialization
#------------------------------------------------------------------------------
# Variable inits
# ========================================
def initialize_var(args_init, initializer):
    tf.get_variable(*args_init, dtype=tf.float32, initializer=initializer)


def initialize_weight(name, kdims, restore=False):
    """ initialize weight Variable
    Args:
        name (str): variable name
        kdims tuple(int): kernel sizes (k_in, k_out)
        restore (bool): if restore, then do not user initializer
    """
    args_init = (name, kdims)
    initializer = None if restore else tf.glorot_normal_initializer(None)
    initialize_var(args_init, initializer)


def initialize_bias(name, kdims, restore=False):
    """ biases initialized to be near zero
    Args:
        name (str): variable name
        kdims tuple(int): kernel sizes (k_in, k_out), only k_out used for bias
        restore (bool): if restore, then do not user initializer
    """
    k_out = kdims[-1]
    args_init = (name,)
    if restore:
        initializer = None
        args_init += (k_out,)
    else:
        initializer = tf.ones((k_out,), dtype=tf.float32) * 1e-8
    initialize_var(args_init, initializer)


def initialize_scalars(init_val=0.002, restore=False):
    """ 1D scalars used to scale network outputs """
    initializer = tf.constant([init_val])
    for i in range(2):
        initialize_var((SCALAR_TAG.format(i),), initializer)


# Model parameter init wrappers
# ========================================
def initialize_vanilla_params(kdims, restore=False, **kwargs):
    """ Vanilla layers have 1 bias, 1 weight, scalars"""
    for layer_idx, kdim in enumerate(kdims):
        bname = BIAS_TAG.format(layer_idx)
        Wname = WEIGHT_TAG.format(layer_idx, 0)
        initialize_bias(  bname, kdim, restore=restore)
        initialize_weight(Wname, kdim, restore=restore)
    initialize_scalars(restore=restore)


def initialize ShiftInv_params(kdims, restore=False, **kwargs):
    """ ShiftInv layers have 1 bias, 4 weights, scalars """
    for layer_idx, kdim in enumerate(kdims):
        initialize_bias(BIAS_TAG.format(layer_idx), kdim, restore=restore)
        for w_idx in SHIFT_INV_W_IDX:
            Wname = WEIGHT_TAG.format(layer_idx, w_idx)
            initialize_weight(Wname, kdim, restore=restore)
    initialize_scalars(restore=restore)


def initialize_RotInv_params(kdims, restore=False, **kwargs):
    # TODO
    assert False


def initialize_model_params(layer_type, channels, scope=VAR_SCOPE,
                            seed=PARAMS_SEED, restore=False, **kwargs):
    """ Initialize model parameters, dispatch based on layer_type
    Args:
        layer_type (str): layer-type ['vanilla', 'shift-inv', 'rot-inv']
        channels list(int): network channels
        scope (str): scope for tf.variable_scope
        seed (int): RNG seed for param inits
        restore (bool): whether new params are initialized, or just placeholders
    """
    # Check layer_type integrity
    assert layer_type in LAYER_TYPES

    # Convert channels to (k_in, k_out) tuples
    kdims = [(channels[i], channels[i+1]) for i in range(len(channels) - 1)]

    # Seed and initialize
    tf.set_random_seed(seed)
    layer_init_func = LAYER_INIT_FUNCS[layer_type]
    with tf.variable_scope(scope, reuse=True):
        layer_init_func(kdims, restore=restore, **kwargs)
    print('Initialized {} layer parameters'.format(layer_type))



#------------------------------------------------------------------------------
# Parameter getters
#------------------------------------------------------------------------------
# Variable gets
# ========================================
def get_var(name):
    """ Assumes within variable scope """
    return tf.get_variable(name)


def get_weight(layer_idx, w_idx=0):
    name = WEIGHT_TAG.format(layer_idx, w_idx)
    return get_var(name)


def get_bias(layer_idx):
    name = BIAS_TAG.format(layer_idx)
    return get_var(name)


def get_scalars():
    scalars = [get_var(SCALAR_TAG.format(i)) for i in range(2)]
    return scalars


# Layer var get wrappers
# ========================================
"""
Assumed to be within the tf.variable_scope of the respective network funcs
  themselves (called directly), so no dispatching layer wrapper get func
"""
def get_vanilla_layer_vars(layer_idx, **kwargs):
    weight = get_weight(layer_idx)
    bias = get_bias(layer_idx)
    return weight, bias


def get_ShiftInv_layer_vars(layer_idx, **kwargs):
    weights = []
    for w_idx in SHIFT_INV_W_IDX:
        weights.append(get_weight(layer_idx, w_idx=w_idx))
    bias = get_bias(layer_idx)
    return weights, bias


def get_RotInv_layer_vars(layer_idx, **kwargs):
    # TODO
    assert False


#=============================================================================
# Model save, load utilities
#=============================================================================
""" Note: all save, load utilities are provided by the functions below, but are
    interfaced to the trainer by TrainSaver
"""
#------------------------------------------------------------------------------
# Standalone save/restore util functions
#------------------------------------------------------------------------------
# Model save utils
# ========================================
def make_dirs(dirs):
    """ Make all directories along paths in dirs """
    for path in dirs:
        if not os.path.exists(path): os.makedirs(path)

def save_files(save_path, files_to_save=FILE_SAVE_NAMES):
    """ Copy project files to directory """
    for f in files_to_save:
        src = './{}'.format(f)
        dst = save_path + f
        shutil.copy(src, dst)

def save_cube(cube, redshifts, save_path, ground_truth=False):
    """ Save data cube """
    rsX, rsY = redshifts
    name = CUBE_NAME_TRUTH if ground_truth else CUBE_NAME_PRED
    name = name.format(rsX, rsY)
    np.save(result_save_path + name, cube)
    print('Saved cube: {}'.format(name))

def save_error(error, save_path, training=False):
    """ Save model error """
    suffix = 'training' if training else 'validation'
    name = 'error_{}'.format(suffix)
    np.save(save_path + name, error)
    print('Saved {}'.format(name))

def save_params(saver, sess, cur_iter, path, write_meta_graph=True):
    """ Save trained model parameters """
    step = cur_iter + 1
    saver.save(sess, path, global_step=step, write_meta_graph=write_meta_graph)

# Model restore utils
# ========================================
def restore_parameters(saver, sess, save_dir):
    """ restore trained model parameters """
    path = tf.train.get_checkpoint_state(save_dir)
    saver.restore(sess, path.model_checkpoint_path)
    print('Restored trained model parameters from {}'.format(save_dir))


#------------------------------------------------------------------------------
# Save/Restore utils interface class
#------------------------------------------------------------------------------
class TrainSaver:
    """ TrainSaver wraps tf.train.Saver() for session,
        and interfaces all essential save/restore utilities
    """
    def __init__(self, mname, num_iters, always_write_meta=False, restore=False):
        # Training vars
        self.saver = tf.train.Saver()
        self.model_name = mname
        self.num_iters = num_iters
        self.always_write_meta = always_write_meta
        self.restore = restore

        # Paths
        self.model_path  = MODEL_SAVE_PATH.format(mname)
        self.result_path = RESULTS_SAVE_PATH.format(mname)
        self.file_path   = FILE_SAVE_PATH.format(mname)
        self.make_model_dirs()

    def make_model_dirs(self):
        paths = [self.model_path, self.result_path, self.file_path]
        make_dirs(paths)

    # ==== Restoring
    def restore_model_parameters(self, sess):
        restore_parameters(self.saver, sess, self.model_path)

    # ==== Saving
    def save_model_files(self):
        path = self.file_path
        save_files(path)

    def save_model_cube(self, cube, rs, save_path=self.result_path, ground_truth=False):
        save_cube(cube, redshifts, save_path, ground_truth)

    def save_model_error(self, error, save_path=self.result_path, training=False):
        save_error(error, save_path, training)

    def save_model_params(self, session, cur_iter):
        is_final_step = step == self.num_iters
        wr_meta = True if is_final_step else self.always_write_meta
        save_model(self.saver, session, cur_iter, self.model_path, wr_meta)



#=============================================================================
# Simulation dataset read/load utilities
#=============================================================================
""" Note: due to long read times and disk space constraints, data is not
    read from binaries.
    Instead, the numpy npy formatted simulation data is what is used regularly
"""
#------------------------------------------------------------------------------
# Dataset (binary) dataset read functions
#------------------------------------------------------------------------------
# Read simulation cubes stored in binary structs
# ========================================
def read_simulation_binaries(file_list, n_P=32):
    """ reads simulation data from binaries and and converts to numpy ndarray
    Args:
        file_list list(str): paths to files
        n_P (int): number of particles base (n_P**3 particles)
            NB: only 32**3 simulation data is used
    Returns: numpy array of data
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

# Interface for binary read func
# ========================================
def load_simulation_cube_binary(redshift, data_path=DATA_PATH_BINARIES):
    """ loads two redshift datasets from proper data directory
    Args:
        redshift (float): redshift value
    """
    glob_paths = glob.glob(data_path.format(redshift))
    X = read_sim(glob_paths).astype(np.float32)
    return X



#------------------------------------------------------------------------------
# Dataset (npy) read functions
#------------------------------------------------------------------------------
# Load simulation cube from npy
# ========================================
def load_simulation_cube_npy(redshift, cat_dim=True):
    """ Loads uniformly timestepped simulation cube stored in npy format
    Note redshift here is true redshift float value
    Args:
        redshift (float): redshift value
        cat_dim (bool): expand a new dim at axis 0 for concatenation
    """
    # Cube dims
    num_cubes = 1000; num_particles = 32**3; num_features = 6;

    # Load cube
    cube_path = DATA_PATH_NPY.format(redshift)
    print('Loading Redshift {:.4f} Cube from: {}'.format(redshift, cube_path[-13:]))
    X = np.load(cube_path).astype(np.float32)
    if cat_dim:
        X = np.expand_dims(X, 0)
    return X


# Load cubes for each redshift
# ========================================
def load_simulation_data(redshift_indices):
    """ Loads uniformly timestep data serialized as np array of np.float32
    Redshift indices are used instead of true float values for ease
    Args:
        redshift_indices list(int): ordered list of indices into REDSHIFTS
    """
    num_rs = len(redshifts) # number of cubes to load

    # Load cubes
    rs_idx = redshift_indices[0]
    redshift = REDSHIFTS[rs_idx]
    X = load_simulation_cube_npy(redshift)
    if num_rs == 1:
        return X
    for rs_idx in redshift_indices[1:]:
        redshift = REDSHIFTS[rs_idx]
        X = np.concatenate([X, load_simulation_cube_npy(redshift)], axis=0)
    return X




#=============================================================================
# Data utils
#=============================================================================

def normalize(X_in, scale_range=(0,1)):
    """ Normalize data features
    coordinates are rescaled to be in range [0,1]
    velocities are normalized to zero mean and unit variance

    Args:
        X_in (ndarray): data to be normalized, of shape (N, D, 6)
        scale_range   : range to which coordinate data is rescaled
    """
    x_r = np.reshape(X_in, [-1,6])
    coo, vel = np.split(x_r, [3], axis=-1)

    coo_min = np.min(coo, axis=0)
    coo_max = np.max(coo, axis=0)
    a,b = scale_range
    x_r[:,:3] = (b-a) * (x_r[:,:3] - coo_min) / (coo_max - coo_min) + a

    vel_mean = np.mean(vel, axis=0)
    vel_std  = np.std( vel, axis=0)
    x_r[:,3:] = (x_r[:,3:] - vel_mean) / vel_std

    X_out = np.reshape(x_r,X_in.shape).astype(np.float32) # just convert to float32 here
    return X_out

def normalize_rescale_vel(X_in, scale_range=(0,1)):
    """ Normalize data features
    coordinates are rescaled to be in range [0,1]
    velocities are normalized to zero mean and unit variance

    Args:
        X_in (ndarray): data to be normalized, of shape (N, D, 6)
        scale_range   : range to which coordinate data is rescaled
    """
    x_r = np.reshape(X_in, [-1,6])
    coo, vel = np.split(x_r, [3], axis=-1)

    coo_min = np.min(coo, axis=0)
    coo_max = np.max(coo, axis=0)
    #a,b = scale_range
    a,b = (0, 1)
    x_r[:,:3] = (b-a) * (x_r[:,:3] - coo_min) / (coo_max - coo_min) + a

    # PREVIOUS velocity normalization
    #vel_mean = np.mean(vel, axis=0)
    #vel_std  = np.std( vel, axis=0)
    #x_r[:,3:] = (x_r[:,3:] - vel_mean) / vel_std

    # RESCALE velocity to be within scale_range
    a,b = scale_range
    #vel_max = np.max(np.max(vel, axis=0), axis=0)
    #vel_min = np.min(np.min(vel, axis=0), axis=0)
    vel_max = np.max(vel)
    vel_min = np.min(vel)
    x_r[:,3:] = (x_r[:,3:] - vel_min) / (vel_max - vel_min)

    X_out = np.reshape(x_r,X_in.shape).astype(np.float32) # just convert to float32 here
    return X_out

def normalize_fullrs(X, scale_range=(0,1)):
    """ Normalize data features, for full data array of redshifts
    coordinates are rescaled to be in range [0,1]
    velocities are normalized to zero mean and unit variance

    Args:
        X_in (ndarray): data to be normalized, of shape (rs, N, D, 6)
        scale_range   : range to which coordinate data is rescaled
    """
    for rs_idx in range(X.shape[0]):
        X[rs_idx] = normalize_rescale_vel(X[rs_idx])
    return X


def split_data_validation(X, Y, num_val_samples=NUM_VAL_SAMPLES, seed=DATASET_SEED):
    """ split dataset into training and validation sets

    Args:
        X, Y (ndarray): data arrays of shape (num_samples, num_particles, 6)
        num_val_samples (int): size of validation set
    Returns: tuple([X_train, X_val], [Y_train, Y_val])
    """
    num_samples = X.shape[0]
    np.random.seed(seed)
    idx_list = np.random.permutation(num_samples)
    X = np.split(X[idx_list], [-num_val_samples])
    Y = np.split(Y[idx_list], [-num_val_samples])
    return X, Y

def split_data_validation_combined(X, num_val_samples=200, seed=DATASET_SEED):
    """ split dataset into training and validation sets

    Args:
        X (ndarray): data arrays of shape (num_rs, num_samples, num_particles, 6)
        num_val_samples (int): size of validation set
    """
    np.random.seed(seed)
    idx_list = np.random.permutation(X.shape[1])
    X = X[:,idx_list]
    X_train = X[:, :-num_val_samples]
    X_val   = X[:, -num_val_samples:]
    return X_train, X_val

def random_augmentation_shift(batch):
    """ Randomly augment data by shifting indices
    and symmetrically relocating particles
    Args:
        batch (ndarray): (num_rs, batch_size, D, 6)
    Returns:
        batch (ndarray): randomly shifted data array
    """
    #batch_size = batch.shape[1]
    rands = np.random.rand(6)
    if batch.ndim > 3: # there is rs dim
        batch_size = batch.shape[1]
        shift = np.random.rand(1,batch_size,1,3)
    else:
        batch_size = batch.shape[0]
        shift = np.random.rand(batch_size,1,3)
    # shape (11, bs, n_P, 6)
    if rands[0] < .5:
        batch = batch[...,[1,0,2,4,3,5]]
    if rands[1] < .5:
        batch = batch[...,[0,2,1,3,5,4]]
    if rands[2] < .5:
        batch = batch[...,[2,1,0,5,4,3]]
    if rands[3] < .5:
        batch[...,0] = 1 - batch[...,0]
        batch[...,3] = -batch[...,3]
    if rands[4] < .5:
        batch[...,1] = 1 - batch[...,1]
        batch[...,4] = -batch[...,4]
    if rands[5] < .5:
        batch[...,2] = 1 - batch[...,2]
        batch[...,5] = -batch[...,5]
    batch_coo = batch[...,:3]
    batch_coo += shift
    gt1 = batch_coo > 1
    batch_coo[gt1] = batch_coo[gt1] - 1
    batch[...,:3] = batch_coo
    return batch


def next_minibatch(X_in, batch_size, data_aug=False):
    """ randomly select samples for training batch

    Args:
        X_in (ndarray): (num_rs, S, N, 6) data input
        batch_size (int): minibatch size
        data_aug: if data_aug, randomly shift input data
    Returns:
        batches (ndarray): randomly selected and shifted data
    """
    index_list = np.random.choice(X_in.shape[1], batch_size)
    batches = np.copy(X_in[:,index_list])
    if data_aug:
        batches = random_augmentation_shift(batches)

    #assert np.all(batches[0,:,:,-1] > batches[1,:,:,-1])
    return batches


def next_zuni_minibatch(X_in, batch_size, data_aug=True):
    """ randomly select samples for training batch

    Args:
        X_in (ndarray): (num_rs, N, D, 6) data input
        batch_size (int): minibatch size
        data_aug: if data_aug, randomly shift input data
    Returns:
        batches (ndarray): randomly selected and shifted data
    """
    index_list = np.random.choice(X_in.shape[1], batch_size)
    batches = X_in[:,index_list]
    if data_aug:
        batches[...,:-1] = random_augmentation_shift(batches[...,:-1])
    return batches
#=============================================================================
# Saving utils
#=============================================================================
def make_dirs(dirs):
    """ Make directories based on paths in dirs
    Args:
        dirs (list): list of paths of dirs to create
    """
    for path in dirs:
        if not os.path.exists(path): os.makedirs(path)

def make_save_dirs(model_dir, model_name):
    """ Make save directories for saving:
        - model hyper parameters
        - loss data
        - cube data
    Args:
        model_dir (str): the root path for saving model
        model_name (str): name for model
    Returns: (model_path, loss_path, cube_path)
    """
    model_path = '{}{}/'.format(model_dir, model_name)
    tf_params_save_path = model_path + 'Session/'
    loss_path  = model_path + 'Loss/'
    cube_path  = model_path + 'Cubes/'
    make_dirs([tf_params_save_path, loss_path, cube_path]) # model_dir lower dir, so automatically created
    #save_pyfiles(model_path)
    return tf_params_save_path, loss_path, cube_path


def save_pyfiles(model_dir):
    """ Save project files to save_path
    For backing up files used for a model
    Args:
        save_path (str): path to save files
    """
    save_path = model_dir + 'original_files/'
    make_dirs([save_path])
    for fname in SAVE_FILE_NAMES:
        src = './{}'.format(fname)
        dst = '{}{}'.format(save_path, fname)
        shutil.copyfile(src, dst)
        print('saved {} to {}'.format(src, dst))

#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
# two get model names
#==============================================================================

def get_model_name(dparams, mtype, vel_coeff, save_prefix):
    """ Consistent model naming format
    Model name examples:
        'GL_32_12-04': GraphModel|WithVelCoeff|32**3 Dataset|redshift 1.2->0.4
        'S_16_04-00': SetModel|16**3 Dataset|redshift 0.4->0.0
    """
    n_P, rs = dparams
    zX = RS_TAGS[rs[0]]
    zY = RS_TAGS[rs[1]]

    model_tag = NBODY_MODELS[mtype]['tag']
    vel_tag = 'L' if vel_coeff is not None else ''

    model_name = '{}{}_{}_{}-{}'.format(model_tag, vel_tag, n_P, zX, zY)
    if save_prefix != '':
        model_name = '{}_{}'.format(save_prefix, model_name)
    return model_name

def get_zuni_model_name(mtype, zX, zY, save_prefix):
    """ Consistent model naming format
    Model name examples:
        'GL_32_12-04': GraphModel|WithVelCoeff|32**3 Dataset|redshift 1.2->0.4
        'S_16_04-00': SetModel|16**3 Dataset|redshift 0.4->0.0
    """
    #n_P, rs = dparams
    #zX = RS_TAGS[rs[0]]
    #zY = RS_TAGS[rs[1]]

    model_tag = NBODY_MODELS[mtype]['tag']
    #vel_tag = 'L' if vel_coeff is not None else ''

    #model_name = '{}{}_{}_{}-{}'.format(model_tag, vel_tag, n_P, zX, zY)
    #model_name = 'ZG_90-00'
    model_name = 'Z{}_{}-{}'.format(model_tag, zX, zY)
    if save_prefix != '':
        model_name = '{}_{}'.format(save_prefix, model_name)
    return model_name

#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
# two save cubes
# save loss likely need to be changed
# relocate plot3d
#==============================================================================

def save_test_cube(x, cube_path, rs, prediction=False):
    #code.interact(local=dict(globals(), **locals())) # DEBUGGING-use
    '''
    if prediction:
        rs_tag = '{}-{}'.format(*rs) # (zX, zY)
        ptag   = 'prediction'
        save_cube(x, cube_path, rs_tag, ptag)
    else:
        for i in range(x.shape[0]): # 2
            rs_tag = '{}'.format(rs[i])
            ptag   = 'data'
            save_cube(x[i], cube_path, rs_tag, ptag)
    '''
    rs_tag = '{}-{}'.format(*rs) # (zX, zY)
    ptag = 'prediction' if prediction else 'true'
    save_cube(x, cube_path, rs_tag, ptag)


def save_cube(x, cube_path, rs_tag, ptag):
    """ Save validation data
    """
    num_particles = 16 if x.shape[-2] == 4096 else 32
    # eg X32_0.6-0.0_val_prediction.npy'
    val_fname = 'X{}_{}_{}'.format(num_particles, rs_tag, ptag)
    save_path = '{}{}'.format(cube_path, val_fname)
    np.save(save_path, x)
    print('saved {}'.format(save_path))

def save_loss(save_path, data, validation=False):
    save_name = '_loss_validation' if validation else '_loss_train'
    np.save(save_path + save_name, data)

def plot_3D_pointcloud(xt, xh, j, pt_size=(.9,.9), colors=('b','r'), fsize=(12,12), xin=None):
    xt_x, xt_y, xt_z = np.split(xt[...,:3], 3, axis=-1)
    xh_x, xh_y, xh_z = np.split(xh[...,:3], 3, axis=-1)

    fig = plt.figure(figsize=fsize)
    ax = fig.gca(projection='3d')
    ax.scatter(xt_x[j], xt_y[j], xt_z[j], s=pt_size[0], c=colors[0])
    ax.scatter(xh_x[j], xh_y[j], xh_z[j], s=pt_size[1], c=colors[1])

    ax.set_xlabel('X')
    ax.set_ylabel('Y')
    ax.set_zlabel('Z')
    return fig
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================

#==============================================================================

#=============================================================================
# State and Misc Utils
#=============================================================================

#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
# get timestep unecessary here
#==============================================================================
def _get_mask(x, bound=0.1):
    xtmp = x[...,:3]
    lower, upper = bound, 1-bound
    mask1 = np.logical_and(xtmp[...,0] < upper, xtmp[...,0] > lower)
    mask2 = np.logical_and(xtmp[...,1] < upper, xtmp[...,1] > lower)
    mask3 = np.logical_and(xtmp[...,2] < upper, xtmp[...,2] > lower)
    mask = mask1 * mask2 * mask3
    mask_nz = np.nonzero(mask)[0]
    return mask_nz

def _mask_data(x_in, x_truth):
    mask = _get_mask(x_in)
    masked_input = x_in[mask]
    masked_truth = x_truth[mask]
    return masked_input, masked_truth

def get_timestep(x_in, x_true):
    """ # calculates timestep from input redshift to target redshift
    """
    x_in_flat   = x_in.reshape([-1, 6])
    x_true_flat = x_true.reshape([-1, 6])

    m_in, m_true = _mask_data(x_in_flat, x_true_flat)
    #diff = x_true[...,:3] - x_in[...,:3]
    diff = m_true[...,:3] - m_in[...,:3]
    timestep = np.linalg.lstsq(m_in[...,3:].ravel()[:,None], diff.ravel())[0]
    return timestep[0]

#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================
#==============================================================================


def print_checkpoint(step, err, sc_err=None):
    text = 'Checkpoint {:>5}--> LOC: {:.8f}'.format(step+1, err)
    if sc_err is not None:
        text = text + ', SCA:: {:.6f}'.format(sc_err)
    print(text)


def print_median_validation_loss(rs, err, sc_err=None):
    zx, zy = rs
    err_median = np.median(err)
    print('\nEvaluation Median Loss:\n{}'.format('='*78))
    print('# LOCATION LOSS:')
    print('  {:>2} --> {:>2}: {:.9f}'.format(zx, zy, err_median))
    if sc_err is not None:
        sc_err_median = np.median(sc_err)
        print('# SCALED LOSS:')
        print('  {:>2} --> {:>2}: {:.9f}'.format(zx, zy, sc_err_median))
