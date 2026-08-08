# Aidison 实际应用与生产可用性差距审计

日期：2026-08-05  
基线：`main@a82c0d0`  
范围：当前仓库、Compose 交付、V0 工程闭环、V1 Shopping；不实施修改。

## 结论

- **个人本机、单用户工程助手：有限可用。** V0 项目、需求、研究、决策、方案、反馈链路有真实 PostgreSQL 状态、Worker、幂等和浏览器控制台；配置真实模型与 Tavily 后可以作为受控 pilot 使用。
- **真实 Shopify 采购助手：当前不能开箱即用。** Shopify GraphQL 适配器存在，但标准 `aidison.api.app:app` 和 `compose.yaml` 没有实例化它，标准启动结果是 `provider=none, available=false`。
- **公开网络生产服务/SaaS：不具备上线条件。** API 没有用户认证、授权或租户隔离，Compose 暴露 PostgreSQL 和 API/Web 端口，缺少 TLS/反向代理、限流和生产级监控。

## 已具备的真实能力

1. PostgreSQL canonical state、Alembic 空库迁移、乐观并发、命令幂等、事件游标与 SSE。
2. OfferSnapshot、PurchaseProposal、人工确认、CheckoutHandoff 的完整持久化模型。
3. Checkout 外部副作用前先提交 `PREPARED` 和 command receipt，重放不会重复调用 provider。
4. Shopify Storefront GraphQL 已实现单店商品查询、库存/价格解析、`cartCreate` 和托管 `checkoutUrl`。
5. Provider 响应有 HTTPS、超时、结果上限、JSON MIME 和 2 MiB 响应限制。
6. Compose 包含 PostgreSQL、迁移、API、Worker、Web 健康检查；仓库已有 Artifact 完整性、备份和隔离恢复能力。

## 阻断真实使用的问题（P0）

### P0-1 标准启动没有 Shopping provider

- `create_app(..., shopping_provider=None)`，模块级 `app = create_app()`。
- `compose.yaml` 与 `.env.example` 没有 Shopify domain/token/version 配置，也没有 provider 工厂。
- 结果：普通 `docker compose up` 中 Shopping API 固定返回 `503 shopping_unavailable`。

验收门槛：增加经过校验的 Shopping settings/provider factory；标准启动能通过环境变量选择 `none` 或 `shopify`，`/api/integration-health` 显示真实配置状态且不泄露 token。

### P0-2 Shopify API 版本已退休

- 代码固定 `_STOREFRONT_API_VERSION = "2025-07"`。
- Shopify 官方版本表显示 `2025-07` 于 2026-07-16 停止可访问；当前稳定版本为 `2026-07`。退休版本可能被 fall-forward，不能作为可预测生产契约。

验收门槛：升级并显式配置受支持版本，检查响应 `X-Shopify-API-Version`，锁定查询契约测试。

### P0-3 没有真实 Shopify 集成验证

- 现有 Shopify 测试只有 MockTransport 边界测试，没有 development store 的搜索与 cartCreate live gate。
- `not_checked`：真实 token 权限、字段兼容性、国际定价、库存行为、cart warnings、checkout URL。

验收门槛：对 Shopify development store 执行只创建 cart、不付款的 live smoke；凭据只在运行时注入。

### P0-4 报价时效和总价保护不足

- Shopify offer 的 `expires_at=None`，因此快照不会因时间自动失效。
- Checkout 前不重新读取 variant 价格/库存；真实价格可能已变化。
- 后端 `max_total` 只比较 `unit_price * quantity`，未把 shipping/tax 纳入；Shopify adapter 本身也不给 shipping/tax estimate。
- 前端自动把上限设为商品小计的 2 倍，且 region 固定为 `CN`。

验收门槛：定义报价 TTL；handoff 前重新验证商品/库存；明确总价语义并覆盖运输、税费或标记为“结账页最终确认”；预算和地区由用户显式选择。

### P0-5 Checkout 崩溃恢复缺少闭环

- 安全策略避免重复创建 cart，但进程在 provider 成功后、最终状态提交前崩溃时，重放只返回 `PREPARED`。
- `AMBIGUOUS`/`PREPARED` 没有管理员 reconciliation、超时恢复或 provider cart 查询闭环。

验收门槛：提供可审计 reconciliation 流程；能够确认既有 cart、标记失败或由人工安全重试，同时保证不重复副作用。

### P0-6 网络暴露前没有身份边界

- 仓库 API 路由没有认证/授权；项目 ID 即访问能力。
- 没有用户、角色或租户字段。
- Compose 默认发布 API、Web、PostgreSQL 端口，数据库密码有开发默认值。

验收门槛：个人本机模式必须只绑定 loopback 且不公开 PostgreSQL；任何局域网/公网部署必须增加认证、项目级授权、TLS、秘密管理和限流。

## 重要但可后置的问题（P1）

1. 当前只支持**一个 Shopify 店铺**，不是全网商品搜索或多卖家比价平台。
2. 一个 PurchaseProposal/CheckoutHandoff 只携带一个 offer；多 BOM 行不会汇总成一个跨行购物车。
3. 搜索只是用 BOM 名称查询商品，兼容性判断仍需用户；没有规格参数归一化或自动等价验证。
4. `/health` 只返回静态 `ok`；integration health 只看配置是否存在，不探测 DB/provider 实际可达性。
5. 缺少 Shopping 请求指标、失败率、延迟、PREPARED/AMBIGUOUS 告警和审计管理界面。
6. 没有项目删除、数据保留和用户数据导出策略；若公开服务需要补齐隐私与运营规则。
7. 真实研究链路依赖模型、Tavily 和 Worker，仍需做长期运行、配额、成本和供应商故障演练。

## 推荐落地顺序

1. `production-shopping-wiring`：配置模型、provider factory、受支持 API version、秘密注入。
2. `shopify-live-contract`：development store 搜索/cart smoke 和版本响应校验。
3. `offer-revalidation-budget`：TTL、结账前复核、真实总价/地区预算规则。
4. `checkout-reconciliation`：PREPARED/AMBIGUOUS 恢复与人工运维界面。
5. `single-user-secure-deploy`：loopback/TLS、认证、关闭数据库公网端口、限流和监控。
6. 再决定产品边界：单店采购助手，还是多商店/多平台 BOM 比价器；后者是明显更大的产品和合规范围。

## 证据索引

- `src/aidison/api/app.py`：provider 注入、integration health、模块级生产 app。
- `src/aidison/providers/shopify.py`：API version、单店查询、cartCreate、报价字段。
- `src/aidison/providers/shopping.py`：V1 明确不处理订单、取消、退款或支付。
- `src/aidison/application/shopping.py`：proposal 上限校验、handoff 幂等与 crash 行为。
- `web/src/app/components/ShoppingView.tsx`：固定 CN、自动 max_total、单 offer proposal。
- `compose.yaml`、`.env.example`：当前生产入口和缺失的 Shopify wiring。
- Shopify API versioning：<https://shopify.dev/docs/api/usage/versioning>
- Shopify Storefront API 2026-04 reference：<https://shopify.dev/docs/api/storefront/2026-04>
- Shopify cartCreate payload：<https://shopify.dev/docs/api/storefront/latest/payloads/cartcreatepayload>

