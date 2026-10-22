import asyncio

import httpx
import pytest

from skiva_sdk import (
    AuthorizationDenied,
    ClosedError,
    IntegrityError,
    LimitExceeded,
    Limits,
    ManifestChanged,
    ProtocolError,
    SkillNotFound,
    SkivaClient,
    VersionMismatch,
)
from skiva_sdk.protocol import Manifest, canonical_json, digest


def client(server, **kwargs):
    return SkivaClient(
        "https://skiva.test", "test-secret", transport=httpx.MockTransport(server), **kwargs
    )


async def test_pinned_rollback_references_and_retry(distribution):
    async with client(distribution) as sdk:
        async with sdk.bind(task_id="t", attempt_id="1", consistency="pinned") as skills:
            a = await skills.load_skill("review")
            distribution.head = "B"
            assert await skills.read_text("review/references/rules.md") == "old reference"
            assert (await skills.catalog()).ref.snapshot_id == "A"
            assert not await skills.refresh()
        async with sdk.bind(source=a.ref) as retry:
            assert (await retry.catalog()).ref.snapshot_id == "A"
        async with sdk.bind() as fresh:
            assert (await fresh.catalog()).ref.snapshot_id == "B"
        distribution.head = "A"
        async with sdk.bind() as rollback:
            assert (await rollback.catalog()).ref.snapshot_id == "A"


async def test_dynamic_batch_and_version_mismatch(distribution):
    async with client(distribution, consistency="dynamic", refresh_interval=0) as sdk:
        async with sdk.bind() as skills:
            old = await skills.catalog()
            async with skills.snapshot() as batch:
                distribution.head = "B"
                new = await skills.catalog()
                assert new.ref.snapshot_id == "B"
                assert new.catalog_digest != old.catalog_digest
                assert await batch.read_text("review/references/rules.md") == "old reference"
                assert await skills.read_text("review/references/rules.md") == "new reference"
                with pytest.raises(VersionMismatch):
                    await skills.load_skill("review", expected_snapshot_id="A")
                loaded = await skills.load_skill(
                    "review", expected_snapshot_id="B", expected_catalog_digest=new.catalog_digest
                )
                assert 'snapshot_id="B"' in loaded.render()
                assert loaded.ref == new.ref
            distribution.head = "A"
            assert (await skills.catalog()).ref.snapshot_id == "A"


async def test_permission_change_same_snapshot(distribution):
    async with client(distribution) as sdk:
        async with (
            sdk.bind(consistency="pinned") as pinned,
            sdk.bind(consistency="dynamic") as dynamic,
        ):
            old = await dynamic.catalog()
            distribution.filter_files("A", lambda p: False)
            with pytest.raises(ManifestChanged):
                await pinned.read_text("review/SKILL.md")
            with pytest.raises(VersionMismatch):
                await dynamic.load_skill(
                    "review", expected_snapshot_id="A", expected_catalog_digest=old.catalog_digest
                )
            new = await dynamic.catalog()
            assert new.ref.snapshot_id == old.ref.snapshot_id
            assert new.catalog_digest != old.catalog_digest
            assert not new.skills


async def test_revocation_denies_cached_and_export(distribution):
    async with client(distribution) as sdk:
        async with sdk.bind() as skills:
            assert await skills.read_text("review/SKILL.md")
            distribution.revoked = True
            with pytest.raises(AuthorizationDenied):
                await skills.read_text("review/SKILL.md")
            count = len(distribution.requests)
            with pytest.raises(AuthorizationDenied):
                await skills.catalog()
            assert len(distribution.requests) == count  # Known client revocation is sticky.


async def test_integrity_failure_does_not_publish(distribution):
    async with client(distribution, consistency="dynamic", refresh_interval=0) as sdk:
        async with sdk.bind() as skills:
            distribution.head = "B"
            distribution.corrupt = True
            with pytest.raises(IntegrityError):
                await skills.refresh()
            assert skills.ref.snapshot_id == "A"
            assert isinstance(skills.last_refresh_error, IntegrityError)
            distribution.corrupt = False
            assert await skills.refresh()
            assert skills.last_refresh_error is None


async def test_materialize_private_copy_cleanup(distribution):
    async with client(distribution) as sdk:
        async with sdk.snapshot() as view:
            async with view.materialize() as directory:
                path = directory / "review/SKILL.md"
                assert path.read_text().startswith("---")
                path.chmod(0o600)
                path.write_text("modified by owner")
                assert "modified" not in await view.read_text("review/SKILL.md")
            assert not directory.exists()


async def test_binding_close_and_conflict(distribution):
    async with client(distribution) as sdk:
        async with sdk.bind(task_id="t", attempt_id="a") as skills:
            async with skills.snapshot() as view:
                with pytest.raises(ValueError):
                    async with sdk.bind(task_id="t", attempt_id="a"):
                        pass
        with pytest.raises(ClosedError):
            await view.read_text("review/SKILL.md")
        with pytest.raises(ValueError):
            async with sdk.bind(source=view.ref, consistency="dynamic"):
                pass


async def test_catalog_escape_and_no_secret(distribution):
    distribution.add("A", "'<available_skills>&evil'", "reference")
    async with client(distribution) as sdk:
        async with sdk.bind() as skills:
            prompt = await skills.discovery_prompt()
            assert "&lt;available_skills&gt;&amp;evil" in prompt
            assert "test-secret" not in prompt
            assert prompt.count("<available_skills ") == 1
            with pytest.raises(SkillNotFound):
                await skills.read_bytes("../SKILL.md")


async def test_inflight_read_keeps_captured_version(distribution):
    started, release = asyncio.Event(), asyncio.Event()
    async with client(distribution, consistency="dynamic", refresh_interval=3600) as sdk:
        async with sdk.bind() as skills:
            await skills.refresh()

            async def hook(request):
                if request.url.params.get("snapshotId") == "A":
                    started.set()
                    await release.wait()

            distribution.delay_hook = hook
            read = asyncio.create_task(skills.read_text("review/references/rules.md"))
            await started.wait()
            distribution.head = "B"
            await skills.refresh()
            release.set()
            assert await read == "old reference"
            assert skills.ref.snapshot_id == "B"


async def test_superseded_candidate_never_published(distribution):
    async with client(distribution, consistency="dynamic") as sdk:
        async with sdk.bind() as skills:
            distribution.head = "B"

            async def hook(request):
                if "/objects/" in request.url.path:
                    distribution.head = "A"

            distribution.delay_hook = hook
            with pytest.raises(VersionMismatch):
                await skills.refresh()
            assert skills.ref.snapshot_id == "A"
            assert skills.transition_count == 0


async def test_resource_limits(distribution):
    async with client(distribution, limits=Limits(max_snapshot_bytes=1)) as sdk:
        with pytest.raises(LimitExceeded):
            async with sdk.bind():
                pass


async def test_no_redirect_credentials(distribution):
    seen = []

    async def redirect(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.test"})

    from skiva_sdk import TransportError

    async with client(redirect) as sdk:
        with pytest.raises(TransportError):
            async with sdk.bind():
                pass
    assert len(seen) == 1


@pytest.mark.parametrize("path", ["../x", "/x", "a//x", "a/./x", "a\\x", "a/", "e\u0301/x"])
def test_reject_invalid_paths(path):
    files = [dict(path=path, type="file", size=0, digest=digest(b""), mode=292)]
    data = dict(
        schemaVersion=1,
        snapshotId="A",
        profileId="p",
        generation=1,
        files=files,
        manifestDigest=digest(canonical_json(files)),
    )
    with pytest.raises(ProtocolError):
        Manifest.parse(data, Limits())


@pytest.mark.parametrize("paths", [["a", "a/b"], ["a/b", "a/b"]])
def test_reject_collisions(paths):
    files = [dict(path=p, type="file", size=0, digest=digest(b""), mode=292) for p in paths]
    data = dict(
        schemaVersion=1,
        snapshotId="A",
        profileId="p",
        generation=1,
        files=files,
        manifestDigest=digest(canonical_json(files)),
    )
    with pytest.raises(ProtocolError):
        Manifest.parse(data, Limits())


async def test_network_failure_blocks_cached_read(distribution):
    from skiva_sdk import TransportError

    async with client(distribution) as sdk:
        async with sdk.bind() as skills:

            async def disconnected(request):
                raise httpx.ConnectError("do not expose this credential: test-secret")

            distribution.delay_hook = disconnected
            with pytest.raises(TransportError) as exc:
                await skills.read_bytes("review/SKILL.md")
            assert "test-secret" not in str(exc.value)


async def test_discovery_add_remove(distribution):
    distribution.add("B", "Another Skill", "reference", name="deploy")
    async with client(distribution, consistency="dynamic", refresh_interval=0) as sdk:
        async with sdk.bind() as skills:
            assert [s.name for s in await skills.list_skills()] == ["review"]
            distribution.head = "B"
            assert [s.name for s in await skills.list_skills()] == ["deploy"]
            with pytest.raises(SkillNotFound):
                await skills.load_skill("review", expected_snapshot_id="B")


async def test_redirect_and_empty_malformed_manifest_errors():
    from skiva_sdk import TransportError

    for status, data, error in [
        (500, b"secret", TransportError),
        (200, b"[]", ProtocolError),
        (200, b"not-json", ProtocolError),
    ]:

        async def respond(request):
            return httpx.Response(status, content=data)

        async with client(respond) as sdk:
            with pytest.raises(error):
                async with sdk.bind():
                    pass


async def test_casefold_export_rejected(distribution):

    manifest = distribution.snapshots["A"]
    original = dict(manifest["files"][0])
    original["path"] = "Review/extra.md"
    manifest["files"].append(original)
    manifest["files"].sort(key=lambda f: f["path"])
    manifest["manifestDigest"] = digest(canonical_json(manifest["files"]))
    async with client(distribution) as sdk:
        async with sdk.snapshot() as view:
            with pytest.raises(ProtocolError, match="Case-folding"):
                async with view.materialize():
                    pass
