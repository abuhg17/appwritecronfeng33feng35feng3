import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class AppwritePausedError(RuntimeError):
    """Raised when Appwrite pauses the project due to inactivity."""


def log_progress(message: str) -> None:
    print(f"[progress] {message}", flush=True)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def appwrite_get(
    endpoint: str,
    project_id: str,
    api_key: str,
    path: str,
    params: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    query = ""
    if params:
        query = "?" + urlencode(params, doseq=True)

    request = Request(
        f"{endpoint.rstrip('/')}{path}{query}",
        headers={
            "X-Appwrite-Project": project_id,
            "X-Appwrite-Key": api_key,
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None

        if exc.code == 403 and isinstance(payload, dict) and payload.get("type") == "project_paused":
            raise AppwritePausedError(payload.get("message", "Project is paused.")) from exc

        raise RuntimeError(f"Appwrite API error {exc.code} on {path}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Failed to reach Appwrite endpoint: {exc}") from exc


def build_query(method: str, values: List[Any], column: str | None = None) -> str:
    payload: Dict[str, Any] = {
        "method": method,
        "values": values,
    }
    if column is not None:
        payload["column"] = column
    return json.dumps(payload, separators=(",", ":"))


def list_collections(endpoint: str, project_id: str, api_key: str, database_id: str) -> List[Dict[str, Any]]:
    collections: List[Dict[str, Any]] = []
    offset = 0
    page_size = 100

    while True:
        payload = appwrite_get(
            endpoint,
            project_id,
            api_key,
            f"/databases/{database_id}/collections",
            params={
                "queries[]": [
                    build_query("limit", [page_size]),
                    build_query("offset", [offset]),
                ],
                "total": "false",
            },
        )
        batch = payload.get("collections", [])
        collections.extend(batch)
        log_progress(f"Loaded collection page at offset {offset}; total collections discovered: {len(collections)}")

        if len(batch) < page_size:
            break
        offset += page_size

    return collections


def list_documents(
    endpoint: str,
    project_id: str,
    api_key: str,
    database_id: str,
    collection_id: str,
    collection_name: str,
    current_index: int,
    total_collections: int,
) -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []
    offset = 0
    page_size = 100
    page_number = 0

    while True:
        page_number += 1
        payload = appwrite_get(
            endpoint,
            project_id,
            api_key,
            f"/databases/{database_id}/collections/{collection_id}/documents",
            params={
                "queries[]": [
                    build_query("limit", [page_size]),
                    build_query("offset", [offset]),
                ],
                "total": "false",
            },
        )
        batch = payload.get("documents", [])
        documents.extend(batch)
        log_progress(
            f"[{current_index}/{total_collections}] {collection_name} ({collection_id}) page {page_number}: "
            f"fetched {len(batch)} docs, accumulated {len(documents)}"
        )

        if len(batch) < page_size:
            break
        offset += page_size

    return documents


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_snapshot() -> Dict[str, Any]:
    endpoint = require_env("APPWRITE_ENDPOINT")
    project_id = require_env("APPWRITE_PROJECT_ID")
    database_id = require_env("APPWRITE_DATABASE_ID")
    api_key = require_env("APPWRITE_API_KEY")

    log_progress(f"Starting Appwrite export for database {database_id}")
    collections = list_collections(endpoint, project_id, api_key, database_id)
    exported_collections = []
    total_collections = len(collections)
    log_progress(f"Discovered {total_collections} collections to export")

    for index, collection in enumerate(collections, start=1):
        collection_id = collection["$id"]
        collection_name = collection.get("name") or collection_id
        log_progress(f"[{index}/{total_collections}] Exporting collection {collection_name} ({collection_id})")
        documents = list_documents(
            endpoint,
            project_id,
            api_key,
            database_id,
            collection_id,
            collection_name,
            index,
            total_collections,
        )
        exported_collections.append(
            {
                "collection": collection,
                "documentsCount": len(documents),
                "documents": documents,
            }
        )
        log_progress(
            f"[{index}/{total_collections}] Completed {collection_name} ({collection_id}) with {len(documents)} documents"
        )

    exported_at = datetime.now(timezone.utc)
    return {
        "exportedAt": exported_at.isoformat(),
        "projectId": project_id,
        "databaseId": database_id,
        "collectionCount": len(exported_collections),
        "collections": exported_collections,
    }


def main() -> int:
    try:
        snapshot = build_snapshot()
        exported_at = datetime.fromisoformat(snapshot["exportedAt"])
        stamp = exported_at.strftime("%Y%m%dT%H%M%SZ")

        base_dir = Path("data/appwrite")
        latest_path = base_dir / "latest.json"
        history_path = base_dir / "history" / f"snapshot-{stamp}.json"

        write_json(latest_path, snapshot)
        write_json(history_path, snapshot)

        print(f"Exported {snapshot['collectionCount']} collections to {latest_path} and {history_path}")
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
