{
  "principles": [
    "只做用户要求的事，优先复用现有代码和项目模式",
    "实现能力时按标准库、成熟开源、官方 API/SDK/MCP/Skill 的顺序优先复用；自写代码只保留领域规则、安全边界、预算、状态和薄适配层",
    "主控默认并行优先：发现两个以上可独立验收且所有权不重叠的单元时，主动拆分并行执行；主控保留架构、共享接口、集成和最终验收",
    "先定义可验证的成功标准，再实施最小且充分的修改",
    "小任务直接完成；只有复杂度、风险或长期维护价值足够高时才引入重型 Skill 或协作单元",
    "未执行的检查必须标记为 not_checked，不以模型判断替代测试结果"
  ],
  "external_capability_reuse": {
    "search_stack": {
      "local_files": "优先使用 rg/rg --files 和现有文件系统工具",
      "global_web": "使用 Tavily Remote MCP；不得重复实现通用搜索引擎，也不并存第二套 Tavily SDK 产品路径",
      "known_url": "使用 HTTPX 执行受控下载，Trafilatura 提取正文；仅在明确需要 HTML 到 Markdown 格式转换时使用 Markdownify",
      "site_structure_and_multi_page": "使用 Tavily Map/Crawl，不自建通用爬虫",
      "github": "存在可用能力时优先使用 GitHub 官方 MCP 的只读模式"
    },
    "boundary": "外部能力使用薄 adapter 接入；Aidison 继续拥有 SSRF/重定向/MIME/大小/超时校验、预算账本、Artifact/hash、EvidenceBinding 和 canonical Domain 写入",
    "secrets": "外部 Key 只进入本机安全环境或未提交的 .env，不写入聊天、仓库、日志、Artifact、Mai 或状态文档"
  },
  "skills": {
    "selection": "任务一旦匹配当前运行时已暴露 Skill 的描述或触发条件，必须先完整读取并实际调用该 Skill；多 Skill 同时适用时选择覆盖需求的最小充分集合并说明顺序。同一职责只保留一个事实来源，避免重复文档和重复状态系统",
    "execution_rule": "Skill 不是只用于研究：实现、测试、前端、浏览器验证、文档、PDF、数据分析、项目记忆、状态同步等任务进入对应阶段时都必须调用匹配 Skill；不可用或读取失败时明确记录并采用最小替代方案",
    "architecture_and_maintenance": {
      "research-software-project": {
        "use_when": "实施前需要深度理解现有架构、比较方案、评估可行性或结合仓库与 Web/GitHub 证据研究时",
        "skip_when": "需求明确的小改动、简单查找或可直接实现的任务",
        "output": "只读研究证据、结论、风险和实施交接；研究阶段不修改产品代码"
      },
      "grilling": {
        "use_when": "重大需求或架构方案存在关键假设、边界、取舍或未决策点，需要在实施前压力测试时，且当前运行时明确暴露该 Skill",
        "output": "澄清后的需求、反例和取舍；是否形成 ADR 仍由 domain-modeling 的门槛决定"
      },
      "domain-modeling": {
        "use_when": "需要统一领域术语、业务边界、核心实体关系或记录重要架构决策时",
        "output": "维护 CONTEXT.md 中的领域语言；必要时创建 docs/adr/ 决策记录",
        "rule": "不得覆盖用户已经确认的领域语言"
      },
      "project-memory-manager": {
        "init": "仅当现有项目缺少长期文档且后续工作确实依赖项目记忆时初始化",
        "sync": "实质性或架构变更完成并验证后，同步长期有效的上下文、架构、状态和 ADR",
        "audit": "怀疑长期文档与代码、已归档规格或当前状态不一致时审计",
        "owns": ["CONTEXT.md", "docs/ARCHITECTURE.md", "docs/STATUS.md", "docs/adr/"],
        "rule": "只记录长期有效事实，不复制源码、聊天、完整规格、秘密或猜测"
      },
      "mai-sync": {
        "use_when": "存在多 Agent、模块任务，或任务、模块、接口、决策、风险、里程碑、测试、阻塞等项目状态发生实质变化时",
        "output": "同步紧凑、结构化且已验证的项目事实",
        "rule": "Mai 用于结构化项目状态，不替代长期架构文档，也不记录源码全文或过程流水"
      },
      "change_specification": {
        "kind": "仓库内变更约定，不是假设存在的 Skill",
        "use_when": ["新能力", "跨模块变更", "外部行为变化", "数据模型变化", "安全或迁移风险", "需要多项验收场景"],
        "skip_when": "简单修复、局部重构或单一明确的小改动",
        "output": "在仓库已有规格目录中记录边界、验收、风险和迁移；若仓库尚无约定，由主控在实施前选择最小格式"
      }
    },
    "orchestration": {
      "small_change": "直接实施和验证，不运行架构维护类 Skill",
      "existing_project_research": "research-software-project -> 形成只读研究交接 -> 再决定是否实施",
      "domain_or_architecture_design": "必要时 project-memory-manager init -> 当前可用时 grilling -> domain-modeling -> 主控冻结变更边界",
      "substantial_change": "必要时 change_specification -> 实施 -> 审查与验证 -> project-memory-manager sync -> 必要时 mai-sync",
      "maintenance": "长期文档不一致时 project-memory-manager audit；项目结构化状态变化时 mai-sync",
      "boundaries": [
        "change_specification 管单次变更边界，project-memory-manager 管长期知识，mai-sync 管结构化当前状态投影",
        "domain-modeling 管术语、边界和重要决策，research-software-project 只负责实施前研究",
        "只有当前运行时 Skills 清单中存在且 SKILL.md 可完整读取的 Skill 才能进入工作流；不可用能力由主控直接完成，不伪造 Skill 名称",
        "没有对应产出需要时跳过该 Skill，不为满足流程而运行 Skill"
      ]
    },
    "codex-team-workflow": {
      "must_use_when": "准备使用任何 Subagent、模块任务、独立 Reviewer、Branch/Worktree 或多 Agent 协作时",
      "rule": "统一管理执行形态、Agent 路由、委派契约、文件所有权、隔离、审查、集成和交接；复杂任务默认形成并行 wave，简单或强耦合任务才由主任务直接完成",
      "module_task_authority": "用户已授权主控为长期、可独立验收的模块创建侧边栏任务；只有外部权限、不可逆操作或重大产品取舍仍需另行确认"
    }
  },
  "subagents": {
    "routing": {
      "qwen-local": "完整功能 Writer、测试、普通模块实现，也可承担检索、总结、explorer 和常规审查",
      "glm-5-2": "完整功能 Writer、测试、普通模块实现，也可承担独立审查、反例检查和文档沉淀",
      "gpt-5-6-sol": "深度研究、架构定调、复杂调试、高难算法，以及共享接口、安全、迁移或高风险发布终审"
    },
    "reasoning_effort": "调用时请求 high；effective_reasoning_effort 只有在运行时明确报告时才记录，否则记为 runtime_managed/not_checked",
    "rules": [
      "任何 Subagent 调用前必须先使用 codex-team-workflow",
      "显式设置运行时支持且符合硬路由的 agent_type，并请求 high 推理强度",
      "普通功能实现优先使用 qwen-local 或 glm-5-2；两者都可以获得明确且互不重叠的文件所有权，并直接实现、测试和交付模块",
      "功能 Writer 可以修改其 Scope 内文件，但未经主控明确授权不得执行 git add、git commit、git push 或改写 Git 历史；Git 集成默认由主控完成",
      "不得使用 gpt-5-5 或 default 替代 qwen-local、glm-5-2 或 gpt-5-6-sol 路由",
      "委派需写明 Outcome、Benefit、Sources、Scope、Checks、Stop when、Return 和唯一写入所有权",
      "取消 nonce/canary 和首包逐字回显门禁；Subagent 接到完整契约后可直接在 Scope 内执行，主控以实际 Diff、测试和文件所有权审查结果验收",
      "允许 Subagent 在委派契约明确授权时继续拆分互不重叠的子任务；未授权时不递归委派，嵌套任务同样遵守单 Writer 和主控独占边界",
      "空回包、越界修改或不合格交付只允许一次定向纠正；随后由主控检查现有 Diff/测试并决定接管、替换或停止，不进行 nonce 重试",
      "所需角色不可用时停止该项委派，不用其他角色替代",
      "普通审查使用 qwen-local；只有涉及架构定调、共享接口、安全、迁移、高难算法或重大发布风险时使用 gpt-5-6-sol 终审"
    ]
  },
  "module_tasks": {
    "routing": "普通模块任务由 qwen-local 或 glm-5-2 作为功能 Writer；深度架构、研究、复杂调试或高风险模块使用 gpt-5-6-sol",
    "reasoning_effort": "请求 high，实际值以运行时报告为准",
    "rule": "用户已授权主控为长期独立模块主动创建侧边栏任务；每个任务必须报告目标、所有权、依赖、Worktree、验收标准、请求配置和运行时可验证的实际配置"
  },
  "collaboration_governance": {
    "controller": "rick 是唯一主控，负责需求拆解、共享契约冻结、并行 wave、冲突处理、顺序集成、项目级验证和状态同步；其他 Agent 不得改变项目方向或共享事实源",
    "parallel_first": [
      "复杂任务开始时先形成依赖图，把无依赖且所有权不重叠的任务组成同一 wave；串行只用于共享接口冻结、依赖集成和最终验收",
      "默认保持 3–5 个有效并行单元：2–3 个功能 Writer，加 1 个测试/反例 Agent 和 1 个 Reviewer；任务不足时不为填槽位而制造工作",
      "长生命周期、跨多轮且可独立验收的模块使用侧边栏任务和独立 Worktree；短期文件级实现、测试和审查使用 Subagent",
      "主控在每个 wave 开始前把 owner、文件、依赖、验收、风险写入 team.json；wave 封存后才启动独立 Reviewer，随后逐个集成"
    ],
    "controller_exclusive": [
      "AGENTS.md 和协作治理文件",
      "跨模块 API/contracts/schema、数据库迁移顺序和 Event envelope",
      "lockfile、根级构建配置、CI、Compose 和基础设施配置",
      "docs/status/team.json、集成分支和项目级最终验证",
      "CONTEXT.md、docs/ARCHITECTURE.md、docs/STATUS.md、docs/adr/ 的最终落盘",
      "Mai 的项目级状态同步"
    ],
    "single_writer": "每个可变文件或稳定实体在同一并发阶段只能有一个 Writer；所有权必须在委派 Scope 与 team.json 一致。共享目标默认归主控，未显式转移不得写入",
    "worker_rule": "Worker 对主控独占文件只返回提案、patch 片段、测试结果或证据，不直接修改",
    "concurrency": [
      "接口冻结前可并行 3–5 个只读、检索、测试或反例 Subagent；共享文件仍由主控写",
      "接口冻结后默认 2–3 个互不重叠 Writer 并行，另加最多 2 个只读测试/Reviewer；同一文件或稳定实体始终只有一个 Writer",
      "独立 Writer 默认使用独立 Branch/Worktree；短期且文件完全不重叠时可共享工作区，但必须在 team.json 明确所有权",
      "并发上限由主控合并能力、共享状态和验证成本决定，不为填满运行时槽位而增加 Agent"
    ],
    "phases": ["intake_and_required_reading", "contract_freeze", "parallel_isolated_execution", "seal_and_read_only_review", "sequential_integration", "project_verification_and_state_sync"]
  },
  "state_sources": {
    "coordination": "docs/status/team.json 是当前 Agent、所有权、branch/worktree、检查和阻塞的 canonical source",
    "module_handoff": "docs/status/<module>.md 是模块交接证据快照，不维护全局状态",
    "implementation_truth": "运行代码、配置和已执行测试是实际行为事实源",
    "domain_language": "CONTEXT.md 是用户确认的领域语言事实源",
    "decision_rationale": "accepted ADR 是工程决策、替代项和失效条件事实源",
    "durable_memory": "docs/ARCHITECTURE.md 与 docs/STATUS.md 只保存已验证且长期有效的架构和阶段",
    "dashboard": "Mai 是由主控同步的结构化投影，不反向覆盖 team.json、代码、测试、CONTEXT 或 ADR"
  },
  "required_reading": {
    "all_agents": ["AGENTS.md", "完整委派契约", "Scope 内代码/测试", "契约列出的 Sources"],
    "module_writer": ["模块入口", "直接依赖接口", "相关 CONTEXT/ADR", "该模块当前 handoff"],
    "shared_boundary": ["相关 contract/schema", "调用方和使用方", "适用 ADR；共享文件仍由主控写"],
    "reviewer": ["冻结 Diff", "验收标准", "相关 contracts/ADR", "验证证据；不得以未冻结工作区作终审依据"],
    "rule": "按任务路由阅读，不默认载入完整研究包或全部历史文档"
  },
  "decision_lineage": {
    "fields": ["problem", "alternatives", "selected", "rejected", "evidence", "counterevidence", "expected_consequences", "validation", "invalidation_trigger", "supersedes", "commit_lineage"],
    "rule": "只在真实且长期的取舍满足 ADR 门槛时完整记录；小决策留在变更规格或模块 handoff，禁止把聊天流水或隐藏推理写入仓库"
  },
  "workflow": {
    "change": "明确验收标准 -> 实施最小修改 -> 运行相关验证 -> 报告结果",
    "bug": "复现 -> 定位根因 -> 最小修复 -> 回归测试 -> 验证",
    "substantial": "明确边界和风险 -> 按 skills.orchestration 选择必要 Skill -> 实施 -> 审查与验证 -> 同步长期文档和项目状态"
  },
  "done": [
    "需求满足",
    "相关测试或检查通过",
    "外部接口和长期事实已在必要时同步",
    "风险、未完成项和 not_checked 项已明确说明"
  ],
  "safety": [
    "不覆盖用户改动，不 force-reset Git，不未经批准删除文件或执行不可逆操作",
    "风险操作前确认目标并备份重要文件",
    "不暴露或记录密码、API Key、Token 等敏感信息"
  ]
}
