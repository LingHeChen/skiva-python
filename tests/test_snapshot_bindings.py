import asyncio

import httpx
import pytest

from skiva_sdk import (
    AuthorizationDenied,
    ClosedError,
    IntegrityError,
    LimitExceeded,
    SkivaClient,
    SnapshotBusy,
    VersionMismatch,
)


def client(server, **kwargs):
    return SkivaClient(
        "https://skiva.test", "test-secret", transport=httpx.MockTransport(server), **kwargs
    )


async def test_default_dynamic_checks_every_access(distribution):
    async with client(distribution) as sdk:
        assert sdk.refresh_interval == 0
        async with sdk.bind() as live:
            assert (await live.catalog()).ref.snapshot_id == "A"
            distribution.head = "B"
            assert await live.read_text("review/references/rules.md") == "new reference"
            distribution.head = "A"
            assert (await live.catalog()).ref.snapshot_id == "A"


async def test_named_snapshot_survives_application_failure_and_refreshes_explicitly(distribution):
    async with client(distribution, retry_policy="latest") as sdk:
        with pytest.raises(RuntimeError):
            async with sdk.snapshot("job") as fixed:
                distribution.head = "B"
                assert await fixed.read_text("review/references/rules.md") == "old reference"
                raise RuntimeError("application failure")
        with pytest.raises(ClosedError):
            await fixed.catalog()
        # Snapshot mode defaults to reuse, independently of low-level checkpoint retry default.
        async with sdk.snapshot("job") as retry:
            assert retry.ref == fixed.ref
        async with sdk.snapshot("job", retry_policy="latest") as refreshed:
            assert refreshed.ref.snapshot_id == "B"
            distribution.head = "A"
            assert await refreshed.read_text("review/references/rules.md") == "new reference"
        async with sdk.snapshot("job") as retry:
            assert retry.ref.snapshot_id == "B"
        async with sdk.snapshot("job", retry_policy="latest") as rollback:
            assert rollback.ref.snapshot_id == "A"


async def test_task_alias_and_other_ids_are_independent(distribution):
    async with client(distribution) as sdk:
        async with sdk.task(task_id="job") as a:
            distribution.head = "B"
            async with sdk.snapshot("other") as b:
                assert a.ref.snapshot_id == "A"
                assert b.ref.snapshot_id == "B"
        async with sdk.snapshot("job") as retry:
            assert retry.ref == a.ref
        async with sdk.task("job", retry_policy="latest") as latest:
            assert latest.ref == b.ref


async def test_sources_and_anonymous_snapshots_remain_compatible(distribution):
    async with client(distribution) as sdk:
        async with sdk.snapshot() as first:
            ref = first.ref
        distribution.head = "B"
        async with sdk.snapshot(ref) as positional, sdk.snapshot(source=ref) as keyword:
            assert positional.ref == keyword.ref == ref
        async with sdk.snapshot("restored", source=ref) as restored:
            assert restored.ref == ref
        async with sdk.snapshot() as anonymous:
            assert anonymous.ref.snapshot_id == "B"
            with pytest.raises(ValueError):
                async with sdk.snapshot("restored", source=anonymous.ref):
                    pass
    # No implicit durable/global map: a new client starts a new binding.
    async with client(distribution) as other:
        async with other.snapshot("restored") as fresh:
            assert fresh.ref.snapshot_id == "B"


async def test_same_id_busy_and_cleanup(distribution):
    async with client(distribution) as sdk:
        async with sdk.snapshot("job"):
            for policy in ("reuse_snapshot", "latest"):
                with pytest.raises(SnapshotBusy):
                    async with sdk.snapshot("job", retry_policy=policy):
                        pass
            with pytest.raises(SnapshotBusy):
                sdk.forget_snapshot("job")
        assert sdk.forget_snapshot("job")
        assert not sdk.forget_snapshot("job")
        distribution.head = "B"
        async with sdk.snapshot("job") as fresh:
            assert fresh.ref.snapshot_id == "B"


async def test_limit_does_not_silently_evict(distribution):
    async with client(distribution, max_snapshot_bindings=1) as sdk:
        async with sdk.snapshot("one"):
            pass
        with pytest.raises(LimitExceeded):
            async with sdk.snapshot("two"):
                pass
        distribution.head = "B"
        async with sdk.snapshot("one") as original:
            assert original.ref.snapshot_id == "A"
        sdk.forget_snapshot("one")
        async with sdk.snapshot("two") as fresh:
            assert fresh.ref.snapshot_id == "B"


async def test_cancelled_initial_prepare_releases_busy_and_capacity(distribution):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(request):
        if "/objects/" in request.url.path:
            entered.set()
            await release.wait()

    distribution.delay_hook = pause
    async with client(distribution, max_snapshot_bindings=1) as sdk:

        async def prepare():
            async with sdk.snapshot("one"):
                pytest.fail("Preparation should be cancelled")

        pending = asyncio.create_task(prepare())
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(SnapshotBusy):
            async with sdk.snapshot("one"):
                pass
        with pytest.raises(LimitExceeded):
            async with sdk.snapshot("two"):
                pass
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not sdk.forget_snapshot("one")
        distribution.delay_hook = None
        async with sdk.snapshot("two"):
            pass


async def test_failed_latest_preserves_original_binding(distribution):
    async with client(distribution) as sdk:
        async with sdk.snapshot("job") as original:
            ref = original.ref
        distribution.head = "B"
        distribution.corrupt = True
        with pytest.raises(IntegrityError):
            async with sdk.snapshot("job", retry_policy="latest"):
                pass
        distribution.corrupt = False
        async with sdk.snapshot("job") as retry:
            assert retry.ref == ref


async def test_permissions_and_revocation_are_not_bypassed(distribution):
    async with client(distribution) as sdk:
        async with sdk.snapshot("job"):
            pass
        distribution.filter_files("A", lambda p: False)
        with pytest.raises(VersionMismatch):
            async with sdk.snapshot("job"):
                pass
        async with sdk.snapshot("job", retry_policy="latest") as revised:
            assert not await revised.list_skills()
        distribution.revoked = True
        with pytest.raises(AuthorizationDenied):
            async with sdk.snapshot("job"):
                pass


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": ""},
        {"id": 42},
        {"id": "x" * 257},
        {"retry_policy": "invalid"},
        {"source": "server-snapshot-id"},
    ],
)
async def test_invalid_binding_inputs(distribution, kwargs):
    async with client(distribution) as sdk:
        with pytest.raises((ValueError, TypeError)):
            async with sdk.snapshot(**kwargs):
                pass


async def test_invalid_source_combinations(distribution):
    async with client(distribution) as sdk:
        async with sdk.snapshot() as view:
            ref = view.ref
        for kwargs in ({"id": ref, "source": ref}, {"source": ref, "retry_policy": "latest"}):
            with pytest.raises(ValueError):
                async with sdk.snapshot(**kwargs):
                    pass
        with pytest.raises(ValueError):
            sdk.forget_snapshot("")


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_binding_limit(distribution, limit):
    with pytest.raises(ValueError):
        client(distribution, max_snapshot_bindings=limit)
