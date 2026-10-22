# SDK scenario tests

English · [简体中文](TESTING.zh-CN.md)

## Run

```bash
uv sync --extra dev --extra pydantic-ai
uv run pytest -q --cov=skiva_sdk --cov-fail-under=90 \
  --cov-report=term-missing --cov-report=json:.test-results/coverage.json \
  --junitxml=.test-results/junit.xml
uv run ruff check src tests examples
```

Generated reports, coverage data and Hypothesis state are ignored. The GitHub Actions matrix runs Python 3.11/3.12/3.13, requires 90% combined statement/branch coverage, and preserves JUnit/coverage artifacts. Coverage is an automated regression gate, not a production-readiness certificate.

## Inspectable Skill cases

The [cases directory](../tests/cases/) contains 36 files across two releases of three synthetic Skills. These are deliberately simple business rules inside structurally realistic multi-file packages; they are not financial, security or deployment advice.

| Skill | Files and references | Expected version difference |
|---|---|---|
| `expense-audit` | Folded YAML description, policy/currency JSON, nested Chinese region reference, receipts, report template, Python calculator | The same receipts total 800. Cap 1000 approves in release-1; cap 600 rejects in release-2 |
| `release-review` | Checklist, nested security guide, rollout instructions, decision template | A breaking change changes from `warning` to `blocking` |
| `data-contract` | Versioned schema, CSV, NFC Unicode filename, binary asset, empty file | release-2 additionally requires `consent`; binary/empty files remain readable as bytes |

The calculator executes only on these authored fixtures, in a temporary exported directory, using an isolated Python interpreter. Tests await process termination before directory cleanup. No deployment or external side effects are executed.

## Verification layers

| Layer | Test files | What is established |
|---|---|---|
| Existing protocol/HTTP baseline | `test_sdk.py`, `test_http.py`, `test_protocol_fixture.py` | Core policies, socket HTTP, Go-generated canonicalization fixture |
| Complex releases | `test_complex_cases.py` | Nested reads, actual script results, rollback isolation, profile pins, credential grants, permission changes, cancellation and recovery |
| Adversarial input | `test_adversarial.py` | Invalid YAML/types/paths, symlink entries, malformed manifests/denials, streaming failure, oversized responses, all resource limits, unsafe export names |
| Generated sequences | `test_properties.py` | 40 deterministic Hypothesis examples, each 1–20 publish/rollback choices, compared with a release-state oracle |
| Actual Agent loop | `test_pydantic_ai.py`, `test_agent_scenarios.py` | Per-request catalog replacement, version errors, retained provenance, concurrent runs, old history, permission changes, streaming and revocation |

Concurrency checks include 24 simultaneous refreshes (exactly one successful transition) and 24 reads after publication. A 500-Skill/1,000-file catalog confirms shared-reference deduplication: 501 object downloads, not 1,000. These are correctness tests, not throughput or latency benchmarks.

The multi-turn Agent scenario performs eight model requests: load two Skills, change the release before a reference read, reject the stale read, reload, inspect the new policy, roll back, reject the formerly current reference version, and reload the rolled-back policy. Historical outputs from both snapshots remain labeled. The test does not pretend old model context is erased.

## Test infrastructure and limitations

`tests/scenarios.py` models immutable releases, credential-specific profiles, filtered manifests, historical grants and profile pins. It permits deterministic faults at await boundaries without sleep-based race assumptions. It is a test double, not the real Go service. The existing socket test checks the network transport against a local test HTTP server; the Go fixture checks serialization compatibility only.

Agent tests execute the real Pydantic AI 2.59.0 loop using `FunctionModel` with scripted decisions. No paid/external model calls, API secrets or real Skill data are needed. They verify adapter orchestration and tool results, not whether an actual LLM reliably obeys stale-version instructions. Agno/LangChain compatibility is not certified by these tests.

Remaining deployment acceptance: authenticated actual Skiva backend, real profile updates and grants, TLS/proxies, process/network failure under deployment load, a selected model provider, and sandbox/OS-specific behavior. macOS local test results do not prove Windows compatibility; Linux is configured in CI but is only executed after the workflow runs.

## Recorded local result

2026-10-22, macOS ARM64, Python 3.12.10: **102 passed**. Combined statement/branch coverage **95.40%** (523/542 statements, 120/132 branches). Python 3.13.5 also passed all 102 tests in a separate environment. The local Python 3.11 attempt was stopped while downloading dependencies; no 3.11 test result is claimed. The CI matrix retains that version. Catalog rendering/parsing has 100% measured combined coverage. These numbers describe this revision, not a permanent guarantee.

The expanded suite found a real defect: a pinned binding retained `last_refresh_error` after a later successful refresh. The fix clears the error and records the successful check time; the pinned/dynamic recovery regression test covers both policies.

## 0.2.0 retry policy regression

Python 3.12.10 local validation: **118 passed**, combined statement/branch coverage **95.44%**. `test_retry.py` covers dynamic defaults, fixed default retries, latest retries and rollback, client overrides, persisted checkpoints, authorization failures, failed refresh checkpoints, and concurrent attempt isolation. The Pydantic AI tests also verify that retry policy selects the model's discovery catalog. No live model calls are used.

## 0.3.0 named snapshot regression

Local Python 3.12: 138 tests passed; combined statement/branch coverage 95.78%. Added same-ID reuse, explicit latest, cancellation recovery, capacity limits, concurrent conflicts, permission changes and Pydantic AI discovery/tool calls using SnapshotView.

## Persistence and conditional requests

143 local tests passed; combined coverage 95.90%. Covers separate-process persistence, namespace isolation, atomic conflicts, capacity, SQLite lock contention, malformed references, 304 authorization and 503 recovery. Real Go/PostgreSQL/Python SDK integration also passed in the main repository; see its SDK_RUNTIME guide for commands.
