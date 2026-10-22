"""Distribution emulator with immutable releases and credential-scoped grants/filtering.

This is a test double, not a replacement for acceptance against the Go service.
"""

from pathlib import Path

import httpx

from skiva_sdk import SkivaClient
from skiva_sdk.protocol import canonical_json, digest

CASES = Path(__file__).parent / "cases"


class ScenarioServer:
    def __init__(self):
        self.head = "release-1"
        self.allowed = {
            "all": {"expense-audit", "release-review", "data-contract"},
            "reviewer": {"release-review"},
        }
        self.revoked = set()
        self.pins = {}
        self.grants = set()
        self.calls = []
        self.hook = None
        self.failure = None
        self.damage = None
        self.trees = {}
        self.blobs = {}
        for release in ("release-1", "release-2"):
            files = []
            for path in sorted((CASES / release).rglob("*")):
                if not path.is_file():
                    continue
                name = path.relative_to(CASES / release).as_posix()
                data = path.read_bytes()
                sha = digest(data)
                self.blobs[sha] = data
                files.append(
                    dict(
                        path=name,
                        type="file",
                        size=len(data),
                        digest=sha,
                        mode=365 if "/scripts/" in name else 292,
                    )
                )
            self.trees[release] = files

    def client(self, token="all", **kwargs):
        return SkivaClient(
            "https://scenario.test", token, transport=httpx.MockTransport(self), **kwargs
        )

    def manifest(self, token, release):
        files = [f for f in self.trees[release] if f["path"].split("/")[0] in self.allowed[token]]
        return dict(
            schemaVersion=1,
            snapshotId=release,
            profileId="profile-" + token,
            generation=1 if self.head == "release-1" else 2,
            files=files,
            manifestDigest=digest(canonical_json(files)),
        )

    async def __call__(self, request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.calls.append((token, request.url.path, dict(request.url.params)))
        if self.hook:
            await self.hook(request)
        if token in self.revoked or token not in self.allowed:
            return httpx.Response(403, json={"error": {"code": "CLIENT_FORBIDDEN"}})
        if self.failure:
            return httpx.Response(self.failure)
        current = self.pins.get(token, self.head)
        release = request.url.params.get("snapshotId", current)
        if release not in self.trees or (
            release != current and (token, release) not in self.grants
        ):
            return httpx.Response(403, json={"error": {"code": "SNAPSHOT_FORBIDDEN"}})
        manifest = self.manifest(token, release)
        if request.url.path.endswith("/manifest"):
            self.grants.add((token, release))
            return httpx.Response(200, json=manifest)
        sha = request.url.path.split("/objects/")[-1]
        if not any(f["digest"] == sha for f in manifest["files"]):
            return httpx.Response(403, json={"error": {"code": "OBJECT_FORBIDDEN"}})
        data = self.blobs[sha]
        if self.damage == sha:
            data = b"x" * len(data)
        return httpx.Response(200, content=data)
