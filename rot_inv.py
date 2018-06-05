import os, code, sys, time
from tabulate import tabulate
import numpy as np
from sklearn.neighbors import kneighbors_graph, radius_neighbors_graph
from scipy.sparse import coo_matrix, csr_matrix
import tensorflow as tf

#import utils
#from utils import VAR_SCOPE
#=============================================================================
# Globals
#=============================================================================
# segment labels
SEGNAMES_3D = ['CD', 'RD', 'RC', 'D', 'C', 'R', 'A']

# weight mapping
WMAP_3D = {'CD': 1, # col-depth
           'RD': 2, # row-depth
           'RC': 2, # row-col
           'D' : 3, # depth
           'C' : 3, # col
           'R' : 4, # row
           'A' : 5, # all
           'Z' : 6} # none (no pooling)

#=============================================================================
# ROTATION INVARIANT UTILS (normally in utils.py)
#=============================================================================
#------------------------------------------------------------------------------
# Network params init
#------------------------------------------------------------------------------
def init_RotInv_params(channels, var_scope, vcoeff=False, restore=False, seed=None):
    """ Init parameters for perm-equivariant, rotation-invariant model
    For every layer in this model, there are 6 weights (k, q) and 1 bias (q,)
        row-depth, row-col share weight
        depth, col share weight
    """
    #vinit = 0.002 # best vcoeff constant for 15-19 redshifts on shiftInv model

    # Get (k_in, k_out) tuples from channels
    # ========================================
    kdims = [(channels[i], channels[i+1]) for i in range(len(channels) - 1)]

    # Initialize all layer weights
    # ========================================
    with tf.variable_scope(var_scope):
        for layer_idx, ktup in enumerate(kdims):
            # bias
            utils.init_bias(*ktup, utils.BIAS_TAG.format(layer_idx), restore=restore) # B

            # weights
            for w_idx in set(WMAP_3D.values()): # [1, 2, 3, 4, 5, 6]
                wtag = utils.MULTI_WEIGHT_TAG.format(layer_idx, w_idx)
                utils.init_weight(*ktup, wtag, restore=restore, seed=seed)

        if vcoeff:
            assert False # don't use vcoeff yet
            utils.init_vel_coeff(restore, vinit)

#------------------------------------------------------------------------------
# Var getters (CALLEE ASSUMES with tf.variable_scope)
#------------------------------------------------------------------------------
def get_scoped_RotInv_weight(layer_idx, w_idx):
    W = tf.get_variable(utils.MULTI_WEIGHT_TAG.format(layer_idx, w_idx))
    return W

def get_scoped_bias(layer_idx):
    B = tf.get_variable(utils.BIAS_TAG.format(layer_idx))
    return B

#=============================================================================
# ROTATION INVARIANT NETWORK/LAYER OPS
#=============================================================================
#------------------------------------------------------------------------------
# ROTATION invariant layer ops
#------------------------------------------------------------------------------
def pool_RotInv(X, idx, broadcast=True):
    """
    Args:
        X (tensor): has shape (c, k), row-major order
        idx (numpy array): has shape (c),
            must be row idx of non-zero entries to pool over columns
            must be column idx of non-zero entries to pool over rows
        N (int): number of segments (number of particles in this case)
        b (int): batch size
        broadcast (bool): if True, after pooling re-broadcast to original shape

    Returns:
        tensor of shape (c, k) if broadcast, else (b*N, k)
    """
    num_segs = tf.reduce_max(idx) + 1 # number of segments
    X_pooled = tf.unsorted_segment_mean(X, idx, num_segs)

    if broadcast: # same shape as X
        X_pooled = tf.gather_nd(X_pooled, tf.expand_dims(idx, axis=2))
    else:
        X_pooled = tf.reshape(X_pooled, [tf.shape(X)[0], -1, tf.shape(X)[2]])
    return X_pooled


def RotInv_layer(H_in, segID_3D, bN, layer_id, is_last=False):
    """
    Args:
        H_in (tensor): (b, e, k)
            b = minibatch size
            e = N*(M-1)*(M-2), number of edges in 3D adjacency (no diagonals)
              N = num_particles
              M = num neighbors
            k = input channels
        segID_3D (tensor): (b, 7, e) segment ids for pooling, 7 total:
            [col-depth, row-depth, row-col, depth, col, row, all]
        layer_id (int): layer id in network, for retrieving layer vars
    Returns:
        tensor of shape (b, e, q) if not is_last else (b, N*(M-1), q)
    """
    # Helper funcs
    # ========================================
    def _left_mult(h, w_idx):
        W = get_scoped_RotInv_weight(layer_id, w_idx)
        return tf.einsum("bek,kq->beq", h, W)

    # Forward pass
    # ========================================
    # No pooling
    H = _left_mult(H_in, WMAP_3D['Z'])

    # Pooling ops, ORDER MATTERS
    for i, pool_op in enumerate(SEGNAMES_3D):
        pooled_H = pool_RotInv(H_in, segID_3D[:,i], broadcast=True)
        H = H + _left_mult(pooled_H, WMAP_3D[pool_op])

    # Output
    # ========================================
    H_out = H + get_scoped_bias(layer_id) # (b, e, q)
    if is_last:
        # pool over depth dimension: (b, e, q) --> (b, N*(M-1), q)
        H_out = pool_RotInv(H_out, segID_3D[:,3], broadcast=False)
    return H_out



# Helpers
# ========================================
def _pool(X, idx, broadcast):
    """
    Args:
        X. Shape (b, e, k).
        idx. Shape (b, e).
        broadcast (bool).

    Returns:
        tensor of shape (b, e, k) if broadcast is True
        tensor of shape (b, number of segments, k) if broadcast is False
    """
    n_segments = tf.reduce_max(idx) + 1  # number of segments
    X_pooled = tf.unsorted_segment_mean(X, idx, n_segments)

    if broadcast:
        return tf.gather_nd(X_pooled, tf.expand_dims(idx, axis=2))  # same shape as X

    else:
        return tf.reshape(X_pooled, [tf.shape(X)[0], -1, tf.shape(X)[2]])  # (b, number of segments, k)


# Pre-process adjacency batch
# ========================================
def pre_process_adjacency_batch(batch, m, sparse=True):
    """
    Process batch of adjacency matrices and return segment_idx.

    Args:
        batch. List of adjacency matrices. Each matrix can be dense NxN or any scipy sparse format, like csr.
        m (int). Number of neighbors.
        sparse (bool). If True, matrices in the batch must be in sparse format.

    Returns:
        numpy array with shape (b, 7, e)
            e=N*(M-1)*(M-2), number of edges in 3D adjacency (diagonals removed), N=num of particles, M=num of neighbors
            The seven arrays correpond to segment_idx for pooling over col-depth, row-depth, row-col, depth, col,
            row, all, respectively
    """

    def _combine_segment_idx(idx_1, idx_2):
        """
        Combine pairs of segment idx.
        """
        idx_12 = np.transpose(np.array([idx_1, idx_2]))  # pair up idx
        vals, idx = np.unique(idx_12, axis=0, return_inverse=True)

        return idx  # return idx of unique pairs

    out = []

    for a in batch:

        # Get all segment idx for pooling
        # row, col, depth indices correspond to segment_idx for pooling over col-depth, row-depth, row-col, respectively
        if sparse:
            r_idx, c_idx, d_idx = _make_cube_adjacency_sparse(A_sparse=a, m=m)
        else:
            r_idx, c_idx, d_idx = _make_cube_adjacency_dense(A_dense=a)

        # By combining pairs, will get segment idx for pooling over depth (combine row and col),
        # col (combine row and depth), and row (combine col and depth)
        rc_idx = _combine_segment_idx(r_idx, c_idx)
        rd_idx = _combine_segment_idx(r_idx, d_idx)
        cd_idx = _combine_segment_idx(c_idx, d_idx)

        # Get idx for pooling over all
        all_idx = np.zeros_like(r_idx)

        out.append(np.array([r_idx, c_idx, d_idx, rc_idx, rd_idx, cd_idx, all_idx]))

    out = np.array(out)

    # Offset: note that number of segments is not always N as for 2D case
    for i in range(1, out.shape[0]):
        for j in range(out.shape[1]):
            out[i][j] += np.max(out[i - 1][j]) + 1

    return out


def _make_cube_adjacency_dense(A_dense):
    """
    Build brute-force 3D adjacency from dense NxN input.
    This is just for testing/debugging. Don't use this.
    """
    N = A_dense.shape[0]
    A_cube = np.zeros(shape=[N, N, N], dtype=np.int32)

    for i in range(N):
        for j in range(N):
            if A_dense[i, j] > 0:
                A_cube[i, j, :] = A_dense[i]

    for i in range(N):
        for j in range(N):
            for k in range(N):
                if i == j or i == k or j == k:
                    A_cube[i, j, k] = 0

    return np.nonzero(A_cube)


def _make_cube_adjacency_sparse(A_sparse, m):
    """
    Build 3D adjacency from sparse input.

    Args:
        A_sparse. csr_matrix or any other scipy sparse format.
        m (int). number of neighbors.

    Returns:
        row, col, depth. numpy arrays for indices of non-zero entries. Diagonals removed.
    """
    A_sparse.setdiag(0)  # Don't need diagonal elements
    m_eff = m - 1 # IF NO SELF

    rows, cols = A_sparse.nonzero()

    # Will fill indices for rows, columns, depth, in this order.
    r = []
    c = []
    d = []

    for i in range(len(rows)):
        r.extend([rows[i]] * (m_eff - 1))
        c.extend([cols[i]] * (m_eff - 1))
        cumulative_m = (rows[i] + 1) * m_eff
        depth_idx = cols[cumulative_m - m_eff:cumulative_m]
        depth_idx = np.delete(depth_idx, np.where(depth_idx==cols[i]))  # Remove neighbor-neighbor diagonal
        d.extend(depth_idx)

    return np.array(r), np.array(c), np.array(d)


def get_segment_idx_2D(batch_A_sparse):
    """
    Return row, col, indices of a list of 2D sparse adjacencies with batch indices too.
        Sorry, using a different indexing system from 3D adjacency case. TODO: make indexing consistent for clarity.

    Args:
        batch_A_sparse. List of csr (or any other sparse format) adjacencies.

    Returns:
        array of shape (2, b * N * (M-1), 2). Each pair in the third axis is a batch idx - row idx or
            batch idx - col idx for non-zero entries of 2D adjacency. 0-axis is rows/cols respectively.
    """
    rows = []
    cols = []

    for i in range(len(batch_A_sparse)):
        a = batch_A_sparse[i]
        a.setdiag(0)
        r, c = a.nonzero()
        batch = np.zeros_like(r) + i
        rows.append(np.transpose([batch, r]))
        cols.append(np.transpose([batch, c]))

    rows = np.reshape(np.array(rows), (-1, 2))
    cols = np.reshape(np.array(cols), (-1, 2))

    return np.array([rows, cols])


# Pre-process input
# ========================================
def rot_invariant_input(batch_X, batch_V, batch_A, m):
    """
    Args:
         batch_X. Shape (b, N, 3). Coordinates.
         batch_V. Shape (b, N, 3), Velocties.
         batch_A. List of csr adjacencies.
         m (int). Number of neighbors.

    Returns:
        numpy array of shape (b, e, 10)
            e=N*(M-1)*(M-2), number of edges in 3D adjacency (diagonals removed), N=num of particles, M=num of neighbors
            10 input channels corresponding to 1 edge feature + 9 broadcasted surface features, those are broken
            down into 3 surfaces x (1 scalar distance + 1 row velocity projected onto cols + 1 col velocity
            projected onto rows)
    """
    def _process(X, V, A):
        def _angle(v1, v2):
            return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

        def _norm(v):
            return np.linalg.norm(v)

        def _project(v1, v2):
            return np.dot(v1, v2) / np.linalg.norm(v2)

        rows, cols, depth = _make_cube_adjacency_sparse(A, m)

        X_out = []
        for r, c, d in zip(rows, cols, depth):

            # Relative distance vectors
            dx1 = X[c] - X[r]
            dx2 = X[d] - X[r]
            dx3 = X[d] - X[c]

            # Edge features
            features = [_angle(dx1, dx2)]

            # rc surface features
            # scalar distance + projection of row vel to rc vectors + projection of col vel to cr vectors
            features.extend([_norm(dx1), _project(V[r], dx1), _project(V[c], -dx1)])

            # rd surface features
            # scalar distance + projection of row vel to rd vectors + projection of depth vel to dr vectors
            features.extend([_norm(dx2), _project(V[r], dx2), _project(V[d], -dx2)])

            # cd surface features
            # scalar distance + projection of col vel to cd vectors + projection of depth vel to dc vectors
            features.extend([_norm(dx3), _project(V[c], dx3), _project(V[d], -dx3)])

            X_out.append(features)

        return X_out

    return np.array([_process(batch_X[i], batch_V[i], batch_A[i]) for i in range(len(batch_X))])


# Post-process output
# ========================================
def get_final_position(X_in, segment_idx_2D, weights, m):
    """
    Calculate displacement vectors = linear combination of neighbor relative positions, with weights = last layer
    outputs (pooled over depth), and add diplacements to initial position to get final position.

    Args:
        X_in. Shape (b, N, 3). Initial positions.
        segment_idx_2D . Shape (2, b * N * (M-1), 2). Each pair in the third axis is a batch idx - row idx or
            batch idx - col idx for non-zero entries of 2D adjacency.
            0-axis is rows/cols respectively. Get it from get_segment_idx_2D()
        weights. Shape (b, N, M - 1, 1). Outputs from last layer (pooled over depth dimension).
        m (int). Number of neighbors.

    Returns:
        Tensor of shape (b, N, 3). Final positions.
    """

    # Find relative position of neighbors (neighbor - node)
    dX = tf.gather_nd(X_in, segment_idx_2D[1]) - tf.gather_nd(X_in, segment_idx_2D[0])
    dX_reshaped = tf.reshape(dX, [tf.shape(X_in)[0], tf.shape(X_in)[1], m - 1, tf.shape(X_in)[2]])  # (b, N, M - 1, 3)

    # Return initial position + displacement (=weighted combination of neighbor relative distances)
    return X_in + tf.reduce_sum(tf.multiply(dX_reshaped, weights), axis=2)


# Example
# ========================================
def toy_example():

    # Parameters
    # ============================
    N = 3  # number of particles
    M = 3  # number of neighbors
    b = 2  # minibatch size

    e = N * (M - 1) * (M - 2)  # number of edges in 3D adjacency (diagonal removed)

    # Graph
    # ============================
    # Make up random weights
    # Normally there would be 8 independent weight matrices - but since the two neighbor dimensions are excheangeable
    # row-depth pooling and row-col pooling share the same weight, as well as col pooling and depth pooling.
    # So, there are 6 independent, instead of 8.
    W1 = tf.Variable(tf.constant(1., shape=[10, 1]))
    W2 = tf.Variable(tf.constant(2., shape=[10, 1]))
    W3 = tf.Variable(tf.constant(3., shape=[10, 1]))
    W4 = tf.Variable(tf.constant(4., shape=[10, 1]))
    W5 = tf.Variable(tf.constant(5., shape=[10, 1]))
    W6 = tf.Variable(tf.constant(6., shape=[10, 1]))

    W = {
        "no-pooling": W1,
        "col-depth": W2,
        "row-depth": W3,
        "row-col": W3,
        "depth": W4,
        "col": W4,
        "row": W5,
        "all": W6
    }
    B = tf.Variable(tf.constant(1., shape=[1]))

    # Inputs
    _X_edges = tf.placeholder(tf.float32, [b, e, 10])
    _segment_idx_3D = tf.placeholder(tf.int32, [b, 7, e])
    _X_in = tf.placeholder(tf.float32, [b, N, 3])
    _segment_idx_2D = tf.placeholder(tf.int32, [2, b * N * (M - 1), 2])

    layer_output = rot_inv_layer(
        X_edges=_X_edges,
        segment_idx_3D=_segment_idx_3D,
        W=W,
        B=B,
        activation=tf.nn.relu,
        is_last=True  # This should be set to True only if it's the last layer (depth is pooled)
    )

    weights = tf.reshape(layer_output, [b, N, M - 1, 1])

    final_positions = get_final_position(
        X_in=_X_in,
        segment_idx_2D=_segment_idx_2D,
        weights=weights,
        m=M
    )

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())

        # Pre-processing
        # ============================
        # Batch of adjacencies, b=2, N=3, M=3
        # Number of edges (i.e., non-zero entries in the 3D adjacency) e = N*(M-1)*(M-2) = 6,
        # E has to be constant across the batch.
        # This is a particularly simple case but shows the expected behavior.
        A = np.array([
            [[1, 1, 1], [1, 1, 1], [1, 1, 1]],
            [[1, 1, 1], [1, 1, 1], [1, 1, 1]]
        ])

        # Make adjacencies in sparse format (can be any of the scipy sparse formats)
        A_csr = [csr_matrix(a) for a in A]

        # Get segment idx that correspond to the 7 pooling operations.
        # They're automatically offset along batch.
        segment_idx_3D = pre_process_adjacency_batch(batch=A_csr, sparse=True, m=M)  # (b, 7, e), b=2, e=6

        print(segment_idx_3D[:, 0, :])

        # Print(segment_idx
        # CAN MANUALLY CHECK THAT THE SEGMENT IDX ARE INDEED THE EXPECTED ONES FOR THE CORRESPONDING POOLING OPERATION
        segment_names = ["col-depth", "row-depth", "row-col", "depth", "col", "row", "all"]

        print("First matrix")
        print(tabulate(
            [[name, segment_idx_3D[0][i], np.max(segment_idx_3D[0][i]) + 1] for i, name in enumerate(segment_names)],
            headers=["Pooling", "Segment idx", "Number of Segments"]
        ))

        # In this case poolings are the same - note offset idx.
        print("\nSecond matrix")
        print(tabulate(
            [[name, segment_idx_3D[1][i], np.max(segment_idx_3D[0][i]) + 1] for i, name in enumerate(segment_names)],
            headers=["Pooling", "Segment idx", "Number of Segments"]
        ))

        # Make up random coordinate vectors
        X_in = np.random.rand(2, 3, 3)  # (b, N, 3)

        # Make up random velocities
        V = np.random.rand(2, 3, 3)  # (b, N, 3)

        # Generate input
        # This generate 10 input channels corresponding to 1 edge feature + 9 broadcasted surface features,
        # those are broken down into 3 surfaces x (1 scalar distance + 1 row velocity projected onto cols
        # + 1 col velocity projected onto rows)
        X_edges = rot_invariant_input(batch_X=X_in, batch_V=V, batch_A=A_csr, m=M)  # (b, e, 10), b=2, e=6

        print("-----------")
        print("Input shape: %s" % str(X_edges.shape))

        segment_idx_2D = get_segment_idx_2D(batch_A_sparse=A_csr)

        # Output
        # ============================
        out = sess.run(
            final_positions,
            feed_dict={
                _X_edges: X_edges,
                _segment_idx_3D: segment_idx_3D,
                _X_in: X_in,
                _segment_idx_2D: segment_idx_2D
            }
        )

        print("Output shape: %s" % str(out.shape))


if __name__ == "__main__":
    toy_example()


'''
#############

Hey guys,

sorry for the delay. Attached is my code for the rotational invariant model.
It required some work - but I think it should implement what we discussed.
Here are some remarks, I'd appreciate if you could share your comments.

1. I have included an end-to-end toy example, which shows how the different
parts should be used together, and how poolings (which are the tricky part) work.

2. Inputs for the rot-invariant layers have now shape X = (b, e, k),
where b=batch, k=channels and e is the number of edge features. Edge features
sit on the non-zero entries of a 3D adjacency with shape (N, M, M), which is
symmetric under exchange of the last two dimensions. The number of edge
features is e=N*(M-1)*(M-2). I'm working under the assumption of fixed number
of neighbors M for now, and the code removes the diagonals from the 3D
adjacency (given that we fill edges with the angle between <nm, nm'>, one
should have n!=m and n!=m'; additionally, broadcasting surface features to
edges (see below for details) also requires m != m', so I have removed all
diagonal elements). Note that the subsampling trick Siamak suggested last
time for dealing with variable number of neighbors, required a fixed number
of 2D edges across the batch (i.e., fixed total number of non-zero entries in
the adjacency across the batch) for the shift-invariant case; but now it would
require a fixed number of 3D edges, which is not guaranteed to be constant
even if the 2D edges are fixed. I'm postponing these issues for later
discussion, and sticking with fixed M for now.

3. As you can see, the layer is very simple in principle, all operations
are very symmetric. Here is the main idea: we have a three-dimensional
adjacency tensor, whose non-zero elements (there are e of them) are contained
in X, in row-column-depth order (generalization of row-major order). There are
7 possible pooling + no-pooling, which correspond to 8 sets of weights. Think
about the 2D case first: row indices of non-zero adjacency elements define
segments for pooling over columns, and viceversa column indices define segments
for pooling over rows. For the 3D case we have row, column and depth indices
which indicate the non-zero entries and correspond exactly to pooling over
col-and-depth, row-and-depth, row-and-col, respectively. Additionally, a
proper combination of row and col indices defines segments for pooling over
depth, a combination of row and depth indices defines segments for pooling
over col, and a combination of col and depth indices defines segments for
pooling over row (you can see the code for how the combination is calculated).
Finally, there is a pool over all e edges.

4. Because adjacency is symmetric for exchange of col and depth, I think we
should actually have 6 independent sets of weights, instead of 8. row-and-col
and row-and-depth should share the same weight. Same for col and depth, as I
did in the toy example. Also, I've assumed the batch of adjacency is in csr
(or any other scipy sparse format).

5. There are functions for preprocessing and postprocessing.
Preprocessing: the input data is a tensor of shape (b, e, 10).
There are 10 channels because: 1 true edge feature
(angle between <nm, nm'> + 9 features coming from surface broadcasting.
Assume that the surface is IJ, where I and J can be {row, col, depth},
and they are indexed by ij. There are 3 possible surfaces, and for each
surface there are 1 scalar distance for the pair ij + 1 projection of
velocity_i onto vector_ij + 1 projection of velocity_j onto vector_ji = - vector_ij.
This gives 3 surfaces x 3 features = 9 features coming from surface broadcasting.
I think this is one possible way of encoding the input (possibly redundant?),
there could be others. For example, once we have a triplet of particles
identified by an edge, we could add all 3 relative angles on the edge,
instead of only one angle and two scalar distances on the surface.

6. Postprocessing: last layer of the network (once you set is_last=True) pools
over the depth dimension, and so returns an output X_out of shape (b, N*(M-1), q).
With q=1 this can be reshaped to (b, N, M-1, 1). For each particle, we take a
linear combination of the relative distances of its neighbors with weight given
by X_out, this gives the displacement. Once summed to the initial position, you
get the final position.

7. I have tested separate pieces of the code and some overall functionalities -
but I have not done extensive tests. Some code, especially for the pre or
post processing, can definitely be cleaned up.
'''
