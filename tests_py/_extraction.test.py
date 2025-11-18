import importlib
import sys
from types import ModuleType
from unittest.mock import patch

import pytest
from docnote import ReftypeMarker

from docnote_extract._extraction import _MODULE_TO_INSPECT
from docnote_extract._extraction import GLOBAL_REFTYPE_MARKERS
from docnote_extract._extraction import StubsConfig
from docnote_extract._extraction import _DelegatedLoaderState
from docnote_extract._extraction import _ExtractionFinderLoader
from docnote_extract._extraction import _ExtractionLoaderState
from docnote_extract._extraction import _ExtractionPhase
from docnote_extract._extraction import _stubbed_getattr
from docnote_extract._extraction import _StubStrategy
from docnote_extract._extraction import is_module_post_extraction
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import CrossrefMixin
from docnote_extract.crossrefs import is_crossreffed

import docnote_extract_testpkg
from docnote_extract_testutils.fixtures import set_inspection
from docnote_extract_testutils.fixtures import set_phase


@pytest.fixture
def fresh_unpurgeable_modules():
    """Use this if you want a quick and dirty fixture to control the
    value of the unpurgeable_modules constant.
    """
    unpurgeable_modules = set()
    with patch(
        'docnote_extract._extraction.UNPURGEABLE_MODULES',
        unpurgeable_modules
    ):
        yield unpurgeable_modules


def fake_import_module(name: str) -> ModuleType:
    result = ModuleType(name)
    sys.modules[name] = result
    return result


class TestExtractionFinderLoader:

    def test_stash_firstparty_or_nostub(self):
        """_stash_firstparty_or_nostub_raw must add firstparty and
        nostub packages to the correct stash, but not others.
        """
        import this  # noqa: F401, I001
        import pytest  # noqa: F401
        import docnote_extract_testpkg  # noqa: F401

        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)
        floader._stash_firstparty_or_nostub_raw()
        assert 'this' not in floader.module_stash_nostub_raw
        assert 'pytest' in floader.module_stash_nostub_raw
        assert 'docnote_extract_testpkg' in floader.module_stash_nostub_raw

    @set_phase(_ExtractionPhase.EXTRACTION)
    def test_extract_firstparty(self):
        """extract_firstparty must return a module-post-extraction, it
        must reset the module to inspect before returning, and it must
        remove the module from sys before and after extraction.

        Additionally, it must not call ``import_module``.
        """
        import docnote_extract_testpkg._hand_rolled as raw_module
        assert _MODULE_TO_INSPECT.get(None) is None
        assert 'docnote_extract_testpkg._hand_rolled' in sys.modules

        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),
            module_stash_nostub_raw={
                'pytest': pytest,
                'docnote_extract_testpkg': docnote_extract_testpkg,
                'docnote_extract_testpkg._hand_rolled': raw_module})

        # We're going to patch this out because we want to isolate the internal
        # behavior of the import system from the extraction function
        with patch(
            'docnote_extract._extraction.import_module',
            autospec=True,
        ) as import_module_mock:
            result = floader.extract_firstparty(
                'docnote_extract_testpkg._hand_rolled')

        assert import_module_mock.call_count == 0
        assert is_module_post_extraction(result)
        assert result is not raw_module
        assert result.__name__ == 'docnote_extract_testpkg._hand_rolled'
        assert _MODULE_TO_INSPECT.get(None) is None
        assert 'docnote_extract_testpkg._hand_rolled' in sys.modules
        assert sys.modules[
            'docnote_extract_testpkg._hand_rolled'] is raw_module

    def test_find_spec_skips_stdlib(self):
        """find_spec() must return None for modules in the stdlib.
        """
        floader = _ExtractionFinderLoader(
            frozenset(),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)
        assert floader.find_spec('antigravity', None, None) is None

    def test_find_spec_skips_nohook(self):
        """find_spec() must return None for modules in the nohook set.
        """
        floader = _ExtractionFinderLoader(
            frozenset(),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)
        assert floader.find_spec('docnote', None, None) is None

    @set_phase(_ExtractionPhase.EXPLORATION)
    def test_find_spec_nostub_exploration(self):
        """find_spec() must return None for modules in the nostub set
        during the exploration phase.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)
        assert floader.find_spec('pytest', None, None) is None

    @set_phase(_ExtractionPhase.EXPLORATION)
    def test_find_spec_firstparty_exploration(self):
        """find_spec() must return None for modules in the firstparty
        set during the exploration phase.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)
        assert floader.find_spec('docnote_extract_testpkg', None, None) is None

    @set_inspection('')
    @set_phase(_ExtractionPhase.EXTRACTION)
    def test_find_spec_nostub_extraction(self):
        """find_spec() must return a delegated spec for modules in the
        nostub set during the extraction phase.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),
            module_stash_nostub_raw={
                'pytest': pytest,
                'docnote_extract_testpkg': docnote_extract_testpkg})
        spec = floader.find_spec('pytest', None, None)
        assert spec is not None
        assert isinstance(spec.loader_state, _DelegatedLoaderState)
        assert spec.loader_state.stub_strategy == _StubStrategy.TRACK
        assert not spec.loader_state.is_firstparty

    @set_inspection('')
    @set_phase(_ExtractionPhase.EXTRACTION)
    def test_find_spec_firstparty_extraction(self):
        """find_spec() must return a delegated spec for modules in the
        firstparty set during the extraction phase.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),
            module_stash_nostub_raw={
                'pytest': pytest,
                'docnote_extract_testpkg': docnote_extract_testpkg})
        spec = floader.find_spec('docnote_extract_testpkg', None, None)
        assert spec is not None
        assert isinstance(spec.loader_state, _DelegatedLoaderState)
        assert spec.loader_state.stub_strategy == _StubStrategy.STUB
        assert spec.loader_state.is_firstparty

    @set_inspection('docnote_extract_testpkg')
    @set_phase(_ExtractionPhase.EXTRACTION)
    def test_find_spec_firstparty_extraction_under_inspection(self, caplog):
        """find_spec() must return a delegated spec for modules in the
        firstparty set during the extraction phase. If the module is
        under inspection, it must use the STUB stub strategy, and warn
        that the feature is not supported.
        """
        floader = _ExtractionFinderLoader(
            frozenset({'docnote_extract_testpkg'}),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),
            module_stash_nostub_raw={
                'pytest': pytest,
                'docnote_extract_testpkg': docnote_extract_testpkg})

        caplog.clear()
        spec = floader.find_spec('docnote_extract_testpkg', None, None)

        # This is a quick and dirty way of checking for the log message
        captured_log_raw = ''.join(record.msg for record in caplog.records)
        assert 'Direct import detected' in captured_log_raw
        assert spec is not None
        assert isinstance(spec.loader_state, _DelegatedLoaderState)
        assert spec.loader_state.stub_strategy == _StubStrategy.STUB
        assert spec.loader_state.is_firstparty

    def test_find_spec_for_stubbable(self):
        """find_spec() must return a ModuleSpec with a set
        loader_state=_ExtractionLoaderState for a stubbable module.
        """
        floader = _ExtractionFinderLoader(
            frozenset(),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)
        spec = floader.find_spec('docnote_extract_testpkg', None, None)
        assert spec is not None
        assert isinstance(spec.loader_state, _ExtractionLoaderState)
        assert spec.loader_state.stub_strategy == _StubStrategy.STUB

    def test_import_hook_installation(self):
        """Installing the import hook must add it to sys.meta_path;
        uninstalling must remove it.

        This test deliberately does as little as possible; we'll save
        the heavier lifting for an integration test.
        """
        floader = _ExtractionFinderLoader(
            frozenset(),
            stubs_config=StubsConfig(
                enable_stubs=True,
                global_allowlist=None,
                firstparty_blocklist=frozenset(),
                thirdparty_blocklist=frozenset({'pytest'})),)
        assert not _check_for_hook()
        floader.install()
        try:
            assert _check_for_hook()
        finally:
            floader.uninstall()
        assert not _check_for_hook()

    def test_cleanup_sys_purge(self, fresh_unpurgeable_modules, capsys):
        """Cleanup_sys must force reloading of the module.
        If the module is purgeable, cleanup_sys must remove it
        from sys.modules.
        """
        importlib.invalidate_caches()
        import this  # noqa: F401
        # This makes sure we get the diff right
        _, _ = capsys.readouterr()
        assert 'this' in sys.modules

        _ExtractionFinderLoader.cleanup_sys({'this'})

        # This is a quick and dirty way of checking for re-import without
        # patching out importlib.reload, which sounds like a terrible idea.
        # Note that each call to readouterr() flushes the buffer, so this is
        # already a diff.
        stdout_diff, _ = capsys.readouterr()
        assert not stdout_diff
        assert 'this' not in sys.modules

    def test_cleanup_sys_nopurge(
            self, fresh_unpurgeable_modules, capsys):
        """Cleanup_sys must force reloading of the module.
        If the module is unpurgeable, cleanup_sys must forcibly
        reload it a second time.
        """
        importlib.invalidate_caches()
        fresh_unpurgeable_modules.add('this')
        import this  # noqa: F401
        # This makes sure we get the diff right
        _, _ = capsys.readouterr()
        assert 'this' in sys.modules

        _ExtractionFinderLoader.cleanup_sys({'this'})

        # This is a quick and dirty way of checking for re-import without
        # patching out importlib.reload, which sounds like a terrible idea.
        # Note that each call to readouterr() flushes the buffer, so this is
        # already a diff.
        stdout_diff, _ = capsys.readouterr()
        assert 'this' in sys.modules
        assert 'The Zen of Python' in stdout_diff


class TestStubbedGetattr:

    def test_shared_metaclass_markers(self):
        """Must return a metaclass reftype for any module:attr in the
        shared metaclass markers lookup.
        """
        retval = _stubbed_getattr(
            module_name='configatron',
            name='ConfigMeta',
            special_reftype_markers=GLOBAL_REFTYPE_MARKERS)
        assert isinstance(retval, type)
        assert issubclass(retval, type)
        assert not issubclass(retval, CrossrefMixin)
        assert is_crossreffed(retval)
        assert retval._docnote_extract_metadata == Crossref(
            module_name='configatron', toplevel_name='ConfigMeta')

    def test_manual_metaclass_markers(self):
        """Must return a metaclass reftype for any module:attr in the
        manual metaclass markers lookup.
        """
        retval = _stubbed_getattr(
            module_name='foo',
            name='Foo',
            special_reftype_markers={
                Crossref(module_name='foo', toplevel_name='Foo'):
                ReftypeMarker.METACLASS})
        assert isinstance(retval, type)
        assert issubclass(retval, type)
        assert not issubclass(retval, CrossrefMixin)
        assert is_crossreffed(retval)
        assert retval._docnote_extract_metadata == Crossref(
            module_name='foo', toplevel_name='Foo')

    def test_normal_reftype(self):
        """Must return a normal reftype for anything not marked as a
        metaclass.
        """
        retval = _stubbed_getattr(
            module_name='foo',
            name='Foo',
            special_reftype_markers={})
        assert isinstance(retval, type)
        assert not issubclass(retval, type)
        assert issubclass(retval, CrossrefMixin)
        assert is_crossreffed(retval)
        assert retval._docnote_extract_metadata == Crossref(
            module_name='foo', toplevel_name='Foo')


def _check_for_hook() -> bool:
    instance_found = False
    for loader in sys.meta_path:
        instance_found |= isinstance(loader, _ExtractionFinderLoader)

    return instance_found
