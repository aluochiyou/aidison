"""Frozen UAV/DIY cases for the opt-in controlled-live benchmark."""

from __future__ import annotations

from aidison.evaluation.controlled_live_contracts import (
    ControlledLiveCase,
    ControlledLiveManifest,
    ControlledLiveRequirements,
)

CONTROLLED_LIVE_CASES: tuple[ControlledLiveCase, ...] = (
    ControlledLiveCase(
        key="balanced-quadrotor",
        title="预算受限的通用四旋翼选型",
        project_name="Live Eval：通用四旋翼",
        goal="设计一套预算 5000 元以内、可载荷 500 克且目标续航 25 分钟的四旋翼方案。",
        requirements=ControlledLiveRequirements(
            hard_constraints=("总预算不超过 5000 元", "有效载荷不少于 500 克"),
            preferences=("优先选择资料完整且国内容易采购的部件",),
            available_resources=("具备基础焊接工具和 3D 打印条件",),
            usage_context="实验室外场验证与日常算法开发。",
            budget_context="整机硬件预算 5000 元。",
            skill_context="使用者能完成基础装配和飞控参数配置。",
            unknowns=("最终传感器型号尚未确定",),
        ),
        planning_brief="拆分动力、电源、飞控、机架和通信，并显式检查重量与功率耦合。",
        objective="比较候选部件并形成有原文证据支持的兼容方案。",
        concept_groups=(("动力", "电机", "螺旋桨"), ("电源", "电池", "供电"), ("飞控", "控制")),
        min_module_count=3,
        min_plan_task_count=3,
    ),
    ControlledLiveCase(
        key="long-range-fpv",
        title="七寸长航时 FPV 平台",
        project_name="Live Eval：长航时 FPV",
        goal="设计一套预算 3500 元、七寸桨、目标续航 20 分钟并具备可靠失控保护的 FPV 平台。",
        requirements=ControlledLiveRequirements(
            hard_constraints=("使用七寸桨", "必须提供失控保护方案", "预算不超过 3500 元"),
            preferences=("优先成熟开源飞控生态",),
            usage_context="开阔区域长距离图传与航线验证。",
            budget_context="不包含遥控器和显示设备。",
            skill_context="具备 Betaflight 或 ArduPilot 基础。",
            unknowns=("目标图传制式待比较",),
        ),
        planning_brief="关注动力效率、链路预算、导航定位和失控保护之间的约束。",
        objective="给出长航时 FPV 的候选组合、链路风险与验证步骤。",
        concept_groups=(
            ("动力", "电机", "螺旋桨"),
            ("图传", "通信", "链路"),
            ("失控", "保护", "failsafe"),
        ),
        min_module_count=3,
        min_plan_task_count=3,
    ),
    ControlledLiveCase(
        key="indoor-inspection",
        title="无 GPS 室内巡检无人机",
        project_name="Live Eval：室内巡检",
        goal="设计一套可在无 GPS、弱光和狭窄空间运行的室内巡检无人机，并降低碰撞风险。",
        requirements=ControlledLiveRequirements(
            hard_constraints=("不能依赖 GPS", "需要桨叶防护", "支持弱光环境"),
            preferences=("整机尺寸尽量紧凑", "优先可重复标定的传感器"),
            usage_context="实验室管廊与室内设备巡检。",
            skill_context="团队具备 ROS 2 和视觉算法经验。",
            unknowns=("是否需要自主避障由评测结果决定",),
        ),
        planning_brief="区分定位感知、飞控、结构防护、照明成像和计算平台。",
        objective="验证无 GPS 定位、弱光成像及安全防护的组合可行性。",
        concept_groups=(
            ("定位", "视觉", "slam"),
            ("防护", "碰撞", "桨叶"),
            ("弱光", "照明", "成像"),
        ),
        min_module_count=3,
        min_plan_task_count=3,
    ),
    ControlledLiveCase(
        key="mapping-rtk",
        title="RTK 测绘无人机",
        project_name="Live Eval：RTK 测绘",
        goal="设计一套面向一平方公里区域航测的 RTK 四旋翼，目标续航 35 分钟并保留影像地理标记。",
        requirements=ControlledLiveRequirements(
            hard_constraints=("支持 RTK", "目标续航不少于 35 分钟", "影像需要可追溯的地理标记"),
            preferences=("优先开放接口和可导出的任务日志",),
            usage_context="科研测绘和重复航线采集。",
            skill_context="团队熟悉航线规划和摄影测量基础。",
            unknowns=("相机重量和快门类型待选型",),
        ),
        planning_brief="同时规划定位、载荷、触发同步、航线、存储和动力预算。",
        objective="形成 RTK 测绘链路的候选、证据和端到端同步风险分析。",
        concept_groups=(("rtk", "定位"), ("相机", "载荷", "影像"), ("同步", "触发", "时间戳")),
        min_module_count=3,
        min_plan_task_count=3,
    ),
    ControlledLiveCase(
        key="payload-retrofit",
        title="既有平台载荷升级",
        project_name="Live Eval：载荷改装",
        goal="在保留现有飞控和机架的前提下，为已有四旋翼增加 700 克热成像载荷并评估续航变化。",
        requirements=ControlledLiveRequirements(
            hard_constraints=("保留现有飞控", "保留现有机架", "新增载荷约 700 克"),
            preferences=("尽量减少不可逆结构改动",),
            available_resources=("现有平台使用 6S 电池和 CAN 外设总线",),
            usage_context="热源巡检验证。",
            skill_context="可修改安装件、供电线束和飞控参数。",
            unknowns=("现有电机型号和剩余推力余量需复核",),
        ),
        planning_brief="把不可变部件作为锁定约束，重点分析推力、重量、供电和接口变化。",
        objective="判断载荷升级是否可行，并给出最小改动路径和验证清单。",
        concept_groups=(
            ("载荷", "重量"),
            ("推力", "动力"),
            ("供电", "电源", "6s"),
            ("can", "接口", "总线"),
        ),
        min_module_count=3,
        min_plan_task_count=3,
    ),
    ControlledLiveCase(
        key="vtol-fixed-wing",
        title="垂直起降固定翼方案",
        project_name="Live Eval：VTOL 固定翼",
        goal="设计一套可垂直起降、巡航 60 分钟并支持自动航线任务的固定翼无人机验证方案。",
        requirements=ControlledLiveRequirements(
            hard_constraints=("支持垂直起降", "目标巡航时间 60 分钟", "支持自动航线"),
            preferences=("优先具有公开集成文档的飞控生态",),
            usage_context="长距离科研观测的技术验证机。",
            budget_context="原型阶段预算 12000 元。",
            skill_context="团队具备固定翼装配和 ArduPilot 调参经验。",
            unknowns=("采用尾座式还是复合翼布局待比较",),
        ),
        planning_brief="显式比较布局、垂直动力、巡航动力、能源、飞控模式切换和结构风险。",
        objective="比较 VTOL 架构并形成可验证的原型集成与测试方案。",
        concept_groups=(
            ("布局", "复合翼", "尾座"),
            ("垂直", "起降", "悬停"),
            ("巡航", "固定翼"),
            ("切换", "模式", "飞控"),
        ),
        min_module_count=4,
        min_plan_task_count=4,
    ),
)

CONTROLLED_LIVE_MANIFEST = ControlledLiveManifest.from_cases(CONTROLLED_LIVE_CASES)

__all__ = ["CONTROLLED_LIVE_CASES", "CONTROLLED_LIVE_MANIFEST"]
