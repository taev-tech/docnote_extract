from __future__ import annotations

import typing
from collections.abc import Collection
from collections.abc import Iterator
from dataclasses import KW_ONLY
from dataclasses import dataclass
from dataclasses import field
from dataclasses import fields as dc_fields
from typing import Annotated
from typing import Self

from docnote import DocnoteConfig
from docnote import Note

from docnote_extract._utils import coerce_config
from docnote_extract._utils import validate_config
from docnote_extract.summaries import ModuleSummary
from docnote_extract.summaries import SummaryMetadataProtocol

if typing.TYPE_CHECKING:
    from docnote_extract._extraction import ModulePostExtraction


@dataclass(slots=True, frozen=True)
class ModuleTreeNode:
    """Module trees represent the hierarchy of modules within a package.
    They also provide some utility functions to navigate the module
    tree.

    Note that this is intended to be subclassed for particular use
    cases (for example, construction of post-extraction effective
    configs).
    """
    fullname: str
    relname: str
    children: dict[str, Self] = field(default_factory=dict)

    def find(self, name: str) -> Self:
        """Finds the node associated with the passed module name.
        Intended to be used from the module root, with absolute names,
        but also generally usable to traverse into child nodes.
        """
        relname_segments = name.split('.')
        if self.relname != relname_segments[0]:
            raise ValueError(
                'Find must start with the current node! Path not in tree.',
                self.relname, name)

        node = self
        for relname in relname_segments[1:]:
            try:
                node = node.children[relname]
            except KeyError as exc:
                exc.add_note(
                    f'Module {name} not found within in package/module '
                    + self.fullname)
                raise exc

        return node

    def clone_without_children(self) -> Self:
        """Creates a copy of the current node, except without any
        children. Useful when you need to create a copy of the tree
        while filtering some children out.
        """
        params = {}
        for field_obj in dc_fields(self):
            if field_obj.name != 'children':
                params[field_obj.name] = getattr(self, field_obj.name)

        return type(self)(**params)

    def flatten(self) -> Iterator[Self]:
        """Yields all of the nodes in the tree in a depth-first
        fashion. Note that the ordering of branches is arbitrary.
        """
        yield self
        for child in self.children.values():
            yield from child.flatten()

    @classmethod
    def from_discovery(
            cls,
            discovered: Collection[str]
            ) -> dict[str, ModuleTreeNode]:
        """Constructs one module name tree for each of the toplevel
        packages contained in ``discovered`` and returns them (with
        the toplevel package name as a key).
        """
        max_depth = max(module_name.count('.') for module_name in discovered)
        # We're going to sort all of the modules based on how deep their
        # names are. That way we can always assume the parent already exists
        # within the tree.
        depth_stack: list[list[str]] = [[] for _ in range(max_depth + 1)]
        for module_name in discovered:
            depth_stack[module_name.count('.')].append(module_name)

        all_nodes: dict[str, ModuleTreeNode] = {}
        roots_by_pkg: dict[str, ModuleTreeNode] = {}
        for package_name in depth_stack[0]:
            node = cls(fullname=package_name, relname=package_name)
            roots_by_pkg[package_name] = node
            all_nodes[package_name] = node

        for submodule_depth in depth_stack[1:]:
            for submodule_name in submodule_depth:
                parent_module_name, _, relname = submodule_name.rpartition('.')
                parent_node = all_nodes[parent_module_name]
                node = cls(submodule_name, relname)
                parent_node.children[relname] = node
                all_nodes[submodule_name] = node

        return roots_by_pkg

    def __truediv__(self, other: str) -> Self:
        return self.children[other]


@dataclass(slots=True, frozen=True)
class ConfiguredModuleTreeNode(ModuleTreeNode):
    """In addition to the underlying base module tree, these include
    both the actual module-post-extraction and the effective docnote
    config for every module in the tree.

    Note that the existence of the ``effective_config`` is the primary
    reason this class exists!
    """
    _: KW_ONLY
    effective_config: DocnoteConfig = field(compare=False, repr=False)

    def __post_init__(self):
        validate_config(
            self.effective_config, f'Module-level config for {self.fullname}')

    @classmethod
    def from_extraction(
            cls,
            extraction: dict[str, ModulePostExtraction]
            ) -> dict[str, ConfiguredModuleTreeNode]:
        """Given the results of
        ``_ExtractionFinderLoader.discover_and_extract`` -- namely, a
        dict of ``{module_fullname: ModulePostExtraction}`` -- construct
        a new ``ModuleTreeNode`` for each of the firstparty modules
        contained in the extraction.
        """
        max_depth = max(module_name.count('.') for module_name in extraction)
        # We're going to sort all of the modules based on how deep their
        # names are. We can then use this to make sure that the parent is
        # fully defined before continuing on to the children, making it easier
        # to construct the effective config.
        depth_stack: list[dict[str, ModulePostExtraction]] = [
            {} for _ in range(max_depth + 1)]
        for module_name, module in extraction.items():
            depth_stack[module_name.count('.')][module_name] = module

        roots_by_pkg: dict[str, ConfiguredModuleTreeNode] = {}
        for package_name, root_module in depth_stack[0].items():
            roots_by_pkg[package_name] = cls(
                fullname=package_name,
                relname=package_name,
                effective_config=coerce_config(root_module))

        for submodule_depth in depth_stack[1:]:
            for submodule_name, submodule in submodule_depth.items():
                root_pkg_name, *_, relname = submodule_name.split('.')
                parent_module_name, _, _ = submodule_name.rpartition('.')
                root_node = roots_by_pkg[root_pkg_name]
                parent_node = root_node.find(parent_module_name)
                parent_cfg = parent_node.effective_config.get_stackables()
                cfg = coerce_config(submodule, parent_stackables=parent_cfg)
                parent_node.children[relname] = cls(
                    submodule_name,
                    relname,
                    effective_config=cfg)

        return roots_by_pkg


@dataclass(slots=True, frozen=True)
class SummaryTreeNode[T: SummaryMetadataProtocol](ModuleTreeNode):
    _: KW_ONLY
    module_summary: ModuleSummary[T] = field(compare=False, repr=False)
    to_document: Annotated[
            bool | None,
            Note('''This is the value determined during filtering for whether
                or not a particular module summary should be included in
                the final documentation. After calling
                ``filter_module_summaries``, this will always be in sync with
                the ``to_document`` attribute on the ``module_summary``'s
                metadata. However, docs generation libraries might modify the
                value on the description metadata (for example, to "hoist" an
                otherwise undocumented private module based on its usage in a
                different public module), causing the two values to drift out
                of sync.''')
        ] = field(default=None, compare=False, init=False)

    @classmethod
    def from_configured_module_tree(
            cls,
            configured_tree_node: ConfiguredModuleTreeNode,
            summary_lookup: dict[str, ModuleSummary[T]]
            ) -> SummaryTreeNode[T]:
        """Uses a ``{module_name: module_summary}`` lookup to
        recursively convert a single``ConfiguredModuleTreeNode``
        root node into a ``SummaryTreeNode`` instance.
        """
        kwargs = {}
        for dc_field in dc_fields(ModuleTreeNode):
            if dc_field.name != 'children':
                kwargs[dc_field.name] = getattr(
                    configured_tree_node, dc_field.name)

        children: dict[str, SummaryTreeNode[T]] = {
            name: cls.from_configured_module_tree(node, summary_lookup)
            for name, node in configured_tree_node.children.items()}

        return cls(
            **kwargs,
            children=children,
            module_summary=summary_lookup[configured_tree_node.fullname])
