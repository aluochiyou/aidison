# Mai Project Model v1

Mai stores durable project state in `.mai/project.json`. Mutable entities use a stable `id`; source documents use their relative `path` as the stable key. Update the existing key instead of appending another copy.

## Collections

| Collection | Required fields | Important optional fields |
| --- | --- | --- |
| `modules` | `id`, `name` | `path`, `kind`, `responsibility`, `dependencies`, `technologies`, `evidence`, `owner` |
| `workItems` | `id`, `title`, `status`, `priority`, `progress` | `phaseId`, `milestoneId`, `moduleId`, `assignee`, `startDate`, `dueDate`, `dependsOn`, `acceptanceCriteria`, `evidence` |
| `phases` | `id`, `name` | `status`, `startDate`, `endDate`, `description` |
| `milestones` | `id`, `title`, `status` | `dueDate`, `phaseId`, `progress`, `evidence` |
| `decisions` | `id`, `title` | `status`, `context`, `outcome`, `rationale`, `alternatives`, `moduleId`, `date`, `evidence` |
| `risks` | `id`, `title` | `status`, `probability`, `impact`, `mitigation`, `owner`, `moduleId`, `evidence` |
| `interfaces` | `id`, `name`, `type` | `moduleId`, `targetModuleId`, `method`, `path`, `signature`, `description`, `evidence` |
| `architectureNodes` | `id`, `label`, `type` | `moduleId`, `description`, `status`, `x`, `y` |
| `architectureEdges` | `id`, `source`, `target` | `type`, `label` |
| `sourceDocuments` | `path` | `hash`, `updatedAt`, `lastReadAt` |

## Enumerations

- Work item status: `todo`, `in_progress`, `review`, `done`, `blocked`
- Priority: `low`, `medium`, `high`, `critical`
- Risk status: `open`, `mitigating`, `closed`, `accepted`
- Probability: `low`, `medium`, `high`
- Impact: `low`, `medium`, `high`, `critical`
- Interface type: `http`, `event`, `function`, `database`, `file`, `other`
- Architecture node type: `service`, `database`, `api`, `client`, `queue`, `cache`, `external`
- Architecture edge type: `sync`, `async`, `data`, `auth`

Source documents track parsed process documents and other reference files. `path` is both the stable key and a path relative to project root; `hash` is a content hash for change detection; `updatedAt` and `lastReadAt` use ISO 8601.

When an ADR exists, add `adr:<relative-path>` to `decisions.evidence`; the ADR remains canonical. Mai stores only dashboard fields and verified evidence and must not become a second full decision record. Any new field must first be accepted by the active Mai API/model schema and validated by an actual upsert.

Dates use ISO 8601. `progress` is an integer from 0 through 100. Dependencies contain stable work-item IDs. Acceptance criteria are observable outcomes, and evidence records only facts that have actually been verified.
