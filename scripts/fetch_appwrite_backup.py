#!/usr/bin/env python3
"""Export Appwrite database collections/documents to versioned JSON snapshots.

Architecture (see README for full 運作原理):
  GitHub Actions cron → this script → Appwrite REST API (paginated)
  → sanitize secrets → write latest.json + optional history snapshot
  → landtophistory: write on odd UTC hours, remove on even UTC hours
  → workflow commits only when data/ under data/appwrite changed
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ---------------------------------------------------------------------------
# Exceptions & config
# ---------------------------------------------------------------------------


class AppwritePausedError(RuntimeError):
    """Raised when Appwrite pauses the project due to inactivity."""


def _env_flag(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got: {raw!r}") from exc
    if value < 0:
        raise RuntimeError(f"Environment variable {name} must be >= 0, got: {value}")
    return value


DEBUG_ENABLED = _env_flag("APPWRITE_EXPORT_DEBUG", "1")
PAGE_SIZE = _env_int("APPWRITE_PAGE_SIZE", 100)
# Keep newest N history files; 0 = unlimited. Default ~7 days of hourly backups.
HISTORY_RETENTION = _env_int("APPWRITE_HISTORY_RETENTION", 168)
# If true, skip writing when collection data matches latest.json (ignores exportedAt).
SKIP_IF_UNCHANGED = _env_flag("APPWRITE_SKIP_IF_UNCHANGED", "1")
HTTP_TIMEOUT_SEC = _env_int("APPWRITE_HTTP_TIMEOUT", 60)

REDACTED_SECRET = "[REDACTED_SECRET]"

# Exact JSON keys that almost always hold secrets (case-insensitive match on key).
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "apikey",
        "api_key",
        "api-key",
        "authorization",
        "auth",
        "accesstoken",
        "access_token",
        "refreshtoken",
        "refresh_token",
        "privatekey",
        "private_key",
        "sessionsecret",
        "session_secret",
        "clientsecret",
        "client_secret",
        "x-appwrite-key",
    }
)
# Suffixes: match keys like userPassword, db_secret, myApiKey
SENSITIVE_KEY_SUFFIXES = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
)
# Value patterns for secrets that appear as free text
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b", re.IGNORECASE),
)

BASE_DIR = Path("data/appwrite")
LATEST_PATH = BASE_DIR / "latest.json"
HISTORY_DIR = BASE_DIR / "history"
# Alternating history: write on odd UTC hours, remove on even UTC hours.
LANDTOP_HISTORY_DIR = BASE_DIR / "landtophistory"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log_progress(message: str) -> None:
    print(f"[progress] {message}", flush=True)


def log_debug(message: str) -> None:
    if DEBUG_ENABLED:
        print(f"[debug] {message}", flush=True)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    log_debug(f"Loaded required environment variable {name}")
    return value


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppwriteClient:
    """Thin REST client for Appwrite Databases API (server API key)."""

    endpoint: str
    project_id: str
    api_key: str
    timeout: int = HTTP_TIMEOUT_SEC

    @classmethod
    def from_env(cls) -> AppwriteClient:
        return cls(
            endpoint=require_env("APPWRITE_ENDPOINT").rstrip("/"),
            project_id=require_env("APPWRITE_PROJECT_ID"),
            api_key=require_env("APPWRITE_API_KEY"),
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = f"{self.endpoint}{path}{query}"
        log_debug(f"GET {url}")

        request = Request(
            url,
            headers={
                "X-Appwrite-Project": self.project_id,
                "X-Appwrite-Key": self.api_key,
                "Content-Type": "application/json",
            },
            method="GET",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
                size_hint = len(payload) if isinstance(payload, dict) else "n/a"
                log_debug(f"Response {response.status} from {path}; top-level size hint: {size_hint}")
                return payload
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            log_debug(f"HTTPError {exc.code} on {path}: {body}")
            try:
                err_payload = json.loads(body)
            except json.JSONDecodeError:
                err_payload = None

            if (
                exc.code == 403
                and isinstance(err_payload, dict)
                and err_payload.get("type") == "project_paused"
            ):
                raise AppwritePausedError(
                    err_payload.get("message", "Project is paused.")
                ) from exc

            raise RuntimeError(f"Appwrite API error {exc.code} on {path}: {body}") from exc
        except URLError as exc:
            log_debug(f"URLError on {path}: {exc}")
            raise RuntimeError(f"Failed to reach Appwrite endpoint: {exc}") from exc


def build_query(method: str, values: list[Any], column: str | None = None) -> str:
    """Build an Appwrite Query JSON string (legacy query object form)."""
    payload: dict[str, Any] = {"method": method, "values": values}
    if column is not None:
        payload["column"] = column
    return json.dumps(payload, separators=(",", ":"))


def paginate(
    fetch_page: Callable[[int, int], list[dict[str, Any]]],
    *,
    page_size: int = PAGE_SIZE,
    on_page: Callable[[int, int, list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """Offset/limit pagination until a short page is returned."""
    items: list[dict[str, Any]] = []
    offset = 0
    page_number = 0

    while True:
        page_number += 1
        batch = fetch_page(offset, page_size)
        items.extend(batch)
        if on_page:
            on_page(page_number, offset, batch)
        if len(batch) < page_size:
            break
        offset += page_size

    return items


# ---------------------------------------------------------------------------
# Domain: list collections & documents
# ---------------------------------------------------------------------------


def list_collections(client: AppwriteClient, database_id: str) -> list[dict[str, Any]]:
    def fetch_page(offset: int, page_size: int) -> list[dict[str, Any]]:
        payload = client.get(
            f"/databases/{database_id}/collections",
            params={
                "queries[]": [
                    build_query("limit", [page_size]),
                    build_query("offset", [offset]),
                ],
                "total": "false",
            },
        )
        return payload.get("collections", [])

    def on_page(page_number: int, offset: int, batch: list[dict[str, Any]]) -> None:
        log_progress(
            f"Loaded collection page {page_number} at offset {offset}; "
            f"page size {len(batch)}"
        )

    collections = paginate(fetch_page, on_page=on_page)
    log_progress(f"Discovered {len(collections)} collections")
    return collections


def list_documents(
    client: AppwriteClient,
    database_id: str,
    collection_id: str,
    *,
    collection_name: str,
    index: int,
    total: int,
) -> list[dict[str, Any]]:
    def fetch_page(offset: int, page_size: int) -> list[dict[str, Any]]:
        payload = client.get(
            f"/databases/{database_id}/collections/{collection_id}/documents",
            params={
                "queries[]": [
                    build_query("limit", [page_size]),
                    build_query("offset", [offset]),
                ],
                "total": "false",
            },
        )
        return payload.get("documents", [])

    accumulated = 0

    def on_page(page_number: int, offset: int, batch: list[dict[str, Any]]) -> None:
        nonlocal accumulated
        accumulated += len(batch)
        log_progress(
            f"[{index}/{total}] {collection_name} ({collection_id}) page {page_number}: "
            f"fetched {len(batch)} docs, accumulated {accumulated}"
        )
        log_debug(f"offset={offset} page_size={PAGE_SIZE}")

    return paginate(fetch_page, on_page=on_page)


# ---------------------------------------------------------------------------
# Sanitization (do NOT treat schema field name "key" as a secret)
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: str) -> bool:
    """True if this JSON key name likely holds a secret.

    Intentionally does NOT treat bare names like ``key`` / ``sortKey`` as
    sensitive — Appwrite attribute schema uses ``{"key": "fieldName"}``.
    """
    normalized = key.lower().replace("-", "_")
    if normalized in SENSITIVE_KEYS:
        return True
    # Schema / metadata keys that happen to contain "key" as a word
    if normalized in {"key", "primary_key", "foreign_key", "sort_key", "partition_key"}:
        return False
    # Match exact, snake_case suffix (_token), or camelCase lowered (userToken → usertoken)
    return any(
        normalized == suffix
        or normalized.endswith(f"_{suffix}")
        or (len(normalized) > len(suffix) and normalized.endswith(suffix))
        for suffix in SENSITIVE_KEY_SUFFIXES
    )


def redact_string(value: str, key_hint: str | None = None) -> str:
    if key_hint and _is_sensitive_key(key_hint):
        return REDACTED_SECRET
    redacted = value
    for pattern in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED_SECRET, redacted)
    return redacted


def sanitize_payload(value: Any, key_hint: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: sanitize_payload(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, key_hint) for item in value]
    if isinstance(value, str):
        return redact_string(value, key_hint)
    return value


# ---------------------------------------------------------------------------
# Snapshot build, content hash, retention
# ---------------------------------------------------------------------------


def content_fingerprint(snapshot: dict[str, Any]) -> str:
    """Stable hash of export body, ignoring wall-clock export time."""
    body = {
        "projectId": snapshot.get("projectId"),
        "databaseId": snapshot.get("databaseId"),
        "collectionCount": snapshot.get("collectionCount"),
        "collections": snapshot.get("collections"),
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log_debug(f"Could not load {path}: {exc}")
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log_debug(f"Wrote JSON file to {path.resolve()}")


def prune_history(history_dir: Path, keep: int) -> int:
    """Delete oldest snapshot-*.json files beyond `keep`. Returns number removed."""
    if keep <= 0 or not history_dir.is_dir():
        return 0

    snapshots = sorted(history_dir.glob("snapshot-*.json"), key=lambda p: p.name)
    excess = len(snapshots) - keep
    if excess <= 0:
        return 0

    removed = 0
    for path in snapshots[:excess]:
        try:
            path.unlink()
            removed += 1
            log_debug(f"Pruned history file {path.name}")
        except OSError as exc:
            log_debug(f"Failed to prune {path}: {exc}")
    if removed:
        log_progress(f"Pruned {removed} old history snapshot(s); retention={keep}")
    return removed


def remove_tree(path: Path) -> bool:
    """Remove a file or directory tree. Returns True if something was deleted."""
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        log_debug(f"Removed {path}")
        return True
    except OSError as exc:
        log_debug(f"Failed to remove {path}: {exc}")
        raise RuntimeError(f"Failed to remove {path}: {exc}") from exc


def apply_landtop_history(snapshot: dict[str, Any], now: datetime | None = None) -> tuple[str, bool]:
    """Odd UTC hour → write landtophistory; even UTC hour → remove it.

    Returns:
      (action, disk_changed) where action is "write" | "remove" | "remove-noop"
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)

    hour = moment.hour
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")

    # Odd hours (1,3,5,...,23): write. Even hours (0,2,4,...,22): remove.
    if hour % 2 == 1:
        landtop_latest = LANDTOP_HISTORY_DIR / "latest.json"
        landtop_stamp = LANDTOP_HISTORY_DIR / f"snapshot-{stamp}.json"
        log_progress(
            f"UTC hour {hour:02d} is odd → writing landtophistory "
            f"({landtop_latest.name}, {landtop_stamp.name})"
        )
        write_json(landtop_latest, snapshot)
        write_json(landtop_stamp, snapshot)
        return "write", True

    log_progress(f"UTC hour {hour:02d} is even → removing landtophistory")
    removed = remove_tree(LANDTOP_HISTORY_DIR)
    if removed:
        log_progress(f"Removed {LANDTOP_HISTORY_DIR}")
        return "remove", True
    log_progress(f"{LANDTOP_HISTORY_DIR} already absent; nothing to remove")
    return "remove-noop", False


def build_snapshot(client: AppwriteClient, database_id: str) -> dict[str, Any]:
    log_progress(f"Starting Appwrite export for database {database_id}")
    log_debug(f"Debug logging is {'enabled' if DEBUG_ENABLED else 'disabled'}")
    log_debug(f"Endpoint: {client.endpoint}")
    log_debug(f"Project ID: {client.project_id}")
    log_debug(f"PAGE_SIZE={PAGE_SIZE} HISTORY_RETENTION={HISTORY_RETENTION} SKIP_IF_UNCHANGED={SKIP_IF_UNCHANGED}")

    collections = list_collections(client, database_id)
    total = len(collections)
    exported: list[dict[str, Any]] = []

    for index, collection in enumerate(collections, start=1):
        collection_id = collection["$id"]
        collection_name = collection.get("name") or collection_id
        log_progress(f"[{index}/{total}] Exporting collection {collection_name} ({collection_id})")

        documents = list_documents(
            client,
            database_id,
            collection_id,
            collection_name=collection_name,
            index=index,
            total=total,
        )
        exported.append(
            {
                "collection": collection,
                "documentsCount": len(documents),
                "documents": documents,
            }
        )
        log_progress(
            f"[{index}/{total}] Completed {collection_name} ({collection_id}) "
            f"with {len(documents)} documents"
        )

    exported_at = datetime.now(timezone.utc)
    snapshot = {
        "exportedAt": exported_at.isoformat(),
        "projectId": client.project_id,
        "databaseId": database_id,
        "collectionCount": len(exported),
        "collections": exported,
    }
    sanitized = sanitize_payload(snapshot)
    if sanitized != snapshot:
        log_progress("Redacted sensitive values from exported snapshot")
    return sanitized


def export_to_disk(snapshot: dict[str, Any]) -> dict[str, Any]:
    """
    Write latest (+ classic history when data changed) and apply landtophistory
    odd/even hour policy (always runs; independent of content change).

    Returns a result dict for logging.
    """
    fingerprint = content_fingerprint(snapshot)
    previous = load_json(LATEST_PATH)
    history_path: Path | None = None
    latest_written = False
    history_pruned = 0

    content_unchanged = (
        SKIP_IF_UNCHANGED
        and previous is not None
        and content_fingerprint(previous) == fingerprint
    )

    if content_unchanged:
        log_progress(
            "No data changes vs latest.json (content fingerprint match); "
            "skipping latest/history snapshot write"
        )
        history_pruned = prune_history(HISTORY_DIR, HISTORY_RETENTION)
    else:
        stamp = datetime.fromisoformat(snapshot["exportedAt"]).strftime("%Y%m%dT%H%M%SZ")
        history_path = HISTORY_DIR / f"snapshot-{stamp}.json"

        log_progress(f"Writing latest snapshot → {LATEST_PATH}")
        write_json(LATEST_PATH, snapshot)
        latest_written = True

        log_progress(f"Writing history snapshot → {history_path}")
        write_json(history_path, snapshot)

        history_pruned = prune_history(HISTORY_DIR, HISTORY_RETENTION)

    # Always apply: odd UTC hour write / even UTC hour remove landtophistory.
    export_moment = datetime.fromisoformat(snapshot["exportedAt"])
    landtop_action, landtop_changed = apply_landtop_history(snapshot, export_moment)

    disk_changed = (
        latest_written
        or history_path is not None
        or history_pruned > 0
        or landtop_changed
    )

    return {
        "latest_path": LATEST_PATH,
        "history_path": history_path,
        "latest_written": latest_written,
        "history_pruned": history_pruned,
        "landtop_action": landtop_action,
        "landtop_changed": landtop_changed,
        "disk_changed": disk_changed,
        "content_unchanged": content_unchanged,
    }


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        client = AppwriteClient.from_env()
        database_id = require_env("APPWRITE_DATABASE_ID")
        snapshot = build_snapshot(client, database_id)
        result = export_to_disk(snapshot)

        parts = [
            f"collections={snapshot['collectionCount']}",
            f"latest={'wrote' if result['latest_written'] else 'skipped'}",
            f"history={result['history_path'] or 'skipped'}",
            f"landtophistory={result['landtop_action']}",
        ]
        if result["history_pruned"]:
            parts.append(f"history_pruned={result['history_pruned']}")
        print("Export done: " + ", ".join(parts))
        return 0
    except AppwritePausedError as exc:
        print(
            "::warning::Appwrite project is paused, so this backup run was skipped. "
            f"Restore the project in Appwrite Console to resume exports. Details: {exc}"
        )
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
