from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import ModuleType
from typing import Annotated
from typing import Any
from typing import ClassVar
from typing import Protocol
from typing import TypeAliasType
from typing import TypeGuard
from typing import TypeVar
from typing import overload

from docnote import Note


class SyntacticTraversalType(Enum):
    TYPEVAR = 'typevar'
    ANONYMOUS_OVERLOAD = 'overload'
    ANONYMOUS_IMPORT = '<unknown imported object>'


@dataclass(slots=True, frozen=True)
class SyntacticTraversal:
    """
    """
    type_: SyntacticTraversalType
    key: str


@dataclass(slots=True, frozen=True)
class GetattrTraversal:
    """
    """
    name: str


@dataclass(slots=True, frozen=True)
class CallTraversal:
    """
    """
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass(slots=True, frozen=True)
class GetitemTraversal:
    """
    """
    key: Any


@dataclass(slots=True, frozen=True)
class SignatureTraversal:
    """If you need to reference a particular signature (ie, the
    particular overload of a function), use this. Note that you must
    always include this when referencing parameters, even if the
    function has only the one implementation and no overloads. In that
    case, set the ``ordering_index`` to None.
    """
    ordering_index: int | None


@dataclass(slots=True, frozen=True)
class ParamTraversal:
    """If you need to reference a particular parameter within one of a
    callable's signatures, this is how you do it.
    """
    name: str


type CrossrefTraversal = (
    SyntacticTraversal
    | GetattrTraversal
    | CallTraversal
    | GetitemTraversal
    | SignatureTraversal
    | ParamTraversal)


@dataclass(slots=True, frozen=True, kw_only=True)
class Crossref:
    """A reference to something defined and/or documented elsewhere.
    """
    module_name: Annotated[
        str | None,
        Note('''The name of the module containing the reference. If the
            reference points to a module, then this is simply the name of
            the module. ``builtins`` indicates a built-in type.''')]
    toplevel_name: Annotated[
        str | None,
        Note('''The name of the toplevel parent object within the parent
            module. For modules, this is None.

            Note that this is not the same as the cross-referenced object's
            name within its immediate parent scope; that is included within
            traversals.''')]
    traversals: Annotated[
            tuple[CrossrefTraversal, ...],
            Note('''The traversal stack describes which extra steps were taken
                (attribute or getitem references, function calls, parameters,
                etc) to arrive at a final crossref. Empty tuples are used for
                modules and their toplevel objects.''')
        ] = ()

    def __truediv__(self, traversal: CrossrefTraversal) -> Crossref:
        # Getattr traversals on a MODULE must result in setting the toplevel
        # name instead of appending a traversal.
        if (
            self.toplevel_name is None
            and isinstance(traversal, GetattrTraversal)
        ):
            return Crossref(
                module_name=self.module_name,
                toplevel_name=traversal.name,
                # Tuples are immutable so we don't need to bother copying it
                traversals=self.traversals)

        else:
            return Crossref(
                module_name=self.module_name,
                toplevel_name=self.toplevel_name,
                traversals=(*self.traversals, traversal))

    @classmethod
    def from_object(
            cls,
            obj: Any,
            *,
            typevars: Mapping[TypeVar, Crossref],
            allow_fallback: bool = False
            ) -> Crossref:
        """Attempts to create a crossref from the passed object. This
        can only work conditionally; generally only types, functions,
        and enums will succeed.

        Note that this is safe to call on things that are already
        crossrefs.
        """
        if isinstance(obj, Crossref):
            return obj

        # Note: NOT true for the enum class itself! Just for enum members.
        if isinstance(obj, Enum):
            name = obj.name
            enum_cls = type(obj)
            return cls(
                module_name=enum_cls.__module__,
                toplevel_name=enum_cls.__name__,
                traversals=(GetattrTraversal(name),))

        if isinstance(obj, ModuleType):
            return cls(
                module_name=obj.__name__,
                toplevel_name=None,
                traversals=())

        if isinstance(obj, TypeVar):
            if obj in typevars:
                return typevars[obj]
            else:
                raise ValueError(
                    'Cannot create a typespec for an unknown type variable!',
                    obj)

        if (
            hasattr(obj, '__module__')
            and hasattr(obj, '__name__')
            and (
                # Type aliases don't have qualnames, but also aren't valid in
                # closures
                isinstance(obj, TypeAliasType)
                or (
                    hasattr(obj, '__qualname__')
                    # This is a quick and dirty way to detect the existence of
                    # a closure
                    and '<locals>' not in obj.__qualname__
        ))):
            return cls(
                module_name=obj.__module__,
                toplevel_name=obj.__name__,
                traversals=())

        else:
            if allow_fallback:
                return cls.make_fallback(obj)
            else:
                raise TypeError(
                    'Cannot create a crossref from that object without '
                    + 'further information!', obj)

    @classmethod
    def make_fallback(
            cls,
            obj: Any
            ) -> Crossref:
        """There are some situations where we'd rather give a fallback,
        less-useful crossref instead of breaking the entire extraction.
        In these cases, we use this to do our best at constructing
        something useful.
        """
        return cls(
            module_name=None,
            toplevel_name=None,
            traversals=(SyntacticTraversal(
                SyntacticTraversalType.ANONYMOUS_IMPORT,
                repr(obj)),))


class Crossreffed(Protocol):
    _docnote_extract_metadata: Crossref


class _ClassWithCrossreffedBaseProtocol(Protocol):
    _docnote_extract_base_classes: tuple[type | Crossreffed, ...]


class ClassWithCrossreffedBase(type, _ClassWithCrossreffedBaseProtocol):
    """This pseudo-intersection type is a type subclass that includes
    the _docnote_extract_base_classes attribute.
    """


class _ClassWithCrossreffedMetaclassProtocol(Protocol):
    _docnote_extract_metaclass: Crossreffed


class ClassWithCrossreffedMetaclass(
        type, _ClassWithCrossreffedMetaclassProtocol):
    """This pseudo-intersection type is a type subclass that includes
    the _docnote_extract_metaclass attribute.
    """


def is_crossreffed(obj: Any) -> TypeGuard[Crossreffed]:
    return hasattr(obj, '_docnote_extract_metadata')


def has_crossreffed_base(obj: type) -> TypeGuard[ClassWithCrossreffedBase]:
    return hasattr(obj, '_docnote_extract_base_classes')


def has_crossreffed_metaclass(
        obj: type
        ) -> TypeGuard[ClassWithCrossreffedMetaclass]:
    return hasattr(obj, '_docnote_extract_metaclass')


class CrossrefMetaclass(type):
    """By necessity, the reftype objects need to be actual types, and
    not instances -- otherwise, you can't subclass them. Therefore, we
    need to support __getattr__, __getitem__, etc on the class itself;
    this is responsible for that.
    """
    _docnote_extract_metadata: Crossref

    def __new__(
            metacls,
            name: str,
            bases: tuple[type],
            namespace: dict[str, Any],
            *,
            __docnote_extract_traversal__: bool = False,
            # Kwargs here are needed in case something is subclassing from a
            # class with a defined metaclass, which itself accepts keywords
            **kwargs):
        """The goal here is to minimize the spread of the reftype
        metaclass, limiting it strictly to stubbed imports, and NOT
        objects actually defined in a module being inspected. We control
        this via the __docnote_extract_traversal__ magic keyword.
        """
        # The point here is that we can discard the metaclass for anything that
        # inherits from a CrossrefMixin. By requiring the unique keyword, which
        # only we supply (inside make_crossreffed), any other class
        # instantiations get a normal object.
        if __docnote_extract_traversal__:
            return super().__new__(metacls, name, bases, namespace)
        else:
            # We need to strip out our custom base class to avoid it injecting
            # the metaclass indirectly, causing infinite recursion.
            # The easiest way to do this is just drop it entirely, and not try
            # to replace it with something.
            stripped_bases = tuple(
                base for base in bases if not issubclass(base, CrossrefMixin))
            cls = super().__new__(type, name, stripped_bases, namespace)
            cls._docnote_extract_base_classes = bases
            return cls

    def __delattr__(cls, name: str) -> None: ...
    def __setattr__(cls, name: str, value: Any) -> None: ...
    def __getattr__(cls, name: str) -> type[CrossrefMixin]:
        return make_crossreffed(
            metadata=cls._docnote_extract_metadata,
            traversal=GetattrTraversal(name))

    def __delitem__(cls, key: Any) -> None: ...
    def __setitem__(cls, key: Any, value: Any) -> None: ...
    def __getitem__(cls, key: Any) -> type[CrossrefMixin]:
        return make_crossreffed(
            metadata=cls._docnote_extract_metadata,
            traversal=GetitemTraversal(key))


class CrossrefMixin(
        metaclass=CrossrefMetaclass,
        # Note: this is necessary for the metaclass to actually be applied,
        # otherwise it'll be stripped from the bases tuple and be replaced
        # with a normal type instance
        __docnote_extract_traversal__=True):
    """This is used as a mixin class when constructing Crossrefs. It
    contains the actual implementation for the magic methods that return
    more reftypes.
    """
    _docnote_extract_metadata: ClassVar[Crossref]

    def __init_subclass__(cls, **kwargs):
        """We use this to suppress issues with our magic
        __docnote_extract_traversal__ parameter.
        """
        pass

    def __new__(cls, *args, **kwargs) -> type[CrossrefMixin]:
        """We use __new__ as a stand-in for a function call. Therefore,
        it creates a new concrete Crossref class, and returns it.
        """
        return make_crossreffed(
            metadata=cls._docnote_extract_metadata,
            traversal=CallTraversal(args, kwargs))


class CrossrefMetaclassMetaclass(type):
    """This "I'm-seeing-double"-ly-named class gets used as the base
    type for ``make_metaclass_crossreffed``. It does some magic to handle
    metaclass kwargs and strip out all traces of itself, while adding
    in the bookkeeping attribute to the final class.
    """

    def __new__(
            metacls,
            name: str,
            bases: tuple[type],
            namespace: dict[str, Any],
            # This is a stand-in for any kwargs from the stubbed-out metaclass.
            **kwargs):
        """In addition to handling any kwargs from the stubbed-out
        metaclass, this is responsible for reworking the bookkeeping a
        bit, replacing some weird metaclass shenanigans with an
        attribute on the final resulting class. Additionally, we remove
        ourselves from the metaclass hierarchy entirely -- so nach dem
        Motto "be kind, rewind".
        """
        injected_bases = (_SwallowsInitSubclassKwargs, *bases)

        if not is_crossreffed(metacls):
            raise TypeError(
                'docnote_extract internal error: concrete MetaclassMetaclass '
                + 'is not crossreffed!', metacls)

        cls = super().__new__(type, name, injected_bases, namespace)
        cls._docnote_extract_metaclass = metacls
        return cls


class _SwallowsInitSubclassKwargs:
    """We inject this as a base class for anything using the
    CrossrefMetaclassMetaclass so that subclass kwargs can be handled
    without error.
    """

    def __init_subclass__(cls, **kwargs): ...


def make_metaclass_crossreffed(
        *,
        module: str,
        name: str,
        ) -> type:
    """Metaclass reftypes don't implement any special logic beyond
    normal types. Therefore, they don't support mock-like behavior, nor
    traversals. However, unlike normal ``Crossref``s, they can -- as the
    name suggests -- be used as a metaclass.

    They also, of course, include the ``_docnote_extract_metadata``
    attribute on the created metaclass.
    """
    metadata = Crossref(module_name=module, toplevel_name=name)
    return type(
        'CrossrefMetaclassMetaclass',
        (CrossrefMetaclassMetaclass,),
        # We'll strip this out in just a second, but we need it to assign the
        # metadata for the _docnote_extract_metaclass attribute on the final
        # class object
        {'_docnote_extract_metadata': metadata})


@overload
def make_crossreffed(*, module: str, name: str) -> type[CrossrefMixin]: ...
@overload
def make_crossreffed(
        *,
        metadata: Crossref,
        traversal: GetattrTraversal | CallTraversal | GetitemTraversal
        ) -> type[CrossrefMixin]: ...
def make_crossreffed(
        *,
        module: str | None = None,
        name: str | None = None,
        metadata: Crossref | None = None,
        traversal:
            GetattrTraversal | CallTraversal | GetitemTraversal | None = None,
        ) -> type[CrossrefMixin]:
    """This makes an actual Crossref class.
    """
    if module is not None and name is not None:
        new_metadata = Crossref(module_name=module, toplevel_name=name)

    elif metadata is not None and traversal is not None:
        new_metadata = Crossref(
            module_name=metadata.module_name,
            toplevel_name=metadata.toplevel_name,
            traversals=(*metadata.traversals, traversal))

    else:
        raise TypeError(
            'Invalid make_crossreffed call signature! (type checker failure?)')

    # This is separate purely so we can isolate the type: ignore
    retval = CrossrefMetaclass(
        'Crossreffed',
        (CrossrefMixin,),
        {'_docnote_extract_metadata': new_metadata},
        __docnote_extract_traversal__=True)
    return retval  # type: ignore
