from __future__ import annotations
import zipfile
from pathlib import Path
from app.errors import VidScribeError
from app.paths import RunPaths


class ZipBuildError(VidScribeError):
    pass


def build_research_pack_zip(paths: RunPaths) -> Path:
    zip_path = paths.research_pack_zip
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    try:
        with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path, arcname in _iter_ai_pack_files(paths):
                archive.write(file_path, arcname=arcname)
    except OSError as exc:
        raise ZipBuildError(f'Cannot create research pack ZIP {zip_path}: {exc}') from exc
    except zipfile.BadZipFile as exc:
        raise ZipBuildError(f'Invalid ZIP state while creating {zip_path}: {exc}') from exc
    return zip_path


def _iter_ai_pack_files(paths: RunPaths) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    if paths.summary_input_md.exists() and paths.summary_input_md.is_file():
        files.append((paths.summary_input_md, paths.summary_input_md.name))
        return files
    if paths.transcripts_clean_dir.exists() and paths.transcripts_clean_dir.is_dir():
        for file_path in sorted(paths.transcripts_clean_dir.glob('*.txt')):
            files.append((file_path, f'transcripts/{file_path.name}'))
    return files
