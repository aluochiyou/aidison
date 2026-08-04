# Mai Event Schema

Each event uses schema version `1` and includes:

| Field | Type | Limit | Meaning |
| --- | --- | --- | --- |
| `id` | string | unique | Random for manual events; deterministic for scans |
| `type` | string | 60 | `project_scanned`, `module_discovered`, `feature_started`, `feature_completed`, `tests_passed`, `tests_failed`, `blocked`, `git_milestone`, or a precise compatible value |
| `title` | string | 140 | One-line result |
| `detail` | string | 900 | One to three factual sentences with implementation specifics |
| `status` | string | 40 | `planned`, `in_progress`, `passed`, `failed`, `blocked`, or `completed` |
| `module` | string | 160 | Stable module or responsibility boundary |
| `feature` | string | 160 | User-facing feature name |
| `files` | string[] | 24 paths | Relevant paths relative to project root |
| `evidence` | string[] | 12 items | Tests, commands, commits, or documents that prove the result |
| `tags` | string[] | 12 items | Optional filtering labels |
| `timestamp` | ISO string | - | Event creation time |

Do not include source code, secrets, credentials, chain-of-thought, or unverified conclusions. The receiver may display every field directly to the user.
