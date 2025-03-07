# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""JAX primitives related to sparse operations.

This is experimental work to explore sparse support in JAX.

The primitives defined here are deliberately low-level: each primitive implements
a common sparse operation (sparse to dense, dense to sparse, sparse matrix/vector
product, sparse matrix/matrix product) for two common sparse representations
(CSR and COO).

These routines have reference implementations defined via XLA scatter/gather
operations that will work on any backend, although they are not particularly
performant. On GPU runtimes built against CUDA 11.0 or newer, each operation is
computed efficiently via cusparse.

Further down are some examples of potential high-level wrappers for sparse objects.
(API should be considered unstable and subject to change).
"""
import operator
from typing import Tuple

import numpy as np

import jax
from jax import core
from jax import tree_util
from jax.interpreters import ad
from jax.interpreters import batching
from jax.interpreters import xla
from jax._src import dtypes
from jax._src.lib import cusparse
from jax._src.lib import xla_bridge
from jax._src.lib import xla_client
from jax.util import safe_zip
import jax.numpy as jnp

xb = xla_bridge
xops = xla_client.ops

#--------------------------------------------------------------------
# utilities
# TODO: possibly make these primitives, targeting cusparse rountines
#       csr2coo/coo2csr/SPDDMM
@jax.jit
def _csr_to_coo(indices, indptr):
  """Given CSR (indices, indptr) return COO (row, col)"""
  return jnp.cumsum(jnp.zeros_like(indices).at[indptr].add(1)) - 1, indices

def _csr_extract(indices, indptr, mat):
  """Extract values of dense matrix mat at given CSR indices."""
  return _coo_extract(*_csr_to_coo(indices, indptr), mat)

def _coo_extract(row, col, mat):
  """Extract values of dense matrix mat at given COO indices."""
  return mat[row, col]

def _is_placeholder(*args):
  return all(type(arg) is object for arg in args) or all(arg is None for arg in args)

def _is_aval(*args):
  return all(isinstance(arg, core.AbstractValue) for arg in args)

def _asarray_or_float0(arg):
  if isinstance(arg, np.ndarray) and arg.dtype == dtypes.float0:
    return arg
  return jnp.asarray(arg)

#--------------------------------------------------------------------
# csr_todense

csr_todense_p = core.Primitive('csr_todense')

def csr_todense(data, indices, indptr, *, shape):
  """Convert CSR-format sparse matrix to a dense matrix.

  Args:
    data : array of shape ``(nse,)``.
    indices : array of shape ``(nse,)``
    indptr : array of shape ``(shape[0] + 1,)`` and dtype ``indices.dtype``
    shape : length-2 tuple representing the matrix shape

  Returns:
    mat : array with specified shape and dtype matching ``data``
  """
  return csr_todense_p.bind(data, indices, indptr, shape=shape)

@csr_todense_p.def_impl
def _csr_todense_impl(data, indices, indptr, *, shape):
  return _coo_todense_impl(data, *_csr_to_coo(indices, indptr), shape=shape)

@csr_todense_p.def_abstract_eval
def _csr_todense_abstract_eval(data, indices, indptr, *, shape):
  assert data.ndim == indices.ndim == indptr.ndim == 1
  assert indices.dtype == indptr.dtype
  assert data.shape == indices.shape
  assert indptr.shape[0] == shape[0] + 1
  return core.ShapedArray(shape, data.dtype)

def _csr_todense_gpu_translation_rule(ctx, avals_in, avals_out, data, indices,
                                      indptr, *, shape):
  return [cusparse.csr_todense(ctx.builder, data, indices, indptr, shape=shape)]

def _csr_todense_jvp(data_dot, data, indices, indptr, *, shape):
  return csr_todense(data_dot, indices, indptr, shape=shape)

def _csr_todense_transpose(ct, data, indices, indptr, *, shape):
  # Note: we assume that transpose has the same sparsity pattern.
  # Can we check this?
  assert ad.is_undefined_primal(data)
  if ad.is_undefined_primal(indices) or ad.is_undefined_primal(indptr):
    raise ValueError("Cannot transpose with respect to sparse indices")
  assert ct.shape == shape
  assert indices.aval.dtype == indptr.aval.dtype
  assert ct.dtype == data.aval.dtype
  return _csr_extract(indices, indptr, ct), indices, indptr

ad.defjvp(csr_todense_p, _csr_todense_jvp, None, None)
ad.primitive_transposes[csr_todense_p] = _csr_todense_transpose
xla.register_translation(csr_todense_p, xla.lower_fun(
    _csr_todense_impl, multiple_results=False, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(csr_todense_p, _csr_todense_gpu_translation_rule,
                           platform='gpu')

#--------------------------------------------------------------------
# csr_fromdense

csr_fromdense_p = core.Primitive('csr_fromdense')
csr_fromdense_p.multiple_results = True

def csr_fromdense(mat, *, nse, index_dtype=np.int32):
  """Create CSR-format sparse matrix from a dense matrix.

  Args:
    mat : array to be converted to CSR.
    nse : number of specified entries in ``mat``
    index_dtype : dtype of sparse indices

  Returns:
    data : array of shape ``(nse,)`` and dtype ``mat.dtype``.
    indices : array of shape ``(nse,)`` and dtype ``index_dtype``
    indptr : array of shape ``(mat.shape[0] + 1,)`` and dtype ``index_dtype``
  """
  mat = jnp.asarray(mat)
  nse = core.concrete_or_error(operator.index, nse, "nse argument of csr_fromdense()")
  return csr_fromdense_p.bind(mat, nse=nse, index_dtype=np.dtype(index_dtype))

@csr_fromdense_p.def_impl
def _csr_fromdense_impl(mat, *, nse, index_dtype):
  mat = jnp.asarray(mat)
  assert mat.ndim == 2
  m = mat.shape[0]

  row, col = jnp.nonzero(mat, size=nse)
  data = mat[row, col]

  true_nonzeros = jnp.arange(nse) < (mat != 0).sum()
  data = jnp.where(true_nonzeros, data, 0)
  row = jnp.where(true_nonzeros, row, m)
  indices = col.astype(index_dtype)
  indptr = jnp.zeros(m + 1, dtype=index_dtype).at[1:].set(
      jnp.cumsum(jnp.bincount(row, length=m)))
  return data, indices, indptr

@csr_fromdense_p.def_abstract_eval
def _csr_fromdense_abstract_eval(mat, *, nse, index_dtype):
  data = core.ShapedArray((nse,), mat.dtype)
  indices = core.ShapedArray((nse,), index_dtype)
  indptr = core.ShapedArray((mat.shape[0] + 1,), index_dtype)
  return data, indices, indptr

def _csr_fromdense_gpu_translation_rule(ctx, avals_in, avals_out, mat, *, nse,
                                        index_dtype):
  data, indices, indptr = cusparse.csr_fromdense(
      ctx.builder, mat, nnz=nse, index_dtype=np.dtype(index_dtype))
  return [data, indices, indptr]

def _csr_fromdense_jvp(primals, tangents, *, nse, index_dtype):
  M, = primals
  Mdot, = tangents

  primals_out = csr_fromdense(M, nse=nse, index_dtype=index_dtype)
  data, indices, indptr = primals_out

  if type(Mdot) is ad.Zero:
    data_dot = ad.Zero.from_value(data)
  else:
    data_dot = _csr_extract(indices, indptr, Mdot)

  tangents_out = (data_dot, ad.Zero.from_value(indices), ad.Zero.from_value(indptr))

  return primals_out, tangents_out

def _csr_fromdense_transpose(ct, M, *, nse, index_dtype):
  data, indices, indptr = ct
  assert len(data) == nse
  assert indices.dtype == indptr.dtype == index_dtype
  if isinstance(indices, ad.Zero) or isinstance(indptr, ad.Zero):
    raise ValueError("Cannot transpose with respect to sparse indices")
  assert ad.is_undefined_primal(M)
  return csr_todense(data, indices, indptr, shape=M.aval.shape)

ad.primitive_jvps[csr_fromdense_p] = _csr_fromdense_jvp
ad.primitive_transposes[csr_fromdense_p] = _csr_fromdense_transpose
xla.register_translation(csr_fromdense_p, xla.lower_fun(
    _csr_fromdense_impl, multiple_results=True, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(csr_fromdense_p,
                           _csr_fromdense_gpu_translation_rule,
                           platform='gpu')

#--------------------------------------------------------------------
# csr_matvec

csr_matvec_p = core.Primitive('csr_matvec')

def csr_matvec(data, indices, indptr, v, *, shape, transpose=False):
  """Product of CSR sparse matrix and a dense vector.

  Args:
    data : array of shape ``(nse,)``.
    indices : array of shape ``(nse,)``
    indptr : array of shape ``(shape[0] + 1,)`` and dtype ``indices.dtype``
    v : array of shape ``(shape[0] if transpose else shape[1],)``
      and dtype ``data.dtype``
    shape : length-2 tuple representing the matrix shape
    transpose : boolean specifying whether to transpose the sparse matrix
      before computing.

  Returns:
    y : array of shape ``(shape[1] if transpose else shape[0],)`` representing
      the matrix vector product.
  """
  return csr_matvec_p.bind(data, indices, indptr, v, shape=shape, transpose=transpose)

@csr_matvec_p.def_impl
def _csr_matvec_impl(data, indices, indptr, v, *, shape, transpose):
  return _coo_matvec_impl(data, *_csr_to_coo(indices, indptr), v, shape=shape, transpose=transpose)

@csr_matvec_p.def_abstract_eval
def _csr_matvec_abstract_eval(data, indices, indptr, v, *, shape, transpose):
  assert len(shape) == 2
  assert v.ndim == data.ndim == indices.ndim == indptr.ndim == 1
  assert data.shape == indices.shape
  assert data.dtype == v.dtype
  assert indices.dtype == indptr.dtype
  assert indptr.shape[0] == shape[0] + 1
  out_shape = shape[1] if transpose else shape[0]
  assert v.shape[0] == (shape[0] if transpose else shape[1])
  return core.ShapedArray((out_shape,), data.dtype)

def _csr_matvec_gpu_translation_rule(ctx, avals_in, avals_out, data, indices,
                                     indptr, v, *, shape, transpose):
  return [cusparse.csr_matvec(ctx.builder, data, indices, indptr, v,
                              shape=shape, transpose=transpose)]

def _csr_matvec_jvp_mat(data_dot, data, indices, indptr, v, *, shape, transpose):
  return csr_matvec(data_dot, indices, indptr, v, shape=shape, transpose=transpose)

def _csr_matvec_jvp_vec(v_dot, data, indices, indptr, v, *, shape, transpose):
  return csr_matvec(data, indices, indptr, v_dot, shape=shape, transpose=transpose)

def _csr_matvec_transpose(ct, data, indices, indptr, v, *, shape, transpose):
  assert not ad.is_undefined_primal(indices)
  assert not ad.is_undefined_primal(indptr)

  if ad.is_undefined_primal(v):
    return data, indices, indptr, csr_matvec(data, indices, indptr, ct, shape=shape, transpose=not transpose)
  else:
    v = jnp.asarray(v)
    # The following lines do this, but more efficiently.
    # return _csr_extract(indices, indptr, jnp.outer(ct, v)), indices, indptr, v
    row, col = _csr_to_coo(indices, indptr)
    return ct[row] * v[col], indices, indptr, v

ad.defjvp(csr_matvec_p, _csr_matvec_jvp_mat, None, None, _csr_matvec_jvp_vec)
ad.primitive_transposes[csr_matvec_p] = _csr_matvec_transpose
xla.register_translation(csr_matvec_p, xla.lower_fun(
    _csr_matvec_impl, multiple_results=False, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(csr_matvec_p, _csr_matvec_gpu_translation_rule,
                           platform='gpu')


#--------------------------------------------------------------------
# csr_matmat

csr_matmat_p = core.Primitive('csr_matmat')

def csr_matmat(data, indices, indptr, B, *, shape, transpose=False):
  """Product of CSR sparse matrix and a dense matrix.

  Args:
    data : array of shape ``(nse,)``.
    indices : array of shape ``(nse,)``
    indptr : array of shape ``(shape[0] + 1,)`` and dtype ``indices.dtype``
    B : array of shape ``(shape[0] if transpose else shape[1], cols)`` and
      dtype ``data.dtype``
    shape : length-2 tuple representing the matrix shape
    transpose : boolean specifying whether to transpose the sparse matrix
      before computing.

  Returns:
    C : array of shape ``(shape[1] if transpose else shape[0], cols)``
      representing the matrix-matrix product product.
  """
  return csr_matmat_p.bind(data, indices, indptr, B, shape=shape, transpose=transpose)

@csr_matmat_p.def_impl
def _csr_matmat_impl(data, indices, indptr, B, *, shape, transpose):
  return _coo_matmat_impl(data, *_csr_to_coo(indices, indptr), B, shape=shape, transpose=transpose)

@csr_matmat_p.def_abstract_eval
def _csr_matmat_abstract_eval(data, indices, indptr, B, *, shape, transpose):
  assert len(shape) == 2
  assert data.ndim == indices.ndim == indptr.ndim == 1
  assert B.ndim == 2
  assert data.shape == indices.shape
  assert data.dtype == B.dtype
  assert indices.dtype == indptr.dtype
  assert indptr.shape[0] == shape[0] + 1
  out_shape = shape[1] if transpose else shape[0]
  assert B.shape[0] == (shape[0] if transpose else shape[1])
  return core.ShapedArray((out_shape, B.shape[1]), data.dtype)

def _csr_matmat_gpu_translation_rule(ctx, avals_in, avals_out, data, indices,
                                     indptr, B, *, shape, transpose):
  return [cusparse.csr_matmat(ctx.builder, data, indices, indptr, B,
                              shape=shape, transpose=transpose)]

def _csr_matmat_jvp_left(data_dot, data, indices, indptr, B, *, shape, transpose):
  return csr_matmat(data_dot, indices, indptr, B, shape=shape, transpose=transpose)

def _csr_matmat_jvp_right(B_dot, data, indices, indptr, B, *, shape, transpose):
  return csr_matmat(data, indices, indptr, B_dot, shape=shape, transpose=transpose)

def _csr_matmat_transpose(ct, data, indices, indptr, B, *, shape, transpose):
  assert not ad.is_undefined_primal(indices)
  assert not ad.is_undefined_primal(indptr)

  if ad.is_undefined_primal(B):
    return data, indices, indptr, csr_matmat(data, indices, indptr, ct, shape=shape, transpose=not transpose)
  else:
    B = jnp.asarray(B)
    row, col = _csr_to_coo(indices, indptr)
    return (ct[row] * B[col]).sum(1), indices, indptr, B

ad.defjvp(csr_matmat_p, _csr_matmat_jvp_left, None, None, _csr_matmat_jvp_right)
ad.primitive_transposes[csr_matmat_p] = _csr_matmat_transpose
xla.register_translation(csr_matmat_p, xla.lower_fun(
    _csr_matmat_impl, multiple_results=False, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(csr_matmat_p, _csr_matmat_gpu_translation_rule,
                           platform='gpu')


#--------------------------------------------------------------------
# coo_todense

coo_todense_p = core.Primitive('coo_todense')

def coo_todense(data, row, col, *, shape):
  """Convert CSR-format sparse matrix to a dense matrix.

  Args:
    data : array of shape ``(nse,)``.
    row : array of shape ``(nse,)``
    col : array of shape ``(nse,)`` and dtype ``row.dtype``
    shape : length-2 tuple representing the matrix shape

  Returns:
    mat : array with specified shape and dtype matching ``data``
  """
  return coo_todense_p.bind(data, row, col, shape=shape)

@coo_todense_p.def_impl
def _coo_todense_impl(data, row, col, *, shape):
  return jnp.zeros(shape, data.dtype).at[row, col].add(data)

@coo_todense_p.def_abstract_eval
def _coo_todense_abstract_eval(data, row, col, *, shape):
  return core.ShapedArray(shape, data.dtype)

def _coo_todense_gpu_translation_rule(ctx, avals_in, avals_out, data, row, col,
                                      *, shape):
  return [cusparse.coo_todense(ctx.builder, data, row, col, shape=shape)]

def _coo_todense_jvp(data_dot, data, row, col, *, shape):
  return coo_todense(data_dot, row, col, shape=shape)

def _coo_todense_transpose(ct, data, row, col, *, shape):
  # Note: we assume that transpose has the same sparsity pattern.
  # Can we check this?
  assert ad.is_undefined_primal(data)
  if ad.is_undefined_primal(row) or ad.is_undefined_primal(col):
    raise ValueError("Cannot transpose with respect to sparse indices")
  assert ct.shape == shape
  assert row.aval.dtype == col.aval.dtype
  assert ct.dtype == data.aval.dtype
  return _coo_extract(row, col, ct), row, col

ad.defjvp(coo_todense_p, _coo_todense_jvp, None, None)
ad.primitive_transposes[coo_todense_p] = _coo_todense_transpose
xla.register_translation(coo_todense_p, xla.lower_fun(
    _coo_todense_impl, multiple_results=False, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(coo_todense_p, _coo_todense_gpu_translation_rule,
                           platform='gpu')

#--------------------------------------------------------------------
# coo_fromdense

coo_fromdense_p = core.Primitive('coo_fromdense')
coo_fromdense_p.multiple_results = True

def coo_fromdense(mat, *, nse, index_dtype=jnp.int32):
  """Create COO-format sparse matrix from a dense matrix.

  Args:
    mat : array to be converted to COO.
    nse : number of specified entries in ``mat``
    index_dtype : dtype of sparse indices

  Returns:
    data : array of shape ``(nse,)`` and dtype ``mat.dtype``
    row : array of shape ``(nse,)`` and dtype ``index_dtype``
    col : array of shape ``(nse,)`` and dtype ``index_dtype``
  """
  mat = jnp.asarray(mat)
  nse = core.concrete_or_error(operator.index, nse, "nse argument of coo_fromdense()")
  return coo_fromdense_p.bind(mat, nse=nse, index_dtype=index_dtype)

@coo_fromdense_p.def_impl
def _coo_fromdense_impl(mat, *, nse, index_dtype):
  mat = jnp.asarray(mat)
  assert mat.ndim == 2

  row, col = jnp.nonzero(mat, size=nse)
  data = mat[row, col]

  true_nonzeros = jnp.arange(nse) < (mat != 0).sum()
  data = jnp.where(true_nonzeros, data, 0)

  return data, row.astype(index_dtype), col.astype(index_dtype)

@coo_fromdense_p.def_abstract_eval
def _coo_fromdense_abstract_eval(mat, *, nse, index_dtype):
  data = core.ShapedArray((nse,), mat.dtype)
  row = col = core.ShapedArray((nse,), index_dtype)
  return data, row, col

def _coo_fromdense_gpu_translation_rule(ctx, avals_in, avals_out, mat, *, nse,
                                        index_dtype):
  data, row, col = cusparse.coo_fromdense(
      ctx.builder, mat, nnz=nse, index_dtype=np.dtype(index_dtype))
  return [data, row, col]

def _coo_fromdense_jvp(primals, tangents, *, nse, index_dtype):
  M, = primals
  Mdot, = tangents

  primals_out = coo_fromdense(M, nse=nse, index_dtype=index_dtype)
  data, row, col = primals_out

  if type(Mdot) is ad.Zero:
    data_dot = ad.Zero.from_value(data)
  else:
    data_dot = _coo_extract(row, col, Mdot)

  tangents_out = (data_dot, ad.Zero.from_value(row), ad.Zero.from_value(col))

  return primals_out, tangents_out

def _coo_fromdense_transpose(ct, M, *, nse, index_dtype):
  data, row, col = ct
  assert len(data) == nse
  assert row.dtype == col.dtype == index_dtype
  if isinstance(row, ad.Zero) or isinstance(col, ad.Zero):
    raise ValueError("Cannot transpose with respect to sparse indices")
  assert ad.is_undefined_primal(M)
  return coo_todense(data, row, col, shape=M.aval.shape)

ad.primitive_jvps[coo_fromdense_p] = _coo_fromdense_jvp
ad.primitive_transposes[coo_fromdense_p] = _coo_fromdense_transpose

xla.register_translation(coo_fromdense_p, xla.lower_fun(
    _coo_fromdense_impl, multiple_results=True, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(coo_fromdense_p,
                           _coo_fromdense_gpu_translation_rule,
                           platform='gpu')

#--------------------------------------------------------------------
# coo_matvec

coo_matvec_p = core.Primitive('coo_matvec')

def coo_matvec(data, row, col, v, *, shape, transpose=False):
  """Product of COO sparse matrix and a dense vector.

  Args:
    data : array of shape ``(nse,)``.
    row : array of shape ``(nse,)``
    col : array of shape ``(nse,)`` and dtype ``row.dtype``
    v : array of shape ``(shape[0] if transpose else shape[1],)`` and
      dtype ``data.dtype``
    shape : length-2 tuple representing the matrix shape
    transpose : boolean specifying whether to transpose the sparse matrix
      before computing.

  Returns:
    y : array of shape ``(shape[1] if transpose else shape[0],)`` representing
      the matrix vector product.
  """
  return coo_matvec_p.bind(data, row, col, v, shape=shape, transpose=transpose)

@coo_matvec_p.def_impl
def _coo_matvec_impl(data, row, col, v, *, shape, transpose):
  v = jnp.asarray(v)
  if transpose:
    row, col = col, row
  out_shape = shape[1] if transpose else shape[0]
  dv = data * v[col]
  return jnp.zeros(out_shape, dv.dtype).at[row].add(dv)

@coo_matvec_p.def_abstract_eval
def _coo_matvec_abstract_eval(data, row, col, v, *, shape, transpose):
  assert data.shape == row.shape == col.shape
  assert data.dtype == v.dtype
  assert row.dtype == col.dtype
  assert len(shape) == 2
  assert v.ndim == 1
  assert v.shape[0] == (shape[0] if transpose else shape[1])
  out_shape = shape[1] if transpose else shape[0]
  return core.ShapedArray((out_shape,), data.dtype)

def _coo_matvec_gpu_translation_rule(ctx, avals_in, avals_out, data, row, col,
                                     v, *, shape, transpose):
  return [cusparse.coo_matvec(ctx.builder, data, row, col, v, shape=shape,
                              transpose=transpose)]

def _coo_matvec_jvp_mat(data_dot, data, row, col, v, *, shape, transpose):
  return coo_matvec(data_dot, row, col, v, shape=shape, transpose=transpose)

def _coo_matvec_jvp_vec(v_dot, data, row, col, v, *, shape, transpose):
  return coo_matvec(data, row, col, v_dot, shape=shape, transpose=transpose)

def _coo_matvec_transpose(ct, data, row, col, v, *, shape, transpose):
  assert not ad.is_undefined_primal(row)
  assert not ad.is_undefined_primal(col)

  if ad.is_undefined_primal(v):
    return data, row, col, coo_matvec(data, row, col, ct, shape=shape, transpose=not transpose)
  else:
    v = jnp.asarray(v)
    # The following line does this, but more efficiently:
    # return _coo_extract(row, col, jnp.outer(ct, v)), row, col, v
    return ct[row] * v[col], row, col, v

ad.defjvp(coo_matvec_p, _coo_matvec_jvp_mat, None, None, _coo_matvec_jvp_vec)
ad.primitive_transposes[coo_matvec_p] = _coo_matvec_transpose
xla.register_translation(coo_matvec_p, xla.lower_fun(
    _coo_matvec_impl, multiple_results=False, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(coo_matvec_p, _coo_matvec_gpu_translation_rule,
                           platform='gpu')

#--------------------------------------------------------------------
# coo_matmat

coo_matmat_p = core.Primitive('coo_matmat')

def coo_matmat(data, row, col, B, *, shape, transpose=False):
  """Product of COO sparse matrix and a dense matrix.

  Args:
    data : array of shape ``(nse,)``.
    row : array of shape ``(nse,)``
    col : array of shape ``(nse,)`` and dtype ``row.dtype``
    B : array of shape ``(shape[0] if transpose else shape[1], cols)`` and
      dtype ``data.dtype``
    shape : length-2 tuple representing the matrix shape
    transpose : boolean specifying whether to transpose the sparse matrix
      before computing.

  Returns:
    C : array of shape ``(shape[1] if transpose else shape[0], cols)``
      representing the matrix vector product.
  """
  return coo_matmat_p.bind(data, row, col, B, shape=shape, transpose=transpose)

@coo_matmat_p.def_impl
def _coo_matmat_impl(data, row, col, B, *, shape, transpose):
  B = jnp.asarray(B)
  if transpose:
    row, col = col, row
  out_shape = shape[1] if transpose else shape[0]
  dB = data[:, None] * B[col]
  return jnp.zeros((out_shape, B.shape[1]), dB.dtype).at[row].add(dB)

@coo_matmat_p.def_abstract_eval
def _coo_matmat_abstract_eval(data, row, col, B, *, shape, transpose):
  assert data.shape == row.shape == col.shape
  assert data.dtype == B.dtype
  assert B.ndim == 2
  assert len(shape) == 2
  assert B.shape[0] == (shape[0] if transpose else shape[1])
  out_shape = shape[1] if transpose else shape[0]
  return core.ShapedArray((out_shape, B.shape[1]), data.dtype)

def _coo_matmat_gpu_translation_rule(ctx, avals_in, avals_out, data, row, col,
                                     B, *, shape, transpose):
  return [cusparse.coo_matmat(ctx.builder, data, row, col, B, shape=shape,
                              transpose=transpose)]

def _coo_matmat_jvp_left(data_dot, data, row, col, B, *, shape, transpose):
  return coo_matmat(data_dot, row, col, B, shape=shape, transpose=transpose)

def _coo_matmat_jvp_right(B_dot, data, row, col, B, *, shape, transpose):
  return coo_matmat(data, row, col, B_dot, shape=shape, transpose=transpose)

def _coo_matmat_transpose(ct, data, row, col, B, *, shape, transpose):
  assert not ad.is_undefined_primal(row)
  assert not ad.is_undefined_primal(col)
  if ad.is_undefined_primal(B):
    return data, row, col, coo_matmat(data, row, col, ct, shape=shape, transpose=not transpose)
  else:
    B = jnp.asarray(B)
    return (ct[row] * B[col]).sum(1), row, col, B

ad.defjvp(coo_matmat_p, _coo_matmat_jvp_left, None, None, _coo_matmat_jvp_right)
ad.primitive_transposes[coo_matmat_p] = _coo_matmat_transpose
xla.register_translation(coo_matmat_p, xla.lower_fun(
    _coo_matmat_impl, multiple_results=False, new_style=True))
if cusparse and cusparse.is_supported:
  xla.register_translation(coo_matmat_p, _coo_matmat_gpu_translation_rule,
                           platform='gpu')


#----------------------------------------------------------------------
# todense – function to convert sparse matrices to dense while letting
#           dense matrices pass through.
todense_p = core.Primitive('todense')
todense_p.multiple_results = False

def todense(arr):
  """Convert input to a dense matrix. If input is already dense, pass through."""
  bufs, tree = tree_util.tree_flatten(arr)
  return todense_p.bind(*bufs, tree=tree)

@todense_p.def_impl
def _todense_impl(*bufs, tree):
  arr = tree_util.tree_unflatten(tree, bufs)
  if isinstance(arr, (jnp.ndarray, np.ndarray)):
    return arr
  return arr.todense()

@todense_p.def_abstract_eval
def _todense_abstract_eval(*bufs, tree):
  arr = tree_util.tree_unflatten(tree, bufs)
  if isinstance(arr, core.ShapedArray):
    return arr
  return core.ShapedArray(arr.shape, arr.dtype, weak_type=dtypes.is_weakly_typed(arr.data))

def _todense_jvp(primals, tangents, *, tree):
  assert not isinstance(tangents[0], ad.Zero)
  assert all(isinstance(t, ad.Zero) for t in tangents[1:])
  primals_out = todense_p.bind(*primals, tree=tree)
  tangents_out = todense_p.bind(tangents[0], *primals[1:], tree=tree)
  return primals_out, tangents_out

def _todense_transpose(ct, *bufs, tree):
  assert ad.is_undefined_primal(bufs[0])
  assert not any(ad.is_undefined_primal(buf) for buf in bufs[1:])

  standin = object()
  obj = tree_util.tree_unflatten(tree, [standin] * len(bufs))
  from . import BCOO, bcoo_extract
  if obj is standin:
    return (ct,)
  elif isinstance(obj, BCOO):
    _, indices = bufs
    return bcoo_extract(indices, ct), indices
  elif isinstance(obj, COO):
    _, row, col = bufs
    return _coo_extract(row, col, ct), row, col
  else:
    raise NotImplementedError(f"todense_transpose for {type(obj)}")

def _todense_batching_rule(batched_args, batch_dims, *, tree):
  if any(b not in [0, None] for b in batch_dims):
    raise NotImplementedError(f"batch_dims={batch_dims}. Only 0 and None are supported.")
  batched_args = [arg[None, ...] if dim is None else arg
                  for arg, dim in safe_zip(batched_args, batch_dims)]
  return todense_p.bind(*batched_args, tree=tree), 0

ad.primitive_jvps[todense_p] = _todense_jvp
ad.primitive_transposes[todense_p] = _todense_transpose
batching.primitive_batchers[todense_p] = _todense_batching_rule
xla.register_translation(todense_p, xla.lower_fun(
    _todense_impl, multiple_results=False, new_style=True))


#----------------------------------------------------------------------
# Sparse objects (APIs subject to change)
class JAXSparse:
  """Base class for high-level JAX sparse objects."""
  data: jnp.ndarray
  shape: Tuple[int, ...]
  nse: property
  dtype: property

  @property
  def ndim(self):
    return len(self.shape)

  @staticmethod
  def _safe_asarray(args):
    if _is_placeholder(*args) or _is_aval(*args):
      return args
    return map(_asarray_or_float0, args)

  def __init__(self, args, *, shape):
    self.shape = shape

  def __repr__(self):
    name = self.__class__.__name__
    try:
      nse = self.nse
      dtype = self.dtype
      shape = list(self.shape)
    except:
      repr_ = f"{name}(<invalid>)"
    else:
      repr_ = f"{name}({dtype}{shape}, nse={nse})"
    if isinstance(self.data, core.Tracer):
      repr_ = f"{type(self.data).__name__}[{repr_}]"
    return repr_

  def tree_flatten(self):
    raise NotImplementedError("tree_flatten")

  @classmethod
  def tree_unflatten(cls, aux_data, children):
    return cls(children, **aux_data)

  def matvec(self, v):
    raise NotImplementedError("matvec")

  def matmat(self, B):
    raise NotImplementedError("matmat")

  def transpose(self, axes=None):
    raise NotImplementedError()

  @property
  def T(self):
    return self.transpose()

  def __matmul__(self, other):
    if isinstance(other, JAXSparse):
      raise NotImplementedError("matmul between two sparse objects.")
    other = jnp.asarray(other)
    if other.ndim == 1:
      return self.matvec(other)
    elif other.ndim == 2:
      return self.matmat(other)
    else:
      raise NotImplementedError(f"matmul with object of shape {other.shape}")

  def block_until_ready(self):
    for arg in self.tree_flatten()[0]:
      arg.block_until_ready()
    return self


@tree_util.register_pytree_node_class
class CSR(JAXSparse):
  """Experimental CSR matrix implemented in JAX; API subject to change."""
  data: jnp.ndarray
  indices: jnp.ndarray
  indptr: jnp.ndarray
  shape: Tuple[int, int]
  nse = property(lambda self: self.data.size)
  dtype = property(lambda self: self.data.dtype)

  def __init__(self, args, *, shape):
    self.data, self.indices, self.indptr = self._safe_asarray(args)
    super().__init__(args, shape=shape)

  @classmethod
  def fromdense(cls, mat, *, nse=None, index_dtype=np.int32):
    if nse is None:
      nse = (mat != 0).sum()
    return cls(csr_fromdense(mat, nse=nse, index_dtype=index_dtype), shape=mat.shape)

  def todense(self):
    return csr_todense(self.data, self.indices, self.indptr, shape=self.shape)

  def matvec(self, v):
    return csr_matvec(self.data, self.indices, self.indptr, v, shape=self.shape)

  def matmat(self, B):
    return csr_matmat(self.data, self.indices, self.indptr, B, shape=self.shape)

  def transpose(self, axes=None):
    assert axes is None
    return CSC((self.data, self.indices, self.indptr), shape=self.shape[::-1])

  def tree_flatten(self):
    return (self.data, self.indices, self.indptr), {"shape": self.shape}


@tree_util.register_pytree_node_class
class CSC(JAXSparse):
  """Experimental CSC matrix implemented in JAX; API subject to change."""
  data: jnp.ndarray
  indices: jnp.ndarray
  indptr: jnp.ndarray
  shape: Tuple[int, int]
  nse = property(lambda self: self.data.size)
  dtype = property(lambda self: self.data.dtype)

  def __init__(self, args, *, shape):
    self.data, self.indices, self.indptr = self._safe_asarray(args)
    super().__init__(args, shape=shape)

  @classmethod
  def fromdense(cls, mat, *, nse=None, index_dtype=np.int32):
    if nse is None:
      nse = (mat != 0).sum()
    return cls(csr_fromdense(mat.T, nse=nse, index_dtype=index_dtype), shape=mat.shape)

  def todense(self):
    return csr_todense(self.data, self.indices, self.indptr, shape=self.shape[::-1]).T

  def matvec(self, v):
    return csr_matvec(self.data, self.indices, self.indptr, v, shape=self.shape[::-1], transpose=True)

  def matmat(self, B):
    return csr_matmat(self.data, self.indices, self.indptr, B, shape=self.shape[::-1], transpose=True)

  def transpose(self, axes=None):
    assert axes is None
    return CSR((self.data, self.indices, self.indptr), shape=self.shape[::-1])

  def tree_flatten(self):
    return (self.data, self.indices, self.indptr), {"shape": self.shape}


@tree_util.register_pytree_node_class
class COO(JAXSparse):
  """Experimental COO matrix implemented in JAX; API subject to change."""
  data: jnp.ndarray
  row: jnp.ndarray
  col: jnp.ndarray
  shape: Tuple[int, int]
  nse = property(lambda self: self.data.size)
  dtype = property(lambda self: self.data.dtype)

  def __init__(self, args, *, shape):
    self.data, self.row, self.col = self._safe_asarray(args)
    super().__init__(args, shape=shape)

  @classmethod
  def fromdense(cls, mat, *, nse=None, index_dtype=np.int32):
    if nse is None:
      nse = (mat != 0).sum()
    return cls(coo_fromdense(mat, nse=nse, index_dtype=index_dtype), shape=mat.shape)

  def todense(self):
    return coo_todense(self.data, self.row, self.col, shape=self.shape)

  def matvec(self, v):
    return coo_matvec(self.data, self.row, self.col, v, shape=self.shape)

  def matmat(self, B):
    return coo_matmat(self.data, self.row, self.col, B, shape=self.shape)

  def transpose(self, axes=None):
    assert axes is None
    return COO((self.data, self.col, self.row), shape=self.shape[::-1])

  def tree_flatten(self):
    return (self.data, self.row, self.col), {"shape": self.shape}
