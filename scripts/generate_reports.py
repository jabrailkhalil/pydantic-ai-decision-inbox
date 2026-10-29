import json
import tempfile
from pathlib import Path

from pydantic_ai_decision_inbox.lab import SCENARIOS, run_scenario

ROOT = Path(__file__).resolve().parents[1]
workspace = ROOT / "demo-work"
workspace.mkdir(exist_ok=True)
reports = []
if __name__ == "__main__":
    for identifier in SCENARIOS:
        with tempfile.TemporaryDirectory(
            prefix=identifier + "-", dir=workspace
        ) as temp:
            directory = Path(temp).resolve()
            assert directory.is_relative_to(workspace.resolve())
            report = run_scenario(identifier, directory)
            reports.append(report.model_dump(mode="json"))
            print(identifier, len(report.steps), "steps; checks passed", flush=True)
    (ROOT / "public").mkdir(exist_ok=True)
    (ROOT / "public" / "reports.json").write_text(
        json.dumps(
            dict(
                version=1,
                source="Recorded executions of the included Python lab",
                reports=reports,
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
