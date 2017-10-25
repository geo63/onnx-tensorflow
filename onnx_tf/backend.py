"""Backend for running ONNX on Tensorflow

To run this, you will need to have Tensorflow installed as well.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections
import re
import warnings

import numpy as np
from onnx import checker
from onnx.onnx_pb2 import GraphProto, TensorProto, AttributeProto
import onnx.numpy_helper
import onnx.defs
from onnx.backend.base import (
    Backend,
    BackendRep,
    Device,
    DeviceType,
    namedtupledict,
)

from onnx import onnx_pb2, helper
import tensorflow as tf

# TODO: allow more flexible placement
def get_device_option(device):
  m = {DeviceType.CPU: '/cpu',
       DeviceType.CUDA: '/gpu'}
  return m[device.type]

# TODO: Move this into ONNX main library
def convertAttributeProto(onnx_arg):
  """
  Convert an ONNX AttributeProto into an appropriate Python object
  for the type.
  NB: Tensor attribute gets returned as the straight proto.
  """
  if onnx_arg.HasField('f'):
    return onnx_arg.f
  elif onnx_arg.HasField('i'):
    return onnx_arg.i
  elif onnx_arg.HasField('s'):
    return onnx_arg.s
  elif onnx_arg.HasField('t'):
    return onnx_arg.t  # this is a proto!
  elif onnx_arg.floats:
    return list(onnx_arg.floats)
  elif onnx_arg.ints:
    return list(onnx_arg.ints)
  elif onnx_arg.strings:
    return list(onnx_arg.strings)
  else:
    raise ValueError("Unsupported ONNX attribute: {}".format(onnx_arg))

class OnnxAttributes(dict):
  """
  This is a more convenient way to work with ONNX/Caffe2 attributes
  that is not the protobuf representation.
  """
  @staticmethod
  def from_onnx(args):
    d = OnnxAttributes()
    for arg in args:
      d[arg.name] = convertAttributeProto(arg)
    return d

  def caffe2(self, kmap=lambda x: x):
    for k, v in self.items():
      yield caffe2.python.utils.MakeArgument(kmap(k), v)

# TODO: Move this into ONNX main library
class OnnxNode(object):
  """
  Reimplementation of NodeProto from ONNX, but in a form
  more convenient to work with from Python.
  We may temporarily edit these nodes to get them into Caffe2 form,
  before actually translating into the Caffe2 protobuf, since this
  is easier than decomposing everything, and putting it back together
  when we're ready.
  """
  def __init__(self, node):
    self.name = str(node.name)
    self.op_type = str(node.op_type)
    self.attrs = OnnxAttributes.from_onnx(node.attribute)
    self.consumed_inputs = self.attrs.pop("consumed_inputs", None)
    self.inputs = list(node.input)
    self.outputs = list(node.output)

class TensorflowBackend(Backend):
  """ Tensorflow Backend for ONNX
  """

  onnx_tf_attribute_map = {
      "scale": "stddev",
      "high": "maxval",
      "low": "minval",
      "axes": "axis",
      "keepdims": "keep_dims",
      "axis": "dim",
  }

  onnx_tf_per_op_attr_map = {}

  onnx_tf_op_map = {
      "relu": tf.nn.relu,
      "pow": tf.pow,
      "random_normal": tf.random_normal,
      "random_uniform": tf.random_uniform,
      "reciprocal": tf.reciprocal,
      "reduce_log_sum_exp": tf.reduce_logsumexp,
      "reduce_max": tf.reduce_max,
      "reduce_mean": tf.reduce_mean,
      "reduce_min": tf.reduce_min,
      "reduce_prod": tf.reduce_prod,
      "reduce_sum": tf.reduce_sum,
      "sigmoid": tf.sigmoid,
      # default parameter
      "softmax": tf.nn.softmax,
      "sqrt": tf.sqrt,
      "squeeze": tf.squeeze,
      "tanh": tf.tanh,
      "transpose": tf.transpose,
  }

  tensor_type_to_tf_type = {
      TensorProto.FLOAT: tf.float32,
      TensorProto.UINT8: tf.uint8,
      TensorProto.INT8: tf.int8,
      TensorProto.UINT16: tf.uint16,
      TensorProto.INT16: tf.int16,
      TensorProto.INT32: tf.int32,
      TensorProto.INT64: tf.int64,
      TensorProto.BOOL: tf.bool,
      TensorProto.FLOAT16: tf.float16,
      TensorProto.DOUBLE: tf.float64,
      TensorProto.COMPLEX64: tf.complex64,
      TensorProto.COMPLEX128: tf.complex128,
      # TODO: uncomment this in the future
      # TensorProto.UINT32: tf.uint32,
      # TensorProto.UINT64: tf.uint64,
  }

  attr_translator = {
      "dtype": lambda cls, x: cls.tensor_type_to_tf_type[x],
      "keepdims": lambda cls, x: bool(x),
  }

  @classmethod
  def run_node(cls, node, inputs, device='CPU'):
    super(TensorflowBackend, cls).run_node(node, inputs, device)
    node = OnnxNode(node)
    device_option = get_device_option(Device(device))
    input_tensors = []
    for i in inputs:
      input_tensors.append(tf.constant(i))

    if isinstance(inputs, dict):
      feed_dict_raw = inputs
    else:
      assert len(node.inputs) == len(inputs)
      feed_dict_raw = dict(zip(node.inputs, inputs))
    # TODO: is constant the best way for feeding inputs?
    input_dict = dict([(x[0], tf.constant(x[1])) for x in \
                       feed_dict_raw.items()])
    ops = cls._onnx_node_to_tensorflow_op(node, input_dict)
    output_vals = []
    with tf.Session() as sess:
      with tf.device(device_option):
        output_vals = [sess.run(op) for op in ops]
    return namedtupledict('Outputs', node.outputs)(*output_vals)

  @classmethod
  def op_name_to_lower(cls, name):
    return re.sub('(?<!^)(?=[A-Z])', '_', name).lower()

  @classmethod
  def _onnx_node_to_tensorflow_op(cls, node, input_dict):
    op_name_lowered = cls.op_name_to_lower(node.op_type)
    if op_name_lowered in cls.onnx_tf_op_map.keys():
      return cls.handle_trivial(node, input_dict)

    handler_name = "handle_" + op_name_lowered
    # Check if specialized handler exists.
    if handler_name in dir(cls):
      method_to_call = getattr(cls, handler_name)
      return method_to_call(node, input_dict)

  @classmethod
  def handle_trivial(cls, node, input_dict):
    # Perform automatic attribute value translation.
    attrs = dict([(x, cls.attr_translator[x](cls, node.attrs[x]) \
      if x in cls.attr_translator else node.attrs[x]) \
      for x in node.attrs.keys()])

    # Create an identity map from onnx attribute names to tf
    # attribute names.
    attr_map = dict([(x, x) for x in node.attrs.keys()])

    # Modify the map accoridng to onnx_tf_attribute_map.
    attr_map = dict([(x, cls.onnx_tf_attribute_map[x] \
      if x in cls.onnx_tf_attribute_map.keys() else x) \
      for x in attr_map.keys()])

    # TODO: Per op attribute name mapping has the final say.

    # Substitute attribute names in attrs.
    attrs = dict([(attr_map[x], y) for (x, y) in attrs.items()])
    inputs = [input_dict[name] for name in node.inputs]
    return [cls.onnx_tf_op_map[cls.op_name_to_lower(node.op_type)] \
      (*inputs, **attrs)]

  @classmethod
  def handle_p_relu(cls, node, input_dict):
    """
    Reference implementation at
    https://github.com/tflearn/tflearn/blob/4ba8c8d78bf1bbdfc595bf547bad30580cb4c20b/tflearn/activations.py#L191
    """
    x = input_dict[node.inputs[0]]
    slope = input_dict[node.inputs[1]]
    pos = tf.nn.relu(x)
    neg = slope * (x - abs(x)) * 0.5
    return [pos + neg]

  @classmethod
  def handle_pad(cls, node, input_dict):
    mode = node.attrs["mode"]
    value = node.attrs["value"]
    num_dim = int(len(node.attrs["paddings"])/2)
    padding = tf.constant(np.array(node.attrs["paddings"])
                          .reshape([num_dim, 2])
                          .astype(np.int32)) # tf requires int32 paddings
    return [tf.pad(input_dict[node.inputs[0]], padding, mode, None, value)]

  @classmethod
  def handle_random_normal_like(cls, node, input_dict):
    shape = tf.shape(input_dict[node.inputs[0]])
    mean = node.attrs["mean"]
    stddev = node.attrs["scale"]
    dtype = cls.tensor_type_to_tf_type[node.attrs["dtype"]]
    seed = node.attrs["seed"] if "seed" in node.attrs.keys() else None
    return [tf.random_normal(shape, mean, stddev, dtype, seed)]

  @classmethod
  def handle_random_uniform_like(cls, node, input_dict):
    shape = tf.shape(input_dict[node.inputs[0]])
    minval = node.attrs["low"]
    maxval = node.attrs["high"]
    dtype = cls.tensor_type_to_tf_type[node.attrs["dtype"]]
    seed = node.attrs["seed"] if "seed" in node.attrs.keys() else None
    return [tf.random_uniform(shape, minval, maxval, dtype, seed)]

  @classmethod
  def handle_reshape(cls, node, input_dict):
    tensor = input_dict[node.inputs[0]]
    shape = tf.constant(node.attrs["shape"])
    return [tf.reshape(tensor, shape)]

  @classmethod
  def handle_selu(cls, node, input_dict):
    warnings.warn("Definition of Selu is incompatible"
      "between onnx and tensorflow.", UserWarning)
    return [tf.nn.selu(input_dict[node.inputs[0]])]

  # TODO: take care of negative indicies, discontinuous axes
  @classmethod
  def handle_slice(cls, node, input_dict):
    shape = tf.reshape(tf.rank(input_dict[node.inputs[0]]), tf.constant([1]))
    indices = tf.expand_dims(input_dict[node.inputs[1]], -1)
    begin = tf.scatter_nd(indices, input_dict[node.inputs[2]], shape)
    end = tf.scatter_nd(indices, input_dict[node.inputs[3]], shape)
    size = end - begin
    return [tf.slice(input_dict[node.inputs[0]], begin, size)]

  @classmethod
  def handle_split(cls, node, input_dict):
    split = tf.constant(node.attrs["split"]) if "split" in node.attrs else input_dict[node.inputs[1]]
    axis = node.attrs["axis"]
    return [tf.split(input_dict[node.inputs[0]], split, axis)]

  @classmethod
  def handle_sub(cls, node, input_dict):
    x = input_dict[node.inputs[0]]
    y = input_dict[node.inputs[1]]
    broadcast = node.attrs["broadcast"]
    if broadcast == 0:
      warnings.warn("Definition of Sub with broadcast disabled is incompatible"
        "between onnx and tensorflow.", UserWarning)
    if "axis" in node.attrs.keys():
      warnings.warn("Unsupported axis attribute by Tensorflow in Sub."
        "This attribute will be ignored.", UserWarning)
    return [tf.subtract(x, y)]

  @classmethod
  def handle_sum(cls, node, input_dict):
    values = [input_dict[a] for a in node.inputs]
    return [tf.reduce_sum(tf.stack(values), axis=0)]

prepare = TensorflowBackend.prepare

run_node = TensorflowBackend.run_node

run_model = TensorflowBackend.run_model

supports_device = TensorflowBackend.supports_device
