from __future__ import annotations
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from app.models import SubtitleInfo, SubtitleType, VideoCandidate
from app.subtitles.subtitle_selector import SubtitleSelection
logger = logging.getLogger(__name__)

class YtDlpSubtitleError(Exception):
    pass

class YtDlpSubtitleNoFileError(YtDlpSubtitleError):
    pass

class YtDlpSubtitleDownloader:

    def download(self, *, index: int, candidate: VideoCandidate, selection: SubtitleSelection, run_root: Path, subtitles_raw_dir: Path) -> SubtitleInfo:
        temp_dir = subtitles_raw_dir / '_tmp' / f'{index:03d}__{candidate.video_id}'
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)
        cmd = self._build_command(candidate.url, selection, temp_dir)
        logger.info('Downloading subtitle: video_id=%s language=%s type=%s format=%s', candidate.video_id, selection.selected_language, selection.subtitle_type.value, selection.subtitle_format)
        try:
            completed = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
        except OSError as exc:
            raise YtDlpSubtitleError(f'Cannot run yt-dlp for {candidate.video_id}: {exc}') from exc
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            if 'No module named yt_dlp' in stderr or 'No module named yt-dlp' in stderr:
                raise YtDlpSubtitleError('yt-dlp is not installed in the active Python environment. Run: python -m pip install -r requirements.txt')
            raise YtDlpSubtitleError(f'yt-dlp subtitle download failed for {candidate.video_id} with exit code {completed.returncode}: {stderr}')
        downloaded_file = self._find_downloaded_subtitle(temp_dir)
        if downloaded_file is None:
            raise YtDlpSubtitleNoFileError(f'yt-dlp did not create a subtitle file for {candidate.video_id} ({selection.selected_language}, {selection.subtitle_type.value})')
        final_path = self._move_to_final_path(index=index, candidate=candidate, selection=selection, source_path=downloaded_file, subtitles_raw_dir=subtitles_raw_dir)
        self._cleanup_temp_dir(temp_dir)
        return SubtitleInfo(video_id=candidate.video_id, selected_language=selection.selected_language, subtitle_type=selection.subtitle_type, subtitle_format=final_path.suffix.lstrip('.').lower(), raw_subtitle_path=final_path.relative_to(run_root).as_posix(), clean_transcript_path=None, status='downloaded')

    def _build_command(self, url: str, selection: SubtitleSelection, temp_dir: Path) -> list[str]:
        command = [sys.executable, '-m', 'yt_dlp', '--skip-download', '--no-playlist', '--no-warnings', '--sub-langs', selection.selected_language, '--sub-format', f'{selection.subtitle_format}/best', '--paths', str(temp_dir), '--output', '%(id)s.%(ext)s']
        if selection.subtitle_type == SubtitleType.MANUAL:
            command.append('--write-subs')
        else:
            command.append('--write-auto-subs')
        command.append(url)
        return command

    def _find_downloaded_subtitle(self, temp_dir: Path) -> Path | None:
        candidates = [path for path in temp_dir.rglob('*') if path.is_file() and path.suffix.lower() in {'.vtt', '.srt'} and (not path.name.endswith('.part'))]
        if not candidates:
            return None
        return sorted(candidates, key=lambda path: (path.suffix.lower() != '.vtt', path.name))[0]

    def _move_to_final_path(self, *, index: int, candidate: VideoCandidate, selection: SubtitleSelection, source_path: Path, subtitles_raw_dir: Path) -> Path:
        language = _safe_filename_part(selection.selected_language)
        subtitle_type = _safe_filename_part(selection.subtitle_type.value)
        extension = source_path.suffix.lower().lstrip('.') or selection.subtitle_format
        final_path = subtitles_raw_dir / f'{index:03d}__{candidate.video_id}.{language}.{subtitle_type}.{extension}'
        if final_path.exists():
            final_path.unlink()
        shutil.move(str(source_path), str(final_path))
        return final_path

    def _cleanup_temp_dir(self, temp_dir: Path) -> None:
        tmp_root = temp_dir.parent
        shutil.rmtree(temp_dir, ignore_errors=True)
        try:
            tmp_root.rmdir()
        except OSError:
            pass

def _safe_filename_part(value: str) -> str:
    clean = re.sub('[^a-zA-Z0-9_.-]+', '_', value.strip())
    return clean.strip('._-') or 'unknown'
