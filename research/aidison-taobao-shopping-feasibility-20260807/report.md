# Aidison 淘宝购物接入可行性与选型

日期：2026-08-07

## 结论

推荐第一版采用：

> 淘宝开放平台/淘宝联盟官方 API 做商品发现与导购链接生成，Aidison 做需求理解、筛选、排序、报价快照、用户确认和审计，最终跳转淘宝 App/网页，由用户核对地址、实时价格、优惠与库存并完成支付。

不推荐第一版直接采用第三方淘宝 MCP，也不推荐自动化消费者购物车、订单或支付。

淘宝官方确实已经提供 MCP 体系，但公开文档展示的是“淘宝开放平台 Agent 应用中添加官方/自建/已申请 MCP”的平台内模式；当前公开资料没有给出一个可由 Aidison 直接连接、且明确包含淘宝商品搜索与消费者购买能力的标准 MCP endpoint/tool catalog。因此，官方 MCP 适合作为后续受控 spike，而不是当前生产主路径。

## Globex 教程有没有用

有用，但用途是产品搜索逻辑与算法设计，不是淘宝连接器。

可复用部分：

- 自然语言需求拆解：预算、材质、品牌、负向约束、数量和平台偏好。
- 统一候选商品结构、结果上限、过滤、同款归一、排序和推荐理由。
- 多平台并行搜索、部分失败降级、超时与熔断思路。
- 到手价展示、价格比较、搜索与排序评测方法。

不能复用或不能作为完成证据的部分：

- 教程平台是 Amazon、Shopee、AliExpress、eBay，不包含淘宝适配器。
- 教程明确说明真实平台 API、OAuth 和反爬接入超出课程范围，示例只是统一 `SearchClient` 抽象。
- 教程明确不覆盖真实账号授权、下单、支付、物流追踪和实时库存闭环。
- 参考目录只有 Markdown 教程，没有可执行项目、依赖清单或淘宝生产代码。

因此，Globex 的参考级别应是：

| 对象 | 参考程度 | 结论 |
|---|---:|---|
| 搜索意图与候选排序算法 | 中高 | 选择性重写到 Aidison |
| 搜索工具 contract 与并行 fan-out 思路 | 中 | 适配 Aidison durable runtime |
| 淘宝 API/MCP 接入代码 | 无 | 必须依据淘宝官方接口重写 |
| 下单、支付、订单闭环 | 无 | 教程没有实现 |

本地证据：

- `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/工具设计篇/11 ItemSearch商品检索工具实现与跨平台fork触发场景.md`
- `/home/aluo/project/cankao_ws/完整学习项目/globex电商采购助手/多Agent篇/教程导读.md`

## 淘宝官方 API 能做到什么

### 商品搜索：可行

当前适合作为候选基线的是 `taobao.tbk.dg.material.optional.upgrade`，即“淘宝客-推广者-物料搜索升级版”。官方文档显示：

- 接口用于通用导购物料搜索。
- 不需要终端淘宝用户授权，但仍需要 TOP 应用的 `app_key`、请求签名和获批的应用 scope。
- 必填 `adzone_id`，来自淘宝联盟推广位。
- 可按关键词、价格、是否天猫、店铺 DSR、优惠券、所在地等条件搜索，并限制页大小。
- 返回结果示例包含商品 ID、标题、图片、类目、店铺、销量、原价、折后价、预估到手价、邮费、优惠/推广链接等。

官方文档：[物料搜索升级版](https://open.taobao.com/api.htm?docId=64759&docType=2)

重要边界：这是淘宝联盟导购接口，不是“匿名查询淘宝全部商品并替消费者交易”的通用买家 API。部分商品 ID、单商品标题搜索和消费者比价场景还受 `biz_scene_id=2` 等权限约束。

### 跳转购买：可行

国内淘宝客 API `taobao.tbk.tpwd.create` 可以把联盟官方渠道生成的推广链接转换成淘口令；它不需要终端用户授权，但需要对应的淘宝客应用 scope。Aidison 也可以直接返回官方 HTTPS 推广/商品链接，让用户在淘宝端继续购买。

官方文档：[淘宝客-公用-淘口令生成](https://open.taobao.com/api.htm?docId=31127&docType=2)

### 通用消费者加购、下单、支付：当前不成立

官方文档搜索到的订单创建接口主要是商家、分销、采购或垂直业务接口，不是一个允许第三方助手替任意消费者购买任意淘宝商品的通用 API。

淘宝官方 FAQ 对“加入购物车”权限也给出了明确的场景限制：组合购等合理导购场景可申请；非消费者主动意愿的引导加购被禁止。这不足以支撑 Aidison 当前通用消费者助手的 `create_cart` 语义。

- [商家应用如何申请加入购物车权限](https://open.taobao.com/doc.htm?docId=5594&docType=14)
- [加入购物车权限为何申请不通过](https://open.taobao.com/doc.htm?docId=3818&docType=14)

所以第一版必须把交易边界停在“用户确认后跳转淘宝”，不能宣称已加购、已下单或已支付。

## 淘宝 MCP 能不能用

### 官方 MCP：存在，但当前不优先

淘宝开放平台官方文档确认：

- 在淘宝的 Agent 应用中，可以添加“官方”“我的”“已申请的”MCP 服务。
- 自建 MCP 服务需要填写输入输出、调试、发布并进入审核。
- 淘宝开放平台 MCP 已按 AppKey 和调用量收费；官方公告列出基础、增值、客服、质检等 MCP 类型。

官方资料：

- [调用 MCP 服务](https://open.taobao.com/doc.htm?docId=122221&docType=1)
- [创建 MCP 服务](https://open.taobao.com/doc.htm?docId=122222&docType=1)
- [MCP 服务收费启动公告](https://open.taobao.com/doc.htm?docId=25809&docType=12)

公开文档没有证明以下事项：

- Aidison 能否直接获得标准 Streamable HTTP/SSE MCP endpoint。
- 当前账号可见的官方 MCP catalog 是否有商品搜索、商品详情、推广链接或消费者购买工具。
- 这些工具的输入输出、权限、配额、数据完整性和允许使用场景。

这些信息必须登录淘宝开放平台控制台、创建合适类型的应用后才能核验。未经控制台核验，不能把“淘宝有 MCP”推导为“Aidison 可直接接淘宝购物 MCP”。

### 第三方 MCP：不作为生产依赖

社区淘宝 MCP 常见实现可能依赖网页抓取、浏览器 Cookie、非公开接口或 UI 自动化。除非能逐项证明官方授权、稳定 schema、凭据隔离、速率限制、审计、隐私与交易幂等，否则不应接触用户淘宝登录态，更不应执行加购、下单或支付。

Aidison 已依赖 `mcp>=1.24,<2` 和 `langchain-mcp-adapters==0.3.1`，技术上具备连接标准 MCP 的基础；缺少的不是 MCP client，而是一个经过淘宝官方确认、适合本业务且可外部调用的服务 contract。

## API 与 MCP 选型对比

| 维度 | 淘宝官方 API | 淘宝官方 MCP | 第三方 MCP |
|---|---|---|---|
| 官方性 | 已确认 | 已确认平台能力 | 不确定 |
| 商品搜索 contract | 文档明确 | 公开 catalog 未确认 | 实现各异 |
| Aidison 直接接入 | 标准 HTTPS TOP API，可行 | endpoint/外部调用方式未确认 | 通常可接，但风险高 |
| 权限与审核 | 应用 scope + 淘宝联盟推广位 | 场景默认/申请审核 | 常缺官方授权 |
| 数据可控性 | 字段和签名规则明确 | 工具 schema 待控制台核验 | 容易漂移 |
| 加购/下单/支付 | 无通用消费者闭环 | 未证明具备 | 不应信任 |
| 推荐 | **第一阶段主路径** | 第二阶段受控 spike | 不采用生产路径 |

## 与 Aidison 现有 Shopping 的适配缺口

现有代码已经具备值得保留的部分：

- `OfferSnapshot`：冻结观察时的商品、价格、来源和时间。
- `PurchaseProposal`：绑定方案、报价、数量、区域、预算上限和用户确认。
- `CheckoutHandoff`：外部副作用前先落 `PREPARED`，带幂等 command receipt。
- `ShoppingProvider`：严格超时、结果上限、HTTPS、错误净化和 provider 隔离。

但当前 contract 是为 Shopify 单店 `cartCreate` 设计的，淘宝不能原样套用：

1. `ShoppingProvider.create_cart()` 假设 provider 能创建托管购物车；淘宝导购 API只提供商品/推广链接。
2. `CheckoutHandoff` 的 `DISPATCHED/SUCCEEDED` 强制要求 `provider_cart_id`，淘宝跳转没有真实 cart ID；不能用商品 ID 冒充 cart ID。
3. `create_purchase_proposal()` 只接受 `IN_STOCK` 且要求精确 `quantity_available`；物料搜索结果没有可靠的实时 SKU 库存。
4. 淘宝“预估到手价”受会员身份、优惠资格、活动、地区和时间影响，不能作为最终成交价承诺。
5. 邮费可能依赖请求 IP，搜索时只能作为估算；最终以淘宝结算页为准。

建议做最小 contract 扩展：

- 增加 provider capability，例如 `SEARCH`、`CART_HANDOFF`、`PRODUCT_REDIRECT`。
- 把 `create_cart` 上移为 `create_handoff`，返回 `handoff_kind`、`checkout_url`、可选 provider reference；Shopify 实现 cart handoff，淘宝实现 product/promotion redirect。
- 淘宝报价允许 `availability=UNKNOWN`，但只能进入“需在淘宝复核”的 redirect proposal，不能声称已锁库存。
- 对预估到手价设置短 TTL，并保留原价、优惠路径和价格说明；最终确认页明确展示“淘宝结算页为准”。
- 模型只能调用 Aidison 的窄工具；TOP 签名、AppSecret、响应校验和字段归一全部留在 adapter 内。

建议链路：

```text
用户需求
  -> Aidison 需求拆解/Globex 风格约束提取
  -> TaobaoAffiliateAdapter.search_offers
  -> OfferSnapshot + 过滤/排序/推荐理由
  -> PurchaseProposal + 用户逐项确认
  -> PRODUCT_REDIRECT handoff
  -> 淘宝 App/网页核价、选 SKU/地址、支付
```

## 用户侧需要准备什么

在实施真实调用前，需要用户完成一次淘宝侧账号与权限操作：

1. 注册/登录淘宝开放平台开发者账号并创建符合导购场景的应用。
2. 开通淘宝联盟推广者身份与推广位，取得 `adzone_id`。
3. 申请“淘宝客【推广者】商品物料获取”scope，确认应用允许调用升级版物料搜索。
4. 在官方 API 测试工具中，用一个普通关键词验证有结果并能返回合法推广链接。
5. 如果要评估官方 MCP：在 Agent 应用的 MCP 页面查看当前账号实际可见的官方服务，并提供服务名称、工具列表、授权方式和 endpoint/调用文档；不要提供登录 Cookie 或明文 Secret。

建议配置边界：

- `.env`：`TAOBAO_APP_KEY`、`TAOBAO_APP_SECRET` 等凭据。
- `config.yaml`：`shopping.provider: taobao`、`adzone_id`、`material_id`、endpoint、timeout、max results、报价 TTL 等非秘密配置。

## 实施顺序

### P0：官方 API 只读 spike

- 只实现签名后的关键词搜索和响应归一。
- 不写购物车、不下单、不支付。
- 验证 scope、限流、错误码、价格/链接字段和数据稳定性。

### P1：Aidison 搜索与用户跳转闭环

- 新增 `TaobaoAffiliateAdapter`。
- 扩展 capability-aware handoff contract。
- 接入 OfferSnapshot、排序、Proposal 和 PRODUCT_REDIRECT。
- 增加 fixture contract test、录制响应回归测试与真实只读 smoke。

### P2：官方 MCP 受控对比

只有在控制台确认存在合适的官方购物 MCP 和外部调用 contract 后，才做同样查询的 A/B spike，比较覆盖率、字段质量、延迟、费用、可观测性和权限边界。若 MCP 只是淘宝托管 Agent 的内部工具，则不引入第二套 Agent 调度器，继续使用直接 API。

## 验证状态

| 检查 | 状态 | 说明 |
|---|---|---|
| Globex 教程范围与缺口 | passed | 本地逐文件检索并核对相关章节 |
| 淘宝物料搜索 API contract | passed | 官方文档、scope、参数和响应样例已核对 |
| 国内淘口令 handoff | passed | 官方 API 文档已核对 |
| 通用消费者 cart/order/payment API | failed | 未发现可支撑当前 contract 的通用官方能力；官方 FAQ 显示加购权限受具体场景约束 |
| 淘宝官方 MCP 存在性 | passed | 官方调用、创建和收费文档已核对 |
| 官方 MCP 可由 Aidison 直接连接 | not_checked | 公开文档未提供适合本场景的 endpoint/tool catalog；需要用户控制台权限核验 |
| 真实 AppKey/adzone live 请求 | not_checked | 尚未配置淘宝应用和联盟权限，本轮没有调用真实业务 API |

