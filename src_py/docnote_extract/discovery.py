"""This module is responsible for the code that explores firstparty
packages to recursively find all their modules.
"""
from __future__ import annotations

import inspect
import logging
from collections.abc import Iterable
from importlib import import_module
from pkgutil import iter_modules
from types import ModuleType
from typing import cast

from docnote import DOCNOTE_CONFIG_ATTR
from docnote import DocnoteConfig
from docnote import ReftypeMarker

from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import GetattrTraversal
from docnote_extract.crossrefs import is_crossreffed

logger = logging.getLogger(__name__)


def discover_all_modules(
        root_packages: Iterable[str]
        ) -> dict[str, ModuleType]:
    """Recursively imports all of the modules under the passed
    ``root_packages``. Returns the loaded modules.
    """
    retval = {}
    for root_package in root_packages:
        root_module = retval[root_package] = import_module(root_package)
        eager_import_submodules(root_module, loaded_modules=retval)

    return retval


def eager_import_submodules(
        module: ModuleType,
        *,
        # This is purely here for the purposes of making the recursion more
        # efficient by avoiding repeated dict merges
        loaded_modules: dict[str, ModuleType]
        ) -> dict[str, ModuleType]:
    """This recursively imports all of the submodules of the passed
    module. It returns a mapping of the full module name to its loaded
    module.

    Note that, as per stdlib docs on ``walk_packages``, this relies upon
    the package finder supporting ``iter_modules()``, and is therefore
    sensitive to the current status of our import hook:

    >
    __embed__: 'text/quote'
        Only works for a finder which defines an iter_modules() method.
        This interface is non-standard, so the module also provides
        implementations for importlib.machinery.FileFinder and
        zipimport.zipimporter.

    Also note that **this does not support namespace packages.** This is
    a limitation of cpython; the only workarounds would be to eagerly
    explore every single subfolder within a python package. This might
    be desirable in some cases, but in others, it's a mess.
    """
    # Note that this is the whole absolute name and not the relative path.
    parent_package_name = module.__name__

    # Note: all packages are modules (in this context), but not all modules
    # are packages. In other words: packages have submodules, but if
    # is_package is falsy, then it doesn't. That's literally its meaning
    # here.
    recurseable_pkg_modules: set[ModuleType] = set()

    # In theory we could just use ``walk_packages``, but I've had a bunch of
    # problems with it, so we're basically implementing our own version of it
    # here.
    # https://stackoverflow.com/questions/3365740/
    # https://stackoverflow.com/questions/17024605/
    for __, submodule_name_rel, is_package in iter_modules(module.__path__):
        submodule_name_abs = f'{parent_package_name}.{submodule_name_rel}'

        logger.debug(
            'Importing module %s for submodule discovery', submodule_name_abs)
        try:
            submodule = import_module(submodule_name_abs)
        except ModuleNotFoundError:
            logger.exception(
                'Failed to import submodule %s; skipping', submodule_name_abs)
            continue

        # Make sure to do this check before adding it to loaded_modules, or
        # this is a noop!
        if is_package and submodule.__name__ not in loaded_modules:
            recurseable_pkg_modules.add(submodule)

        loaded_modules[submodule_name_abs] = submodule

    # Separating this from the above for loop does insulates us from the actual
    # behavior of walk_packages in two ways. First, it deduplicates the modules
    # into a set, so we can skip over any duplicate loads. Second, it lets us
    # compare the values with the already loaded modules, and skip them if they
    # were already loaded.
    for recurseable_pkg_module in recurseable_pkg_modules:
        eager_import_submodules(
            recurseable_pkg_module,
            loaded_modules=loaded_modules)

    return loaded_modules


def find_special_reftypes(
        modules: Iterable[ModuleType]
        ) -> dict[Crossref, ReftypeMarker]:
    """Exhaustively inspects modules (via getattr traversals) for any
    marked special reftypes.

    Note that this relies upon the fact that special reftypes **must**
    be declared via decorator (since that's the only way to attach
    configs to classes and functions, which are the only things that
    can become special reftypes).
    """
    retval: dict[Crossref, ReftypeMarker] = {}
    for module in modules:
        module_name = module.__name__
        for name, obj in module.__dict__.items():
            toplevel_crossref = Crossref(
                module_name=module_name,
                toplevel_name=name,
                traversals=())
            _find_special_reftypes_recursive(
                module_name, obj, toplevel_crossref, retval)

    return retval


def _find_special_reftypes_recursive(
        module_name: str,
        obj: object,
        crossref: Crossref,
        lookup: dict[Crossref, ReftypeMarker],
        *,
        _recursion_guard: set[int] | None = None
        ) -> None:
    """Performs a recursive search for special reftypes, adding any
    found ones in-place to the passed lookup.
    """
    if _recursion_guard is None:
        recursion_guard = set()
    else:
        recursion_guard = _recursion_guard

    # This is a **crucially** important performance improvement; otherwise,
    # everything proceeds GLACIALLY and consumes a ton of memory, as we
    # recursively check like literally the entirety of everything.
    # Seriously, this is the difference between tests passing in less than a
    # second, and each test taking several seconds. It's a big deal.
    if getattr(obj, '__module__', None) != module_name:
        return

    # This prevents infinite recursion on anything that is self-referential;
    # keep in mind that this exists even in the stdlib, because the language
    # itself makes use of this.
    obj_id = id(obj)
    if obj_id in recursion_guard:
        return
    else:
        recursion_guard.add(obj_id)

    if hasattr(obj, DOCNOTE_CONFIG_ATTR):
        decorated_config = getattr(obj, DOCNOTE_CONFIG_ATTR)

        # Unlike in normalization, this is probably unnecessary, but... better
        # safe than sorry.
        if not is_crossreffed(decorated_config):
            decorated_config = cast(DocnoteConfig, decorated_config)
            if decorated_config.mark_special_reftype is not None:
                lookup[crossref] = decorated_config.mark_special_reftype

    for attr_name, attr_obj in inspect.getmembers_static(obj):
        child_crossref = crossref / GetattrTraversal(attr_name)
        _find_special_reftypes_recursive(
            module_name,
            attr_obj,
            child_crossref,
            lookup,
            _recursion_guard=recursion_guard)
