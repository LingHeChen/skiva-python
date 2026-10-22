"""Immutable models and byte-compatible Skiva Go manifest validation."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .errors import IntegrityError, LimitExceeded, ProtocolError


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def valid_path(value: str) -> bool:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return False
    return (
        0 < size <= 4096
        and unicodedata.normalize("NFC", value) == value
        and not any(c in value for c in "\\\x00\r\n")
        and not value.startswith("/")
        and value not in (".", "..")
        and not value.startswith("../")
        and posixpath.normpath(value) == value
    )


def canonical_json(value: Any) -> bytes:
    # encoding/json escapes HTML and U+2028/U+2029 even with UTF-8 output.
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for char, escaped in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(char, escaped)
    return text.encode("utf-8")


@dataclass(frozen=True)
class Limits:
    max_files: int = 10_000
    max_file_bytes: int = 8 * 1024 * 1024
    max_snapshot_bytes: int = 64 * 1024 * 1024
    max_manifest_bytes: int = 8 * 1024 * 1024
    max_skill_bytes: int = 256 * 1024

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise ValueError("Limits must be positive integers")


@dataclass(frozen=True)
class SnapshotRef:
    server: str
    profile_id: str
    snapshot_id: str
    manifest_digest: str


@dataclass(frozen=True)
class RetryCheckpoint:
    """Explicit retry provenance; contains no credentials or authorization grant."""

    task_id: str
    attempt_id: str
    snapshot: SnapshotRef

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in (self.task_id, self.attempt_id)):
            raise ValueError("Checkpoint IDs must be non-empty strings")
        if not isinstance(self.snapshot, SnapshotRef):
            raise TypeError("Checkpoint snapshot must be a SnapshotRef")


@dataclass(frozen=True)
class File:
    path: str
    type: str
    size: int
    digest: str
    mode: int

    def wire(self) -> dict:
        return dict(
            path=self.path, type=self.type, size=self.size, digest=self.digest, mode=self.mode
        )


@dataclass(frozen=True)
class Manifest:
    snapshot_id: str
    manifest_digest: str
    profile_id: str
    generation: int
    files: tuple[File, ...]

    @classmethod
    def parse(cls, data: Any, limits: Limits) -> Manifest:
        try:
            if not isinstance(data, dict) or type(data["schemaVersion"]) is not int:
                raise ValueError
            if data["schemaVersion"] != 1:
                raise ValueError
            for key in ("snapshotId", "profileId", "manifestDigest"):
                if not isinstance(data[key], str) or not data[key]:
                    raise ValueError
            if type(data["generation"]) is not int or data["generation"] < 0:
                raise ValueError
            raw = data["files"]
            if not isinstance(raw, list):
                raise ValueError
            if len(raw) > limits.max_files:
                raise LimitExceeded("Manifest has too many files")
            files = []
            for item in raw:
                f = File(**{k: item[k] for k in ("path", "type", "size", "digest", "mode")})
                if (
                    not isinstance(f.path, str)
                    or not valid_path(f.path)
                    or f.type != "file"
                    or type(f.mode) is not int
                    or f.mode not in (292, 365)
                    or type(f.size) is not int
                    or f.size < 0
                    or not isinstance(f.digest, str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", f.digest)
                ):
                    raise ValueError
                if f.size > limits.max_file_bytes:
                    raise LimitExceeded("File exceeds size limit")
                files.append(f)
            files.sort(key=lambda f: f.path.encode("utf-8"))
            seen = set()
            for f in files:
                parents = f.path.split("/")[:-1]
                if f.path in seen or any(
                    "/".join(parents[:i]) in seen for i in range(1, len(parents) + 1)
                ):
                    raise ValueError
                seen.add(f.path)
            if sum(f.size for f in files) > limits.max_snapshot_bytes:
                raise LimitExceeded("Snapshot exceeds size limit")
            if digest(canonical_json([f.wire() for f in files])) != data["manifestDigest"]:
                raise IntegrityError("Manifest digest mismatch")
            return cls(
                data["snapshotId"],
                data["manifestDigest"],
                data["profileId"],
                data["generation"],
                tuple(files),
            )
        except (KeyError, TypeError, ValueError, UnicodeError) as e:
            raise ProtocolError("Invalid distribution manifest") from e
