from uuid import UUID

from aidison.module_visuals import ModuleVisualInput, ModuleVisualSpec, render_module_visual_svg


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
