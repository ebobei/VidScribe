"""Manifest artifact builder for VidScribe Stage 6."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models import RagChunk, RunArtifact, RunConfig, TranscriptDocument, VideoStatus
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision


def build_manifest(
    *,
    config: RunConfig,
    paths: RunPaths,
    created_at: datetime,
    decisions: list[CandidateDecision],
    artifacts: list[RunArtifact],
    transcript_documents: list[TranscriptDocument],
    chunks: list[RagChunk],
) -> dict[str, Any]:
    processed = [item for item in decisions if item.status == VideoStatus.PROCESSED]
    skipped = [item for item in decisions if item.status == VideoStatus.SKIPPED]
    failed = [item for item in decisions if item.status == VideoStatus.FAILED]
    subtitle_downloaded = [item for item in decisions if item.subtitle_info is not None]
    clean_transcripts = [
        item for item in decisions if item.subtitle_info is not None and item.subtitle_info.clean_transcript_path
    ]
    chunks_by_video = _chunks_count_by_video(chunks)

    return {
        "run_id": paths.run_id,
        "project_name": config.project_name,
        "stage": "stage_6",
        "status": "completed",
        "collected_at": created_at.isoformat(),
        "query": config.query,
        "search_provider": "yt-dlp",
        "subtitle_provider": "yt-dlp",
        "transcript_cleaner": "local-vtt-srt-cleaner",
        "chunk_builder": "local-word-window-jsonl",
        "search_note": (
            "Stage 6 uses local yt-dlp search/extraction instead of Google Cloud / YouTube Data API. "
            "Video and audio files are not downloaded. Subtitle commands always use --skip-download. "
            "Raw subtitle files are cleaned locally into transcripts_clean/*.txt, then converted into "
            "chunks/chunks.jsonl without embeddings or a vector DB. Stage 6 also builds summary_input.md and research_pack.zip."
        ),
        "config": config.model_dump(mode="json"),
        "chunking": {
            "enabled": config.output.build_chunks_jsonl,
            "chunk_size_words": 1000,
            "overlap_words": 120,
            "timestamps_note": (
                "Chunk start_time/end_time are best-effort values derived from raw subtitle cue timings. "
                "They may be null when timing data cannot be parsed."
            ),
        },
        "summary": {
            "total_candidates_written": len(decisions),
            "processed_count": len(processed),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "downloaded_subtitles_count": len(subtitle_downloaded),
            "clean_transcripts_count": len(clean_transcripts),
            "transcript_documents_count": len(transcript_documents),
            "chunks_count": len(chunks),
        },
        "documents": [document.model_dump(mode="json") for document in transcript_documents],
        "videos": [_video_payload(item, chunks_by_video=chunks_by_video) for item in decisions],
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
    }


def _video_payload(item: CandidateDecision, *, chunks_by_video: dict[str, int]) -> dict[str, Any]:
    subtitle = item.subtitle_info
    has_document = bool(subtitle and subtitle.clean_transcript_path)
    return {
        "video_id": item.candidate.video_id,
        "status": item.status.value,
        "reason": item.reason.value if item.reason else None,
        "error_message": item.error_message,
        "title": item.candidate.title,
        "channel_title": item.candidate.channel_title,
        "url": item.candidate.url,
        "published_at": item.candidate.published_at.isoformat() if item.candidate.published_at else None,
        "duration_seconds": item.candidate.duration_seconds,
        "view_count": item.candidate.view_count,
        "like_count": item.candidate.like_count,
        "search_rank": item.candidate.search_rank,
        "subtitle_info": subtitle.model_dump(mode="json") if subtitle else None,
        "clean_transcript_path": subtitle.clean_transcript_path if subtitle else None,
        "document_id": f"yt_{item.candidate.video_id}" if has_document else None,
        "chunks_count": chunks_by_video.get(item.candidate.video_id, 0),
    }


def _chunks_count_by_video(chunks: list[RagChunk]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in chunks:
        result[chunk.video_id] = result.get(chunk.video_id, 0) + 1
    return result
