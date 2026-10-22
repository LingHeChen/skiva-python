import sqlite3
import subprocess
import sys

import httpx
import pytest

from skiva_sdk import LimitExceeded, ProtocolError, SkivaClient, SnapshotBusy, SQLiteBindingStore
from skiva_sdk.protocol import SnapshotRef


def make_client(distribution, store):
    return SkivaClient(
        "https://skiva.test",
        "test-secret",
        binding_store=store,
        transport=httpx.MockTransport(distribution),
    )


async def test_persistent_binding_restart_and_latest(distribution, tmp_path):
    path = tmp_path / "bindings.sqlite"
    store = SQLiteBindingStore(path, namespace="app/profile")
    async with make_client(distribution, store) as client:
        async with client.snapshot("job") as view:
            ref = view.ref
    distribution.head = "B"
    # Read in an actual separate process; no inherited in-memory binding.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from skiva_sdk import SQLiteBindingStore; "
            "import sys; print(SQLiteBindingStore(sys.argv[1], namespace='app/profile')"
            ".get('https://skiva.test\\njob').snapshot_id)",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "A"
    async with make_client(
        distribution, SQLiteBindingStore(path, namespace="app/profile")
    ) as client:
        async with client.snapshot("job") as retry:
            assert retry.ref == ref
        async with client.snapshot("job", retry_policy="latest") as updated:
            assert updated.ref.snapshot_id == "B"
        assert client.forget_snapshot("job")
        assert not client.forget_snapshot("job")


def test_store_cas_capacity_and_namespaces(tmp_path):
    path = tmp_path / "bindings.sqlite"
    first = SQLiteBindingStore(path, namespace="one")
    other = SQLiteBindingStore(path, namespace="one")
    isolated = SQLiteBindingStore(path, namespace="two")
    a = SnapshotRef("server", "profile", "A", "digestA")
    b = SnapshotRef("server", "profile", "B", "digestB")
    first.put("id", a, expected=None, limit=1)
    with pytest.raises(SnapshotBusy):
        other.put("id", b, expected=None, limit=1)
    assert other.get("id") == a
    assert isolated.get("id") is None
    with pytest.raises(LimitExceeded):
        other.put("other", b, expected=None, limit=1)
    with pytest.raises(SnapshotBusy):
        other.delete("id", expected=b)
    other.put("id", b, expected=a, limit=1)
    assert first.get("id") == b
    assert first.delete("id", expected=b)


def test_invalid_store_and_corrupt_ref(tmp_path):
    with pytest.raises(ValueError):
        SQLiteBindingStore(tmp_path / "x", namespace="")
    with pytest.raises(ValueError):
        SQLiteBindingStore(":memory:", namespace="a")
    path = tmp_path / "x"
    store = SQLiteBindingStore(path, namespace="a")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO bindings VALUES('a','id','{}')")
    with pytest.raises(ProtocolError):
        store.get("id")
    with sqlite3.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        ref = SnapshotRef("s", "p", "a", "d")
        with pytest.raises(SnapshotBusy):
            store.put("id", ref, expected=None, limit=1)
        with pytest.raises(SnapshotBusy):
            store.delete("id", expected=None)


async def test_conditional_manifest_still_checks_authorization(distribution):
    checks = []
    denied = False

    async def handler(request):
        response = await distribution(request)
        if request.url.path.endswith("/manifest"):
            checks.append(request.headers.get("if-none-match"))
            if denied:
                return httpx.Response(403, json={"error": {"code": "SNAPSHOT_FORBIDDEN"}})
            tag = '"' + distribution.head + '"'
            if request.headers.get("if-none-match") == tag:
                return httpx.Response(304)
            response.headers["ETag"] = tag
        return response

    from skiva_sdk import AuthorizationDenied

    async with SkivaClient(
        "https://skiva.test", "test-secret", transport=httpx.MockTransport(handler)
    ) as client:
        first = await client._manifest()
        assert await client._manifest() == first
        assert checks == [None, '"A"']
        distribution.head = "B"
        assert (await client._manifest()).snapshot_id == "B"
        denied = True
        with pytest.raises(AuthorizationDenied):
            await client._manifest()


async def test_transient_503_does_not_revoke_client(distribution):
    unavailable = True

    async def handler(request):
        if unavailable:
            return httpx.Response(503, json={"error": {"code": "DISTRIBUTION_UNAVAILABLE"}})
        return await distribution(request)

    from skiva_sdk import TransportError

    async with SkivaClient(
        "https://skiva.test", "test-secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(TransportError):
            async with client.snapshot():
                pass
        unavailable = False
        async with client.snapshot() as view:
            assert view.ref.snapshot_id == "A"
