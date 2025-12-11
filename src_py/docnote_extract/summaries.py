from __future__ import annotations

import inspect
import itertools
import typing
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Annotated
from typing import Any
from typing import Protocol
from typing import TypeVar
from uuid import UUID

from docnote import DocnoteGroup
from docnote import MarkupLang
from docnote import Note

from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import CrossrefTraversal
from docnote_extract.crossrefs import GetattrTraversal
from docnote_extract.crossrefs import ParamTraversal
from docnote_extract.crossrefs import SignatureTraversal
from docnote_extract.crossrefs import SyntacticTraversal
from docnote_extract.crossrefs import SyntacticTraversalType
from docnote_extract.crossrefs import is_crossreffed

if typing.TYPE_CHECKING:
    from docnote_extract.normalization import LazyResolvingValue
    from docnote_extract.normalization import TypeSpec


class Singleton(Enum):
    MISSING = 'missing'
    UNKNOWN = 'unknown'


@dataclass(slots=True)
class ObjClassification:
    is_reftype: bool
    has_traversals: bool | None
    is_module: bool
    is_class: bool
    is_method: bool
    is_function: bool
    is_generator_function: bool
    is_generator: bool
    is_coroutine_function: bool
    is_coroutine: bool
    is_awaitable: bool
    is_async_generator_function: bool
    is_async_generator: bool
    is_method_wrapper: bool
    # Note: the primary place you're likely to encounter these in third-party
    # code is as the type of a slot. So for example, any dataclass with
    # slots=True will have this type on its attributes. As per stdlib docs,
    # these are **never** a function, class, method, or builtin.
    # ... but it's still True for int.__add__. Errrm??? Confusing AF.
    is_method_descriptor: bool
    is_data_descriptor: bool
    is_getset_descriptor: bool
    is_member_descriptor: bool
    is_callable: bool
    is_typevar: bool

    @property
    def is_any_generator(self) -> bool:
        return (
            self.is_generator_function
            or self.is_generator
            or self.is_async_generator_function
            or self.is_async_generator)

    @property
    def is_async(self) -> bool:
        return (
            self.is_coroutine_function
            or self.is_coroutine
            or self.is_awaitable
            or self.is_async_generator_function
            or self.is_async_generator)

    @classmethod
    def from_obj(cls, obj: Any) -> ObjClassification:
        if (crossreffed := is_crossreffed(obj)):
            has_traversals = bool(obj._docnote_extract_metadata.traversals)
        else:
            has_traversals = None

        return cls(
            is_reftype=crossreffed,
            has_traversals=has_traversals,
            is_module=inspect.ismodule(obj),
            is_class=inspect.isclass(obj),
            is_method=inspect.ismethod(obj),
            is_function=inspect.isfunction(obj),
            is_generator_function=inspect.isgeneratorfunction(obj),
            is_generator=inspect.isgenerator(obj),
            is_coroutine_function=inspect.iscoroutinefunction(obj),
            is_coroutine=inspect.iscoroutine(obj),
            is_awaitable=inspect.isawaitable(obj),
            is_async_generator_function=inspect.isasyncgenfunction(obj),
            is_async_generator=inspect.isasyncgen(obj),
            is_method_wrapper=inspect.ismethodwrapper(obj),
            is_method_descriptor=inspect.ismethoddescriptor(obj),
            is_data_descriptor=inspect.isdatadescriptor(obj),
            is_getset_descriptor=inspect.isgetsetdescriptor(obj),
            is_member_descriptor=inspect.ismemberdescriptor(obj),
            is_callable=callable(obj),
            is_typevar=isinstance(obj, TypeVar))

    def get_summary_class(self) -> type[SummaryBase]:  # noqa: PLR0911
        """Given the current classification, returns which summary
        type should be applied to the object, so that the caller can
        then create a summary instance for it.
        """
        if self.is_reftype:
            if self.has_traversals:
                return VariableSummary
            else:
                return CrossrefSummary
        if self.is_class:
            return ClassSummary
        if self.is_module:
            return ModuleSummary
        if (
            self.is_method
            or self.is_function
            or self.is_generator_function
            or self.is_coroutine_function
            or self.is_async_generator_function
            or self.is_method_wrapper
            or (self.is_member_descriptor and self.is_callable)
            or (self.is_method_descriptor and self.is_callable)
        ):
            return CallableSummary
        if self.is_typevar:
            return TypeVarSummary

        return VariableSummary


class CallableColor(Enum):
    ASYNC = 'async'
    SYNC = 'sync'


class MethodType(Enum):
    INSTANCE = 'instance'
    CLASS = 'class'
    STATIC = 'static'

    @staticmethod
    def classify(src_obj: Any, in_class: bool) -> MethodType | None:
        """Classifies a (hopefully callable) into a method type, or
        None if no method was applicable.

        Note that if you're in a class, you must BOTH set in_class
        to True, **and also get the ``src_obj`` from the class
        ``__dict__``, and ^^not by direct getattr reference on the
        class!^^**. Eg ``cls.__dict__['foo']``, **not** ``cls.foo``.
        The latter won't work!
        """
        if isinstance(src_obj, classmethod):
            return MethodType.CLASS
        elif isinstance(src_obj, staticmethod):
            return MethodType.STATIC
        elif in_class:
            return MethodType.INSTANCE

        return None


class ParamStyle(Enum):
    KW_ONLY = 'kw_only'
    KW_STARRED = 'kw_starred'
    POS_ONLY = 'pos_only'
    POS_STARRED = 'pos_starred'
    POS_OR_KW = 'pos_or_kw'

    @classmethod
    def from_inspect_param_kind(cls, kind) -> ParamStyle:
        if kind is inspect.Parameter.POSITIONAL_ONLY:
            return ParamStyle.POS_ONLY
        if kind is inspect.Parameter.POSITIONAL_OR_KEYWORD:
            return ParamStyle.POS_OR_KW
        if kind is inspect.Parameter.VAR_POSITIONAL:
            return ParamStyle.POS_STARRED
        if kind is inspect.Parameter.KEYWORD_ONLY:
            return ParamStyle.KW_ONLY
        if kind is inspect.Parameter.VAR_KEYWORD:
            return ParamStyle.KW_STARRED

        raise TypeError('Not a member of ``inspect.Parameter.kind``!', kind)


@dataclass(slots=True, frozen=True, kw_only=True)
class DocText:
    value: str
    markup_lang: str | MarkupLang | None


class SummaryMetadataProtocol(Protocol):
    id_: Annotated[
        str | int | UUID | None,
        Note('''This directly copies the underlying object's
            ``DocnoteConfig.id_`` value. As noted in the ``docnote`` docs,
            this allows library developers to have a persistent identifier
            that outlives renaming.''')]

    extracted_inclusion: Annotated[
        bool | None,
        Note('''This directly copies the underlying object's
            ``DocnoteConfig.included_in_docs`` value, and represents an
            explicit override by the code author.

            It is set after the metadata instance is created via the
            factory method passed to ``summarize_module``, and then used
            to help determine the final value of ``to_document``
            during final filtering.''')]

    canonical_module: Annotated[
        str | None,
        Note('''For any objects that can be attributed a canonical
            module (typically via the object's ``__module__`` attribute),
            this will be set to the fullname (ex ``foo.bar``) of that
            module. If no canonical module can be determined, it will be
            set to None.

            This is set after the metadata instance is created via the
            metadata factory method, and then used during filtering (to
            determine canonical ownership and remove extraneous stdlib
            dunder methods).''')]

    to_document: Annotated[
        bool,
        Note('''This value is initially set during filtering, and reflects
            whether or not the value is ^^directly^ included in the
            final docs (note that it might still be ^^indirectly^^
            included -- for example as an aside -- but that this is
            dependent upon the documentation generator).

            Note that, in the case of module summaries, this will be
            initially redundant with the read-only ``to_document`` value
            on the ``SummaryTreeNode`` for the module. However, unlike the
            summary tree node, this value can be freely modified by docs
            generation library, allowing it to be explicitly overridden
            (whether explicitly by the user or implicitly by the docs
            generation library) as part of the docs generation process.

            Beyond modules, documentation generators can use this to, for
            example, implicitly include any mixin methods of a private base
            class in the documentation of its public descendants.''')]

    disowned: Annotated[
        bool,
        Note('''This value is initially set during filtering to describe
            whether or not the description should be considered a member of
            its containing module. If ``False``, it must be excluded from
            documentation for that particular module (though it may very
            well be included elsewhere, as will be the case for
            imported firstparty names within a module's namespace).''')]

    crossref_namespace: Annotated[
        dict[str, Crossref],
        Note('''This contains a snapshot of any objects contained within
            the locals and globals for the member that can be expressed as
            ``Crossref`` instances. Objects within ``locals`` and ``globals``
            that cannot be expressed as a ``Crossref`` will be omitted.

            The primary intended use of this is for automatic linking of
            code-fenced blocks -- for example, if you reference ``Foo`` in
            the docstring of ``Bar``, this could be used to automatically
            link back to ``Foo`` in post-processing.

            This can also be used when processing python code embedded within
            docstrings themselves, if -- for example -- you wanted to run
            doctests against the code block while automatically applying the
            namespace of the surrounding module.

            This value is set after the metadata instance is created via the
            factory method passed to ``summarize_module``. It is not used by
            ``docref_extract``; it purely exists for documentation generators.
            ''')]


class SummaryMetadataFactoryProtocol[T: SummaryMetadataProtocol](Protocol):

    def __call__(
            self,
            *,
            classification: ObjClassification | None,
            summary_class: type[SummaryBase],
            crossref: Crossref | None,
            annotateds: Annotated[
                tuple[LazyResolvingValue, ...],
                Note('''``Annotated`` instances (other than docnote ones)
                    declared on the object will be included here.

                    Note that any imported annotation will take the form of a
                    ``LazyResolvingValue``. These must be called to resolve
                    the actuall annotation.

                    This part of the API should be considered experimental and
                    subject to change.''')],
            metadata: Annotated[
                dict[str, Any],
                Note(
                    'Any metadata defined via ``DocnoteConfig`` attachments.'
                )],
            ) -> T:
        """A summary metadata factory function must be passed to
        ``summarize_module`` to create the individual metadata instances
        to include in the summary objects.
        """
        ...


class _SummaryBaseProtocol[T: SummaryMetadataProtocol](Protocol):

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        """If the object has a traversal with the passed name, return
        it. Otherwise, raise ``LookupError``.
        """
        ...

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        """Yield all of the nodes at the summary, recursively,
        in a depth-first fashion. Primarily intended for updating
        metadata values based on filters.

        Order of the branches is arbitrary.

        By default, order from outermost node to innermost (parents
        first, then children). If ``reverse`` is true, order from
        innermost node to outermost (children first, then parents).
        """
        ...


type NamespaceMemberSummary[T: SummaryMetadataProtocol] = (
    ClassSummary[T]
    | VariableSummary[T]
    | CallableSummary[T]
    | CrossrefSummary[T])


@dataclass(slots=True, frozen=True, kw_only=True)
class SummaryBase[T: SummaryMetadataProtocol](_SummaryBaseProtocol[T]):
    crossref: Crossref | None
    ordering_index: int | None
    child_groups: Annotated[
            Sequence[DocnoteGroup],
            Note('Any child groups defined via ``DocnoteConfig`` attachments.')
        ]
    parent_group_name: Annotated[
            str | None,
            Note(''''Any parent group assignment defined via ``DocnoteConfig``
                attachments.''')]
    metadata: T = field(compare=False, repr=False)

    def __truediv__(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        return self.traverse(traversal)


@dataclass(slots=True, frozen=True, kw_only=True)
class ModuleSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    name: Annotated[str, Note('The module fullname, ex ``foo.bar.baz``.')]
    dunder_all: frozenset[str] | None
    docstring: DocText | None
    members: frozenset[NamespaceMemberSummary[T]]
    typevars: frozenset[TypeVarSummary[T]]

    _member_lookup: \
        dict[
                CrossrefTraversal,
                NamespaceMemberSummary[T] | TypeVarSummary[T]] = field(
            default_factory=dict, repr=False, init=False, compare=False)

    def __post_init__(self):
        # Note that module-level typevars don't use the syntactic traversal,
        # because they can't be defined as sugared typevars
        for member in itertools.chain(self.members, self.typevars):
            self._member_lookup[GetattrTraversal(member.name)] = member

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        # KeyError is a LookupError subclass, so this is fine.
        return self._member_lookup[traversal]

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        if reverse:
            for child in self.members:
                yield from child.flatten(reverse=reverse)
            yield self

        else:
            yield self
            for child in self.members:
                yield from child.flatten(reverse=reverse)

    def in_dunder_all(self, name: str) -> bool:
        """Returns True if the module has a dunder all declared **and**
        the name was found within it.
        """
        if self.dunder_all is None:
            return False
        else:
            return name in self.dunder_all


@dataclass(slots=True, frozen=True, kw_only=True)
class CrossrefSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    """Used when something is being re-exported (at the module level) or
    is otherwise a direct reference to something else (for example, a
    classvar referencing an imported enum value).
    """
    name: str
    typespec: Annotated[
        TypeSpec | None,
        Note('''Typically None. An explicit type annotation on the re-export,
            in addition to any annotation from its definition site.''')]
    notes: Annotated[
        tuple[DocText, ...],
        Note('''The contents of any ``Note``s directly attached to the
            re-export (in addition to any notes from its definition site).''')]
    src_crossref: Crossref

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        raise LookupError(
            'Crossref summaries have no traversals', self, traversal)

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        yield self


@dataclass(slots=True, frozen=True, kw_only=True)
class TypeVarSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    """Type var summaries attach to things that can declare type
    variables -- signatures, classes, and modules -- and contain all
    applicable information for the underlying type var. They can then
    be referenced via crossref from the sites that use them.
    """
    name: str
    bound: TypeSpec | None
    constraints: tuple[TypeSpec, ...]
    # Note: because this is wrapped in a typespec, we don't need to worry
    # about a default of none. Explicit defaults of None will still be wrapped.
    default: TypeSpec | None

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        raise LookupError(
            'TypeVar summaries have no traversals', self, traversal)

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        yield self


@dataclass(slots=True, frozen=True, kw_only=True)
class VariableSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    """VariableSummary instances are used for module variables as well as
    class members. Note that within a class, variables annotated as
    ``ClassVar``s will have the literal ``ClassVar`` added to their
    ``annotations`` tuple.
    """
    name: str
    typespec: Annotated[
        TypeSpec | None,
        Note('''Note that a value of ``None`` indicates that no type hint was
            defined, not that the hint itself was an explicit ``None``. The
            latter case will be a ``Crossref`` with object source, ``None``
            as the module name, ane ``None`` as the name.''')]
    notes: Annotated[
        tuple[DocText, ...],
        Note('The contents of any ``Note``s attached to the variable.')]

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        raise LookupError(
            'Variable summaries have no traversals', self, traversal)

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        yield self


@dataclass(slots=True, frozen=True, kw_only=True)
class ClassSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    name: str
    docstring: DocText | None
    metaclass: Annotated[
        TypeSpec | None,
        Note('''Note that this only includes an explicit metaclass, as defined
            on the class itself. Implicit metaclasses inherited from base
            classes will not be detected.''')]
    bases: tuple[TypeSpec, ...]
    members: frozenset[NamespaceMemberSummary[T]]
    typevars: frozenset[TypeVarSummary[T]]

    _member_lookup: \
        dict[CrossrefTraversal, NamespaceMemberSummary[T]] = field(
            default_factory=dict, repr=False, init=False, compare=False)
    _syntactic_lookup: dict[SyntacticTraversal, TypeVarSummary[T]] = field(
        default_factory=dict, repr=False, init=False, compare=False)

    def __post_init__(self):
        for member in self.members:
            self._member_lookup[GetattrTraversal(member.name)] = member

        for typevar in self.typevars:
            self._syntactic_lookup[
                SyntacticTraversal(
                    type_=SyntacticTraversalType.TYPEVAR,
                    key=typevar.name)
            ] = typevar

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        # KeyError is a LookupError subclass, so this is fine.
        if isinstance(traversal, SyntacticTraversal):
            return self._syntactic_lookup[traversal]

        # KeyError is a LookupError subclass, so this is fine.
        return self._member_lookup[traversal]

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        if reverse:
            for child in self.members:
                yield from child.flatten(reverse=reverse)
            yield self
        else:
            yield self
            for child in self.members:
                yield from child.flatten(reverse=reverse)


@dataclass(slots=True, frozen=True, kw_only=True)
class CallableSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    name: str
    docstring: Annotated[
            DocText | None,
            Note('''For non-overloaded callables, this is simply the value
                of the callable's docstring.

                For overloaded callables, this is specifically the docstring
                associated with the callable **implementation**, and not its
                overloads.''')]
    color: CallableColor
    method_type: MethodType | None
    is_generator: bool
    signatures: frozenset[SignatureSummary[T]]

    _member_lookup: dict[SignatureTraversal, SignatureSummary[T]] = field(
        default_factory=dict, repr=False, init=False, compare=False)

    def __post_init__(self):
        # We can just skip the single-signature version entirely; we don't
        # need a lookup for it (see ``traverse``)
        if len(self.signatures) > 1:
            for member in self.signatures:
                if member.ordering_index is not None:
                    # Note: these aren't necessarily sequential, nor are they
                    # necessarily in order!
                    self._member_lookup[
                        SignatureTraversal(member.ordering_index)] = member

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        """Traversals into callables work like this:
        ++  A callable with a single signature (ie, with no overloads)
            is always referenced by ``ordering_index=None``
        ++  A callable with multiple signatures (ie, with overloads,
            or with unions where each union member has separate
            ``Note``s attached) can only be referenced by the explicit
            ``ordering_index`` attached to it by a ``DocnoteConfig``.
            If none is defined (ie, if default ordering is used), it
            cannot be referenced by traversal.
        """
        if not isinstance(traversal, SignatureTraversal):
            raise LookupError('Invalid traversal type!', self, traversal)

        # There's no reason for a lookup here, we can just validate the
        # traversal and return the only possible result
        if len(self.signatures) == 1:
            if traversal.ordering_index is not None:
                raise LookupError(
                    '``ordering_index`` for non-overloaded callables must '
                    + ' always be None', self, traversal)
            return next(iter(self.signatures))

        if traversal not in self._member_lookup:
            raise LookupError(
                'Traversals for overloaded callables must match the explicit '
                + "``ordering_index`` defined on the signature's attached "
                + '``DocnoteConfig', self, traversal)

        return self._member_lookup[traversal]

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        if reverse:
            for child in self.signatures:
                yield from child.flatten(reverse=reverse)
            yield self
        else:
            yield self
            for child in self.signatures:
                yield from child.flatten(reverse=reverse)


@dataclass(slots=True, frozen=True, kw_only=True)
class SignatureSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    """These are used to express a particular combination of parameters
    and return values. Callables with a single signature will typically
    have only one of these (with the exception of union types that have
    separate ``Note``s attached to individual members of the union).
    Overloaded callables will have one ``SignatureSpec`` per overload.
    """
    params: frozenset[ParamSummary[T]]
    retval: RetvalSummary[T]
    docstring: Annotated[
            DocText | None,
            Note('''In practice, this is typically None. However, it will be
                non-None if:
                ++  The parent callable defines overloads
                ++  The overloads themselves have docstrings
                Note that in this case, the docstring for the implementation
                will be included in the parent callable.''')]
    typevars: frozenset[TypeVarSummary[T]]

    _member_lookup: dict[ParamTraversal, ParamSummary[T]] = field(
        default_factory=dict, repr=False, init=False, compare=False)
    _syntactic_lookup: dict[SyntacticTraversal, TypeVarSummary[T]] = field(
        default_factory=dict, repr=False, init=False, compare=False)

    def __post_init__(self):
        for member in self.params:
            self._member_lookup[ParamTraversal(member.name)] = member

        for typevar in self.typevars:
            self._syntactic_lookup[
                SyntacticTraversal(
                    type_=SyntacticTraversalType.TYPEVAR,
                    key=typevar.name)
            ] = typevar

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        # KeyError is a LookupError subclass, so this is fine.
        if isinstance(traversal, SyntacticTraversal):
            return self._syntactic_lookup[traversal]

        if not isinstance(traversal, ParamTraversal):
            raise LookupError(
                'Traversals for signatures must be ``ParamTraversal`` '
                + 'instances!', self, traversal)

        if traversal.name == 'return':
            return self.retval

        # KeyError is a LookupError subclass, so this is fine.
        return self._member_lookup[traversal]

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        if reverse:
            for child in self.params:
                yield from child.flatten(reverse=reverse)
            yield self.retval
            yield self
        else:
            yield self
            yield self.retval
            for child in self.params:
                yield from child.flatten(reverse=reverse)


@dataclass(slots=True, frozen=True, kw_only=True)
class ParamSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    name: str
    index: int
    style: ParamStyle
    default: LazyResolvingValue | None
    typespec: Annotated[
        TypeSpec | None,
        Note('''Note that a value of ``None`` indicates that no type hint was
            defined, not that the hint itself was an explicit ``None``. The
            latter case will be a ``Crossref`` with object source, ``None``
            as the module name, ane ``None`` as the name.''')]
    notes: Annotated[
        tuple[DocText, ...],
        Note('The contents of any ``Note``s attached to the param.')]

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        raise LookupError(
            'Param summaries have no traversals', self, traversal)

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        yield self


@dataclass(slots=True, frozen=True, kw_only=True)
class RetvalSummary[T: SummaryMetadataProtocol](SummaryBase[T]):
    typespec: Annotated[
        TypeSpec | None,
        Note('''Note that a value of ``None`` indicates that no type hint was
            defined, not that the hint itself was an explicit ``None``. The
            latter case will be a ``Crossref`` with object source, ``None``
            as the module name, ane ``None`` as the name.''')]
    notes: Annotated[
        tuple[DocText, ...],
        Note('The contents of any ``Note``s attached to the return value.')]

    def traverse(self, traversal: CrossrefTraversal) -> SummaryBase[T]:
        raise LookupError(
            'Retval summaries have no traversals', self, traversal)

    def flatten(self, *, reverse: bool = False) -> Iterator[SummaryBase[T]]:
        yield self
