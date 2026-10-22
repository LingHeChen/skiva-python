"""Malformed inputs, streaming failures and limits must fail before publication."""

import httpx
import pytest

from skiva_sdk import (
    AuthorizationDenied,
    IntegrityError,
    LimitExceeded,
    Limits,
    ProtocolError,
    SkivaClient,
    TransportError,
)
from skiva_sdk.catalog import parse_skill
from skiva_sdk.protocol import Manifest, canonical_json, digest


@pytest.mark.parametrize(
    "data",
    [
        b"plain markdown",
        b"---\nname: x",
        b"---\n[]\n---",
        b"---\nname: [x]\n---",
        b"---\nname: wrong\ndescription: valid\n---",
        b"---\nname: x\ndescription: ''\n---",
        b"---\nname: x\ndescription: 42\n---",
        b"---\nname: x\ndescription: [a,b]\n---",
        b"---\nname: x\ndescription: !!python/object:os.system {}\n---",
        b"---\nname: x\ndescription: \xff\n---",
        b"---\nname: x\ndescription: " + b"a" * 1025 + b"\n---",
    ],
)
def test_invalid_skill_metadata_is_rejected(data):
    with pytest.raises(ProtocolError):
        parse_skill("x/SKILL.md", data)


def manifest_with(**updates):
    data = dict(
        schemaVersion=1,
        snapshotId="s",
        profileId="p",
        generation=1,
        files=[],
        manifestDigest=digest(b"[]"),
    )
    data.update(updates)
    return data


@pytest.mark.parametrize(
    "updates",
    [
        {"schemaVersion": True},
        {"schemaVersion": 2},
        {"snapshotId": None},
        {"profileId": ""},
        {"generation": -1},
        {"generation": True},
        {"files": {}},
        {"files": [None]},
    ],
)
def test_invalid_manifest_structure(updates):
    with pytest.raises(ProtocolError):
        Manifest.parse(manifest_with(**updates), Limits())


@pytest.mark.parametrize(
    "updates",
    [
        {"size": -1},
        {"size": True},
        {"size": 1.5},
        {"mode": 511},
        {"mode": True},
        {"type": "symlink"},
        {"digest": "sha256:" + "A" * 64},
        {"path": "a\x00b"},
        {"path": "a\rb"},
        {"path": "a\nb"},
        {"path": "a" * 4097},
    ],
)
def test_invalid_file_metadata(updates):
    file = dict(path="x/file", type="file", size=0, digest=digest(b""), mode=292)
    file.update(updates)
    data = manifest_with(files=[file], manifestDigest=digest(canonical_json([file])))
    with pytest.raises(ProtocolError):
        Manifest.parse(data, Limits())


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, fail=False):
        self.chunks, self.fail, self.closed = chunks, fail, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail:
            raise httpx.ReadError("credential-must-not-leak")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("fail", [False, True])
async def test_stream_size_limit_and_failure_close_response(fail):
    stream = Chunks([b"x" * 8, b"y" * 8], fail=fail)

    async def respond(request):
        return httpx.Response(200, stream=stream)

    limit = 32 if fail else 10
    async with SkivaClient(
        "https://test.invalid",
        "token",
        limits=Limits(max_manifest_bytes=limit),
        transport=httpx.MockTransport(respond),
    ) as sdk:
        with pytest.raises(TransportError if fail else LimitExceeded) as exc:
            async with sdk.bind():
                pytest.fail("Invalid stream yielded a view")
        assert "credential-must-not-leak" not in str(exc.value)
    assert stream.closed


@pytest.mark.parametrize("body", [b"[]", b"not json", b'{"error":"bad"}', b"{" * 5000])
async def test_malformed_denial_remains_authorization_error(body):
    async def respond(request):
        return httpx.Response(403, content=body)

    async with SkivaClient(
        "https://test.invalid", "token", transport=httpx.MockTransport(respond)
    ) as sdk:
        with pytest.raises(AuthorizationDenied):
            async with sdk.bind():
                pass


async def test_excessive_json_nesting_is_protocol_error():
    async def respond(request):
        return httpx.Response(200, content=b"[" * 2000 + b"]" * 2000)

    async with SkivaClient(
        "https://test.invalid", "token", transport=httpx.MockTransport(respond)
    ) as sdk:
        with pytest.raises(ProtocolError):
            async with sdk.bind():
                pass


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_files", 1),
        ("max_file_bytes", 1),
        ("max_snapshot_bytes", 10),
        ("max_skill_bytes", 1),
        ("max_manifest_bytes", 10),
    ],
)
async def test_each_limit_is_enforced(distribution, field, value):
    async with SkivaClient(
        "https://test.invalid",
        "test-secret",
        limits=Limits(**{field: value}),
        transport=httpx.MockTransport(distribution),
    ) as sdk:
        with pytest.raises(LimitExceeded):
            async with sdk.bind():
                pass


@pytest.mark.parametrize(
    "path", ["x/CON.txt", "x/a:b", "x/trailing.", "x/trailing ", "x/a\tfile", "x/NUL", "x/AUX.py"]
)
async def test_export_rejects_platform_aliases(distribution, path):
    manifest = distribution.snapshots["A"]
    file = dict(path=path, type="file", size=0, digest=digest(b""), mode=292)
    distribution.blobs[file["digest"]] = b""
    manifest["files"].append(file)
    manifest["files"].sort(key=lambda f: f["path"])
    manifest["manifestDigest"] = digest(canonical_json(manifest["files"]))
    async with SkivaClient(
        "https://test.invalid", "test-secret", transport=httpx.MockTransport(distribution)
    ) as sdk:
        async with sdk.snapshot() as view:
            with pytest.raises(ProtocolError):
                async with view.materialize():
                    pass


async def test_conflicting_sizes_for_deduplicated_object(distribution):
    m = distribution.snapshots["A"]
    first = m["files"][0]
    m["files"].append(dict(first, path="z/duplicate", size=first["size"] + 1))
    m["manifestDigest"] = digest(canonical_json(m["files"]))
    async with SkivaClient(
        "https://test.invalid", "test-secret", transport=httpx.MockTransport(distribution)
    ) as sdk:
        with pytest.raises(IntegrityError, match="Conflicting"):
            async with sdk.bind():
                pass
