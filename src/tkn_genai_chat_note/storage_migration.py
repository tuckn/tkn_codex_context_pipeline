"""Explicit, copy-based migration from storage v2/v3 to Session Note storage v4.

Original Raw, notes, canonical files, and immutable provenance stay readable.
Only mutable catalogs/state locators are rewritten; old notes keep their bytes.
"""
from __future__ import annotations

import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import AppConfig
from .initialization import ROOT_KINDS, ROOT_OWNERSHIP_MARKER, _ownership_marker_document
from .raw_capture import _mirror_relative
from .session_notes import PipelineError, atomic_write_bytes, now_iso
from .storage import STORAGE_VERSION, _root_lock, read_json, validate_storage


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True)
class MigrationFile:
    destination: Path
    source: Path | None = None
    source_hash: str | None = None
    output: bytes | None = None
    replace_existing: bool = False

    def content(self) -> bytes:
        original = self.source.read_bytes() if self.source is not None else b""
        if self.source_hash is not None and sha256(original).hexdigest() != self.source_hash:
            raise PipelineError(f"migration source changed: {self.source}")
        return self.output if self.output is not None else original


def _safe_path(path: Path, root: Path) -> None:
    if not path.absolute().is_relative_to(root.absolute()):
        raise PipelineError(f"migration path escapes its root: {path}")
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise PipelineError(f"migration path must not contain links: {item}")
        if item == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise PipelineError(f"migration path escapes its resolved root: {path}")


def _files(path: Path, root: Path) -> list[Path]:
    _safe_path(path, root)
    if not path.exists():
        return []
    if path.is_file():
        return [path]
    result = []
    # Inspect each directory before descending, including Windows junctions.
    for child in sorted(path.iterdir()):
        result.extend(_files(child, root))
    return result


def _rewrite(value: Any, config: AppConfig) -> Any:
    if isinstance(value, list):
        return [_rewrite(item, config) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite(item, config) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    prefix = f"{config.source_provider}/{config.source_id}/"
    pairs = [(f"raw:/{config.source_id}/", f"raw:/{prefix}")]
    for kind in ("thread-notes", "source-aligned", "threads", "projects"):
        pairs.append((f"data:/{kind}/", f"data:/{prefix}{kind}/"))
    for old, new in (
        (config.data_root / "thread-notes", config.source_data_root / "thread-notes"),
        (config.data_root / "source-aligned", config.source_data_root / "source-aligned"),
        (config.data_root / "projects", config.source_data_root / "projects"),
        (config.state_root / "threads", config.source_state_root / "threads"),
        (config.state_root / "reports", config.source_state_root / "reports"),
        (config.state_root / "projects", config.source_state_root / "projects"),
    ):
        for old_text, new_text in ((str(old), str(new)), (old.as_posix(), new.as_posix())):
            if value == old_text:
                return new_text
            separator = "\\" if "\\" in old_text else "/"
            pairs.append((old_text + separator, new_text + separator))
    for old_text, new_text in pairs:
        if value.startswith(old_text):
            return new_text + value[len(old_text):]
    return value


def _manifest(content: bytes, config: AppConfig) -> bytes:
    records = []
    for line in content.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise PipelineError("legacy Raw manifest records must be objects")
        version = record.get("schemaVersion")
        if version not in {1, 2} or record.get("sourceId") != config.source_id:
            raise PipelineError("migration requires a schema-1/2 Raw manifest for the configured source_id")
        digest = str(record.get("sha256") or "")
        from .raw_capture import SOURCE_ID_PATTERN

        if len(digest) != 64 or not all(character in "0123456789abcdef" for character in digest):
            raise PipelineError("invalid legacy Raw digest")
        if not SOURCE_ID_PATTERN.fullmatch(config.source_id):
            raise PipelineError("unsafe source_id")
        relative = (
            f"sha256/{digest[:2]}/{digest}.jsonl" if version == 1
            else _mirror_relative(str(record.get("sourceRef") or ""))
        )
        old_ref = f"raw:/{config.source_id}/{relative}"
        if record.get("captureRef") != old_ref:
            raise PipelineError("legacy Raw capture reference does not match its manifest")
        captured = config.raw_root / config.source_id / relative
        _safe_path(captured, config.raw_root)
        if not captured.is_file():
            raise PipelineError(f"legacy Raw capture is missing: {captured}")
        raw = captured.read_bytes()
        if sha256(raw).hexdigest() != digest or len(raw) != record.get("byteCount"):
            raise PipelineError(f"legacy Raw capture hash/size mismatch: {captured}")
        records.append({
            **record, "schemaVersion": 1 if version == 1 else 3,
            "sourceProvider": config.source_provider,
            "captureRef": f"raw:/{config.source_provider}/{config.source_id}/{relative}",
        })
    return "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    ).encode("utf-8")


def _plan_v2(config: AppConfig) -> tuple[list[MigrationFile], bool]:
    legacy_path = config.state_root / "pipeline.json"
    _safe_path(legacy_path, config.state_root)
    legacy = read_json(legacy_path)
    current = read_json(config.source_state_root / "pipeline.json")
    if current:
        if current.get("storageVersion") != STORAGE_VERSION or current.get("sourceId") != config.source_id:
            raise PipelineError("destination namespace has incompatible storage metadata")
        if current.get("sourceProvider") != config.source_provider:
            raise PipelineError("destination namespace has a different provider")
        if not legacy or legacy.get("layout") == "provider-source":
            return [], True
    if legacy and legacy.get("layout") == "provider-source":
        return [], False
    if legacy and (legacy.get("storageVersion") != 2 or legacy.get("sourceId") != config.source_id):
        raise PipelineError("select the source_id of the legacy schema-2 store before migration")
    raw_manifest = config.raw_root / config.source_id / "manifest.jsonl"
    if not legacy and not raw_manifest.exists():
        if any((config.data_root / name).exists() for name in ("thread-notes", "project-registry.jsonl")):
            raise PipelineError("cannot infer legacy data ownership without schema-2 pipeline metadata")
        return [], False
    if not legacy and any((config.data_root / name).exists() for name in ("thread-notes", "source-aligned")):
        raise PipelineError("cannot migrate legacy data without its pipeline metadata")
    operations = []

    def add(source: Path, destination: Path, output: bytes | None = None, *, replace: bool = False) -> None:
        content = source.read_bytes()
        operations.append(MigrationFile(destination, source, sha256(content).hexdigest(), output, replace))

    groups = [
        (config.raw_root / config.source_id, config.source_raw_root, config.raw_root, "raw"),
        *[(config.data_root / name, config.source_data_root / name, config.data_root, "data")
          for name in ("thread-notes", "source-aligned", "threads", "projects", "project-registry.jsonl")],
        *[(config.state_root / name, config.source_state_root / name, config.state_root, "state")
          for name in ("threads", "normalization", "reports", "ledger.json", "last-run.json", "projects")],
        *[(config.cache_root / name, config.source_cache_root / name, config.cache_root, "cache")
          for name in ("pending", "rebuild", "runs")],
    ]
    for old, new, root, kind in groups:
        for source in _files(old, root):
            destination = new / source.relative_to(old) if old.is_dir() else new
            output = None
            if source == raw_manifest:
                output = _manifest(source.read_bytes(), config)
            elif kind in {"state", "cache"} and source.suffix == ".json":
                output = _json(_rewrite(read_json(source), config))
            add(source, destination, output)
    backup = config.source_state_root / "migrations" / "storage-v2-backup"
    for relative in ("catalog/threads.json", "provenance/index.json"):
        source = config.data_root / relative
        if not source.is_file():
            continue
        _safe_path(source, config.data_root)
        saved = backup / relative
        _safe_path(saved, config.state_root)
        original = saved.read_bytes() if saved.is_file() else source.read_bytes()
        value = json.loads(original.decode("utf-8-sig"))
        items_key = "threads" if relative.startswith("catalog/") else "artifacts"
        for item in value.get(items_key, []):
            if item.get("sourceProvider", config.source_provider) != config.source_provider:
                raise PipelineError("legacy catalog contains another provider")
            if item.get("sourceId", config.source_id) != config.source_id:
                raise PipelineError("legacy catalog contains another source")
            item.update(sourceProvider=config.source_provider, sourceId=config.source_id)
        if items_key == "artifacts":
            value["sourceRuns"] = {
                f"{config.source_provider}/{config.source_id}": bool(value.get("pipelineComplete", False))
            }
        output = _json(_session_names(_rewrite(value, config)))
        if saved.exists():
            if source.read_bytes() not in (original, output):
                raise PipelineError(f"shared catalog changed after migration began: {source}")
        else:
            add(source, saved)
        add(source, source, output, replace=True)
    if legacy:
        add(legacy_path, backup / "pipeline.json")
    created = legacy.get("createdAt") or now_iso()
    operations.append(MigrationFile(config.source_state_root / "pipeline.json", output=_json({
        "storageVersion": STORAGE_VERSION, "sourceProvider": config.source_provider,
        "sourceId": config.source_id, "createdAt": created, "migratedFromStorageVersion": 2,
    }), replace_existing=bool(current)))
    # Retire the old entry point so v0.9 cannot resume writing the old layout.
    retired = _json({
        "storageVersion": STORAGE_VERSION, "layout": "provider-source",
        "migratedSourceProvider": config.source_provider, "migratedSourceId": config.source_id,
    })
    if legacy:
        add(legacy_path, legacy_path, retired, replace=True)
    else:
        operations.append(MigrationFile(legacy_path, output=retired))
    return operations, False


def _session_names(value: Any, config: AppConfig | None = None) -> Any:
    """Rewrite mutable artifact locators; immutable evidence keeps its bytes."""
    if isinstance(value, list):
        return [_session_names(item, config) for item in value]
    if isinstance(value, dict):
        return {key.replace("threadNote", "sessionNote").replace("ThreadNote", "SessionNote"):
                _session_names(item, config) for key, item in value.items()}
    if isinstance(value, str):
        if config is not None and value.startswith("data:/") and not value.startswith(
            f"data:/{config.source_provider}/{config.source_id}/"
        ):
            return value
        if config is not None and not value.startswith("data:/") and (
            ":\\" in value or ":/" in value or value.startswith("/")
        ):
            roots = (config.source_data_root, config.source_state_root)
            prefixes = [text for root in roots for text in (str(root), root.as_posix())]
            if not any(value == text or value.startswith((text + "/", text + "\\")) for text in prefixes):
                return value
        return re.sub(r"(^|[/\\])thread-notes(?=[/\\]|$)", r"\1session-notes", value)
    return value


def _session_destination(path: Path, config: AppConfig) -> Path:
    if not path.is_relative_to(config.source_data_root):
        return path
    relative = path.relative_to(config.source_data_root)
    return config.source_data_root.joinpath(*(
        "session-notes" if part == "thread-notes" else part for part in relative.parts
    ))


def _plan_v3(config: AppConfig, current: dict[str, Any]) -> tuple[list[MigrationFile], bool]:
    if current.get("sourceId") != config.source_id or current.get("sourceProvider") != config.source_provider:
        raise PipelineError("destination namespace has incompatible source identity")
    operations: list[MigrationFile] = []
    backup = config.source_state_root / "migrations" / "session-note-v4-backup"
    # Copy notes, including project-assigned notes. Originals and note IDs survive.
    for directory in (config.source_data_root / "thread-notes", config.projects_data_root):
        for source in _files(directory, config.data_root):
            destination = _session_destination(source, config)
            if destination != source:
                operations.append(MigrationFile(destination, source, sha256(source.read_bytes()).hexdigest()))
    mutable = [config.data_root / "catalog/threads.json", config.data_root / "provenance/index.json",
               config.source_data_root / "project-registry.jsonl",
               config.source_state_root / "ledger.json", config.source_state_root / "last-run.json"]
    for directory in (config.source_state_root / "threads", config.source_state_root / "projects"):
        mutable.extend(path for path in _files(directory, config.state_root) if path.suffix == ".json")
    # The pipeline version is the final write, so interrupted copies can be resumed.
    mutable.append(config.source_state_root / "pipeline.json")
    for source in mutable:
        if not source.exists():
            continue
        root = config.data_root if source.is_relative_to(config.data_root) else config.state_root
        _safe_path(source, root)
        saved = backup / ("data" if root == config.data_root else "state") / source.relative_to(root)
        _safe_path(saved, config.state_root)
        original = saved.read_bytes() if saved.exists() else source.read_bytes()
        if source.suffix == ".jsonl":
            records = [_session_names(json.loads(line), config) for line in original.decode("utf-8-sig").splitlines()
                       if line.strip()]
            output = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records).encode("utf-8")
        else:
            value = _session_names(json.loads(original.decode("utf-8-sig")), config)
            if source == config.source_state_root / "pipeline.json":
                value.update(storageVersion=STORAGE_VERSION, migratedFromStorageVersion=3)
            output = _json(value)
        if saved.exists() and source.read_bytes() not in (original, output):
            raise PipelineError(f"mutable state changed after migration began: {source}")
        if not saved.exists():
            operations.append(MigrationFile(saved, source, sha256(source.read_bytes()).hexdigest()))
        operations.append(MigrationFile(source, source, sha256(source.read_bytes()).hexdigest(), output, True))
    return operations, False


def _plan(config: AppConfig) -> tuple[list[MigrationFile], bool]:
    current = read_json(config.source_state_root / "pipeline.json")
    if current.get("storageVersion") == 3:
        return _plan_v3(config, current)
    operations, migrated = _plan_v2(config)
    updated = []
    for operation in operations:
        destination = _session_destination(operation.destination, config)
        output = operation.output
        # v2 backups and immutable provenance are historical evidence.
        if "migrations" not in destination.parts:
            if output is not None and destination.suffix == ".json":
                output = _json(_session_names(json.loads(output)))
            elif destination.name == "project-registry.jsonl":
                records = [_session_names(json.loads(line)) for line in operation.content().decode(
                    "utf-8-sig").splitlines() if line.strip()]
                output = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records).encode("utf-8")
        updated.append(replace(operation, destination=destination, output=output))
    return updated, migrated


def migrate_storage(config: AppConfig, *, dry_run: bool, config_path: Path | None = None) -> dict[str, Any]:
    roots = validate_storage(config, config_path, allow_legacy=True)

    def inspect() -> tuple[list[MigrationFile], dict[str, Any]]:
        operations, migrated = _plan(config)
        details = []
        for operation in operations:
            destination = operation.destination
            root = next((root for root in roots if destination.absolute().is_relative_to(root.absolute())), None)
            if root is None:
                raise PipelineError(f"migration target escapes configured roots: {destination}")
            _safe_path(destination, root)
            if operation.source is not None:
                source_root = next(
                    root for root in roots if operation.source.absolute().is_relative_to(root.absolute())
                )
                _safe_path(operation.source, source_root)
            content = operation.content()
            if destination.exists() and not destination.is_file():
                raise PipelineError(f"migration destination is not a file: {destination}")
            same = destination.is_file() and destination.read_bytes() == content
            if destination.exists() and not same and not operation.replace_existing:
                raise PipelineError(f"migration destination conflicts with existing data: {destination}")
            details.append({
                "source": str(operation.source) if operation.source else None,
                "destination": str(destination), "bytes": len(content),
                "sha256": sha256(content).hexdigest(), "action": "unchanged" if same else "write",
            })
        return operations, {
            "mode": "storage-migrate", "dryRun": dry_run, "ok": True,
            "status": "already-migrated" if migrated else "planned" if operations else "no-legacy-storage",
            "sourceProvider": config.source_provider, "sourceId": config.source_id,
            "fromStorageVersion": 2, "toStorageVersion": STORAGE_VERSION,
            "preservesOriginalEvidence": True, "files": details,
        }

    operations, report = inspect()
    if dry_run or not operations:
        return report
    for kind, root in zip(ROOT_KINDS, roots, strict=True):
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ROOT_OWNERSHIP_MARKER
        if not marker.exists():
            atomic_write_bytes(marker, _json(_ownership_marker_document(kind)))
    with ExitStack() as stack:
        for root in sorted(roots):
            stack.enter_context(_root_lock(root))
        operations, report = inspect()
        changed: list[tuple[Path, bytes | None]] = []
        try:
            for operation in operations:
                content = operation.content()
                prior = operation.destination.read_bytes() if operation.destination.exists() else None
                if prior == content:
                    continue
                # Record rollback before replacing in case an I/O error follows replacement.
                changed.append((operation.destination, prior))
                atomic_write_bytes(operation.destination, content)
                if operation.destination.read_bytes() != content:
                    raise PipelineError(f"migration copy verification failed: {operation.destination}")
        except Exception:
            failures = []
            for destination, prior in reversed(changed):
                try:
                    if prior is None:
                        destination.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(destination, prior)
                except OSError as exc:
                    failures.append(f"{destination}: {exc}")
            if failures:
                raise PipelineError("migration rollback failed: " + "; ".join(failures)) from None
            raise
        report["status"] = "migrated"
    return report
