from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .archive import Archive
from .client import GrokClient, GrokClientError
from .config import AccountConfig
from .download import _download_pending_assets_locked
from .extract import extract_conversation_assets, extract_response_assets
from .lock import ArchiveLock


@dataclass
class SyncSummary:
    conversations_seen: int = 0
    responses_seen: int = 0
    assets_seen: int = 0
    downloaded: int = 0
    errors: int = 0
    pages: int = 0


def sync_account(
    account: AccountConfig,
    *,
    full: bool = True,
    limit_conversations: int | None = None,
    page_limit: int = 40,
    download: bool = True,
    download_concurrency: int = 6,
) -> SyncSummary:
    if not full and limit_conversations is None:
        limit_conversations = 20
    summary = SyncSummary()
    mode = "full" if full else "limited"
    archive_ref = Archive(account.alias)
    with ArchiveLock(archive_ref.root / ".write.lock"), Archive(account.alias) as archive, GrokClient(account) as client:
        run_id = archive.begin_run(mode=mode, limit_conversations=limit_conversations)
        try:
            page_token = ""
            page_index = 0
            remaining = limit_conversations
            seen_tokens: set[str] = set()

            while True:
                response = client.conversation_list(pageToken=page_token, pageSize=page_limit, kind="CONVERSATION_KIND_IMAGINE")
                archive.save_page("conversations", page_index, response)
                page_index += 1
                summary.pages += 1

                conversations = response.get("conversations") if isinstance(response, dict) else None
                if not isinstance(conversations, list):
                    raise GrokClientError("conversations list response is missing conversations array")

                for conv in conversations:
                    if remaining is not None and remaining <= 0:
                        break
                    if not isinstance(conv, dict):
                        continue

                    conv_id = archive.upsert_conversation(conv)
                    if not conv_id:
                        continue
                    summary.conversations_seen += 1

                    conv_assets = extract_conversation_assets(conv)
                    summary.assets_seen += len(conv_assets)
                    archive.upsert_assets(conv_assets)

                    try:
                        resp_page_token = ""
                        resp_page = 0
                        while True:
                            resp_response = client.response_list(conv_id, pageToken=resp_page_token, conversationKind="CONVERSATION_KIND_IMAGINE", orderBy="ORDER_BY_CREATE_TIME")
                            archive.save_page(f"conv-{conv_id}-responses", resp_page, resp_response)
                            resp_page += 1

                            responses = resp_response.get("responses") if isinstance(resp_response, dict) else None
                            if isinstance(responses, list):
                                for resp in responses:
                                    if not isinstance(resp, dict):
                                        continue
                                    
                                    sender = str(resp.get("sender") or "").upper()
                                    if sender != "ASSISTANT": 
                                        continue

                                    resp_id = archive.upsert_response(conv_id, resp)
                                    if resp_id:
                                       summary.responses_seen += 1
                                       resp_assets = extract_response_assets(conv_id, resp)
                                       summary.assets_seen += len(resp_assets)
                                       archive.upsert_assets(resp_assets)

                            next_resp_token = str(
                                resp_response.get("nextPageToken") or resp_response.get("nextCursor") or ""
                            ) if isinstance(resp_response, dict) else ""
                            if not next_resp_token or next_resp_token == resp_page_token:
                                break
                            resp_page_token = next_resp_token
                    except GrokClientError as exc:
                        summary.errors += 1
                        archive.failures_dir.joinpath(f"responses-{conv_id}.txt").write_text(
                            f"{exc}\n", encoding="utf-8"
                        )

                    if remaining is not None:
                        remaining -= 1

                archive.db.commit()
                if remaining is not None and remaining <= 0:
                    break

                next_token = str(
                    response.get("nextPageToken") or response.get("nextCursor") or ""
                ) if isinstance(response, dict) else ""
                if not next_token or next_token in seen_tokens:
                    break
                seen_tokens.add(next_token)
                page_token = next_token
                if not full and limit_conversations is None:
                    break

            if download:
                download_summary = _download_pending_assets_locked(
                    account,
                    concurrency=download_concurrency,
                    retry_failed=True,
                    progress=False,
                )
                summary.downloaded += download_summary.downloaded
                summary.errors += download_summary.failed

            archive.finish_run(
                run_id,
                conversations_seen=summary.conversations_seen,
                responses_seen=summary.responses_seen,
                assets_seen=summary.assets_seen,
                errors=summary.errors,
                status="ok" if summary.errors == 0 else "partial",
            )
        except Exception:
            archive.finish_run(
                run_id,
                conversations_seen=summary.conversations_seen,
                responses_seen=summary.responses_seen,
                assets_seen=summary.assets_seen,
                errors=summary.errors + 1,
                status="failed",
            )
            raise
    return summary
