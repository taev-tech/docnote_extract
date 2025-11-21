from collections.abc import Callable
from importlib import import_module
from typing import Any
from typing import ClassVar
from typing import Literal
from typing import Optional
from typing import TypeVar
from typing import Union
from typing import cast
from typing import get_type_hints

from docnote import DocnoteConfig
from docnote import Note

from docnote_extract._extraction import ExtractionMetadata
from docnote_extract._extraction import ModulePostExtraction
from docnote_extract._module_tree import ConfiguredModuleTreeNode
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import GetattrTraversal
from docnote_extract.crossrefs import SyntacticTraversal
from docnote_extract.crossrefs import SyntacticTraversalType
from docnote_extract.normalization import NormalizedConcreteType
from docnote_extract.normalization import NormalizedEmptyGenericType
from docnote_extract.normalization import NormalizedLiteralType
from docnote_extract.normalization import NormalizedObj
from docnote_extract.normalization import NormalizedSpecialType
from docnote_extract.normalization import NormalizedUnionType
from docnote_extract.normalization import TypeSpec
from docnote_extract.normalization import normalize_module_dict
from docnote_extract.normalization import normalize_namespace_item

from docnote_extract_testpkg._hand_rolled.noteworthy import (
    ClassWithDecoratedConfigMethod,
)
from docnote_extract_testutils.fixtures import purge_cached_testpkg_modules


class TestNormalizeNamespaceItem:

    @purge_cached_testpkg_modules
    def test_config_via_decorator(self):
        """A value defined within a namespace (ex a class) that contains
        a ``DocnoteConfig`` attached via the ``@docnote`` decorator must
        include it within the normalized object's config attribute.
        """
        test_module = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled.noteworthy'))
        test_module.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'_hand_rolled': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                {'noteworthy': ConfiguredModuleTreeNode(
                    'docnote_extract_testpkg._hand_rolled.noteworthy',
                    'noteworthy',
                    effective_config=DocnoteConfig())},
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized_module = normalize_module_dict(test_module, module_tree)
        parent_obj = normalized_module['ClassWithDecoratedConfigMethod']

        result = normalize_namespace_item(
            'func_with_config',
            crossref=Crossref(
                module_name='docnote_extract_testpkg._hand_rolled.noteworthy',
                toplevel_name='ClassWithDecoratedConfigMethod',
                traversals=(GetattrTraversal('func_with_config'),)),
            value=ClassWithDecoratedConfigMethod.func_with_config,
            parent_annotations=get_type_hints(ClassWithDecoratedConfigMethod),
            parent_effective_config=parent_obj.effective_config,
            parent_typevars={})

        assert not result.annotateds
        assert result.typespec is None
        assert not result.notes
        assert result.effective_config == DocnoteConfig(include_in_docs=False)

    @purge_cached_testpkg_modules
    def test_canonical_overrides(self):
        """A value defined within a namespace (ex a class) that contains
        a ``DocnoteConfig`` with overrides for the canonical name and
        module must reflect those in the returned normalized object.
        """
        test_module = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled.noteworthy'))
        test_module.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'_hand_rolled': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                {'noteworthy': ConfiguredModuleTreeNode(
                    'docnote_extract_testpkg._hand_rolled.noteworthy',
                    'noteworthy',
                    effective_config=DocnoteConfig())},
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized_module = normalize_module_dict(test_module, module_tree)
        parent_obj = normalized_module['ClassWithDecoratedConfigMethod']

        result = normalize_namespace_item(
            'func_with_canonical_overrides',
            crossref=Crossref(
                module_name='docnote_extract_testpkg._hand_rolled.noteworthy',
                toplevel_name='ClassWithDecoratedConfigMethod',
                traversals=(
                    GetattrTraversal('func_with_canonical_overrides'),)),
            value=ClassWithDecoratedConfigMethod.func_with_canonical_overrides,
            parent_annotations=get_type_hints(ClassWithDecoratedConfigMethod),
            parent_effective_config=parent_obj.effective_config,
            parent_typevars={})

        assert result.canonical_module == 'foo.bar'
        assert result.canonical_name == 'baz'


class TestNormalizeModuleMembers:
    """Performs spot tests against a testpkg module that does no
    stubbing and has no dependencies (so that we can isolate stubbing
    behavior from the normalization step).

    Note that integration tests are responsible for checking modules
    that DO perform stubbing, both with and without stub bypasses.
    """
    @purge_cached_testpkg_modules
    def test_return_type_correct(self):
        """All returned objects must be _NormaliezdObj instances. The
        entire module dict must be returned in the normalized output.
        """
        docnote = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testutils.fixtures'))
        docnote.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testutils',
            'docnote_extract_testutils',
            {'taevcode': ConfiguredModuleTreeNode(
                'docnote_extract_testutils.fixtures',
                'fixtures',
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(docnote, module_tree)

        assert all(
            isinstance(obj, NormalizedObj) for obj in normalized.values())
        assert set(normalized) == set(docnote.__dict__)

    @purge_cached_testpkg_modules
    def test_local_class(self):
        """A class defined within the current module must be assigned
        the correct canonical origin.
        """
        docnote = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled'))
        docnote.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'taevcode': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(docnote, module_tree)

        norm_cls = normalized['ThisGetsUsedToTestNormalization']
        assert not norm_cls.annotateds
        assert norm_cls.typespec is None
        assert norm_cls.canonical_module == \
            'docnote_extract_testpkg._hand_rolled'
        assert norm_cls.canonical_name == 'ThisGetsUsedToTestNormalization'

    @purge_cached_testpkg_modules
    def test_bare_annotation(self):
        """A bare annotation defined within the current module must be
        included and assigned the correct canonical origin.
        """
        docnote = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled'))
        docnote.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'taevcode': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(docnote, module_tree)

        assert 'bare_annotation' in normalized
        norm_bare_anno = normalized['bare_annotation']
        assert not norm_bare_anno.annotateds
        assert norm_bare_anno.typespec == TypeSpec.from_typehint(
            str, typevars={})
        assert norm_bare_anno.canonical_module == \
            'docnote_extract_testpkg._hand_rolled'
        assert norm_bare_anno.canonical_name == 'bare_annotation'

    @purge_cached_testpkg_modules
    def test_note(self):
        """A value defined within the module that contains a ``Note``
        annotation must include it within the normalized object's note
        attribute.
        """
        test_module = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled.noteworthy'))
        test_module.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'_hand_rolled': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                {'noteworthy': ConfiguredModuleTreeNode(
                    'docnote_extract_testpkg._hand_rolled.noteworthy',
                    'noteworthy',
                    effective_config=DocnoteConfig())},
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(test_module, module_tree)

        norm_cfg_attr = normalized['DOCNOTE_CONFIG_ATTR']
        assert not norm_cfg_attr.annotateds
        assert norm_cfg_attr.typespec == TypeSpec.from_typehint(
            str, typevars={})
        assert len(norm_cfg_attr.notes) == 1
        note, = norm_cfg_attr.notes
        assert note.value.startswith('Docs generation libraries should use ')
        assert norm_cfg_attr.effective_config == DocnoteConfig()

    @purge_cached_testpkg_modules
    def test_config(self):
        """A value defined within the module that contains a
        ``DocnoteConfig`` annotation must include it within the
        normalized object's config attribute.
        """
        test_module = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled.noteworthy'))
        test_module.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'_hand_rolled': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                {'noteworthy': ConfiguredModuleTreeNode(
                    'docnote_extract_testpkg._hand_rolled.noteworthy',
                    'noteworthy',
                    effective_config=DocnoteConfig())},
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(test_module, module_tree)

        clcnote_attr = normalized['ClcNote']
        assert not clcnote_attr.annotateds
        assert clcnote_attr.typespec == TypeSpec.from_typehint(
            Callable[[str], Note], typevars={})  # type: ignore
        assert not clcnote_attr.notes
        assert clcnote_attr.effective_config == DocnoteConfig(
            include_in_docs=False)

    @purge_cached_testpkg_modules
    def test_config_stacking(self):
        """A value defined within the module that contains a
        ``DocnoteConfig`` annotation must include it within the
        normalized object's config attribute, and this must be stacked
        on top of the module-level config.
        """
        test_module = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled.noteworthy'))
        test_module.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'_hand_rolled': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                {'noteworthy': ConfiguredModuleTreeNode(
                    'docnote_extract_testpkg._hand_rolled.noteworthy',
                    'noteworthy',
                    effective_config=DocnoteConfig(enforce_known_lang=False))},
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(test_module, module_tree)

        clcnote_attr = normalized['ClcNote']
        assert not clcnote_attr.annotateds
        assert clcnote_attr.typespec == TypeSpec.from_typehint(
            Callable[[str], Note], typevars={})  # type: ignore
        assert not clcnote_attr.notes
        assert clcnote_attr.effective_config == DocnoteConfig(
            include_in_docs=False,
            enforce_known_lang=False)

    @purge_cached_testpkg_modules
    def test_config_via_decorator(self):
        """A value defined within the module that contains a
        ``DocnoteConfig`` attached via the ``@docnote`` decorator must
        include it within the normalized object's config attribute.
        """
        test_module = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled.noteworthy'))
        test_module.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'_hand_rolled': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                {'noteworthy': ConfiguredModuleTreeNode(
                    'docnote_extract_testpkg._hand_rolled.noteworthy',
                    'noteworthy',
                    effective_config=DocnoteConfig())},
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(test_module, module_tree)

        func_attr = normalized['func_with_config']
        assert not func_attr.annotateds
        assert func_attr.typespec is None
        assert not func_attr.notes
        assert func_attr.effective_config == DocnoteConfig(
            include_in_docs=False)

    @purge_cached_testpkg_modules
    def test_canonical_overrides(self):
        """A value defined within a namespace (ex a class) that contains
        a ``DocnoteConfig`` with overrides for the canonical name and
        module must reflect those in the returned normalized object.
        """
        test_module = cast(
            ModulePostExtraction,
            import_module('docnote_extract_testpkg._hand_rolled.noteworthy'))
        test_module.__docnote_extract_metadata__ = ExtractionMetadata(
            tracking_registry={},
            sourcecode='')
        module_tree = ConfiguredModuleTreeNode(
            'docnote_extract_testpkg',
            'docnote_extract_testpkg',
            {'_hand_rolled': ConfiguredModuleTreeNode(
                'docnote_extract_testpkg._hand_rolled',
                '_hand_rolled',
                {'noteworthy': ConfiguredModuleTreeNode(
                    'docnote_extract_testpkg._hand_rolled.noteworthy',
                    'noteworthy',
                    effective_config=DocnoteConfig())},
                effective_config=DocnoteConfig())},
            effective_config=DocnoteConfig())

        normalized = normalize_module_dict(test_module, module_tree)
        result = normalized['func_with_canonical_overrides']

        assert result.canonical_module == 'foo.bar'
        assert result.canonical_name == 'baz'


class TestTypeSpec:
    """Note: the test cases here could probably be parameterized, but
    they were just different enough that I wanted to hand-code them
    in a first run.
    """

    def test_from_stdlib_plain_type(self):
        """TypeSpec.from_typehint with a stdlib plain (non-generic) type
        as the typehint must return a result with all normalized types.
        """
        result = TypeSpec.from_typehint(int, typevars={})

        assert isinstance(result, TypeSpec)
        assert isinstance(result.normtype, NormalizedConcreteType)
        assert result.normtype.primary.toplevel_name == 'int'
        assert not result.normtype.params

    def test_from_stdlib_collection_type(self):
        """TypeSpec.from_typehint with a stdlib generic collection type
        as the typehint must return a result with all normalized types.
        """
        result = TypeSpec.from_typehint(frozenset[int], typevars={})

        assert isinstance(result, TypeSpec)
        assert isinstance(result.normtype, NormalizedConcreteType)
        assert result.normtype.primary.toplevel_name == 'frozenset'
        assert len(result.normtype.params) == 1
        paramtype, = result.normtype.params
        assert isinstance(paramtype, TypeSpec)
        assert isinstance(paramtype.normtype, NormalizedConcreteType)
        assert not paramtype.normtype.params
        assert paramtype.normtype.primary.toplevel_name == 'int'

    def test_from_optional(self):
        """TypeSpec.from_typehint with an Optional[...] type
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(Optional[int], typevars={})

        assert isinstance(result, TypeSpec)
        assert isinstance(result.normtype, NormalizedUnionType)

        assert len(result.normtype.normtypes) == 2
        assert result.normtype.normtypes == frozenset({
            NormalizedSpecialType.NONE,
            NormalizedConcreteType(
                primary=Crossref(
                    module_name='builtins', toplevel_name='int'))})

    def test_from_pipe_union(self):
        """TypeSpec.from_typehint with a ``pipe | union`` type
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(int | bool, typevars={})

        assert isinstance(result, TypeSpec)
        assert isinstance(result.normtype, NormalizedUnionType)

        assert len(result.normtype.normtypes) == 2
        assert result.normtype.normtypes == frozenset({
            NormalizedConcreteType(
                primary=Crossref(
                    module_name='builtins', toplevel_name='int')),
            NormalizedConcreteType(
                primary=Crossref(
                    module_name='builtins', toplevel_name='bool')),})

    def test_from_explicit_union(self):
        """TypeSpec.from_typehint with an explicit ``Union[...]`` type
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(Union[int, bool], typevars={})

        assert isinstance(result, TypeSpec)
        assert isinstance(result.normtype, NormalizedUnionType)

        assert len(result.normtype.normtypes) == 2
        assert result.normtype.normtypes == frozenset({
            NormalizedConcreteType(
                primary=Crossref(
                    module_name='builtins', toplevel_name='int')),
            NormalizedConcreteType(
                primary=Crossref(
                    module_name='builtins', toplevel_name='bool')),})

    def test_from_none(self):
        """TypeSpec.from_typehint with the colloquial type of ``None``
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(None, typevars={})

        assert isinstance(result, TypeSpec)
        assert result.normtype is NormalizedSpecialType.NONE

    def test_from_any(self):
        """TypeSpec.from_typehint with an ``Any`` type
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(Any, typevars={})  # type: ignore

        assert isinstance(result, TypeSpec)
        assert result.normtype is NormalizedSpecialType.ANY

    def test_from_classvar(self):
        """TypeSpec.from_typehint with a ``ClassVar`` type
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(ClassVar[int], typevars={})

        assert isinstance(result, TypeSpec)
        assert result.normtype == NormalizedConcreteType(
            primary=Crossref(
                module_name='builtins', toplevel_name='int'))
        assert result.has_classvar is True

    def test_from_literal(self):
        """TypeSpec.from_typehint with a ``Literal`` type
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(Literal[True], typevars={})  # type: ignore

        assert isinstance(result, TypeSpec)
        assert result.normtype == NormalizedLiteralType(
            values=frozenset({True}))

    def test_from_callable(self):
        """TypeSpec.from_typehint with a ``Callable`` type
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(Callable[[int], bool], typevars={})  # type: ignore

        assert isinstance(result, TypeSpec)
        assert result.normtype == NormalizedConcreteType(
            primary=Crossref(
                module_name='collections.abc', toplevel_name='Callable'),
            params=(
                TypeSpec(NormalizedEmptyGenericType(
                    params=(TypeSpec(NormalizedConcreteType(primary=Crossref(
                        module_name='builtins', toplevel_name='int'))),),)),
                TypeSpec(NormalizedConcreteType(primary=Crossref(
                    module_name='builtins', toplevel_name='bool')))))

    def test_from_typevar_mod(self):
        """TypeSpec.from_typehint with module-level ``TypeVar`` instance
        as the typehint must return a correct result.
        """
        result = TypeSpec.from_typehint(
            _ModTypeVar, typevars={
                _ModTypeVar: Crossref(
                    module_name='foo',
                    toplevel_name='_ModTypeVar')})

        assert isinstance(result, TypeSpec)
        assert result.normtype == NormalizedConcreteType(
            primary=Crossref(
                module_name='foo', toplevel_name='_ModTypeVar'),)

    def test_from_typevar_sugared[T](self):
        """TypeSpec.from_typehint with syntactic-sugared type vars
        must return a correct result.
        """
        result = TypeSpec.from_typehint(
            T, typevars={
                T: Crossref(  # type: ignore
                    module_name='foo',
                    toplevel_name='bar',
                    traversals=(
                        SyntacticTraversal(
                            type_=SyntacticTraversalType.TYPEVAR,
                            key='T'),))})

        assert isinstance(result, TypeSpec)
        print(result.normtype)
        assert result.normtype == NormalizedConcreteType(
            primary=Crossref(
                module_name='foo',
                toplevel_name='bar',
                traversals=(
                    SyntacticTraversal(
                        type_=SyntacticTraversalType.TYPEVAR,
                        key='T'),)),
            params=())

    def test_from_aliased_generic(self):
        """TypeSpec.from_typehint with module-level generic type alias
        must return a correct result.
        """
        result = TypeSpec.from_typehint(
            AliasedGeneric[int], typevars={  # type: ignore
                AliasedGeneric.__type_params__[0]: Crossref(  # type: ignore
                    module_name='foo',
                    toplevel_name='AliasedGeneric',
                    traversals=(
                        SyntacticTraversal(
                            type_=SyntacticTraversalType.TYPEVAR,
                            key='T'),))})

        assert isinstance(result, TypeSpec)
        print(result.normtype)
        assert result.normtype == NormalizedConcreteType(
            primary=Crossref(
                module_name='tests_py.normalization_test',
                toplevel_name='AliasedGeneric'),
            params=(TypeSpec(NormalizedConcreteType(primary=Crossref(
                        module_name='builtins', toplevel_name='int'))),))


_ModTypeVar = TypeVar('_ModTypeVar')
type AliasedGeneric[T] = list[T]
