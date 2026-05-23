from __future__ import annotations
import zipfile
from pathlib import Path
from datetime import datetime
from app.errors import VidScribeError
from app.models import RagChunk, RunConfig
from app.pack.ai_pack_builder import build_ai_manifest, build_ai_readme_md, build_ai_transcript_records, build_combined_transcripts_md, build_individual_transcript_md, build_processing_summary_md, manifest_json_text
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision


class ZipBuildError(VidScribeError):
    pass


def build_research_pack_zip(*, paths: RunPaths, config: RunConfig, decisions: list[CandidateDecision], chunks: list[RagChunk], created_at: datetime, app_version: str) -> Path:
    zip_path = paths.research_pack_zip
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    records = build_ai_transcript_records(config=config, paths=paths, decisions=decisions, chunks=chunks)
    try:
        with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('README.md', build_ai_readme_md(config=config, records=records, created_at=created_at, app_version=app_version))
            archive.writestr('manifest.json', manifest_json_text(build_ai_manifest(config=config, records=records, created_at=created_at, app_version=app_version)))
            archive.writestr('combined_transcripts.md', build_combined_transcripts_md(config=config, records=records, created_at=created_at, app_version=app_version))
            archive.writestr('processing_summary.md', build_processing_summary_md(config=config, records=records, decisions=decisions, created_at=created_at))
            for record in records:
                archive.writestr(record.transcript_file, build_individual_transcript_md(record))
    except OSError as exc:
        raise ZipBuildError(f'Cannot create research pack ZIP {zip_path}: {exc}') from exc
    except zipfile.BadZipFile as exc:
        raise ZipBuildError(f'Invalid ZIP state while creating {zip_path}: {exc}') from exc
    return zip_path
