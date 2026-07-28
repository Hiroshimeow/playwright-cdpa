from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from .errors import ConflictingIdentityError, CorruptStateError, InvalidInputError
from .locking import FileLock
from .targets import ChatTarget

_PROJECT_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class ProjectMemoryScope(str, Enum):
    DEFAULT = "default"
    PROJECT_ONLY = "project_only"


@dataclass(frozen=True, slots=True)
class ProjectRef:
    project_id: str
    canonical_url: str
    name: str
    memory_scope: ProjectMemoryScope = ProjectMemoryScope.DEFAULT

    def __post_init__(self) -> None:
        target = ChatTarget.project(self.project_id)
        if self.canonical_url != target.canonical_url:
            raise InvalidInputError("project canonical URL does not match project ID")
        _validate_project_name(self.name)
        if not isinstance(self.memory_scope, ProjectMemoryScope):
            raise InvalidInputError("invalid project memory scope")

    @property
    def target(self) -> ChatTarget:
        return ChatTarget.project(self.project_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "canonical_url": self.canonical_url,
            "name": self.name,
            "memory_scope": self.memory_scope.value,
        }

    @classmethod
    def from_dict(cls, value: object) -> ProjectRef:
        if not isinstance(value, dict) or set(value) != {
            "project_id",
            "canonical_url",
            "name",
            "memory_scope",
        }:
            raise CorruptStateError("invalid project identity record")
        try:
            return cls(
                project_id=str(value["project_id"]),
                canonical_url=str(value["canonical_url"]),
                name=str(value["name"]),
                memory_scope=ProjectMemoryScope(str(value["memory_scope"])),
            )
        except (TypeError, ValueError, InvalidInputError) as exc:
            raise CorruptStateError("invalid project identity record") from exc


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    key: str
    name: str
    memory_scope: ProjectMemoryScope
    creation_unknown: bool
    project: ProjectRef | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "key": self.key,
            "name": self.name,
            "memory_scope": self.memory_scope.value,
            "creation_unknown": self.creation_unknown,
            "project": None if self.project is None else self.project.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> ProjectRecord:
        expected = {
            "schema_version",
            "key",
            "name",
            "memory_scope",
            "creation_unknown",
            "project",
        }
        if not isinstance(value, dict) or set(value) != expected or value["schema_version"] != 1:
            raise CorruptStateError("invalid project key record")
        key = _validate_project_key(value["key"])
        name = _validate_project_name(value["name"])
        try:
            scope = ProjectMemoryScope(value["memory_scope"])
        except (TypeError, ValueError) as exc:
            raise CorruptStateError("invalid project memory scope") from exc
        if type(value["creation_unknown"]) is not bool:
            raise CorruptStateError("invalid project creation state")
        project = None if value["project"] is None else ProjectRef.from_dict(value["project"])
        if project is not None and (project.name != name or project.memory_scope is not scope):
            raise CorruptStateError("project identity does not match key record")
        return cls(key, name, scope, value["creation_unknown"], project)


class ProjectCreationGuard:
    """Deployment-scoped metadata claim for the irreversible Create boundary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.records = self.root / "project-creation-claims"
        self.locks = self.root / "project-creation-locks"

    def lock(self, key: str, *, timeout: float = 0.0) -> FileLock:
        key = _validate_project_key(key)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return FileLock(self.locks / f"{digest}.lock", timeout=timeout)

    def load(self, key: str) -> tuple[str, ProjectMemoryScope] | None:
        key = _validate_project_key(key)
        path = self._path(key)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "key",
                "name",
                "memory_scope",
            }:
                raise ValueError("invalid fields")
            if value["schema_version"] != 1 or _validate_project_key(value["key"]) != key:
                raise ValueError("invalid identity")
            name = _validate_project_name(value["name"])
            scope = ProjectMemoryScope(value["memory_scope"])
            return name, scope
        except CorruptStateError:
            raise
        except Exception as exc:
            raise CorruptStateError("corrupt deployment project creation claim") from exc

    def claim(
        self, *, key: str, name: str, memory_scope: ProjectMemoryScope
    ) -> None:
        key = _validate_project_key(key)
        name = _validate_project_name(name)
        if not isinstance(memory_scope, ProjectMemoryScope):
            raise InvalidInputError("invalid project memory scope")
        current = self.load(key)
        if current is not None:
            if current != (name, memory_scope):
                raise ConflictingIdentityError("project key is bound to different metadata")
            return
        _atomic_write(
            self._path(key),
            {
                "schema_version": 1,
                "key": key,
                "name": name,
                "memory_scope": memory_scope.value,
            },
        )

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.records / f"{digest}.json"


class ProjectRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.records = self.root / "projects"
        self.locks = self.root / "project-locks"

    def load(self, key: str) -> ProjectRecord | None:
        key = _validate_project_key(key)
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return ProjectRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except CorruptStateError:
            raise
        except Exception as exc:
            raise CorruptStateError("corrupt project key record") from exc

    def begin_creation(
        self, *, key: str, name: str, memory_scope: ProjectMemoryScope
    ) -> ProjectRecord:
        return self.claim_creation(
            key=key,
            name=name,
            memory_scope=memory_scope,
        )[0]

    def claim_creation(
        self, *, key: str, name: str, memory_scope: ProjectMemoryScope
    ) -> tuple[ProjectRecord, bool]:
        key = _validate_project_key(key)
        name = _validate_project_name(name)
        if not isinstance(memory_scope, ProjectMemoryScope):
            raise InvalidInputError("invalid project memory scope")
        with self._lock(key):
            current = self.load(key)
            if current is not None:
                if current.name != name or current.memory_scope is not memory_scope:
                    raise ConflictingIdentityError("project key is bound to different metadata")
                return current, False
            record = ProjectRecord(key, name, memory_scope, True, None)
            _atomic_write(self._path(key), record.to_dict())
            return record, True

    def resolve(self, key: str, project: ProjectRef) -> ProjectRecord:
        key = _validate_project_key(key)
        with self._lock(key):
            current = self.load(key)
            if current is None:
                raise ConflictingIdentityError("project key has no creation record")
            if current.name != project.name or current.memory_scope is not project.memory_scope:
                raise ConflictingIdentityError("project identity differs from key metadata")
            if current.project is not None and current.project != project:
                raise ConflictingIdentityError("project key is already bound to another project")
            resolved = ProjectRecord(key, current.name, current.memory_scope, False, project)
            _atomic_write(self._path(key), resolved.to_dict())
            return resolved

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.records / f"{digest}.json"

    def _lock(self, key: str) -> FileLock:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return FileLock(self.locks / f"{digest}.lock", timeout=2.0)

def _atomic_write(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def select_exact_project(
    projects: Iterable[ProjectRef], *, name: str, project_id: str | None = None
) -> ProjectRef | None:
    name = _validate_project_name(name)
    matches = [
        project
        for project in projects
        if project.name == name and (project_id is None or project.project_id == project_id)
    ]
    if len(matches) > 1:
        raise ConflictingIdentityError("multiple exact projects matched")
    return matches[0] if matches else None


def _validate_project_key(value: object) -> str:
    if type(value) is not str or not _PROJECT_KEY.fullmatch(value):
        raise InvalidInputError("project key must contain 1-160 path-safe characters")
    return value


def _validate_project_name(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > 120:
        raise InvalidInputError("project name must contain 1-120 characters")
    if any(ord(character) < 0x20 for character in value):
        raise InvalidInputError("project name contains control characters")
    return value
