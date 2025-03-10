from typing import Any, Dict, List, Optional, Type

import numpy as np

from .exceptions import PythieServingException
from .tensorflow_proto.tensorflow.core.framework import (
    tensor_pb2,
    tensor_shape_pb2,
    types_pb2,
)

# from https://github.com/tensorflow/tensorflow/blob/master/tensorflow/python/framework/dtypes.py
_TF_TO_NP = {
    types_pb2.DT_HALF: np.float16,
    types_pb2.DT_FLOAT: np.float32,
    types_pb2.DT_DOUBLE: np.float64,
    types_pb2.DT_INT32: np.int32,
    types_pb2.DT_UINT8: np.uint8,
    types_pb2.DT_UINT16: np.uint16,
    types_pb2.DT_UINT32: np.uint32,
    types_pb2.DT_UINT64: np.uint64,
    types_pb2.DT_INT16: np.int16,
    types_pb2.DT_INT8: np.int8,
    # NOTE(touts): For strings we use object as it supports variable length # strings.
    types_pb2.DT_STRING: object,
    types_pb2.DT_COMPLEX64: np.complex64,
    types_pb2.DT_COMPLEX128: np.complex128,
    types_pb2.DT_INT64: np.int64,
    types_pb2.DT_BOOL: np.bool_,
}

_NP_TO_TF = {nt: tt for tt, nt in _TF_TO_NP.items()}
_NP_TO_TF[np.bytes_] = types_pb2.DT_STRING
_NP_TO_TF[np.str_] = types_pb2.DT_STRING

_CSV_TYPE = {
    "int": int,
    "str": lambda x: bytes(x, "utf-8"),
    "bool": bool,
}


def get_tf_type(np_dtype: Type):
    """
    :param np_dtype: python Type
    :return: types_pb2.DataType
    """
    try:
        return _NP_TO_TF[np_dtype.type]
    except KeyError:
        raise TypeError(f"Could not infer tensorflow type for {np_dtype.type}")


def get_np_dtype(tf_type: types_pb2.DataType):
    """
    :param tf_type: types_pb2.DataType
    :return: types_pb2.DataType
    """
    try:
        return np.dtype(_TF_TO_NP[tf_type])
    except KeyError:
        raise TypeError(f"Could not infer numpy type for {tf_type}")


def make_tensor_proto(values: List[Any]):
    np_array = np.asarray(values)

    # python/numpy default float type is float64. We prefer float32 instead.
    if np_array.dtype == np.float64:
        np_array = np_array.astype(np.float32)
    # python/numpy default int type is int64. We prefer int32 instead.
    elif np_array.dtype == np.int64:
        downcasted_array = np_array.astype(np.int32)
        # Do not down cast if it leads to precision loss.
        if np.array_equal(downcasted_array, np_array):
            np_array = downcasted_array

    dtype = get_tf_type(np_array.dtype)

    dims = [tensor_shape_pb2.TensorShapeProto.Dim(size=size) for size in np_array.shape]
    tensor_shape_proto = tensor_shape_pb2.TensorShapeProto(dim=dims)

    tensor_kwargs = {}
    if dtype == types_pb2.DT_STRING:
        string_val = []
        for vector in np_array:
            for s in vector:
                if not isinstance(s, bytes):
                    raise TypeError(f"{values} expect a list of bytes when working with DT_STRING types")
                string_val.append(s)
        tensor_kwargs["string_val"] = string_val
    else:
        tensor_kwargs["tensor_content"] = np_array.tobytes()
    return tensor_pb2.TensorProto(dtype=dtype, tensor_shape=tensor_shape_proto, **tensor_kwargs)


def make_ndarray_from_tensor(tensor: tensor_pb2.TensorProto):
    shape = [d.size for d in tensor.tensor_shape.dim]
    np_dtype = get_np_dtype(tensor.dtype)
    if tensor.tensor_content:
        return np.frombuffer(tensor.tensor_content, dtype=np_dtype).copy().reshape(shape)

    if tensor.dtype == types_pb2.DT_FLOAT:
        values = np.fromiter(tensor.float_val, dtype=np_dtype)
    elif tensor.dtype == types_pb2.DT_DOUBLE:
        values = np.fromiter(tensor.double_val, dtype=np_dtype)
    elif tensor.dtype in (types_pb2.DT_INT32, types_pb2.DT_INT64):
        values = np.fromiter(tensor.int_val, dtype=np_dtype)
    elif tensor.dtype == types_pb2.DT_BOOL:
        values = np.fromiter(tensor.bool_val, dtype=np_dtype)
    elif tensor.dtype == types_pb2.DT_STRING:
        values = np.array(tensor.string_val, dtype=np_dtype)
    else:
        raise TypeError("Unsupported tensor type: %s" % tensor.dtype)

    if values.size == 0:
        return np.zeros(shape, np_dtype)

    num_elements = np.prod(shape, dtype=np.int64)
    if values.size != num_elements:
        values = np.pad(values, (0, num_elements - values.size), "edge")

    return values.reshape(shape)


def get_csv_type(type_mapping: Dict[str, str]):
    try:
        return {feature_name: _CSV_TYPE[data_type] for feature_name, data_type in type_mapping.items()}
    except KeyError:
        raise TypeError(
            f"Could not infer conversion type given {type_mapping}. "
            f"Expecting one of following types: {_CSV_TYPE.keys()}"
        )


def parse_sample(
    request_inputs,
    features_names: List[str],
    nb_features: int,
    samples_dtype: Optional[Any] = None,
):
    for feature_name in features_names:
        check_request_feature_exists(request_inputs, feature_name)

    nb_samples = request_inputs[features_names[0]].tensor_shape.dim[0].size
    samples = np.empty((nb_samples, nb_features), samples_dtype)

    for feature_index, feature_name in enumerate(features_names):
        check_request_valid_length(request_inputs, feature_name, nb_samples)
        nd_array = make_ndarray_from_tensor(request_inputs[feature_name])
        check_array_shape(nd_array)

        samples[:, feature_index] = nd_array.reshape(-1)
    return samples


def check_request_valid_length(request_inputs, feature_name: str, nb_samples: int):
    if request_inputs[feature_name].tensor_shape.dim[0].size != nb_samples:
        raise PythieServingException(f"{feature_name} has invalid length.")


def check_array_shape(nd_array: np.ndarray):
    if len(nd_array.shape) != 2 or nd_array.shape[1] != 1:
        raise PythieServingException("All input vectors should be 1D tensor")


def check_request_feature_exists(request_inputs, feature_name: str):
    if feature_name not in request_inputs:
        raise PythieServingException(f"{feature_name} not set in the predict request.")
