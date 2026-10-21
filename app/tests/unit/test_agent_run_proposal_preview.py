from __future__ import annotations

from uuid import uuid4

import pytest

from aidison.api.app import _agent_run_proposal_preview


class _Artifacts:
    def __init__(self, value: object) -> None:
        self._value = value

    async def read_json_ref(self, **_: object) -> object:
        return self._value


@pytest.mark.asyncio
async def test_preview_hides_unsupported_recommendations_and_blocks_adoption() -> None:
    unsupported_module = str(uuid4())
    supported_module = str(uuid4())
    preview = await _agent_run_proposal_preview(
        artifacts=_Artifacts(
            {
                "schema_version": "research-proposal-manifest-v3",
                "question": "Choose a safe design.",
                "task_results": [
                    {
                        "task_key": "power.research",
                        "capability": "research",
                        "plan_revision": 1,
                        "module_id": unsupported_module,
                        "payload": {
                            "recommended_option": "Unsupported motor",
                            "summary": "The model made a recommendation without evidence.",
                            "alternatives": ["Other motor"],
                        },
                        "evidence_refs": [],
                    },
                    {
                        "task_key": "control.research",
                        "capability": "research",
                        "plan_revision": 1,
                        "module_id": supported_module,
                        "payload": {
                            "recommended_option": "Supported controller",
                            "summary": "A source-backed controller recommendation.",
                            "alternatives": ["Other controller"],
                        },
                        "evidence_refs": ["artifact+sha256://evidence/1"],
                    },
                ],
            }
        ),
        project_id=uuid4(),
        basis_hash="a" * 64,
        proposal_manifest_ref="artifact+sha256://manifest/1",
    )

    assert preview is not None
    assert preview["adoption_allowed"] is False
    assert preview["unsupported_module_ids"] == [unsupported_module]
    assert preview["module_summaries"] == [
        {
            "module_id": supported_module,
            "recommended_option": "Supported controller",
            "summary": "A source-backed controller recommendation.",
            "alternatives": ["Other controller"],
            "evidence_count": 1,
        }
    ]
