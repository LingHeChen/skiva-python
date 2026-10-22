import asyncio
from dataclasses import FrozenInstanceError, asdict, replace

import httpx
import pytest
from scenarios import ScenarioServer

from skiva_sdk import (
    AuthorizationDenied,
    IntegrityError,
    RetryCheckpoint,
    SkivaClient,
    SnapshotRef,
    VersionMismatch,
)


def client(server, **kwargs):
    return SkivaClient(
        "https://skiva.test", "test-secret", transport=httpx.MockTransport(server), **kwargs
    )


async def test_default_dynamic_and_explicit_pinned(distribution):
    async with client(distribution, refresh_interval=0) as sdk:
        assert sdk.consistency == "dynamic"
        assert sdk.retry_policy == "reuse_snapshot"
        async with sdk.bind() as live, sdk.bind(consistency="pinned") as fixed:
            distribution.head = "B"
            assert (await live.catalog()).ref.snapshot_id == "B"
            assert (await fixed.catalog()).ref.snapshot_id == "A"
            distribution.head = "A"
            assert (await live.catalog()).ref.snapshot_id == "A"


async def test_failure_checkpoint_survives_close_and_default_retry_is_fixed(distribution):
    async with client(distribution, refresh_interval=0) as sdk:
        with pytest.raises(RuntimeError, match="application failed"):
            async with sdk.bind(task_id="job", attempt_id="first") as skills:
                await skills.load_skill("review")
                raise RuntimeError("application failed")
        checkpoint = skills.retry_checkpoint
        distribution.head = "B"
        async with sdk.retry(checkpoint) as retry:
            assert retry.task_id == "job"
            assert retry.attempt_id != "first"
            assert retry.consistency == "pinned"
            assert (await retry.catalog()).ref.snapshot_id == "A"
            assert await retry.read_text("review/references/rules.md") == "old reference"
            assert not await retry.refresh()
        # Same task ID alone must not implicitly reuse a previous snapshot.
        async with sdk.bind(task_id="job") as fresh:
            assert (await fresh.catalog()).ref.snapshot_id == "B"


async def test_checkpoint_is_explicit_and_tracks_last_successful_view(distribution):
    async with client(distribution, refresh_interval=0) as sdk:
        async with sdk.bind() as skills:
            initial = skills.retry_checkpoint
            distribution.head = "B"
            await skills.refresh()
            final = skills.retry_checkpoint
            assert final.snapshot.snapshot_id == "B"
            assert initial.snapshot.snapshot_id == "A"
            with pytest.raises(FrozenInstanceError):
                initial.task_id = "changed"
        # A rollback does not alter the checkpoint chosen for a retry.
        distribution.head = "A"
        async with sdk.retry(final) as retry:
            assert (await retry.catalog()).ref.snapshot_id == "B"
        async with sdk.retry(initial) as retry:
            assert (await retry.catalog()).ref.snapshot_id == "A"


async def test_latest_retry_loads_new_text_and_follows_rollback(distribution):
    async with client(distribution, refresh_interval=0) as sdk:
        async with sdk.bind() as skills:
            old = skills.retry_checkpoint
        distribution.head = "B"
        async with sdk.retry(old, retry_policy="latest") as retry:
            assert retry.consistency == "dynamic"
            assert await retry.read_text("review/references/rules.md") == "new reference"
            distribution.head = "A"
            assert await retry.read_text("review/references/rules.md") == "old reference"


async def test_client_retry_policy_and_per_retry_override(distribution):
    async with client(distribution, retry_policy="latest", consistency="pinned") as sdk:
        async with sdk.bind() as skills:
            checkpoint = skills.retry_checkpoint
        distribution.head = "B"
        async with sdk.retry(checkpoint) as retry:
            assert retry.consistency == "pinned"
            assert retry.ref.snapshot_id == "B"
        async with sdk.retry(checkpoint, retry_policy="reuse_snapshot") as retry:
            assert retry.ref.snapshot_id == "A"


async def test_checkpoint_round_trip_across_clients(distribution):
    async with client(distribution) as sdk:
        async with sdk.bind() as skills:
            stored = asdict(skills.retry_checkpoint)
    restored = RetryCheckpoint(
        task_id=stored["task_id"],
        attempt_id=stored["attempt_id"],
        snapshot=SnapshotRef(**stored["snapshot"]),
    )
    distribution.head = "B"
    async with client(distribution) as other:
        async with other.retry(restored) as retry:
            assert retry.ref == restored.snapshot


async def test_retry_permission_changes_and_revocation_fail_closed():
    server = ScenarioServer()
    async with server.client() as sdk:
        async with sdk.bind() as skills:
            checkpoint = skills.retry_checkpoint
        server.allowed["all"] = {"release-review"}
        with pytest.raises(VersionMismatch):
            async with sdk.retry(checkpoint):
                pytest.fail("Changed permissions must not silently alter a reused view")
        async with sdk.retry(checkpoint, retry_policy="latest") as latest:
            assert {s.name for s in await latest.list_skills()} == {"release-review"}
        server.revoked.add("all")
        with pytest.raises(AuthorizationDenied):
            async with sdk.retry(checkpoint, retry_policy="latest"):
                pytest.fail("Revoked credentials cannot retry")


async def test_latest_retry_respects_profile_pin():
    server = ScenarioServer()
    async with server.client() as sdk:
        async with sdk.bind() as skills:
            checkpoint = skills.retry_checkpoint
        server.head = "release-2"
        server.pins["all"] = "release-1"
        async with sdk.retry(checkpoint, retry_policy="latest") as retry:
            assert retry.ref.snapshot_id == "release-1"


async def test_missing_old_grant_does_not_fall_back_to_latest(distribution):
    async with client(distribution) as sdk:
        async with sdk.bind() as skills:
            checkpoint = skills.retry_checkpoint
        distribution.head = "B"
        distribution.grants.clear()
        with pytest.raises(AuthorizationDenied):
            async with sdk.retry(checkpoint):
                pytest.fail("Must not fall back to a new snapshot")


async def test_failed_refresh_keeps_retry_checkpoint(distribution):
    async with client(distribution) as sdk:
        async with sdk.bind() as skills:
            checkpoint = skills.retry_checkpoint
            distribution.head = "B"
            distribution.corrupt = True
            with pytest.raises(IntegrityError):
                await skills.refresh()
            assert skills.retry_checkpoint == checkpoint


async def test_retry_validation_and_concurrent_attempt_isolation(distribution):
    async with client(distribution) as sdk:
        async with sdk.bind() as skills:
            checkpoint = skills.retry_checkpoint
        for kwargs in (
            {"attempt_id": checkpoint.attempt_id},
            {"attempt_id": ""},
            {"retry_policy": "unknown"},
        ):
            with pytest.raises(ValueError):
                async with sdk.retry(checkpoint, **kwargs):
                    pass
        with pytest.raises(TypeError):
            async with sdk.retry(checkpoint.snapshot):
                pass
        with pytest.raises(ValueError):
            async with sdk.retry(
                replace(
                    checkpoint, snapshot=replace(checkpoint.snapshot, server="https://other.test")
                ),
                retry_policy="latest",
            ):
                pass
        async with sdk.retry(checkpoint, attempt_id="second"):
            with pytest.raises(ValueError):
                async with sdk.retry(checkpoint, attempt_id="second"):
                    pass

        async def attempt():
            async with sdk.retry(checkpoint) as retry:
                return retry.attempt_id, (await retry.catalog()).ref

        results = await asyncio.gather(*(attempt() for _ in range(12)))
        assert len({r[0] for r in results}) == 12
        assert all(r[1] == checkpoint.snapshot for r in results)


def test_invalid_client_retry_policy(distribution):
    with pytest.raises(ValueError, match="retry policy"):
        client(distribution, retry_policy="unknown")


@pytest.mark.parametrize("field,value", [("task_id", ""), ("attempt_id", None)])
def test_checkpoint_rejects_missing_identity(field, value):
    kwargs = dict(task_id="task", attempt_id="attempt", snapshot=SnapshotRef("s", "p", "a", "d"))
    kwargs[field] = value
    with pytest.raises(ValueError):
        RetryCheckpoint(**kwargs)


def test_checkpoint_requires_snapshot_ref():
    with pytest.raises(TypeError):
        RetryCheckpoint("task", "attempt", {})
