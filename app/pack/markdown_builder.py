from __future__ import annotations
from datetime import datetime
from pathlib import Path
from app.models import RagChunk, RunConfig, TranscriptDocument
from app.pack.ai_pack_builder import build_ai_transcript_records, build_combined_transcripts_md
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision


def write_summary_input_md(path: Path, *, config: RunConfig, paths: RunPaths, created_at: datetime, decisions: list[CandidateDecision], transcript_documents: list[TranscriptDocument], chunks: list[RagChunk], counters: dict[str, int], app_version: str='unknown') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = build_ai_transcript_records(config=config, paths=paths, decisions=decisions, chunks=chunks)
    content = build_combined_transcripts_md(config=config, records=records, created_at=created_at, app_version=app_version)
    path.write_text(content, encoding='utf-8')
