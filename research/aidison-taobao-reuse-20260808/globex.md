# Globex 电商项目可借鉴性分析（Taobao 搜索/推荐范围）

- 分析对象：`/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/`（42 个 Markdown 教程文档，约 17k 行，无独立产品代码，代码均为文档内片段）
- 范围：仅淘宝商品搜索 / 推荐 / 详情展示 / 只读工具权限。不做购买、购物车、下单、支付、PRODUCT_REDIRECT。
- 标记沿用仓库约定并在此标注可借鉴性：`[F]` 可借鉴（有文档内容证据，直接落地）；`[J]` 有条件借鉴（需设计判断，边界受限）；`[X]` 不可复用（与 Aidison 边界冲突或无证据）。
- 引用约定：`工具设计篇/11 ItemSearch…md §2.2` 指该文档第 2.2 节；Aidison 侧引用真实代码路径。

## 总体结论

Globex 是**跨平台对话式购物 Agent**（amazon/shopee/aliexpress/ebay 四平台检索 → 比价 → 算到手价 → 精挑 → 购物清单）。其最大借鉴价值集中在**工具签名设计、双通道召回合并、精挑评分、偏好 Store、只读工具的阶段化过滤**五个工程模式；代码不可直接复用（不存在的 `app/` 包 + 依赖 Faiss/OpenSearch/Redis/LangGraph checkpoint）。商品详情与购物车 Globex 本身没有实现。

| 主题 | 结论 | 关键借鉴点 |
|---|---|---|
| 商品搜索 | `[F]` 模式可借鉴，代码不可复用 | 工具签名、Candidate 稳定结构、语义+个性化双通道合并 |
| 推荐 | `[F]` 模式可借鉴 | ItemPicker 硬约束/软偏好、rejected_brief 截断、偏好 Store |
| 详情展示 | `[J]` 部分借鉴 | attributes 嵌套按需展开；Globex 无详情页本体 |
| 只读工具权限 | `[F]` 阶段化过滤思想，`[X]` 其实现 | 每阶段只暴露工具子集、隐藏优于拒绝、风险分级 |

---

## 1、商品搜索（淘宝 `search_offers`）

**结论：`[F]` 借鉴三个工程模式；`[X]` 直接搬代码。**

### 1.1 工具签名设计原则（可借鉴）`[F]`

`工具设计篇/11 ItemSearch…md §2.2`：工具"真正的用户是大模型"，签名要"入参少而正交，出参字段名稳定且可被后续工具消费"。

```python
class Candidate(BaseModel):
    item_id: str; platform: str; title: str; price: float
    currency: str; rating: float | None; sales: int | None
    image_url: str | None; attributes: dict  # 材质/风格等结构化属性

class ItemSearchOutput(BaseModel):
    platform: str; candidates: list[Candidate]
    total_recall: int   # 召回总数
    truncated: bool     # 是否因 top_k 截断
```

- `platform` 用 `Literal` 而不是 `str`，避免模型生成等价但不规范的字符串 —— 对应淘宝 `region="CN"` 固定值、查询词必须非空的既有约束。
- 出参字段少而稳：`ShoppingOffer`（`src/aidison/providers/shopping.py:51-77`）已是稳定扁平结构，可对齐该原则，不铺平 50 个属性字段。
- `total_recall` / `truncated` 是"给后续工具判断的信号，不是给模型看的统计"（§5.3）：`truncated=True` 时后续可提示再取更大 `top_k`。淘宝 adapter 目前直接截断 `page_size`（`src/aidison/providers/taobao.py:189`），无截断信号，可补充。

### 1.2 双通道召回合并（算法层面可借鉴）`[J]`

`向量召回篇/04-0 LLM三塔…md §4` + `11 ItemSearch…md §3.3`：语义通道 + 个性化通道各自 `asyncio.create_task` 并行召回，合流后 `_dedupe_and_rerank` 按加权分去重合并：

```python
async def _recall(...):
    semantic_task = asyncio.create_task(_semantic_recall(...))
    personalized_task = (asyncio.create_task(_personalized_recall(...)) if user_id else None)
    ...
    merged = _dedupe_and_rerank(semantic, personalized)
```

- `[F]` 双通道合并思想：语义召回 + （若有用户偏好）个性化召回并行、去重加分（`existing["boost"] += 0.5 * score`）。淘宝 `taobao.tbk.dg.material.optimal` 只有关键词检索，可借鉴"两路结果合并 + 双命中加分"的排序思路提升推荐相关度。
- `[X]` 三塔 embedding/ANN 基础设施（`04-0 §3`，User/Query/Item 三塔 + Faiss）需要向量库与 embedding 服务，超出当前淘宝 affiliate 搜索的最小范围；不做模型训练。属算法借鉴而非代码复用。
- `[X]` `ann.py` / `towers.py` 的 Faiss + HTTP 塔调用代码不可复用（Aidison 无该 infra）。

### 1.3 工具链路单向数据流（可借鉴）`[F]`

`工具设计篇/12 PriceCompare…md §6.2`：下游不能反向调用上游；上游产出的字段下游要么用要么忽略。`11 §5.1` 强调 `attributes` 嵌套、"按需展开"。

- 对应淘宝现状：`search_offers` 是独立只读命令，`taobao.py:355-454` 解析后生成 `ShoppingOffer` 快照，无下游反向查询，已符合该原则。
- `[F]` 借鉴点：候选结构（Candidate→PricePoint→LandedCost）的分层 schema 演进，将来若要展示"到手价/邮费"可直接加展示层字段，不改上游。

---

## 2、推荐（精挑 + 偏好）

**结论：`[F]` 精挑评分与偏好 Store 模式直接借鉴；`[X]` 其三塔个性化与多用户分层。**

### 2.1 ItemPicker：硬约束与软偏好分离 `[F]`

`效果评测篇/14 主AgentLoop…md §2.1`：硬约束走 `HARD_FAIL` 直接剔除，软偏好只加分；`rejected_brief` 截断到 8 条、`reasons` 限 3 条，避免被淘汰项撑爆上下文。

```python
flags = _check_preferences(cost, prefs)
if any(f.startswith("HARD_FAIL:") for f in flags):
    rejected.append(...); continue
score, reasons = _score(cost, insight, prefs)
```

- `[F]` "硬约束剔除 + 软偏好加权"两段式评分，可直接用于淘宝搜索结果排序（如预算上限为硬约束、销量/评分/店铺权重为软偏好）。当前淘宝搜索结果按 API 返回顺序透传（`taobao.py:394-453`），无排序策略，可借鉴此模式。
- `[F]` 结果收敛：只返回 Top-N 精选 + 简短排除原因，而不是把全部候选塞回模型（对应 `shopping.py` 搜索返回 `max_results` 有界的设计，`src/aidison/providers/shopping.py:156-172`）。

### 2.2 偏好 Store：结构化条目 + 相关注入 `[F]`

`Context工程篇/06 长期记忆…md §3.1,§6`：存结构化偏好条目而非原始消息；`read_relevant` 按当前 query 只注入最相关 Top-5，控制 token。

```python
@dataclass
class PreferenceEntry:
    key: str; category: str   # preference / history / blacklist
    content: str; source_session: str
    confidence: float = 1.0   # 多次提及 → 高置信
```

- `[F]` 黑名单关键词（如"不要塑料"）→ 搜索时过滤，是单用户 local-first 场景直接可落地的推荐增强，无需向量库（规则匹配即可，`06 §4.1` 有 `maybe_write_preference` 规则版）。
- `[J]` `read_relevant` 的向量匹配注入依赖 embedding；单用户场景可退化为"全部条目按条目注入"或规则相关性，不必上向量库。
- `[X]` Store 后端选 Redis + LangGraph checkpoint（`06` 未定后端），与 Aidison "PostgreSQL 是业务事实源"冲突（`CONTEXT.md:32`）。偏好应落 PostgreSQL，不引 Redis。

### 2.3 个性化推荐

- `[J]` 三塔 User 塔的"历史偏好融合进 query"思路（`04-0 §3`）可借鉴为"用已存偏好给搜索词做加权或过滤"，但不需要训练 embedding。
- `[X]` 多用户 user_tier（free/standard/premium，`17-4 §6`）与 Aidison 单用户 local-first 定位冲突，不借鉴。

---

## 3、详情展示

**结论：`[J]` 仅借鉴"attributes 嵌套按需展开"的展示原则；`[X]` Globex 无商品详情页本体，无法借鉴。**

- `[F]` 证据：`11 ItemSearch…md §5.1` 明确不把 attributes 平铺，嵌套 dict 按需展开；`09 项目总览 §3.4` 9 个工具清单中无详情工具；grep 全库无"详情页/商品详情"实现（唯一出现是面试题库问答）。
- `[F]` Aidison 现状：`ShoppingOffer` 含 `raw_provider_payload` 透传淘宝原始字段（`taobao.py:441-452`），可从中取 `num_iid`/优惠券信息做详情展示，无需新详情接口。
- `[J]` 借鉴点：详情卡片展示时只渲染必要字段 + "按需展开完整属性"，避免一次渲染 50 个字段 —— 与前端 `ShoppingView.tsx` 报价卡片一致即可。

---

## 4、只读工具权限

**结论：`[F]` 借鉴"按阶段/风险暴露只读工具子集"的过滤思想；`[X]` 其内存状态机实现与"写工具"风险分级不适用（本范围只读）。**

### 4.1 阶段化工具过滤与"隐藏优于拒绝" `[F]`

`Harness工程篇/17-4 动态工具权限…md §2,§3`：

```python
PHASE_TOOLS = {
    Phase.SEARCHING: {"item_search", "web_search", "category_insight", "chat_fallback"},
    ...
}
def get_filtered_tool_set():
    allowed = phase_machine.get_allowed_tools()
    return [tool for tool in FULL_TOOL_SET if tool.name in allowed]  # 模型看不到被隐藏工具 schema
```

- `[F]` "缩小工具空间比换更强模型更有效"（`17-4 §1.2` statewright 结论：2/10→10/10）。对 Taobao 搜索：`search_offers` 是唯一只读工具时，不把购物车/下单等不存在的工具暴露给模型，从源头消除错误选择。
- `[F]` 工具风险分级思想（`17-4 §5`：READ_ONLY / WRITE / RESOURCE_HEAVY）：只读搜索类保持全开，写副作用工具才需审批 —— 与本范围"只读工具权限"一致，可作为将来扩展 Gate 的词典。
- `[X]` 其实现：`PhaseStateMachine` 用 ContextVar（`17-4 §3.1`，内存态、随请求消失）不持久化；Aidison 需要 durable 状态（PostgreSQL）。阶段状态机若引入，应落库（如 `EffectApprovalStatus` 已有持久化先例，`src/aidison/domain/models.py:438`）。

### 4.2 工具白名单（只读校验）`[F]`

`部署观测篇/16-6 安全护栏…md §2.1`：L1 工具白名单 —— 模型只能调用已注册工具。

```python
def validate_tool_call(tool_name: str) -> bool:
    return tool_name in ALLOWED_TOOLS
```

- `[F]` 只读白名单：`search_offers` 作为唯一注册的搜索工具，任何非白名单调用直接拒绝。Aidison 已有更严格的确定性 Gate（`EffectApproval` 按 scope_hash 约束，`src/aidison/application/shopping.py:539-575`），Globex 白名单是轻量补充，可直接参照 `taobao.py` 既有 `_is_allowed_redirect_url` 的白名单风格。

---

## 跨主题可借鉴模式（`[F]`）

1. **给模型看小、给工具看稳**：工具出参字段少而稳 + 嵌套属性按需展开（`11 §5.1`）。淘宝搜索结果透传 `ShoppingOffer` 稳定结构即可。
2. **工具间单向数据流、上游剪枝下游消费**（`12 §6.2`）：搜索只产出快照，排序/展示由上层消费。
3. **硬约束剔除 + 软偏好加权 + 结果截断**（`14 §2.1`）：推荐排序两段式，rejected_brief 有界。
4. **只读工具白名单 + 阶段过滤，隐藏优于拒绝**（`17-4`、`16-6 §2.1`）。
5. **偏好结构化条目 + 黑名单规则过滤**（`06 §3-4`），无需向量库即可落地。

## 不能复用清单 `[X]`

- 代码本体：Globex 是教程文档，代码片段 import `app.*` 包在该目录不存在，无法复制执行。
- 三塔 embedding + Faiss/OpenSearch 向量基础设施（`04-0`、`13`）：超出最小搜索范围，且与 PostgreSQL 事实源冲突。
- 汇率/关税/运费硬编码表（`12 §3.3,§4.3` 的 `FX_RATES`/`DUTY_TABLE`/`SHIPPING_TABLE`）：跨境业务规则，淘宝国内 affiliate 不适用；且是静态示意值。
- 多用户分层（user_tier）与 K8s 灰度/熔断部署（`17-4 §6`、`16-6 §3-4`、`16-5`）：单用户 local-first + compose 部署，不适用。
- 购物车/下单/支付相关：Globex 本身没有（grep 无"购物车/加购/详情页"实现），且本范围明确不做购买与 PRODUCT_REDIRECT。

## 结论

Globex 可借鉴的集中在**搜索工具签名、双通道结果合并、精挑评分、偏好 Store、只读工具阶段化过滤**五个只读模式；商品详情与购物车 Globex 无实现，无可借鉴。所有借鉴均为"模式/算法/展示逻辑"层面，无直接可复用代码；基础设施选型（向量库、Redis、checkpoint）与 Aidison 的 PostgreSQL 事实源边界冲突，一律不采纳。落地优先级建议：① Candidate/搜索出参截断信号 ② 硬约束+软偏好排序 ③ 偏好黑名单过滤 ④ 只读工具白名单。
