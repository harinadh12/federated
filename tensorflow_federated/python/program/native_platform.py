# Copyright 2022, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A federated platform implemented using native components."""

import collections
import random
from typing import Any, Iterable, List, Optional, Sequence, Union

import numpy as np
import tensorflow as tf

from tensorflow_federated.python.common_libs import py_typecheck
from tensorflow_federated.python.common_libs import structure
from tensorflow_federated.python.core.api import computation_base
from tensorflow_federated.python.core.impl.context_stack import context_base
from tensorflow_federated.python.core.impl.types import computation_types
from tensorflow_federated.python.core.impl.types import placements
from tensorflow_federated.python.core.impl.types import type_conversions
from tensorflow_federated.python.program import data_source
from tensorflow_federated.python.program import federated_context
from tensorflow_federated.python.program import value_reference


class NumpyValueReference(value_reference.MaterializableValueReference):
  """A `tff.program.MaterializableValueReference` backed by a `numpy` value."""

  def __init__(self, value: Union[np.generic, np.ndarray,
                                  Iterable[Union[np.generic, np.ndarray]]],
               type_signature: Union[computation_types.TensorType,
                                     computation_types.SequenceType]):
    py_typecheck.check_type(
        type_signature,
        (computation_types.TensorType, computation_types.SequenceType))

    self._value = value
    self._type_signature = type_signature

  @property
  def type_signature(
      self
  ) -> Union[computation_types.TensorType, computation_types.SequenceType]:
    """The `tff.TensorType` of this object."""
    return self._type_signature

  def get_value(
      self
  ) -> Union[np.generic, np.ndarray, Iterable[Union[np.generic, np.ndarray]]]:
    """Returns the referenced value as a numpy scalar or array."""
    return self._value

  def __eq__(self, other: Any) -> bool:
    if self is other:
      return True
    elif not isinstance(other, NumpyValueReference):
      return NotImplemented
    if self._type_signature != other._type_signature:
      return False
    if self._type_signature.is_sequence():
      return list(self._value) == list(other._value)
    else:
      return self._value == other._value


def _create_structure_of_value_references(
    value: Any, type_signature: computation_types.Type) -> Any:
  """Returns a structure of `tff.program.NumpyValueReference`s."""
  py_typecheck.check_type(type_signature, computation_types.Type)

  if type_signature.is_struct():
    value = structure.from_container(value)
    elements = []
    element_types = structure.iter_elements(type_signature)
    for element, (name, element_type) in zip(value, element_types):
      element = _create_structure_of_value_references(element, element_type)
      elements.append((name, element))
    return structure.Struct(elements)
  elif type_signature.is_federated():
    return _create_structure_of_value_references(value, type_signature.member)
  elif type_signature.is_sequence():
    return NumpyValueReference(value, type_signature)
  elif type_signature.is_tensor():
    return NumpyValueReference(value, type_signature)
  else:
    raise NotImplementedError(f'Unexpected type found: {type_signature}.')


def _materialize_structure_of_value_references(
    value: Any, type_signature: computation_types.Type) -> Any:
  """Returns a structure of materialized `tff.program.NumpyValueReference`s."""
  py_typecheck.check_type(type_signature, computation_types.Type)

  def _materialize(x):
    if isinstance(x, NumpyValueReference):
      return x.get_value()
    else:
      return x

  if type_signature.is_struct():
    value = structure.from_container(value)
    elements = []
    element_types = structure.iter_elements(type_signature)
    for element, (name, element_type) in zip(value, element_types):
      element = _materialize_structure_of_value_references(
          element, element_type)
      elements.append((name, element))
    return structure.Struct(elements)
  elif type_signature.is_federated():
    return _materialize_structure_of_value_references(value,
                                                      type_signature.member)
  elif type_signature.is_sequence():
    return _materialize(value)
  elif type_signature.is_tensor():
    return _materialize(value)
  else:
    return value


class NativeFederatedContext(federated_context.FederatedContext):
  """A `tff.program.FederatedContext` backed by a `tff.framework.Context`."""

  def __init__(self, context: context_base.Context):
    """Returns an initialized `tff.program.NativeFederatedContext`.

    Args:
      context: A `tff.framework.Context`.
    """
    py_typecheck.check_type(context, context_base.Context)

    self._context = context

  def ingest(self, value: Any, type_spec: computation_types.Type) -> Any:
    """Ingests the 'val' for the type `type_spec`.

    Ingest translates the arguments of `tff.Computation`s from Python values to
    a form that can be used by the context, values must be ingested by the
    context before calling `invoke`.

    Args:
      value: The value to ingest.
      type_spec: The `tff.Type` of the value.

    Returns:
      The result of ingestion, which is context-dependent.
    """
    py_typecheck.check_type(type_spec, computation_types.Type)

    return _materialize_structure_of_value_references(value, type_spec)

  def invoke(self, comp: computation_base.Computation, arg: Any) -> Any:
    """Invokes the `comp` with the argument `arg`.

    The `arg` must be ingested by the context before calling `invoke`, by
    calling `ingest`.

    Args:
      comp: The `tff.Computation` being invoked.
      arg: The optional argument of `comp`.

    Returns:
      The result of invocation, must contain only structures, server-placed
      values, or tensors.

    Raises:
      ValueError: If the result type of the invoked comptuation does not contain
      only structures, server-placed values, or tensors.
    """
    py_typecheck.check_type(comp, computation_base.Computation)

    result_type = comp.type_signature.result
    if not federated_context.contains_only_server_placed_data(result_type):
      raise ValueError(
          'Expected the result type of the invoked computation to contain only '
          'structures, server-placed values, or tensors, found '
          f'\'{result_type}\'.')

    if comp.type_signature.parameter is not None:
      arg = self._context.ingest(arg, comp.type_signature.parameter)
    result = self._context.invoke(comp, arg)
    result = _create_structure_of_value_references(result, result_type)
    result = type_conversions.type_to_py_container(result, result_type)
    return result


class DatasetDataSourceIterator(data_source.FederatedDataSourceIterator):
  """A `tff.program.FederatedDataSourceIterator` backed by `tf.data.Dataset`s.

  A `tff.program.FederatedDataSourceIterator` backed by a sequence of
  `tf.data.Dataset's, one `tf.data.Dataset' per client, and selects data
  uniformly random with replacement.
  """

  def __init__(self, datasets: Sequence[tf.data.Dataset],
               federated_type: computation_types.FederatedType):
    """Returns an initialized `tff.program.DatasetDataSourceIterator`.

    Args:
      datasets: A sequence of `tf.data.Dataset's to use to yield the data from
        this data source.
      federated_type: The type of the data returned by calling `select` on an
        iterator.

    Raises:
      ValueError: If `datasets` is an empty list or if each `tf.data.Dataset` in
        `datasets` does not have the same type specification.
    """
    py_typecheck.check_type(datasets, collections.abc.Sequence)
    if not datasets:
      raise ValueError('Expected `datasets` to not be an empty list.')
    for dataset in datasets:
      py_typecheck.check_type(dataset, tf.data.Dataset)
      element_spec = datasets[0].element_spec
      if dataset.element_spec != element_spec:
        raise ValueError('Expected each `tf.data.Dataset` in `datasets` to '
                         'have the same type specification, found '
                         f'\'{element_spec}\' and \'{dataset.element_spec}\'.')
    py_typecheck.check_type(federated_type, computation_types.FederatedType)

    self._datasets = datasets
    self._federated_type = federated_type

  @property
  def federated_type(self) -> computation_types.FederatedType:
    """The type of the data returned by calling `select`."""
    return self._federated_type

  def select(self, number_of_clients: Optional[int] = None) -> Any:
    """Returns a new selection of data from this iterator.

    Args:
      number_of_clients: A number of clients to use when selecting data, must be
        a positive integer and less than the number of `datasets`.

    Raises:
      ValueError: If `number_of_clients` is not a positive integer or if
        `number_of_clients` is not less than the number of `datasets`.
    """
    if (number_of_clients is None or number_of_clients < 0 or
        number_of_clients > len(self._datasets)):
      raise ValueError('Expected `number_of_clients` to be a positive integer '
                       'and less than the number of `datasets`.')
    return random.choices(population=self._datasets, k=number_of_clients)


class DatasetDataSource(data_source.FederatedDataSource):
  """A `tff.program.FederatedDataSource` backed by `tf.data.Dataset`s.

  A `tff.program.FederatedDataSource` backed by a sequence of
  `tf.data.Dataset's, one `tf.data.Dataset' per client, and selects data
  uniformly random with replacement.
  """

  def __init__(self, datasets: Sequence[tf.data.Dataset]):
    """Returns an initialized `tff.program.DatasetDataSource`.

    Args:
      datasets: A sequence of `tf.data.Dataset's to use to yield the data from
        this data source.

    Raises:
      ValueError: If `datasets` is an empty list or if each `tf.data.Dataset` in
        `datasets` does not have the same type specification.
    """
    py_typecheck.check_type(datasets, collections.abc.Sequence)
    if not datasets:
      raise ValueError('Expected `datasets` to not be an empty list.')
    for dataset in datasets:
      py_typecheck.check_type(dataset, tf.data.Dataset)
      element_spec = datasets[0].element_spec
      if dataset.element_spec != element_spec:
        raise ValueError('Expected each `tf.data.Dataset` in `datasets` to '
                         'have the same type specification, found '
                         f'\'{element_spec}\' and \'{dataset.element_spec}\'.')

    self._datasets = datasets
    self._federated_type = computation_types.FederatedType(
        computation_types.SequenceType(element_spec), placements.CLIENTS)

  @property
  def federated_type(self) -> computation_types.FederatedType:
    """The type of the data returned by calling `select` on an iterator."""
    return self._federated_type

  @property
  def capabilities(self) -> List[data_source.Capability]:
    """The list of capabilities supported by this data source."""
    return [data_source.Capability.RANDOM_UNIFORM]

  def iterator(self) -> DatasetDataSourceIterator:
    """Returns a new iterator for retrieving data from this data source."""
    return DatasetDataSourceIterator(self._datasets, self._federated_type)
