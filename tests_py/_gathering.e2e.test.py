import pytest
from docnote import ReftypeMarker

from docnote_extract import Docnotes
from docnote_extract import SummaryMetadata
from docnote_extract import gather
from docnote_extract._module_tree import SummaryTreeNode
from docnote_extract.crossrefs import Crossref
from docnote_extract.crossrefs import GetattrTraversal
from docnote_extract.normalization import NormalizedConcreteType
from docnote_extract.normalization import NormalizedLiteralType
from docnote_extract.normalization import NormalizedUnionType
from docnote_extract.summaries import CallableSummary
from docnote_extract.summaries import ClassSummary
from docnote_extract.summaries import VariableSummary


@pytest.fixture(scope='module')
def testpkg_docs() -> Docnotes[SummaryMetadata]:
    """We want to do a bunch of spot checks against the testpkg, but
    we only need to gather it once. Hence, we have a module-scoped
    fixture that returns the gathered ``Docnotes``.
    """
    return gather(
        ['docnote_extract_testpkg'],
        special_reftype_markers={
            Crossref(
                module_name='docnote_extract_testutils.for_handrolled',
                toplevel_name='ThirdpartyMetaclass'):
            ReftypeMarker.METACLASS})


@pytest.fixture(scope='module')
def finnr_docs() -> Docnotes[SummaryMetadata]:
    """We want to do a bunch of spot checks against finnr, but
    we only need to gather it once. Hence, we have a module-scoped
    fixture that returns the gathered ``Docnotes``.
    """
    return gather(['finnr'])


class TestGatheringE2EFinnr:
    """Runs end-to-end tests based on the finnr package.
    """

    def test_expected_summaries(self, finnr_docs: Docnotes[SummaryMetadata]):
        """The gathered result must contain the expected number of
        summaries, and it must contain the summary tree root.
        """
        assert len(finnr_docs.summaries) == 1
        (pkg_name, tree_root), = finnr_docs.summaries.items()
        assert pkg_name == 'finnr'
        assert isinstance(tree_root, SummaryTreeNode)

    def test_spotcheck_money(self, finnr_docs: Docnotes[SummaryMetadata]):
        """A spot-check of the finnr money module must match the
        expected results.
        """
        (_, tree_root), = finnr_docs.summaries.items()
        money_mod_node = tree_root.find('finnr.money')
        money_mod_summary = money_mod_node.module_summary
        resulting_names = {
            child.name
            for child in money_mod_summary.members
            if child.metadata.included}
        assert resulting_names == {'amount_getter', 'Money'}

    def test_spotcheck_currency(self, finnr_docs: Docnotes[SummaryMetadata]):
        """A spot-check of the finnr currency module must match the
        expected results. This is particularly concerned with the
        typespec values.
        """
        (_, tree_root), = finnr_docs.summaries.items()
        currency_mod_node = tree_root.find('finnr.currency')
        currency_mod_summary = currency_mod_node.module_summary
        currency_summary = currency_mod_summary / GetattrTraversal('Currency')
        assert isinstance(currency_summary, ClassSummary)

        name_summary = currency_summary / GetattrTraversal('name')
        assert isinstance(name_summary, VariableSummary)

        assert name_summary.typespec is not None
        assert isinstance(
            name_summary.typespec.normtype,
            NormalizedUnionType)
        assert len(name_summary.typespec.normtype.normtypes) == 2

        normtype1, normtype2 = name_summary.typespec.normtype.normtypes

        if isinstance(normtype1, NormalizedConcreteType):
            concrete_union_member = normtype1
            literal_union_member = normtype2
        else:
            concrete_union_member = normtype2
            literal_union_member = normtype1

        # Note that this also catches the else statement in case the types
        # were completely off
        assert isinstance(concrete_union_member, NormalizedConcreteType)
        assert isinstance(literal_union_member, NormalizedLiteralType)

        assert concrete_union_member.primary.toplevel_name == 'str'
        assert not concrete_union_member.primary.traversals
        assert len(literal_union_member.values) == 1
        literal_value, = literal_union_member.values
        assert isinstance(literal_value, Crossref)
        assert literal_value.toplevel_name == 'Singleton'
        assert literal_value.traversals == (GetattrTraversal('UNKNOWN'),)

    def test_currencyset_call(self, finnr_docs: Docnotes[SummaryMetadata]):
        """The ``CurrencySet.__call__`` summary must have a signature
        and not be disowned.
        """
        (_, tree_root), = finnr_docs.summaries.items()
        currency_mod_node = tree_root.find('finnr.currency')
        currency_mod_summary = currency_mod_node.module_summary
        call_summary = (
            currency_mod_summary
            / GetattrTraversal('CurrencySet')
            / GetattrTraversal('__call__'))

        assert isinstance(call_summary, CallableSummary)
        assert len(call_summary.signatures) == 1
        assert not call_summary.metadata.disowned
        assert call_summary.metadata.to_document

        signature_summary, = call_summary.signatures
        assert not signature_summary.metadata.disowned
        assert signature_summary.metadata.to_document

    def test_spotcheck_iso(self, finnr_docs: Docnotes[SummaryMetadata]):
        """A spot-check of the finnr iso module must match the
        expected results. In particular, the mint must be correctly
        assigned to the module, and not disowned.
        """
        (_, tree_root), = finnr_docs.summaries.items()
        iso_mod_node = tree_root.find('finnr.iso')
        iso_mod_summary = iso_mod_node.module_summary
        resulting_names = {
            child.name
            for child in iso_mod_summary.members
            if child.metadata.included}
        assert resulting_names == {'mint'}

        mint_summary = iso_mod_summary / GetattrTraversal('mint')
        assert isinstance(mint_summary, VariableSummary)
        assert mint_summary.typespec is not None
        assert mint_summary.typespec.normtype == NormalizedConcreteType(
            primary=Crossref(
                module_name='finnr.currency', toplevel_name='CurrencySet'),
            params=())
