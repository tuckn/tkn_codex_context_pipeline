"""Explicit, copy-based migration into independent source stores (storage 5).

Original Raw, notes, canonical files, and immutable provenance stay readable.
Only mutable catalogs/state locators are rewritten; old notes keep their bytes.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import AppConfig, CodexSourceConfig, _read_layer, _resolve_paths, validate_source_id
from .initialization import ROOT_KINDS, ROOT_OWNERSHIP_MARKER, _ownership_marker_document
from .provenance import validate_provenance
from .raw_capture import SOURCE_ID_PATTERN, _mirror_relative
from .session_notes import PipelineError, atomic_write_bytes
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


@dataclass(frozen=True)
class SourceStore:
    provider: str
    source_id: str
    raw: Path
    data: Path
    state: Path
    public: Path
    version: int
    metadata: dict[str, Any]
    config_path: Path
    protected_roots: tuple[Path, ...] = ()


def _source_store(path: Path, source_id: str | None = None) -> SourceStore:
    try:
        return _read_source_store(path, source_id)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise PipelineError(f"invalid migration source configuration: {exc}") from exc


def _legacy_app_root() -> Path:
    # Historical omitted roots must never resolve to the renamed application's defaults.
    return Path.home() / ".tkn" / "genai_chat_note_pipeline"


def _legacy_sources(document: dict[str, Any], major: int) -> dict[str, Any]:
    """Read retired acquisition settings only for explicit copy migration."""
    if "sources" in document:
        raise ValueError("legacy config must not contain top-level sources")
    chat = document.pop("chat", {})
    if not isinstance(chat, dict) or set(chat) - {"providers"}:
        raise ValueError("legacy chat must contain only providers")
    providers = chat.get("providers", {})
    if not isinstance(providers, dict) or set(providers) - {"codex", "claude-code", "github-copilot"}:
        raise ValueError("unsupported legacy chat providers")
    sources: dict[str, Any] = {"windows": {}}
    for provider, group in providers.items():
        if not isinstance(group, dict):
            raise ValueError("legacy provider settings must be a mapping")
        if major == 5:
            settings = dict(group)
            identity = settings.pop("source_id", "windows" if provider == "codex" else "windows-" + provider)
            if "home" in settings:
                settings["source_root"] = settings.pop("home")
            entries = {identity: settings}
        else:
            if set(group) - {"sources"}:
                raise ValueError("legacy provider settings must contain only sources")
            entries = group.get("sources", {"windows": {}} if provider == "codex" else {})
        if not isinstance(entries, dict):
            raise ValueError("legacy sources must be a mapping")
        if len({str(key).casefold() for key in entries}) != len(entries):
            raise ValueError("source_id keys must be unique ignoring case")
        for identity, settings in entries.items():
            if not isinstance(identity, str):
                raise ValueError("source_id must be a string")
            validate_source_id(identity)
            if not isinstance(settings, dict):
                raise ValueError("legacy source settings must be a mapping")
            if provider != "codex" and "include_archived" in settings:
                raise ValueError("include_archived is Codex-specific")
            validated = CodexSourceConfig.model_validate({"enabled": provider == "codex", **settings})
            if provider != "codex" and validated.enabled:
                raise ValueError("migration only supports Codex sources; disable other legacy sources")
        if provider == "codex":
            sources = entries
    for identity, settings in sources.items():
        for kind in ("raw", "data", "state"):
            if settings.get(kind + "_root") is None:
                settings[kind + "_root"] = _legacy_app_root() / kind / "codex" / identity
    document["sources"] = sources
    if "cache_root" not in document:
        cache_home = os.getenv("XDG_CACHE_HOME")
        base = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
        document["cache_root"] = base / "genai_chat_note_pipeline"
    return document


def _read_source_store(path: Path, selected_id: str | None = None) -> SourceStore:
    path = path.expanduser().resolve()
    document = _read_layer(path)
    version = str(document.pop("schema_version", ""))
    for field in ("raw_root", "data_root", "state_root", "cache_root", "codex_home"):
        if field in document and (not isinstance(document[field], str) or not document[field].strip()):
            raise ValueError(f"{field} must be a non-empty path string")
    if "source_id" in document and not isinstance(document["source_id"], str):
        raise ValueError("source_id must be a string")
    if not re.fullmatch(r"(?:2|[2-7]\.[0-9]+\.[0-9]+)", version):
        raise PipelineError("migration source requires a standalone schema-2/3/4/5/6/7 configuration")
    major, minor = (2, 0) if version == "2" else tuple(int(part) for part in version.split(".")[:2])
    if minor > {2: 2, 3: 0, 4: 1, 5: 0, 6: 0, 7: 0}[major]:
        raise PipelineError("unsupported migration source config version")
    if major in {5, 6, 7}:
        if major in {5, 6}:
            document = _legacy_sources(document, major)
        config = AppConfig.model_validate(_resolve_paths(document, path.parent))
        if selected_id is not None:
            config = config.for_source(selected_id)
        metadata = read_json(config.state_root / "pipeline.json")
        if metadata.get("storageVersion") != STORAGE_VERSION:
            raise PipelineError("source config 5/6/7 requires a completed storage-5 store")
        receipt = read_json(config.state_root / "migration.json")
        if receipt and receipt.get("status") != "complete":
            raise PipelineError("source store migration is incomplete")
        validate_storage(config, path)
        return SourceStore(
            "codex",
            config.source_id,
            config.raw_root,
            config.data_root,
            config.state_root,
            config.data_root,
            STORAGE_VERSION,
            metadata,
            path,
        )
    # This reader is intentionally standalone: destination/global layers cannot redirect the source.
    resolved = _resolve_paths(document, path.parent)
    chat = resolved.get("chat", {}).get("providers", {}).get("codex", {})
    source_id = str(chat.get("source_id") or resolved.get("source_id") or "windows")
    if not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise PipelineError("unsafe source_id in migration source")
    roots = {kind: Path(resolved.get(kind + "_root") or _legacy_app_root() / kind) for kind in ("raw", "data", "state")}
    scoped_state = roots["state"] / "codex" / source_id
    metadata = read_json(scoped_state / "pipeline.json")
    if metadata:
        storage = metadata.get("storageVersion")
        if storage not in {3, 4}:
            raise PipelineError("legacy source namespace requires storage 3 or 4")
        raw = roots["raw"] / "codex" / source_id
        data = roots["data"] / "codex" / source_id
        state = scoped_state
    else:
        metadata = read_json(roots["state"] / "pipeline.json")
        storage = metadata.get("storageVersion", 2)
        if storage != 2:
            raise PipelineError("select the source_id of the legacy store before migration")
        raw, data, state = roots["raw"] / source_id, roots["data"], roots["state"]
    if metadata and (metadata.get("sourceId") != source_id or metadata.get("sourceProvider", "codex") != "codex"):
        raise PipelineError("source configuration and storage identity differ")
    if not metadata and not (raw / "manifest.jsonl").is_file():
        raise PipelineError("no source pipeline metadata or Raw manifest was found")
    return SourceStore(
        "codex", source_id, raw, data, state, roots["data"], int(storage), metadata, path, tuple(roots.values())
    )


def _local_ref(value: str, source: SourceStore) -> str:
    if source.version >= 3:
        prefix = f"data:/{source.provider}/{source.source_id}/"
        if value.startswith(prefix):
            value = "data:/" + value[len(prefix) :]
    if value.startswith("data:/thread-notes/"):
        value = "data:/session-notes/" + value[len("data:/thread-notes/") :]
    return value


def _state_value(value: Any, source: SourceStore, config: AppConfig) -> Any:
    if isinstance(value, dict):
        return {
            key.replace("threadNote", "sessionNote").replace("ThreadNote", "SessionNote"): (
                str(config.sessions_root) if key == "sourceRoot" else _state_value(item, source, config)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_state_value(item, source, config) for item in value]
    if not isinstance(value, str):
        return value
    value = _local_ref(value, source)
    if source.version == 2 and value.startswith(f"raw:/{source.source_id}/"):
        value = f"raw:/{source.provider}/{source.source_id}/" + value[len(f"raw:/{source.source_id}/") :]
    for old, new in ((source.raw, config.raw_root), (source.data, config.data_root), (source.state, config.state_root)):
        for before, after in ((str(old), str(new)), (old.as_posix(), new.as_posix())):
            if value == before or value.startswith((before + "/", before + "\\")):
                value = after + value[len(before) :]
                return value.replace("thread-notes", "session-notes")
    return value


def _plan_source(source: SourceStore, config: AppConfig) -> list[MigrationFile]:
    try:
        return _build_source_plan(source, config)
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise PipelineError(f"invalid migration source data: {exc}") from exc


def _build_source_plan(source: SourceStore, config: AppConfig) -> list[MigrationFile]:
    if (source.provider, source.source_id) != (config.source_provider, config.source_id):
        raise PipelineError("migration source and destination must retain provider and source_id")
    operations: dict[Path, MigrationFile] = {}

    def add(path: Path, root: Path, destination: Path, output: bytes | None = None) -> None:
        _safe_path(path, root)
        content = path.read_bytes()
        operation = MigrationFile(destination, path, sha256(content).hexdigest(), output)
        prior = operations.get(destination)
        if prior is not None and prior.content() != operation.content():
            raise PipelineError(f"migration destinations conflict: {destination}")
        operations[destination] = operation

    # Copy all source-local payloads. Legacy shared data is limited to the source-owned kinds.
    for path in _files(source.raw, source.raw):
        if path.name not in {ROOT_OWNERSHIP_MARKER, ".pipeline.lock"}:
            add(path, source.raw, config.raw_root / path.relative_to(source.raw))
    groups = ("session-notes", "thread-notes", "source-aligned", "threads", "projects", "project-registry.jsonl")
    for name in groups:
        for path in _files(source.data / name, source.data):
            relative = path.relative_to(source.data)
            relative = Path(*(part.replace("thread-notes", "session-notes") for part in relative.parts))
            add(path, source.data, config.data_root / relative)
    for path in _files(source.state, source.state):
        if path.name in {ROOT_OWNERSHIP_MARKER, ".pipeline.lock", "pipeline.json", "migration.json"}:
            continue
        # Historical migration backups are evidence, not mutable checkpoints.
        output = None
        if path.suffix == ".json" and "migrations" not in path.relative_to(source.state).parts:
            output = _json(_state_value(read_json(path), source, config))
        add(path, source.state, config.state_root / path.relative_to(source.state), output)

    manifest = source.raw / "manifest.jsonl"
    if manifest.is_file():
        records = []
        for line in manifest.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            digest = str(record.get("sha256") or "")
            if record.get("sourceId") != source.source_id or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise PipelineError("invalid Raw manifest identity/hash")
            schema = record.get("schemaVersion")
            if schema not in {1, 2, 3} or record.get("sourceProvider", "codex") != source.provider:
                raise PipelineError("unsupported Raw manifest schema/provider")
            raw_relative = (
                f"sha256/{digest[:2]}/{digest}.jsonl"
                if schema == 1
                else _mirror_relative(str(record.get("sourceRef") or ""))
            )
            expected_refs = {f"raw:/{source.provider}/{source.source_id}/{raw_relative}"}
            if schema in {1, 2}:
                expected_refs.add(f"raw:/{source.source_id}/{raw_relative}")
            if record.get("captureRef") not in expected_refs:
                raise PipelineError("Raw manifest capture reference differs from its source identity/path")
            raw = source.raw / raw_relative
            _safe_path(raw, source.raw)
            content = raw.read_bytes()
            if sha256(content).hexdigest() != digest or len(content) != record.get("byteCount"):
                raise PipelineError(f"Raw capture hash/size mismatch: {raw}")
            records.append(
                {
                    **record,
                    "schemaVersion": 1 if schema == 1 else 3,
                    "sourceProvider": source.provider,
                    "captureRef": f"raw:/{source.provider}/{source.source_id}/{raw_relative}",
                }
            )
        manifest_text = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in records)
        operations.pop(config.raw_root / "manifest.jsonl", None)
        add(manifest, source.raw, config.raw_root / "manifest.jsonl", manifest_text.encode())

    def belongs(item: dict[str, Any]) -> bool:
        return (item.get("sourceProvider", source.provider), item.get("sourceId", source.source_id)) == (
            source.provider,
            source.source_id,
        )

    catalog_path = source.public / "catalog/threads.json"
    catalog = read_json(catalog_path)
    rows: list[dict[str, Any]] = []
    if catalog:
        rows = [
            {**_state_value(row, source, config), "sourceProvider": source.provider, "sourceId": source.source_id}
            for row in catalog.get("threads", [])
            if belongs(row)
        ]
        add(catalog_path, source.public, config.data_root / "catalog/threads.json", _json({**catalog, "threads": rows}))
    index_path = source.public / "provenance/index.json"
    index = read_json(index_path)
    if index:
        artifacts = [item for item in index.get("artifacts", []) if belongs(item)]
        identities = {item["id"] for item in artifacts}
        entities: dict[str, dict[str, Any]] = {}
        activity_refs: list[str] = []

        def source_ref(ref: str) -> bool:
            prefixes = (f"raw:/{source.provider}/{source.source_id}/", f"data:/{source.provider}/{source.source_id}/")
            return source.version == 2 or source.version == 5 or ref.startswith(prefixes)

        def retain_entity(entity: dict[str, Any]) -> None:
            key = sha256(f"{entity['id']}\0{entity['sha256']}".encode()).hexdigest()
            entities[key] = entity

        for item in artifacts:
            retain_entity(item)
        for path in _files(source.public / "provenance/activities", source.public):
            value = read_json(path)
            used_generated = [*value.get("used", []), *value.get("generated", [])]
            if not any(
                source_ref(str(entity.get("ref", ""))) or entity.get("id") in identities for entity in used_generated
            ):
                continue
            add(path, source.public, config.data_root / path.relative_to(source.public))
            activity_refs.append("data:/" + path.relative_to(source.public).as_posix())
            for entity in used_generated:
                retain_entity(entity)
        for path in _files(source.public / "provenance/entities", source.public):
            entity = read_json(path)
            if source_ref(str(entity.get("ref", ""))) or entity.get("id") in identities:
                retain_entity(entity)
        for key, entity in entities.items():
            path = source.public / "provenance/entities" / (key + ".json")
            if not path.is_file():
                raise PipelineError(f"missing evidence entity record: {path}")
            add(path, source.public, config.data_root / path.relative_to(source.public))
            ref = str(entity.get("snapshotRef", ""))
            if not ref.startswith("data:/provenance/blobs/"):
                raise PipelineError("unsupported evidence snapshot reference")
            blob = source.public / ref.removeprefix("data:/")
            _safe_path(blob, source.public)
            content = blob.read_bytes()
            if sha256(content).hexdigest() != entity["sha256"] or len(content) != entity.get("byteCount"):
                raise PipelineError(f"corrupt evidence snapshot: {blob}")
            add(blob, source.public, config.data_root / blob.relative_to(source.public))
        current = []
        for item in artifacts:
            item = {
                **item,
                "ref": _local_ref(item["ref"], source),
                "sourceProvider": source.provider,
                "sourceId": source.source_id,
            }
            operation = operations.get(config.data_root / item["ref"].removeprefix("data:/"))
            if item.get("status") == "current" and (
                operation is None or sha256(operation.content()).hexdigest() != item["sha256"]
            ):
                item["status"] = "protected"
                for row in rows:
                    if row.get("noteId") == item["id"]:
                        row.update(status="protected", reason="edited-or-missing-during-migration")
            current.append(item)
        if catalog:
            operations.pop(config.data_root / "catalog/threads.json", None)
            add(
                catalog_path,
                source.public,
                config.data_root / "catalog/threads.json",
                _json({**catalog, "threads": rows}),
            )
        complete = index.get("sourceRuns", {}).get(
            f"{source.provider}/{source.source_id}", index.get("pipelineComplete", False)
        )
        add(
            index_path,
            source.public,
            config.data_root / "provenance/index.json",
            _json(
                {
                    **index,
                    "artifacts": current,
                    "activityRefs": activity_refs,
                    "pipelineComplete": complete,
                    "sourceRuns": {f"{source.provider}/{source.source_id}": complete},
                }
            ),
        )
    aliases = read_json(source.data / "store.json").get("dataRefAliases", {}) if source.version == 5 else {}
    if source.version in {3, 4}:
        aliases = {
            f"{source.provider}/{source.source_id}/thread-notes/": "session-notes/",
            f"{source.provider}/{source.source_id}/": "",
        }
    elif source.version == 2:
        aliases = {"thread-notes/": "session-notes/"}
    descriptor = {
        "schemaVersion": "1.0.0",
        "storageVersion": STORAGE_VERSION,
        "sourceProvider": source.provider,
        "sourceId": source.source_id,
        "rawRefPrefix": f"raw:/{source.provider}/{source.source_id}/",
        "dataRefAliases": aliases,
        "rawRefAliases": (
            read_json(source.data / "store.json").get("rawRefAliases", [])
            if source.version == 5
            else [f"raw:/{source.source_id}/"]
            if source.version == 2
            else []
        ),
    }
    operations[config.data_root / "store.json"] = MigrationFile(
        config.data_root / "store.json", output=_json(descriptor)
    )
    metadata = {
        **source.metadata,
        "storageVersion": STORAGE_VERSION,
        "sourceProvider": source.provider,
        "sourceId": source.source_id,
        "migratedFromStorageVersion": source.version,
    }
    operations[config.state_root / "pipeline.json"] = MigrationFile(
        config.state_root / "pipeline.json", output=_json(metadata)
    )
    return list(operations.values())


def migrate_storage(
    config: AppConfig, *, from_config: Path, dry_run: bool, config_path: Path | None = None
) -> dict[str, Any]:
    source = _source_store(from_config, config.source_id)
    source_config_hash = sha256(source.config_path.read_bytes()).hexdigest()
    roots = validate_storage(config, config_path, allow_legacy=True)
    for root in roots:
        for old in (source.raw, source.data, source.state, source.public, source.config_path, *source.protected_roots):
            left, right = root.resolve(), old.resolve()
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise PipelineError("migration requires fresh destination roots outside all source roots")
    receipt_path = config.state_root / "migration.json"
    if not receipt_path.exists():
        for root in (config.raw_root, config.data_root, config.state_root):
            if root.is_dir() and any(
                item.name not in {ROOT_OWNERSHIP_MARKER, ".pipeline.lock"} for item in root.iterdir()
            ):
                raise PipelineError("migration requires fresh destination roots or its own pending receipt")

    def inspect() -> tuple[list[MigrationFile], dict[str, Any], str]:
        if sha256(source.config_path.read_bytes()).hexdigest() != source_config_hash:
            raise PipelineError("migration source configuration changed")
        if _source_store(from_config, config.source_id) != source:
            raise PipelineError("migration source metadata changed")
        operations = _plan_source(source, config)
        details = []
        for operation in operations:
            root = next(root for root in roots if operation.destination.is_relative_to(root))
            _safe_path(operation.destination, root)
            content = operation.content()
            destination = operation.destination
            details.append(
                {
                    "source": str(operation.source) if operation.source else None,
                    "destination": str(destination),
                    "sha256": sha256(content).hexdigest(),
                    "sourceSha256": operation.source_hash,
                    "bytes": len(content),
                }
            )
        signature = sha256(_json(details)).hexdigest()
        receipt = read_json(receipt_path)
        if receipt and receipt.get("fingerprint") != signature:
            raise PipelineError("migration source or configuration changed; choose new destination roots")
        complete = receipt.get("status") == "complete"
        if not complete:
            for operation in operations:
                path = operation.destination
                if path.exists() and (not path.is_file() or path.read_bytes() != operation.content()):
                    raise PipelineError(f"migration destination conflicts with existing data: {path}")
        return (
            operations,
            {
                "mode": "storage-migrate",
                "dryRun": dry_run,
                "ok": True,
                "status": "already-migrated" if complete else "planned",
                "sourceProvider": source.provider,
                "sourceId": source.source_id,
                "fromStorageVersion": source.version,
                "toStorageVersion": STORAGE_VERSION,
                "preservesOriginalEvidence": True,
                "files": details,
            },
            signature,
        )

    operations, report, signature = inspect()
    if dry_run or report["status"] == "already-migrated":
        return report
    for kind, root in zip(ROOT_KINDS, roots, strict=True):
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ROOT_OWNERSHIP_MARKER
        if not marker.exists():
            atomic_write_bytes(
                marker,
                _json(
                    {
                        **_ownership_marker_document(kind),
                        "sourceProvider": source.provider,
                        "sourceId": source.source_id,
                    }
                ),
            )
    with ExitStack() as stack:
        for root in sorted(roots):
            stack.enter_context(_root_lock(root))
        operations, report, signature = inspect()
        atomic_write_bytes(receipt_path, _json({"fingerprint": signature, "status": "pending"}))
        changed: list[tuple[Path, bytes | None]] = []
        try:
            for operation in operations:
                content = operation.content()
                path = operation.destination
                prior = path.read_bytes() if path.exists() else None
                if prior == content:
                    continue
                changed.append((path, prior))
                atomic_write_bytes(path, content)
                if path.read_bytes() != content:
                    raise PipelineError(f"migration copy verification failed: {path}")
            # Re-scan the source and verify the same complete plan before declaring completion.
            _, _, checked_signature = inspect()
            if checked_signature != signature:
                raise PipelineError("migration source changed before completion")
            if (config.data_root / "provenance/index.json").is_file():
                validate_provenance(config.data_root)
            atomic_write_bytes(receipt_path, _json({"fingerprint": signature, "status": "complete"}))
        except Exception:
            failures = []
            for path, prior in reversed(changed):
                try:
                    if prior is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(path, prior)
                except OSError as exc:
                    failures.append(f"{path}: {exc}")
            if failures:
                raise PipelineError("migration rollback failed: " + "; ".join(failures)) from None
            raise
    return {**report, "status": "migrated"}
