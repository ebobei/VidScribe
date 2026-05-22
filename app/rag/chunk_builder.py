from __future__ import annotations
import html
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from app.errors import VidScribeError
from app.models import RagChunk, TranscriptDocument, VideoStatus
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision
DEFAULT_CHUNK_SIZE_WORDS = 1000
DEFAULT_CHUNK_OVERLAP_WORDS = 120

class ChunkBuildError(VidScribeError):
    pass

@dataclass(frozen=True)
class TimedTextSegment:
    start_time: str | None
    end_time: str | None
    text: str

@dataclass(frozen=True)
class WordToken:
    text: str
    start_time: str | None
    end_time: str | None

@dataclass(frozen=True)
class ChunkBuildResult:
    documents: list[TranscriptDocument]
    chunks: list[RagChunk]

class ChunkBuilder:
    _TIMECODE_RE = re.compile('(?P<start>(?:\\d{1,2}:)?\\d{2}:\\d{2}[,.]\\d{3})\\s+-->\\s+(?P<end>(?:\\d{1,2}:)?\\d{2}:\\d{2}[,.]\\d{3})')
    _HTML_TAG_RE = re.compile('<[^>]+>')
    _INLINE_VTT_TIMECODE_RE = re.compile('<\\d{1,2}:\\d{2}:\\d{2}[,.]\\d{3}>')
    _NOISE_TAG_RE = re.compile('\\[(?:music|applause|laughter|silence|музыка|аплодисменты|смех|тишина)\\]', flags=re.IGNORECASE)
    _WHITESPACE_RE = re.compile('\\s+')

    def __init__(self, *, chunk_size_words: int=DEFAULT_CHUNK_SIZE_WORDS, overlap_words: int=DEFAULT_CHUNK_OVERLAP_WORDS) -> None:
        if chunk_size_words <= 0:
            raise ChunkBuildError('chunk_size_words must be greater than 0')
        if overlap_words < 0:
            raise ChunkBuildError('overlap_words must be greater than or equal to 0')
        if overlap_words >= chunk_size_words:
            raise ChunkBuildError('overlap_words must be less than chunk_size_words')
        self.chunk_size_words = chunk_size_words
        self.overlap_words = overlap_words

    def build(self, *, paths: RunPaths, decisions: list[CandidateDecision], collected_at: datetime) -> ChunkBuildResult:
        documents: list[TranscriptDocument] = []
        chunks: list[RagChunk] = []
        for decision in decisions:
            if decision.status != VideoStatus.PROCESSED or decision.subtitle_info is None:
                continue
            subtitle = decision.subtitle_info
            if not subtitle.clean_transcript_path:
                continue
            clean_transcript_path = paths.root / subtitle.clean_transcript_path
            raw_subtitle_path = paths.root / subtitle.raw_subtitle_path
            clean_text = self._read_clean_transcript(clean_transcript_path)
            if not clean_text:
                continue
            document = TranscriptDocument(document_id=f'yt_{decision.candidate.video_id}', video_id=decision.candidate.video_id, title=decision.candidate.title, channel_title=decision.candidate.channel_title, url=decision.candidate.url, language=subtitle.selected_language, subtitle_type=subtitle.subtitle_type, text_path=subtitle.clean_transcript_path, collected_at=collected_at)
            documents.append(document)
            tokens = self._tokens_from_raw_subtitle(raw_subtitle_path)
            if not tokens:
                tokens = self._tokens_from_clean_text(clean_text)
            chunks.extend(self._build_chunks_for_document(document=document, decision=decision, tokens=tokens))
        return ChunkBuildResult(documents=documents, chunks=chunks)

    def _read_clean_transcript(self, path: Path) -> str:
        if not path.exists():
            raise ChunkBuildError(f'Clean transcript file does not exist: {path}')
        if not path.is_file():
            raise ChunkBuildError(f'Clean transcript path is not a file: {path}')
        try:
            return path.read_text(encoding='utf-8', errors='replace').strip()
        except OSError as exc:
            raise ChunkBuildError(f'Cannot read clean transcript file {path}: {exc}') from exc

    def _tokens_from_raw_subtitle(self, path: Path) -> list[WordToken]:
        if not path.exists() or not path.is_file():
            return []
        try:
            raw_text = path.read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            return []
        segments = self._parse_timed_segments(raw_text)
        tokens: list[WordToken] = []
        for segment in segments:
            for word in segment.text.split():
                tokens.append(WordToken(text=word, start_time=segment.start_time, end_time=segment.end_time))
        return tokens

    def _parse_timed_segments(self, raw_text: str) -> list[TimedTextSegment]:
        segments: list[TimedTextSegment] = []
        current_start: str | None = None
        current_end: str | None = None
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_start, current_end, current_lines
            if current_start is None or current_end is None:
                current_lines = []
                return
            text = self._clean_segment_text(' '.join(current_lines))
            if text:
                segments.append(TimedTextSegment(start_time=current_start, end_time=current_end, text=text))
            current_start = None
            current_end = None
            current_lines = []
        for raw_line in raw_text.splitlines():
            line = raw_line.strip().lstrip('\ufeff')
            if not line:
                flush()
                continue
            time_match = self._TIMECODE_RE.search(line)
            if time_match:
                flush()
                current_start = self._normalize_timecode(time_match.group('start'))
                current_end = self._normalize_timecode(time_match.group('end'))
                continue
            if current_start is None:
                continue
            if self._is_non_text_line(line):
                continue
            current_lines.append(line)
        flush()
        return segments

    def _tokens_from_clean_text(self, clean_text: str) -> list[WordToken]:
        return [WordToken(text=word, start_time=None, end_time=None) for word in clean_text.split()]

    def _build_chunks_for_document(self, *, document: TranscriptDocument, decision: CandidateDecision, tokens: list[WordToken]) -> list[RagChunk]:
        if not tokens:
            return []
        result: list[RagChunk] = []
        step = self.chunk_size_words - self.overlap_words
        start = 0
        chunk_index = 0
        while start < len(tokens):
            end = min(start + self.chunk_size_words, len(tokens))
            window = tokens[start:end]
            text = ' '.join((token.text for token in window)).strip()
            if text:
                result.append(RagChunk(chunk_id=f'{document.document_id}_chunk_{chunk_index:04d}', document_id=document.document_id, video_id=document.video_id, title=document.title, channel_title=document.channel_title, url=document.url, language=document.language, subtitle_type=document.subtitle_type, chunk_index=chunk_index, start_time=self._first_time(window, attr='start_time'), end_time=self._last_time(window, attr='end_time'), text=text, token_estimate=self._estimate_tokens(text)))
                chunk_index += 1
            if end >= len(tokens):
                break
            start += step
        return result

    def _clean_segment_text(self, text: str) -> str:
        text = self._INLINE_VTT_TIMECODE_RE.sub(' ', text)
        text = self._HTML_TAG_RE.sub(' ', text)
        text = self._NOISE_TAG_RE.sub(' ', text)
        text = html.unescape(text)
        text = text.replace('♪', ' ')
        text = self._WHITESPACE_RE.sub(' ', text)
        return text.strip()

    def _is_non_text_line(self, line: str) -> bool:
        upper = line.upper()
        if upper == 'WEBVTT':
            return True
        if upper.startswith(('NOTE', 'STYLE', 'REGION')):
            return True
        if line.startswith(('Kind:', 'Language:')):
            return True
        return bool(line.isdigit())

    def _normalize_timecode(self, value: str) -> str:
        value = value.replace(',', '.')
        if value.count(':') == 1:
            return f'00:{value}'
        return value

    def _first_time(self, tokens: list[WordToken], *, attr: str) -> str | None:
        for token in tokens:
            value = getattr(token, attr)
            if value:
                return value
        return None

    def _last_time(self, tokens: list[WordToken], *, attr: str) -> str | None:
        for token in reversed(tokens):
            value = getattr(token, attr)
            if value:
                return value
        return None

    def _estimate_tokens(self, text: str) -> int:
        word_count = len(text.split())
        return max(1, math.ceil(word_count * 1.3))

def write_chunks_jsonl(path: Path, chunks: list[RagChunk]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('w', encoding='utf-8') as file:
            for chunk in chunks:
                payload = chunk.model_dump(mode='json')
                file.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except OSError as exc:
        raise ChunkBuildError(f'Cannot write chunks JSONL {path}: {exc}') from exc
