"""Tests for G11 — Evidence Requirement."""

from corpdisc.contracts.common import VerifiedEvidence
from corpdisc.gates import check_g11_evidence_requirement
from corpdisc.models.domain import SourceClass, Supports


class TestG11EvidenceRequirement:
    """Tests for G11 — Evidence Requirement."""

    def test_g11_verified_ownership_evidence(self) -> None:
        """Verified ownership evidence should be allowed."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="ACME Corp owns subsidiary",
                quote_verified=True,
                char_start=0,
                char_end=30,
                source_class=SourceClass.FIRST_PARTY_PARENT,
                supports=Supports.OWNERSHIP,
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is True

    def test_g11_unverified_quote_rejected(self) -> None:
        """Unverified quote should not satisfy requirement."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="ACME Corp owns subsidiary",
                quote_verified=False,  # Not verified
                char_start=None,
                char_end=None,
                source_class=SourceClass.FIRST_PARTY_PARENT,
                supports=Supports.OWNERSHIP,
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is False
        assert result.reason == "INSUFFICIENT_EVIDENCE"

    def test_g11_wrong_supports_rejected(self) -> None:
        """Non-OWNERSHIP support should not satisfy requirement."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="ACME Corp acquired company",
                quote_verified=True,
                char_start=0,
                char_end=30,
                source_class=SourceClass.FIRST_PARTY_PARENT,
                supports=Supports.EXISTENCE,  # Not OWNERSHIP
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is False

    def test_g11_wrong_source_class_rejected(self) -> None:
        """Non-confirming source class should not satisfy requirement."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="ACME Corp owns subsidiary",
                quote_verified=True,
                char_start=0,
                char_end=30,
                source_class=SourceClass.THIRD_PARTY,  # Not confirming
                supports=Supports.OWNERSHIP,
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is False

    def test_g11_third_party_confirms_rejected(self) -> None:
        """THIRD_PARTY should not satisfy requirement (only confirming classes work)."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="ACME Corp owns subsidiary",
                quote_verified=True,
                char_start=0,
                char_end=30,
                source_class=SourceClass.THIRD_PARTY,
                supports=Supports.OWNERSHIP,
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is False

    def test_g11_first_party_child_allowed(self) -> None:
        """FIRST_PARTY_CHILD should satisfy requirement."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="Subsidiary owned by ACME Corp",
                quote_verified=True,
                char_start=0,
                char_end=30,
                source_class=SourceClass.FIRST_PARTY_CHILD,
                supports=Supports.OWNERSHIP,
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is True

    def test_g11_official_document_allowed(self) -> None:
        """OFFICIAL_DOCUMENT should satisfy requirement."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="ACME Corp owns subsidiary",
                quote_verified=True,
                char_start=0,
                char_end=30,
                source_class=SourceClass.OFFICIAL_DOCUMENT,
                supports=Supports.OWNERSHIP,
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is True

    def test_g11_require_for_confirmed_false(self) -> None:
        """If require_for_confirmed=False, empty evidence should be allowed."""
        result = check_g11_evidence_requirement((), require_for_confirmed=False)
        assert result.allowed is True

    def test_g11_multiple_evidence_one_valid(self) -> None:
        """If any evidence satisfies requirement, allow."""
        evidence = (
            VerifiedEvidence(
                page_id=1,
                url="https://example.com",
                quote="Some quote",
                quote_verified=False,
                char_start=None,
                char_end=None,
                source_class=SourceClass.THIRD_PARTY,
                supports=Supports.OWNERSHIP,
            ),
            VerifiedEvidence(
                page_id=2,
                url="https://example.com",
                quote="ACME owns subsidiary",
                quote_verified=True,
                char_start=0,
                char_end=30,
                source_class=SourceClass.FIRST_PARTY_PARENT,
                supports=Supports.OWNERSHIP,
            ),
        )
        result = check_g11_evidence_requirement(evidence, require_for_confirmed=True)
        assert result.allowed is True
