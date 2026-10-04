from __future__ import annotations
from dataclasses import dataclass

from .archive import Archive, hash_file

@dataclass
class VerifySummary:
    conversations: int = 0
    images: int = 0
    videos: int = 0
    thumbnails: int = 0
    downloaded: int = 0
    failed: int = 0
    missing: int = 0
    hash_mismatches: int = 0

def verify_account(alias: str) -> VerifySummary:
    summary = VerifySummary()
    with Archive(alias) as archive:
        stats = archive.stats()
        summary.conversations = stats.get("conversations", 0)
        summary.images = stats.get("images", 0)
        summary.videos = stats.get("videos", 0)
        summary.thumbnails = stats.get("thumbnails", 0)
        
        assets = archive.db.execute("SELECT * FROM assets").fetchall()
        for row in assets:
            if row["status"] == "downloaded":
                summary.downloaded += 1
                path = archive.root / row["local_path"]
                if not path.exists():
                    summary.missing += 1
                elif row["sha256"]:
                    sha256, _ = hash_file(path)
                    if sha256 != row["sha256"]:
                        summary.hash_mismatches += 1
            elif row["status"] == "failed":
                summary.failed += 1
            else:
                summary.missing += 1
    return summary
