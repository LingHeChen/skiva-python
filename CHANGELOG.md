# Changelog

English · [简体中文](CHANGELOG.zh-CN.md)

## 0.3.0 — Unreleased

- Add optional SQLite binding persistence, atomic publication conflict checks and bounded conditional manifest caching.
- Add client-local named snapshot bindings: `snapshot(id)`, `task(id)` alias and explicit cleanup.
- Reuse bindings across retries; opt into latest on entry while keeping the whole view fixed.
- Reject conflicting concurrent bindings and bound retained IDs without implicit eviction.
- Pydantic AI accepts `SnapshotView` directly as dependencies.
- Dynamic access checks updates by default (`refresh_interval=0`, previously 5 seconds).

## 0.2.0 — 2026-10-22

- Behavior change: ordinary bindings now default to dynamic updates; opt into pinned explicitly.
- Independent retry policy: reuse the supplied snapshot by default, or resolve latest explicitly.
- Immutable retry checkpoints survive binding exit and preserve task/attempt/snapshot provenance.
- Explicit snapshot sources still select pinned mode; retries enforce current authorization.

## 0.1.0 — 2026-10-22

- Async distribution/v1 client with immutable manifests and SHA-256 verification.
- Pinned default and opt-in dynamic sessions with captured batch views.
- Versioned discovery, content envelopes and expected snapshot/catalog checks.
- Optional Pydantic AI 2.59.0 discovery instructions refreshed per model request.
- Online permission checks, bounded memory and temporary directory materialization.
- No durable task store, offline mode or automatic history cleanup.
- Expanded regression suite with three multi-file Skill cases in two versions, generated rollback sequences and multi-turn Agent scenarios.
- Fixed stale `last_refresh_error` after successful pinned refresh recovery.
