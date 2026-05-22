from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from app.errors import PathPreparationError

@dataclass(frozen=True)
class RunPaths:
    run_id: str
    root: Path
    metadata_dir: Path
    subtitles_raw_dir: Path
    transcripts_clean_dir: Path
    chunks_dir: Path
    logs_dir: Path
    state_db: Path
    run_json: Path
    manifest_json: Path
    videos_csv: Path
    summary_input_md: Path
    chunks_jsonl: Path
    collect_log: Path
    research_pack_zip: Path

    def relative_to_root(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

def slugify_project_name(project_name: str) -> str:
    value = project_name.strip().lower()
    value = re.sub('[^a-z0-9а-яё_-]+', '_', value, flags=re.IGNORECASE)
    value = re.sub('_+', '_', value).strip('_')
    return value or 'vidscribe_run'

def build_run_paths_from_root(root: str | Path) -> RunPaths:
    root_path = Path(root)
    return RunPaths(run_id=root_path.name, root=root_path, metadata_dir=root_path / 'metadata', subtitles_raw_dir=root_path / 'subtitles_raw', transcripts_clean_dir=root_path / 'transcripts_clean', chunks_dir=root_path / 'chunks', logs_dir=root_path / 'logs', state_db=root_path / 'state.sqlite', run_json=root_path / 'run.json', manifest_json=root_path / 'manifest.json', videos_csv=root_path / 'videos.csv', summary_input_md=root_path / 'summary_input.md', chunks_jsonl=root_path / 'chunks' / 'chunks.jsonl', collect_log=root_path / 'logs' / 'collect.log', research_pack_zip=root_path / 'research_pack.zip')

def ensure_run_directories(paths: RunPaths) -> None:
    try:
        for directory in [paths.root, paths.metadata_dir, paths.subtitles_raw_dir, paths.transcripts_clean_dir, paths.chunks_dir, paths.logs_dir]:
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathPreparationError(f'Cannot create output directories under {paths.root}: {exc}') from exc

def create_run_paths(base_directory: str | Path, project_name: str, now: datetime | None=None) -> RunPaths:
    timestamp = (now or datetime.now().astimezone()).strftime('%Y-%m-%d_%H%M')
    slug = slugify_project_name(project_name)
    base = Path(base_directory)
    run_id_base = f'{slug}_{timestamp}'
    root = base / run_id_base
    suffix = 2
    while root.exists():
        root = base / f'{run_id_base}_{suffix}'
        suffix += 1
    paths = build_run_paths_from_root(root)
    ensure_run_directories(paths)
    return paths

def load_existing_run_paths(run_dir: str | Path) -> RunPaths:
    paths = build_run_paths_from_root(run_dir)
    if not paths.root.exists() or not paths.root.is_dir():
        raise PathPreparationError(f'Run directory does not exist: {paths.root}')
    ensure_run_directories(paths)
    return paths
