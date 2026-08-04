from __future__ import annotations

import os

import pytest

from aidison.providers.gateway import build_chat_model

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_bailian_openai_compatible_live_contract() -> None:
    if not os.getenv("DASHSCOPE_API_KEY"):
        pytest.skip("DASHSCOPE_API_KEY is not configured")
    response = await build_chat_model().ainvoke(
        "Reply with exactly AIDISON_PROVIDER_OK and no other text."
    )
    assert isinstance(response.content, str)
    assert response.content.strip() == "AIDISON_PROVIDER_OK"
