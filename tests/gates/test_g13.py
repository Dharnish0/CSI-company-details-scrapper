"""Tests for G13 — Conservative Direction."""

from corpdisc.gates import check_g13_conservative_direction
from corpdisc.models.domain import VerificationStatus


class TestG13ConservativeDirection:
    """Tests for G13 — Conservative Direction."""

    def test_g13_normal_demotion(self) -> None:
        """Demotion (CONFIRMED -> REVIEW_REQUIRED) should be allowed."""
        result = check_g13_conservative_direction(
            status=VerificationStatus.REVIEW_REQUIRED,
            confidence=0.6,
            previous_status=VerificationStatus.CONFIRMED,
            previous_confidence=0.8,
        )
        assert result.allowed is True

    def test_g13_promotion_prohibited(self) -> None:
        """Promotion (REVIEW_REQUIRED -> CONFIRMED) should be rejected."""
        result = check_g13_conservative_direction(
            status=VerificationStatus.CONFIRMED,
            confidence=0.8,
            previous_status=VerificationStatus.REVIEW_REQUIRED,
            previous_confidence=0.6,
        )
        assert result.allowed is False
        assert result.reason == "PROMOTION_PROHIBITED"

    def test_g13_confidence_promotion_prohibited(self) -> None:
        """Confidence increase should be rejected."""
        result = check_g13_conservative_direction(
            status=VerificationStatus.REVIEW_REQUIRED,
            confidence=0.8,
            previous_status=VerificationStatus.REVIEW_REQUIRED,
            previous_confidence=0.6,
        )
        assert result.allowed is False
        assert result.reason == "CONFIDENCE_PROMOTION_PROHIBITED"

    def test_g13_same_status_same_confidence_allowed(self) -> None:
        """Same status and confidence should be allowed."""
        result = check_g13_conservative_direction(
            status=VerificationStatus.REVIEW_REQUIRED,
            confidence=0.6,
            previous_status=VerificationStatus.REVIEW_REQUIRED,
            previous_confidence=0.6,
        )
        assert result.allowed is True

    def test_g13_both_demoted_allowed(self) -> None:
        """Both demoted should be allowed."""
        result = check_g13_conservative_direction(
            status=VerificationStatus.REJECTED,
            confidence=0.2,
            previous_status=VerificationStatus.REVIEW_REQUIRED,
            previous_confidence=0.6,
        )
        assert result.allowed is True

    def test_g13_status_decrease_confidence_stable(self) -> None:
        """Status decrease with stable confidence should be allowed."""
        result = check_g13_conservative_direction(
            status=VerificationStatus.REVIEW_REQUIRED,
            confidence=0.6,
            previous_status=VerificationStatus.CONFIRMED,
            previous_confidence=0.6,
        )
        assert result.allowed is True
