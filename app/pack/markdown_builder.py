from __future__ import annotations
from datetime import datetime
from pathlib import Path
from app.models import RagChunk, RunConfig, TranscriptDocument
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision


def write_summary_input_md(path: Path, *, config: RunConfig, paths: RunPaths, created_at: datetime, decisions: list[CandidateDecision], transcript_documents: list[TranscriptDocument], chunks: list[RagChunk], counters: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks_by_video = _chunks_count_by_video(chunks)
    lines: list[str] = []
    lines.extend([
        '# VidScribe AI Input',
        '',
        '## Research query',
        '',
        config.query,
        '',
        '## Input contents',
        '',
        f'- Successfully processed videos: {len(transcript_documents)}',
        f'- Collected at: {created_at.isoformat()}',
        '- Video and audio files are not included.',
        '- Only successfully processed transcripts are included.',
        '',
        '## Transcripts',
        '',
    ])
    if not transcript_documents:
        lines.extend(['No clean transcripts were produced in this run.', ''])
    else:
        for index, document in enumerate(transcript_documents, start=1):
            transcript_text = _read_transcript(paths.root / document.text_path)
            subtitle_type = document.subtitle_type.value.replace('_', ' ')
            lines.extend([
                f'### {index:03d} — {document.title}',
                '',
                f'Channel: {document.channel_title or ""}',
                f'URL: {document.url}',
                f'Subtitles: {document.language}, {subtitle_type}',
                f'Chunks: {chunks_by_video.get(document.video_id, 0)}',
                '',
                'Transcript:',
                '',
                transcript_text,
                '',
                '---',
                '',
            ])
    path.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')


def _read_transcript(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ''
    return path.read_text(encoding='utf-8', errors='replace').strip()


def _chunks_count_by_video(chunks: list[RagChunk]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in chunks:
        result[chunk.video_id] = result.get(chunk.video_id, 0) + 1
    return result
