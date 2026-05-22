from __future__ import annotations
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from app.errors import VidScribeError
from app.models import RunConfig, VideoCandidate, YoutubeOrder
logger = logging.getLogger(__name__)

class YtDlpSearchError(VidScribeError):
    pass

class YtDlpMetadataError(Exception):
    pass

class YtDlpSearchProvider:

    def search(self, config: RunConfig) -> list[tuple[VideoCandidate, dict[str, Any]]]:
        return self._search(config, flat=False)

    def search_flat(self, config: RunConfig) -> list[tuple[VideoCandidate, dict[str, Any]]]:
        return self._search(config, flat=True)

    def _search(self, config: RunConfig, *, flat: bool) -> list[tuple[VideoCandidate, dict[str, Any]]]:
        search_spec = self._build_search_spec(config)
        cmd = self._build_command(search_spec, flat=flat)
        mode = 'flat' if flat else 'full'
        logger.info('Searching YouTube via yt-dlp %s discovery: %s', mode, search_spec)
        if config.youtube.order not in {YoutubeOrder.RELEVANCE, YoutubeOrder.DATE}:
            logger.warning("yt-dlp search supports the requested order '%s' only as best-effort; using relevance search.", config.youtube.order.value)
        completed = self._run_command(cmd)
        raw_items = self._parse_ndjson(completed.stdout)
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            self._raise_dependency_error_if_needed(stderr)
            if not raw_items:
                raise YtDlpSearchError(f'yt-dlp search failed with exit code {completed.returncode}: {stderr}')
            logger.warning('yt-dlp search returned exit code %s, but %s partial candidate records were parsed. Continuing with partial discovery. Error: %s', completed.returncode, len(raw_items), stderr)
        candidates: list[tuple[VideoCandidate, dict[str, Any]]] = []
        for index, raw in enumerate(raw_items, start=1):
            candidate = self.to_candidate(raw, config.query, index)
            candidates.append((candidate, raw))
        logger.info('yt-dlp returned %s candidate metadata records', len(candidates))
        return candidates

    def fetch_metadata(self, candidate: VideoCandidate) -> tuple[VideoCandidate, dict[str, Any]]:
        cmd = [sys.executable, '-m', 'yt_dlp', '--dump-json', '--skip-download', '--ignore-errors', '--no-warnings', '--no-playlist', candidate.url]
        try:
            completed = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
        except FileNotFoundError as exc:
            raise YtDlpMetadataError('Python executable was not found while trying to run yt-dlp. Run the command through the same Python environment where dependencies are installed.') from exc
        except OSError as exc:
            raise YtDlpMetadataError(f'Cannot run yt-dlp for {candidate.video_id}: {exc}') from exc
        raw_items = self._parse_ndjson(completed.stdout)
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            self._raise_dependency_error_if_needed(stderr)
            if not raw_items:
                raise YtDlpMetadataError(f'yt-dlp metadata extraction failed for {candidate.video_id} with exit code {completed.returncode}: {stderr}')
            logger.warning('yt-dlp metadata extraction returned exit code %s for video_id=%s, but partial JSON was parsed. Continuing.', completed.returncode, candidate.video_id)
        if not raw_items:
            stderr = completed.stderr.strip()
            raise YtDlpMetadataError(f'yt-dlp metadata extraction returned no JSON for {candidate.video_id}: {stderr}')
        raw = raw_items[0]
        enriched = self.to_candidate(raw, candidate.search_query, candidate.search_rank)
        if not enriched.video_id:
            enriched = enriched.model_copy(update={'video_id': candidate.video_id})
        if not enriched.url:
            enriched = enriched.model_copy(update={'url': candidate.url})
        return (enriched, raw)

    def _build_search_spec(self, config: RunConfig) -> str:
        prefix = 'ytsearchdate' if config.youtube.order == YoutubeOrder.DATE else 'ytsearch'
        return f'{prefix}{config.candidate_pool_size}:{config.query}'

    def _build_command(self, search_spec: str, *, flat: bool) -> list[str]:
        command = [sys.executable, '-m', 'yt_dlp', '--dump-json', '--skip-download', '--ignore-errors', '--no-warnings', '--no-playlist']
        if flat:
            command.append('--flat-playlist')
        command.append(search_spec)
        return command

    def _run_command(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
        except FileNotFoundError as exc:
            raise YtDlpSearchError('Python executable was not found while trying to run yt-dlp. Run the command through the same Python environment where dependencies are installed.') from exc
        except OSError as exc:
            raise YtDlpSearchError(f'Cannot run yt-dlp: {exc}') from exc

    def _raise_dependency_error_if_needed(self, stderr: str) -> None:
        if 'No module named yt_dlp' in stderr or 'No module named yt-dlp' in stderr:
            raise YtDlpSearchError('yt-dlp is not installed in the active Python environment. Run: python -m pip install -r requirements.txt')

    def _parse_ndjson(self, stdout: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.debug('Skipping non-JSON yt-dlp output line: %s', line)
                continue
            if isinstance(payload, dict):
                items.append(payload)
        return items

    def to_candidate(self, raw: dict[str, Any], search_query: str, search_rank: int) -> VideoCandidate:
        video_id = str(raw.get('id') or raw.get('url') or '').strip()
        if video_id.startswith('http'):
            video_id = ''
        webpage_url = str(raw.get('webpage_url') or raw.get('original_url') or raw.get('url') or '').strip()
        if video_id and (not webpage_url.startswith('http')):
            webpage_url = f'https://www.youtube.com/watch?v={video_id}'
        title = str(raw.get('title') or '').strip()
        channel_id = _optional_str(raw.get('channel_id') or raw.get('uploader_id'))
        channel_title = _optional_str(raw.get('channel') or raw.get('uploader'))
        description = _optional_str(raw.get('description'))
        language_hint = _optional_str(raw.get('language') or raw.get('availability'))
        return VideoCandidate(video_id=video_id, url=webpage_url, title=title, channel_id=channel_id, channel_title=channel_title, published_at=_parse_published_at(raw), description=description, duration_seconds=_optional_int(raw.get('duration')), view_count=_optional_int(raw.get('view_count')), like_count=_optional_int(raw.get('like_count')), language_hint=language_hint, search_query=search_query, search_rank=search_rank)

def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None

def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _parse_published_at(raw: dict[str, Any]) -> datetime | None:
    timestamp = _optional_int(raw.get('timestamp') or raw.get('release_timestamp'))
    if timestamp is not None:
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            pass
    upload_date = _optional_str(raw.get('upload_date'))
    if upload_date and len(upload_date) == 8:
        try:
            return datetime.strptime(upload_date, '%Y%m%d').replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None
