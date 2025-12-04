"""This contains notes and configs for testing normalization.
"""
from collections.abc import Callable
from functools import partial
from typing import Annotated

from docnote import DocnoteConfig
from docnote import MarkupLang
from docnote import Note
from docnote import docnote


ClcNote: Annotated[
        Callable[[str], Note],
        DocnoteConfig(include_in_docs=False)
    ] = partial(Note, config=DocnoteConfig(markup_lang=MarkupLang.CLEANCOPY))
DOCNOTE_CONFIG_ATTR: Annotated[
        str,
        Note('''Docs generation libraries should use this value to
            get access to any configs attached to objects via the
            ``docnote`` decorator.
            ''')
    ] = '_docnote_config'


class HasVarsWithAnnotatedUnion:
    foo: Annotated[
        float | int,
        Note('Just here to validate union collapsing')]
    bar: float | int


@docnote(DocnoteConfig(include_in_docs=False))
def func_with_config():
    """This is here just to make sure that normalization works when a
    config is attached via the ``@docnote`` decorator instead of an
    annotation.
    """


@docnote(DocnoteConfig(canonical_module='foo.bar', canonical_name='baz'))
def func_with_canonical_overrides():
    """This is here just to make sure that normalization works when a
    config is attached via the ``@docnote`` decorator instead of an
    annotation.
    """


@docnote(DocnoteConfig(include_in_docs=True))
class ClassWithDecoratedConfigMethod:

    @docnote(DocnoteConfig(include_in_docs=False))
    def func_with_config(self):
        """This is here just to make sure that normalization works when
        a config is attached via the ``@docnote`` decorator instead of
        an annotation when normalizing inside of a class.
        """

    @docnote(DocnoteConfig(canonical_module='foo.bar', canonical_name='baz'))
    def func_with_canonical_overrides(self):
        """This is here just to make sure that normalization works when a
        config is attached via the ``@docnote`` decorator instead of an
        annotation.
        """


class ClassWithProperty:

    @property
    def custom_property(self) -> bool:
        """This should be summarized as a variable, with the return type
        assigned as the typespec, and this docstring as the note.
        """
        ...


@docnote(DocnoteConfig(id_='my_explicit_id'))
class ClassWithStableId:
    """This class has an explicit docnote ID attached to it.
    """
