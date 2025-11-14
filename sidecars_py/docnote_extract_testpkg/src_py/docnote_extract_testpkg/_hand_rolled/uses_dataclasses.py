from __future__ import annotations

from dataclasses import KW_ONLY
from dataclasses import dataclass


@dataclass
class DataclassWithKwOnlyAndDefaults:
    """This isn't immediately obvious, but the point here is to make
    sure that importing KW_ONLY works, even if you have it set up such
    that there's a positional-only argument that 
    """
    foo: int = 1
    # Note that this will get stringified by the __future__ import!
    _: KW_ONLY
    bar: int = 2


@dataclass
class DataclassWithoutDocstring:
    # Dataclasses have an auto-generated docstring that we don't want to have
    # in the extraction. We use this to verify we've wrapped the stdlib in such
    # a way that it gets removed.
    foo: int
