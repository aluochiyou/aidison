from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aidison.api.app import _agent_run_failure_summaries


@pytest.mark.asyncio
async def test_module_visual_failure_is_read_from_versioned_run_event() -> None:
    run_id = uuid4()
    project_id = uuid4()
    session = SimpleNamespace(
        scalars=AsyncMock(
            return_value=[
                SimpleNamespace(
                    payload={
                        "run": {"id": str(run_id)},
                        "failure_code": "module_visual_auth_failed",
                    }
                )
            ]
        )
    )

    summaries = await _agent_run_failure_summaries(
        session=session,
        project_id=project_id,
        run_ids={run_id},
    )

    assert summaries[run_id] == "模块配图模型认证失败，请检查配图服务的 API Key。"
