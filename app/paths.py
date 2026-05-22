"""Output path preparation for a VidScribe run."""

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
    value = re.sub(r"[^a-z0-9а-яё_-]+", "_", value, flags=re.IGNORECASE)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "vidscribe_run"


def create_run_paths(base_directory: str | Path, project_name: str, now: datetime | None = None) -> RunPaths:
    timestamp = (now or datetime.now().astimezone()).strftime("%Y-%m-%d_%H%M")
    slug = slugify_project_name(project_name)
    base = Path(base_directory)
    run_id_base = f"{slug}_{timestamp}"

    root = base / run_id_base
    suffix = 2
    while root.exists():
        root = base / f"{run_id_base}_{suffix}"
        suffix += 1

    run_id = root.name

    paths = RunPaths(
        run_id=run_id,
        root=root,
        metadata_dir=root / "metadata",
        subtitles_raw_dir=root / "subtitles_raw",
        transcripts_clean_dir=root / "transcripts_clean",
        chunks_dir=root / "chunks",
        logs_dir=root / "logs",
        run_json=root / "run.json",
        manifest_json=root / "manifest.json",
        videos_csv=root / "videos.csv",
        summary_input_md=root / "summary_input.md",
        chunks_jsonl=root / "chunks" / "chunks.jsonl",
        collect_log=root / "logs" / "collect.log",
        research_pack_zip=root / "research_pack.zip",
    )

    try:
        for directory in [
            paths.root,
            paths.metadata_dir,
            paths.subtitles_raw_dir,
            paths.transcripts_clean_dir,
            paths.chunks_dir,
            paths.logs_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PathPreparationError(f"Cannot create output directories under {root}: {exc}") from exc

    return paths
