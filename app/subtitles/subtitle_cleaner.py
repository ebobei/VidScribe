from __future__ import annotations
import html
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path

class SubtitleCleaningError(Exception):
    pass

@dataclass(frozen=True)
class CleanTranscriptResult:
    raw_subtitle_path: Path
    clean_transcript_path: Path
    line_count: int
    word_count: int

class SubtitleCleaner:
    _TIMECODE_LINE_RE = re.compile('^\\s*\\d{1,2}:\\d{2}:\\d{2}[,.]\\d{3}\\s+-->\\s+\\d{1,2}:\\d{2}:\\d{2}[,.]\\d{3}.*$')
    _SRT_INDEX_RE = re.compile('^\\s*\\d+\\s*$')
    _INLINE_VTT_TIMECODE_RE = re.compile('<\\d{1,2}:\\d{2}:\\d{2}[,.]\\d{3}>')
    _HTML_TAG_RE = re.compile('<[^>]+>')
    _NOISE_TAG_RE = re.compile('\\[(?:music|applause|laughter|silence|музыка|аплодисменты|смех|тишина)\\]', flags=re.IGNORECASE)
    _WHITESPACE_RE = re.compile('\\s+')

    def clean_file(self, raw_subtitle_path: Path, clean_transcript_path: Path) -> CleanTranscriptResult:
        raw_subtitle_path = Path(raw_subtitle_path)
        clean_transcript_path = Path(clean_transcript_path)
        if not raw_subtitle_path.exists():
            raise SubtitleCleaningError(f'Raw subtitle file does not exist: {raw_subtitle_path}')
        if not raw_subtitle_path.is_file():
            raise SubtitleCleaningError(f'Raw subtitle path is not a file: {raw_subtitle_path}')
        try:
            raw_text = raw_subtitle_path.read_text(encoding='utf-8-sig', errors='replace')
        except OSError as exc:
            raise SubtitleCleaningError(f'Cannot read raw subtitle file {raw_subtitle_path}: {exc}') from exc
        lines = self._extract_text_lines(raw_text)
        if not lines:
            raise SubtitleCleaningError(f'No readable text found in subtitle file: {raw_subtitle_path}')
        plain_text = self._build_plain_text(lines)
        word_count = len(plain_text.split())
        if word_count == 0:
            raise SubtitleCleaningError(f'Cleaned transcript is empty: {raw_subtitle_path}')
        clean_transcript_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            clean_transcript_path.write_text(plain_text + '\n', encoding='utf-8')
        except OSError as exc:
            raise SubtitleCleaningError(f'Cannot write clean transcript {clean_transcript_path}: {exc}') from exc
        return CleanTranscriptResult(raw_subtitle_path=raw_subtitle_path, clean_transcript_path=clean_transcript_path, line_count=len(lines), word_count=word_count)

    def _extract_text_lines(self, raw_text: str) -> list[str]:
        result: list[str] = []
        previous_line_key: str | None = None
        for raw_line in raw_text.splitlines():
            line = raw_line.strip().lstrip('\ufeff')
            if not line:
                continue
            if self._is_service_line(line):
                continue
            cleaned = self._clean_text_line(line)
            if not cleaned:
                continue
            line_key = cleaned.casefold()
            if line_key == previous_line_key:
                continue
            result.append(cleaned)
            previous_line_key = line_key
        return result

    def _is_service_line(self, line: str) -> bool:
        upper = line.upper()
        if upper == 'WEBVTT':
            return True
        if upper.startswith(('NOTE', 'STYLE', 'REGION')):
            return True
        if line.startswith(('Kind:', 'Language:')):
            return True
        if self._SRT_INDEX_RE.match(line):
            return True
        return bool(self._TIMECODE_LINE_RE.match(line))

    def _clean_text_line(self, line: str) -> str:
        line = self._INLINE_VTT_TIMECODE_RE.sub(' ', line)
        line = self._HTML_TAG_RE.sub(' ', line)
        line = self._NOISE_TAG_RE.sub(' ', line)
        line = html.unescape(line)
        line = line.replace('♪', ' ')
        line = self._WHITESPACE_RE.sub(' ', line)
        return line.strip()

    def _build_plain_text(self, lines: list[str]) -> str:
        joined = ' '.join(lines)
        joined = self._WHITESPACE_RE.sub(' ', joined).strip()
        return textwrap.fill(joined, width=100)
