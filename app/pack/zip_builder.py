"""ZIP research pack builder for VidScribe Stage 6."""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.errors import VidScribeError
from app.paths import RunPaths


class ZipBuildError(VidScribeError):
    """Raised when research_pack.zip cannot be created."""


def build_research_pack_zip(paths: RunPaths) -> Path:
    """Create research_pack.zip from the current run artifacts.

    The ZIP intentionally contains only local run outputs. It never includes
    itself and it skips temporary yt-dlp working directories.
    """

    zip_path = paths.research_pack_zip
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    try:
        with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in _iter_pack_files(paths):
                archive.write(file_path, arcname=file_path.relative_to(paths.root).as_posix())
    except OSError as exc:
        raise ZipBuildError(f"Cannot create research pack ZIP {zip_path}: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise ZipBuildError(f"Invalid ZIP state while creating {zip_path}: {exc}") from exc

    return zip_path


def _iter_pack_files(paths: RunPaths) -> list[Path]:
    candidates: list[Path] = []

    for file_path in [
        paths.run_json,
        paths.manifest_json,
        paths.videos_csv,
        paths.summary_input_md,
        paths.chunks_jsonl,
        paths.collect_log,
    ]:
        if file_path.exists() and file_path.is_file():
            candidates.append(file_path)

    for directory in [
        paths.metadata_dir,
        paths.subtitles_raw_dir,
        paths.transcripts_clean_dir,
        paths.chunks_dir,
        paths.logs_dir,
    ]:
        if not directory.exists() or not directory.is_dir():
            continue
        for file_path in directory.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path == paths.research_pack_zip:
                continue
            if "_tmp" in file_path.relative_to(paths.root).parts:
                continue
            if file_path not in candidates:
                candidates.append(file_path)

    return sorted(candidates, key=lambda path: path.relative_to(paths.root).as_posix())
