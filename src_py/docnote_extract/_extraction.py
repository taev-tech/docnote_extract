from __future__ import annotations

import dataclasses
import inspect
import logging
import sys
import typing
from collections import defaultdict
from collections.abc import Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import KW_ONLY
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from functools import partial
from functools import wraps
from importlib import import_module
from importlib import reload as reload_module
from importlib.abc import Loader
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import Annotated
from typing import Any
from typing import Protocol
from typing import TypeGuard
from typing import cast

from docnote import Note
from docnote import ReftypeMarker

from docnote_extract._module_tree import ModuleTreeNode
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import make_crossreffed
from docnote_extract.crossrefs import make_metaclass_crossreffed
from docnote_extract.discovery import discover_all_modules
from docnote_extract.discovery import find_special_reftypes
from docnote_extract.summaries import Singleton

type TrackingRegistry = dict[int, tuple[str, str] | None]
UNPURGEABLE_MODULES: Annotated[
        set[str],
        Note('''As noted in the stdlib documentation for the ``sys`` module,
            removing certain modules from ``sys.modules`` can create problems.
            If you run into such issues, you can add the problem module to
            this set to prevent it from being removed. In that case, any time
            the stub status changes, it will be reloaded instead of removed.
            ''')
    ] = set()
# These are completely 100% untouched by our import hook.
NOHOOK_PACKAGES = {
    'docnote',
    'docnote_extract',
}
_EXTRACTION_PHASE: ContextVar[_ExtractionPhase] = ContextVar(
    '_EXTRACTION_PHASE')

_MODULE_TO_INSPECT: ContextVar[str] = ContextVar('_MODULE_TO_INSPECT')
_ACTIVE_TRACKING_REGISTRY: ContextVar[TrackingRegistry] = ContextVar(
    '_ACTIVE_TRACKING_REGISTRY')
MODULE_ATTRNAME_STUBSTRATEGY = '_docnote_extract_stub_strat'

logger = logging.getLogger(__name__)


class _StubStrategy(Enum):
    """Slightly different than extraction phase, this determines what
    we're going to do when we get to later steps in the import process.
    """
    STUB = 'stub'
    INSPECT = 'inspect'
    TRACK = 'track'


GLOBAL_REFTYPE_MARKERS: dict[Crossref, ReftypeMarker] = {
    Crossref(module_name='configatron', toplevel_name='ConfigMeta'):
        ReftypeMarker.METACLASS,
}


class _ExtractionPhase(Enum):
    """This describes exactly which phase of extraction we're in. The
    _ExtractionFinderLoader uses it to avoid recursion issues and
    dictate control flow and/or delegation to other imports.
    """
    HOOKED = 'hooked'
    EXPLORATION = 'exploration'
    PREPARATION = 'preparation'
    EXTRACTION = 'extraction'


@dataclass(slots=True, frozen=True)
class _ExtractionFinderLoader(Loader):
    """We use this import finder/loader to power all of our docnote
    extraction.
    """
    firstparty_packages: frozenset[str]

    _: KW_ONLY

    # See note in ``gather`` for explanations of these three config-able
    # parameters
    special_reftype_markers: dict[Crossref, ReftypeMarker] = field(
        default_factory=dict)
    # Note: full module name
    nostub_firstparty_modules: frozenset[str] = field(
        default_factory=frozenset)
    # Note: root package, not individual modules
    nostub_packages: frozenset[str] = field(default_factory=frozenset)

    module_stash_prehook: dict[str, ModuleType] = field(
        default_factory=dict, repr=False)
    # Mocking stubs. These are created lazily, as-needed, on-the-fly, whenever
    # we need one -- but as with sys.modules, once it's been created, we'll
    # return the same one, from this lookup.
    module_stash_stubbed: dict[str, ModuleType] = field(
        default_factory=dict, repr=False)
    # Internals of the module are real, but any non-bypassed third-party
    # deps are stubbed out. This is what we use for constructing specs, and
    # for modules that need real versions of objects. Note that this version
    # of it is the actual real module, NOT our tracking man-in-the-middle
    # version of the module.
    module_stash_nostub_raw: dict[str, ModuleType] = field(
        default_factory=dict, repr=False)
    # Same as above, but this is the tracked version. This is what we use
    # during inspection to keep a registry of objects that were imported from
    # a particular partial module. This will always be smaller than nostub_raw,
    # because nostub_raw includes all first-party modules, but this only
    # includes stubbed ones.
    module_stash_tracked: dict[str, ModuleType] = field(
        default_factory=dict, repr=False)
    # This is used for marking things dirty.
    inspected_modules: set[str] = field(default_factory=set, repr=False)

    def discover_and_extract(self) -> dict[str, ModulePostExtraction]:
        ctx_token = _EXTRACTION_PHASE.set(_ExtractionPhase.HOOKED)
        try:
            logger.info('Stashing prehook modules and installing import hook.')
            self._stash_prehook_modules()
            self.install()

            # We're relying upon the full exploration here to import all
            # possible modules needed for extraction. Then we stash the raw
            # versions of nostub- and firstparty modules, cleanup sys, and
            # move on to the next phase, where we use the raw modules.
            logger.info('Starting exploration phase.')
            _EXTRACTION_PHASE.set(_ExtractionPhase.EXPLORATION)
            firstparty_modules = discover_all_modules(self.firstparty_packages)
            self.special_reftype_markers.update(
                find_special_reftypes(firstparty_modules.values()))
            firstparty_names = frozenset(firstparty_modules)
            self._stash_firstparty_or_nostub_raw()
            # We need to clean up everything here because we'll be
            # transitioning into tracked modules instead of the raw ones
            logger.info('Exploration done; cleaning up sys.modules.')
            self.cleanup_sys(self._get_all_dirty_modules())

            # We want to preemptively create tracking or stub versions of all
            # first-party modules; this ensures we have the cleanest,
            # stubbiest-possible collection of firstparty modules.
            # We might not **need** all of these, but stubbing is quick (since
            # we don't need an exec), and this dramatically improves our
            # reliability.
            logger.info('Starting preparation phase.')
            _EXTRACTION_PHASE.set(_ExtractionPhase.PREPARATION)
            self._prepare_firstparty_stubs_or_tracking(firstparty_names)

            # Clean everything one more time in case there were weird import
            # deps in the firstparty nostub modules
            logger.info('Preparation done; cleaning up sys.modules.')
            self.cleanup_sys(self._get_all_dirty_modules())

            logger.info('Starting extraction phase.')
            _EXTRACTION_PHASE.set(_ExtractionPhase.EXTRACTION)

            # Since extraction doesn't use imports to generate the extracted
            # module, we can prepopulate once, instead of needing to do it for
            # every module.
            self._prepopulate_sys(firstparty_names)
            retval: dict[str, ModulePostExtraction] = {}
            for module_name in firstparty_names:
                retval[module_name] = self.extract_firstparty(module_name)

            # Note that uninstall will handle final cleanup
            logger.info('Extraction completed successfully.')
            return retval

        finally:
            try:
                logger.info(
                    'Uninstalling import hook and restoring prehook modules.')
                self.uninstall()
            finally:
                _EXTRACTION_PHASE.reset(ctx_token)
                self._unstash_prehook_modules()

    def _stash_firstparty_or_nostub_raw(self):
        """This checks sys.modules for any firstparty or nostub modules,
        adding references to them within ``module_stash_nostub_raw``.
        """
        for fullname, module in sys.modules.items():
            package_name, _, _ = fullname.partition('.')
            if (
                package_name in self.firstparty_packages
                or package_name in self.nostub_packages
            ):
                self.module_stash_nostub_raw[fullname] = module

    def extract_firstparty(
            self,
            module_name: str
            ) -> ModulePostExtraction:
        """Here, given a firstparty module name, we construct a new
        module object for it that mocks out all non-stub-bypassed
        external dependencies, regardless of first- or third-party.

        We've structured this to go on a per-module basis to make
        unit tests easier.
        """
        import_tracking_registry: TrackingRegistry = {}
        inspect_ctx_token = _MODULE_TO_INSPECT.set(module_name)
        try:
            with _activatate_tracking_registry(import_tracking_registry):
                # HERE BE DRAGONS.
                # Whatever you do, do **NOT** import the module here. The
                # import system will overwrite our prepared submodule attrs
                # on existing tracking/stub modules, causing heisenbugs.
                # Especially pernicious: the problems are order-dependent,
                # meaning that our use of sets will cause things to sometimes
                # pass our test suite.
                # KEEP THIS OUT OF THE IMPORT SYSTEM AT ALL COSTS!
                nostub_module = self.module_stash_nostub_raw[module_name]
                spec = ModuleSpec(
                    name=module_name,
                    loader=self,
                    loader_state=_DelegatedLoaderState(
                        fullname=module_name,
                        is_firstparty=True,
                        delegated_module=nostub_module,
                        stub_strategy=_StubStrategy.INSPECT))
                nostub_module_spec = getattr(nostub_module, '__spec__', None)
                _clone_spec_attrs(nostub_module_spec, spec)

                module_source = inspect.getsource(nostub_module)
                extracted_module = cast(
                    ModulePostExtraction,
                    _clone_import_attrs(
                        self.module_stash_nostub_raw[module_name],
                        spec))

                logger.info(
                    'Re-execing module for inspection: %s', module_name)

                # Putting the partially-completed module into sys.modules
                # prevents other things (some stdlib code -- ex dataclasses --
                # does WEIRD stuff with imports) from getting a stubbed ref
                # to the module we're inspecting
                stub_module = sys.modules.pop(module_name)
                sys.modules[module_name] = extracted_module
                # This allows us to also get references hidden behind circular
                # imports
                typing.TYPE_CHECKING = True
                try:
                    exec(module_source, extracted_module.__dict__)  # noqa: S102
                finally:
                    typing.TYPE_CHECKING = False
                    sys.modules[module_name] = stub_module
                    self.inspected_modules.add(module_name)

            extracted_module._docnote_extract_import_tracking_registry = (
                import_tracking_registry)
            return extracted_module
        finally:
            _MODULE_TO_INSPECT.reset(inspect_ctx_token)

    def install(self) -> None:
        """Installs the loader in sys.meta_path and then gets everything
        ready for discovery.
        """
        for finder in sys.meta_path:
            if isinstance(finder, _ExtractionFinderLoader):
                raise RuntimeError(
                    'Cannot have multiple active extraction loaders!')

        sys.meta_path.insert(0, self)

    @classmethod
    def uninstall(cls) -> None:
        """As you might guess by the name, this removes any installed
        import hook. Note that it is safe to call this multiple times,
        and regardless of whether or not an import hook has been
        installed; in those cases, it will simply be a no-op.

        What is not immediately obvious from the name, however, is that
        this **will also force reloading of every stubbed module loaded
        by the import hook.** Therefore, after calling``uninstall``,
        you should be reverted to a clean slate.
        """
        """DON'T FORGET THAT YOU NEED TO PURGE EVERY IMPORT FROM ALL
        OF THE LOOKUPS FROM sys.modules!!!
        """
        # In theory, we only have one of these -- install() won't allow
        # multiples -- but we want to be extra defensive here (and also,
        # idempotent!)
        target_indices = []
        for index, meta_path_finder in enumerate(sys.meta_path):
            if isinstance(meta_path_finder, cls):
                target_indices.append(index)

        modules_to_remove: set[str] = set()
        # By reversing, we don't need to worry about offsets from deleting
        # stuff in case we somehow have multiples
        for index in reversed(target_indices):
            meta_path_finder = cast(
                _ExtractionFinderLoader,
                sys.meta_path.pop(index))

            modules_to_remove.update(meta_path_finder._get_all_dirty_modules())

        cls.cleanup_sys(modules_to_remove)

    def _prepopulate_sys(self, firstparty_names: frozenset[str]):
        """Just to **make damn sure** that we have the correct modules
        in place during extraction, we preemptively populate sys.modules
        with all of our firstparty stubs/tracking modules.

        This potentially also speeds up extraction marginally by
        bypassing the import hook, but this is really just a happy
        coincidence.
        """
        # Note: order doesn't matter here, since we're bypassing imports
        # entirely.
        for module_name in firstparty_names:
            if module_name in self.nostub_firstparty_modules:
                target_module = self.module_stash_tracked[module_name]
            else:
                target_module = self.module_stash_stubbed[module_name]

            sys.modules[module_name] = target_module

    @classmethod
    def cleanup_sys(cls, modules_to_remove: set[str]) -> None:
        """Given a list of module names, removes them from sys.modules,
        unless they're unpurgeable, in which case we force a reload.
        """
        # Note that we don't need to worry about importlib.invalidate_caches,
        # because we're not changing the actual content of the modules, just
        # the environment they're exec'd into.
        for module_to_remove in modules_to_remove:
            module_obj = sys.modules.get(module_to_remove)
            if module_obj is not None:
                if module_to_remove in UNPURGEABLE_MODULES:
                    reload_module(module_obj)
                else:
                    del sys.modules[module_to_remove]

    def _get_all_dirty_modules(self) -> set[str]:
        """Get a snapshot set of every single module that might be dirty
        at the finder/loader. Use this to clean sys.modules when
        transitioning between phases.
        """
        modules: set[str] = set()
        modules.update(self.module_stash_stubbed)
        modules.update(self.module_stash_nostub_raw)
        modules.update(self.module_stash_tracked)
        modules.update(self.inspected_modules)
        return modules

    def _get_firstparty_dirty_modules(self) -> set[str]:
        """Same as above, but limited just to the firstparty modules.
        Use this between inspecting individual firstparty modules.
        """
        retval: set[str] = set()
        all_modules = self._get_all_dirty_modules()
        for module in all_modules:
            pkg_name, _, _ = module.partition('.')
            if pkg_name in self.firstparty_packages:
                retval.add(module)

        return retval

    def _stash_prehook_modules(self):
        """This checks all of sys.modules, stashing and removing
        anything that isn't stdlib or a thirdparty bypass package.
        """
        prehook_module_names = sorted(sys.modules)
        for prehook_module_name in prehook_module_names:
            package_name, _, _ = prehook_module_name.partition('.')

            if package_name == 'dataclasses':
                self.module_stash_prehook['dataclasses'] = dataclasses
                patched_dataclasses = ModuleType('dataclasses')
                patched_dataclasses.__getattr__ = _patched_dataclass_getattr
                sys.modules['dataclasses'] = patched_dataclasses

            if (
                package_name not in sys.stdlib_module_names
                and package_name not in NOHOOK_PACKAGES
            ):
                logger.debug('Stashing prehook module %s', prehook_module_name)

                # This is purely to save us needing to reimport the package
                # to build out a raw package for use during the exploration
                # phase. The only difference is that we're not popping it;
                # we're JUST stashing it so it can be restored after
                # uninstalling the import hook.
                if (
                    package_name in self.nostub_packages
                    or package_name in self.firstparty_packages
                ):
                    prehook_module = sys.modules[prehook_module_name]

                else:
                    logger.debug(
                        'Popping %s from sys.modules for stash',
                        prehook_module_name)
                    prehook_module = sys.modules.pop(prehook_module_name)

                self.module_stash_prehook[prehook_module_name] = prehook_module

    def _unstash_prehook_modules(self):
        for name, module in self.module_stash_prehook.items():
            logger.info('Restoring prehook module %s', name)
            sys.modules[name] = module

    def _prepare_firstparty_stubs_or_tracking(
            self,
            firstparty_names: frozenset[str]):
        """We use this to eagerly construct stubs or tracking wrappers
        for all firstparty modules **before** any module is under
        inspection. That way, we have the absolute bare minimum of real
        modules, and we don't need to constantly re-create the
        firstparty tracking modules to accommodate which modules were
        unstubbed because they were under inspection. We also avoid a
        bunch of import instability because we're violating underlying
        assumptions of the import system.

        This also add the submodules as attributes in each module, and
        sets the ``__all__`` for modules as required.
        """
        # We want to order this such that shallower levels are always done
        # before deeper ones; that way we avoid weird edge cases from the
        # import system implicitly loading parent levels
        by_depth = defaultdict(list)
        for name in firstparty_names:
            by_depth[name.count('.')].append(name)

        for module_names_for_depth in by_depth.values():
            for module_name in module_names_for_depth:
                # The import hook will manage stub vs tracking for us; we don't
                # need to worry about it
                import_module(module_name)

        name_tree = ModuleTreeNode.from_discovery(firstparty_names)
        for name_tree_root in name_tree.values():
            for name_node in name_tree_root.flatten():
                module_name = name_node.fullname
                nostub_module = self.module_stash_nostub_raw[module_name]

                if module_name in self.nostub_firstparty_modules:
                    target_module = self.module_stash_tracked[module_name]
                else:
                    target_module = self.module_stash_stubbed[module_name]

                # Note: we ONLY want to do this for modules that define an
                # __all__. If you're doing star intra-project starred imports
                # and **not** defining an __all__, we really can't help you.
                # Any workaround is inherently super dangerous, because we
                # might, for example, accidentally clobber the importing
                # module's __name__, which would break relative imports in
                # an extremely-difficult-to-debug way.
                if hasattr(nostub_module, '__all__'):
                    # pyright doesn't like modules not necessarily having an
                    # __all__, hence the ignore directive
                    target_module.__all__ = tuple(nostub_module.__all__)  # type: ignore

                # And now we need to populate the children as attributes on
                # the parent.
                for child_node in name_node.children.values():
                    child_name = child_node.fullname
                    if child_name in self.nostub_firstparty_modules:
                        child_module = self.module_stash_tracked[child_name]
                    else:
                        child_module = self.module_stash_stubbed[child_name]

                    setattr(
                        target_module,
                        child_node.relname,
                        child_module)

    def find_spec(
            self,
            fullname: str,
            path: Sequence[str] | None,
            target: ModuleType | None = None
            ) -> ModuleSpec | None:
        """This determines:
        ++  whether or not we're going to load a package at all
        ++  what strategy we're going to take for loading
        etc.
        """
        base_package, *_ = fullname.split('.')

        # Note that base_package is correct here; stdlib doesn't add in
        # every submodule.
        if base_package in sys.stdlib_module_names:
            logger.debug('Bypassing wrapping for stdlib module %s.', fullname)
            return None
        if base_package in NOHOOK_PACKAGES:
            logger.debug(
                'Bypassing tracker wrapping for %s via hard-coded third party '
                + 'nohook package %s',
                fullname, base_package)
            return None

        # Thirdparty packages not marked with nostub will ALWAYS return a
        # stub package as long as the import hook is installed, regardless of
        # extraction phase. Otherwise, we'd need them to be installed in the
        # docs virtualenv, reftypes would never be generated, etc etc etc.
        if (
            base_package not in self.firstparty_packages
            and base_package not in self.nostub_packages
        ):
            logger.debug('Will return stub spec for %s', fullname)
            # We don't need any loader state here; we're just going to stub it
            # completely, so we can simply return a plain spec.
            spec = ModuleSpec(
                name=fullname,
                loader=self,
                loader_state=_ExtractionLoaderState(
                    fullname=fullname,
                    is_firstparty=False,
                    stub_strategy=_StubStrategy.STUB))
            # As per stdlib docs on modulespecs, this indicates to the import
            # system that this has submodules. For stubs, we can't actually
            # know this, so we just always set it.
            spec.submodule_search_locations = []
            return spec

        # All of the rest of our behavior depends upon our current
        # extraction phase.
        else:
            phase = _EXTRACTION_PHASE.get()
            if phase is _ExtractionPhase.EXPLORATION:
                # During exploration, we defer all non-stubbed importing
                # to the rest of the finder/loaders. This is then stashed
                # before re-cleaning sys.modules, so that we can harvest the
                # raw specs for delegated loading.
                return None

            elif (
                phase is _ExtractionPhase.EXTRACTION
                or phase is _ExtractionPhase.PREPARATION
            ):
                return self._get_delegated_spec(
                    base_package, fullname, path, target)

            else:
                logger.warning(
                    'Import %s during invalid extraction phase %s will be '
                    + 'neither hooked nor tracked. You may encounter import '
                    + 'errors. This is almost certainly a bug.',
                    fullname, phase)
                return None

    def _get_delegated_spec(
            self,
            base_package: str,
            fullname: str,
            path: Sequence[str] | None,
            target: ModuleType | None
            ) -> ModuleSpec:
        """Delegated specs are ones where we need the other
        finder/loaders to do the actual importing, but we need to first
        manipulate the import environment in some way, or wrap the
        resulting module.
        """
        # The preparation phase doesn't have this set, hence the default
        module_to_inspect = _MODULE_TO_INSPECT.get(None)
        # Note: ordering here is important. The inspection needs to happen
        # first, because you might have a nostub firstparty module under
        # inspection, and we need to short-circuit the other checks.
        if fullname == module_to_inspect:
            logger.warning(
                'Direct import detected of a module currently under '
                + 'inspection (%s). This is either a circular import, or an '
                + 'error. Downstream code may break. Returning a stub.',
                fullname)
            stub_strategy = _StubStrategy.STUB
        elif (
            fullname in self.nostub_firstparty_modules
            or base_package in self.nostub_packages
        ):
            logger.debug('Returning TRACK stub strategy for %s', fullname)
            stub_strategy = _StubStrategy.TRACK
        else:
            logger.debug('Returning STUB stub strategy for %s', fullname)
            stub_strategy = _StubStrategy.STUB

        raw_module = self.module_stash_nostub_raw[fullname]
        spec = ModuleSpec(
            name=fullname,
            loader=self,
            loader_state=_DelegatedLoaderState(
                fullname=fullname,
                is_firstparty=base_package in self.firstparty_packages,
                delegated_module=raw_module,
                stub_strategy=stub_strategy))

        raw_module_spec = getattr(raw_module, '__spec__', None)
        _clone_spec_attrs(raw_module_spec, spec)

        return spec

    def create_module(self, spec: ModuleSpec) -> None | ModuleType:
        """What we do here depends on the stubbing strategy for the
        module.

        If we're going to ``_StubbingStrategy.STUB`` the module, we
        don't need to do anything special; we can just return None and
        allow normal semantics to happen.

        Otherwise, though, we're going to delegate the loading to a
        different finder/loader, which means we need to do a bit of
        black magic. We need to keep the delegated loader's version of
        the module, and that then, in turn, needs to be the thing that's
        actually used in the ^^delegated^^ ``exec_module``. However, we
        internally need to preserve our own **separate** module object
        for use as the wrapper, which we then use for our own
        ``exec_module``.
        """
        loader_state = spec.loader_state
        if not isinstance(loader_state, _ExtractionLoaderState):
            logger.warning(
                'Missing loader state for %s. This is almost certainly a bug, '
                + 'and may cause stuff to break.', spec.name)
            return None

        # Note: this needs to be ahead of the _DelegatedLoaderState check,
        # since this might be a stubbed firstparty module!
        if loader_state.stub_strategy is _StubStrategy.STUB:
            if loader_state.fullname in self.module_stash_stubbed:
                logger.debug(
                    'Using cached stub module for %s', loader_state.fullname)
                loader_state.from_stash = True
                return self.module_stash_stubbed[loader_state.fullname]

            else:
                logger.debug(
                    'Using default module machinery for stubbed module: %s',
                    loader_state.fullname)

        # This could be either: firstparty inspect, firstparty nostub, or
        # thirdparty nostub.
        else:
            if not isinstance(loader_state, _DelegatedLoaderState):
                # Theoretically impossible. Indicates a bug.
                logger.warning(
                    'Likely bug: delegated/nostub ``_StubStrategy`` without '
                    + '``_DelegatedLoaderState``! Tracking and/or inspection '
                    + 'will break for %s.', loader_state.fullname)

            else:
                # We don't have a stash for inspected modules (because they're
                # only used once), so we only need to check this one stash.
                if loader_state.fullname in self.module_stash_tracked:
                    logger.debug(
                        'Using cached tracking module for %s',
                        loader_state.fullname)
                    loader_state.from_stash = True
                    return self.module_stash_tracked[loader_state.fullname]

                # This can happen either during preparation (for firstparty
                # nostub/tracking modules) or during inspection (for the module
                # being inspected)
                logger.debug(
                    'Creating new delegated module %s for inspection or '
                    + 'tracking (firstparty: %s, under inspection: %s)',
                    loader_state.fullname, loader_state.is_firstparty,
                    loader_state.stub_strategy is _StubStrategy.INSPECT)

                # In all 3 cases, we use the default module creation semantics,
                # because we're only going to be referencing or retrieving
                # source from the already-loaded module, and not relying upon
                # the import system for delegation.
                return None

    def exec_module(self, module: ModuleType):  # noqa: PLR0912
        """Ah, at long last: the final step of the import process.
        We have a module object ready to go and a spec with a
        ``loader_state``, which itself contains a ``stub_strategy``
        telling us what to do. From here on out, it's smooth sailing.

        If we see ``_StubbingStrategy.STUB``, then we're going to just
        add a module-level ``__getattr__`` that creates proxy objects
        on the fly, do a bit of other bookkeeping, and return the
        resulting module. Easy peasy.

        The other two stubbing strategies are a bit more interesting.
        In both cases, we're reliant upon already having established
        the actual module during the exploration phase, which is
        retrieved within ``_get_delegated_spec``.

        In the ``TRACK`` strategy, we need to first let the delegated
        loader ``exec_module`` on its prepared module object from
        ``create_module``. We then wrap this into a tracking module.

        In both of those cases, we need to remember to cache the
        resulting object (and check the ``from_stash`` attribute to
        potentially bypass loading ``exec`` entirely).

        In the ``INSPECT`` strategy, we again let the delegated loader
        ``exec_module`` on its prepared module object. However here, we
        neither cache the module itself (since we only inspect each
        module once), nor do we wrap it. We also don't have to worry
        about setting the tracking registry; this is done within
        ``extract_firstparty``. There is one thing we need to do though:
        we do need to make sure to add the module name to
        ``self.inspected_modules``, so we're absolutely sure
        it gets cleaned up during uninstallation.
        """
        spec = getattr(module, '__spec__', None)
        if (
            spec is None
            or not isinstance(spec.loader_state, _ExtractionLoaderState)
        ):
            logger.error(
                'Missing spec for delegated or stubbed module %s during '
                + '``exec_module``. Will noop; expect import errors!',
                module.__name__)
            return

        loader_state = spec.loader_state
        # We don't need to do anything when returning stashed modules; they've
        # already been populated.
        if loader_state.from_stash:
            # Note: better hope the import system didn't mess around with our
            # stuff in the meantime...
            logger.debug(
                'Delegated module from stash %s; exec_module will noop',
                loader_state.fullname)
            return

        if loader_state.stub_strategy is _StubStrategy.STUB:
            module_name = module.__name__
            logger.debug('Stubbing module: %s', module_name)

            if (
                (real_module := self.module_stash_nostub_raw.get(module_name))
                is not None
            ):
                logger.debug(
                    'Nostub module exists for %s; cloning import attrs',
                    module_name)
                _clone_import_attrs(real_module, spec, dest_module=module)

            else:
                logger.debug(
                    'Lacking nostub module for %s. Assuming nonempty __path__',
                    module_name)
                # Always set this to indicate that it has submodules. We can't
                # know this without a nostub module, so we always just set it.
                # It we don't, attempts to import subpackages will break.
                module.__path__ = []

            # Do this after the above, otherwise the hasattrs while cloning
            # import attrs will return false positives
            module.__getattr__ = partial(
                _stubbed_getattr,
                module_name=module.__name__,
                special_reftype_markers=self.special_reftype_markers)
            self.module_stash_stubbed[loader_state.fullname] = module

        elif isinstance(loader_state, _DelegatedLoaderState):
            real_module = loader_state.delegated_module

            if loader_state.stub_strategy is _StubStrategy.TRACK:
                logger.info(
                    'Wrapping module w/ tracking proxy: %s',
                    loader_state.fullname)
                module = cast(WrappedTrackingModule, module)
                # Firstparty tracking needs to re-exec'd, because the stub
                # state of other firstparty modules has changed. Note that we
                # only do this once, eagerly (during the preparation phase),
                # so that we're not constantly recreating tracking modules.
                if loader_state.is_firstparty:
                    module_source = inspect.getsource(real_module)
                    delegated_module = _clone_import_attrs(real_module, spec)
                    exec(module_source, delegated_module.__dict__)  # noqa: S102

                # Thirdparty tracking can just reuse the real module directly
                # for its attr lookups, because thirdparty stub state never
                # changes.
                else:
                    delegated_module = real_module

                module.__getattr__ = partial(
                    _wrapped_tracking_getattr,
                    module_name=module.__name__,
                    src_module=delegated_module)
                module._docnote_extract_src_module = delegated_module
                self.module_stash_tracked[loader_state.fullname] = module

            # See note in extract_firstparty for the reasoning here.
            elif loader_state.stub_strategy is _StubStrategy.INSPECT:
                raise ValueError(
                    'Cannot directly import modules under inspection!',
                    loader_state.fullname)

            else:
                logger.error(
                    'Unknown stub strategy for delegated module %s during '
                    + '``exec_module``! Will noop; expect import errors!',
                    loader_state.fullname)
                return

            # We don't want to copy the attribute, because it doesn't have
            # semantic meaning for us. However, we do want to make sure that
            # its (non-)existence matches the original, since it has meaning
            # for the import system
            if hasattr(real_module, '__path__'):
                module.__path__ = []

        else:
            logger.error(
                'Wrong loader state type for delegated or stubbed module %s '
                + 'during ``exec_module``. Will noop; expect import errors!',
                loader_state.fullname)
            return

        # This makes debugging edge cases easier
        setattr(
            module,
            MODULE_ATTRNAME_STUBSTRATEGY,
            loader_state.stub_strategy)

    def __post_init__(self):
        # Do this manually instead of via .update() so that we don't overwrite
        # any explicit values given there
        for crossref, marker in GLOBAL_REFTYPE_MARKERS.items():
            if crossref not in self.special_reftype_markers:
                self.special_reftype_markers[crossref] = marker


def _clone_import_attrs(
        src_module: ModuleType,
        spec: ModuleSpec,
        *,
        dest_module: ModuleType | None = None
        ) -> ModuleType:
    """Given an existing module, this creates a clone of that module,
    with ONLY the import-specific values populated. This allows us to
    then exec into that module's namespace, resulting in an alternate
    version of the module, which is useful when you need to create a
    new version of the module with a different stubbing status.
    """
    if dest_module is None:
        logger.debug(
            'No module passed to _clone_import_attrs; Manually creating one '
            + 'to bypass importlib internals: %s',
            src_module.__name__)
        # We explicitly don't want to use module_from_spec here, because we
        # want to maintain absolute control over which parts are set and
        # which ones aren't, and we're about to copy over a bunch of it
        # from the src_module anyways
        dest_module = ModuleType(src_module.__name__)
        dest_module.__loader__ = spec.loader
        dest_module.__spec__ = spec
    elif not hasattr(dest_module, '__spec__'):
        dest_module.__spec__ = spec
        dest_module.__loader__ = spec.loader

    if hasattr(src_module, '__package__'):
        dest_module.__package__ = src_module.__package__
    elif hasattr(dest_module, '__package__'):
        delattr(dest_module, '__package__')

    if hasattr(src_module, '__path__'):
        dest_module.__path__ = []
    elif hasattr(dest_module, '__path__'):
        delattr(dest_module, '__path__')

    if hasattr(src_module, '__file__'):
        dest_module.__file__ = src_module.__file__
    elif hasattr(dest_module, '__file__'):
        delattr(dest_module, '__file__')

    return dest_module


def _clone_spec_attrs(
        src_spec: ModuleSpec | None,
        dest_spec: ModuleSpec
        ) -> None:
    """This fixes up the importlib-specific spec attributes that are
    critical to its internals:

    ++  ``submodule_search_locations``:

        This gets used by the import system to deduce whether or
        not the file is a "package" in importlib parlance, ie, if
        it has submodules or not. If you forget this, it will affect
        the calculation of spec.parent, which will (in future
        versions of python) break relative imports. (Currently it's
        superceded by the module's ``__package__`` attribute, but
        this behavior is deprecated).

    ++  ``origin``:

        I'm honestly not sure what this gets used for, but I've had
        enough bad experiences with innards-fiddling inside of
        importlib that I'd rather be safe than sorry.
    """
    # Can't use hasattr here; it will always exist (though might already
    # be None)!
    if (
        getattr(src_spec, 'submodule_search_locations', None)
        is not None
    ):
        # Delegated specs are always, by definition, a wrapper of some
        # sorts, so we don't want to copy the value over, but we do want
        # to make sure the existence of the attr is the same.
        dest_spec.submodule_search_locations = []

    if (origin := getattr(src_spec, 'origin', None)) is not None:
        dest_spec.origin = origin


@contextmanager
def _activatate_tracking_registry(registry: TrackingRegistry):
    """This sets up a fresh tracking registry for use during extraction.

    Note that we use a different one of these for every time we load a
    firstparty module, because we want to be as precise as possible with
    avoiding duplicate constants (for example, multiple modules using
    bools).
    """
    if _ACTIVE_TRACKING_REGISTRY.get(None) is not None:
        raise RuntimeError(
            'Cannot have multiple activated tracking registries!')

    ctx_token = _ACTIVE_TRACKING_REGISTRY.set(registry)
    try:
        yield
    finally:
        _ACTIVE_TRACKING_REGISTRY.reset(ctx_token)


@dataclass(slots=True, kw_only=True)
class _ExtractionLoaderState:
    """
    """
    fullname: str
    is_firstparty: bool
    stub_strategy: _StubStrategy
    from_stash: bool = False

    @property
    def toplevel_package(self) -> str:
        return self.fullname.partition('.')[0]


@dataclass(slots=True, kw_only=True)
class _DelegatedLoaderState(_ExtractionLoaderState):
    """We use this partly as a container for ``loader_state``, and
    partly as a way to easily detect that a module was created via the
    delegated/alt path.
    """
    delegated_module: ModuleType


def _wrapped_tracking_getattr(
        name: str,
        *,
        module_name: str,
        src_module: ModuleType
        ) -> Any:
    """Okay, yes, we could create our own module type. Alternatively,
    we could just inject a module.__getattr__!

    This returns the original object from the src_module, but before
    doing so, it records the module name and attribute name within
    the registry.

    If we encounter a repeated import of the same object, but with a
    different source, then we overwrite the registry value with None to
    indicate that we no longer know definitively where the object came
    from.
    """
    logger.debug(
        'Detected attribute access at wrapped tracking module %s:%s; '
        + 'delegating to %s (id=%s)',
        module_name, name, src_module, id(src_module))
    registry = _ACTIVE_TRACKING_REGISTRY.get(None)
    src_object = getattr(src_module, name)
    obj_id = id(src_object)
    tracked_src = (module_name, name)

    if registry is None:
        logger.debug('No tracking active for %s:%s', module_name, name)
    else:
        logger.debug('Tracking import for %s:%s', module_name, name)
        # We use None to indicate that there's a conflict within the retrieval
        # imports we've encountered, so we can't use it as a stand-in for
        # missing stuff.
        existing_record = registry.get(obj_id, Singleton.MISSING)
        if existing_record is Singleton.MISSING:
            registry[obj_id] = tracked_src

        # Note: we only need to overwrite if it isn't already none; otherwise
        # we can just skip it. None is a sink state, a black hole.
        elif (
            existing_record is not None
            and existing_record is not tracked_src
            and existing_record != tracked_src
        ):
            registry[obj_id] = None

    return src_object


def _stubbed_getattr(
        name: str,
        *,
        module_name: str,
        special_reftype_markers: dict[Crossref, ReftypeMarker]):
    """Okay, yes, we could create our own module type. Alternatively,
    we could just inject a module.__getattr__!

    This replaces every attribute access (regardless of whether or not
    it exists on the true source module; we're relying upon type
    checkers to ensure that) with a reftype.
    """
    # Note that with firstparty packages, we inject the real __all__ from
    # the nostub module, so this condition should never be hit.
    if name == '__all__':
        logger.warning(
            'Star imports from stubbed thirdparty modules (or firstparty '
            + 'modules lacking an ``__all__``) are unsupported (consult the '
            + 'docs for more details). As a fallback, we return an empty '
            + '``__all__``; expect downstream code to break. (%s)',
            module_name)
        return []

    to_reference = Crossref(module_name=module_name, toplevel_name=name)

    special_reftype = special_reftype_markers.get(to_reference)
    if special_reftype is None:
        logger.debug('Returning normal reftype for %s', to_reference)
        return make_crossreffed(module=module_name, name=name)

    elif special_reftype is ReftypeMarker.METACLASS:
        logger.debug('Returning metaclass reftype for %s.', to_reference)
        return make_metaclass_crossreffed(module=module_name, name=name)

    else:
        # This is just blocked on having a decorator flavor added to
        # reftypes. Should be straightforward, but I want to limit the scope
        # of the current code push to just a refactor.
        raise NotImplementedError(
            'Other special metaclass reftypes not yet supported.')


def is_wrapped_tracking_module(
        module: ModuleType
        ) -> TypeGuard[WrappedTrackingModule]:
    return (
        isinstance(module, ModuleType)
        and hasattr(module, '_docnote_extract_src_module'))


class _WrappedTrackingModuleBase(Protocol):
    _docnote_extract_src_module: ModuleType


class WrappedTrackingModule(ModuleType, _WrappedTrackingModuleBase):
    """This is really just intended for use as a pseudo-protocol, since
    protocols can't inherit from concrete base classes, but we need
    something that's the intersection between a moduletype and a
    WrappedTrackingModuleBase.
    """
    # Including this to silence type errors when we create these manually for
    # testing purposes
    _docnote_extract_src_module: ModuleType


class _ModulePostExtractionBase(Protocol):
    _docnote_extract_import_tracking_registry: TrackingRegistry


class ModulePostExtraction(ModuleType, _ModulePostExtractionBase):
    """This is really just intended for use as a pseudo-protocol, since
    protocols can't inherit from concrete base classes, but we need
    something that's the intersection between a moduletype and a
    ModulePostExtractionBase.
    """
    # Including this to silence type errors when we create these manually for
    # testing purposes
    _docnote_extract_import_tracking_registry: TrackingRegistry


def is_module_post_extraction(
        module: ModuleType
        ) -> TypeGuard[ModulePostExtraction]:
    return (
        isinstance(module, ModuleType)
        and hasattr(module, '_docnote_extract_import_tracking_registry'))


@wraps(dataclass)
def _dataclass_decorator_wrapper(maybe_cls: type | None = None, **kwargs):
    if maybe_cls is None:
        def decorator[T: type](cls: T) -> T:
            docstr_before = cls.__doc__
            dataclassed = dataclass(**kwargs)(cls)
            dataclassed.__doc__ = docstr_before
            return dataclassed

        return decorator

    cls = maybe_cls
    docstr_before = cls.__doc__
    dataclassed = dataclass(cls)
    dataclassed.__doc__ = docstr_before
    return dataclassed


def _patched_dataclass_getattr(name: str):
    if name == 'dataclass':
        return _dataclass_decorator_wrapper
    else:
        return getattr(dataclasses, name)
