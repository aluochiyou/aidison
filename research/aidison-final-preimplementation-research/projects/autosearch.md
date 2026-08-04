# AutoSearch 拆解

- Evidence: imported from prior reports
- Role: source adapter and evidence discovery donor
- Runtime: AST/static checks only; integrated runtime `not_checked`

## 价值

AutoSearch 的 supplier/channel registry、typed errors、cooldown、URL 与内容去重、Evidence candidate 和可选 BM25 与 Aidison 的 SourceGateway 最接近。

## 融合方式

V0 只重写或小范围迁移 registry、错误归一、URL canonicalization、SimHash/内容去重。输出必须停在 `DiscoveryHit`/`EvidenceCandidate`，由 Aidison fetch、snapshot、span 和 audit 流水线晋升。

## 拒绝

不继承 Session/Evidence truth、整包依赖、隐式副作用、自动执行未知 skill 或渠道 secret 暴露。BM25 在真实排序基线前推迟。

## 验证

使用录制 fixture 测 timeout、429/cooldown、重复 URL、相似内容、partial fetch、来源隔离和敏感配置 redaction。

