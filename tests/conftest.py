import copy

import httpx
import pytest

from skiva_sdk.protocol import canonical_json, digest


class Distribution:
    def __init__(self):
        self.head = "A"
        self.snapshots = {}
        self.blobs = {}
        self.grants = set()
        self.revoked = False
        self.corrupt = False
        self.requests = []
        self.delay_hook = None
        self.add("A", "Old rules", "old reference")
        self.add("B", "New rules", "new reference")

    def add(self, version, description, reference, name="review"):
        contents = {
            f"{name}/SKILL.md": (
                f"---\nname: {name}\ndescription: {description}\n---\n"
                f"# {version}\nUse references/rules.md\n"
            ).encode(),
            f"{name}/references/rules.md": reference.encode(),
        }
        files = []
        for path, data in sorted(contents.items()):
            sha = digest(data)
            self.blobs[sha] = data
            files.append(dict(path=path, type="file", size=len(data), digest=sha, mode=292))
        self.snapshots[version] = dict(
            schemaVersion=1,
            snapshotId=version,
            profileId="profile-1",
            generation=1,
            files=files,
            manifestDigest=digest(canonical_json(files)),
        )

    def filter_files(self, version, keep):
        m = self.snapshots[version]
        m["files"] = [f for f in m["files"] if keep(f["path"])]
        m["manifestDigest"] = digest(canonical_json(m["files"]))

    async def __call__(self, request):
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer test-secret"
        if self.delay_hook:
            await self.delay_hook(request)
        if self.revoked:
            return httpx.Response(403, json={"error": {"code": "CLIENT_FORBIDDEN"}})
        path = request.url.path
        version = request.url.params.get("snapshotId", self.head)
        if version != self.head and version not in self.grants:
            return httpx.Response(403, json={"error": {"code": "SNAPSHOT_FORBIDDEN"}})
        if path.endswith("/manifest"):
            self.grants.add(version)
            return httpx.Response(200, json=copy.deepcopy(self.snapshots[version]))
        if "/objects/" in path:
            assert "snapshotId" in request.url.params
            sha = path.split("/objects/")[1]
            if not any(f["digest"] == sha for f in self.snapshots[version]["files"]):
                return httpx.Response(403, json={"error": {"code": "OBJECT_FORBIDDEN"}})
            return httpx.Response(200, content=b"bad" if self.corrupt else self.blobs[sha])
        return httpx.Response(404)


@pytest.fixture
def distribution():
    return Distribution()
