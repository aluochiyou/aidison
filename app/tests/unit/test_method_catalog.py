from uuid import uuid4

from aidison.domain.models import ResearchStrategyTask
from aidison.research.method_catalog import select_methods


def test_method_catalog_selects_only_outputs_authorized_by_the_task() -> None:
    task = ResearchStrategyTask(
        task_key="power.compatibility", title="Power compatibility", objective="Check limits.",
        module_ids=(uuid4(),), priority="must", expected_outputs=("evidence", "compatibility"),
        stop_conditions=("Evidence is recorded.",),
    )
    methods = select_methods(task=task, research_depth="standard")

    assert [item.key for item in methods] == [
        "evidence.source_triage", "compatibility.interface_check"
    ]
