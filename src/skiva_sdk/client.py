"""Async distribution client. All network operations are read-only."""

from __future__ import annotations

import json
import math
import os
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .errors import (
    AuthorizationDenied,
    ClosedError,
    IntegrityError,
    LimitExceeded,
    ProtocolError,
    TransportError,
    VersionMismatch,
)
from .protocol import Limits, Manifest, SnapshotRef, digest

Consistency = Literal["pinned", "dynamic"]


class SkivaClient:
    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        consistency: Consistency = "pinned",
        refresh_interval: float = 5.0,
        timeout: float = 20.0,
        trust_env: bool = False,
        limits: Limits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed = urlsplit(server_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected an HTTP(S) server URL without credentials/query/fragment")
        if not token or any(c in token for c in "\r\n"):
            raise ValueError("A client token is required")
        if (
            consistency not in ("pinned", "dynamic")
            or refresh_interval < 0
            or not math.isfinite(refresh_interval)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Invalid consistency or refresh interval")
        self.server = server_url.rstrip("/")
        self.consistency = consistency
        self.refresh_interval = refresh_interval
        self.limits = limits or Limits()
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "User-Agent": "skiva-python/0.1.0"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=trust_env,
        )
        self._closed = False
        self._revoked = False
        self._active: set[tuple[str, str]] = set()

    @classmethod
    def from_env(cls, **kwargs) -> SkivaClient:
        return cls(os.environ["SKIVA_SERVER_URL"], os.environ["SKIVA_CLIENT_TOKEN"], **kwargs)

    async def __aenter__(self):
        self._check()
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        self._closed = True
        await self._http.aclose()

    def _check(self):
        if self._closed:
            raise ClosedError("Client is closed")
        if self._revoked:
            raise AuthorizationDenied("CLIENT_FORBIDDEN")

    async def _get(self, path: str, *, params: dict | None = None, limit: int) -> bytes:
        self._check()
        try:
            async with self._http.stream(
                "GET",
                self.server + "/distribution/v1" + path,
                params=params,
            ) as response:
                if response.status_code in (401, 403):
                    # Server bodies may contain user data. Never include them in exceptions.
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk[: max(0, 4096 - len(body))])
                        if len(body) >= 4096:
                            break
                    try:
                        payload = json.loads(body)
                        error = payload.get("error", {})
                        code = (
                            error.get("code", "FORBIDDEN")
                            if isinstance(error, dict)
                            else "FORBIDDEN"
                        )
                        if not isinstance(code, str) or len(code) > 64:
                            code = "FORBIDDEN"
                    except (ValueError, AttributeError, RecursionError):
                        code = "FORBIDDEN"
                    if code == "CLIENT_FORBIDDEN":
                        self._revoked = True
                    raise AuthorizationDenied(code)
                if response.status_code != 200:
                    raise TransportError(response.status_code)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise LimitExceeded("Distribution response exceeds configured limit")
                    data.extend(chunk)
                self._check()
                return bytes(data)
        except httpx.HTTPError:
            raise TransportError() from None

    async def _manifest(self, snapshot_id: str | None = None) -> Manifest:
        raw = await self._get(
            "/manifest",
            params={"snapshotId": snapshot_id} if snapshot_id else {},
            limit=self.limits.max_manifest_bytes,
        )
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as e:
            raise ProtocolError("Invalid manifest JSON") from e
        result = Manifest.parse(data, self.limits)
        if snapshot_id is not None and result.snapshot_id != snapshot_id:
            raise ProtocolError("Server returned an unexpected snapshot")
        return result

    async def _prepare(self, source: SnapshotRef | None = None, *, owner=None):
        from .view import SnapshotView

        if source and source.server != self.server:
            raise ValueError("SnapshotRef belongs to a different server")
        manifest = await self._manifest(source.snapshot_id if source else None)
        if source and (
            source.profile_id != manifest.profile_id
            or source.manifest_digest != manifest.manifest_digest
        ):
            raise VersionMismatch(source.manifest_digest, manifest.manifest_digest)
        # Eager preparation makes publication all-or-nothing. Memory is bounded per view.
        blobs: dict[str, bytes] = {}
        for file in manifest.files:
            if file.digest not in blobs:
                data = await self._get(
                    "/objects/" + file.digest,
                    params={"snapshotId": manifest.snapshot_id},
                    limit=file.size,
                )
                if len(data) != file.size or digest(data) != file.digest:
                    raise IntegrityError("Object digest or size mismatch")
                blobs[file.digest] = data
            elif len(blobs[file.digest]) != file.size:
                raise IntegrityError("Conflicting object sizes")
        view = SnapshotView(self, manifest, blobs, owner=owner)
        await view._authorize()
        return view

    @asynccontextmanager
    async def bind(
        self,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
        source: SnapshotRef | None = None,
        consistency: Consistency | None = None,
    ):
        from .view import SkillSession

        policy = consistency if consistency is not None else self.consistency
        if policy not in ("pinned", "dynamic"):
            raise ValueError("Unknown consistency policy")
        if source is not None and policy != "pinned":
            raise ValueError("An explicit SnapshotRef requires pinned consistency")
        task, attempt = task_id or str(uuid4()), attempt_id or str(uuid4())
        key = (task, attempt)
        if key in self._active:
            raise ValueError("This attempt already has an active binding on this client")
        self._active.add(key)
        session = SkillSession(self, task, attempt, policy)
        try:
            session._current = await self._prepare(source, owner=session)
            yield session
        finally:
            session._closed = True
            self._active.discard(key)

    @asynccontextmanager
    async def snapshot(self, source: SnapshotRef | None = None):
        """Convenience binding with an explicitly pinned policy."""
        async with self.bind(source=source, consistency="pinned") as session:
            async with session.snapshot() as view:
                yield view
