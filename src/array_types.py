"""NumPy array aliases for annotations.

They fix the dtype *kind* rather than the bit width: NumPy's typing widens arithmetic results
(``intp - intp`` is ``signedinteger[Any]``), and inputs arrive as int8/float32 from the loader
or float64 from feature code.
"""

from __future__ import annotations

from typing import Any, TypeAlias

import numpy as np
import numpy.typing as npt

FloatArray: TypeAlias = npt.NDArray[np.floating[Any]]
IntArray: TypeAlias = npt.NDArray[np.integer[Any]]
BoolArray: TypeAlias = npt.NDArray[np.bool_]
NumArray: TypeAlias = npt.NDArray[np.number[Any]]  # any numeric dtype: labels, scores, days
