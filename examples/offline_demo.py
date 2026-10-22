"""Run with `python examples/offline_demo.py`; no server or model credentials needed."""

import asyncio

import httpx

from skiva_sdk import SkivaClient, VersionMismatch
from skiva_sdk.protocol import canonical_json, digest


async def main():
    snapshots, blobs = {}, {}
    for version in ("A", "B"):
        text = (
            f"---\nname: review\ndescription: Review using version {version}\n"
            f"---\nRules {version}\n"
        )
        body = text.encode()
        sha = digest(body)
        blobs[sha] = body
        files = [dict(path="review/SKILL.md", type="file", size=len(body), digest=sha, mode=292)]
        snapshots[version] = dict(
            schemaVersion=1,
            snapshotId=version,
            profileId="demo",
            generation=1,
            files=files,
            manifestDigest=digest(canonical_json(files)),
        )
    head = "A"

    async def distribution(request):
        if request.url.path.endswith("/manifest"):
            version = request.url.params.get("snapshotId", head)
            return httpx.Response(200, json=snapshots[version])
        return httpx.Response(200, content=blobs[request.url.path.split("/objects/")[1]])

    async with SkivaClient(
        "https://demo.invalid",
        "demo-only",
        consistency="dynamic",
        transport=httpx.MockTransport(distribution),
    ) as client:
        async with client.bind(task_id="demo") as skills:
            old = await skills.catalog()
            print("Initial catalog:", old.ref.snapshot_id)
            head = "B"
            try:
                await skills.load_skill(
                    "review",
                    expected_snapshot_id=old.ref.snapshot_id,
                    expected_catalog_digest=old.catalog_digest,
                )
            except VersionMismatch:
                print("Old-version load: VERSION_MISMATCH (no silent substitution)")
            current = await skills.catalog()
            content = await skills.load_skill(
                "review",
                expected_snapshot_id=current.ref.snapshot_id,
                expected_catalog_digest=current.catalog_digest,
            )
            print("Updated catalog:", current.ref.snapshot_id)
            print(content.render())
            head = "A"
            await skills.refresh()
            print("Rollback catalog:", (await skills.catalog()).ref.snapshot_id)


if __name__ == "__main__":
    asyncio.run(main())
