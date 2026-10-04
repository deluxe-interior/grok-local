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
                     ORDER BY create_time ASC 
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
                    "SELECT * FROM assets WHERE conversation_id = ? ORDER BY updated_at ASC",
                    (conversation_id,),
                ).fetchall()

                conv_dict = dict(conv)

                return {
                    "account": current_account,
                    "id": conv["id"],
                    "title": conv["title"],
                    "createTime": conv["create_time"],
                    "updateTime": conv["update_time"],
                    "kind": conv["kind"],
                    "conversation": conv_dict,
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
            headers={
                "Cache-Control": "public, max-age=3600",
                "Accept-Ranges": "bytes",
            },
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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Grok Local</title>
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
      --accent: #6366f1;
      --radius: 16px;
      --safe-bottom: env(safe-area-inset-bottom, 0px);
      --safe-top: env(safe-area-inset-top, 0px);
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font: 14px/1.5 Inter, sans-serif;
      background: var(--bg);
      padding-top: var(--safe-top);
      overflow-x: hidden;
    }
    button { font: inherit; color: inherit; cursor: pointer; touch-action: manipulation; }
    a { color: inherit; }

    main {
      margin-right: 440px;
      padding: 20px 16px calc(20px + var(--safe-bottom));
    }

    .account-accordion {
      margin-bottom: 16px;
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
      font-size: 15px;
      font-weight: 700;
      padding: 16px 18px;
      min-height: 48px;
      cursor: pointer;
      user-select: none;
      background: var(--surface-2);
      display: flex;
      justify-content: space-between;
      align-items: center;
      list-style: none;
    }
    .account-title::-webkit-details-marker { display: none; }
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
      padding: 14px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 12px;
    }
    .tile {
      background: var(--surface-2);
      border: 2px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      cursor: pointer;
      transition: transform .2s, border-color .2s, box-shadow .2s;
    }
    .tile:active { transform: scale(0.98); }
    .tile.selected {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent);
    }
    .media-wrap { position: relative; width: 100%; aspect-ratio: 1/1; background: #000; }
    .media-wrap img { width: 100%; height: 100%; object-fit: contain; display: block; }
    .tile-body { padding: 10px; }
    .tile-id { font-weight: 600; font-size: 12px; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--muted); }
    .tile-sub { font-size: 11px; color: var(--muted); margin-top: 4px; display: flex; justify-content: space-between; }

    /* Desktop Side Panel / Mobile Bottom Sheet */
    .right-panel {
      position: fixed;
      top: 0;
      right: 0;
      width: 440px;
      height: 100vh;
      background: var(--surface);
      border-left: 1px solid var(--line-strong);
      padding: 20px;
      overflow-y: auto;
      z-index: 100;
      display: flex;
      flex-direction: column;
    }

    @media (max-width: 900px) {
      main { margin-right: 0; padding-bottom: 120px; }
      .grid { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
      .right-panel {
        top: auto;
        bottom: 0;
        left: 0;
        width: 100%;
        height: 60vh;
        max-height: 80vh;
        border-left: none;
        border-top: 1px solid var(--line-strong);
        border-radius: 20px 20px 0 0;
        box-shadow: 0 -10px 30px rgba(0, 0, 0, 0.7);
        padding: 16px 16px calc(16px + var(--safe-bottom));
        transform: translateY(100%);
        transition: transform 0.3s cubic-bezier(0.1, 0.9, 0.2, 1);
      }
      .right-panel.open {
        transform: translateY(0);
      }
      .sheet-handle {
        width: 40px;
        height: 4px;
        background: var(--line-strong);
        border-radius: 999px;
        margin: 0 auto 12px auto;
      }
    }

    .panel-title {
      font-size: 15px;
      font-weight: 700;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line-strong);
      margin-bottom: 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .panel-section { margin-bottom: 16px; }
    .panel-section h4 {
      margin: 0 0 6px 0;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--muted);
    }
    .right-panel pre {
      background: var(--surface-2);
      border: 1px solid var(--line);
      padding: 10px;
      border-radius: 8px;
      white-space: pre-wrap;
      word-break: break-all;
      max-height: 180px;
      overflow-y: auto;
      margin: 0;
      font-family: monospace;
      font-size: 11px;
      color: #e1e1e6;
    }

    .action-btn {
      width: 100%;
      min-height: 48px;
      background: var(--accent);
      color: #fff;
      border: none;
      padding: 12px;
      border-radius: 12px;
      font-weight: 600;
      font-size: 14px;
      margin-bottom: 16px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
    }
    .action-btn:active { opacity: 0.9; transform: scale(0.99); }

    .placeholder-text {
      color: var(--muted);
      text-align: center;
      margin: auto 0;
      padding: 40px 20px;
    }

    /* Pixel 7 Pro Optimized Dialog & Full Screen Video Feed */
    dialog {
      width: 100vw;
      height: 100vh;
      max-width: 100vw;
      max-height: 100vh;
      border: none;
      background: #000;
      color: var(--text);
      padding: 0;
      margin: 0;
    }
    dialog::backdrop { background: #000; }

    .modal-container {
      display: flex;
      flex-direction: column;
      height: 100vh;
      width: 100vw;
      background: #000;
      position: relative;
      overflow: hidden;
    }

    .modal-header {
      padding: calc(12px + var(--safe-top)) 16px 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: linear-gradient(to bottom, rgba(0,0,0,0.8), transparent);
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      z-index: 20;
    }

    /* Vertical Snap Feed for Edge-to-Edge Full Screen Videos */
    .video-feed {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow-y: scroll;
      overflow-x: hidden;
      scroll-snap-type: y mandatory;
      -webkit-overflow-scrolling: touch;
      height: 100vh;
      width: 100vw;
    }

    .asset-card {
      flex: 0 0 100vh;
      width: 100vw;
      height: 100vh;
      scroll-snap-align: start;
      scroll-snap-stop: always;
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      background: #000;
      padding: 0;
      margin: 0;
    }

    .asset-card video {
      width: 100vw;
      height: 100vh;
      object-fit: cover;
      display: block;
    }

    .close-btn {
      min-height: 40px;
      background: rgba(255, 255, 255, 0.2);
      border: 1px solid var(--line-strong);
      padding: 6px 16px;
      border-radius: 999px;
      font-weight: 600;
      font-size: 13px;
      backdrop-filter: blur(8px);
    }
  </style>
</head>
<body>
  <main id="mainContainer">
    <div style="color: var(--muted); text-align: center; padding: 40px;">Loading conversations...</div>
  </main>

  <aside id="rightPanel" class="right-panel">
    <div class="sheet-handle"></div>
    <div class="placeholder-text">Select a conversation tile to view records & videos.</div>
  </aside>

  <dialog id="dialog">
    <div id="detailContent" style="height: 100%;"></div>
  </dialog>

  <script>
    const els = {
      main: document.querySelector('#mainContainer'),
      dialog: document.querySelector('#dialog'),
      detail: document.querySelector('#detailContent'),
      panel: document.querySelector('#rightPanel'),
    };

    const detailCache = new Map();
    let selectedTileEl = null;

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    async function selectConversation(conv, tileEl) {
      if (selectedTileEl) {
        selectedTileEl.classList.remove('selected');
      }
      selectedTileEl = tileEl;
      selectedTileEl.classList.add('selected');

      els.panel.classList.add('open');
      els.panel.innerHTML = '<div class="sheet-handle"></div><div class="panel-title">Loading details...</div>';

      const key = conv.account + ':' + conv.id;
      try {
        let data = detailCache.get(key);
        if (!data) {
          const res = await fetch('/api/conversations/' + encodeURIComponent(conv.id) + '?account=' + encodeURIComponent(conv.account));
          if (!res.ok) throw new Error('Fetch failed');
          data = await res.json();
          detailCache.set(key, data);
        }
        renderPanelDetails(data);
      } catch (err) {
        els.panel.innerHTML = '<div class="sheet-handle"></div><div style="color: #ff6b6b; padding: 20px;">Failed to load details for this conversation.</div>';
      }
    }

    function renderPanelDetails(data) {
      const convData = data.conversation || {
        id: data.id,
        create_time: data.createTime,
        update_time: data.updateTime,
        kind: data.kind,
        account: data.account,
        raw: data.raw,
      };

      const videoAssets = data.assets ? data.assets.filter(a => a.kind === 'video') : [];

      els.panel.innerHTML = `
        <div class="sheet-handle"></div>
        <div class="panel-title">
          <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 70%;">${escapeHtml(data.id)}</span>
          <span style="font-size: 11px; font-weight: normal; color: var(--muted);">${escapeHtml(data.account)}</span>
        </div>
        <button class="action-btn" id="viewVideosBtn">
          ▶ Watch Videos (${videoAssets.length})
        </button>
        <div class="panel-section">
          <h4>Conversation Record</h4>
          <pre><code>${escapeHtml(JSON.stringify(convData, null, 2))}</code></pre>
        </div>
        <div class="panel-section">
          <h4>Responses (${data.responses ? data.responses.length : 0})</h4>
          <pre><code>${escapeHtml(JSON.stringify(data.responses || [], null, 2))}</code></pre>
        </div>
        <div class="panel-section">
          <h4>Assets (${data.assets ? data.assets.length : 0})</h4>
          <pre><code>${escapeHtml(JSON.stringify(data.assets || [], null, 2))}</code></pre>
        </div>
      `;

      const viewVideosBtn = document.getElementById('viewVideosBtn');
      if (viewVideosBtn) {
        viewVideosBtn.onclick = () => openVideosModal(data.id, data.account);
      }
    }

    async function openVideosModal(id, account) {
      const key = account + ':' + id;
      let data = detailCache.get(key);
      if (!data) {
        const res = await fetch('/api/conversations/' + encodeURIComponent(id) + '?account=' + encodeURIComponent(account));
        if (!res.ok) return;
        data = await res.json();
        detailCache.set(key, data);
      }

      const videoAssets = data.assets ? data.assets.filter(a => a.kind === 'video') : [];
      let videosHtml = '';
      if (videoAssets.length > 0) {
        videosHtml = videoAssets.map((a, idx) => {
          let mediaElement = '';
          if (a.status === 'downloaded' && a.local_path) {
            const assetUrl = '/media/' + encodeURIComponent(data.account + '/' + a.local_path);
            mediaElement = `<video id="video_${idx}" src="${assetUrl}" controls playsinline preload="metadata"></video>`;
          } else {
            mediaElement = '<div style="color: var(--muted); font-size: 13px; padding: 20px; text-align: center;">Video asset not downloaded.</div>';
          }
          return `<div class="asset-card" id="card_${idx}">${mediaElement}</div>`;
        }).join('');
      } else {
        videosHtml = '<div style="color: var(--muted); text-align: center; margin: auto;">No videos found for this conversation.</div>';
      }

      els.detail.innerHTML = `
        <div class="modal-container">
          <div class="modal-header">
            <span style="font-weight: 600; font-size: 15px;">Videos (${videoAssets.length})</span>
            <button class="close-btn" onclick="els.dialog.close()">Done</button>
          </div>
          <div class="video-feed" id="videoFeed">${videosHtml}</div>
        </div>
      `;

      els.dialog.showModal();
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
        summary.innerHTML = '<span>Account: ' + escapeHtml(account) + '</span><span style="font-weight: normal; color: var(--muted); font-size: 13px;">(' + convs.length + ')</span>';
        details.appendChild(summary);

        const grid = document.createElement('div');
        grid.className = 'grid';

        for (const conv of convs) {
          const tile = document.createElement('div');
          tile.className = 'tile';
          tile.addEventListener('click', () => selectConversation(conv, tile));
          
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

    init();
  </script>
</body>
</html>
"""
