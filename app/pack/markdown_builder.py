from __future__ import annotations
from datetime import datetime
from pathlib import Path
from app.models import RagChunk, RunConfig, TranscriptDocument, VideoStatus
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision
MAX_TABLE_TEXT_LENGTH = 90

def write_summary_input_md(path: Path, *, config: RunConfig, paths: RunPaths, created_at: datetime, decisions: list[CandidateDecision], transcript_documents: list[TranscriptDocument], chunks: list[RagChunk], counters: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks_by_video = _chunks_count_by_video(chunks)
    documents_by_video = {document.video_id: document for document in transcript_documents}
    lines: list[str] = []
    lines.extend(['# VidScribe Research Pack', '', '## Suggested analysis prompt', '', 'Проанализируй этот VidScribe research pack. Выдели повторяющиеся мнения, полезные факты, риски, практические выводы и рекомендации для проекта. Не пересказывай все подряд — сгруппируй выводы по темам и укажи, из каких видео они следуют.', '', '## Run context', '', f'- Project: `{config.project_name}`', f'- Query: `{config.query}`', f'- Collected at: `{created_at.isoformat()}`', '- Provider: `yt-dlp search/extraction`', '- Safety: video/audio download disabled; subtitles only.', '', '## Counters', '', '| Metric | Value |', '|---|---:|'])
    for key in sorted(counters):
        lines.append(f'| `{_escape_table_cell(key)}` | {counters[key]} |')
    lines.extend(['', '## Config snapshot', '', '| Parameter | Value |', '|---|---|', f'| `limit` | `{config.limit}` |', f'| `candidate_pool_size` | `{config.candidate_pool_size}` |', f'| `duration.min_seconds` | `{config.duration.min_seconds}` |', f'| `duration.max_seconds` | `{config.duration.max_seconds}` |', f"| `languages.preferred` | `{', '.join(config.languages.preferred)}` |", f'| `languages.allow_manual_subtitles` | `{config.languages.allow_manual_subtitles}` |', f'| `languages.allow_auto_subtitles` | `{config.languages.allow_auto_subtitles}` |', f'| `youtube.order` | `{config.youtube.order.value}` |', f'| `youtube.exclude_shorts` | `{config.youtube.exclude_shorts}` |', f'| `youtube.exclude_live` | `{config.youtube.exclude_live}` |', '', '## Video status table', '', '| # | Status | Reason | Title | Channel | Duration | Language | Subtitle | Chunks | URL |', '|---:|---|---|---|---|---:|---|---|---:|---|'])
    for index, decision in enumerate(decisions, start=1):
        candidate = decision.candidate
        subtitle = decision.subtitle_info
        lines.append(f"| {index} | {_escape_table_cell(decision.status.value)} | {_escape_table_cell(decision.reason.value if decision.reason else '')} | {_escape_table_cell(_shorten(candidate.title))} | {_escape_table_cell(_shorten(candidate.channel_title or ''))} | {candidate.duration_seconds or ''} | {_escape_table_cell(subtitle.selected_language if subtitle else '')} | {_escape_table_cell(subtitle.subtitle_type.value if subtitle else '')} | {chunks_by_video.get(candidate.video_id, 0)} | {_escape_table_cell(candidate.url)} |")
    lines.extend(['', '## Processed documents', ''])
    if not transcript_documents:
        lines.extend(['No clean transcripts were produced in this run.', ''])
    else:
        processed_index = 1
        for decision in decisions:
            document = documents_by_video.get(decision.candidate.video_id)
            if document is None:
                continue
            transcript_text = _read_transcript(paths.root / document.text_path)
            lines.extend([f'### {processed_index:03d} — {document.title}', '', f'- Document ID: `{document.document_id}`', f'- Video ID: `{document.video_id}`', f"- Channel: `{document.channel_title or ''}`", f'- URL: {document.url}', f'- Language: `{document.language}`', f'- Subtitle type: `{document.subtitle_type.value}`', f'- Clean transcript path: `{document.text_path}`', f'- Chunks: `{chunks_by_video.get(document.video_id, 0)}`', '', '#### Transcript', '', _fenced_text_block(transcript_text), ''])
            processed_index += 1
    lines.extend(['## Artifact paths', '', f'- `run.json`: `{paths.relative_to_root(paths.run_json)}`', f'- `manifest.json`: `{paths.relative_to_root(paths.manifest_json)}`', f'- `videos.csv`: `{paths.relative_to_root(paths.videos_csv)}`', f'- `chunks.jsonl`: `{paths.relative_to_root(paths.chunks_jsonl)}`', f'- `summary_input.md`: `{paths.relative_to_root(paths.summary_input_md)}`', f'- `research_pack.zip`: `{paths.relative_to_root(paths.research_pack_zip)}`', ''])
    path.write_text('\n'.join(lines), encoding='utf-8')

def _read_transcript(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ''
    return path.read_text(encoding='utf-8', errors='replace').strip()

def _chunks_count_by_video(chunks: list[RagChunk]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in chunks:
        result[chunk.video_id] = result.get(chunk.video_id, 0) + 1
    return result

def _shorten(value: str, max_length: int=MAX_TABLE_TEXT_LENGTH) -> str:
    clean = ' '.join(value.split())
    if len(clean) <= max_length:
        return clean
    return clean[:max_length - 1].rstrip() + '…'

def _escape_table_cell(value: str) -> str:
    return str(value).replace('|', '\\|').replace('\n', ' ').strip()

def _fenced_text_block(text: str) -> str:
    fence = '```'
    while fence in text:
        fence += '`'
    return f'{fence}text\n{text}\n{fence}'
