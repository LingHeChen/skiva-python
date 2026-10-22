import asyncio
import json
import sys
from dataclasses import replace

import pytest
from scenarios import CASES, ScenarioServer

from skiva_sdk import (
    AuthorizationDenied,
    IntegrityError,
    ManifestChanged,
    ProtocolError,
    SkillNotFound,
    TransportError,
    VersionMismatch,
)


@pytest.fixture
def server():
    return ScenarioServer()


@pytest.mark.parametrize("policy", ["pinned", "dynamic"])
async def test_multiskill_nested_references_match_disk(server, policy):
    async with server.client(consistency=policy) as client:
        async with client.bind() as skills:
            catalog = await skills.catalog()
            assert {s.name for s in catalog.skills} == {
                "expense-audit",
                "release-review",
                "data-contract",
            }
            async with skills.snapshot() as batch:
                for path in await batch.list_files():
                    assert await batch.read_bytes(path) == (CASES / "release-1" / path).read_bytes()
                body = await batch.load_skill("expense-audit")
                assert "references/regions/上海.md" in body.text
                assert body.catalog_digest == catalog.catalog_digest
                assert await batch.read_bytes("data-contract/assets/empty.txt") == b""
                with pytest.raises(ProtocolError):
                    await batch.read_text("data-contract/assets/sample.bin")


async def test_audit_script_export_pinned_and_republished(server):
    async with server.client(consistency="dynamic", refresh_interval=0) as client:
        async with client.bind() as skills:
            async with skills.snapshot() as old:
                async with old.materialize() as old_dir:
                    server.head = "release-2"
                    await skills.refresh()
                    async with skills.snapshot() as new:
                        async with new.materialize() as new_dir:
                            outputs = []
                            for directory in (old_dir, new_dir):
                                process = await asyncio.create_subprocess_exec(
                                    sys.executable,
                                    "-I",
                                    str(directory / "expense-audit/scripts/evaluate.py"),
                                    stdout=asyncio.subprocess.PIPE,
                                    stderr=asyncio.subprocess.PIPE,
                                )
                                stdout, stderr = await process.communicate()
                                assert process.returncode == 0, stderr.decode()
                                outputs.append(json.loads(stdout))
                            assert outputs == [
                                {"revision": "release-1", "total": 800, "approved": True},
                                {"revision": "release-2", "total": 800, "approved": False},
                            ]
                            assert (
                                old_dir / "data-contract/assets/sample.bin"
                            ).read_bytes() == bytes([0, 255, 254, 1, 2])
                    assert not new_dir.exists()
                assert not old_dir.exists()


async def test_cross_credential_refs_and_grants_do_not_authorize(server):
    async with server.client() as admin, server.client("reviewer") as reviewer:
        async with admin.bind() as full:
            ref = full.ref
            with pytest.raises(VersionMismatch):
                async with reviewer.bind(source=ref):
                    pass
            async with reviewer.bind() as restricted:
                assert [s.name for s in await restricted.list_skills()] == ["release-review"]
                with pytest.raises(SkillNotFound):
                    await restricted.read_text("expense-audit/SKILL.md")
            server.head = "release-2"
            # A new credential has no historical grant even with a genuine ref.
            server.allowed["new"] = server.allowed["all"].copy()
            async with server.client("new") as fresh:
                with pytest.raises(AuthorizationDenied):
                    async with fresh.bind(source=replace(ref, profile_id="profile-new")):
                        pass


async def test_permission_loss_invalidates_batch_and_dynamic_rebuilds_catalog(server):
    async with server.client(consistency="dynamic", refresh_interval=0) as client:
        async with client.bind() as skills:
            before = await skills.catalog()
            async with skills.snapshot() as old:
                server.allowed["all"].remove("expense-audit")
                with pytest.raises(ManifestChanged):
                    await old.read_text("expense-audit/SKILL.md")
                after = await skills.catalog()
                assert before.ref.snapshot_id == after.ref.snapshot_id
                assert before.catalog_digest != after.catalog_digest
                with pytest.raises(VersionMismatch):
                    await skills.load_skill(
                        "release-review",
                        expected_snapshot_id=before.ref.snapshot_id,
                        expected_catalog_digest=before.catalog_digest,
                    )
                assert {s.name for s in after.skills} == {"release-review", "data-contract"}
                # Existing content is inaccessible even if permissions are later restored.
                server.allowed["all"].add("expense-audit")
                with pytest.raises(ManifestChanged):
                    await old.read_bytes("expense-audit/SKILL.md")


async def test_profile_pin_overrides_dynamic_client(server):
    server.pins["all"] = "release-1"
    async with server.client(consistency="dynamic", refresh_interval=0) as client:
        async with client.bind() as skills:
            server.head = "release-2"
            assert not await skills.refresh()
            assert (await skills.catalog()).ref.snapshot_id == "release-1"


async def test_cancellation_during_preparation_releases_attempt(server):
    started = asyncio.Event()

    async def stall(request):
        if "/objects/" in request.url.path:
            started.set()
            await asyncio.Event().wait()

    server.hook = stall
    async with server.client() as client:

        async def bind():
            async with client.bind(task_id="task", attempt_id="attempt"):
                pytest.fail("Cancelled preparation must not yield")

        task = asyncio.create_task(bind())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.hook = None
        async with client.bind(task_id="task", attempt_id="attempt") as recovered:
            assert len(await recovered.list_skills()) == 3


async def test_cancel_refresh_does_not_publish_partial_view(server):
    started = asyncio.Event()
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as skills:
            server.head = "release-2"

            async def stall(request):
                if "/objects/" in request.url.path:
                    started.set()
                    await asyncio.Event().wait()

            server.hook = stall
            task = asyncio.create_task(skills.refresh())
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert skills.ref.snapshot_id == "release-1"
            assert skills.transition_count == 0
            server.hook = None
            assert await skills.refresh()


async def test_revoked_halfway_through_download_never_yields(server):
    objects = 0

    async def revoke(request):
        nonlocal objects
        if "/objects/" in request.url.path:
            objects += 1
            if objects == 3:
                server.revoked.add("all")

    server.hook = revoke
    async with server.client() as client:
        with pytest.raises(AuthorizationDenied):
            async with client.bind():
                pytest.fail("Partially downloaded revoked view must not be exposed")


async def test_concurrent_refresh_publishes_once(server):
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as skills:
            server.head = "release-2"
            changes = await asyncio.gather(*(skills.refresh() for _ in range(24)))
            assert sum(changes) == 1
            assert skills.transition_count == 1
            contents = await asyncio.gather(
                *(skills.read_skill_file("expense-audit/references/policy.json") for _ in range(24))
            )
            assert all(json.loads(c.text)["cap"] == 600 for c in contents)
            assert all(c.ref.snapshot_id == "release-2" for c in contents)


@pytest.mark.parametrize("policy", ["pinned", "dynamic"])
async def test_refresh_error_status_clears_after_recovery(server, policy):
    async with server.client(consistency=policy) as client:
        async with client.bind() as skills:
            server.failure = 503
            with pytest.raises(TransportError):
                await skills.refresh()
            assert isinstance(skills.last_refresh_error, TransportError)
            server.failure = None
            assert not await skills.refresh()
            assert skills.last_refresh_error is None


async def test_integrity_failure_late_in_candidate_preserves_batch(server):
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as skills:
            async with skills.snapshot() as old:
                server.head = "release-2"
                server.damage = server.trees["release-2"][-1]["digest"]
                with pytest.raises(IntegrityError):
                    await skills.refresh()
                assert skills.ref == old.ref
                assert (
                    json.loads(await old.read_text("expense-audit/references/policy.json"))["cap"]
                    == 1000
                )
                server.damage = None
                assert await skills.refresh()


async def test_release_rules_and_schema_change_together(server):
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as skills:
            for release, severity, fields in [
                ("release-1", "warning", ["id", "email"]),
                ("release-2", "blocking", ["id", "email", "consent"]),
            ]:
                server.head = release
                await skills.refresh()
                async with skills.snapshot() as batch:
                    review = await batch.read_skill_file("release-review/references/checklist.json")
                    schema = await batch.read_skill_file("data-contract/references/schema.json")
                    assert review.ref == schema.ref == batch.ref
                    assert json.loads(review.text)["breaking_change"] == severity
                    assert json.loads(schema.text)["required"] == fields


async def test_export_cleans_up_on_consumer_exception(server):
    async with server.client() as client:
        async with client.snapshot() as batch:
            with pytest.raises(RuntimeError, match="consumer failed"):
                async with batch.materialize() as directory:
                    assert directory.exists()
                    raise RuntimeError("consumer failed")
            assert not directory.exists()
            # Consumer failure does not invalidate the authorized source view.
            assert await batch.read_bytes("data-contract/assets/empty.txt") == b""


async def test_large_catalog_deduplicates_shared_reference(server):
    from skiva_sdk.protocol import digest

    files = []
    shared = b"One reference shared by 500 Skills."
    shared_digest = digest(shared)
    server.blobs[shared_digest] = shared
    for i in range(500):
        name = f"skill-{i:04d}"
        body = f"---\nname: {name}\ndescription: Test workflow {i}\n---\nRead ref.md.\n".encode()
        sha = digest(body)
        server.blobs[sha] = body
        server.allowed["all"].add(name)
        files.extend(
            [
                dict(path=f"{name}/SKILL.md", type="file", size=len(body), digest=sha, mode=292),
                dict(
                    path=f"{name}/ref.md",
                    type="file",
                    size=len(shared),
                    digest=shared_digest,
                    mode=292,
                ),
            ]
        )
    server.trees["release-1"] = files
    async with server.client() as client:
        async with client.bind() as skills:
            assert len(await skills.list_skills()) == 500
            assert len(await skills.list_files()) == 1000
            assert await skills.read_bytes("skill-0499/ref.md") == shared
    downloads = [path for _, path, _ in server.calls if "/objects/" in path]
    assert len(downloads) == 501
    assert sum(path.endswith(shared_digest) for path in downloads) == 1
