"""Portable, versioned entities and generation activities for downstream consumers.

This is a JSON contract. RDF identifiers and ontology mappings belong downstream.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .frontmatter import parse_simple_frontmatter
from .storage import read_json
from .thread_notes import PipelineError, atomic_write_bytes, atomic_write_json, now_iso

PROVENANCE_SCHEMA_VERSION = "1.0.0"


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def immutable_bytes(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise PipelineError(f"immutable output must not be a symbolic link: {path}")
    if path.exists():
        if path.read_bytes() != content:
            raise PipelineError(f"immutable output differs from expected bytes: {path}")
        return
    atomic_write_bytes(path, content)


class ProvenanceStore:
    def __init__(self, data_root: Path, *, dry_run: bool = False) -> None:
        self.data_root = data_root
        self.root = data_root / "provenance"
        self.dry_run = dry_run

    def entity(
        self,
        content: bytes,
        *,
        identity: str,
        ref: str,
        kind: str,
        media_type: str = "application/json",
    ) -> dict[str, Any]:
        digest = sha256(content).hexdigest()
        snapshot = self.root / "blobs" / digest[:2] / digest
        entity_key = sha256(f"{identity}\0{digest}".encode()).hexdigest()
        record: dict[str, Any] = {
            "schemaVersion": PROVENANCE_SCHEMA_VERSION,
            "id": identity,
            "version": f"sha256:{digest}",
            "sha256": digest,
            "ref": ref,
            "kind": kind,
            "mediaType": media_type,
            "byteCount": len(content),
            "snapshotRef": "data:/" + snapshot.relative_to(self.data_root).as_posix(),
        }
        if not self.dry_run:
            immutable_bytes(snapshot, content)
            # Location can change while logical identity and bytes stay the same.
            entity_path = self.root / "entities" / f"{entity_key}.json"
            prior = read_json(entity_path)
            if not prior:
                immutable_bytes(entity_path, json_bytes(record))
        return record

    def artifact(self, path: Path) -> dict[str, Any]:
        content = path.read_bytes()
        metadata = parse_simple_frontmatter(content.decode("utf-8-sig"))
        identity = metadata.get("id")
        if not identity:
            raise PipelineError(f"artifact has no stable id: {path}")
        return self.entity(
            content,
            identity=identity,
            ref="data:/" + path.relative_to(self.data_root).as_posix(),
            kind=metadata.get("type") or "artifact",
            media_type="text/markdown",
        )

    def activity(
        self,
        *,
        run_id: str,
        stage: str,
        subject: str,
        started_at: str,
        used: list[dict[str, Any]],
        generated: list[dict[str, Any]],
        agent: dict[str, Any],
        status: str = "completed",
    ) -> str:
        activity_id = str(uuid4())
        record = {
            "schemaVersion": PROVENANCE_SCHEMA_VERSION,
            "id": activity_id,
            "runId": run_id,
            "stage": stage,
            "subject": subject,
            "startedAt": started_at,
            "endedAt": now_iso(),
            "status": status,
            "agent": agent,
            "used": used,
            "generated": generated,
            "relations": [
                {
                    "type": "derivedFrom",
                    "from": {"id": output["id"], "version": output["version"]},
                    "to": {"id": source["id"], "version": source["version"]},
                }
                for output in generated
                for source in used
            ],
        }
        if not self.dry_run:
            immutable_bytes(self.root / "activities" / f"{activity_id}.json", json_bytes(record))
        return activity_id

    def has_activity(self, activity_id: str | None) -> bool:
        return bool(activity_id and (self.root / "activities" / f"{activity_id}.json").is_file())

    def publish_index(self, artifacts: list[dict[str, Any]], *, run_id: str, complete: bool = False) -> None:
        if self.dry_run:
            return
        ids = [artifact["id"] for artifact in artifacts]
        if len(ids) != len(set(ids)):
            raise PipelineError("duplicate artifact id in downstream index")
        current_ids = set(ids)
        previous = read_json(self.root / "index.json").get("artifacts", [])
        artifacts = [
            *artifacts,
            *[{**artifact, "status": "inactive"} for artifact in previous if artifact["id"] not in current_ids],
        ]
        atomic_write_json(
            self.root / "index.json",
            {
                "schemaVersion": PROVENANCE_SCHEMA_VERSION,
                "runId": run_id,
                "asOf": now_iso(),
                "pipelineComplete": complete,
                "artifacts": artifacts,
                "activityRefs": [
                    "data:/" + path.relative_to(self.data_root).as_posix()
                    for path in sorted((self.root / "activities").glob("*.json"))
                ],
            },
        )


def validate_provenance(data_root: Path) -> dict[str, Any]:
    try:
        return _validate_provenance(data_root)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PipelineError(f"invalid provenance structure: {exc}") from exc


def _validate_provenance(data_root: Path) -> dict[str, Any]:
    """Verify retained snapshots, activity edges, and advertised current artifacts."""
    index_path = data_root / "provenance" / "index.json"
    if not index_path.is_file():
        raise PipelineError("provenance index not found; run clone or pull first")
    index = read_json(index_path)
    if index.get("schemaVersion") != PROVENANCE_SCHEMA_VERSION:
        raise PipelineError("unsupported provenance index schema")
    checked_snapshots: set[tuple[str, str]] = set()

    def resolve(ref: str) -> Path:
        if not ref.startswith("data:/"):
            raise PipelineError(f"unsupported snapshot reference: {ref}")
        path = (data_root / ref.removeprefix("data:/")).resolve()
        if not path.is_relative_to(data_root.resolve()):
            raise PipelineError(f"snapshot reference escapes data root: {ref}")
        return path

    def entity_check(entity: dict[str, Any]) -> None:
        if entity.get("schemaVersion") != PROVENANCE_SCHEMA_VERSION:
            raise PipelineError("unsupported provenance entity schema")
        digest = entity.get("sha256", "")
        if not entity.get("id") or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise PipelineError("invalid provenance entity identity/hash")
        if entity.get("version") != f"sha256:{digest}":
            raise PipelineError("entity version and hash disagree")
        ref = str(entity.get("snapshotRef") or "")
        key = (ref, digest)
        if key not in checked_snapshots:
            path = resolve(ref)
            if (
                not path.is_file()
                or sha256(path.read_bytes()).hexdigest() != digest
                or path.stat().st_size != entity.get("byteCount")
            ):
                raise PipelineError(f"missing or corrupt evidence snapshot: {ref}")
            checked_snapshots.add(key)

    artifacts = index.get("artifacts", [])
    identities: set[str] = set()
    for artifact in artifacts:
        entity_check(artifact)
        if artifact["id"] in identities:
            raise PipelineError("duplicate artifact id in provenance index")
        identities.add(artifact["id"])
        if artifact.get("status") == "current":
            current = resolve(artifact["ref"])
            if not current.is_file() or sha256(current.read_bytes()).hexdigest() != artifact["sha256"]:
                raise PipelineError(f"current artifact changed since the indexed run: {artifact['ref']}")
    for ref in index.get("activityRefs", []):
        activity = read_json(resolve(ref))
        if activity.get("schemaVersion") != PROVENANCE_SCHEMA_VERSION:
            raise PipelineError(f"unsupported or missing activity: {ref}")
        try:
            UUID(activity["id"])
            UUID(activity["runId"])
        except (KeyError, ValueError) as exc:
            raise PipelineError(f"invalid activity identity: {ref}") from exc
        for entity in [*activity["used"], *activity["generated"]]:
            entity_check(entity)
        used = {(entity["id"], entity["version"]) for entity in activity["used"]}
        generated = {(entity["id"], entity["version"]) for entity in activity["generated"]}
        for relation in activity["relations"]:
            if relation.get("type") != "derivedFrom":
                raise PipelineError(f"unsupported relation: {ref}")
            if (relation["from"]["id"], relation["from"]["version"]) not in generated:
                raise PipelineError(f"unknown generated entity in relation: {ref}")
            if (relation["to"]["id"], relation["to"]["version"]) not in used:
                raise PipelineError(f"unknown used entity in relation: {ref}")
    return {
        "ok": True,
        "schemaVersion": PROVENANCE_SCHEMA_VERSION,
        "artifactCount": len(artifacts),
        "activityCount": len(index.get("activityRefs", [])),
        "snapshotCount": len(checked_snapshots),
    }
