from __future__ import annotations

import dataclasses
import inspect
import logging
import sys
import typing
from collections.abc import Collection
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

from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import make_crossreffed
from docnote_extract.crossrefs import make_decorator_2o_crossreffed
from docnote_extract.crossrefs import make_decorator_crossreffed
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
    # Note: py is also from pytest
    'py',
    'pytest',
    '_pytest',
    '_virtualenv',
    'typing_extensions',
}
_EXTRACTION_PHASE: ContextVar[_ExtractionPhase] = ContextVar(
    '_EXTRACTION_PHASE')

_MODULE_TO_INSPECT: ContextVar[str] = ContextVar('_MODULE_TO_INSPECT')
_ACTIVE_TRACKING_REGISTRY: ContextVar[TrackingRegistry] = ContextVar(
    '_ACTIVE_TRACKING_REGISTRY')
MODULE_ATTRNAME_STUBSTRATEGY = '_docnote_extract_stub_strat'
_CLONABLE_IMPORT_ATTRS = {
    '__package__',
    '__path__',
    '__file__',
}

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

    special_reftype_markers: dict[Crossref, ReftypeMarker] = field(
        default_factory=dict)
    stubs_config: StubsConfig

    module_stash_prehook: dict[str, ModuleType] = field(
        default_factory=dict, repr=False)
    # Internals of the module are real, but any third-party stubs strategies
    # are applied. This is what we use for constructing specs, and
    # for modules that need real versions of objects. Note that this can also
    # contain third-party modules -- it all depends on the stubs strategies!
    module_stash_raw: dict[str, ModuleType] = field(
        default_factory=dict, repr=False)
    # This is used for marking things dirty.
    inspected_modules: set[str] = field(default_factory=set, repr=False)
    # This is set immediately after stashing prehook modules to determine what
    # the minimum set of known-clean modules is, so we can revert to this state
    # between extractions
    known_clean_modules: set[str] = field(default_factory=set, repr=False)

    def discover_and_extract(self) -> dict[str, ModulePostExtraction]:
        ctx_token = _EXTRACTION_PHASE.set(_ExtractionPhase.HOOKED)
        try:
            logger.info('Stashing prehook modules and installing import hook.')
            self._stash_prehook_modules()
            self.known_clean_modules.update(sys.modules)
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
            self._stash_raw_modules()
            # Note: we don't need to clean up anything here, because we do it
            # at the start of every iteration during extraction.

            logger.info('Starting extraction phase.')
            _EXTRACTION_PHASE.set(_ExtractionPhase.EXTRACTION)
            retval: dict[str, ModulePostExtraction] = {}
            for module_name in firstparty_names:
                self.cleanup_sys(self._get_all_dirty_modules())
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

    def _stash_raw_modules(self):
        """This checks sys.modules for any firstparty or nostub modules,
        adding references to them within ``module_stash_raw``.
        """
        for fullname, module in sys.modules.items():
            package_name, _, _ = fullname.partition('.')
            if (
                # Note that this excludes stdlib and other bypasses
                self.stubs_config.use_stub_strategy(fullname) is not None
                # Note that this condition is only relevant if we're extracting
                # something that is contained in the bypass list, ex docnote
                # itself
                or package_name in self.firstparty_packages
            ):
                self.module_stash_raw[fullname] = module

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
                # This is extremely delicate. On the one hand, we need to make
                # sure to respect the import semantics. That means that the
                # parent modules of a particular module must always be
                # available on sys.modules, even if only partially-initialized.
                # But at the same time, we need to work around the fact that
                # the import system will opaquely modify the namespace of
                # modules as their children are imported (to add the relname
                # of the child into its parent's __dict__). This means that we
                # have to be extremely careful about any pre-work we do (in
                # fact, it's part of the reason we have to recreate stubs and
                # tracking modules every single time we inspect a module).
                # Simultaneously, you have libraries -- including stdlib ones
                # like dataclasses -- relying upon the module in question being
                # available in sys.modules. So you can't bypass the import
                # system entirely.
                # So although yes, we are "just" importing the module here and
                # relying upon our import hook to correctly detect it being the
                # module under inspection, this only works because of the
                # extremely delicate dance of everything else we're doing here.
                logger.info(
                    'Re-IMPORTing module for inspection: %s', module_name)
                try:
                    extracted_module = cast(
                        ModulePostExtraction,
                        import_module(module_name))
                finally:
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

    def _reexec_tracking_wrapper(
            self,
            module_name: str,
            spec: ModuleSpec,
            module_source: str,
            raw_module: ModuleType,
            dest_module: ModuleType
            ) -> ModuleType:
        """First-party tracking modules need to be re-executed for
        every module inspection, because (by definition) the stub state
        of at least one module (the inspectee) will have changed, and we
        need to maintain consistent state across all modules.

        However, this is non-trivial for two reasons:
        1.. we need to undo the ``typing.TYPE_CHECKING`` override done
            as part of inspection, lest we encounter circular imports
        2.. some packages, including extremely popular stdlib libraries
            ^^like **dataclasses**^^, do super funky shenanigans with
            importing, and (when these dependencies are exercised at
            import time, as they are with decorators like
            ``@dataclass``) this can break execution.

        The general strategy for the second point is:
        ++  create a snapshot of the prepared namespace for the tracking
            (not the delegated!) module before re-exec'ing its source
            code
        ++  re-exec the source code into the prepared namespace for the
            tracking (not the delegated!) module
        ++  copy everything added by exec into the tracking module, into
            the delegated module
        ++  ``clear()`` the tracking module's ``__dict__``, and then
            restore it from the earlier snapshot

        > Re: dataclasses
            This is a very specific edge case, but it's important to
            understand:
            ++  ``from __future__ import annotations`` converts all
                annotations into strings
            ++  dataclasses needs to look for some module-level magic
                values as type hints, notably ``KW_ONLY``
            ++  instead of calling the other typing-specific facilities
                for resolving stringified type hints, dataclasses has
                its own implementation. This manually checks for the
                module in ``sys.modules``, and then looks directly at
                that module's ``__dict__``. If at any time it fails a
                lookup, it short-circuits, assuming that the stringified
                value is irrelevant to tthe dataclass transform
            ++  note that this bypasses the module ``__getattr__`` hook!

            Therefore, for dataclasses to work, **during tracking module
            re-exec**, the following must be true:
            ++  there must be a module in ``sys.modules`` for the module
                fullname we want to track
            ++  the real imported objects have to exist in that module's
                namespace during ``exec`` time
            ++  those objects must be the same (literally the same, ie
                same ``id`` and ``(x is y) is True``) as the ones
                returned during the actual tracking imports at
                inspection time
            ++  the tracking module ``__dict__`` must be **missing** a
                name in order for the ``__getattr__`` hook to have an
                effect
        """
        logger.info('Re-exec-ing module for tracking: %s', module_name)

        # Note: yes, we do indeed want to create a new module
        # object here! Remember, this is for the **delegated**
        # module (ie, the module we do lookups against), and not
        # the raw module.
        delegated_module = _clone_import_attrs(raw_module, spec)
        # Save this for later
        prepped_dict = {**dest_module.__dict__}

        # We need to first undo any changes we might have made to
        # the type checking flag as part of re-execing the current
        # inspectee. This prevents us getting stuck in an import
        # cascade that ultimately turns into a circular import.
        existing_typecheck_flag = typing.TYPE_CHECKING
        try:
            typing.TYPE_CHECKING = False
            exec(module_source, dest_module.__dict__)  # noqa: S102
        except Exception:
            # The traceback we get for this is miserable, so double-log so that
            # we get more info (at least the damn module name, seriously)
            logger.exception('Failed to re-exec %s', module_name)
            raise
        finally:
            typing.TYPE_CHECKING = existing_typecheck_flag

        for key, value in dest_module.__dict__.items():
            if key not in delegated_module.__dict__:
                delegated_module.__dict__[key] = value

        dest_module.__dict__.clear()
        dest_module.__dict__.update(prepped_dict)

        return delegated_module

    def _reexec_inspectee_module(
            self,
            module_name: str,
            module_source: str,
            dest_namespace: dict[str, Any]):
        """Re-exec'ing the module under inspection is a bit more
        complicated than it first might seem, because we need to make
        any names hidden behind ``if typing.TYPE_CHECKING:`` blocks
        available for analysis after extraction. We also need to avoid
        having non-identical (as in, same ``id()``) objects between any
        quasi-circular deps hidden behind those blocks, so we can't
        simply call exec twice and overwrite the values there.

        Instead, our strategy is:
        1.. exec the module normally, with typing.TYPE_CHECKING set to
            false. This will also carry through to any downstream
            imports.
        2.. re-exec the module into a separate, temporary dict, setting
            typing.TYPE_CHECKING to true. **This needs to then be
            overridden in any downstream imports.**
        3.. add any missing values discovered in the second execution
            back to the destination namespace
        """
        logger.info(
            'Re-EXECing module for inspection: %s (normal namespace)',
            module_name)
        # Now we can re-exec with the normal TYPE_CHECKING flag.
        try:
            exec(module_source, dest_namespace)  # noqa: S102
        except Exception:
            # The traceback we get for this is miserable, so double-log so that
            # we get more info (at least the damn module name, seriously)
            logger.exception('Failed to re-exec %s', module_name)
            raise

        logger.info(
            'Re-EXECing module for inspection: %s (expanded namespace)',
            module_name)
        # Copy over the entire existing namespace from the module, including
        # anything that was already defined there. This ensures that it is
        # fully populated with the real objects defined there before we expand
        # the namespace. Yes, those values will then be overwritten when we
        # execute the body of the module, but we simply won't copy those keys
        # back to the dest_namespace!
        expanded_namespace = {**dest_namespace}
        typing.TYPE_CHECKING = True
        try:
            exec(module_source, expanded_namespace)  # noqa: S102
        except Exception:
            # The traceback we get for this is miserable, so double-log so that
            # we get more info (at least the damn module name, seriously)
            logger.exception('Failed to re-exec %s', module_name)
            raise
        finally:
            typing.TYPE_CHECKING = False

        for key, value in expanded_namespace.items():
            if key not in dest_namespace:
                dest_namespace[key] = value

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
        module_names: set[str] = set()

        for module_name in sys.modules:
            package_name, _, _ = module_name.partition('.')
            if (
                package_name not in NOHOOK_PACKAGES
                and package_name not in sys.stdlib_module_names
            ):
                module_names.add(module_name)

        module_names.difference_update(self.known_clean_modules)
        module_names.difference_update(UNPURGEABLE_MODULES)
        return module_names

    def _stash_prehook_modules(self):
        """This checks all of sys.modules, stashing and removing
        anything that isn't stdlib or a thirdparty bypass package.
        """
        prehook_module_names = sorted(sys.modules)
        for prehook_module_name in prehook_module_names:
            package_name, _, _ = prehook_module_name.partition('.')

            if package_name == 'dataclasses':
                logger.debug('Shimming dataclasses in sys.modules')
                self.module_stash_prehook['dataclasses'] = dataclasses
                patched_dataclasses = _clone_import_attrs(
                    src_module=dataclasses,
                    spec=dataclasses.__spec__)
                patched_dataclasses.__getattr__ = _patched_dataclass_getattr
                sys.modules['dataclasses'] = patched_dataclasses
                continue

            stub_strategy = self.stubs_config.use_stub_strategy(
                prehook_module_name)

            if stub_strategy is not None:
                logger.debug(
                    'Popping %s from sys.modules for stash',
                    prehook_module_name)
                prehook_module = sys.modules.pop(prehook_module_name)

                self.module_stash_prehook[prehook_module_name] = prehook_module

    def _unstash_prehook_modules(self):
        for name, module in self.module_stash_prehook.items():
            logger.info('Restoring prehook module %s', name)
            sys.modules[name] = module

    def _prepare_stub_or_tracking_module(
            self,
            module_name: str,
            spec: ModuleSpec,
            target_module: ModuleType
            ):
        """We use this to construct stub and/or tracking wrappers for
        modules. This is done on an as-needed basis, as modules are
        imported during the re-execution of the module-under-inspection.

        Tracking/stub modules are single use; they are discarded as soon
        as the inspectee module is fully extracted. This prevents issues
        with inconsistent stubbing state of circular import loops, and
        other such ^^incredibly difficult to diagnose and/or fix^^
        errors.
        """
        raw_module = self.module_stash_raw.get(module_name)
        if raw_module is None:
            logging.debug(
                'No raw module found for %s. This is expected if the module '
                + 'will be a stubbed, uninstalled third-party dep, but in '
                + 'other scenarios this would indicate an error. At any rate, '
                + "we'll be assuming a nonempty __path__.",
                module_name)
            # Always set this to indicate that it has submodules. We can't
            # know this without a nostub module, so we always just set it.
            # It we don't, attempts to import subpackages will break.
            target_module.__path__ = []

            return

        logger.debug(
            'Raw module exists for %s; cloning import attrs', module_name)
        _clone_import_attrs(raw_module, spec, dest_module=target_module)

        # Note: we ONLY want to do this for modules that define an
        # __all__. If you're doing star intra-project starred imports
        # and **not** defining an __all__, we really can't help you.
        # Any workaround is inherently super dangerous, because we
        # might, for example, accidentally clobber the importing
        # module's __name__, which would break relative imports in
        # an extremely-difficult-to-debug way.
        if hasattr(raw_module, '__all__'):
            # pyright doesn't like modules not necessarily having an
            # __all__, hence the ignore directive
            target_module.__all__ = tuple(raw_module.__all__)  # type: ignore

        # We don't want to copy the attribute, because it doesn't have
        # semantic meaning for us. However, we do want to make sure that
        # its (non-)existence matches the original, since it has meaning
        # for the import system
        if hasattr(raw_module, '__path__'):
            target_module.__path__ = []

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
        stub_strategy = self.stubs_config.use_stub_strategy(fullname)
        base_package, *_ = fullname.split('.')
        if stub_strategy is None:
            logger.debug(
                'Bypassing wrapping for %s, either as stdlib module or via '
                + 'hard-coded third party nohook package %s',
                fullname, base_package)
            return None

        # If a stub strategy is active for a thirdparty package, it will always
        # return a stub (as long as the import hook is installed), regardless
        # of extraction phase.
        # Note: simple truthiness works here because we already filtered out
        # the Nones (just above!)
        if base_package not in self.firstparty_packages and stub_strategy:
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
            stub_strategy = _StubStrategy.INSPECT
        # Note that truthiness is okay because we already returned a None
        # spec for anything that is a None stub strategy.
        elif self.stubs_config.use_stub_strategy(fullname):
            logger.debug('Returning STUB stub strategy for %s', fullname)
            stub_strategy = _StubStrategy.STUB
        else:
            logger.debug('Returning TRACK stub strategy for %s', fullname)
            stub_strategy = _StubStrategy.TRACK

        raw_module = self.module_stash_raw[fullname]
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
        """Since we always create fresh tracking/stubbing modules for
        each inspectee, we really don't have anything special to do
        here; we can simply rely upon the default stdlib import
        mechanics to create the module object for us, and then populate
        it during ``exec_module``.
        """
        return None

    def exec_module(self, module: ModuleType):
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

        module_name = module.__name__
        self._prepare_stub_or_tracking_module(
            module_name,
            spec,
            module)

        loader_state = spec.loader_state
        if loader_state.stub_strategy is _StubStrategy.STUB:
            logger.debug('Stubbing module: %s', module_name)
            # Do this after the above, otherwise the hasattrs while cloning
            # import attrs will return false positives
            module.__getattr__ = partial(
                _stubbed_getattr,
                module_name=module_name,
                special_reftype_markers=self.special_reftype_markers)

        elif isinstance(loader_state, _DelegatedLoaderState):
            real_module = loader_state.delegated_module

            if loader_state.stub_strategy is _StubStrategy.TRACK:
                logger.info(
                    'Wrapping module w/ tracking proxy: %s',
                    loader_state.fullname)
                module = cast(WrappedTrackingModule, module)

                # Firstparty tracking needs to re-exec'd, because the stub
                # state of other firstparty modules may have changed, and there
                # might be downstream imports of those modules.
                if loader_state.is_firstparty:
                    module_source = inspect.getsource(real_module)
                    delegated_module = self._reexec_tracking_wrapper(
                        module_name,
                        spec,
                        module_source,
                        real_module,
                        module)

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

            # See note in extract_firstparty for the reasoning here.
            elif loader_state.stub_strategy is _StubStrategy.INSPECT:
                module_source = inspect.getsource(real_module)
                self._reexec_inspectee_module(
                    module_name,
                    module_source,
                    module.__dict__)

            else:
                logger.error(
                    'Unknown stub strategy for delegated module %s during '
                    + '``exec_module``! Will noop; expect import errors!',
                    loader_state.fullname)
                return

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

    # This is a little bit hard to read, but we're checking for the import attr
    # on the source module. If we find it, we copy it over. It we don't find
    # it, we check the dest module and delete any existing one there, so that
    # the existence or non-existence matches between them.
    for import_attr_name in _CLONABLE_IMPORT_ATTRS:
        if hasattr(src_module, import_attr_name):
            setattr(
                dest_module, import_attr_name,
                getattr(src_module, import_attr_name))
        elif hasattr(dest_module, import_attr_name):
            delattr(dest_module, import_attr_name)

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
    # These are always correct, so never delegate them.
    if name in _CLONABLE_IMPORT_ATTRS:
        raise AttributeError(name)

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

    elif special_reftype is ReftypeMarker.DECORATOR:
        logger.debug(
            'Returning first-order decorator reftype for %s.', to_reference)
        return make_decorator_crossreffed(module=module_name, name=name)

    elif special_reftype is ReftypeMarker.DECORATOR_SECOND_ORDER:
        logger.debug(
            'Returning second-order decorator reftype for %s.', to_reference)
        return make_decorator_2o_crossreffed(module=module_name, name=name)

    else:
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


@dataclass
class StubsConfig:
    enable_stubs: bool

    # Note: if this is defined, it takes precedence over the others, which are
    # therefore ignored
    global_allowlist: frozenset[str] | None
    # Note: full module name
    firstparty_blocklist: frozenset[str]
    # Note: root package, not individual modules
    thirdparty_blocklist: frozenset[str]

    @classmethod
    def from_gather_kwargs(
            cls,
            enabled_stubs: bool | Collection[str],
            nostub_firstparty_modules: Collection[str] | None,
            nostub_packages: Collection[str] | None,
            ) -> StubsConfig:
        """Does some convenience stuff to construct a stubstate from
        the kwargs used in gathering.
        """
        if nostub_firstparty_modules is None:
            nostub_firstparty_modules = frozenset()
        else:
            nostub_firstparty_modules = frozenset(nostub_firstparty_modules)
        if nostub_packages is None:
            nostub_packages = frozenset()
        else:
            nostub_packages = frozenset(nostub_packages)
        if enabled_stubs is True:
            enable_stubs = True
            global_allowlist = None
        elif enabled_stubs is False:
            enable_stubs = False
            global_allowlist = None
        else:
            enable_stubs = True
            global_allowlist = frozenset(enabled_stubs)

        return cls(
            enable_stubs=enable_stubs,
            global_allowlist=global_allowlist,
            firstparty_blocklist=nostub_firstparty_modules,
            thirdparty_blocklist=nostub_packages)

    def use_stub_strategy(self, module_fullname: str) -> bool | None:
        """Returns True if the passed module fullname should be stubbed,
        False if it should be tracked, and None if it should be
        completely bypassed.
        """
        package_name, _, _ = module_fullname.partition('.')
        if (
            # Note that package_name is correct here; stdlib doesn't add in
            # every submodule.
            package_name in sys.stdlib_module_names
            or package_name in NOHOOK_PACKAGES
        ):
            return None

        if not self.enable_stubs:
            return False

        if self.global_allowlist is None:
            if (
                module_fullname in self.firstparty_blocklist
                or package_name in self.thirdparty_blocklist
            ):
                return False

            return True

        else:
            return module_fullname in self.global_allowlist
