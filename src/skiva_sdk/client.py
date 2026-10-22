"""Async distribution client. All network operations are read-only."""

from __future__ import annotations

import json
import math
import os
from collections import OrderedDict
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
    SnapshotBusy,
    TransportError,
    VersionMismatch,
)
from .protocol import Limits, Manifest, RetryCheckpoint, SnapshotRef, digest
from .store import BindingStore

Consistency = Literal["pinned", "dynamic"]
RetryPolicy = Literal["reuse_snapshot", "latest"]


class SkivaClient:
    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        consistency: Consistency = "dynamic",
        retry_policy: RetryPolicy = "reuse_snapshot",
        refresh_interval: float = 0.0,
        max_snapshot_bindings: int = 1024,
        binding_store: BindingStore | None = None,
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
        if retry_policy not in ("reuse_snapshot", "latest"):
            raise ValueError("Unknown retry policy")
        if type(max_snapshot_bindings) is not int or max_snapshot_bindings < 1:
            raise ValueError("max_snapshot_bindings must be a positive integer")
        self.max_snapshot_bindings = max_snapshot_bindings
        self.binding_store = binding_store
        self._manifest_cache: OrderedDict[str | None, tuple[str, bytes]] = OrderedDict()
        self._snapshots: dict[str, SnapshotRef] = {}
        self._snapshot_active: set[str] = set()
        self.retry_policy = retry_policy
        self.server = server_url.rstrip("/")
        self.consistency = consistency
        self.refresh_interval = refresh_interval
        self.limits = limits or Limits()
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "User-Agent": "skiva-python/0.3.0"},
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

    async def _get(
        self,
        path: str,
        *,
        params: dict | None = None,
        limit: int,
        cached: tuple[str, bytes] | None = None,
        response_headers: dict | None = None,
    ) -> bytes:
        self._check()
        try:
            async with self._http.stream(
                "GET",
                self.server + "/distribution/v1" + path,
                params=params,
                headers={"If-None-Match": cached[0]} if cached else {},
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
                if response.status_code == 304 and cached is not None:
                    self._check()
                    return cached[1]
                if response.status_code != 200:
                    raise TransportError(response.status_code)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > limit:
                        raise LimitExceeded("Distribution response exceeds configured limit")
                    data.extend(chunk)
                self._check()
                if response_headers is not None:
                    response_headers.update(response.headers)
                return bytes(data)
        except httpx.HTTPError:
            raise TransportError() from None

    async def _manifest(self, snapshot_id: str | None = None) -> Manifest:
        cached = self._manifest_cache.get(snapshot_id)
        headers: dict[str, str] = {}
        raw = await self._get(
            "/manifest",
            params={"snapshotId": snapshot_id} if snapshot_id else {},
            limit=self.limits.max_manifest_bytes,
            cached=cached,
            response_headers=headers,
        )
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as e:
            raise ProtocolError("Invalid manifest JSON") from e
        result = Manifest.parse(data, self.limits)
        if snapshot_id is not None and result.snapshot_id != snapshot_id:
            raise ProtocolError("Server returned an unexpected snapshot")
        etag = headers.get("etag")
        if etag:
            self._manifest_cache[snapshot_id] = (etag, raw)
            self._manifest_cache.move_to_end(snapshot_id)
            # Bound by both entries and bytes; only metadata, never credentials.
            while (
                len(self._manifest_cache) > 16
                or sum(len(v[1]) for v in self._manifest_cache.values())
                > self.limits.max_manifest_bytes
            ):
                self._manifest_cache.popitem(last=False)
        elif headers:
            self._manifest_cache.pop(snapshot_id, None)
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

        policy = (
            consistency
            if consistency is not None
            else ("pinned" if source is not None else self.consistency)
        )
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
    async def retry(
        self,
        previous: RetryCheckpoint,
        *,
        retry_policy: RetryPolicy | None = None,
        attempt_id: str | None = None,
    ):
        """Start one retry binding; never execute/replay application actions automatically.

        reuse_snapshot fixes the supplied checkpoint for this entire retry.
        latest resolves the current authorized target and uses the client consistency.
        """
        if not isinstance(previous, RetryCheckpoint):
            raise TypeError("retry requires a RetryCheckpoint")
        if previous.snapshot.server != self.server:
            raise ValueError("RetryCheckpoint belongs to a different server")
        policy = self.retry_policy if retry_policy is None else retry_policy
        if policy not in ("reuse_snapshot", "latest"):
            raise ValueError("Unknown retry policy")
        attempt = attempt_id if attempt_id is not None else str(uuid4())
        if not attempt or attempt == previous.attempt_id:
            raise ValueError("A retry requires a new non-empty attempt ID")
        reuse = policy == "reuse_snapshot"
        async with self.bind(
            task_id=previous.task_id,
            attempt_id=attempt,
            source=previous.snapshot if reuse else None,
            consistency="pinned" if reuse else self.consistency,
        ) as session:
            yield session

    @asynccontextmanager
    async def snapshot(
        self,
        id: str | SnapshotRef | None = None,
        *,
        source: SnapshotRef | None = None,
        retry_policy: RetryPolicy = "reuse_snapshot",
    ):
        """Fixed snapshot view, optionally remembered by a client-local binding ID.

        A string ID is an application binding key, NOT a server snapshot UUID.
        Re-entry reuses the recorded ref unless latest is explicitly requested.
        A positional SnapshotRef remains supported for compatibility.
        """
        self._check()
        if isinstance(id, SnapshotRef):
            if source is not None:
                raise ValueError("Specify only one snapshot source")
            source, id = id, None
        if id is not None and (not isinstance(id, str) or not id or len(id) > 256):
            raise ValueError("Snapshot binding ID must be a non-empty string up to 256 characters")
        if retry_policy not in ("reuse_snapshot", "latest"):
            raise ValueError("Unknown retry policy")
        if source is not None and not isinstance(source, SnapshotRef):
            raise TypeError("source must be a SnapshotRef")
        if source is not None and retry_policy == "latest":
            raise ValueError("latest cannot be combined with an explicit source")
        if id is not None:
            if id in self._snapshot_active:
                raise SnapshotBusy("Snapshot binding is already in use")
            existing = (
                self.binding_store.get(self.server + "\n" + id)
                if self.binding_store
                else self._snapshots.get(id)
            )
            if existing is not None:
                if source is not None and source != existing:
                    raise ValueError("Binding already refers to another snapshot")
                if retry_policy == "reuse_snapshot":
                    source = existing
            # Count preparing new bindings as well as completed records.
            if (
                self.binding_store is None
                and existing is None
                and len(self._snapshots.keys() | self._snapshot_active)
                >= (self.max_snapshot_bindings)
            ):
                raise LimitExceeded("Snapshot bindings are full; forget an inactive binding")
            self._snapshot_active.add(id)
        try:
            async with self.bind(task_id=id, source=source, consistency="pinned") as session:
                async with session.snapshot() as view:
                    if id is not None:
                        # No await before publication: failed preparation preserves the old ref.
                        if self.binding_store:
                            self.binding_store.put(
                                self.server + "\n" + id,
                                view.ref,
                                expected=existing,
                                limit=self.max_snapshot_bindings,
                            )
                        else:
                            self._snapshots[id] = view.ref
                    yield view
        finally:
            if id is not None:
                self._snapshot_active.discard(id)

    def task(self, task_id: str, *, retry_policy: RetryPolicy = "reuse_snapshot"):
        """Alias for snapshot(task_id); no task scheduling or model-history management."""
        return self.snapshot(task_id, retry_policy=retry_policy)

    def forget_snapshot(self, id: str) -> bool:
        """Forget an inactive local binding. Does not delete server snapshots or grants."""
        self._check()
        if not isinstance(id, str) or not id:
            raise ValueError("Snapshot binding ID must be a non-empty string")
        if id in self._snapshot_active:
            raise SnapshotBusy("Cannot forget an active snapshot binding")
        if self.binding_store:
            key = self.server + "\n" + id
            return self.binding_store.delete(key, expected=self.binding_store.get(key))
        return self._snapshots.pop(id, None) is not None
