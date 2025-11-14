from __future__ import annotations

import itertools
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import ModuleType
from types import NoneType
from types import UnionType
from typing import Annotated
from typing import Any
from typing import ClassVar
from typing import Final
from typing import Literal
from typing import LiteralString
from typing import Never
from typing import NoReturn
from typing import NotRequired
from typing import Required
from typing import Self
from typing import TypeAliasType
from typing import TypedDict
from typing import TypeVar
from typing import Union
from typing import cast
from typing import get_args as get_type_args
from typing import get_origin
from typing import get_type_hints

try:
    from typing import ReadOnly  # type: ignore
except ImportError:
    # This can just be whatever; doesn't matter -- as long as it's guaranteed
    # to fail an ``is`` comparison!
    ReadOnly = object()

from docnote import DOCNOTE_CONFIG_ATTR
from docnote import DocnoteConfig
from docnote import DocnoteConfigParams
from docnote import Note

from docnote_extract._extraction import ModulePostExtraction
from docnote_extract._extraction import TrackingRegistry
from docnote_extract._module_tree import ConfiguredModuleTreeNode
from docnote_extract._utils import validate_config
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import Crossreffed
from docnote_extract.crossrefs import SyntacticTraversal
from docnote_extract.crossrefs import SyntacticTraversalType
from docnote_extract.crossrefs import is_crossreffed
from docnote_extract.summaries import Singleton

logger = logging.getLogger(__name__)


def normalize_namespace_item(
        name_in_parent: str,
        crossref: Crossref | None,
        value: Any,
        parent_annotations: dict[str, Any],
        parent_effective_config: DocnoteConfig,
        *,
        parent_typevars: Mapping[TypeVar, Crossref]
        ) -> NormalizedObj:
    """Given a single item from a namespace (ie, **not a module**), this
    creates a NormalizedObj and returns it.
    """
    raw_annotation = parent_annotations.get(name_in_parent, Singleton.MISSING)
    typevars = extend_typevars(
        parent_crossref=crossref,
        parent_typevars=parent_typevars,
        obj=value)
    normalized_annotation = normalize_annotation(
        raw_annotation, typevars=typevars)

    config_params: DocnoteConfigParams = \
        parent_effective_config.get_stackables()
    config_params.update(normalized_annotation.config_params)

    # We need to do some unwrapping here of misc common things. This will
    # let us extract the docstring along with any config decorations, but
    # doesn't do any special handling for properties (yet)
    if isinstance(value, property):
        unwrapped = value.fget
    elif isinstance(value, (staticmethod, classmethod)):
        unwrapped = value.__func__
    else:
        unwrapped = value

    # This gets any config that was attrached via decorator, for classes
    # and functions.
    if hasattr(unwrapped, DOCNOTE_CONFIG_ATTR):
        decorated_config = getattr(unwrapped, DOCNOTE_CONFIG_ATTR)
        # Beware: remove this, and you'll run into infinite loops!
        if not is_crossreffed(decorated_config):
            config_params.update(decorated_config.as_nontotal_dict())
    effective_config = DocnoteConfig(**config_params)

    canonical_module: str | Literal[Singleton.UNKNOWN] | None
    # First of all, if the config defines an override, use that!
    if effective_config.canonical_module is not None:
        canonical_module = effective_config.canonical_module
    # We have to be careful here, because the __module__ of the singletons
    # is actually docnote_extract.summaries!
    elif unwrapped is Singleton.MISSING:
        canonical_module = Singleton.UNKNOWN
    # IMPORTANT NOTE: we cannot infer that the module of the namespace object
    # matches that of its parent, because of subclassing. You also can't rely
    # upon its existence or non-existence in the parent annotations for the
    # same reason: ``get_type_hints`` also retrieves the hints in the
    # superclass!
    else:
        canonical_module = getattr(unwrapped, '__module__', Singleton.UNKNOWN)

    if effective_config.canonical_name is None:
        canonical_name = None
    else:
        canonical_name = effective_config.canonical_name

    # All done. Filtering comes later; here we JUST want to do the
    # normalization!
    return NormalizedObj(
        # We want this to be the original object, not the wrapped one, because
        # that affects how we do classification during summarization
        obj_or_stub=value,
        annotateds=normalized_annotation.annotateds,
        effective_config=effective_config,
        notes=normalized_annotation.notes,
        typespec=normalized_annotation.typespec,
        typevars=typevars,
        canonical_module=canonical_module,
        canonical_name=canonical_name)


@dataclass(slots=True)
class NormalizedAnnotation:
    """
    """
    typespec: TypeSpec | None
    notes: tuple[Note, ...]
    config_params: DocnoteConfigParams
    annotateds: tuple[LazyResolvingValue, ...]


def normalize_annotation(
        annotation: Any | Literal[Singleton.MISSING],
        *,
        typevars: Mapping[TypeVar, Crossref]
        ) -> NormalizedAnnotation:
    """Given the annotation for a particular $thing, this extracts out
    any the type hint itself, any attached notes, config params, and
    also any additional ``Annotated`` extras.
    """
    if annotation is Singleton.MISSING:
        return NormalizedAnnotation(
            typespec=None,
            notes=(),
            config_params={},
            annotateds=())
    if is_crossreffed(annotation):
        return NormalizedAnnotation(
            typespec=TypeSpec.from_typehint(annotation, typevars=typevars),
            notes=(),
            config_params={},
            annotateds=())

    all_annotateds: tuple[Any, ...]
    origin = get_origin(annotation)
    if origin is Annotated:
        type_ = annotation.__origin__
        all_annotateds = annotation.__metadata__

    else:
        type_ = annotation
        all_annotateds = ()

    config_params: DocnoteConfigParams = {}

    notes: list[Note] = []
    external_annotateds: list[LazyResolvingValue] = []
    for annotated in all_annotateds:
        # Note: if the note has its own config, that gets used later; it
        # doesn't modify the rest of the notes!
        if isinstance(annotated, Note):
            notes.append(annotated)
        elif isinstance(annotated, DocnoteConfig):
            config_params.update(annotated.as_nontotal_dict())
        else:
            external_annotateds.append(
                LazyResolvingValue.from_annotated(annotated))

    return NormalizedAnnotation(
        typespec=TypeSpec.from_typehint(type_, typevars=typevars),
        notes=tuple(notes),
        config_params=config_params,
        annotateds=tuple(external_annotateds))


def normalize_module_dict(
        module: ModulePostExtraction,
        module_tree: Annotated[
                ConfiguredModuleTreeNode,
                Note('''Note that this needs to be the ^^full^^ firstparty
                    module tree, and not just the node for the current module!
                    ''')]
        ) -> dict[str, NormalizedObj]:
    from_annotations: dict[str, Any] = get_type_hints(
        module, include_extras=True)
    dunder_all: set[str] = set(getattr(module, '__all__', ()))
    retval: dict[str, NormalizedObj] = {}

    # First -- very first -- we need to collect any typevars from the module
    # and make them available for normalized objects. We need to do this ahead
    # of time because they all need to be available for any other members of
    # the module (to deal with forward references).
    # Note that we need to keep the typevars within the normalized result,
    # because we need to preserve the crossref. It's up to the docs
    # generation library to do anything with the typevar, so keeping
    # it around is the only way to document things that use it
    mutable_typevars: dict[TypeVar, Crossref] = {
        obj: Crossref(module_name=module.__name__, toplevel_name=name)
        for name, obj in module.__dict__.items()
        if isinstance(obj, TypeVar)}

    # Note that, though rare, it's theoretically possible for values to appear
    # in a module's annotations but not its __dict__. (This is much more common
    # for classes, but syntactically valid here, and might be used to surface
    # some kind of docnote or something.)
    bare_annotations = {
        name: Singleton.MISSING for name in from_annotations
        if name not in module.__dict__}

    for name, obj in itertools.chain(
        module.__dict__.items(),
        bare_annotations.items()
    ):
        canonical_module, canonical_name = _get_or_infer_canonical_origin(
            name,
            obj,
            tracking_registry=module._docnote_extract_import_tracking_registry,
            containing_module=module.__name__,
            containing_dunder_all=dunder_all,
            containing_annotation_names=set(from_annotations))

        # Here we're starting to construct an effective config for the object.
        # Note that this is kinda unseparable from the next part, since we're
        # iterating over all of the annotations and separating them out into
        # docnote-vs-not. I mean, yes, we could actually carve this out into
        # a separate function, but it would be more effort than it's worth.
        config_params: DocnoteConfigParams
        if canonical_module is Singleton.UNKNOWN or canonical_module is None:
            config_params = {}
        else:
            # Remember that we're checking EVERYTHING in the module right now,
            # including things we've imported, so this might be outside the
            # firstparty tree. Therefore, we need a fallback here.
            try:
                canonical_module_node = module_tree.find(canonical_module)
            except (KeyError, ValueError):
                config_params = {}
            else:
                config_params = (
                    canonical_module_node.effective_config.get_stackables())

        # This gets any config that was attrached via decorator, for classes
        # and functions.
        if hasattr(obj, DOCNOTE_CONFIG_ATTR):
            decorated_config = getattr(obj, DOCNOTE_CONFIG_ATTR)
            # Beware: remove this, and you'll run into infinite loops!
            if not is_crossreffed(decorated_config):
                config_params.update(decorated_config.as_nontotal_dict())

        raw_annotation = from_annotations.get(name, Singleton.MISSING)
        normalized_obj_annotations = normalize_annotation(
            raw_annotation, typevars=mutable_typevars)
        config_params.update(normalized_obj_annotations.config_params)

        effective_config = DocnoteConfig(**config_params)
        if effective_config.canonical_module is not None:
            canonical_module = effective_config.canonical_module
        if effective_config.canonical_name is not None:
            canonical_name = effective_config.canonical_name

        if (
            isinstance(canonical_module, str)
            and isinstance(canonical_name, str)
        ):
            obj_crossref = Crossref(
                module_name=canonical_module,
                toplevel_name=canonical_name)
        else:
            obj_crossref = None

        # All done. Filtering comes later; here we JUST want to do the
        # normalization!
        retval[name] = NormalizedObj(
            obj_or_stub=obj,
            annotateds=normalized_obj_annotations.annotateds,
            effective_config=effective_config,
            notes=normalized_obj_annotations.notes,
            typespec=normalized_obj_annotations.typespec,
            typevars=extend_typevars(
                parent_crossref=obj_crossref,
                parent_typevars=mutable_typevars,
                obj=obj),
            canonical_module=canonical_module,
            canonical_name=canonical_name)

    return retval


def _get_or_infer_canonical_origin(  # noqa: PLR0911
        name_in_containing_module: str,
        obj: Any,
        *,
        tracking_registry: TrackingRegistry,
        containing_module: str,
        containing_dunder_all: set[str],
        containing_annotation_names: set[str]
        ) -> tuple[
            str | Literal[Singleton.UNKNOWN] | None,
            str | Literal[Singleton.UNKNOWN] | None]:
    """Call this on a module member to retrieve its __module__
    attribute, as well as the name it was assigned within that module,
    or to try and infer the canonical source of the object when no
    __module__ attribute is available.

    This function is purely responsible for checking the object itself.
    Callers are expected to override the result if there is an attached
    docnote config with an explicit canonical origin.
    """
    if isinstance(obj, ModuleType):
        return None, None

    # This can only happen if it's a bare annotation -- something coming
    # directly from the containing module's annotations, but without a value
    # assigned. That's weird, but we can safely assume it's coming from inside
    # the house (so to speak)
    if obj is Singleton.MISSING:
        return containing_module, name_in_containing_module

    assignable_to_module = (
        # If the name is in the containing module's dunder all, assume it's
        # canonically part of the module. If this behavior is not desired,
        # you need to explicitly set the override via docnote config
        name_in_containing_module in containing_dunder_all
        # Same deal if the name is in the module's annotations. This is an
        # even stronger chance of correct inference, since re-exports aren't
        # likely to re-annotate things they're importing
        or name_in_containing_module in containing_annotation_names)

    if is_crossreffed(obj):
        # It should be pretty safe (and is reasonable) to assume that something
        # crossreffed but also assignable to the module is either a re-export,
        # or (for example) an instance of an imported class. In any case, this
        # can be overridden via the docnote config if undesired.
        if assignable_to_module:
            return containing_module, name_in_containing_module

        metadata = obj._docnote_extract_metadata
        if metadata.traversals:
            logger.warning(
                'Canonical source not inferred due to traversals on module '
                + 'attribute. %s:%s -> %s',
                containing_module, name_in_containing_module, metadata)
            return Singleton.UNKNOWN, Singleton.UNKNOWN

        return metadata.module_name, metadata.toplevel_name

    # Do this next. This allows us more precise tracking of non-stubbed objects
    # that are imported from a re-exported location. In other words, we want
    # the import location to be canonical, and would prefer to have that rather
    # than the definition location, which is what we would get from
    # ``__module__`` and ``__name__`.
    canonical_from_registry = tracking_registry.get(id(obj), None)
    # Note that the None could be coming EITHER from the default in the above
    # .get(), OR because we had multiple conflicting references to it, and we
    # therefore can't use the registry to infer its location.
    if canonical_from_registry is not None:
        return canonical_from_registry

    canonical_module, canonical_name = _get_dunder_module_and_name(obj)
    if canonical_module is None:
        # Summary:
        # ++  not imported from a tracking module (or at least not uniquely
        #     so) -- therefore, either a reftype or an actual value
        # ++  no ``__name__`` and/or ``__module__`` attribute
        # ++  name contained within module annotations or dunder all
        # Conclusion: assume it's a canonical member
        if assignable_to_module:
            canonical_module = containing_module
            canonical_name = name_in_containing_module

        else:
            canonical_module = Singleton.UNKNOWN
            canonical_name = Singleton.UNKNOWN

    # Purely here to be defensive.
    elif canonical_name is None:
        raise RuntimeError(
            'Impossible branch! ``__module__`` detected without ``__name__``!')

    return canonical_module, canonical_name


def _get_dunder_module_and_name(
        obj: Any
        ) -> tuple[str, str] | tuple[None, None]:
    """So, things are a bit more complicated than simply getting the
    ``__module__`` attribute of an object and using it. The problem is
    that INSTANCES of a class will inherit its ``__module__`` value.
    This causes problems with... well, basically everything ^^except^^
    classes, functions, methods, descriptors, and generators that are
    defined within the module being inspected.

    I thought about trying to import the ``__module__`` and then
    comparing the actual ``obj`` against ``__module__.__name__``, but
    that's a whole can of worms.

    Instead, we're simply limiting the ``__module__`` value to only
    return something if the ``__name__`` is also defined. This should
    limit it to only the kinds of objects that don't cause problems.
    """
    canonical_name = getattr(obj, '__name__', None)
    if canonical_name is None:
        return None, None
    else:
        return obj.__module__, canonical_name


def extend_typevars(
        parent_crossref: Crossref | None,
        parent_typevars: Mapping[TypeVar, Crossref],
        obj: Any
        ) -> Mapping[TypeVar, Crossref]:
    """This creates a new typevars mapping that includes any new
    typevars defined on the passed object. If it has none, it simply
    creates a copy of the parent typevars.
    """
    typevars = {**parent_typevars}

    raw_typevars = getattr(obj, '__type_params__', ())
    if parent_crossref is None:
        if raw_typevars:
            logging.warning(
                'Failed to extend typevars for %s due to missing parent '
                + 'crossref!', obj)

    else:
        for typevar in raw_typevars:
            typevars[typevar] = parent_crossref / SyntacticTraversal(
                type_=SyntacticTraversalType.TYPEVAR,
                key=typevar.__name__)

    return typevars


@dataclass(slots=True)
class NormalizedObj:
    """This is a normalized representation of an object. It contains the
    (stubbed) runtime value of the object along with any annotateds
    (from ``Annotated``), as well as the unpacked-from-``Annotated``
    type itself.

    TODO: we should at least consider moving crossref generation into
    normalized objects, for actual directly-accessible object. The only
    challenge is that there are documentable things that cannot be
    reached during normalization (for example, parameters in function
    signatures), which therefore can't get crossrefs that way. Also,
    traversals could potentially be problematic.
    """
    obj_or_stub: Annotated[
            Any,
            Note('''This is the actual runtime value of the object. It might
                be a ``RefType`` stub or an actual object.''')]
    notes: tuple[Note, ...]
    effective_config: Annotated[
            DocnoteConfig,
            Note('''This contains the end result of all direct configs on the
                object, layered on top of any stackable config items from
                parent scope(s).''')]
    annotateds: tuple[object, ...]
    typespec: Annotated[
            TypeSpec | None,
            Note('''This is a normalized representation of the type that was
                declared on the object.''')]
    typevars: Annotated[
            Mapping[TypeVar, Crossref],
            Note('''This contains the cumulative collection of all typevars
                (and their crossrefs) in the current and parent contexts.''')]

    # Where the value was declared. String if known (because it had a
    # __module__ or it had a docnote). None in some weird situations, like
    # object.__init_subclass__.
    canonical_module: str | Literal[Singleton.UNKNOWN] | None
    # What name the object had in the module it was declared. String if
    # known, None if not applicable (because it isn't a direct child of a
    # module)
    canonical_name: str | Literal[Singleton.UNKNOWN] | None

    def __post_init__(self):
        validate_config(
            self.effective_config,
            f'Object effective config for {self.obj_or_stub} '
            + f'({self.canonical_module=}, {self.canonical_name=})')


class _TypeSpecSpecialForms(TypedDict, total=False):
    """This keeps track of any encountered special forms so they can be
    applied to the root TypeSpec.
    """
    has_classvar: bool
    has_final: bool
    has_required: bool
    has_not_required: bool
    has_read_only: bool


def _extract_special_forms(origin: Any) -> _TypeSpecSpecialForms | None:
    if origin is ClassVar:
        return {'has_classvar': True}
    if origin is Final:
        return {'has_final': True}
    if origin is Required:
        return {'has_required': True}
    if origin is NotRequired:
        return {'has_not_required': True}
    if origin is ReadOnly:
        return {'has_read_only': True}


@dataclass(slots=True, frozen=True)
class TypeSpec:
    """This is used as a container for ``NormalizedType``s, which stores
    information about any applied special forms for the type (ex
    ``ClassVar``, ``Final``, etc). Therefore it is only useful as a
    wrapper in contexts where those special forms are also valid.

    **These are not meant to be constructed directly.** Instead, use the
    ``from_typehint`` method to create them.
    """
    normtype: NormalizedType
    has_classvar: bool = False
    has_final: bool = False
    has_required: bool = False
    has_not_required: bool = False
    has_read_only: bool = False

    # The noqa flags are due to normalization hell; they're all about this
    # being too complicated of a method
    @classmethod
    def from_typehint(  # noqa: C901, PLR0912
            cls,
            typehint:
                Crossreffed | type | TypeVar | TypeAliasType | UnionType
                | list | None,
            *,
            typevars: Mapping[TypeVar, Crossref],
            _special_forms: _TypeSpecSpecialForms | None = None
            ) -> TypeSpec:
        """Converts an extracted type hint into a NormalizedType
        instance.
        """
        if _special_forms is None:
            special_forms = _TypeSpecSpecialForms()
        else:
            special_forms = _special_forms

        normtype: NormalizedType
        if is_crossreffed(typehint):
            normtype = NormalizedConcreteType(
                typehint._docnote_extract_metadata)

        elif NormalizedSpecialType.is_special_type(typehint):
            normtype = NormalizedSpecialType.from_typehint(typehint)

        elif isinstance(typehint, UnionType):
            normtype = NormalizedUnionType.from_typehint(
                get_type_args(typehint), typevars=typevars)

        elif isinstance(typehint, TypeAliasType):
            # Note that any type params here aren't relevant; they won't be
            # bound vars! They'll just be as-declared on the alias, which will
            # be documented (if needed) on the alias itself.
            normtype = NormalizedConcreteType(
                Crossref.from_object(typehint, typevars=typevars))

        # This is the case in some special forms, like the argspec for
        # callables
        elif isinstance(typehint, list):
            normtype = NormalizedEmptyGenericType(
                params=tuple(
                    cls.from_typehint(generic_arg, typevars=typevars)
                    for generic_arg in typehint))

        else:
            # Note that this will return ``None`` for generics that have not
            # been passed a parameter. This is usually not a recoverable
            # situation; the only exception is type vars, which have the
            # ``__type_params__`` attribute, but that is only available for
            # type aliases, which we already handled.
            origin = get_origin(typehint)
            # ----------------  Non-generics
            if origin is None:
                # This is necessary because we're using TypeGuard instead of
                # TypeIs so that we can have pseudo-intersections.
                typehint = cast(type, typehint)

                normtype = NormalizedConcreteType(
                    Crossref.from_object(
                        typehint,
                        typevars=typevars,
                        # Some stdlib sentinels require fallbacks, ex KW_ONLY
                        # from dataclasses
                        allow_fallback=True))

            # ---------------- Special-case generics
            elif origin is Literal:
                normtype = NormalizedLiteralType.from_typehint(
                    typehint, typevars=typevars)

            # THIS MIGHT STILL BE A UNION TYPE!
            # Things typed as ``Optional[...]`` will be converted behind
            # the scenes into a ``_UnionGenericAlias`` type, which is,
            # well, a generic -- **but the origin will be a plain union!**
            elif origin is Union:
                normtype = NormalizedUnionType.from_typehint(
                        get_type_args(typehint), typevars=typevars)

            # Theoretically possible because Annotated doesn't always get
            # collapsed when nested in weird ways.
            elif origin is Annotated:
                # Pyright thinks this is a crossref; no idea why
                typehint = cast(type, typehint)
                # Here we want to fully unpack things and just defer to it,
                # bypassing our usual special forms assignments
                return cls.from_typehint(
                    typehint.__origin__,
                    _special_forms=special_forms,
                    typevars=typevars)

            # We want to normalize the special forms into the root TypeSpec
            # for convenience; otherwise they make processing things difficult
            # downstream, since you're constantly checking for them
            elif (
                update_special_forms := _extract_special_forms(origin)
            ) is not None:
                type_args = get_type_args(typehint)
                if len(type_args) != 1:
                    raise TypeError(
                        'Unsupported arg count for special-form type!',
                        typehint)

                special_forms.update(update_special_forms)
                return cls.from_typehint(
                    type_args[0],
                    _special_forms=special_forms,
                    typevars=typevars)

            # ----------------  (ahem...) Generic generics
            else:
                normtype = NormalizedConcreteType(
                    primary=Crossref.from_object(origin, typevars=typevars),
                    params=tuple(
                        cls.from_typehint(generic_arg, typevars=typevars)
                        for generic_arg in get_type_args(typehint)))

        return cls(normtype, **special_forms)


@dataclass(slots=True, frozen=True)
class NormalizedUnionType:
    """This is used as a container for the members of union types. In
    the future, if python adds an intersection type, it will need to be
    included here as well.
    """
    normtypes: frozenset[NormalizedType]

    @classmethod
    def from_typehint(
            cls,
            typehint: tuple[
                Crossreffed | type | TypeAliasType | UnionType | list, ...],
            *,
            typevars: Mapping[TypeVar, Crossref]
            ) -> NormalizedUnionType:
        norm_types = set()

        for union_member in typehint:
            # Note that we don't want to bubble out any special forms from
            # the parent into the members of the union; that's not the way
            # that typing works. In theory, the type checker will fail this,
            # since it wouldn't be a valid declaration, but we want to be
            # resilient against that (because it's trivial to do so; we just
            # don't pass in the _special_forms argument!)
            norm_types.add(
                TypeSpec.from_typehint(
                    union_member, typevars=typevars
                ).normtype)

        return cls(frozenset(norm_types))


class NormalizedSpecialType(Enum):
    """There are several special types in python; we use this to mark
    them in a way that doesn't require a crossref.
    """
    ANY = Any
    # Deliberately omitting AnyStr since it's deprecated
    LITERAL_STRING = LiteralString
    NEVER = Never
    NORETURN = NoReturn
    SELF = Self
    NONE = NoneType

    @classmethod
    def from_typehint(
            cls,
            typehint: Any,
            ) -> NormalizedSpecialType:
        if typehint is None:
            typehint = NoneType

        return cls(typehint)

    @classmethod
    def is_special_type(cls, typehint: Any) -> bool:
        """Returns if the passed typehint is in fact a special type.
        """
        # Note: we want to be both fast AND resilient against unhashable types,
        # so no sets here!
        return (
            typehint is Any
            or typehint is LiteralString
            or typehint is Never
            or typehint is NoReturn
            or typehint is Self
            or typehint is NoneType
            or typehint is None)


@dataclass(slots=True, frozen=True)
class NormalizedConcreteType:
    """This is used for all type annotations after normalization.
    """
    # None is used for some special forms (for example, the argspec for
    # callables)
    primary: Crossref
    params: tuple[TypeSpec, ...] = ()


@dataclass(slots=True, frozen=True)
class NormalizedEmptyGenericType:
    """This is used for some special-form type annotations that are
    effectively just an empty generic -- for example, the argspec in a
    callable.
    """
    params: tuple[TypeSpec, ...] = ()


@dataclass(slots=True, frozen=True)
class NormalizedLiteralType:
    values: frozenset[int | bool | str | bytes | Crossref]

    @classmethod
    def from_typehint(
            cls,
            typehint: Any,
            *,
            typevars: Mapping[TypeVar, Crossref]
            ) -> NormalizedLiteralType:
        values: set[int | bool | str | bytes | Crossref] = set()

        for type_arg in get_type_args(typehint):
            if isinstance(type_arg, int | bool | str | bytes | Crossref):
                values.add(type_arg)

            elif is_crossreffed(type_arg):
                values.add(type_arg._docnote_extract_metadata)

            # Note: this must be a enum object! (and a live one, not a
            # crossref -- hence needing to convert it)
            elif isinstance(type_arg, Enum):
                values.add(Crossref.from_object(type_arg, typevars=typevars))

            else:
                raise TypeError('Invalid type for literal arg!', typehint)

        return cls(frozenset(values))


type NormalizedType = (
    NormalizedUnionType
    | NormalizedEmptyGenericType
    | NormalizedConcreteType
    | NormalizedSpecialType
    | NormalizedLiteralType)


@dataclass(slots=True, frozen=True)
class LazyResolvingValue:
    """
    """
    _crossref: Crossref | None
    _value: Literal[Singleton.MISSING] | Any

    def __call__(self) -> Any:
        """Resolves the actual annotation. Note that the import hook
        must be uninstalled **before** calling this!
        """
        raise NotImplementedError

    @classmethod
    def from_annotated(
            cls,
            annotated: Crossreffed | Any
            ) -> LazyResolvingValue:
        """Converts a reftype-based ``Annotated[]`` member into a
        ``LazyResolvingValue`` instance. If the member was not
        a reftype, returns the value back.

        TODO: this should recurse into containers.
        """
        if is_crossreffed(annotated):
            return cls(
                _crossref=annotated._docnote_extract_metadata,
                _value=Singleton.MISSING)
        else:
            return cls(
                _crossref=None,
                _value=annotated)

    def __post_init__(self):
        if not ((self._crossref is None) ^ (self._value is Singleton.MISSING)):
            raise TypeError(
                'LazyResolvingValue can only have a crossref xor value!',
                self)
