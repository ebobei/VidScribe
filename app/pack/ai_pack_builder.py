from __future__ import annotations
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from app.models import RagChunk, RunConfig, SubtitleType, VideoStatus
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision


@dataclass(frozen=True)
class TimestampedLine:
    start_time: str | None
    end_time: str | None
    text: str


@dataclass(frozen=True)
class AiTranscriptRecord:
    source_id: str
    video_id: str
    title: str
    channel: str
    channel_id: str | None
    url: str
    duration_seconds: int | None
    upload_date: str | None
    view_count: int | None
    like_count: int | None
    subtitle_language: str
    subtitle_type: str
    transcript_file: str
    word_count: int
    character_count: int
    line_count: int
    chunk_count: int
    has_timestamps: bool
    quality_flags: list[str]
    lines: list[TimestampedLine]


_TIMECODE_RE = re.compile('(?P<start>(?:\\d{1,2}:)?\\d{2}:\\d{2}[,.]\\d{3})\\s+-->\\s+(?P<end>(?:\\d{1,2}:)?\\d{2}:\\d{2}[,.]\\d{3})')
_INLINE_TIMECODE_RE = re.compile('<\\d{1,2}:\\d{2}:\\d{2}[,.]\\d{3}>')
_HTML_TAG_RE = re.compile('<[^>]+>')
_NOISE_TAG_RE = re.compile('\\[(?:music|applause|laughter|silence|музыка|аплодисменты|смех|тишина)\\]', flags=re.IGNORECASE)
_WHITESPACE_RE = re.compile('\\s+')
_SAFE_FILENAME_RE = re.compile('[^a-zA-Z0-9_-]+')


def build_ai_transcript_records(*, config: RunConfig, paths: RunPaths, decisions: list[CandidateDecision], chunks: list[RagChunk]) -> list[AiTranscriptRecord]:
    records: list[AiTranscriptRecord] = []
    chunks_by_video = _chunks_count_by_video(chunks)
    source_index = 1
    for decision in decisions:
        if decision.status != VideoStatus.PROCESSED or decision.subtitle_info is None:
            continue
        subtitle = decision.subtitle_info
        if not subtitle.clean_transcript_path:
            continue
        clean_path = paths.root / subtitle.clean_transcript_path
        raw_path = paths.root / subtitle.raw_subtitle_path
        clean_text = _read_text(clean_path)
        lines = _timestamped_lines_from_raw(raw_path)
        if not lines:
            lines = _lines_from_clean_text(clean_text)
        plain_text = _plain_text(lines)
        word_count = len(plain_text.split())
        if word_count == 0:
            continue
        source_id = f'{source_index:03d}'
        title = decision.candidate.title or decision.candidate.video_id
        transcript_file = f'transcripts/{source_id}_{_safe_slug(title, fallback=decision.candidate.video_id)}.md'
        has_timestamps = any(line.start_time for line in lines)
        flags = _quality_flags(config=config, decision=decision, word_count=word_count, has_timestamps=has_timestamps)
        records.append(AiTranscriptRecord(source_id=source_id, video_id=decision.candidate.video_id, title=title, channel=decision.candidate.channel_title or '', channel_id=decision.candidate.channel_id, url=decision.candidate.url, duration_seconds=decision.candidate.duration_seconds, upload_date=decision.candidate.published_at.date().isoformat() if decision.candidate.published_at else None, view_count=decision.candidate.view_count, like_count=decision.candidate.like_count, subtitle_language=subtitle.selected_language, subtitle_type=subtitle.subtitle_type.value, transcript_file=transcript_file, word_count=word_count, character_count=len(plain_text), line_count=len(lines), chunk_count=chunks_by_video.get(decision.candidate.video_id, 0), has_timestamps=has_timestamps, quality_flags=flags, lines=lines))
        source_index += 1
    return records


def build_ai_readme_md(*, config: RunConfig, records: list[AiTranscriptRecord], created_at: datetime, app_version: str) -> str:
    languages = _unique_sorted(record.subtitle_language for record in records)
    subtitle_types = _unique_sorted(record.subtitle_type for record in records)
    lines = [
        '# VidScribe Research Pack',
        '',
        'This archive contains AI-ready YouTube subtitle transcripts prepared by VidScribe.',
        '',
        'Video and audio files are not included.',
        '',
        '## Query',
        '',
        config.query,
        '',
        '## Generation info',
        '',
        f'- Generated at: {created_at.isoformat()}',
        f'- Tool: VidScribe {app_version}',
        '- Search/downloader: yt-dlp',
        f'- Requested videos: {config.limit}',
        f'- Videos included in this archive: {len(records)}',
        f'- Total words: {_total_words(records)}',
        f'- Total characters: {_total_characters(records)}',
        f'- Languages: {_join_or_dash(languages)}',
        f'- Subtitle types: {_join_or_dash(subtitle_types)}',
        '',
        '## Files',
        '',
        '- `combined_transcripts.md` — all included transcripts in one Markdown file.',
        '- `transcripts/` — one Markdown transcript per video.',
        '- `manifest.json` — structured metadata for included videos.',
        '- `processing_summary.md` — compact technical summary of this archive.',
        '- `analysis_prompt.md` — optional suggested prompt for broad structured AI analysis.',
        '',
        '## Optional analysis prompt',
        '',
        '`analysis_prompt.md` is not a mandatory instruction. It is a suggested helper prompt for broad analysis. If the user gives a different task, the user task takes priority.',
        '',
        '## Source index',
        '',
        _source_index_table(records),
        '',
        'Skipped, failed and diagnostic data are not included in this archive. They remain in the local `output/` run folder.',
    ]
    return '\n'.join(lines).rstrip() + '\n'


def build_combined_transcripts_md(*, config: RunConfig, records: list[AiTranscriptRecord], created_at: datetime, app_version: str) -> str:
    lines = [
        '# YouTube Transcript Research Pack',
        '',
        '## Query',
        '',
        config.query,
        '',
        '## Pack summary',
        '',
        f'- Generated at: {created_at.isoformat()}',
        f'- Tool: VidScribe {app_version}',
        f'- Videos included: {len(records)}',
        f'- Total words: {_total_words(records)}',
        f'- Total characters: {_total_characters(records)}',
        '',
        '## Source index',
        '',
        _source_index_table(records),
        '',
        '---',
        '',
    ]
    for record in records:
        lines.append(build_individual_transcript_md(record).rstrip())
        lines.extend(['', '---', ''])
    return '\n'.join(lines).rstrip() + '\n'


def build_individual_transcript_md(record: AiTranscriptRecord) -> str:
    lines = [
        f'# Transcript {record.source_id}: {record.title}',
        '',
        '## Metadata',
        '',
        f'- ID: {record.source_id}',
        f'- Video ID: {record.video_id}',
        f'- Title: {record.title}',
        f'- Channel: {record.channel}',
        f'- URL: {record.url}',
        f'- Duration: {_format_duration(record.duration_seconds)}',
        f'- Upload date: {record.upload_date or ""}',
        f'- Subtitle language: {record.subtitle_language}',
        f'- Subtitle type: {record.subtitle_type}',
        f'- Word count: {record.word_count}',
        f'- Character count: {record.character_count}',
        f'- Has timestamps: {str(record.has_timestamps).lower()}',
        f'- Quality flags: {_join_or_dash(record.quality_flags)}',
        '',
        '## Transcript',
        '',
    ]
    for line in record.lines:
        if line.start_time:
            lines.append(f'[{line.start_time}] {line.text}')
        else:
            lines.append(line.text)
    return '\n'.join(lines).rstrip() + '\n'


def build_ai_manifest(*, config: RunConfig, records: list[AiTranscriptRecord], created_at: datetime, app_version: str) -> dict[str, Any]:
    return {
        'schema_version': '1.0',
        'generated_at': created_at.isoformat(),
        'tool': {'name': 'VidScribe', 'version': app_version},
        'query': config.query,
        'config': {
            'requested_video_count': config.limit,
            'candidate_pool_size': config.candidate_pool_size,
            'min_duration_seconds': config.duration.min_seconds,
            'max_duration_seconds': config.duration.max_seconds,
            'preferred_languages': config.languages.preferred,
            'include_auto_subtitles': config.languages.allow_auto_subtitles,
            'include_manual_subtitles': config.languages.allow_manual_subtitles,
        },
        'stats': {
            'videos_included': len(records),
            'total_words': _total_words(records),
            'total_characters': _total_characters(records),
            'languages': _unique_sorted(record.subtitle_language for record in records),
            'subtitle_types': _unique_sorted(record.subtitle_type for record in records),
        },
        'videos': [_manifest_video(record) for record in records],
    }


def build_processing_summary_md(*, config: RunConfig, records: list[AiTranscriptRecord], decisions: list[CandidateDecision], created_at: datetime) -> str:
    skipped = sum(1 for decision in decisions if decision.status.value == 'skipped')
    failed = sum(1 for decision in decisions if decision.status.value == 'failed')
    flags = _unique_sorted(flag for record in records for flag in record.quality_flags)
    lines = [
        '# Processing Summary',
        '',
        'This file contains only a compact technical summary of the AI archive.',
        '',
        '## Result',
        '',
        f'- Generated at: {created_at.isoformat()}',
        f'- Query: {config.query}',
        f'- Requested videos: {config.limit}',
        f'- Candidate pool size: {config.candidate_pool_size}',
        f'- Included videos: {len(records)}',
        f'- Skipped videos outside this archive: {skipped}',
        f'- Failed videos outside this archive: {failed}',
        f'- Total words: {_total_words(records)}',
        f'- Total characters: {_total_characters(records)}',
        '',
        '## Quality flags used',
        '',
    ]
    if flags:
        for flag in flags:
            lines.append(f'- `{flag}` — {_quality_flag_description(flag)}')
    else:
        lines.append('- none')
    lines.extend(['', 'Skipped/failed videos, logs and raw diagnostic files are not included in this AI archive. See the local output folder for diagnostics.'])
    return '\n'.join(lines).rstrip() + '\n'


def build_analysis_prompt_md(*, config: RunConfig, records: list[AiTranscriptRecord], created_at: datetime, app_version: str) -> str:
    lines = [
        '# Suggested AI Analysis Prompt',
        '',
        'This file contains an optional helper prompt for analyzing this VidScribe research pack.',
        '',
        "The latest user message always takes priority. If the user asks a specific question, answer that question directly using the archive as source material. Use this prompt only when the user asks for a broad or structured analysis.",
        '',
        '## Context',
        '',
        f'- Query: {config.query}',
        f'- Generated at: {created_at.isoformat()}',
        f'- Tool: VidScribe {app_version}',
        f'- Included videos: {len(records)}',
        f'- Total words: {_total_words(records)}',
        f'- Total characters: {_total_characters(records)}',
        '',
        '## Recommended analysis task',
        '',
        'Analyze the YouTube transcript research pack using only the files in this archive.',
        '',
        'Do not invent facts. Do not use external knowledge unless the user explicitly asks for it.',
        '',
        'When possible, cite:',
        '',
        '- source ID;',
        '- video title;',
        '- timestamp;',
        '- URL.',
        '',
        '## Suggested outputs',
        '',
        '### 1. topics_index.md',
        '',
        'Group recurring topics found across videos.',
        '',
        'For each topic include:',
        '',
        '- short topic name;',
        '- what the videos say about it;',
        '- source IDs and timestamps;',
        '- whether evidence is strong, moderate, or weak.',
        '',
        '### 2. top_sources.md',
        '',
        'Select the most useful videos for the user research goal.',
        '',
        'For each video include:',
        '',
        '- source ID;',
        '- title;',
        '- why it is useful;',
        '- key timestamps;',
        '- limitations, if any.',
        '',
        '### 3. noise_report.md',
        '',
        'Identify videos or transcript sections that appear low-value for the user research goal.',
        '',
        'Do not remove sources silently. Explain why they are likely noise:',
        '',
        '- unrelated topic;',
        '- too little useful speech;',
        '- poor subtitles;',
        '- duplicate or repetitive content;',
        '- mostly entertainment or unboxing with little technical detail.',
        '',
        '### 4. evidence_map.md',
        '',
        'Map practical conclusions to supporting evidence.',
        '',
        'Use this format:',
        '',
        '| Finding | Supporting sources | Timestamps | Evidence strength | Notes |',
        '|---|---|---|---|---|',
        '',
        '## Rules',
        '',
        '- Separate direct evidence from interpretation.',
        '- Prefer repeated claims across multiple sources.',
        '- Mark uncertainty clearly.',
        '- Do not treat auto-generated subtitles as perfect.',
        '- If evidence is weak, say so.',
        '- Do not summarize skipped or failed videos unless they appear in the archive.',
        '- If the user asks a narrower question, ignore the suggested outputs and answer only that question.',
    ]
    return '\n'.join(lines).rstrip() + '\n'


def manifest_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


def _timestamped_lines_from_raw(path: Path) -> list[TimestampedLine]:
    if not path.exists() or not path.is_file():
        return []
    try:
        raw_text = path.read_text(encoding='utf-8-sig', errors='replace')
    except OSError:
        return []
    lines: list[TimestampedLine] = []
    current_start: str | None = None
    current_end: str | None = None
    current_text: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_end, current_text
        if current_start is None:
            current_text = []
            return
        text = _clean_segment_text(' '.join(current_text))
        if text:
            normalized = TimestampedLine(start_time=_normalize_time(current_start), end_time=_normalize_time(current_end), text=text)
            if not lines or lines[-1].text.casefold() != normalized.text.casefold():
                lines.append(normalized)
        current_start = None
        current_end = None
        current_text = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip().lstrip('\ufeff')
        if not line:
            flush()
            continue
        match = _TIMECODE_RE.search(line)
        if match:
            flush()
            current_start = match.group('start')
            current_end = match.group('end')
            current_text = []
            continue
        if _is_service_line(line):
            continue
        if current_start is not None:
            current_text.append(line)
    flush()
    return lines


def _lines_from_clean_text(clean_text: str) -> list[TimestampedLine]:
    text = _WHITESPACE_RE.sub(' ', clean_text).strip()
    if not text:
        return []
    return [TimestampedLine(start_time=None, end_time=None, text=text)]


def _read_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ''
    return path.read_text(encoding='utf-8', errors='replace').strip()


def _plain_text(lines: list[TimestampedLine]) -> str:
    return _WHITESPACE_RE.sub(' ', ' '.join(line.text for line in lines)).strip()


def _is_service_line(line: str) -> bool:
    upper = line.upper()
    if upper == 'WEBVTT':
        return True
    if upper.startswith(('NOTE', 'STYLE', 'REGION')):
        return True
    if line.startswith(('Kind:', 'Language:')):
        return True
    return line.isdigit()


def _clean_segment_text(value: str) -> str:
    value = _INLINE_TIMECODE_RE.sub(' ', value)
    value = _HTML_TAG_RE.sub(' ', value)
    value = _NOISE_TAG_RE.sub(' ', value)
    value = html.unescape(value)
    value = value.replace('♪', ' ')
    value = _WHITESPACE_RE.sub(' ', value)
    return value.strip()


def _normalize_time(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.replace(',', '.')
    base = value.split('.', 1)[0]
    parts = base.split(':')
    if len(parts) == 2:
        hours = 0
        minutes = int(parts[0])
        seconds = int(parts[1])
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
    else:
        return None
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def _quality_flags(*, config: RunConfig, decision: CandidateDecision, word_count: int, has_timestamps: bool) -> list[str]:
    flags: list[str] = []
    subtitle = decision.subtitle_info
    if subtitle and subtitle.subtitle_type == SubtitleType.MANUAL:
        flags.append('manual_subtitles')
    if subtitle and subtitle.subtitle_type == SubtitleType.AUTO:
        flags.append('auto_subtitles')
    if word_count < 100:
        flags.append('too_short')
    if word_count < 30:
        flags.append('very_short')
    if decision.candidate.duration_seconds and decision.candidate.duration_seconds >= 300 and word_count < 100:
        flags.append('low_text_density')
    if not has_timestamps:
        flags.append('no_timestamps')
    if subtitle and subtitle.selected_language not in config.languages.preferred:
        flags.append('non_preferred_language')
    return flags or ['ok']


def _manifest_video(record: AiTranscriptRecord) -> dict[str, Any]:
    return {
        'id': record.source_id,
        'video_id': record.video_id,
        'title': record.title,
        'channel': record.channel,
        'channel_id': record.channel_id,
        'url': record.url,
        'duration_seconds': record.duration_seconds,
        'upload_date': record.upload_date,
        'view_count': record.view_count,
        'like_count': record.like_count,
        'subtitle_language': record.subtitle_language,
        'subtitle_type': record.subtitle_type,
        'transcript_file': record.transcript_file,
        'word_count': record.word_count,
        'character_count': record.character_count,
        'line_count': record.line_count,
        'chunk_count': record.chunk_count,
        'has_timestamps': record.has_timestamps,
        'quality_flags': record.quality_flags,
    }


def _source_index_table(records: list[AiTranscriptRecord]) -> str:
    lines = ['| ID | Title | Channel | URL | Duration | Subtitle | Words | Quality flags |', '|---|---|---|---|---:|---|---:|---|']
    for record in records:
        subtitle = f'{record.subtitle_language} {record.subtitle_type}'
        lines.append(f'| {record.source_id} | {_escape_table(record.title)} | {_escape_table(record.channel)} | {record.url} | {_format_duration(record.duration_seconds)} | {subtitle} | {record.word_count} | {_escape_table(_join_or_dash(record.quality_flags))} |')
    return '\n'.join(lines)


def _quality_flag_description(flag: str) -> str:
    descriptions = {
        'ok': 'no additional formal quality warnings were added',
        'manual_subtitles': 'subtitles were manually provided',
        'auto_subtitles': 'subtitles were generated automatically',
        'too_short': 'transcript contains fewer than 100 words',
        'very_short': 'transcript contains fewer than 30 words',
        'low_text_density': 'video is at least 5 minutes long but transcript has fewer than 100 words',
        'no_timestamps': 'timestamps were unavailable or could not be parsed',
        'non_preferred_language': 'subtitle language is not in preferred languages',
    }
    return descriptions.get(flag, 'formal technical quality flag')


def _safe_slug(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    normalized = normalized.lower().replace('&', ' and ')
    normalized = _SAFE_FILENAME_RE.sub('_', normalized)
    normalized = re.sub('_+', '_', normalized).strip('_-')
    if not normalized:
        normalized = fallback
    return normalized[:80].strip('_-') or fallback


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ''
    hours = seconds // 3600
    minutes = seconds % 3600 // 60
    rest = seconds % 60
    return f'{hours:02d}:{minutes:02d}:{rest:02d}'


def _chunks_count_by_video(chunks: list[RagChunk]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in chunks:
        result[chunk.video_id] = result.get(chunk.video_id, 0) + 1
    return result


def _total_words(records: list[AiTranscriptRecord]) -> int:
    return sum(record.word_count for record in records)


def _total_characters(records: list[AiTranscriptRecord]) -> int:
    return sum(record.character_count for record in records)


def _unique_sorted(values) -> list[str]:
    return sorted({str(value) for value in values if value})


def _join_or_dash(values: list[str]) -> str:
    return ', '.join(values) if values else '-'


def _escape_table(value: str) -> str:
    return value.replace('|', '\\|').replace('\n', ' ')
