import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pydantic import SecretStr

from aidison.module_visuals import (
    ModuleVisualInput,
    ModuleVisualSettings,
    ModuleVisualSpec,
    QwenModuleVisualProvider,
    render_module_visual_svg,
)


def test_module_visual_svg_is_rendered_only_from_validated_safe_scalars() -> None:
    module = ModuleVisualInput(
        module_id=UUID(int=1),
        name='<script>alert("x")</script> 飞控',
        responsibility="协调姿态控制与任务载荷",
    )
    spec = ModuleVisualSpec(
        scene="electronics",
        accent="#D98934",
        secondary="#2E7794",
        icon="controller",
        badges=("闭环控制", "接口核验"),
        caption="飞控与载荷协同",
    )

    svg = render_module_visual_svg(module, spec).decode("utf-8")

    assert svg.startswith("<svg")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "#D98934" in svg
    assert "闭环控制" in svg


@pytest.mark.asyncio
async def test_qwen_visual_provider_repairs_out_of_contract_enums_once() -> None:
    create = AsyncMock(
        side_effect=(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "scene": "高空悬停工况",
                                    "accent": "#D98934",
                                    "secondary": "#2E7794",
                                    "icon": "uav_platform",
                                    "badges": ["载荷"],
                                    "caption": "飞行平台",
                                }
                            )
                        )
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "scene": "airfield",
                                    "accent": "#D98934",
                                    "secondary": "#2E7794",
                                    "icon": "drone",
                                    "badges": ["载荷"],
                                    "caption": "飞行平台",
                                }
                            )
                        )
                    )
                ]
            ),
        )
    )
    client = MagicMock()
    client.chat.completions.create = create
    provider = QwenModuleVisualProvider(
        ModuleVisualSettings(api_key=SecretStr("sk-test"), model="qwen3.7-flash"), client=client
    )

    spec = await provider.propose(
        ModuleVisualInput(
            module_id=UUID(int=2),
            name="飞行平台",
            responsibility="承载任务载荷",
        )
    )

    assert spec.scene == "airfield"
    assert spec.icon == "drone"
    assert create.await_count == 2
    assert create.await_args_list[0].kwargs["response_format"] == {"type": "json_object"}
    retry_prompt = create.await_args_list[1].kwargs["messages"][0]["content"]
    assert "do not translate or invent enum values" in retry_prompt
