from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .archive import Archive, json_loads


def create_app(
    alias: str,
    *,
    aliases: list[str] | tuple[str, ...] | None = None,
) -> FastAPI:
    account_aliases = normalize_aliases(alias, aliases)
    app = FastAPI(title=f"Grok Archive - {alias}")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "account": alias}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML.replace("__ALIAS__", alias)

    @app.get("/api/conversations")
    def get_conversations() -> dict[str, Any]:
        items: list[dict[str, Any]] = []

        for current_account in account_aliases:
            archive = Archive(current_account)
            if not archive.db_path.exists():
                continue

            reader = archive.open_readonly()
            try:
                db = reader.db
                db.row_factory = sqlite3.Row
                
                conv_rows = db.execute(
                    """
                    SELECT id, title, create_time, update_time, kind, raw_json
                      FROM conversations
                     ORDER BY create_time DESC
                    """
                ).fetchall()

                for conv in conv_rows:
                    conv_id = conv["id"]

                    preview_row = db.execute(
                        """
                        SELECT local_path, mime_type FROM assets
                         WHERE conversation_id = ?
                           AND status = 'downloaded'
                           AND kind = 'image'
                         ORDER BY CASE WHEN role = 'thumbnail' THEN 0 ELSE 1 END
                         LIMIT 1
                        """,
                        (conv_id,),
                    ).fetchone()

                    resp_count = db.execute(
                        "SELECT COUNT(*) FROM responses WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchone()[0]

                    asset_count = db.execute(
                        "SELECT COUNT(*) FROM assets WHERE conversation_id = ?",
                        (conv_id,),
                    ).fetchone()[0]

                    items.append({
                        "account": current_account,
                        "id": conv_id,
                        "title": conv["title"] or f"Conversation {conv_id[:8]}",
                        "createTime": conv["create_time"],
                        "updateTime": conv["update_time"],
                        "kind": conv["kind"],
                        "previewPath": preview_row["local_path"] if preview_row else None,
                        "responseCount": resp_count,
                        "assetCount": asset_count,
                    })
            except Exception as e:
                print(f"Error reading archive for {current_account}: {e}")
            finally:
                reader.close()

        items.sort(
            key=lambda x: (x.get("createTime") or "", x.get("account") or "", x.get("id") or ""),
            reverse=True,
        )

        return {
            "items": items,
            "total": len(items),
        }

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation_detail(conversation_id: str, account: str = "") -> dict[str, Any]:
        target_accounts = [account] if account else account_aliases

        for current_account in target_accounts:
            archive = Archive(current_account)
            if not archive.db_path.exists():
                continue

            reader = archive.open_readonly()
            try:
                db = reader.db
                db.row_factory = sqlite3.Row

                conv = db.execute(
                    "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                if not conv:
                    continue

                responses = db.execute(
                    "SELECT * FROM responses WHERE conversation_id = ? ORDER BY create_time ASC",
                    (conversation_id,),
                ).fetchall()

                assets = db.execute(
                    "SELECT * FROM assets WHERE conversation_id = ? ORDER BY kind, role",
                    (conversation_id,),
                ).fetchall()

                return {
                    "account": current_account,
                    "id": conv["id"],
                    "title": conv["title"],
                    "createTime": conv["create_time"],
                    "updateTime": conv["update_time"],
                    "kind": conv["kind"],
                    "raw": json_loads(conv["raw_json"], {}),
                    "responses": [dict(r) for r in responses],
                    "assets": [dict(a) for a in assets],
                }
            except Exception as e:
                print(f"Error reading detail for {conversation_id} in {current_account}: {e}")
            finally:
                reader.close()

        raise HTTPException(status_code=404, detail="Conversation not found")

    @app.get("/media/{path:path}")
    def media(path: str) -> FileResponse:
        media_account, relative_path = split_media_account(path, alias, account_aliases)
        requested = resolve_media_path(Archive(media_account).root, relative_path)
        if not requested.exists() or not requested.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(
            requested,
            media_type=guess_media_type(requested),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return app


def normalize_aliases(alias: str, aliases: list[str] | tuple[str, ...] | None) -> list[str]:
    ordered: list[str] = []
    for current in (alias, *(aliases or ())):
        if current and current not in ordered:
            ordered.append(current)
    return ordered


def split_media_account(path: str, default_alias: str, aliases: list[str]) -> tuple[str, str]:
    first, separator, rest = path.partition("/")
    if separator and first in aliases:
        return first, rest
    return default_alias, path


def resolve_media_path(root: Path, path: str) -> Path:
    archive_root = root.resolve()
    requested = (archive_root / path).resolve()
    try:
        requested.relative_to(archive_root)
    except ValueError as exc:
        raise HTTPException(status_code=404) from exc
    return requested


def guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    return "application/octet-stream"


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Grok Archive · __ALIAS__</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #050505;
      --surface: #121214;
      --surface-2: #1a1a1e;
      --text: #f5f5f7;
      --muted: #8e8e96;
      --line: rgba(255,255,255,0.08);
      --line-strong: rgba(255,255,255,0.14);
      --radius: 16px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font: 14px/1.5 Inter, sans-serif;
      background: var(--bg);
      padding-top: 24px;
    }
    button { font: inherit; color: inherit; cursor: pointer; }
    a { color: inherit; }

    main { max-width: 1600px; margin: 0 auto; padding: 0 20px 40px; }
    
    .account-accordion {
      margin-bottom: 20px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow: hidden;
      transition: border-color .2s;
    }
    .account-accordion[open] {
      border-color: var(--line-strong);
    }
    .account-title {
      font-size: 16px;
      font-weight: 700;
      padding: 16px 20px;
      cursor: pointer;
      user-select: none;
      background: var(--surface-2);
      display: flex;
      justify-content: space-between;
      align-items: center;
      list-style: none;
    }
    .account-title::-webkit-details-marker {
      display: none;
    }
    .account-title::after {
      content: '▼';
      font-size: 12px;
      color: var(--muted);
      transition: transform 0.2s;
    }
    .account-accordion[open] .account-title::after {
      transform: rotate(180deg);
    }
    .account-accordion .grid {
      padding: 20px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 16px;
    }
    .tile {
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      cursor: pointer;
      transition: transform .2s, border-color .2s;
    }
    .tile:hover { transform: translateY(-3px); border-color: var(--line-strong); }
    .media-wrap { position: relative; width: 100%; aspect-ratio: 1/1; background: #000; }
    .media-wrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .tile-body { padding: 12px; }
    .tile-id { font-weight: 600; font-size: 12px; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--muted); }
    .tile-sub { font-size: 11px; color: var(--muted); margin-top: 4px; display: flex; justify-content: space-between; }

    dialog {
      width: min(1000px, 95vw);
      height: min(850px, 90vh);
      border: 1px solid var(--line-strong);
      border-radius: 20px;
      background: var(--surface);
      color: var(--text);
      padding: 0;
    }
    dialog::backdrop { background: rgba(0,0,0,0.7); backdrop-filter: blur(8px); }
    .asset-card { margin-bottom: 16px; border: 1px solid var(--line); border-radius: 12px; padding: 16px; background: var(--surface-2); }
    .asset-card video { width: 100%; border-radius: 8px; display: block; max-height: 500px; object-fit: contain; background: #000; }
    .close-btn { float: right; background: transparent; border: 1px solid var(--line); padding: 4px 12px; border-radius: 999px; }
  </style>
</head>
<body>
  <main id="mainContainer">
    <div style="color: var(--muted); text-align: center; padding: 40px;">Loading conversations...</div>
  </main>

  <dialog id="dialog">
    <div id="detailContent" style="height: 100%;"></div>
  </dialog>

  <script>
    const els = {
      main: document.querySelector('#mainContainer'),
      dialog: document.querySelector('#dialog'),
      detail: document.querySelector('#detailContent'),
    };

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    async function init() {
      try {
        const res = await fetch('/api/conversations');
        if (!res.ok) throw new Error('Failed to load conversations');
        const data = await res.json();
        renderConversations(data.items);
      } catch (err) {
        console.error(err);
        els.main.innerHTML = '<div style="color: #ff6b6b; text-align: center; padding: 40px;">Failed to load conversation archive. Check server logs.</div>';
      }
    }

    function renderConversations(items) {
      els.main.innerHTML = '';
      if (!items || !items.length) {
        els.main.innerHTML = '<div style="color: var(--muted); text-align: center; padding: 40px;">No conversations found in the database.</div>';
        return;
      }

      const grouped = {};
      for (const item of items) {
        if (!grouped[item.account]) grouped[item.account] = [];
        grouped[item.account].push(item);
      }

      let isFirst = true;
      for (const [account, convs] of Object.entries(grouped)) {
        const details = document.createElement('details');
        details.className = 'account-accordion';
        if (isFirst) {
          details.open = true;
          isFirst = false;
        }

        const summary = document.createElement('summary');
        summary.className = 'account-title';
        summary.innerHTML = '<span>Account: ' + escapeHtml(account) + '</span><span style="font-weight: normal; color: var(--muted); font-size: 13px;">(' + convs.length + ' conversations)</span>';
        details.appendChild(summary);

        const grid = document.createElement('div');
        grid.className = 'grid';

        for (const conv of convs) {
          const tile = document.createElement('div');
          tile.className = 'tile';
          tile.addEventListener('click', () => openDetail(conv.id, conv.account));
          
          const mediaWrap = document.createElement('div');
          mediaWrap.className = 'media-wrap';
          if (conv.previewPath) {
            const img = document.createElement('img');
            img.src = '/media/' + encodeURI(conv.account + '/' + conv.previewPath);
            img.loading = 'lazy';
            mediaWrap.appendChild(img);
          } else {
            mediaWrap.style.display = 'grid';
            mediaWrap.style.placeItems = 'center';
            mediaWrap.style.color = 'var(--muted)';
            mediaWrap.style.fontSize = '12px';
            mediaWrap.textContent = 'No Preview';
          }
          tile.appendChild(mediaWrap);

          const body = document.createElement('div');
          body.className = 'tile-body';
          
          const tId = document.createElement('div');
          tId.className = 'tile-id';
          tId.textContent = conv.id;
          
          const formattedDate = conv.createTime ? new Date(conv.createTime).toLocaleDateString() : '';
          
          const sub = document.createElement('div');
          sub.className = 'tile-sub';
          sub.innerHTML = '<span>Assets: ' + conv.assetCount + '</span><span>' + formattedDate + '</span>';
          
          body.append(tId, sub);
          tile.appendChild(body);

          grid.appendChild(tile);
        }

        details.appendChild(grid);
        els.main.appendChild(details);
      }
    }

    async function openDetail(id, account) {
      try {
        const res = await fetch('/api/conversations/' + encodeURIComponent(id) + '?account=' + encodeURIComponent(account));
        if (!res.ok) return;
        const data = await res.json();

        const videoAssets = data.assets ? data.assets.filter(function(a) { return a.kind === 'video'; }) : [];

        let videosHtml = '';
        if (videoAssets.length > 0) {
          videosHtml = videoAssets.map(function(a) {
            let mediaElement = '';
            if (a.status === 'downloaded' && a.local_path) {
              const assetUrl = '/media/' + encodeURIComponent(data.account + '/' + a.local_path);
              mediaElement = '<video src="' + assetUrl + '" controls preload="metadata"></video>';
            } else {
              mediaElement = '<div style="color: var(--muted); font-size: 12px; padding: 20px; text-align: center; background: #000; border-radius: 8px;">Video not downloaded or missing.</div>';
            }
            return '<div class="asset-card">' + mediaElement + '</div>';
          }).join('');
        } else {
          videosHtml = '<p style="color: var(--muted); text-align: center; padding: 40px;">No videos found for this conversation.</p>';
        }

        els.detail.innerHTML = 
          '<div style="padding: 24px; height: 100%; overflow-y: auto;">' +
          '<button class="close-btn" onclick="els.dialog.close()">Close</button>' +
          '<h2 style="font-family: monospace; font-size: 16px; margin-top: 0;">ID: ' + escapeHtml(data.id) + '</h2>' +
          '<p style="color: var(--muted); font-size: 12px; margin-bottom: 20px;">Account: ' + escapeHtml(data.account) + ' · Created: ' + escapeHtml(data.createTime || 'N/A') + '</p>' +
          '<h3 style="margin-bottom: 12px;">Video Assets (' + videoAssets.length + ')</h3>' +
          '<div style="display:flex; flex-direction:column; gap:16px;">' + videosHtml + '</div>' +
          '</div>';

        els.dialog.showModal();
      } catch (err) {
        console.error('Error fetching conversation details:', err);
      }
    }

    init();
  </script>
</body>
</html>
"""
