"""Offline registry admission and pinned receipt evaluation (no provider I/O)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUTHOR = "kanban-registry/v1"
PREFIX = AUTHOR + ":"
STAGES = ("development", "merge", "deployment")
STATES = {"unknown", "queued", "running", "waiting_user", "waiting_time", "review",
          "failed", "idle", "stopped", "completed"}
SENSITIVE = re.compile(
    r"(?i)(?:\b(?:sk-|ghp_|github_pat_|xox[baprs]-)[a-z0-9_-]{8,}"
    r"|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+"
    r"|[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:\d{1,3}\.){3}\d{1,3}\b"
    r"|\b1[3-9]\d{9}\b|-----BEGIN [A-Z ]*PRIVATE KEY)"
)


class RegistryError(ValueError):
    """Public errors contain fixed codes, never source content or credentials."""


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def checked_path(path: Path, *, root: Path | None = None) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise RegistryError("unsafe_path")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise RegistryError("symlink_refused")
    resolved = path.resolve()
    if root is not None and not resolved.is_relative_to(root):
        raise RegistryError("path_outside_root")
    if path.is_file() and path.stat().st_nlink != 1:
        raise RegistryError("hardlink_refused")
    return resolved


def read_json(path: Path):
    checked_path(path)
    if path.stat().st_size > 2_000_000:
        raise RegistryError("input_too_large")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def text(value, *, limit=500) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > limit:
        raise RegistryError("invalid_text")
    if SENSITIVE.search(value) or any(ord(c) < 32 for c in value):
        raise RegistryError("sensitive_or_control_text")
    # These fields are rendered as plain text by CLI/JSON and must also be safe
    # when a board renderer treats title/body/comments as Markdown or HTML.
    if any(c in value for c in "<>`[]\\"):
        raise RegistryError("markup_refused")
    return value.strip()


def identifier(value) -> str:
    value = text(value, limit=160)
    if not re.fullmatch(r"[\w:.-]+", value):
        raise RegistryError("invalid_identifier")
    return value


def load_config(path: Path) -> dict:
    config = read_json(path)
    if not isinstance(config, dict):
        raise RegistryError("invalid_config")
    if config.get("version") == 2:
        from scripts.kanban_registry_native import target_config
        config = target_config(config)
    else:
        if config.get("version") != 1 or config.get("mode") != "offline":
            raise RegistryError("offline_mode_required")
        root = checked_path(Path(config["offline_root"]), root=REPO / ".hermes")
        if root == REPO / ".hermes":
            raise RegistryError("dedicated_offline_root_required")
        config["offline_root"] = root
    config["source_root"] = checked_path(Path(config["source_root"]))
    config["profile"] = identifier(config["profile"])
    board = config["board"]
    if not isinstance(board, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", board) or board == "default":
        raise RegistryError("named_board_required")
    if type(config["max_age_seconds"]) is not int or not 1 <= config["max_age_seconds"] <= 604800:
        raise RegistryError("invalid_freshness_window")
    if not isinstance(config["evidence"], dict):
        raise RegistryError("invalid_evidence_manifest")
    for key, evidence in config["evidence"].items():
        identifier(key)
        if not isinstance(evidence, dict) or not isinstance(evidence["claims"], list) or any(not isinstance(c, str) for c in evidence["claims"]):
            raise RegistryError("invalid_evidence_claims")
        if evidence["profile"] != config["profile"]:
            raise RegistryError("cross_profile_evidence")
        if not re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"]):
            raise RegistryError("invalid_receipt_digest")
        relative = Path(evidence["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.suffix not in {".md", ".json", ".txt"}:
            raise RegistryError("unsafe_receipt_path")
        evidence["resolved"] = checked_path(config["source_root"] / relative, root=config["source_root"])
    return config


def validate_record(record: dict, config: dict) -> dict:
    if not isinstance(record, dict):
        raise RegistryError("invalid_record")
    if record["profile"] != config["profile"]:
        raise RegistryError("cross_profile_record")
    if config.get('version') == 2 and record['project'] not in config['projects']:
        raise RegistryError('unmapped_project')
    for field in ("task_id", "project", "owner"):
        identifier(record[field])
    for field in ("title", "goal", "acceptance"):
        text(record[field])
    if not re.fullmatch(r"[0-9]{10,24}", record["thread"]):
        raise RegistryError("invalid_thread")
    source = record["source"]
    if not isinstance(source, dict) or set(source) != {"kind", "id"}:
        raise RegistryError("invalid_source")
    if source["kind"] not in {"development", "ops", "manual"}:
        raise RegistryError("invalid_source_kind")
    identifier(source["id"])
    if record["handoff"] not in config["evidence"]:
        raise RegistryError("unapproved_handoff")
    if not isinstance(record["executor_ids"], list) or not isinstance(record["receipts"], list):
        raise RegistryError("invalid_references")
    for external_id in record["executor_ids"]:
        identifier(external_id)
    if type(record["revision"]) is not int or record["revision"] < 1:
        raise RegistryError("invalid_revision")
    if record["external_state"] not in STATES:
        raise RegistryError("invalid_external_state")
    if not isinstance(record["stages"], dict) or set(record["stages"]) != set(STAGES) or any(v not in {"unknown", "pending", "passed", "failed"} for v in record["stages"].values()):
        raise RegistryError("invalid_stages")
    if record["goal_status"] not in {"unverified", "accepted", "rejected"}:
        raise RegistryError("invalid_goal_status")
    for ref in [*record["receipts"], record["handoff"]]:
        if ref not in config["evidence"]:
            raise RegistryError("unapproved_receipt")
        if config["evidence"][ref]["task_id"] != record["task_id"]:
            raise RegistryError("cross_task_receipt")
    timestamp(record["observed_at"])
    return dict(record, executor_ids=sorted(set(record["executor_ids"])), receipts=sorted(set(record["receipts"])))


def timestamp(value: str) -> int:
    if not isinstance(value, str):
        raise RegistryError("invalid_timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise RegistryError("timezone_required")
    return int(parsed.timestamp())


def receipt_claims(config: dict, ref: str) -> tuple[set[str], str | None]:
    entry = config["evidence"][ref]
    path = checked_path(entry["resolved"], root=config["source_root"])
    if not path.is_file():
        return set(), "receipt_missing"
    if path.stat().st_size > 2_000_000:
        return set(), "receipt_too_large"
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        return set(), "receipt_changed"
    return set(entry["claims"]), None


def identity(record: dict) -> dict:
    fields = ("profile", "task_id", "project", "goal", "acceptance", "source", "owner", "thread", "handoff")
    return {k: record[k] for k in fields}


def task_key(record: dict) -> str:
    return PREFIX + digest([record["profile"], record["task_id"]])


def snapshot(record: dict, config: dict, *, now: int | None = None) -> dict:
    now = int(datetime.now(UTC).timestamp()) if now is None else now
    claims, reasons = set(), set()
    for ref in sorted(set(record["receipts"]) | {record["handoff"]}):
        found, reason = receipt_claims(config, ref)
        claims.update(found)
        if reason:
            reasons.add(reason)
        if any(c.startswith("external:") for c in found):
            receipt_age = now - timestamp(config["evidence"][ref]["observed_at"])
            if receipt_age < -60 or receipt_age > config["max_age_seconds"]:
                reasons.add("receipt_stale_or_future")
    handoff_claims, _ = receipt_claims(config, record["handoff"])
    admitted = "admission:" + record["task_id"] in handoff_claims
    if not admitted:
        reasons.add("admission_unverified")
    age = now - timestamp(record["observed_at"])
    if age < -60 or age > config["max_age_seconds"]:
        reasons.add("observation_stale_or_future")
    state = record["external_state"]
    if state == "unknown":
        reasons.add("external_observation_unknown")
    if state != "unknown" and "external:" + state not in claims:
        reasons.add("external_evidence_missing")
    stages = {}
    for stage, value in record["stages"].items():
        stages[stage] = value
        if value != "unknown" and f"{stage}:{value}" not in claims:
            stages[stage] = "unknown"
            reasons.add("stage_evidence_missing")
    goal = record["goal_status"]
    if goal != "unverified" and "goal:" + goal not in claims:
        goal = "unverified"
        reasons.add("goal_evidence_missing")
    if reasons:
        state, goal = "unknown", "unverified"
        stages = dict.fromkeys(STAGES, "unknown")
    return {"schema": AUTHOR, "revision": record["revision"], "input_digest": digest(record),
            "external_state": state, "goal_status": goal, "stages": stages,
            "observed_at": record["observed_at"], "executor_ids": sorted(set(record["executor_ids"])),
            "receipt_versions": {ref: config["evidence"][ref]["sha256"] for ref in sorted(set(record["receipts"]) | {record["handoff"]})},
            "reasons": sorted(reasons), "admitted": admitted}
