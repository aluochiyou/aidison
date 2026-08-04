# 实施验证矩阵

- Status: sealed specification
- Current result: all scenarios `not_checked`
- Artifact rule: 每次运行保存命令、环境/commit、原始输出、数据库断言与失败快照

| ID | Phase | Scenario / precondition | Fault or action | Pass oracle | Required artifact |
|---|---|---|---|---|---|
| V-001A | S0 quick gate | release SHA and main SHA + lockfiles | each fresh WSL2/Compose start + inventory/smoke | 两候选用同一命令/fixture；记录 blocker、耗时、现有失败测试、非 V0 依赖面 | per-candidate compose/log/test inventory |
| V-001B | S0 selected slice | current candidate in sequential quick-gate order；第一失败时对第二候选重复本行 | vertical slice + single domain-commit crash + provider request/stream smoke + Project-first proof + patch dry replay | slice canonical state 只在 Aidison；crash 不重复；两 provider smoke 可归类；UI 不读 raw message；replay 冲突有记录 | slice trace, DB rows, request snapshots, UI build, replay report |
| V-001C | S1–S3 | selected SHA + owned baseline | second independent WSL2/Compose build/start | web/api/worker/db healthy；migration exact；无手工容器修补 | compose config/logs, image digest, elapsed time |
| V-001D | S1–S3 | selected SHA | provider qualification subset + minimal Project-first production build + pruned non-V0 build + patch dry replay | text/stream/one tool/typed output 通过或 fail-closed；两个 build 通过；replay 人工处理不超过门槛 | contract output, build logs, pruned dependency report, replay report |
| V-002 | S1 | database + artifact backup | destroy dev volumes then restore | domain/checkpoint/event rows match；artifact hash audit 100%；missing/corrupt metadata explicit | backup manifest, restore log, hash report |
| V-003 | S2 | interactive command | repeat same command ID/payload; then same ID/different payload | first two return same receipt；different payload fail closed | receipt rows and HTTP responses |
| V-004 | S2 | approved SolutionVersion | attempt in-place update | update rejected；new version required；active pointer unchanged | DB constraint/test output |
| V-005 | S3 | claimed running Job | lease expires; old worker submits Domain command | transaction rejects old generation；no fact/receipt/outbox | jobs/attempts/facts query snapshot |
| V-006 | S3 | Domain command transaction | crash after fact+receipt+outbox, before checkpoint | rerun returns receipt；one fact；one domain event；projector emits one RunEvent；reconciler 形成预期 Job terminal 且只有一个 terminal event | fault trace and row counts |
| V-007 | S3 | external effect request | response lost after provider may accept | tool receipt becomes ambiguous；no auto retry；reconcile path required | attempt/receipt state history |
| V-008 | S3 | cancel running Job | late model/tool result and worker commit | new commit rejected unless receipt pre-existed；terminal never reverses | state transition/event trace |
| V-009 | S3 | Project SSE with retained events | 100 randomized disconnect/reconnects | monotonic project_seq；no semantic gaps/duplicates；disconnect never cancels Job | cursor/gap report |
| V-010 | S3 | cursor older than retention | reconnect with old Last-Event-ID | `stream.reset_required` + watermark；query snapshot source_event_seq matches resume cursor | HTTP/SSE transcript |
| V-011 | S4 | OpenAI and Bailian configured model | text/stream/tool/typed output/image/error fixtures | per-capability declared/live status accurate；unsupported fails closed | redacted request/event snapshots |
| V-012 | S4 | provider continuation expired/unavailable | replay on same/other provider | request rebuilt from canonical input；no foreign provider state ID | attempt lineage and request snapshot |
| V-013 | S4 | malicious/large source | private URL, redirect, prompt injection, oversize | SSRF/size/media guards work；source content never becomes instruction | security matrix output |
| V-014 | S5 | hard compatibility conflict or unknown | attempt freeze/activate | hard conflict blocks；unknown requires Decision/test；basis stale rejects approval | domain error and decision rows |
| V-015 | S5 | Observation affects one module subtree | generate ImpactAnalysis/Patch | affected closure correct；unaffected hashes unchanged；new immutable version | semantic Diff + DB hashes |
| V-016 | S6 | browser complete flow | requirements→research→decision→solution→observation→patch | UI only consumes DTO/query API；refresh/cache clear preserves truth | Playwright trace/screenshots |
| V-017 | S7 | three domain fixtures | run same graph/schema | no domain-specific core branch/entity；all mandatory artifacts produced | fixture report and graph route log |
| V-018 | release | 四旋翼无人机 real pilot | execute guidance and submit feedback | user can explain choices/evidence；one real Observation/Patch；safety issues recorded | pilot report, decisions, artifacts |
| V-019 | S3 | same parent attempt replays fan-out twice | create two shards twice | only two unique Delegation/child Jobs；duplicate call returns original IDs | delegation/job row counts |
| V-020 | S3 | one child lease expires and result arrives late | run join reconciler | stale result retained but rejected；canonical/proposal join unpolluted | result state + JoinReceipt |
| V-021 | S3 | parent cancel races child completion | inject both commit orders | no new join/Domain fact after cancel；terminal never reverses | transaction/event trace |
| V-022 | S3 | crash immediately before/after JoinReceipt | replay reconciler and parent | one JoinReceipt and one parent resume generation | join/job row counts |
| V-023 | S3 | transient child retry | retry same delegation | same delegation ID, new attempt；parent global budget not reset | attempt/budget ledger |
| V-024 | S3–S4 | Profile active pointer changes mid-run | resume old run and start new run | old run remains pinned；new run uses promoted revision | attempt/profile refs |
| V-025 | S2–S4 | Working/Episodic Proposal without Domain command | query canonical APIs | Proposal is absent until validated command + receipt | API/DB snapshot |
| V-026 | S2–S4 | explicit Preference revoke/new RequirementRevision | assemble context again | old preference no longer retrieved；inferred preference cannot be written | command/context manifest |
| V-027 | S3–S4 | bound Artifact becomes quarantined/missing | run outbox/projector | Evidence becomes stale/review_required；old hash/lineage retained | artifact/evidence history |
| V-028 | S4 | grader timeout/invalid/cheating mutation | attempt promotion | invalid evaluation cannot promote candidate | Evaluation/Promotion rows |
| V-029 | S4 | candidate vs baseline Profile | equal provider/tool/global budget, repeated seeds | report variance, safety regression, cost and latency；threshold predeclared | ablation artifact |
| V-030 | S3–S6 | SSE replay of multi-Agent run | disconnect/reconnect during fan-out/join | complete causal delegation/join chain；no Prompt/retrieved content/secret leak | SSE transcript/redaction audit |
| V-031 | release | finish 四旋翼 pilot then run non-drone fixtures | compare graph/profile/schema and unaffected hashes | no drone-specific core branch/entity/Profile/tool route；unaffected fixture hashes stable | pilot + regression report |

## Artifact volume semantics

Artifact bytes 使用 content hash；metadata 的状态至少为 `present|missing|corrupt|quarantined|expired`。数据库备份必须配套 artifact manifest。恢复后逐项 hash audit；丢失字节不能被静默视为 Evidence present。Retention 删除先写状态/事件，再删除字节，并保留原 hash/size/media metadata。

## Phase gates

- Phase A/S0：V-001A..V-001B，并以 V-019..V-022 的最小 two-child/crash/stale/single-join 子集验证 adoption 可行性；
- S1–S3 qualification：V-001C..V-001D、V-002..V-010、V-019..V-027、V-030；
- S4–S6：V-011..V-016、V-024..V-030；
- V0 release：V-017、V-018 与 V-031。

跨阶段用例在首次适用阶段建立，在后续阶段以完整组件重跑；任何 phase gate 不得因同一 V-ID 已在早期 spike 运行而跳过正式 qualification。
