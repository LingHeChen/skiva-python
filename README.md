# Skiva Python SDK

English · [简体中文](https://github.com/LingHeChen/skiva-python/blob/main/README.zh-CN.md)

Async Python SDK for Skiva's `/distribution/v1` protocol. Python 3.11+. Package name: `skiva-sdk`; import: `skiva_sdk`. Version: 0.2.0.

[GitHub repository](https://github.com/LingHeChen/skiva-python) · [Skiva server](https://github.com/LingHeChen/skiva)

## Install

Install from PyPI:

```bash
python -m pip install skiva-sdk==0.2.0
# Optional framework adapter, tested against exactly Pydantic AI 2.59.0:
python -m pip install 'skiva-sdk[pydantic-ai]==0.2.0'
```

Use a **client credential assigned to an enabled Agent Profile**, not an admin/API session token. Use the server origin (or reverse-proxy prefix), without `/distribution/v1`. Use HTTPS outside a trusted local environment.

```bash
export SKIVA_SERVER_URL=http://localhost:8080
export SKIVA_CLIENT_TOKEN=your-client-credential
```

The profile must have a published snapshot and authorized Skills. SDK dynamic mode respects server-side profile pins; it does not bypass them.

## Discovery and loading

```python
import asyncio
from skiva_sdk import SkivaClient

async def main():
    async with SkivaClient.from_env() as client:
        async with client.bind(task_id="review-42") as skills:
            catalog = await skills.catalog()
            print(catalog.render())
            if catalog.skills:
                content = await skills.load_skill(
                    catalog.skills[0].name,
                    expected_snapshot_id=catalog.ref.snapshot_id,
                    expected_catalog_digest=catalog.catalog_digest,
                )
                print(content.render())

asyncio.run(main())
```

`Catalog` contains a `SnapshotRef`, immutable Skill metadata and `catalog_digest`. `SkillContent` contains the actual ref, catalog digest, manifest-relative path, file digest and text. Rendered envelopes escape XML markup. Names/descriptions still originate from published Skills: treat publishers as a trust boundary, not as trusted system-policy authors simply because text is escaped.

The discovery prompt tells the model to compare snapshot IDs for equality, not chronological order, and to reload content whose version differs. Rollback is handled the same way as forward updates. `catalog_digest` also changes when the authorized file set changes within one snapshot. This is a model-facing fallback, **not a guarantee that old instructions are forgotten**.

## Fixed and dynamic policies

```python
async with client.bind(consistency="dynamic") as skills:
    await skills.refresh()  # explicit reconciliation; raises on failure
    async with skills.snapshot() as batch:
        body = await batch.read_text("review/SKILL.md")
        rules = await batch.read_text("review/references/rules.md")
        original_ref = batch.ref
```

- Opt-in `pinned`: one snapshot and filtered manifest for the binding. Future channel changes do not change it.
- Default `dynamic`: reads/discovery reconcile on access after `refresh_interval` (default 5 seconds). `refresh()` forces reconciliation. No background daemon or SSE subscription.
- An expected-version load forces dynamic reconciliation even before the interval expires. Snapshot or catalog mismatch raises `VersionMismatch`; no silent substitution.
- A fully verified candidate replaces the active view atomically. Each operation captures one view; different operations can span versions. `snapshot()` captures one view for related operations, even while the enclosing dynamic session updates.
- Refresh failure aborts that operation and populates `last_refresh_error`; it never silently reports stale discovery as fresh. Later successful refresh clears the error. `transition_count` counts successful view changes.
- The client default can be overridden per binding. An explicit `source=SnapshotRef` selects pinned mode when consistency is omitted; explicitly combining it with dynamic is rejected. Reuse a ref for an explicit retry, subject to current server grants/permissions.
- `view.ref` in dynamic mode is a point-in-time observation. Use returned `SkillContent.ref` or `batch.ref` for provenance.

Task/attempt IDs are optional correlation fields and auto-generated if absent. Duplicate active IDs are rejected **within one client**. There is no durable attempt store, automatic crash resume or cross-process exactly-once guarantee. Persist refs in your application's own storage for explicit pinned retries. A ref does not grant access and cannot bypass a new credential's permissions.

## Retry policy is separate from update policy

Version 0.2.0 changes the client default from pinned to dynamic. Existing applications that need fixed ordinary bindings must pass `consistency="pinned"`. Retry behavior is independently configured with `retry_policy="reuse_snapshot"` (default) or `"latest"`, on the client or on `client.retry()`.

```python
async with client.bind(task_id="review-42") as skills:
    catalog = await skills.catalog()
    # Run your application/agent with this session.

checkpoint = skills.retry_checkpoint

# Default: the checkpoint snapshot stays fixed for the whole retry.
async with client.retry(checkpoint) as retry:
    print((await retry.catalog()).render())

# Opt in: resolve the current authorized target and follow the client policy.
async with client.retry(checkpoint, retry_policy="latest") as retry:
    print((await retry.catalog()).render())
```

`retry_checkpoint` is an immutable `RetryCheckpoint(task_id, attempt_id, snapshot)` containing the session's **last fully published view**, not necessarily its initial view. It is available after the binding exits, including when application code raises. Capture and keep it before further updates if you want that specific view. A failed/cancelled refresh does not advance it. A dynamic attempt may have used several versions: one checkpoint does not reproduce its full sequence of reads or model history. For an exact returned content/batch version, construct a checkpoint with that `SkillContent.ref` or `SnapshotView.ref` and the session's IDs.

`client.retry(checkpoint)` keeps the task ID, generates a new attempt ID, revalidates current permissions and fixes the checkpoint snapshot throughout the retry. It never falls back to latest on denied access or a changed manifest. `retry_policy="latest"` resolves the current authorized Profile target (including server pins) and then uses the client's consistency policy, dynamic by default. Each retry can produce its own checkpoint for another retry. The checkpoint records no secrets and grants no access; applications can persist it with `dataclasses.asdict` and reconstruct its nested `SnapshotRef` when loading.

Reusing a task ID with `bind()` does not infer a retry. The SDK does not automatically rerun business actions or retry framework tools; wrap the retry boundary you own. For an application failure, read `skills.retry_checkpoint` in the exception handler after `bind` exits, then explicitly call `client.retry`. A failure before the initial binding is prepared has no usable checkpoint. Choosing latest reloads SDK content, but does not erase historical messages; decide whether to start a fresh model run/context in the application.

## Pydantic AI integration

```python
from pydantic_ai import Agent
from pydantic_ai.capabilities import Caching
from skiva_sdk import SkillSession
from skiva_sdk.integrations.pydantic_ai import SkivaSkills

agent = Agent(
    model,  # Supply your configured Pydantic AI model/provider.
    name="skiva-agent",
    deps_type=SkillSession,
    capabilities=[Caching(), SkivaSkills()],
)

async with agent:
    async with client.bind(consistency="dynamic") as skills:
        result = await agent.run("Review this change", deps=skills)
```

`SkivaSkills` provides a named, dynamic discovery instruction part with **rewrite** semantics, evaluated before **every model request**. It explicitly refreshes the session at each request, bypassing the normal polling interval. It also registers `load_skill` and `read_skill_file`, requiring both expected version fields. Version mismatch/not-found become structured tool results; authorization/network failures abort rather than encourage stale fallback. It does not rewrite historical tool returns or erase derived conclusions. Provider wire placement of instructions depends on Pydantic AI and the provider.

Keep business dependencies with `SkivaSkills(get_session=lambda deps: deps.skills)`. Each run must receive its own session; the capability has no mutable run state. Attach one SkivaSkills capability per Agent (tool names are fixed). This is a dedicated discovery/tool integration, **not** a replacement for the native Harness `Skills` workspace backend. Shell/direct filesystem access is not intercepted. Configure application tracing in the host application; the SDK exports no prompts/tokens to an external telemetry service.

An active binding owns both discovery and reads. Bindings follow updates by default. An explicitly pinned binding, including a reuse-snapshot retry, intentionally advertises its fixed catalog. Instructions are not modified while a model request is in flight. Header changes can affect prompt caching.

## Real files when necessary

```python
async with skills.snapshot() as batch:
    async with batch.materialize() as directory:
        # Give this path to a framework or awaited subprocess.
        # The child process must finish before leaving this block.
        print(directory)
```

This exports a complete private copy into a temporary directory, preserving read/execute modes. It does not create a FUSE mount or writable hardlinks to shared objects. It rejects paths incompatible with supported platforms, including case-folding collisions. Cleanup occurs on exit. File modes are not sandbox isolation against the owning process. **Direct reads of exported files bypass SDK authorization; revocation cannot recall exported bytes.** New snapshots require new exports, never rewriting an old directory.

## Validation, authorization and limits

Every snapshot is fully downloaded and validated before publication: Go-compatible manifest canonicalization, SHA-256, sizes, paths and modes. Cache is in memory and private to each view/client; no shared disk/offline cache. Default limits: 10,000 files, 8 MiB per file/manifest, 64 MiB per snapshot, 256 KiB per SKILL.md parsing input. Concurrent/retained views multiply memory use; applications should limit concurrency and release references.

Every public read/catalog/export rechecks the explicit snapshot manifest online, even for cached bytes. Changed permissions invalidate a captured view; explicit credential revocation is sticky for that client instance. Network failure blocks the operation. No offline mode, transparent network retries, or model-task retries. An authorization race after the final check and previously delivered content cannot be undone. HTTP redirects are not followed; bearer tokens are not included in SDK error messages. Raw transport instrumentation can still expose credentials; configure logging accordingly. Environment proxy/CA settings are ignored by default (`trust_env=False`); opt in explicitly with `trust_env=True` if needed, and install `httpx[socks]` for SOCKS proxies. A custom HTTPX transport can supply application TLS/proxy settings.

Useful errors: `VersionMismatch`, `ManifestChanged`, `AuthorizationDenied`, `IntegrityError`, `ProtocolError`, `LimitExceeded`, `SkillNotFound`, `TransportError`, `ClosedError`.

## Development and verification

```bash
uv sync --extra dev --extra pydantic-ai
uv run pytest -q
uv run ruff check src tests examples
uv run python -m build
uv run python examples/offline_demo.py
```

Tests use a controlled distribution fixture and Pydantic's actual `FunctionModel` agent loop, without external model calls. They cover updates/rollback, same-snapshot permission changes, concurrent reads/runs, old-content provenance, corruption, export isolation, and Go-generated manifest fixtures. The offline demo requires no server or credentials. It is not a substitute for deployment acceptance against your Skiva server and selected model provider.

See [examples/](https://github.com/LingHeChen/skiva-python/tree/main/examples/) and [CHANGELOG.md](https://github.com/LingHeChen/skiva-python/blob/main/CHANGELOG.md). Official integration references: [Pydantic capabilities](https://pydantic.dev/docs/ai/capabilities/custom/), [Agent Skills implementation guide](https://agentskills.io/client-implementation/adding-skills-support).

Detailed scenarios and recorded results: [Testing guide](https://github.com/LingHeChen/skiva-python/blob/main/docs/TESTING.md).
