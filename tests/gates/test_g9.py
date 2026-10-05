"""Tests for G9 — Budget."""

from dataclasses import dataclass

from corpdisc.gates import check_g9_budget


@dataclass(frozen=True)
class MockBudgetGuard:
    """Mock BudgetGuard for testing."""
    max_depth: int = 8
    max_pages_per_domain: int = 40
    max_total_pages: int = 2000
    max_llm_calls_per_task: int = 12
    max_tool_calls_per_task: int = 20

    def check_depth(self, depth: int) -> object:
        from corpdisc.contracts.common import BudgetDecision
        if depth > self.max_depth:
            return BudgetDecision(allow=False, reason="DEPTH_LIMIT", detail=f"depth {depth} exceeds MAX_DEPTH {self.max_depth}")
        return BudgetDecision(allow=True)

    def check_page(self, domain: str) -> object:
        from corpdisc.contracts.common import BudgetDecision
        return BudgetDecision(allow=True)  # Don't track pages in mock

    def check_llm(self, **kwargs) -> object:
        from corpdisc.contracts.common import BudgetDecision
        return BudgetDecision(allow=True)  # Don't track LLM in mock

    def check_tool_call(self, **kwargs) -> object:
        from corpdisc.contracts.common import BudgetDecision
        return BudgetDecision(allow=True)  # Don't track tool calls in mock


class TestG9Budget:
    """Tests for G9 — Budget."""

    def test_g9_within_budget(self) -> None:
        """Within budget should be allowed."""
        guard = MockBudgetGuard(max_depth=8)
        result = check_g9_budget(guard, depth=3)
        assert result.allowed is True

    def test_g9_depth_exceeded(self) -> None:
        """Depth exceeded should be rejected with BUDGET_STOPPED."""
        guard = MockBudgetGuard(max_depth=5)
        result = check_g9_budget(guard, depth=10)
        assert result.allowed is False
        assert result.reason == "BUDGET_STOPPED"
        assert "depth" in result.detail.lower()
        assert "exceeds" in result.detail.lower()

    def test_g9_page_budget(self) -> None:
        """Page budget check should be allowed in mock."""
        guard = MockBudgetGuard(max_pages_per_domain=40)
        result = check_g9_budget(guard, domain="example.com")
        assert result.allowed is True

    def test_g9_llm_budget(self) -> None:
        """LLM budget check should be allowed in mock."""
        guard = MockBudgetGuard()
        result = check_g9_budget(guard, llm_tokens=50)
        assert result.allowed is True

    def test_g9_tool_call_budget(self) -> None:
        """Tool call budget check should be allowed in mock."""
        guard = MockBudgetGuard(max_tool_calls_per_task=5)
        result = check_g9_budget(guard, task_id=1)
        assert result.allowed is True
