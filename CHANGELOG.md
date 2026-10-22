# Changelog

English · [简体中文](CHANGELOG.zh-CN.md)

## 0.1.0 — local release candidate

- Async distribution/v1 client with immutable manifests and SHA-256 verification.
- Pinned default and opt-in dynamic sessions with captured batch views.
- Versioned discovery, content envelopes and expected snapshot/catalog checks.
- Optional Pydantic AI 2.59.0 discovery instructions refreshed per model request.
- Online permission checks, bounded memory and temporary directory materialization.
- No PyPI publication, durable task store, offline mode or automatic history cleanup.
- Expanded regression suite with three multi-file Skill cases in two versions, generated rollback sequences and multi-turn Agent scenarios.
- Fixed stale `last_refresh_error` after successful pinned refresh recovery.
