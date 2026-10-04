from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import GrokClient
from .config import archive_root


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


@dataclass(frozen=True)
class AssetRecord:
    asset_key: str
    conversation_id: str
    response_id: str
    kind: str
    role: str
    url: str
    mime_type: str = ""
    source_path: str = ""


class Archive:
    def __init__(self, alias: str, root: Path | None = None) -> None:
        self.alias = alias
        self.root = (root or archive_root()) / "accounts" / alias
        self.db_path = self.root / "index.sqlite"
        self.media_images = self.root / "media" / "images"
        self.media_videos = self.root / "media" / "videos"
        self.thumbs = self.root / "thumbs"
        self.metadata_conversations = self.root / "metadata" / "conversations"
        self.metadata_responses = self.root / "metadata" / "responses"
        self.raw_pages = self.root / "metadata" / "pages"
        self.failures_dir = self.root / "metadata" / "failures"
        self.conn: sqlite3.Connection | None = None

    def open(self, *, readonly: bool = False) -> "Archive":
        if readonly:
            if not self.db_path.exists():
                raise FileNotFoundError(f"missing archive database: {self.db_path}")
            self.conn = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout=30000")
            return self

        for directory in (
            self.media_images,
            self.media_videos,
            self.thumbs,
            self.metadata_conversations,
            self.metadata_responses,
            self.raw_pages,
            self.failures_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()
        return self

    def open_readonly(self) -> "Archive":
        return self.open(readonly=True)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Archive":
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("archive is not open")
        return self.conn

    def _migrate(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
              id TEXT PRIMARY KEY,
              title TEXT,
              create_time TEXT,
              update_time TEXT,
              kind TEXT,
              aux_keys TEXT,
              raw_json TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS responses (
              id TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL,
              parent_response_id TEXT,
              sender TEXT,
              message TEXT,
              create_time TEXT,
              file_attachment_asset_metadata TEXT,
              media_gen_input TEXT,
              aux_keys TEXT,
              raw_json TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS assets (
              asset_key TEXT PRIMARY KEY,
              conversation_id TEXT,
              response_id TEXT,
              kind TEXT NOT NULL,
              role TEXT NOT NULL,
              url TEXT NOT NULL,
              mime_type TEXT,
              source_path TEXT,
              local_path TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              sha256 TEXT,
              size INTEGER,
              http_status INTEGER,
              fail_reason TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              finished_at TEXT,
              account TEXT NOT NULL,
              mode TEXT NOT NULL,
              limit_conversations INTEGER,
              conversations_seen INTEGER NOT NULL DEFAULT 0,
              responses_seen INTEGER NOT NULL DEFAULT 0,
              assets_seen INTEGER NOT NULL DEFAULT 0,
              errors INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'running'
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_create_time ON conversations(create_time);
            CREATE INDEX IF NOT EXISTS idx_responses_conversation_id ON responses(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_responses_create_time ON responses(create_time);
            CREATE INDEX IF NOT EXISTS idx_assets_conversation_id ON assets(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_assets_response_id ON assets(response_id);
            CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
            """
        )
        self.db.commit()

    def begin_run(self, *, mode: str, limit_conversations: int | None) -> int:
        cur = self.db.execute(
            "INSERT INTO sync_runs (account, mode, limit_conversations) VALUES (?, ?, ?)",
            (self.alias, mode, limit_conversations),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        conversations_seen: int,
        responses_seen: int,
        assets_seen: int,
        errors: int,
        status: str,
    ) -> None:
        self.db.execute(
            """
            UPDATE sync_runs
               SET finished_at = CURRENT_TIMESTAMP,
                   conversations_seen = ?,
                   responses_seen = ?,
                   assets_seen = ?,
                   errors = ?,
                   status = ?
             WHERE id = ?
            """,
            (conversations_seen, responses_seen, assets_seen, errors, status, run_id),
        )
        self.db.commit()

    def save_page(self, scope: str, index: int, payload: Any) -> None:
        safe_scope = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in scope)
        path = self.raw_pages / f"{safe_scope}-{index:05d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert_conversation(self, conv: dict[str, Any]) -> str:
        conv_id = str(conv.get("conversationId") or "").strip()
        if not conv_id:
            return ""
        title = str(conv.get("title") or "")
        create_time = str(conv.get("createTime") or "")
        update_time = str(conv.get("updateTime") or "")
        kind = str(conv.get("kind") or "")
        aux_keys = conv.get("auxKeys")
        aux_keys_str = json_dumps(aux_keys) if isinstance(aux_keys, dict) else None

        self.metadata_conversations.joinpath(f"{conv_id}.json").write_text(
            json.dumps(conv, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.db.execute(
            """
            INSERT INTO conversations (
              id, title, create_time, update_time, kind, aux_keys, raw_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,
              create_time=excluded.create_time,
              update_time=excluded.update_time,
              kind=excluded.kind,
              aux_keys=excluded.aux_keys,
              raw_json=excluded.raw_json,
              updated_at=CURRENT_TIMESTAMP
            """,
            (conv_id, title, create_time, update_time, kind, aux_keys_str, json_dumps(conv)),
        )
        self.db.commit()
        return conv_id

    def upsert_response(self, conversation_id: str, resp: dict[str, Any]) -> str:
        resp_id = str(resp.get("responseId") or "").strip()
        if not resp_id:
            return ""
        parent_response_id = str(resp.get("parentResponseId") or resp.get("parentId") or "").strip() or None
        sender = str(resp.get("sender") or "")
        message = str(resp.get("message") or resp.get("text") or "")
        create_time = str(resp.get("createTime") or "")

        file_attachment_asset_metadata = resp.get("fileAttachmentAssetMetadata")
        file_attachment_asset_metadata_str = json_dumps(file_attachment_asset_metadata) if file_attachment_asset_metadata is not None else None

        media_gen_input = resp.get("mediaGenInput")
        media_gen_input_str = json_dumps(media_gen_input) if media_gen_input is not None else None

        aux_keys = resp.get("auxKeys")
        aux_keys_str = json_dumps(aux_keys) if isinstance(aux_keys, (dict, list)) else None

        self.metadata_responses.joinpath(f"{resp_id}.json").write_text(
            json.dumps(resp, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.db.execute(
            """
            INSERT INTO responses (
              id, conversation_id, parent_response_id, sender, message, create_time,
              file_attachment_asset_metadata, media_gen_input, aux_keys, raw_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
              conversation_id=excluded.conversation_id,
              parent_response_id=excluded.parent_response_id,
              sender=excluded.sender,
              message=excluded.message,
              create_time=excluded.create_time,
              file_attachment_asset_metadata=excluded.file_attachment_asset_metadata,
              media_gen_input=excluded.media_gen_input,
              aux_keys=excluded.aux_keys,
              raw_json=excluded.raw_json,
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                resp_id,
                conversation_id,
                parent_response_id,
                sender,
                message,
                create_time,
                file_attachment_asset_metadata_str,
                media_gen_input_str,
                aux_keys_str,
                json_dumps(resp),
            ),
        )
        self.db.commit()
        return resp_id

    def upsert_asset(self, asset: AssetRecord) -> bool:
        existing = self.db.execute(
            "SELECT status, local_path FROM assets WHERE asset_key = ?",
            (asset.asset_key,),
        ).fetchone()
        self.db.execute(
            """
            INSERT INTO assets (
              asset_key, conversation_id, response_id, kind, role, url, mime_type, source_path, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(asset_key) DO UPDATE SET
              conversation_id=excluded.conversation_id,
              response_id=excluded.response_id,
              kind=excluded.kind,
              role=excluded.role,
              url=excluded.url,
              mime_type=excluded.mime_type,
              source_path=excluded.source_path,
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                asset.asset_key,
                asset.conversation_id,
                asset.response_id,
                asset.kind,
                asset.role,
                asset.url,
                asset.mime_type,
                asset.source_path,
            ),
        )
        return existing is None or not existing["local_path"]

    def upsert_assets(self, assets: Iterable[AssetRecord]) -> int:
        count = 0
        for asset in assets:
            if self.upsert_asset(asset):
                count += 1
        self.db.commit()
        return count

    def pending_assets(self, *, retry_failed: bool = True) -> list[sqlite3.Row]:
        statuses = ("pending", "failed") if retry_failed else ("pending",)
        placeholders = ",".join("?" for _ in statuses)
        return list(
            self.db.execute(
                f"""
                SELECT * FROM assets
                 WHERE status IN ({placeholders})
                    OR local_path IS NULL
                 ORDER BY kind, conversation_id, role
                """,
                statuses,
            )
        )

    def download_asset(self, client: GrokClient, row: sqlite3.Row) -> bool:
        url = str(row["url"])
        local_path = self.local_path_for_asset(row)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists() and local_path.stat().st_size > 0:
            sha256, size = hash_file(local_path)
            self.mark_asset_downloaded(row["asset_key"], local_path, sha256, size)
            return False
        try:
            response = client.get(url, timeout=180, stream=True)
            if response.status_code != 200:
                self.mark_asset_failed(row["asset_key"], response.status_code, f"HTTP {response.status_code}")
                return False
            fd, tmp_name = tempfile.mkstemp(prefix=".download-", dir=str(local_path.parent))
            sha = hashlib.sha256()
            size = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
                if size <= 0:
                    raise RuntimeError("empty response body")
                os.replace(tmp_name, local_path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            self.mark_asset_downloaded(row["asset_key"], local_path, sha.hexdigest(), size)
            return True
        except Exception as exc:
            self.mark_asset_failed(row["asset_key"], None, str(exc)[:1000])
            return False

    def local_path_for_asset(self, row: sqlite3.Row) -> Path:
        kind = str(row["kind"] or "other")
        role = str(row["role"] or "media")
        conv_id = str(row["conversation_id"] or "unknown")
        url = str(row["url"] or "")
        mime_type = str(row["mime_type"] or "")
        suffix = suffix_for_url(url, mime_type)
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        filename = f"{conv_id}-{role}-{digest}{suffix}"
        filename = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)
        if role == "thumbnail":
            return self.thumbs / filename
        if kind == "video":
            return self.media_videos / filename
        if kind == "image":
            return self.media_images / filename
        return self.root / "media" / "other" / filename

    def mark_asset_downloaded(
        self, asset_key: str, local_path: Path, sha256: str, size: int
    ) -> None:
        self.db.execute(
            """
            UPDATE assets
               SET status = 'downloaded',
                   local_path = ?,
                   sha256 = ?,
                   size = ?,
                   http_status = 200,
                   fail_reason = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE asset_key = ?
            """,
            (str(local_path.relative_to(self.root)), sha256, size, asset_key),
        )
        self.db.commit()

    def mark_asset_failed(self, asset_key: str, http_status: int | None, reason: str) -> None:
        self.db.execute(
            """
            UPDATE assets
               SET status = 'failed',
                   http_status = ?,
                   fail_reason = ?,
                   retry_count = retry_count + 1,
                   updated_at = CURRENT_TIMESTAMP
             WHERE asset_key = ?
            """,
            (http_status, reason, asset_key),
        )
        self.db.commit()

    def stats(self) -> dict[str, int]:
        row = self.db.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM conversations) AS conversations,
              (SELECT COUNT(*) FROM responses) AS responses,
              (SELECT COUNT(*) FROM assets WHERE kind='image' AND role!='thumbnail') AS images,
              (SELECT COUNT(*) FROM assets WHERE kind='video' AND role!='thumbnail') AS videos,
              (SELECT COUNT(*) FROM assets WHERE role='thumbnail') AS thumbnails,
              (SELECT COUNT(*) FROM assets WHERE status='downloaded') AS downloaded,
              (SELECT COUNT(*) FROM assets WHERE status='failed') AS failed,
              (SELECT COUNT(*) FROM assets WHERE status!='downloaded') AS missing
            """
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def status(self) -> dict[str, Any]:
        stats = self.stats()
        rows = self.db.execute(
            """
            SELECT kind, role, status, COUNT(*) AS count
              FROM assets
             GROUP BY kind, role, status
             ORDER BY kind, role, status
            """
        ).fetchall()
        latest_run = self.db.execute(
            """
            SELECT id, mode, started_at, finished_at, conversations_seen, responses_seen, assets_seen, errors, status
              FROM sync_runs
             ORDER BY id DESC
             LIMIT 1
            """
        ).fetchone()
        return {
            **stats,
            "archive_root": str(self.root),
            "db_path": str(self.db_path),
            "assets_by_kind_role_status": [dict(row) for row in rows],
            "latest_run": dict(latest_run) if latest_run else None,
        }


def hash_file(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def suffix_for_url(url: str, mime_type: str = "") -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    suffix = Path(name).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    if "video" in mime_type:
        return ".mp4"
    if "image" in mime_type:
        return ".jpg"
    return ".bin"
