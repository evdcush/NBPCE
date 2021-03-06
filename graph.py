import code, sys
import numpy as np
import tensorflow as tf
from sklearn.neighbors import kneighbors_graph, radius_neighbors_graph
from scipy.sparse import coo_matrix



#██████████████████████████████████████████████████████████████████████████████
#██████████████████████████████████████████████████████████████████████████████


#-----------------------------------------------------------------------------#
#                               15 Op Shift Inv                               #
#-----------------------------------------------------------------------------#


# Updated/Corrected Shiftinv layer
# ===================================
def shift_inv_15op_layer(H_in, adj, bN, layer_vars, is_last=False):
    """

    New basis with 15 independent weights.
      see: https://openreview.net/pdf?id=Syx72jC9tm.

    Let S = sum_i( symmetrized_i ) for i = 0...b-1, where b = batch size.
    symmetrized_i = number of non-zero entries of symmetrized adjacency.
    If adjacency is symmetric, then symmetrized_i = N*M;
    but in general it's not.

    Also, even if all instances in the batch have a fixed number of
    neighbors (i.e. all unsymmetrized adjacencies have N*M non-zero entries),
    the corresponding symmetrized versions can contain different number
    of entries.

    Our implementation where all dimensions but channels are flattened
    is handy to deal with this.

    Args:
        H_in(tensor). Shape = (S, k)
            k is number of input channels.
        adj: dict
            adj["row"]: array, shape = (S)
                Row idx of non-zero entries.
            adj["col"]: array, shape = (S)
                Col idx of non-zero entries.
            adj["all"]: array, shape = (S)
                Idx to pool over the entire adjacency.
            adj["tra"]: array, shape = (S)
                Idx to traspose matrix.
            adj["dia"]: array, shape = (b*N)
                Idx of diagonal elements.
            adj["dal"]: array, shape = (b*N)
                Idx to pool diagonal.
            All entries are properly shifted across the batch.
        b(int). Batch size.
        N(int). Number of particles.
        layer_id (int). Id of layer in network, for retrieving variables.
        is_last (bool). If is_last, pool output over columns.

    Returns:
        H_out (tensor). Shape = (S, q) or (b, N, q) if is_last.
    """
    def _pool(h, pool_idx, num_segs):
        """Pool based on indices.

        Given row idx, it corresponds to pooling over columns, given col idx it corresponds
        to pool over rows, etc...

        Args:
            h (tensor). Shape = (S, k), row-major order.
            pool_idx (tensor). Shape = (S) or (b*N).
            num_segs (int). Number of segments (number of unique indices).
        Return:
            tensor.
        """
        return tf.unsorted_segment_mean(h, pool_idx, num_segs)

    def _broadcast(h, broadcast_idx):
        """Broadcast based on indices.

        Given row idx, it corresponds to broadcast over columns,
        given col idx it corresponds to broadcast over rows, etc...
        Note: in the old implementation _pool and _broadcast were
        done together in pool_ShiftInv_graph_conv.

        Args:
            h (tensor). Pooled data.
            broadcast_idx (tensor). Shape = (S) or (b*N).
        Return:
            tensor.
        """
        return tf.gather_nd(h, tf.expand_dims(broadcast_idx, axis=1))

    def _broadcast_to_diag(h, broadcast_idx, shape):
        """Broadcast values to diagonal.

        Args:
            h(tensor). Values to be broadcasted to a diagonal.
            broadcast_idx(tensor). Diagonal indices, shape = (b*N)
            shape(tensor). The shape of the output, should be (S, q)

        Returns:
            tensor with specified shape
        """
        return tf.scatter_nd(tf.expand_dims(broadcast_idx, axis=1), h, shape)



    # ~~~~~~~~~~~~~~~~~~~~~~~~~
    # FIX:
    # S = sum_i( symmetrized_i ) for i = 0...b-1, where b = batch size.
    # Updated: H_in, adj, bN, layer_id, is_last=False
    #     H_in: (S, k_in)
    # Prev:    H_in, COO_feats, bN, layer_id, is_last=False
        # COO_feats (tensor): (3, c), of row, column, cube-wise indices respectively
        # COO_feats (tensor): (3, c), of row, column, cube-wise indices respectively
    #
    # Get layer vars
    # -------------------------
    #==== Data dims
    b, N = bN # batch_size, num_particles

    #   # split vars
    #==== Weights and Biases, UPDATED
    # W : (15, k_in, k_out)
    # B : (2, k_out)
    W, B = layer_vars

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~
    out_shape = tf.shape(H_in)[0], W[0].shape[-1]
    H_all = []

    #==== 1. No pooling
    H_all.append(tf.matmul(H_in, W[0]))

    #==== 2. Transpose
    H2 = tf.gather(H_in, adj["tra"])
    H_all.append(tf.matmul(H2, W[1]))

    #==== 3. Diagonal
    Hd = tf.gather(H_in, adj["dia"])
    H_all.append(_broadcast_to_diag(tf.matmul(Hd, W[2]), adj["dia"], out_shape))

    #==== 4. Pool rows, broadcast to rows
    Hr = _pool(H_in, adj["col"], b * N)
    H_all.append(_broadcast(tf.matmul(Hr, W[3]), adj["col"]))

    #==== 5. Pool rows, broadcast to cols
    H_all.append(_broadcast(tf.matmul(Hr, W[4]), adj["row"]))

    #==== 6. Pool rows, broadcast to diag
    H_all.append(_broadcast_to_diag(tf.matmul(Hr, W[5]), adj["dia"], out_shape))

    #==== 7. Pool cols, broadcast to cols
    Hc = _pool(H_in, adj["row"], b * N)
    H_all.append(_broadcast(tf.matmul(Hc, W[6]), adj["row"]))

    #==== 8. Pool cols, broadcast to rows
    H_all.append(_broadcast(tf.matmul(Hc, W[7]), adj["col"]))

    #==== 9. Pool cols, broadcast to diag
    H_all.append(_broadcast_to_diag(tf.matmul(Hc, W[8]), adj["dia"], out_shape))

    #==== 10. Pool all, broadcast all
    Ha = _pool(H_in, adj["all"], b)
    H_all.append(_broadcast(tf.matmul(Ha, W[9]), adj["all"]))

    #==== 11. Pool all, broadcast diagonal
    Ha_broad = _broadcast(tf.matmul(Ha, W[10]), adj["dal"])
    H_all.append(_broadcast_to_diag(Ha_broad, adj["dia"], out_shape))

    #==== 12. Pool diagonal, broadcast all
    Hp = _pool(Hd, adj["dal"], b)
    H_all.append(_broadcast(tf.matmul(Hp, W[11]), adj["all"]))

    #==== 13. Pool diagonal, broadcast diagonal
    Hp_broad = _broadcast(tf.matmul(Hp, W[12]), adj["dal"])
    H_all.append(_broadcast_to_diag(Hp_broad, adj["dia"], out_shape))

    #==== 14. Broadcast diagonal to rows
    H_all.append(_broadcast(tf.matmul(Hd, W[13]), adj["col"]))

    #==== 15. Broadcast diagonal to cols
    H_all.append(_broadcast(tf.matmul(Hd, W[14]), adj["row"]))

    # Diagonal and off diagonal bias
    # For simplicity will have a bias applied to all and a
    #  separate one to diagonal only
    #  (which is equivalent to diagonal and off-diagonal)
    B_diag = _broadcast_to_diag(tf.broadcast_to(B[0], (b * N, B[0].shape[0])), adj["dia"], out_shape)
    B_all = B[1]

    # Output
    #------------------------
    H = tf.add_n(H_all) + B_diag + B_all
    if is_last:
        return tf.reshape(_pool(H, adj["row"], b * N), (b, N, -1))
    else:
        return H

def network_func_15op_shift_inv_za(edges, adj, num_layers, dims, activation, sess_mgr):
    # Input layer
    # ========================================
    H = activation(shift_inv_15op_layer(edges, adj, dims, sess_mgr.get_layer_vars(0),))

    # Hidden layers
    # ========================================
    for layer_idx in range(1, num_layers):
        is_last = layer_idx == num_layers - 1
        layer_vars = sess_mgr.get_layer_vars(layer_idx)
        H = shift_inv_15op_layer(H, adj, dims, layer_vars, is_last=is_last)
        if not is_last:
            H = activation(H)
    return H


def model_func_15op_shift_inv_za(edges, adj_map, sess_mgr, dims,
                                 activation=tf.nn.relu):
    var_scope = sess_mgr.var_scope
    num_layers = len(sess_mgr.channels) - 1

    # Network forward
    # ========================================
    with tf.variable_scope(var_scope, reuse=True): # so layers can get variables
        # ==== Network output
        pred_error = network_func_15op_shift_inv_za(edges, adj_map, num_layers,
                                               dims[:-1], activation, sess_mgr)
        return pred_error
        # Consider Skip connects if error is off

#██████████████████████████████████████████████████████████████████████████████
#██████████████████████████████████████████████████████████████████████████████




###############################################################################
###############################################################################

#-----------------------------------------------------------------------------#
#                                 graph model                                 #
#-----------------------------------------------------------------------------#

def include_node_features(X_in_edges, X_in_nodes, COO_feats, redshift=None):
    """ Broadcast node features to edges for input layer
    Params
    ------
    X_in_edges : tensor; (c,3)
        input edges features (relative pos of neighbors)
    X_in_nodes : tensor; (b*N, 3)
        input node features (velocities)
    COO_feats : tensor; (3, c)
        rows, cols, cube indices

    Returns
    -------
    X_in : tensor; (c,9)
        model graph input, with node features broadcasted to edges
    """
    # ==== get row, col indices
    row_idx = COO_feats[0]
    col_idx = COO_feats[1]

    # ==== get node row, columns
    node_rows = tf.gather_nd(X_in_nodes, tf.expand_dims(row_idx, axis=1))
    node_cols = tf.gather_nd(X_in_nodes, tf.expand_dims(col_idx, axis=1))

    # ==== full node, edges graph
    X_in = tf.concat([X_in_edges, node_rows, node_cols], axis=1) # (c, 9)

    # ==== broadcast redshifts
    if redshift is not None:
        X_in = tf.concat([X_in, redshift], axis=1) # (c, 10)
    return X_in

    """
    There will be ONE edge in edges that is zeros because difference between
    self
    - we want to insert ZA_displacements here

    For a particle, there is one neighbor index in adjacency that is equal
    to its index in cube, when include_self=True
      - thus it will be zero if we diff
      - THIS IS WHERE YOU INSERT ZA_displacements
    """


def get_input_features_shift_inv_ZA(init_pos, ZA_displacement, coo, diag, dims):
    """ get edges and nodes with TF ops
    get relative distances of each particle from its M neighbors
    use diff tensor

    Params
    ------
    init_pos : tensor; (b, N, 3)
        initial positions on the grid

    ZA_displacement : tensor; (b, N, 3)
        za displacement vector, X[...,1:4]

    coo : tensor; (3, b*N*M)
        the sparse adjacency matrix, MADE FROM init_pos
        # Diagonal indices notes:
        -
    diag : tensor; (b*N)
        the diagonal indices; the indices where the particles in init_pos are
        neighboring nodes with themself (via `include_self=True)

    Returns
    -------
    edges : tensor; (C, 3)
        NO NODES
    """
    def _broadcast_to_diag(h, broadcast_idx, shape):
        """Broadcast values to diagonal.
        Args:
            h(tensor). Values to be broadcasted to a diagonal.
            broadcast_idx(tensor). Diagonal indices, shape = (b*N)
            shape(tensor). The shape of the output, should be (S, q)
        Returns:
            tensor with specified shape
        """
        return tf.scatter_nd(tf.expand_dims(broadcast_idx, axis=1), h, shape)

    b, N, M = dims
    #==== get edges (neighbors)
    flattened_pos = tf.reshape(init_pos, (-1, 3))
    cols = coo[1]
    #gath_edges = tf.gather(flattened_pos, cols)
    #code.interact(local=dict(globals(), **locals()))
    #edges = tf.reshape(gath_edges, [b, N, M, 3])
    edges = tf.reshape(tf.gather(flattened_pos, cols), [b, N, M, 3])

    #=== weight edges
    edges = edges - tf.expand_dims(init_pos, axis=2) # (b, N, M, 3) - (b, N, 1, 3)

    #=== broadcast ZA_displacements to diagonal
    flattened_za_disp = tf.reshape(ZA_displacement, (-1, 3))
    out_shape = (b*N*M, 3)
    diagonal_za = _broadcast_to_diag(flattened_za_disp, diag, out_shape)
    features_out = tf.reshape(edges, [-1, 3]) + diagonal_za
    return features_out


def get_input_features_shift_inv(X_in, coo, dims):
    """ get edges and nodes with TF ops
    get relative distances of each particle from its M neighbors
    Args:
        X_in (tensor): (b, N, 6), input data
        coo (tensor): (3,b*N*M)
    """
    # ==== split input
    b, N, M = dims
    X = tf.reshape(X_in, (-1, 6))
    edges = X[...,:3] # loc
    nodes = X[...,3:] # vel

    # ==== get edges (neighbors)
    cols = coo[1]
    edges = tf.reshape(tf.gather(edges, cols), [b, N, M, 3])
    # weight edges
    edges = edges - tf.expand_dims(X_in[...,:3], axis=2) # (b, N, M, 3) - (b, N, 1, 3)
    return tf.reshape(edges, [-1, 3]), nodes


def shift_inv_conv(h, pool_idx, num_segs, broadcast):
    """
    Params
    ------
    h : tensor; (c, k)
        data to be avgd, row-major order
    pool_idx : tensor; (c,)
        indices for pooling over segments in data
        - column indices ---> pools rows
        - row indices    ---> pools cols
        - cube indices   ---> rows and cols
    num_segs : int
        number of segments in h (eg, num of particles in h)
    broadcast : bool
        re-broadcast to original shape after pooling

    Returns
    -------
    pooled_conv : tensor
        shape (c, k) if broadcast else (num_segs, k)
    """
    pooled_conv = tf.unsorted_segment_mean(h, pool_idx, num_segs)
    if broadcast:
        pooled_conv = tf.gather_nd(pooled_conv, tf.expand_dims(pool_idx, axis=1))
    return pooled_conv


def shift_inv_layer(H_in, COO_feats, bN, layer_vars, is_last=False):
    """ Shift-invariant network layer
    # pooling relations
    # row : col
    # col : row
    # cubes : cubes
    Args:
        H_in (tensor): (c, k), stores shift-invariant edge features, row-major
          - c = b*N*M, if KNN then M is fixed, and k = num_edges = num_neighbors = M
        COO_feats (tensor): (3, c), of row, column, cube-wise indices respectively
        bN (tuple(int)): (b, N), where b is batch_size, N is number of particles
        layer_id (int): id of layer in network, for retrieving variables
          - each layer has 4 weights W (k, q), and 1 bias B (q,)
        is_last (bool): if is_last, pool output over columns
    Returns:
        H_out (tensor): (c, q), or (b, N, q) if is_last
    """
    # Prepare data and parameters
    # ========================================
    # split inputs
    b, N = bN
    row_idx  = COO_feats[0]
    col_idx  = COO_feats[1]
    cube_idx = COO_feats[2]

    # split vars
    weights, B = layer_vars
    W1, W2, W3, W4 = weights

    # Helper funcs
    # ========================================
    def _pool(H, idx, broadcast=True):
        return shift_inv_conv(H, idx, b*N, broadcast)

    def _left_mult(h, W):
        return tf.einsum('ck,kq->cq', h, W)

    # Layer forward pass
    # ========================================
    # H1 : no pooling
    #code.interact(local=dict(globals(), **locals()))
    # H1.shape = (1835008, 3)
    # W1.shape = (9, 32) <----- input chans off
    H1 = _left_mult(H_in, W1) # (c, q)

    # H2 : pool rows
    H_pooled_rows = _pool(H_in, col_idx)
    H2 = _left_mult(H_pooled_rows, W2) # (c, q)

    # H3 : pool cols
    H_pooled_cols = _pool(H_in, row_idx)
    H3 = _left_mult(H_pooled_cols, W3) # (c, q)

    # H4 : pool cubes
    H_pooled_all = _pool(H_in, cube_idx)
    H4 =  _left_mult(H_pooled_all, W4) # (c, q)

    # Output
    # ========================================
    H_out = (H1 + H2 + H3 + H4) + B
    if is_last:
        H_out = tf.reshape(_pool(H_out, row_idx, broadcast=False), (b, N, -1))
    return H_out


#==============================================================================
# Network ops
#==============================================================================

def network_func_shift_inv_za(edges, coo, num_layers, dims, activation, model_vars):
    # Input layer
    # ========================================
    H = activation(shift_inv_layer(edges, coo, dims, model_vars.get_layer_vars(0),))

    # Hidden layers
    # ========================================
    for layer_idx in range(1, num_layers):
        is_last = layer_idx == num_layers - 1
        layer_vars = model_vars.get_layer_vars(layer_idx)
        H = shift_inv_layer(H, coo, dims, layer_vars, is_last=is_last)
        if not is_last:
            H = activation(H)
    return H


def model_func_shift_inv_za(init_pos, COO_feats, ZA_displacement, ZA_diagonal,
                            model_vars, dims, activation=tf.nn.relu):
    """

    Params
    ------
    init_pos : tensor; (b, N, 3)
        the initial positions of particles on the grid

    coo_feats : tensor; (3, c) -- where c = b * N * M
        segment IDs for rows, cols, all

    ZA_displacement : tensor; (b, N, 3)
        za displacement vector, X[...,1:4]

    ZA_diagonal : tensor; (b*N,)
        diagonal indices of the initial positions

    model_vars : Initializer
        Initializer instance that has model config and variable utils
    """
    var_scope = model_vars.var_scope
    num_layers = len(model_vars.channels) - 1

    # Get graph inputs
    # ========================================
    #edges, nodes = get_input_features_shift_inv(X_in, COO_feats, dims)
    edges = get_input_features_shift_inv_ZA(init_pos, ZA_displacement,
                                            COO_feats, ZA_diagonal, dims)

    # Network forward
    # ========================================
    with tf.variable_scope(var_scope, reuse=True): # so layers can get variables
        # ==== Network output
        pred_error = network_func_shift_inv_za(edges, COO_feats, num_layers,
                                               dims[:-1], activation, model_vars)
        return pred_error

'''
def _network_func_shift_inv(X_in_edges, X_in_nodes, COO_feats, num_layers,
                           dims, activation, model_vars, redshift=None):
    # Input layer
    # ========================================
    H_in = include_node_features(X_in_edges, X_in_nodes, COO_feats, redshift=redshift)
    H = activation(shift_inv_layer(H_in, COO_feats, dims, model_vars.get_layer_vars(0),))

    # Hidden layers
    # ========================================
    for layer_idx in range(1, num_layers):
        is_last = layer_idx == num_layers - 1
        layer_vars = model_vars.get_layer_vars(layer_idx)
        H = shift_inv_layer(H, COO_feats, dims, layer_vars, is_last=is_last)
        if not is_last:
            H = activation(H)
    return H


def _model_func_shift_inv(X_in, COO_feats, model_vars, dims, activation=tf.nn.relu, redshift=None):
    """
    Args:
        X_in (tensor): (b, N, 6)
        COO_feats (tensor): (3, B*N*M), segment ids for rows, cols, all
        redshift (tensor): (b*N*M, 1) redshift broadcasted
    """
    var_scope = model_vars.var_scope
    num_layers = len(model_vars.channels) - 1

    # Get graph inputs
    # ========================================
    edges, nodes = get_input_features_shift_inv(X_in, COO_feats, dims)

    # Network forward
    # ========================================
    with tf.variable_scope(var_scope, reuse=True): # so layers can get variables
        # ==== Split input
        X_in_loc, X_in_vel = X_in[...,:3], X_in[...,3:]
        # ==== Network output
        net_out = network_func_shift_inv(edges, nodes, COO_feats, num_layers,
                                        dims[:-1], activation, model_vars, redshift)
        # ==== Scale network output
        loc_scalar, vel_scalar = model_vars.get_scalars()
        H_out = net_out[...,:3]*loc_scalar + X_in_loc + X_in_vel*vel_scalar

        # ==== Concat velocity predictions
        if net_out.get_shape().as_list()[-1] > 3:
            H_vel = net_out[...,3:]*vel_scalar + X_in_vel
            H_out = tf.concat([H_out, H_vel], axis=-1)
        return H_out
'''


#=============================================================================
# Graph, adjacency functions
#=============================================================================

#------------------------------------------------------------------------------
# Adjacency utils
#------------------------------------------------------------------------------
# Kgraph
# ========================================
def alist_to_indexlist(alist):
    """ Reshapes adjacency list for tensorflow gather_nd func
    alist.shape: (B, N, K)
    ret.shape:   (B*N*K, 2)
    """
    batch_size, N, K = alist.shape
    id1 = np.reshape(np.arange(batch_size),[batch_size,1])
    id1 = np.tile(id1,N*K).flatten()
    out = np.stack([id1,alist.flatten()], axis=1).astype(np.int32)
    return out


# Sparse matrix conversions
# ========================================
def get_indices_from_list_CSR(A, offset=True):
    # Dims
    # ----------------
    b = len(A) # batch size
    N = A[0].shape[0] # (32**3)
    M = A[0].indices.shape[0] // N

    # Get CSR feats (indices)
    # ----------------
    CSR_feats = np.zeros((b*N*M)).astype(np.int32)
    for i in range(b):
        # Offset indices
        idx = A[i].indices + i*N

        # Assign csr feats
        k, q = i*N*M, (i+1)*N*M
        CSR_feats[k:q] = idx
    return CSR_feats

def confirm_CSR_to_COO_index_integrity(A, COO_feats):
    """ CSR.indices compared against COO.cols
    Sanity check to ensure that my indexing algebra is correct
    """
    CSR_feats = get_indices_from_list_CSR(A)
    cols = COO_feats[1]
    assert np.all(CSR_feats == cols)


def to_coo_batch_ZA_diag(A):
    """ Get row and column indices from csr
    DOES NOT LIKE OFFSET IDX, tocoo() method will complain about index being
    greater than matrix size

    Args:
        A (csr): list of csrs of shape (N, N)
    """
    # Dims
    # ----------------
    b = len(A) # batch size
    N = A[0].shape[0] # (32**3)
    M = A[0].indices.shape[0] // N
    dia = []

    # Get COO feats
    # ----------------
    COO_feats = np.zeros((3, b*N*M)).astype(np.int32)
    for i in range(b):
        #coo = A[i].tocoo()
        r, c = A[i].nonzero()

        # Offset coo feats
        row = r + i*N
        col = c + i*N
        cube = np.zeros_like(row) + i

        # Assign coo feats
        k, q = i*N*M, (i+1)*N*M
        COO_feats[0, k:q] = row
        COO_feats[1, k:q] = col
        COO_feats[2, k:q] = cube

        # Make diagonals
        d = np.array(np.where(r == c)[0])
        dia.extend(d + i * len(r))

    #code.interact(local=dict(globals(), **locals()))
    diagonals = np.array(dia)
    # sanity check
    #confirm_CSR_to_COO_index_integrity(A, COO_feats) # checked out
    return COO_feats, diagonals

def to_coo_batch(A):
    """ Get row and column indices from csr
    DOES NOT LIKE OFFSET IDX, tocoo() method will complain about index being
    greater than matrix size

    Args:
        A (csr): list of csrs of shape (N, N)
    """
    # Dims
    # ----------------
    b = len(A) # batch size
    N = A[0].shape[0] # (32**3)
    M = A[0].indices.shape[0] // N

    # Get COO feats
    # ----------------
    COO_feats = np.zeros((3, b*N*M)).astype(np.int32)
    for i in range(b):
        coo = A[i].tocoo()

        # Offset coo feats
        row = coo.row + i*N
        col = coo.col + i*N
        cube = np.zeros_like(row) + i

        # Assign coo feats
        k, q = i*N*M, (i+1)*N*M
        COO_feats[0, k:q] = row
        COO_feats[1, k:q] = col
        COO_feats[2, k:q] = cube

    # sanity check
    #confirm_CSR_to_COO_index_integrity(A, COO_feats) # checked out
    return COO_feats

#------------------------------------------------------------------------------
# Graph func wrappers
#------------------------------------------------------------------------------
# Graph gets
# ========================================
def get_kneighbor_list(X_in, M, offset_idx=False, include_self=True):
    b, N, D = X_in.shape
    lst_csrs = []
    #print('nn.get_kneighbor_list\n M: {}, include_self: {}'.format(M, include_self))
    for i in range(b):
        kgraph = kneighbors_graph(X_in[i,:,:3], M, include_self=include_self).astype(np.float32)
        if offset_idx:
            kgraph.indices = kgraph.indices + (N * i)
        lst_csrs.append(kgraph)
    return lst_csrs


#=============================================================================
# RADIUS graph ops
#=============================================================================

def radius_graph_fn(x, R, include_self=True):
    """ Wrapper for sklearn.Neighbors.radius_neighbors_graph function

    Params
    ------
    x : ndarray.float32; (N, D)
        input data, where x[:,:3] == particle coordinates
    R : float
        neighborhood search radius

    Returns
    -------
    xR_ngraph : scipy.CSR; (N,N)
        sparse matrix representing each particle's neighboring
        particles within radius R
    """
    xR_ngraph = radius_neighbors_graph(x[...,:3], R, include_self=include_self)
    return xR_ngraph.astype(np.float32)

def get_radius_graph_COO(X_in, R):
    """ Normalize radius neighbor graph by number of neighbors

    This function prepares a single sample for direct conversion from
    scipy CSR format to tensorflow's SparseTensor, which is structured
    much like a modified scipy COO matrix.

    The matrix data is divided by the number of neighbors for each respective
    particle for the graph convolution operation in the network layer.
    """
    N = X_in.shape[0]
    # just easier to diff indptr for now
    # get csr
    rad_csr = radius_graph_fn(X_in, R)
    rad_coo = rad_csr.tocoo()

    # diff data for matmul op select
    div_diff = np.diff(rad_csr.indptr)
    coo_data_divisor = np.repeat(div_diff, div_diff).astype(np.float32)
    coo_data = rad_coo.data / coo_data_divisor

    coo = coo_matrix((coo_data, (rad_coo.row, rad_coo.col)), shape=(N, N)).astype(np.float32)
    return coo

def get_radNeighbor_coo_batch(X_in, R):
    b, N = X_in.shape[:2]

    # accumulators
    coo = get_radNeighbor_coo(X_in[0], R)
    rows = coo.row
    cols = coo.col
    data = coo.data

    for i in range(1, b):
        # get coo, offset indices
        coo = get_radNeighbor_coo(X_in[i], R)
        row = coo.row + (N * i)
        col = coo.col + (N * i)
        datum = coo.data

        # concat to what we have
        rows = np.concatenate((rows, row))
        cols = np.concatenate((cols, col))
        data = np.concatenate((data, datum))

    coo = coo_matrix((data, (rows, cols)), shape=(N*b, N*b)).astype(np.float32)
    return coo

def get_radNeighbor_sparseT_attributes(coo):
    idx = np.mat([coo.row, coo.col]).transpose()
    return idx, coo.data, coo.shape

def get_radius_graph_input(X_in, R):
    coo = get_radNeighbor_coo_batch(X_in, R)
    sparse_tensor_attributes = get_radNeighbor_sparseT_attributes(coo)
    return sparse_tensor_attributes



#=============================================================================
# boundary utils
#=============================================================================
def face_outer(particle, bound): # ret shape (1,3)
    # face only has one coordinate in boundary, so only one relocation
    ret = bound + particle
    return ret[None,:]

def edge_outer(particle, bound):
    # edge has two coordinates in boundary, so 3 relocations (edge, face, face)
    zero_idx = list(bound).index(0)
    edge = np.roll(np.array([[0,1,1],[0,1,0],[0,0,1]]), zero_idx, 1)
    return (edge * bound) + particle

def corner_outer(particle, bound): # ret shape (7, 3)
    # corner has 3 coordinates in boundary, so 7 relocations:
    # (corner, edge, edge, edge, face, face, face)
    corner = np.array([[1,1,1],[1,1,0],[1,0,1],[1,0,0],[0,1,1],[0,1,0],[0,0,1]])
    return (corner * bound) + particle

def get_outer(particle, bound, num_boundary):
    assert num_boundary > 0
    if num_boundary == 1:
        return face_outer(particle, bound)
    elif num_boundary == 2:
        return edge_outer(particle, bound)
    else:
        return corner_outer(particle, bound)

def pad_cube_boundaries(x, boundary_threshold):
    """ check all particles for boundary conditions and
    relocate boundary particles
    I wonder if you could just do one corner_outer over the extracted corners
    in x, edge_outer on extracted edges, and so forth, while saving indices?
    Args:
        x (ndarray): data array, shape (n_P, 3)
    Returns: expanded x, index_list
    """
    N, D = x.shape
    idx_list = np.array([], dtype=np.int32)

    # boundary
    lower = boundary_threshold
    upper = 1 - boundary_threshold
    bound_x = np.where(x >= upper, -1, np.where(x <= lower, 1, 0))
    bound_x_count = np.count_nonzero(bound_x, axis=-1)

    # get bound and add to clone
    for idx in range(N):
        num_boundary = bound_x_count[idx]
        if num_boundary > 0:
            # get particles to add to clone
            outer_particles = get_outer(x[idx], bound_x[idx], num_boundary)
            # add indices
            idx_list = np.append(idx_list, [idx] * outer_particles.shape[0])
            # concat to clone
            x = np.concatenate((x, outer_particles), axis=0)
    return x, idx_list

def get_pcube_adjacency_list(x, idx_map, N, K):
    """ get kneighbor graph from padded cube
    x is padded cube of shape (M, 3),
    where M == (N + number of added boundary particles)
    Args:
        x (ndarray): padded cube, of shape (M, 3)
        idx_map (ndarray): shape (M-N,) indices
        N: number of particles in original cube
        K: number of nearest neighbors
    """
    kgraph = kneighbors_graph(x, K, include_self=True)[:N].indices
    kgraph_outer = kgraph >= N
    for k_idx, is_outer in enumerate(kgraph_outer):
        if is_outer:
            #code.interact(local=dict(globals(), **locals())) # DEBUGGING-use
            outer_idx = kgraph[k_idx]
            kgraph[k_idx] = idx_map[outer_idx - N]
    return kgraph.reshape(N,K)


def get_pcube_csr(x, idx_map, N, K, include_self=False):
    """ get kneighbor graph from padded cube
    x is padded cube of shape (M, 3),
    where M == (N + number of added boundary particles)
    Args:
        x (ndarray): padded cube, of shape (M, 3)
        idx_map (ndarray): shape (M-N,) indices
        N: number of particles in original cube
        K: number of nearest neighbors
    """
    kgraph = kneighbors_graph(x, K, include_self=include_self)[:N]
    kgraph_outer_idx = kgraph.indices >= N
    for k_idx, is_outer in enumerate(kgraph_outer_idx):
        if is_outer:
            #code.interact(local=dict(globals(), **locals())) # DEBUGGING-use
            outer_idx = kgraph.indices[k_idx]
            kgraph.indices[k_idx] = idx_map[outer_idx - N]
    return kgraph

def get_pbc_kneighbors_csr(X, K, boundary_threshold, include_self=False):
    """
    """
    # get boundary range
    lower = boundary_threshold
    upper = 1 - boundary_threshold
    mb_size, N, D = X.shape

    # graph init
    #adjacency_list = np.zeros((mb_size, N, K), dtype=np.int32)
    csr_list = []
    clone = np.copy(X[...,:3])

    for b in range(mb_size):
        # get expanded cube
        clone_cube = clone[b]
        padded_cube, idx_map = pad_cube_boundaries(clone_cube, boundary_threshold)

        # get neighbors from padded_cube
        kgraph = get_pcube_csr(padded_cube, idx_map, N, K, include_self)
        csr_list.append(kgraph)
    return csr_list
