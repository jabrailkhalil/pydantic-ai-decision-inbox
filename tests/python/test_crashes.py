from pathlib import Path

import pytest

from pydantic_ai_decision_inbox.lab import run_scenario


@pytest.mark.subprocess(reason="Verify abrupt death outside Python exception handling")
@pytest.mark.parametrize(
    "identifier",
    [
        "after_decision",
        "after_claim",
        "after_dispatch",
        "after_effect_opaque",
        "after_effect_reconcile",
        "after_receipt",
    ],
)
def test_real_process_crash_boundaries(tmp_path: Path, identifier: str) -> None:
    report = run_scenario(identifier, tmp_path / "run")
    assert report.crash_exit_codes == [73]
    assert all(report.checks.values())
    if identifier == "after_effect_opaque":
        assert report.steps[-1].execution == "unknown"
        assert report.steps[-1].observed_effects == 1
        assert not report.steps[-1].has_receipt
    else:
        assert report.steps[-1].execution == "succeeded"
        assert report.steps[-1].observed_effects == 1


@pytest.mark.subprocess(
    reason="Force two real reviewer processes to contend for one SQLite decision"
)
def test_concurrent_reviewers_have_one_winner(tmp_path: Path) -> None:
    report = run_scenario("reviewer_race", tmp_path / "race")
    assert report.checks == {
        "one_winner_one_conflict": True,
        "one_saved_decision": True,
        "approval_is_not_execution": True,
    }


@pytest.mark.parametrize(
    "identifier",
    ["happy", "denied", "stale_args", "stale_policy", "expired", "negative_control"],
)
def test_noncrash_lab_scenarios(tmp_path: Path, identifier: str) -> None:
    assert all(run_scenario(identifier, tmp_path / "run").checks.values())
