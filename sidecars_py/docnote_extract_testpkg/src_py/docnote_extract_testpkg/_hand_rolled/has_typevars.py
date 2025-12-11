from __future__ import annotations

from typing import TypeVar


_ModuleTypeVar = TypeVar('_ModuleTypeVar')


def uses_module_typevar(arg: _ModuleTypeVar) -> _ModuleTypeVar: ...


def uses_sugared_typevar[T](arg: T) -> T: ...


class HasTypevarSuperclass[T: int](dict[T, T]):
    """Note that we're declaring a typevar here, and then referencing
    it within the superclasses. We need to make sure that the type var
    here is also made available when figuring out the base classes.
    """
