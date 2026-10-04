from __future__ import annotations

import hashlib
from typing import Any

from .archive import AssetRecord


def extract_conversation_assets(conversation: dict[str, Any]) -> list[AssetRecord]:
    assets: list[AssetRecord] = []
    conv_id = str(conversation.get("conversationId") or "").strip()
    if not conv_id:
        return assets

    latest_asset = conversation.get("latestAssetMetadata")
    if not isinstance(latest_asset, dict):
        return assets

    resp_id = str(latest_asset.get("responseId") or "").strip()

    # 1. Extract main media asset from latestAssetMetadata (e.g., generated video/file)
    file_uri = str(latest_asset.get("key") or "").strip()
    if file_uri:
        if not file_uri.startswith(("http://", "https://")):
            url = f"https://assets.grok.com/{file_uri.lstrip('/')}"
        else:
            url = file_uri

        mime_type = str(latest_asset.get("mimeType") or "").lower()
        kind = "video" if "video" in mime_type else ("image" if "image" in mime_type else "other")
        role = "media"

        asset_key = asset_key_for(conv_id, resp_id, role, url)
        assets.append(
            AssetRecord(
                asset_key=asset_key,
                conversation_id=conv_id,
                response_id=resp_id,
                kind=kind,
                role=role,
                url=url,
                mime_type=mime_type,
                source_path="latestAssetMetadata.key",
            )
        )

    # 2. Extract preview image thumbnail from auxKeys if present
    aux_keys = latest_asset.get("auxKeys")
    if isinstance(aux_keys, dict):
        preview_image = aux_keys.get("preview-image")
        if isinstance(preview_image, str) and preview_image:
            if not preview_image.startswith(("http://", "https://")):
                thumb_url = f"https://assets.grok.com/{preview_image.lstrip('/')}"
            else:
                thumb_url = preview_image

            thumb_asset_key = asset_key_for(conv_id, resp_id, "thumbnail", thumb_url)
            assets.append(
                AssetRecord(
                    asset_key=thumb_asset_key,
                    conversation_id=conv_id,
                    response_id=resp_id,
                    kind="image",
                    role="thumbnail",
                    url=thumb_url,
                    mime_type="image/jpeg",
                    source_path="latestAssetMetadata.auxKeys.preview-image",
                )
            )

    return assets


def asset_key_for(conversation_id: str, response_id: str, role: str, url: str) -> str:
    digest = hashlib.sha256(f"{conversation_id}\0{response_id}\0{role}\0{url}".encode("utf-8")).hexdigest()
    return digest


def extract_response_assets(conversation_id: str, response: dict[str, Any]) -> list[AssetRecord]:
    assets: list[AssetRecord] = []
    resp_id = str(response.get("responseId") or "").strip()
    if not resp_id:
        return assets

    sender = str(response.get("sender") or "").upper()
    if sender != "ASSISTANT":
        return []

    # 1. Existing fileAttachmentsMetadata
    file_metadata = response.get("fileAttachmentsMetadata")
    if isinstance(file_metadata, list):
        for meta in file_metadata:
            if not isinstance(meta, dict):
                continue
            file_uri = str(meta.get("fileUri") or meta.get("url") or "").strip()
            if not file_uri:
                continue
            if not file_uri.startswith(("http://", "https://")):
                url = f"https://assets.grok.com/{file_uri.lstrip('/')}"
            else:
                url = file_uri

            mime_type = str(meta.get("fileMimeType") or meta.get("mimeType") or "").lower()
            kind = "video" if "video" in mime_type else ("image" if "image" in mime_type else "other")
            role = "media"
            asset_key = asset_key_for(conversation_id, resp_id, role, url)
            assets.append(
                AssetRecord(
                    asset_key=asset_key,
                    conversation_id=conversation_id,
                    response_id=resp_id,
                    kind=kind,
                    role=role,
                    url=url,
                    mime_type=mime_type,
                    source_path="fileAttachmentsMetadata",
                )
            )

    # 2. Extract from fileAttachmentAssetMetadata
    fa_meta = response.get("fileAttachmentAssetMetadata")
    items = fa_meta if isinstance(fa_meta, list) else ([fa_meta] if isinstance(fa_meta, dict) else [])
    for meta in items:
        if not isinstance(meta, dict):
            continue
        file_uri = str(meta.get("key") or meta.get("fileUri") or meta.get("url") or "").strip()
        if not file_uri:
            continue
        if not file_uri.startswith(("http://", "https://")):
            url = f"https://assets.grok.com/{file_uri.lstrip('/')}"
        else:
            url = file_uri

        mime_type = str(meta.get("mimeType") or meta.get("fileMimeType") or "").lower()
        kind = "video" if "video" in mime_type else ("image" if "image" in mime_type else "other")
        role = "media"
        asset_key = asset_key_for(conversation_id, resp_id, role, url)
        assets.append(
            AssetRecord(
                asset_key=asset_key,
                conversation_id=conversation_id,
                response_id=resp_id,
                kind=kind,
                role=role,
                url=url,
                mime_type=mime_type,
                source_path="fileAttachmentAssetMetadata",
            )
        )

    # 3. Extract preview thumbnails from response auxKeys if present
    aux_keys = response.get("auxKeys")
    if isinstance(aux_keys, dict):
        preview_image = aux_keys.get("preview-image")
        if isinstance(preview_image, str) and preview_image:
            if not preview_image.startswith(("http://", "https://")):
                thumb_url = f"https://assets.grok.com/{preview_image.lstrip('/')}"
            else:
                thumb_url = preview_image

            thumb_asset_key = asset_key_for(conversation_id, resp_id, "thumbnail", thumb_url)
            assets.append(
                AssetRecord(
                    asset_key=thumb_asset_key,
                    conversation_id=conversation_id,
                    response_id=resp_id,
                    kind="image",
                    role="thumbnail",
                    url=thumb_url,
                    mime_type="image/jpeg",
                    source_path="response.auxKeys.preview-image",
                )
            )

    return assets
