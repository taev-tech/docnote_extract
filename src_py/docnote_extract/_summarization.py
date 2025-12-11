from __future__ import annotations

import inspect
import itertools
import logging
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace as dc_replace
from typing import Annotated
from typing import Any
from typing import Literal
from typing import Protocol
from typing import TypeVar
from typing import cast
from typing import get_overloads
from typing import get_type_hints
from uuid import UUID

from docnote import DOCNOTE_CONFIG_ATTR
from docnote import DocnoteConfig
from docnote import DocnoteConfigParams
from docnote import Note

from docnote_extract._extraction import ModulePostExtraction
from docnote_extract._module_tree import ConfiguredModuleTreeNode
from docnote_extract._utils import extract_docstring
from docnote_extract._utils import textify_notes
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import GetattrTraversal
from docnote_extract.crossrefs import ParamTraversal
from docnote_extract.crossrefs import SignatureTraversal
from docnote_extract.crossrefs import SyntacticTraversal
from docnote_extract.crossrefs import SyntacticTraversalType
from docnote_extract.crossrefs import has_crossreffed_base
from docnote_extract.crossrefs import has_crossreffed_metaclass
from docnote_extract.crossrefs import is_crossreffed
from docnote_extract.normalization import LazyResolvingValue
from docnote_extract.normalization import NormalizedObj
from docnote_extract.normalization import TypeSpec
from docnote_extract.normalization import extend_typevars
from docnote_extract.normalization import normalize_annotation
from docnote_extract.normalization import normalize_namespace_item
from docnote_extract.summaries import CallableColor
from docnote_extract.summaries import CallableSummary
from docnote_extract.summaries import ClassSummary
from docnote_extract.summaries import CrossrefSummary
from docnote_extract.summaries import MethodType
from docnote_extract.summaries import ModuleSummary
from docnote_extract.summaries import NamespaceMemberSummary
from docnote_extract.summaries import ObjClassification
from docnote_extract.summaries import ParamStyle
from docnote_extract.summaries import ParamSummary
from docnote_extract.summaries import RetvalSummary
from docnote_extract.summaries import SignatureSummary
from docnote_extract.summaries import Singleton
from docnote_extract.summaries import SummaryBase
from docnote_extract.summaries import SummaryMetadataFactoryProtocol
from docnote_extract.summaries import SummaryMetadataProtocol
from docnote_extract.summaries import TypeVarSummary
from docnote_extract.summaries import VariableSummary

logger = logging.getLogger(__name__)

_summary_factories: dict[type[SummaryBase], _SummaryFactoryProtocol] = {}
def _summary_factory[T: SummaryBase](
        summary_type: type[T]
        ) -> Callable[
            [_SummaryFactoryProtocol[T]], _SummaryFactoryProtocol[T]]:
    """Second-order decorator for declaring a summary factory."""
    def decorator(
            func: _SummaryFactoryProtocol[T]) -> _SummaryFactoryProtocol[T]:
        recast = cast(_SummaryFactoryAttrProto, func)
        recast._summary_factory_type = summary_type
        _summary_factories[summary_type] = func
        return func

    return decorator


class _SummaryFactoryAttrProto(Protocol):
    _summary_factory_type: type[SummaryBase]


class _SummaryFactoryProtocol[T: SummaryBase](Protocol):

    def __call__(
            self,
            name_in_parent: str,
            parent_crossref_namespace: dict[str, Crossref],
            obj: NormalizedObj,
            classification: ObjClassification,
            *,
            module_globals: dict[str, Any],
            in_class: bool = False,
            summary_metadata_factory: SummaryMetadataFactoryProtocol,
            ) -> T:
        """Given an object and its classification, construct a
        summary instance, populating it with any required children.
        """
        ...


@dataclass(slots=True, init=False)
class SummaryMetadata(SummaryMetadataProtocol):
    """The default implementation for summary metadata.
    """
    id_: str | int | UUID | None
    extracted_inclusion: bool | None
    canonical_module: str | None
    to_document: bool
    disowned: bool
    crossref_namespace: dict[str, Crossref] = field(repr=False)

    @classmethod
    def factory(
            cls,
            *,
            classification: ObjClassification | None,
            summary_class: type[SummaryBase],
            crossref: Crossref | None,
            annotateds: tuple[LazyResolvingValue, ...],
            metadata: dict[str, Any]
            ) -> SummaryMetadata:
        return cls()

    @property
    def included(self) -> bool:
        return self.to_document and not self.disowned


def summarize_module[T: SummaryMetadataProtocol](
        module: ModulePostExtraction,
        normalized_objs: Annotated[
                dict[str, NormalizedObj],
                Note('All module members, with no filters applied.')],
        module_tree: ConfiguredModuleTreeNode,
        summary_metadata_factory:
            SummaryMetadataFactoryProtocol[T] = SummaryMetadata.factory
        ) -> ModuleSummary[T]:
    """For the passed post-extraction module, iterates across all
    normalized_objs and extracts their summaries, returning them
    combined into a single ``ModuleSummary``.
    """
    logger.info('Creating extraction summary object for %s', module.__name__)
    module_crossref = Crossref(
        module_name=module.__name__,
        toplevel_name=None)
    namespace: dict[str, Crossref] = {}
    module_name = module.__name__

    typevars: set[TypeVarSummary[T]] = set()
    module_members: set[NamespaceMemberSummary[T]] = set()
    for name, normalized_obj in normalized_objs.items():
        member_summary = _summarize_namespace_member(
            module_name,
            module.__dict__,
            module_crossref,
            namespace,
            name,
            normalized_obj,
            summary_metadata_factory,
            in_class=False)
        if isinstance(member_summary, TypeVarSummary):
            typevars.add(member_summary)
        elif member_summary is not None:
            module_members.add(member_summary)

    config = module_tree.find(module.__name__).effective_config
    metadata = summary_metadata_factory(
        classification=ObjClassification.from_obj(module),
        summary_class=ModuleSummary,
        crossref=module_crossref,
        annotateds=(),
        metadata=config.metadata or {})
    metadata.id_ = config.id_
    metadata.extracted_inclusion = config.include_in_docs
    metadata.crossref_namespace = namespace
    metadata.canonical_module = module.__name__

    if (raw_dunder_all := getattr(module, '__all__', None)) is not None:
        dunder_all = frozenset(raw_dunder_all)
    else:
        dunder_all = None

    return ModuleSummary(
        crossref=module_crossref,
        name=module.__name__,
        ordering_index=config.ordering_index,
        parent_group_name=None,
        child_groups=config.child_groups or (),
        metadata=metadata,
        typevars=frozenset(typevars),
        dunder_all=dunder_all,
        docstring=extract_docstring(module, config),
        members=frozenset(module_members))


@_summary_factory(CrossrefSummary)
def create_crossref_summary(
        name_in_parent: str,
        parent_crossref_namespace: dict[str, Crossref],
        obj: NormalizedObj,
        classification: ObjClassification,
        *,
        module_globals: dict[str, Any],
        in_class: bool = False,
        summary_metadata_factory: SummaryMetadataFactoryProtocol,
        ) -> CrossrefSummary:
    """Given an object and its classification, construct a
    summary instance, populating it with any required children.
    """
    src_obj = obj.obj_or_stub
    # Note: this cannot have traversals, or it would have been classified
    # as a VariableSummary instead of a re-export.
    if not is_crossreffed(src_obj):
        raise TypeError(
            'Impossible branch: re-export from non-reftype!', obj)

    crossref = parent_crossref_namespace.get(name_in_parent)
    metadata = summary_metadata_factory(
        classification=classification,
        summary_class=CrossrefSummary,
        crossref=crossref,
        annotateds=tuple(
            LazyResolvingValue.from_annotated(annotated)
            for annotated in obj.annotateds),
        metadata=obj.effective_config.metadata or {})
    metadata.id_ = obj.effective_config.id_
    metadata.extracted_inclusion = \
        obj.effective_config.include_in_docs
    metadata.crossref_namespace = parent_crossref_namespace
    metadata.canonical_module = (
        obj.canonical_module if obj.canonical_module is not Singleton.UNKNOWN
        else None)

    return CrossrefSummary(
        name=name_in_parent,
        src_crossref=src_obj._docnote_extract_metadata,
        typespec=obj.typespec,
        notes=textify_notes(obj.notes, obj.effective_config),
        crossref=crossref,
        ordering_index=obj.effective_config.ordering_index,
        child_groups=obj.effective_config.child_groups or (),
        parent_group_name=obj.effective_config.parent_group_name,
        metadata=metadata)


@_summary_factory(VariableSummary)
def create_variable_summary(
        name_in_parent: str,
        parent_crossref_namespace: dict[str, Crossref],
        obj: NormalizedObj,
        classification: ObjClassification,
        *,
        module_globals: dict[str, Any],
        in_class: bool = False,
        summary_metadata_factory: SummaryMetadataFactoryProtocol,
        ) -> VariableSummary:
    """Given an object and its classification, construct a
    summary instance, populating it with any required children.
    """
    src_obj = obj.obj_or_stub

    crossref = parent_crossref_namespace.get(name_in_parent)
    metadata = summary_metadata_factory(
        classification=classification,
        summary_class=VariableSummary,
        crossref=crossref,
        annotateds=tuple(
            LazyResolvingValue.from_annotated(annotated)
            for annotated in obj.annotateds),
        metadata=obj.effective_config.metadata or {})
    metadata.id_ = obj.effective_config.id_
    metadata.extracted_inclusion = \
        obj.effective_config.include_in_docs
    metadata.crossref_namespace = parent_crossref_namespace
    metadata.canonical_module = (
        obj.canonical_module if obj.canonical_module is not Singleton.UNKNOWN
        else None)

    # If missing, use the runtime type as an inference -- unless the
    # object was a bare annotation (without a typespec?! weird), then
    # we can't do anything.
    notes = textify_notes(obj.notes, obj.effective_config)
    if obj.typespec is None and src_obj is not Singleton.MISSING:
        if is_crossreffed(src_obj):
            logger.warning(
                'Type inference is not supported for crossreffed variables. '
                + 'For a non-None typespec, you must explicitly declare the '
                + 'type of %s (%s)', name_in_parent, src_obj)
            # I'm punting on this because it's a huge can of worms. For now,
            # we don't support type inference here; you really MUST declare
            # it as an explicit type
            typespec = None

        elif isinstance(src_obj, property):
            renormalized_obj = dc_replace(obj, obj_or_stub=src_obj.fget)
            callable_summary = create_callable_summary(
                name_in_parent,
                parent_crossref_namespace,
                renormalized_obj,
                classification,
                module_globals=module_globals,
                in_class=in_class,
                summary_metadata_factory=summary_metadata_factory)
            signature_summary, = callable_summary.signatures
            typespec = signature_summary.retval.typespec

            if callable_summary.docstring is not None:
                notes = (*notes, callable_summary.docstring)

        else:
            typespec = TypeSpec.from_typehint(
                type(src_obj),
                typevars=obj.typevars)
    else:
        typespec = obj.typespec

    return VariableSummary(
        name=name_in_parent,
        typespec=typespec,
        notes=notes,
        crossref=crossref,
        ordering_index=obj.effective_config.ordering_index,
        child_groups=obj.effective_config.child_groups or (),
        parent_group_name=obj.effective_config.parent_group_name,
        metadata=metadata)


@_summary_factory(TypeVarSummary)
def create_typevar_summary(
        name_in_parent: str,
        parent_crossref_namespace: dict[str, Crossref],
        obj: NormalizedObj,
        classification: ObjClassification,
        *,
        module_globals: dict[str, Any],
        in_class: bool = False,
        summary_metadata_factory: SummaryMetadataFactoryProtocol,
        ) -> TypeVarSummary:
    """This is used **only for module-level typevars** to create
    typevar summaries. If you need typevar summaries for ANYTHING ELSE,
    see ``_make_typevar_summary_direct``.
    """
    src_obj = cast(TypeVar, obj.obj_or_stub)

    crossref = parent_crossref_namespace.get(name_in_parent)
    metadata = summary_metadata_factory(
        classification=classification,
        summary_class=TypeVarSummary,
        crossref=crossref,
        annotateds=tuple(
            LazyResolvingValue.from_annotated(annotated)
            for annotated in obj.annotateds),
        metadata=obj.effective_config.metadata or {})
    metadata.id_ = obj.effective_config.id_
    metadata.extracted_inclusion = \
        obj.effective_config.include_in_docs
    metadata.crossref_namespace = parent_crossref_namespace
    metadata.canonical_module = (
        obj.canonical_module if obj.canonical_module is not Singleton.UNKNOWN
        else None)

    if hasattr(src_obj, 'has_default') and src_obj.has_default():  # type: ignore
        default = TypeSpec.from_typehint(
            src_obj.__default__, typevars=obj.typevars)  # type: ignore
    else:
        default = None

    # This is the default. Creating an actual binding to None doesn't really
    # make sense, so we'll stick to the convention.
    if src_obj.__bound__ is None:
        bound = None
    else:
        bound = TypeSpec.from_typehint(
            src_obj.__bound__, typevars=obj.typevars)

    return TypeVarSummary(
        name=name_in_parent,
        crossref=crossref,
        bound=bound,
        constraints=tuple(
            TypeSpec.from_typehint(constraint, typevars=obj.typevars)
            for constraint in src_obj.__constraints__),
        default=default,
        ordering_index=obj.effective_config.ordering_index,
        child_groups=obj.effective_config.child_groups or (),
        parent_group_name=obj.effective_config.parent_group_name,
        metadata=metadata)


def _summarize_namespace_member[T: SummaryMetadataProtocol](  # noqa: PLR0913
        module_name: str | Literal[Singleton.UNKNOWN] | None,
        module_globals: dict[str, Any],
        parent_crossref: Crossref | None,
        parent_namespace: dict[str, Crossref],
        attr_name: str,
        normalized_obj: NormalizedObj,
        summary_metadata_factory: SummaryMetadataFactoryProtocol[T],
        *,
        in_class: bool
        ) -> NamespaceMemberSummary[T] | None:
    """Given the member of a namespace (ie, either class or module),
    creates a summary for that member.
    """
    classification = ObjClassification.from_obj(normalized_obj.obj_or_stub)
    summary_class = classification.get_summary_class()
    if parent_crossref is None:
        crossref = None
    else:
        crossref = parent_crossref / GetattrTraversal(attr_name)
        parent_namespace[attr_name] = crossref

    logger.debug(
        'Summarizing namespace member for crossref=%s (module %s)',
        crossref, module_name)

    # This seems, at first glance, to be weird. Like, how can we have a
    # module here? Except if you do ``import foo``... welp, now you have
    # a module object!
    if classification.is_module:
        return _create_substitute_crossref_summary(
            normalized_obj.obj_or_stub.__name__,
            None,
            attr_name,
            crossref,
            classification,
            normalized_obj,
            summary_metadata_factory)

    # Note that this will have a false positive on some edge cases -- for
    # example, if you said ``Class.foo = dict.get`` as an imperative mixin
    # or something like that, we're not going to be able to figure that out.
    # TODO: we need some kind of escape hatch for that scenario!
    elif (
        isinstance(module_name, str)
        and isinstance(normalized_obj.canonical_module, str)
        and module_name != normalized_obj.canonical_module
    ):
        return _create_substitute_crossref_summary(
            normalized_obj.canonical_module,
            normalized_obj.canonical_name,
            attr_name,
            crossref,
            classification,
            normalized_obj,
            summary_metadata_factory)

    elif summary_class is not None and issubclass(
        summary_class,
        ClassSummary | VariableSummary | CallableSummary | CrossrefSummary
        | TypeVarSummary
    ):
        factory = _summary_factories[summary_class]
        return factory(
            attr_name,
            parent_namespace,
            normalized_obj,
            classification,
            summary_metadata_factory=summary_metadata_factory,
            module_globals=module_globals,
            in_class=in_class)


def _create_substitute_crossref_summary[T: SummaryMetadataProtocol](
        module_name: str,
        toplevel_name: str | None | Literal[Singleton.UNKNOWN],
        attr_name: str,
        crossref: Crossref | None,
        classification: ObjClassification,
        normalized_obj: NormalizedObj,
        summary_metadata_factory: SummaryMetadataFactoryProtocol[T],
        ) -> CrossrefSummary:
    if toplevel_name is Singleton.UNKNOWN:
        toplevel_name = None

    metadata = summary_metadata_factory(
        classification=classification,
        summary_class=CrossrefSummary,
        crossref=crossref,
        annotateds=tuple(
            LazyResolvingValue.from_annotated(annotated)
            for annotated in normalized_obj.annotateds),
        metadata=normalized_obj.effective_config.metadata or {})
    metadata.id_ = normalized_obj.effective_config.id_
    metadata.extracted_inclusion = \
        normalized_obj.effective_config.include_in_docs
    metadata.crossref_namespace = {}
    metadata.canonical_module = module_name
    return CrossrefSummary(
        name=attr_name,
        crossref=crossref,
        src_crossref=Crossref(
            module_name=module_name,
            toplevel_name=toplevel_name,
            traversals=()),
        typespec=normalized_obj.typespec,
        notes=textify_notes(
            normalized_obj.notes, normalized_obj.effective_config),
        ordering_index=normalized_obj.effective_config.ordering_index,
        child_groups=normalized_obj.effective_config.child_groups or (),
        parent_group_name=
            normalized_obj.effective_config.parent_group_name,
        metadata=metadata)


@_summary_factory(ClassSummary)
def create_class_summary(
        name_in_parent: str,
        parent_crossref_namespace: dict[str, Crossref],
        obj: NormalizedObj,
        classification: ObjClassification,
        *,
        module_globals: dict[str, Any],
        in_class: bool = False,
        summary_metadata_factory: SummaryMetadataFactoryProtocol,
        ) -> ClassSummary:
    src_obj = cast(type, obj.obj_or_stub)
    config = obj.effective_config
    crossref = parent_crossref_namespace.get(name_in_parent)
    logger.debug(
        'Creating class summary for %s (%s)', name_in_parent, crossref)
    # TODO: does this need to support localns with parent typevars? Or is that
    # always, by definition, covered by the module globals?
    try:
        annotations = get_type_hints(
            src_obj,
            globalns=module_globals,
            include_extras=True)
    except Exception as exc:
        logger.info(
            'Failed to get type hints for %s for class analysis.',
            src_obj, exc_info=exc)
        annotations = {}

    # Note that, especially in classes, it's extremely common to have
    # annotations that don't appear in the dict (eg dataclass fields).
    # But we don't want to clobber defined values, so we first extract
    # them here.
    bare_annotations = {
        name: Singleton.MISSING for name in annotations
        if name not in src_obj.__dict__}

    normalized_members: dict[str, NormalizedObj] = {}
    for name, value in itertools.chain(
        # Note that we don't want to do inspect.getmembers here, because
        # it will attempt to traverse the MRO, but we're messing with the
        # MRO as part of the stubbing process
        src_obj.__dict__.items(),
        bare_annotations.items()
    ):
        normalized_members[name] = normalize_namespace_item(
            name,
            crossref,
            value,
            annotations,
            config,
            parent_typevars=obj.typevars)

    namespace = {**parent_crossref_namespace}
    members: dict[
            str,
            ClassSummary | VariableSummary | CallableSummary | CrossrefSummary
        ] = {}
    for name, normalized_obj in normalized_members.items():
        member_summary = _summarize_namespace_member(
            obj.canonical_module,
            module_globals,
            crossref,
            namespace,
            name,
            normalized_obj,
            summary_metadata_factory,
            in_class=True)
        if member_summary is not None:
            members[name] = member_summary

    if has_crossreffed_base(src_obj):
        bases = src_obj._docnote_extract_base_classes
    else:
        # Zeroth is always the class itself, which we want to skip
        bases = src_obj.__mro__[1:]

    if has_crossreffed_metaclass(src_obj):
        metaclass = TypeSpec.from_typehint(
            src_obj._docnote_extract_metaclass,
            typevars=obj.typevars)
    elif (runtime_metaclass := type(src_obj)) is not type:
        metaclass = TypeSpec.from_typehint(
            runtime_metaclass, typevars=obj.typevars)
    else:
        metaclass = None

    metadata = summary_metadata_factory(
        classification=classification,
        summary_class=ClassSummary,
        crossref=crossref,
        annotateds=tuple(
            LazyResolvingValue.from_annotated(annotated)
            for annotated in obj.annotateds),
        metadata=config.metadata or {})
    metadata.id_ = obj.effective_config.id_
    metadata.extracted_inclusion = \
        obj.effective_config.include_in_docs
    metadata.crossref_namespace = namespace
    metadata.canonical_module = (
        obj.canonical_module if obj.canonical_module is not Singleton.UNKNOWN
        else None)

    typevars = getattr(src_obj, '__type_params__', ())
    tv_summaries = frozenset({
        _make_typevar_summary_direct(
            typevar,
            parent_crossref=crossref,
            parent_crossref_namespace=namespace,
            parent_typevars=obj.typevars,
            parent_canonical_module=obj.canonical_module,
            summary_metadata_factory=summary_metadata_factory)
        for typevar in typevars})

    return ClassSummary(
        # Note: might differ from src_obj.__name__
        name=name_in_parent,
        crossref=crossref,
        ordering_index=obj.effective_config.ordering_index,
        child_groups=config.child_groups or (),
        parent_group_name=config.parent_group_name,
        metadata=metadata,
        metaclass=metaclass,
        typevars=tv_summaries,
        bases=tuple(
            TypeSpec.from_typehint(base, typevars=obj.typevars)
            for base in bases),
        members=frozenset(members.values()),
        docstring=extract_docstring(src_obj, config),)


@_summary_factory(CallableSummary)
def create_callable_summary(  # noqa: C901, PLR0912
        name_in_parent: str,
        parent_crossref_namespace: dict[str, Crossref],
        obj: NormalizedObj,
        classification: ObjClassification,
        *,
        in_class: bool = False,
        module_globals: dict[str, Any],
        summary_metadata_factory: SummaryMetadataFactoryProtocol,
        ) -> CallableSummary:
    """Given an object and its classification, construct a
    summary instance, populating it with any required children.
    """
    crossref = parent_crossref_namespace.get(name_in_parent)
    src_obj = obj.obj_or_stub
    canonical_module = (
        obj.canonical_module
        if obj.canonical_module is not Singleton.UNKNOWN
        else None)
    # This MUST happen before unwrapping staticmethods and classmethods,
    # otherwise we end up back at the original function
    method_type = MethodType.classify(src_obj, in_class)

    # Staticmethods and classmethods are wrapped into descriptors that
    # aren't callables. We need to unwrap them first to be able to get
    # their signatures.
    if isinstance(src_obj, (staticmethod, classmethod)):
        src_obj = src_obj.__func__

    # Note that this already has any ``@docnote(DocnoteConfig(...))``
    # attachments (on the **implementation**) attached; we don't need to
    # re-apply them.
    implementation_config = obj.effective_config

    # Note that this doesn't include the implementation, only the
    # overloads, so we still need to merge it with the signature from
    # inspecting the implementation
    try:
        overloads = get_overloads(src_obj)
    except AttributeError:
        logger.debug(
            'Failed to check overloads for %s. This is usually because it was '
            + 'a stlib object without a __module__ attribute, ex '
            + '``Decimal.__repr__``; however, this might indicate a bug.',
            src_obj)
        overloads = []

    namespace_expansion: dict[str, Crossref] = {}

    signatures: list[SignatureSummary] = []
    if overloads:
        for overload_ in overloads:
            overload_config_params: DocnoteConfigParams = {
                **implementation_config.get_stackables()}

            # This gets any config that was attrached via decorator.
            # TODO: we need a more general-purpose way of getting this
            # out, instead of spreading it between here and normalization
            if hasattr(overload_, DOCNOTE_CONFIG_ATTR):
                overload_config_params.update(
                    getattr(overload_, DOCNOTE_CONFIG_ATTR)
                    .as_nontotal_dict())

            overload_config = DocnoteConfig(**overload_config_params)
            # Note: we don't want to use this directly, because it would
            # incorrectly overlap with the no-overload traversal, and
            # because it would be redundant with all other un-indexed
            # overloads.
            if crossref is None:
                signature_crossref = None
                namespace_expansion_key = ''
            elif overload_config.ordering_index is None:
                signature_crossref = crossref / SyntacticTraversal(
                    type_=SyntacticTraversalType.ANONYMOUS_OVERLOAD,
                    key='')
                namespace_expansion_key = ''
            else:
                # Note that this is required to make the signature, so
                # we can't wait until we know if a signature was actually
                # created; we have to just pop it afterwards if not.
                signature_crossref = crossref / SignatureTraversal(
                    overload_config.ordering_index)
                # Doing this here instead of after the signature is created
                # keeps the branch count lower
                namespace_expansion_key = \
                    f'__signature_{overload_config.ordering_index}__'
                namespace_expansion[namespace_expansion_key] = \
                    signature_crossref

            signature = _make_signature(
                parent_crossref_namespace,
                overload_,
                canonical_module,
                signature_crossref,
                signature_config=overload_config,
                parent_effective_config=obj.effective_config,
                parent_typevars=obj.typevars,
                module_globals=module_globals,
                summary_metadata_factory=summary_metadata_factory)
            if signature is None:
                namespace_expansion.pop(namespace_expansion_key, None)
            else:
                signatures.append(signature)

    # ``else`` is correct! If it defines overloads, then we want to rely ONLY
    # upon the overloads for the signature, and treat the implementation as
    # irrelevant for documentation purposes.
    else:
        signature_crossref = (
            crossref / SignatureTraversal(None)
            if crossref is not None
            else None)
        signature = _make_signature(
            parent_crossref_namespace,
            src_obj,
            canonical_module,
            signature_crossref,
            signature_config=implementation_config,
            parent_effective_config=obj.effective_config,
            parent_typevars=obj.typevars,
            module_globals=module_globals,
            summary_metadata_factory=summary_metadata_factory)

        # None signatures happen very occasionally for ex C extensions
        if signature is not None:
            signatures.append(signature)

            if signature_crossref is not None:
                namespace_expansion['__signature_impl__'] = signature_crossref

    crossref = parent_crossref_namespace.get(name_in_parent)
    metadata = summary_metadata_factory(
        classification=classification,
        summary_class=CallableSummary,
        crossref=crossref,
        annotateds=tuple(
            LazyResolvingValue.from_annotated(annotated)
            for annotated in obj.annotateds),
        metadata=obj.effective_config.metadata or {})
    metadata.id_ = obj.effective_config.id_
    metadata.extracted_inclusion = \
        obj.effective_config.include_in_docs
    metadata.crossref_namespace = {
        **parent_crossref_namespace, **namespace_expansion}
    metadata.canonical_module = canonical_module

    return CallableSummary(
        # Note: might differ from src_obj.__name__
        name=name_in_parent,
        crossref=crossref,
        ordering_index=obj.effective_config.ordering_index,
        child_groups=obj.effective_config.child_groups or (),
        parent_group_name=obj.effective_config.parent_group_name,
        metadata=metadata,
        # Note that this is always the implementation docstring.
        docstring=extract_docstring(src_obj, implementation_config),
        color=CallableColor.ASYNC if classification.is_async
            else CallableColor.SYNC,
        method_type=method_type,
        is_generator=classification.is_any_generator,
        signatures=frozenset(signatures))


def _make_signature(  # noqa: PLR0913, PLR0915
        parent_crossref_namespace: dict[str, Crossref],
        src_obj: Callable,
        canonical_module: str | None,
        signature_crossref: Crossref | None,
        signature_config: DocnoteConfig,
        parent_effective_config: DocnoteConfig,
        *,
        parent_typevars: Mapping[TypeVar, Crossref],
        module_globals: dict[str, Any],
        summary_metadata_factory: SummaryMetadataFactoryProtocol,
        ) -> SignatureSummary | None:
    """Extracts all the parameter-specific infos you need to create a
    signature object (including the retval), combining both the actual
    callable's signature and any type hints defined on the callable.

    TODO: this needs to add support for the object filters from the
    parent!
    """
    # TODO: does this need to include anything else?
    localns = {
        typevar.__name__: crossref
        for typevar, crossref in parent_typevars.items()}
    params: list[ParamSummary] = []
    try:
        annotations = get_type_hints(
            src_obj,
            globalns=module_globals,
            localns=localns,
            include_extras=True)
    except Exception as exc:
        logger.info(
            'Failed to get type hints for %s for signature analysis. This is '
            + 'usually because the object is a from a stdlib/builtin callable '
            + 'or a C extension, but it might indicate a bug.',
            src_obj, exc_info=exc)
        annotations = {}

    try:
        raw_sig = inspect.Signature.from_callable(src_obj)
    except ValueError:
        logger.debug(
            'Failed to extract signature from %s. This is usually because the '
            + 'object is a from a stdlib/builtin callable or a C extension, '
            + 'but it might indicate a bug.', src_obj)
        return None

    # Note: we use the same namespace for all params in the signature,
    # and for the signature itself. Literally the same object, not
    # copies thereof. This ensures that all of the params are added,
    # so that params can reference each other.
    signature_namespace: dict[str, Crossref] = {
        **parent_crossref_namespace}
    signature_typevars = extend_typevars(
        parent_crossref=signature_crossref,
        parent_typevars=parent_typevars,
        obj=src_obj)

    for param_index, (param_name, raw_param) in enumerate(
        raw_sig.parameters.items()
    ):
        # Note: there's no escaping this. If we don't have a way to reference
        # the parent signature, we also don't have a way to reference sibling
        # parameters. It doesn't matter that they're relative to each other;
        # the underlying architecture assumes all crossrefs must work globally,
        # so there's no such thing as a relative crossref. (also, that would
        # make the implementation waaaaay more complicated)
        if signature_crossref is None:
            param_crossref = None
        else:
            param_crossref = signature_crossref / ParamTraversal(param_name)
            signature_namespace[param_name] = param_crossref

        style = ParamStyle.from_inspect_param_kind(raw_param.kind)
        if raw_param.default is inspect.Parameter.empty:
            default = None
        else:
            default = LazyResolvingValue.from_annotated(
                raw_param.default)

        annotation = annotations.get(param_name, Singleton.MISSING)
        normalized_annotation = normalize_annotation(
            annotation,
            typevars=signature_typevars)
        combined_params: DocnoteConfigParams = {
            **parent_effective_config.get_stackables(),
            **normalized_annotation.config_params}
        effective_config = DocnoteConfig(**combined_params)

        param_metadata = summary_metadata_factory(
            classification=None,
            summary_class=ParamSummary,
            crossref=param_crossref,
            annotateds=normalized_annotation.annotateds,
            metadata=effective_config.metadata or {})
        param_metadata.id_ = effective_config.id_
        param_metadata.extracted_inclusion = \
            effective_config.include_in_docs
        param_metadata.crossref_namespace = signature_namespace
        param_metadata.canonical_module = canonical_module

        params.append(ParamSummary(
            name=param_name,
            index=param_index,
            crossref=param_crossref,
            ordering_index=effective_config.ordering_index,
            child_groups=effective_config.child_groups or (),
            parent_group_name=effective_config.parent_group_name,
            notes=textify_notes(
                normalized_annotation.notes, effective_config),
            style=style,
            default=default,
            typespec=normalized_annotation.typespec,
            metadata=param_metadata))

    if signature_crossref is None:
        retval_crossref = None
    else:
        retval_crossref = signature_crossref / ParamTraversal('return')
        signature_namespace['return'] = retval_crossref
    retval_annotation = annotations.get('return', Singleton.MISSING)
    normalized_retval_annotation = normalize_annotation(
        retval_annotation, typevars=signature_typevars)
    combined_params: DocnoteConfigParams = {
        **parent_effective_config.get_stackables(),
        **normalized_retval_annotation.config_params}
    retval_effective_config = DocnoteConfig(**combined_params)

    retval_metadata = summary_metadata_factory(
        classification=None,
        summary_class=RetvalSummary,
        crossref=retval_crossref,
        annotateds=normalized_retval_annotation.annotateds,
        metadata=retval_effective_config.metadata or {})
    retval_metadata.id_ = retval_effective_config.id_
    retval_metadata.extracted_inclusion = \
        retval_effective_config.include_in_docs
    retval_metadata.crossref_namespace = signature_namespace
    retval_metadata.canonical_module = canonical_module

    signature_metadata = summary_metadata_factory(
        classification=None,
        summary_class=SignatureSummary,
        crossref=signature_crossref,
        annotateds=(),
        metadata=signature_config.metadata or {})
    signature_metadata.id_ = signature_config.id_
    signature_metadata.extracted_inclusion = \
        signature_config.include_in_docs
    signature_metadata.crossref_namespace = signature_namespace
    signature_metadata.canonical_module = canonical_module

    typevars = getattr(src_obj, '__type_params__', ())
    tv_summaries = frozenset({
        _make_typevar_summary_direct(
            typevar,
            parent_crossref=signature_crossref,
            parent_crossref_namespace=signature_namespace,
            parent_typevars=parent_typevars,
            parent_canonical_module=canonical_module,
            summary_metadata_factory=summary_metadata_factory)
        for typevar in typevars})

    return SignatureSummary(
        params=frozenset(params),
        retval=RetvalSummary(
            typespec=normalized_retval_annotation.typespec,
            notes=textify_notes(
                normalized_retval_annotation.notes, retval_effective_config),
            crossref=retval_crossref,
            ordering_index=retval_effective_config.ordering_index,
            child_groups=retval_effective_config.child_groups or (),
            parent_group_name=retval_effective_config.parent_group_name,
            metadata=retval_metadata
        ),
        docstring=None,
        typevars=tv_summaries,
        crossref=signature_crossref,
        ordering_index=signature_config.ordering_index,
        child_groups=signature_config.child_groups or (),
        parent_group_name=signature_config.parent_group_name,
        metadata=signature_metadata)


def _make_typevar_summary_direct(
        src_obj: TypeVar,
        parent_crossref: Crossref | None,
        parent_crossref_namespace: dict[str, Crossref],
        parent_canonical_module: str | Literal[Singleton.UNKNOWN] | None,
        parent_typevars: Mapping[TypeVar, Crossref],
        *,
        summary_metadata_factory: SummaryMetadataFactoryProtocol,
        ) -> TypeVarSummary:
    """This is used to create typevarsummary objects for anything that
    was defined using the type var SYNTAX -- ie anything not
    module-level.
    """
    if parent_crossref is None:
        crossref = None
    else:
        crossref = parent_crossref / SyntacticTraversal(
            type_=SyntacticTraversalType.TYPEVAR,
            key=src_obj.__name__)

    metadata = summary_metadata_factory(
        classification=ObjClassification.from_obj(src_obj),
        summary_class=TypeVarSummary,
        crossref=crossref,
        annotateds=(),
        metadata={})
    metadata.id_ = None
    metadata.extracted_inclusion = None
    metadata.crossref_namespace = parent_crossref_namespace
    metadata.canonical_module = (
        parent_canonical_module
        if parent_canonical_module is not Singleton.UNKNOWN
        else None)

    if hasattr(src_obj, 'has_default') and src_obj.has_default():  # type: ignore
        default = TypeSpec.from_typehint(
            src_obj.__default__, typevars=parent_typevars)  # type: ignore
    else:
        default = None

    # This is the default. Creating an actual binding to None doesn't really
    # make sense, so we'll stick to the convention.
    if src_obj.__bound__ is None:
        bound = None
    else:
        bound = TypeSpec.from_typehint(
            src_obj.__bound__, typevars=parent_typevars)

    return TypeVarSummary(
        name=src_obj.__name__,
        crossref=crossref,
        bound=bound,
        constraints=tuple(
            TypeSpec.from_typehint(constraint, typevars=parent_typevars)
            for constraint in src_obj.__constraints__),
        default=default,
        ordering_index=None,
        child_groups=(),
        parent_group_name=None,
        metadata=metadata)
