"""
Core safety contracts, access modes, optimistic concurrency hashing,
standardized failure taxonomy, deterministic canonical JSON, and JSONL audit logging.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Union


class AccessMode(str, Enum):
    """Access posture for the MCP server (precedence: CLI > ENV > default)."""

    READ_ONLY = "read-only"
    READ_WRITE = "read-write"
    FULL = "full"


class FailureCategory(str, Enum):
    """Closed machine-readable failure taxonomy inspired by Czarnak/tia-portal-mcp."""

    VALIDATION_ERROR = "validation_error"
    STATE_CHANGED = "state_changed"
    WRITE_GUARDRAIL_BLOCKED = "write_guardrail_blocked"
    ACCESS_DENIED = "access_denied"
    TARGET_NOT_FOUND = "target_not_found"
    COMPILE_FAILED = "compile_failed"
    READBACK_MISMATCH = "readback_mismatch"
    BACKUP_FAILED = "backup_failed"
    COM_UNAVAILABLE = "com_unavailable"
    COM_TIMEOUT = "com_timeout"
    TRANSPORT_ERROR = "transport_error"
    PLCSIM_UNAVAILABLE = "plcsim_unavailable"
    HW_CONFIG_ERROR = "hw_config_error"
    GUI_MODAL_BLOCKED = "gui_modal_blocked"
    INTERNAL_ERROR = "internal_error"


def resolve_access_mode(cli_mode: Optional[str] = None) -> AccessMode:
    """
    Resolves effective server access mode using precedence:
    CLI argument > S7_ACCESS_MODE > S7_MCP_MODE > default (READ_WRITE).
    """
    raw = (
        cli_mode
        or os.environ.get("S7_ACCESS_MODE")
        or os.environ.get("S7_MCP_MODE")
        or AccessMode.READ_WRITE.value
    ).strip().lower()
    normalized = raw.replace("_", "-")
    aliases = {
        "ro": AccessMode.READ_ONLY,
        "readonly": AccessMode.READ_ONLY,
        "read-only": AccessMode.READ_ONLY,
        "rw": AccessMode.READ_WRITE,
        "readwrite": AccessMode.READ_WRITE,
        "read-write": AccessMode.READ_WRITE,
        "full": AccessMode.FULL,
    }
    return aliases.get(normalized, AccessMode.READ_WRITE)


def compute_content_hash(format_prefix: str, content: Union[str, bytes]) -> str:
    """
    Computes a canonical SHA-256 content hash for optimistic concurrency control.
    Normalizes CRLF to LF for text payloads so cross-platform line endings remain stable.
    """
    if isinstance(content, str):
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        raw_bytes = normalized.encode("utf-8")
    else:
        raw_bytes = bytes(content)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    prefix = (format_prefix or "raw").strip().lower()
    return f"{prefix}:sha256:{digest}"


def canonical_json(payload: Dict[str, Any], indent: Optional[int] = 2) -> str:
    """Serializes tool responses deterministically as UTF-8 JSON."""
    return json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=False, default=str)


def make_tool_annotations(
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    open_world: bool = False,
    title: Optional[str] = None,
) -> Any:
    """
    Constructs official MCP ToolAnnotations when the mcp SDK supports it,
    or returns a plain metadata dictionary when running under older/fallback shims.
    """
    data: Dict[str, Any] = {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }
    if title:
        data["title"] = title
    try:
        from mcp.types import ToolAnnotations  # type: ignore

        return ToolAnnotations(**data)
    except Exception:
        return data


class AuditLogger:
    """Thread-safe append-only JSONL audit trail for mutations, compilations, and live PLC ops."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._log_path = log_path

    def set_log_path(self, log_path: Path) -> None:
        with self._lock:
            self._log_path = log_path

    @property
    def log_path(self) -> Path:
        if self._log_path is not None:
            return self._log_path
        default_dir = Path.home() / "Documents" / "Codex-Simatic7"
        return default_dir / "audit.jsonl"

    def record(
        self,
        *,
        tool: str,
        action: str,
        success: bool,
        status: str,
        target: Optional[str] = None,
        content_hash: Optional[str] = None,
        failure_category: Optional[str] = None,
        latency_ms: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry: Dict[str, Any] = {
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tool": tool,
            "action": action,
            "success": bool(success),
            "status": status,
        }
        if target is not None:
            entry["target"] = target
        if content_hash is not None:
            entry["content_hash"] = content_hash
        if failure_category is not None:
            entry["failure_category"] = failure_category
        if latency_ms is not None:
            entry["latency_ms"] = round(float(latency_ms), 2)
        if details:
            entry["details"] = details

        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                path = self.log_path
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception:
                # Audit logging must never crash an industrial safety path
                pass
