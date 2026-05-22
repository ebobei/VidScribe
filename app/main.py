"""VidScribe CLI entry point.

Stage 6 validates YAML config, searches YouTube through yt-dlp metadata
extraction, filters candidates, downloads one best subtitle track per accepted
video, cleans subtitle files into plain text transcripts, builds JSONL chunks,
writes summary_input.md and creates research_pack.zip.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import load_run_config
from app.errors import VidScribeError
from app.logging_config import setup_logging
from app.models import RagChunk, RunArtifact, RunConfig, RunState, SkipReason, TranscriptDocument, VideoStatus, YoutubeOrder
from app.pack.csv_builder import write_videos_csv
from app.pack.manifest_builder import build_manifest
from app.pack.markdown_builder import write_summary_input_md
from app.pack.zip_builder import build_research_pack_zip
from app.paths import RunPaths, create_run_paths
from app.rag.chunk_builder import ChunkBuilder, write_chunks_jsonl
from app.search.video_filter import CandidateDecision, VideoFilter
from app.search.ytdlp_search_provider import YtDlpSearchProvider
from app.subtitles.subtitle_cleaner import SubtitleCleaner, SubtitleCleaningError
from app.subtitles.subtitle_selector import SubtitleSelector
from app.subtitles.ytdlp_client import (
    YtDlpSubtitleDownloader,
    YtDlpSubtitleError,
    YtDlpSubtitleNoFileError,
)

APP_NAME = "VidScribe"
APP_VERSION = "0.6.0-stage6"

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.main",
        description="VidScribe — local CLI utility for collecting research-ready YouTube subtitle packs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect",
        help="Search YouTube via yt-dlp and build a local research pack.",
    )
    collect.add_argument("--config", required=True, help="Path to YAML request config.")
    collect.add_argument("--limit", type=int, help="Override config limit.")
    collect.add_argument(
        "--candidate-pool-size",
        type=int,
        help="Override config candidate_pool_size.",
    )
    collect.add_argument(
        "--order",
        choices=[item.value for item in YoutubeOrder],
        help="Override YouTube search order. In Stage 6, relevance/date are supported best; others are best-effort.",
    )
    collect.add_argument(
        "--output-directory",
        help="Override output.directory from YAML config.",
    )
    collect.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )

    return parser


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
        file.write("\n")


def build_stage6_artifacts(paths: RunPaths, config: RunConfig) -> list[RunArtifact]:
    artifacts = [
        RunArtifact(
            name="run.json",
            path=paths.relative_to_root(paths.run_json),
            description="Resolved run configuration, counters and Stage 6 run metadata.",
        ),
        RunArtifact(
            name="manifest.json",
            path=paths.relative_to_root(paths.manifest_json),
            description="Main manifest with query, provider, counters, videos, subtitle info, transcript info and chunk info.",
        ),
        RunArtifact(
            name="videos.csv",
            path=paths.relative_to_root(paths.videos_csv),
            description="Video candidates with processed/skipped/failed statuses, metadata, transcript paths and chunk counts.",
        ),
        RunArtifact(
            name="metadata/",
            path=paths.relative_to_root(paths.metadata_dir),
            description="Raw yt-dlp metadata JSON files for videos with downloaded subtitles.",
        ),
        RunArtifact(
            name="subtitles_raw/",
            path=paths.relative_to_root(paths.subtitles_raw_dir),
            description="Raw selected subtitle files downloaded through yt-dlp with --skip-download.",
        ),
        RunArtifact(
            name="transcripts_clean/",
            path=paths.relative_to_root(paths.transcripts_clean_dir),
            description="Cleaned plain-text transcripts generated from raw subtitle files.",
        ),
        RunArtifact(
            name="chunks/chunks.jsonl",
            path=paths.relative_to_root(paths.chunks_jsonl),
            description="RAG-ready JSONL chunks generated from clean transcripts.",
        ),
    ]

    if config.output.build_summary_md:
        artifacts.append(
            RunArtifact(
                name="summary_input.md",
                path=paths.relative_to_root(paths.summary_input_md),
                description="Human-readable research input file with run context, video table and clean transcripts.",
            )
        )

    artifacts.append(
        RunArtifact(
            name="collect.log",
            path=paths.relative_to_root(paths.collect_log),
            description="CLI log file for this collection run.",
        )
    )

    if config.output.build_zip:
        artifacts.append(
            RunArtifact(
                name="research_pack.zip",
                path=paths.relative_to_root(paths.research_pack_zip),
                description="ZIP archive containing the complete local research pack for this run.",
            )
        )

    return artifacts


def write_collected_metadata(paths: RunPaths, decisions: list[CandidateDecision]) -> None:
    collected_index = 1
    for decision in decisions:
        if decision.subtitle_info is None:
            continue
        video_id = decision.candidate.video_id
        filename = f"{collected_index:03d}__{video_id}.json"
        payload = {
            "candidate": decision.candidate.model_dump(mode="json"),
            "status": decision.status.value,
            "reason": decision.reason.value if decision.reason else None,
            "error_message": decision.error_message,
            "subtitle_info": decision.subtitle_info.model_dump(mode="json"),
            "yt_dlp_metadata": decision.raw_metadata,
        }
        write_json(paths.metadata_dir / filename, payload)
        collected_index += 1


def count_decisions(decisions: list[CandidateDecision]) -> dict[str, int]:
    return {
        "total_candidates_written": len(decisions),
        "processed_count": sum(1 for item in decisions if item.status == VideoStatus.PROCESSED),
        "skipped_count": sum(1 for item in decisions if item.status == VideoStatus.SKIPPED),
        "failed_count": sum(1 for item in decisions if item.status == VideoStatus.FAILED),
        "downloaded_subtitles_count": sum(1 for item in decisions if item.subtitle_info is not None),
        "clean_transcripts_count": sum(
            1 for item in decisions if item.subtitle_info is not None and item.subtitle_info.clean_transcript_path
        ),
    }


def download_subtitles(
    *,
    config: RunConfig,
    paths: RunPaths,
    decisions: list[CandidateDecision],
) -> list[CandidateDecision]:
    selector = SubtitleSelector()
    downloader = YtDlpSubtitleDownloader()
    updated_decisions: list[CandidateDecision] = []
    accepted_index = 1

    for decision in decisions:
        if decision.status != VideoStatus.PROCESSED:
            updated_decisions.append(decision)
            continue

        selection = selector.select(decision.raw_metadata, config)
        if selection is None:
            logger.info("No suitable subtitles found in metadata: video_id=%s", decision.candidate.video_id)
            updated_decisions.append(
                replace(
                    decision,
                    status=VideoStatus.SKIPPED,
                    reason=SkipReason.NO_SUBTITLES,
                    error_message="No suitable subtitles found for preferred languages and allowed subtitle types.",
                )
            )
            accepted_index += 1
            continue

        try:
            subtitle_info = downloader.download(
                index=accepted_index,
                candidate=decision.candidate,
                selection=selection,
                run_root=paths.root,
                subtitles_raw_dir=paths.subtitles_raw_dir,
            )
        except YtDlpSubtitleNoFileError as exc:
            logger.warning("Subtitle metadata existed but no subtitle file was downloaded: %s", exc)
            updated_decisions.append(
                replace(
                    decision,
                    status=VideoStatus.SKIPPED,
                    reason=SkipReason.NO_SUBTITLES,
                    error_message=str(exc),
                )
            )
        except YtDlpSubtitleError as exc:
            logger.error("Subtitle download failed: %s", exc)
            updated_decisions.append(
                replace(
                    decision,
                    status=VideoStatus.FAILED,
                    reason=SkipReason.YT_DLP_ERROR,
                    error_message=str(exc),
                )
            )
        else:
            updated_decisions.append(replace(decision, subtitle_info=subtitle_info))

        accepted_index += 1

    return updated_decisions


def clean_transcripts(*, paths: RunPaths, decisions: list[CandidateDecision]) -> list[CandidateDecision]:
    cleaner = SubtitleCleaner()
    updated_decisions: list[CandidateDecision] = []

    for decision in decisions:
        subtitle = decision.subtitle_info
        if decision.status != VideoStatus.PROCESSED or subtitle is None:
            updated_decisions.append(decision)
            continue

        raw_path = paths.root / subtitle.raw_subtitle_path
        clean_path = _build_clean_transcript_path(paths, subtitle.raw_subtitle_path)

        try:
            result = cleaner.clean_file(raw_path, clean_path)
        except SubtitleCleaningError as exc:
            logger.error("Subtitle cleaning failed: %s", exc)
            updated_decisions.append(
                replace(
                    decision,
                    status=VideoStatus.FAILED,
                    reason=SkipReason.SUBTITLE_CLEANING_ERROR,
                    error_message=str(exc),
                )
            )
            continue

        logger.info(
            "Clean transcript written: video_id=%s path=%s words=%s",
            decision.candidate.video_id,
            result.clean_transcript_path,
            result.word_count,
        )
        updated_subtitle = subtitle.model_copy(
            update={"clean_transcript_path": result.clean_transcript_path.relative_to(paths.root).as_posix()}
        )
        updated_decisions.append(replace(decision, subtitle_info=updated_subtitle))

    return updated_decisions


def _build_clean_transcript_path(paths: RunPaths, raw_subtitle_relative_path: str) -> Path:
    raw_name = Path(raw_subtitle_relative_path).name
    stem = Path(raw_name).stem
    return paths.transcripts_clean_dir / f"{stem}.txt"


def build_chunks(
    *,
    config: RunConfig,
    paths: RunPaths,
    decisions: list[CandidateDecision],
    collected_at: datetime,
) -> tuple[list[TranscriptDocument], list[RagChunk]]:
    if not config.output.build_chunks_jsonl:
        logger.info("Chunk generation disabled by output.build_chunks_jsonl=false")
        return [], []

    builder = ChunkBuilder()
    result = builder.build(paths=paths, decisions=decisions, collected_at=collected_at)
    write_chunks_jsonl(paths.chunks_jsonl, result.chunks)
    return result.documents, result.chunks


def _chunks_count_by_video(chunks: list[RagChunk]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in chunks:
        result[chunk.video_id] = result.get(chunk.video_id, 0) + 1
    return result


def run_collect(args: argparse.Namespace) -> int:
    overrides = {
        "limit": args.limit,
        "candidate_pool_size": args.candidate_pool_size,
        "order": args.order,
        "output_directory": args.output_directory,
    }
    visible_overrides = {key: value for key, value in overrides.items() if value is not None}

    config = load_run_config(args.config, overrides)
    paths = create_run_paths(config.output.directory, config.project_name)
    setup_logging(paths.collect_log, verbose=args.verbose)

    logger.info("%s %s", APP_NAME, APP_VERSION)
    logger.info("Config validated: %s", args.config)
    logger.info("Output run directory created: %s", paths.root)
    logger.info("Stage 6 provider: yt-dlp search + yt-dlp subtitles + local cleaner + local chunk builder + pack builder")
    logger.info("Safety invariant: video/audio download remains disabled; subtitle commands always use --skip-download")

    created_at = datetime.now().astimezone()

    search_provider = YtDlpSearchProvider()
    raw_candidates = search_provider.search(config)

    video_filter = VideoFilter()
    decisions = video_filter.filter_candidates(raw_candidates, config)
    logger.info("Candidate filtering complete before subtitle stage: %s", count_decisions(decisions))

    decisions = download_subtitles(config=config, paths=paths, decisions=decisions)
    logger.info("Subtitle stage complete: %s", count_decisions(decisions))

    decisions = clean_transcripts(paths=paths, decisions=decisions)
    counters = count_decisions(decisions)
    logger.info("Transcript cleaning stage complete: %s", counters)

    transcript_documents, chunks = build_chunks(
        config=config,
        paths=paths,
        decisions=decisions,
        collected_at=created_at,
    )
    counters.update(
        {
            "transcript_documents_count": len(transcript_documents),
            "chunks_count": len(chunks),
        }
    )
    logger.info("Chunk generation stage complete: documents=%s chunks=%s", len(transcript_documents), len(chunks))

    chunks_by_video = _chunks_count_by_video(chunks)

    counters["summary_input_built"] = 1 if config.output.build_summary_md else 0
    counters["research_pack_built"] = 1 if config.output.build_zip else 0

    write_collected_metadata(paths, decisions)
    write_videos_csv(paths.videos_csv, decisions, chunks_by_video=chunks_by_video)

    if config.output.build_summary_md:
        write_summary_input_md(
            paths.summary_input_md,
            config=config,
            paths=paths,
            created_at=created_at,
            decisions=decisions,
            transcript_documents=transcript_documents,
            chunks=chunks,
            counters=counters,
        )
        logger.info("summary_input.md written: %s", paths.summary_input_md)
    else:
        logger.info("summary_input.md generation disabled by output.build_summary_md=false")

    artifacts = build_stage6_artifacts(paths, config)
    manifest = build_manifest(
        config=config,
        paths=paths,
        created_at=created_at,
        decisions=decisions,
        artifacts=artifacts,
        transcript_documents=transcript_documents,
        chunks=chunks,
    )
    write_json(paths.manifest_json, manifest)

    state = RunState(
        run_id=paths.run_id,
        project_name=config.project_name,
        status="completed",
        stage="stage_6",
        created_at=created_at,
        config_path=str(Path(args.config)),
        cli_overrides=visible_overrides,
        output_root=str(paths.root),
        config=config.model_dump(mode="json"),
        artifacts=artifacts,
        counters=counters,
    )

    write_json(paths.run_json, state.model_dump(mode="json"))
    logger.info("run.json written: %s", paths.run_json)
    logger.info("manifest.json written: %s", paths.manifest_json)
    logger.info("videos.csv written: %s", paths.videos_csv)

    if config.output.build_zip:
        zip_path = build_research_pack_zip(paths)
        logger.info("research_pack.zip written: %s", zip_path)
    else:
        logger.info("research_pack.zip generation disabled by output.build_zip=false")

    logger.info("Stage 6 complete. MVP research pack is ready.")

    print(f"OK: Stage 6 run completed at {paths.root}")
    print(f"Run ID: {paths.run_id}")
    print(f"Processed videos with clean transcripts: {counters['processed_count']}")
    print(f"Skipped videos: {counters['skipped_count']}")
    print(f"Failed videos: {counters['failed_count']}")
    print(f"Downloaded subtitles: {counters['downloaded_subtitles_count']}")
    print(f"Clean transcripts: {counters['clean_transcripts_count']}")
    print(f"Transcript documents: {counters['transcript_documents_count']}")
    print(f"Chunks: {counters['chunks_count']}")
    print(f"Chunks JSONL: {paths.chunks_jsonl}")
    if config.output.build_summary_md:
        print(f"Summary input: {paths.summary_input_md}")
    if config.output.build_zip:
        print(f"Research pack ZIP: {paths.research_pack_zip}")
    print(f"Videos CSV: {paths.videos_csv}")
    print(f"Manifest: {paths.manifest_json}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "collect":
            return run_collect(args)
        parser.error(f"Unknown command: {args.command}")
        return 2
    except VidScribeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
