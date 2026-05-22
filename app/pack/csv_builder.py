"""CSV artifact builder for VidScribe Stage 6."""

from __future__ import annotations

import csv
from pathlib import Path

from app.search.video_filter import CandidateDecision


CSV_FIELDS = [
    "index",
    "video_id",
    "status",
    "reason",
    "title",
    "channel_title",
    "url",
    "published_at",
    "duration_seconds",
    "view_count",
    "like_count",
    "search_rank",
    "selected_language",
    "subtitle_type",
    "subtitle_format",
    "raw_subtitle_path",
    "clean_transcript_path",
    "document_id",
    "chunks_count",
    "error_message",
]


def write_videos_csv(
    path: Path,
    decisions: list[CandidateDecision],
    *,
    chunks_by_video: dict[str, int] | None = None,
) -> None:
    chunks_by_video = chunks_by_video or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for index, decision in enumerate(decisions, start=1):
            candidate = decision.candidate
            subtitle = decision.subtitle_info
            has_document = bool(subtitle and subtitle.clean_transcript_path)
            writer.writerow(
                {
                    "index": index,
                    "video_id": candidate.video_id,
                    "status": decision.status.value,
                    "reason": decision.reason.value if decision.reason else "",
                    "title": candidate.title,
                    "channel_title": candidate.channel_title or "",
                    "url": candidate.url,
                    "published_at": candidate.published_at.isoformat() if candidate.published_at else "",
                    "duration_seconds": candidate.duration_seconds if candidate.duration_seconds is not None else "",
                    "view_count": candidate.view_count if candidate.view_count is not None else "",
                    "like_count": candidate.like_count if candidate.like_count is not None else "",
                    "search_rank": candidate.search_rank,
                    "selected_language": subtitle.selected_language if subtitle else "",
                    "subtitle_type": subtitle.subtitle_type.value if subtitle else "",
                    "subtitle_format": subtitle.subtitle_format if subtitle else "",
                    "raw_subtitle_path": subtitle.raw_subtitle_path if subtitle else "",
                    "clean_transcript_path": subtitle.clean_transcript_path if subtitle and subtitle.clean_transcript_path else "",
                    "document_id": f"yt_{candidate.video_id}" if has_document else "",
                    "chunks_count": chunks_by_video.get(candidate.video_id, 0),
                    "error_message": decision.error_message or "",
                }
            )
