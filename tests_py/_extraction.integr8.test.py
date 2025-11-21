from __future__ import annotations

import importlib
import sys
from dataclasses import is_dataclass
from importlib.machinery import ModuleSpec
from typing import cast

from docnote import ReftypeMarker

from docnote_extract._extraction import StubsConfig
from docnote_extract._extraction import TrackingImportSource
from docnote_extract._extraction import _ExtractionFinderLoader
from docnote_extract._extraction import _ExtractionPhase
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import has_crossreffed_base
from docnote_extract.crossrefs import has_crossreffed_metaclass
from docnote_extract.crossrefs import is_crossreffed

import docnote_extract_testpkg
import docnote_extract_testpkg._hand_rolled
import docnote_extract_testutils
from docnote_extract_testutils.fixtures import mocked_extraction_discovery
from docnote_extract_testutils.fixtures import purge_cached_testpkg_modules
from docnote_extract_testutils.fixtures import set_inspection
from docnote_extract_testutils.fixtures import set_phase


class TestExtractionFinderLoader:

    @set_inspection('')
    @set_phase(_ExtractionPhase.EXTRACTION)
    @purge_cached_testpkg_modules
    def test_uninstall_also_removes_imported_modules(self):
        """Uninstalling the import hook must also remove any stubbed
        modules from sys.modules.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            module_stash_raw={
                'docnote_extract_testpkg': docnote_extract_testpkg},
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset()))
        assert 'docnote_extract_testpkg' not in sys.modules

        floader.install()
        try:
            importlib.import_module('docnote_extract_testpkg')
            assert 'docnote_extract_testpkg' in sys.modules
        finally:
            floader.uninstall()

        assert 'docnote_extract_testpkg' not in sys.modules

    @set_inspection('')
    @set_phase(_ExtractionPhase.EXTRACTION)
    @purge_cached_testpkg_modules
    def test_stubbs_returned_after_installation(self):
        """To ensure park security, imports after installation must be
        escorted by at least one host. Errr, wait, got caught in a
        reverie, wrong thing. After installing the import hook,
        importing a not-under-inspection module must return a stubbed
        module. After uninstallation, the normal module must be
        returned.
        """
        # Empty here just because we want to test stuff against testpkg
        floader = _ExtractionFinderLoader(
            frozenset(),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset()))

        floader.install()
        try:
            floader._stash_prehook_modules()
            try:
                # NOTE: NOT THE TESTPKG! We're making sure that other things
                # get stubbed out.
                testpkg = importlib.import_module('docnote_extract_testutils')
                assert 'docnote_extract_testutils' in sys.modules
                assert is_crossreffed(testpkg)
            finally:
                floader._unstash_prehook_modules()
        finally:
            floader.uninstall()

        assert 'docnote_extract_testutils' not in sys.modules
        assert testpkg is not docnote_extract_testutils
        testpkg_reloaded = importlib.import_module('docnote_extract_testutils')
        assert testpkg_reloaded is not testpkg
        assert not is_crossreffed(testpkg_reloaded)

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.imports_3p_metaclass'])
    @purge_cached_testpkg_modules
    def test_inspection_works_with_3pmetaclasses(self):
        """After installing the import hook and while inspecting a
        module, modules that create classes using imported third-party
        metaclasses must still be inspectable.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),
            special_reftype_markers={
                Crossref(
                    module_name='docnote_extract_testutils.for_handrolled',
                    toplevel_name='ThirdpartyMetaclass'):
                ReftypeMarker.METACLASS})

        retval = floader.discover_and_extract()

        to_inspect = retval[
            'docnote_extract_testpkg._hand_rolled.imports_3p_metaclass']
        assert not is_crossreffed(to_inspect)
        assert not is_crossreffed(to_inspect.Uses3pMetaclass)
        assert has_crossreffed_metaclass(to_inspect.Uses3pMetaclass)

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.defines_1p_metaclass',
        'docnote_extract_testpkg._hand_rolled.imports_1p_metaclass'])
    @purge_cached_testpkg_modules
    def test_inspection_works_with_1pmetaclasses(self):
        """After installing the import hook and while inspecting a
        module, modules that create classes using imported first-party
        metaclasses declared with docnote configs must still be
        inspectable.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()

        to_inspect = retval[
            'docnote_extract_testpkg._hand_rolled.imports_1p_metaclass']
        assert not is_crossreffed(to_inspect)
        assert not is_crossreffed(to_inspect.Uses1pMetaclass)
        assert has_crossreffed_metaclass(to_inspect.Uses1pMetaclass)

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.subclasses_3p_class'])
    @purge_cached_testpkg_modules
    def test_inspection_works_with_subclass(self):
        """After installing the import hook and while inspecting a
        module, modules that create classes that inherit from
        third-party base classes must still be inspectable.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()

        to_inspect = retval[
            'docnote_extract_testpkg._hand_rolled.subclasses_3p_class']
        assert not is_crossreffed(to_inspect)
        assert not is_crossreffed(to_inspect.Uses3pBaseclass)
        assert has_crossreffed_base(to_inspect.Uses3pBaseclass)

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.imports_from_parent'])
    @purge_cached_testpkg_modules
    def test_parent_imports_stubbed(self):
        """After installing the import hook and while inspecting a
        module, imports from that module's parent module must still be
        stubbed.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()

        to_inspect = retval[
            'docnote_extract_testpkg._hand_rolled.imports_from_parent']
        assert not is_crossreffed(to_inspect)
        assert is_crossreffed(to_inspect.SOME_CONSTANT)

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.imports_from_parent'])
    @purge_cached_testpkg_modules
    def test_tracking_imports(self):
        """After installing the import hook and while inspecting a
        module, imports from a nostub module must nonetheless be
        tracked.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                # CRITICAL: this is what makes this test unique!
                firstparty_blocklist=frozenset({
                    'docnote_extract_testpkg._hand_rolled'}),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()
        to_inspect = retval[
            'docnote_extract_testpkg._hand_rolled.imports_from_parent']

        registry = to_inspect.__docnote_extract_metadata__.tracking_registry
        assert id(to_inspect.RENAMED_SENTINEL) in registry
        assert registry[id(to_inspect.RENAMED_SENTINEL)] == (
            'docnote_extract_testpkg._hand_rolled',
            'SOME_SENTINEL')

        # Okay, so... I'd like to just check that call_count == 2. Except
        # it isn't, and it never will be (unless the implementation of
        # importlib._bootstrap._handle_fromlist changes, which is why we're
        # not just going to check that call_count == 4).
        # Because it turns out that the implementation of the import system
        # itself does a hasattr() check against the tracking module, which
        # in turn excercises the __getattr__ hook on the tracking module,
        # causing an extra call for each of the imported attributes. So...
        # yeah. It actually didn't take me ^^that long^^ to figure out what
        # was going on, but I did end up, yknow, patching out internal
        # implementation details of the import system and doing some random
        # stack dumps, so... well anyways. Machete mode debugging and all that.
        # Instead, for robustness, we'll just make sure that we had tracking
        # lookups against the correct attributes.
        targets = set(registry.values())
        assert len(targets) == 2
        target1, target2 = targets
        assert target1 is not None and target2 is not None
        targets = cast(TrackingImportSource, targets)
        module_targets = {target[0] for target in targets}
        attr_targets = {target[1] for target in targets}

        assert attr_targets == {'SOME_CONSTANT', 'SOME_SENTINEL'}
        assert module_targets == {'docnote_extract_testpkg._hand_rolled'}

        assert not is_crossreffed(to_inspect)
        assert not is_crossreffed(to_inspect.SOME_CONSTANT)
        assert not is_crossreffed(to_inspect.RENAMED_SENTINEL)

    @mocked_extraction_discovery([
            'docnote_extract_testpkg',
            'docnote_extract_testpkg._hand_rolled',
            'docnote_extract_testpkg._hand_rolled.noteworthy',
            'docnote_extract_testpkg._hand_rolled.relativity',
            'docnote_extract_testpkg._hand_rolled.uses_import_names',])
    @purge_cached_testpkg_modules
    def test_relative_imports_stubbed(self):
        """After installing the import hook and while inspecting a
        module, relative imports must A) work and B) still be
        stubbed.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()

        to_inspect = retval['docnote_extract_testpkg._hand_rolled.relativity']
        assert not is_crossreffed(to_inspect)
        assert is_crossreffed(to_inspect.SOME_CONSTANT)
        assert is_crossreffed(to_inspect.ROOT_VAR)
        assert is_crossreffed(to_inspect.func_with_config)
        assert is_crossreffed(to_inspect.uses_import_names)

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.uses_import_names'])
    @purge_cached_testpkg_modules
    def test_import_names_available(self):
        """After installing the import hook and while inspecting a
        module, import-relevant names like ``__file__`` and ``__name__``
        must exist.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()

        mod_name = 'docnote_extract_testpkg._hand_rolled.uses_import_names'
        to_inspect = retval[mod_name]
        assert not is_crossreffed(to_inspect)
        assert isinstance(to_inspect.FILE, str)
        assert isinstance(to_inspect.SPEC, ModuleSpec)
        assert to_inspect.NAME == mod_name

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.uses_dataclasses'])
    @purge_cached_testpkg_modules
    def test_dataclass_docstring_strip(self):
        """Extracting values from dataclasses should strip the
        automatically-generated docstring.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()

        mod_name = 'docnote_extract_testpkg._hand_rolled.uses_dataclasses'
        to_inspect = retval[mod_name]
        assert is_dataclass(to_inspect.DataclassWithoutDocstring)
        assert to_inspect.DataclassWithoutDocstring.__doc__ is None

    @mocked_extraction_discovery([
        'docnote_extract_testpkg',
        'docnote_extract_testpkg._hand_rolled',
        'docnote_extract_testpkg._hand_rolled.uses_dataclasses'])
    @purge_cached_testpkg_modules
    def test_dataclass_with_kw_only_works(self):
        """Dataclasses making use of kw_only must extract without
        error.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)

        retval = floader.discover_and_extract()

        mod_name = 'docnote_extract_testpkg._hand_rolled.uses_dataclasses'
        to_inspect = retval[mod_name]
        assert is_dataclass(to_inspect.DataclassWithKwOnlyAndDefaults)
