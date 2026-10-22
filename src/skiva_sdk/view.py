"""Snapshot reads, atomic dynamic publication and temporary directory export."""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from .catalog import Catalog, SkillContent, parse_skill
from .errors import (
    AuthorizationDenied,
    ClosedError,
    LimitExceeded,
    ManifestChanged,
    ProtocolError,
    SkillNotFound,
    VersionMismatch,
)
from .protocol import Manifest, SnapshotRef

if TYPE_CHECKING:
    from .client import SkivaClient


class SnapshotView:
    """Immutable content; every public read revalidates authorization online."""

    def __init__(self, client: SkivaClient, manifest: Manifest, blobs: dict, *, owner=None):
        self._client, self._manifest = client, manifest
        self._blobs = MappingProxyType(blobs)
        self._files = MappingProxyType({f.path: f for f in manifest.files})
        self._owner = owner
        self._invalid = False
        self.ref = SnapshotRef(
            client.server, manifest.profile_id, manifest.snapshot_id, manifest.manifest_digest
        )
        skills = []
        for f in manifest.files:
            if f.path.count("/") == 1 and f.path.endswith("/SKILL.md"):
                if f.size > client.limits.max_skill_bytes:
                    raise LimitExceeded("SKILL.md exceeds metadata parsing limit")
                skills.append(parse_skill(f.path, blobs[f.digest]))
        self._catalog = Catalog(self.ref, tuple(skills))

    def _check(self):
        self._client._check()
        if self._owner is not None and self._owner._closed:
            raise ClosedError("Binding is closed")
        if self._invalid:
            raise ManifestChanged("Captured view is no longer authorized")

    async def _authorize(self):
        self._check()
        try:
            current = await self._client._manifest(self.ref.snapshot_id)
        except AuthorizationDenied:
            self._invalid = True
            raise
        if (
            current.profile_id != self.ref.profile_id
            or current.manifest_digest != self.ref.manifest_digest
        ):
            self._invalid = True
            raise ManifestChanged("Authorized manifest changed; start a new view")
        self._check()

    async def catalog(self) -> Catalog:
        await self._authorize()
        return self._catalog

    async def discovery_prompt(self) -> str:
        return (await self.catalog()).render()

    async def list_skills(self):
        return (await self.catalog()).skills

    async def list_files(self, prefix: str = "") -> tuple[str, ...]:
        await self._authorize()
        return tuple(p for p in self._files if p.startswith(prefix))

    async def read_bytes(self, path: str) -> bytes:
        await self._authorize()
        if path not in self._files:
            raise SkillNotFound("Path is not in this authorized manifest")
        return self._blobs[self._files[path].digest]

    async def read_text(self, path: str) -> str:
        try:
            return (await self.read_bytes(path)).decode("utf-8")
        except UnicodeError as e:
            raise ProtocolError("File is not UTF-8 text; use read_bytes") from e

    def _expect(self, expected_snapshot_id: str | None, expected_catalog_digest: str | None):
        if expected_snapshot_id is not None and expected_snapshot_id != self.ref.snapshot_id:
            raise VersionMismatch(expected_snapshot_id, self.ref.snapshot_id)
        if (
            expected_catalog_digest is not None
            and expected_catalog_digest != self._catalog.catalog_digest
        ):
            raise VersionMismatch(expected_catalog_digest, self._catalog.catalog_digest)

    async def read_skill_file(
        self,
        path: str,
        *,
        expected_snapshot_id: str | None = None,
        expected_catalog_digest: str | None = None,
    ) -> SkillContent:
        await self._authorize()
        self._expect(expected_snapshot_id, expected_catalog_digest)
        if path not in self._files:
            raise SkillNotFound("Path is not in this authorized manifest")
        file = self._files[path]
        try:
            text = self._blobs[file.digest].decode("utf-8")
        except UnicodeError as e:
            raise ProtocolError("File is not UTF-8 text; use read_bytes") from e
        return SkillContent(self.ref, self._catalog.catalog_digest, path, file.digest, text)

    async def load_skill(
        self,
        name: str,
        *,
        expected_snapshot_id: str | None = None,
        expected_catalog_digest: str | None = None,
    ) -> SkillContent:
        # Names are catalog keys, never arbitrary path fragments.
        match = next((s for s in self._catalog.skills if s.name == name), None)
        if match is None:
            await self._authorize()
            self._expect(expected_snapshot_id, expected_catalog_digest)
            raise SkillNotFound("Skill is not in the current catalog")
        return await self.read_skill_file(
            match.path,
            expected_snapshot_id=expected_snapshot_id,
            expected_catalog_digest=expected_catalog_digest,
        )

    @asynccontextmanager
    async def materialize(self):
        """Export a private copy. Direct OS reads cannot enforce SDK revocation."""
        await self._authorize()
        # Reject names that alias or escape on case-insensitive/Windows filesystems.
        seen: dict[str, str] = {}
        for path in self._files:
            parts = path.split("/")
            for i, part in enumerate(parts):
                if (
                    any(c in part for c in '<>:"|?*')
                    or part.endswith((".", " "))
                    or any(ord(c) < 32 for c in part)
                    or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                ):
                    raise ProtocolError("Path cannot be safely materialized on supported platforms")
                prefix = "/".join(parts[: i + 1])
                folded = unicodedata.normalize("NFD", prefix).casefold()
                if folded in seen and seen[folded] != prefix:
                    raise ProtocolError("Case-folding collision during materialization")
                seen[folded] = prefix
        with tempfile.TemporaryDirectory(prefix="skiva-") as root:
            directory = Path(root)
            for file in self._manifest.files:
                target = directory.joinpath(*file.path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(self._blobs[file.digest])
                os.chmod(target, file.mode)
            await self._authorize()
            yield directory


class SkillSession:
    """A binding. Dynamic reconciliation runs on access, never in a hidden background task."""

    def __init__(self, client, task_id, attempt_id, consistency):
        self._client = client
        self.task_id, self.attempt_id = task_id, attempt_id
        self.consistency = consistency
        self._current: SnapshotView | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._checked_at = float("-inf")
        self.last_refresh_error: Exception | None = None
        self.transition_count = 0

    @property
    def ref(self) -> SnapshotRef:
        self._check()
        return self._current.ref

    def _check(self):
        self._client._check()
        if self._closed or self._current is None:
            raise ClosedError("Binding is not active")

    async def refresh(self) -> bool:
        self._check()
        async with self._lock:
            return await self._refresh_locked()

    async def _refresh_locked(self) -> bool:
        self._check()
        try:
            if self.consistency == "pinned":
                await self._current._authorize()
                self._checked_at = time.monotonic()
                self.last_refresh_error = None
                return False
            manifest = await self._client._manifest()
            if (
                manifest.profile_id == self.ref.profile_id
                and manifest.snapshot_id == self.ref.snapshot_id
                and manifest.manifest_digest == self.ref.manifest_digest
            ):
                await self._current._authorize()
                self._checked_at = time.monotonic()
                self.last_refresh_error = None
                return False
            ref = SnapshotRef(
                self._client.server,
                manifest.profile_id,
                manifest.snapshot_id,
                manifest.manifest_digest,
            )
            candidate = await self._client._prepare(ref, owner=self)
            latest = await self._client._manifest()
            if (
                latest.snapshot_id != ref.snapshot_id
                or latest.profile_id != ref.profile_id
                or latest.manifest_digest != ref.manifest_digest
            ):
                raise VersionMismatch(ref.snapshot_id, latest.snapshot_id)
            self._check()
            self._current = candidate  # No await between final check and publication.
            self.transition_count += 1
            self._checked_at = time.monotonic()
            self.last_refresh_error = None
            return True
        except Exception as e:
            self.last_refresh_error = e
            # Fail the operation rather than silently serve stale discovery on refresh error.
            raise

    async def _capture(self, *, force: bool = False) -> SnapshotView:
        self._check()
        if self.consistency == "dynamic":
            async with self._lock:
                if force or time.monotonic() - self._checked_at >= self._client.refresh_interval:
                    await self._refresh_locked()
        return self._current

    @asynccontextmanager
    async def snapshot(self):
        view = await self._capture()
        await view._authorize()
        yield view

    async def catalog(self) -> Catalog:
        return await (await self._capture()).catalog()

    async def discovery_prompt(self) -> str:
        return (await self.catalog()).render()

    async def list_skills(self):
        return (await self.catalog()).skills

    async def read_bytes(self, path: str) -> bytes:
        return await (await self._capture()).read_bytes(path)

    async def read_text(self, path: str) -> str:
        return await (await self._capture()).read_text(path)

    async def list_files(self, prefix: str = ""):
        return await (await self._capture()).list_files(prefix)

    async def load_skill(
        self, name: str, *, expected_snapshot_id=None, expected_catalog_digest=None
    ) -> SkillContent:
        view = await self._capture(
            force=expected_snapshot_id is not None or expected_catalog_digest is not None
        )
        return await view.load_skill(
            name,
            expected_snapshot_id=expected_snapshot_id,
            expected_catalog_digest=expected_catalog_digest,
        )

    async def read_skill_file(
        self, path: str, *, expected_snapshot_id=None, expected_catalog_digest=None
    ) -> SkillContent:
        view = await self._capture(
            force=expected_snapshot_id is not None or expected_catalog_digest is not None
        )
        return await view.read_skill_file(
            path,
            expected_snapshot_id=expected_snapshot_id,
            expected_catalog_digest=expected_catalog_digest,
        )
