from __future__ import annotations

import logging
import sys
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated
from typing import overload

from docnote import Note

from docnote_extract._extraction import ReftypeMarker
from docnote_extract._extraction import StubsConfig
from docnote_extract._extraction import _ExtractionFinderLoader
from docnote_extract._module_tree import ConfiguredModuleTreeNode
from docnote_extract._module_tree import SummaryTreeNode
from docnote_extract._summarization import SummaryMetadata
from docnote_extract._summarization import summarize_module
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import GetattrTraversal
from docnote_extract.exceptions import NotFirstpartyPackage
from docnote_extract.exceptions import UnknownCrossrefTarget
from docnote_extract.filtering import filter_canonical_ownership
from docnote_extract.filtering import filter_module_summaries
from docnote_extract.filtering import filter_private_summaries
from docnote_extract.normalization import normalize_module_dict
from docnote_extract.summaries import ModuleSummary
from docnote_extract.summaries import SummaryBase
from docnote_extract.summaries import SummaryMetadataFactoryProtocol
from docnote_extract.summaries import SummaryMetadataProtocol

logger = logging.getLogger(__name__)


@overload
def gather[T: SummaryMetadataProtocol](
        firstparty_pkg_names: Collection[str],
        *,
        summary_metadata_factory: SummaryMetadataFactoryProtocol[T],
        enabled_stubs: Annotated[
                bool | frozenset[str],
                Note('''Enabling stubbing will cause objects imported from
                    modules to be replaced by magicmock-like objects that can
                    both improve the reliability of cross-module references
                    and decrease required time for gathering, since the actual
                    upstream module is never loaded. This can be particularly
                    beneficial for heavyweight third-party libraries that take
                    a long time to load.

                    Set to True to enable globally (subject to the other
                    nostub settings). Set to False to disable globally. Or
                    pass an explicit allowlist of modules (exact modules, not
                    packages) to stub.

                    **Note that stubbing is highly experimental.** Seemingly
                    innocuous things (like module-level constants that are
                    instances of imported classes) can break extraction in
                    unexpected ways, and there are a number of very rough
                    edges (for example, incomplete specification of special
                    reftype markers can cause infinite loops during
                    extraction).''')
            ] = False,
        special_reftype_markers: Annotated[
                dict[Crossref, ReftypeMarker] | None,
                Note('''If you use metaclasses or decorators from third-party
                    packages, you'll need to add them here for them to be
                    correctly interpreted by the import stubbing mechanism.''')
            ] = None,
        nostub_firstparty_modules: Annotated[
                Collection[str] | None,
                Note('''Note that this applies to only an individual module,
                    not an entire package, and can only be used for firstparty
                    modules (ie, ``firstparty_pkg_names`` and their children).
                    ''')
            ] = None,
        nostub_packages: Annotated[
                Collection[str] | None,
                Note('''Note that this applies to an entire package and not
                    just an individual module, but it can be used for
                    thirdparty dependencies.''')
            ] = None,
        remove_unknown_origins: Annotated[
                bool,
                Note('''Set this to ``False`` if you'd like to preserve
                    module namespace members with an unknown canonical
                    module origin. This can create a large number of false
                    positives in ``metadata.to_document`` values, but can be
                    helpful in recovering module-level constants.''')
            ] = True
        ) -> Docnotes[T]: ...
@overload
def gather(
        firstparty_pkg_names: Collection[str],
        *,
        summary_metadata_factory: None = None,
        enabled_stubs: Annotated[
                bool | frozenset[str],
                Note('''Enabling stubbing will cause objects imported from
                    modules to be replaced by magicmock-like objects that can
                    both improve the reliability of cross-module references
                    and decrease required time for gathering, since the actual
                    upstream module is never loaded. This can be particularly
                    beneficial for heavyweight third-party libraries that take
                    a long time to load.

                    Set to True to enable globally (subject to the other
                    nostub settings). Set to False to disable globally. Or
                    pass an explicit allowlist of modules (exact modules, not
                    packages) to stub.

                    **Note that stubbing is highly experimental.** Seemingly
                    innocuous things (like module-level constants that are
                    instances of imported classes) can break extraction in
                    unexpected ways, and there are a number of very rough
                    edges (for example, incomplete specification of special
                    reftype markers can cause infinite loops during
                    extraction).''')
            ] = False,
        special_reftype_markers: Annotated[
                dict[Crossref, ReftypeMarker] | None,
                Note('''If you use metaclasses or decorators from third-party
                    packages, you'll need to add them here for them to be
                    correctly interpreted by the import stubbing mechanism.''')
            ] = None,
        nostub_firstparty_modules: Annotated[
                Collection[str] | None,
                Note('''Note that this applies to only an individual module,
                    not an entire package, and can only be used for firstparty
                    modules (ie, ``firstparty_pkg_names`` and their children).
                    ''')
            ] = None,
        nostub_packages: Annotated[
                Collection[str] | None,
                Note('''Note that this applies to an entire package and not
                    just an individual module, but it can be used for
                    thirdparty dependencies.''')
            ] = None,
        remove_unknown_origins: Annotated[
                bool,
                Note('''Set this to ``False`` if you'd like to preserve
                    module namespace members with an unknown canonical
                    module origin. This can create a large number of false
                    positives in ``metadata.to_document`` values, but can be
                    helpful in recovering module-level constants.''')
            ] = True
        ) -> Docnotes[SummaryMetadata]: ...
def gather[T: SummaryMetadataProtocol](
        firstparty_pkg_names: Collection[str],
        *,
        summary_metadata_factory:
            SummaryMetadataFactoryProtocol[T] | None = None,
        enabled_stubs: Annotated[
                bool | Collection[str],
                Note('''Enabling stubbing will cause objects imported from
                    modules to be replaced by magicmock-like objects that can
                    both improve the reliability of cross-module references
                    and decrease required time for gathering, since the actual
                    upstream module is never loaded. This can be particularly
                    beneficial for heavyweight third-party libraries that take
                    a long time to load.

                    Set to True to enable globally (subject to the other
                    nostub settings). Set to False to disable globally. Or
                    pass an explicit allowlist of modules (exact modules, not
                    packages) to stub.

                    **Note that stubbing is highly experimental.** Seemingly
                    innocuous things (like module-level constants that are
                    instances of imported classes) can break extraction in
                    unexpected ways, and there are a number of very rough
                    edges (for example, incomplete specification of special
                    reftype markers can cause infinite loops during
                    extraction).''')
            ] = False,
        special_reftype_markers: Annotated[
                dict[Crossref, ReftypeMarker] | None,
                Note('''If you use metaclasses or decorators from third-party
                    packages, you'll need to add them here for them to be
                    correctly interpreted by the import stubbing mechanism.''')
            ] = None,
        nostub_firstparty_modules: Annotated[
                Collection[str] | None,
                Note('''Note that this applies to only an individual module,
                    not an entire package, and can only be used for firstparty
                    modules (ie, ``firstparty_pkg_names`` and their children).
                    ''')
            ] = None,
        nostub_packages: Annotated[
                Collection[str] | None,
                Note('''Note that this applies to an entire package and not
                    just an individual module, but it can be used for
                    thirdparty dependencies.''')
            ] = None,
        remove_unknown_origins: Annotated[
                bool,
                Note('''Set this to ``False`` if you'd like to preserve
                    module namespace members with an unknown canonical
                    module origin. This can create a large number of false
                    positives in ``metadata.to_document`` values, but can be
                    helpful in recovering module-level constants.''')
            ] = True
        ) -> Docnotes[T]:
    """Uses an import hook to discover all firstparty modules within the
    specified toplevel firstparty package names and then extracts
    summaries for all of them, returning a ``Docnotes`` collection
    describing all of the discovered docs. The importhook also applies
    a stubbing technique to all thirdparty dependencies, making docs
    generation significantly faster, and allowing docs generation
    virtualenvs to be much lighter-weight than the full package
    requirements. Generally speaking, absent any "nostub" configuration
    (see below), you will only need the passed ``firstparty_pkg_names``
    available in the docs generation virtualenv (in addition to
    ``docnote``, ``docnote_extract``, and any libraries needed for
    processing the resulting ``Docnotes``).

    **This will result in an ``exec`` of all modules within the
    firstparty packages and can only be used on trusted code!**

    Occasionally the import-hook-based extraction process can run into
    problems. In this case, it may be useful to declare either an
    entire package (firstparty or thirdparty) or a single module
    (firstparty only) as "nostub". This will bypass the import hook for
    that package or module, instead wrapping it in a tracking mechanism
    that will hopefully recover most of the downstream crossrefs to it.
    **This requires the package to be installed within the virtualenv
    used for docs extraction.**

    The downsides of a "nostub" approach are:
    ++  docs extraction will take longer. If the module has import side
        effects, that can be substantial
    ++  depending on the specifics of the un-stubbed module, it can
        result in a cascade of bypasses for its dependencies, and then
        their dependencies, and so on
    ++  it requires that module to be available within the virtualenv
        used for docs generation
    ++  you may run into issues with ``if typing.TYPE_CHECKING`` blocks
    ++  you can very quickly run into issues with traversals. This can,
        for example, cause problems with references to imported
        ``Enum`` members

    In short, you should avoid marking packages as ``nostub`` unless
    you run into problems directly related to stubbing, which cannot be
    solved by other, more precise, escape hatches (for example, using
    ``special_reftype_markers`` or ``DocnoteConfig.mark_special_reftype``
    to force a particular import to be a metaclass- or
    decorator-compatible stub).
    """
    floader_options = {}
    if special_reftype_markers is not None:
        floader_options['special_reftype_markers'] = special_reftype_markers

    if summary_metadata_factory is None:
        factory_kwarg = {}
    else:
        factory_kwarg = {'summary_metadata_factory': summary_metadata_factory}

    firstpary_pkgs = frozenset(firstparty_pkg_names)
    floader = _ExtractionFinderLoader(
        firstpary_pkgs,
        stubs_config=StubsConfig.from_gather_kwargs(
            enabled_stubs,
            nostub_firstparty_modules,
            nostub_packages),
        **floader_options)
    extraction = floader.discover_and_extract()
    configured_trees = ConfiguredModuleTreeNode.from_extraction(extraction)

    summaries: dict[str, SummaryTreeNode] = {}
    for pkg_name, configured_tree in configured_trees.items():
        summary_lookup: dict[str, ModuleSummary] = {}

        for configured_tree_node in configured_tree.flatten():
            module_extraction = extraction[configured_tree_node.fullname]
            normalized_objs = normalize_module_dict(
                module_extraction,
                configured_tree)
            module_summary = summarize_module(
                module_extraction,
                normalized_objs,
                configured_tree,
                **factory_kwarg)
            filter_canonical_ownership(
                module_summary, remove_unknown_origins=remove_unknown_origins)
            filter_private_summaries(module_summary)
            summary_lookup[configured_tree_node.fullname] = module_summary

        summaries[pkg_name] = summary_tree = \
            SummaryTreeNode.from_configured_module_tree(
                configured_tree,
                summary_lookup)

        logger.debug('Applying filtering rules for package %s', pkg_name)
        filter_module_summaries(summary_tree, configured_tree)

    return Docnotes(summaries)


@dataclass(slots=True, frozen=True)
class Docnotes[T: SummaryMetadataProtocol]:
    summaries: dict[str, SummaryTreeNode[T]]

    def is_firstparty(self, crossref: Crossref) -> bool:
        """Returns True if the passed crossref is firstparty (and
        therefore should be resolvable within the gathered docs).
        """
        if crossref.module_name is None:
            return False

        pkg_name, _, _ = crossref.module_name.partition('.')
        return pkg_name in self.summaries

    def is_stdlib(self, crossref: Crossref) -> bool:
        """Returns True if the passed crossref comes from the stdlib.
        This can be useful if docs generation libraries also have a
        way to link to stdlib docs.
        """
        if crossref.module_name is None:
            return False

        pkg_name, _, _ = crossref.module_name.partition('.')
        return pkg_name in sys.stdlib_module_names

    def resolve_crossref(self, crossref: Crossref) -> SummaryBase[T]:
        """Finds the summary for the passed crossref.
        Raises one of two different ``LookupError`` subclasses if none
        is found:
        ++  ``NotFirstpartyPackage`` if the crossref is for a package
            that isn't firstparty, and therefore not summarized
        ++  ``UnknownCrossrefTarget`` if the crossref is a firstparty
            reference, but the target is unknown.
        """
        if crossref.module_name is None:
            raise NotFirstpartyPackage(crossref)

        pkg_name, _, _ = crossref.module_name.partition('.')
        if pkg_name not in self.summaries:
            raise NotFirstpartyPackage(crossref)

        summary_tree = self.summaries[pkg_name]
        try:
            module_node = summary_tree.find(crossref.module_name)
        except KeyError as exc:
            raise UnknownCrossrefTarget(crossref) from exc

        module_summary = module_node.module_summary
        if crossref.toplevel_name is None:
            return module_summary

        traversals = (
            GetattrTraversal(crossref.toplevel_name), *crossref.traversals)
        current_summary = module_summary
        for traversal in traversals:
            try:
                current_summary = current_summary.traverse(traversal)
            except LookupError as exc:
                raise UnknownCrossrefTarget(crossref) from exc

        return current_summary
