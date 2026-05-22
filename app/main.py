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
from app.paths import RunPaths, create_run_paths, load_existing_run_paths
from app.rag.chunk_builder import ChunkBuilder, write_chunks_jsonl
from app.search.video_filter import CandidateDecision, VideoFilter
from app.search.ytdlp_search_provider import YtDlpSearchProvider
from app.state.sqlite_store import SQLiteStateStore
from app.subtitles.subtitle_cleaner import SubtitleCleaner, SubtitleCleaningError
from app.subtitles.subtitle_selector import SubtitleSelector
from app.subtitles.ytdlp_client import YtDlpSubtitleDownloader, YtDlpSubtitleError, YtDlpSubtitleNoFileError
from app.worker.stable_worker import StableWorker
APP_NAME = 'VidScribe'
APP_VERSION = '0.7.1-stage7.1'
logger = logging.getLogger(__name__)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m app.main', description='VidScribe — local CLI utility for collecting research-ready YouTube subtitle packs.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    collect = subparsers.add_parser('collect', help='Search YouTube via yt-dlp and build a local research pack.')
    collect.add_argument('--config', required=True, help='Path to YAML request config.')
    collect.add_argument('--limit', type=int, help='Override config limit.')
    collect.add_argument('--candidate-pool-size', type=int, help='Override config candidate_pool_size.')
    collect.add_argument('--order', choices=[item.value for item in YoutubeOrder], help='Override YouTube search order. relevance/date are supported best; others are best-effort.')
    collect.add_argument('--output-directory', help='Override output.directory from YAML config.')
    collect.add_argument('--stable', action='store_true', help='Enable stable overnight mode: SQLite state, one-by-one processing, pauses and retry/backoff.')
    collect.add_argument('--verbose', action='store_true', help='Enable verbose logging.')
    resume = subparsers.add_parser('resume', help='Resume a stable run from an existing output run directory.')
    resume.add_argument('--run-dir', required=True, help='Path to an existing output run directory with state.sqlite.')
    resume.add_argument('--verbose', action='store_true', help='Enable verbose logging.')
    pack = subparsers.add_parser('pack', help='Rebuild videos.csv, summary_input.md, manifest.json and research_pack.zip from an existing stable run.')
    pack.add_argument('--run-dir', required=True, help='Path to an existing output run directory with state.sqlite.')
    pack.add_argument('--verbose', action='store_true', help='Enable verbose logging.')
    return parser

def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
        file.write('\n')

def build_stage7_artifacts(paths: RunPaths, config: RunConfig) -> list[RunArtifact]:
    artifacts = [RunArtifact(name='run.json', path=paths.relative_to_root(paths.run_json), description='Resolved run metadata and counters.'), RunArtifact(name='manifest.json', path=paths.relative_to_root(paths.manifest_json), description='Main manifest with query, counters, videos, transcript info and chunk info.'), RunArtifact(name='videos.csv', path=paths.relative_to_root(paths.videos_csv), description='Video candidates with processed/skipped/failed statuses and reasons.'), RunArtifact(name='state.sqlite', path=paths.relative_to_root(paths.state_db), description='SQLite state for stable runs, resume and attempt history.'), RunArtifact(name='metadata/', path=paths.relative_to_root(paths.metadata_dir), description='Raw yt-dlp metadata JSON files for videos with downloaded subtitles.'), RunArtifact(name='subtitles_raw/', path=paths.relative_to_root(paths.subtitles_raw_dir), description='Raw selected subtitle files downloaded through yt-dlp with --skip-download.'), RunArtifact(name='transcripts_clean/', path=paths.relative_to_root(paths.transcripts_clean_dir), description='Cleaned plain-text transcripts generated from raw subtitle files.'), RunArtifact(name='chunks/chunks.jsonl', path=paths.relative_to_root(paths.chunks_jsonl), description='RAG-ready JSONL chunks generated from clean transcripts.')]
    if config.output.build_summary_md:
        artifacts.append(RunArtifact(name='summary_input.md', path=paths.relative_to_root(paths.summary_input_md), description='Human-readable research input file with run context, video table and clean transcripts.'))
    artifacts.append(RunArtifact(name='collect.log', path=paths.relative_to_root(paths.collect_log), description='CLI log file for this collection run.'))
    if config.output.build_zip:
        artifacts.append(RunArtifact(name='research_pack.zip', path=paths.relative_to_root(paths.research_pack_zip), description='ZIP archive containing the local research pack for this run.'))
    return artifacts

def write_collected_metadata(paths: RunPaths, decisions: list[CandidateDecision]) -> None:
    collected_index = 1
    for decision in decisions:
        if decision.subtitle_info is None:
            continue
        video_id = decision.candidate.video_id
        filename = f'{collected_index:03d}__{video_id}.json'
        payload = {'candidate': decision.candidate.model_dump(mode='json'), 'status': decision.status.value, 'reason': decision.reason.value if decision.reason else None, 'error_message': decision.error_message, 'subtitle_info': decision.subtitle_info.model_dump(mode='json'), 'yt_dlp_metadata': decision.raw_metadata}
        write_json(paths.metadata_dir / filename, payload)
        collected_index += 1

def count_decisions(decisions: list[CandidateDecision]) -> dict[str, int]:
    return {'total_candidates_written': len(decisions), 'processed_count': sum((1 for item in decisions if item.status == VideoStatus.PROCESSED)), 'skipped_count': sum((1 for item in decisions if item.status == VideoStatus.SKIPPED)), 'failed_count': sum((1 for item in decisions if item.status == VideoStatus.FAILED)), 'downloaded_subtitles_count': sum((1 for item in decisions if item.subtitle_info is not None)), 'clean_transcripts_count': sum((1 for item in decisions if item.subtitle_info is not None and item.subtitle_info.clean_transcript_path))}

def download_subtitles(*, config: RunConfig, paths: RunPaths, decisions: list[CandidateDecision]) -> list[CandidateDecision]:
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
            logger.info('No suitable subtitles found in metadata: video_id=%s', decision.candidate.video_id)
            updated_decisions.append(replace(decision, status=VideoStatus.SKIPPED, reason=SkipReason.NO_SUBTITLES, error_message='No suitable subtitles found for preferred languages and allowed subtitle types.'))
            accepted_index += 1
            continue
        try:
            subtitle_info = downloader.download(index=accepted_index, candidate=decision.candidate, selection=selection, run_root=paths.root, subtitles_raw_dir=paths.subtitles_raw_dir)
        except YtDlpSubtitleNoFileError as exc:
            logger.warning('Subtitle metadata existed but no subtitle file was downloaded: %s', exc)
            updated_decisions.append(replace(decision, status=VideoStatus.SKIPPED, reason=SkipReason.NO_SUBTITLES, error_message=str(exc)))
        except YtDlpSubtitleError as exc:
            logger.error('Subtitle download failed: %s', exc)
            reason = SkipReason.RATE_LIMITED if '429' in str(exc) else SkipReason.YT_DLP_ERROR
            updated_decisions.append(replace(decision, status=VideoStatus.FAILED, reason=reason, error_message=str(exc)))
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
            logger.error('Subtitle cleaning failed: %s', exc)
            updated_decisions.append(replace(decision, status=VideoStatus.FAILED, reason=SkipReason.SUBTITLE_CLEANING_ERROR, error_message=str(exc)))
            continue
        updated_subtitle = subtitle.model_copy(update={'clean_transcript_path': result.clean_transcript_path.relative_to(paths.root).as_posix()})
        updated_decisions.append(replace(decision, subtitle_info=updated_subtitle))
    return updated_decisions

def _build_clean_transcript_path(paths: RunPaths, raw_subtitle_relative_path: str) -> Path:
    raw_name = Path(raw_subtitle_relative_path).name
    stem = Path(raw_name).stem
    return paths.transcripts_clean_dir / f'{stem}.txt'

def build_chunks(*, config: RunConfig, paths: RunPaths, decisions: list[CandidateDecision], collected_at: datetime) -> tuple[list[TranscriptDocument], list[RagChunk]]:
    if not config.output.build_chunks_jsonl:
        logger.info('Chunk generation disabled by output.build_chunks_jsonl=false')
        return ([], [])
    builder = ChunkBuilder()
    result = builder.build(paths=paths, decisions=decisions, collected_at=collected_at)
    write_chunks_jsonl(paths.chunks_jsonl, result.chunks)
    return (result.documents, result.chunks)

def _chunks_count_by_video(chunks: list[RagChunk]) -> dict[str, int]:
    result: dict[str, int] = {}
    for chunk in chunks:
        result[chunk.video_id] = result.get(chunk.video_id, 0) + 1
    return result

def build_final_artifacts(*, config: RunConfig, paths: RunPaths, decisions: list[CandidateDecision], created_at: datetime, config_path: str, cli_overrides: dict[str, Any], status: str='completed') -> dict[str, int]:
    transcript_documents, chunks = build_chunks(config=config, paths=paths, decisions=decisions, collected_at=created_at)
    chunks_by_video = _chunks_count_by_video(chunks)
    counters = count_decisions(decisions)
    counters.update({'transcript_documents_count': len(transcript_documents), 'chunks_count': len(chunks), 'summary_input_built': 1 if config.output.build_summary_md else 0, 'research_pack_built': 1 if config.output.build_zip else 0})
    write_collected_metadata(paths, decisions)
    write_videos_csv(paths.videos_csv, decisions, chunks_by_video=chunks_by_video)
    if config.output.build_summary_md:
        write_summary_input_md(paths.summary_input_md, config=config, paths=paths, created_at=created_at, decisions=decisions, transcript_documents=transcript_documents, chunks=chunks, counters=counters)
        logger.info('summary_input.md written: %s', paths.summary_input_md)
    artifacts = build_stage7_artifacts(paths, config)
    manifest = build_manifest(config=config, paths=paths, created_at=created_at, decisions=decisions, artifacts=artifacts, transcript_documents=transcript_documents, chunks=chunks, stage='stage_7_1')
    manifest['status'] = status
    write_json(paths.manifest_json, manifest)
    state = RunState(run_id=paths.run_id, project_name=config.project_name, status=status, stage='stage_7_1', created_at=created_at, config_path=config_path, cli_overrides=cli_overrides, output_root=str(paths.root), config=config.model_dump(mode='json'), artifacts=artifacts, counters=counters)
    write_json(paths.run_json, state.model_dump(mode='json'))
    if config.output.build_zip:
        zip_path = build_research_pack_zip(paths)
        logger.info('research_pack.zip written: %s', zip_path)
    return counters

def discover_and_filter(config: RunConfig) -> list[CandidateDecision]:
    search_provider = YtDlpSearchProvider()
    raw_candidates = search_provider.search(config)
    video_filter = VideoFilter()
    decisions = video_filter.filter_candidates(raw_candidates, config)
    logger.info('Candidate filtering complete before subtitle stage: %s', count_decisions(decisions))
    return decisions

def discover_stable_queue(config: RunConfig) -> list[CandidateDecision]:
    search_provider = YtDlpSearchProvider()
    raw_candidates = search_provider.search_flat(config)
    decisions: list[CandidateDecision] = []
    seen_video_ids: set[str] = set()
    for candidate, raw in raw_candidates:
        if not candidate.video_id or not candidate.url or not candidate.title:
            decisions.append(CandidateDecision(candidate=candidate, raw_metadata=raw, status=VideoStatus.SKIPPED, reason=SkipReason.METADATA_ERROR, error_message='Flat discovery returned incomplete video metadata.'))
            continue
        if candidate.video_id in seen_video_ids:
            decisions.append(CandidateDecision(candidate=candidate, raw_metadata=raw, status=VideoStatus.SKIPPED, reason=SkipReason.DUPLICATE, error_message='Duplicate video_id returned by discovery.'))
            continue
        seen_video_ids.add(candidate.video_id)
        decisions.append(CandidateDecision(candidate=candidate, raw_metadata=raw, status=VideoStatus.PROCESSED))
    logger.info('Stable flat discovery complete: %s', count_decisions(decisions))
    return decisions

def run_collect(args: argparse.Namespace) -> int:
    overrides = {'limit': args.limit, 'candidate_pool_size': args.candidate_pool_size, 'order': args.order, 'output_directory': args.output_directory}
    visible_overrides = {key: value for key, value in overrides.items() if value is not None}
    config = load_run_config(args.config, overrides)
    if args.stable:
        config = config.model_copy(update={'stability': config.stability.model_copy(update={'enabled': True})})
    paths = create_run_paths(config.output.directory, config.project_name)
    setup_logging(paths.collect_log, verbose=args.verbose)
    logger.info('%s %s', APP_NAME, APP_VERSION)
    logger.info('Config validated: %s', args.config)
    logger.info('Output run directory created: %s', paths.root)
    logger.info('Safety invariant: video/audio download remains disabled; subtitle commands always use --skip-download')
    if config.limit > 20 or config.candidate_pool_size > 60:
        logger.warning('Large collection detected: limit=%s candidate_pool_size=%s. Stable mode is recommended.', config.limit, config.candidate_pool_size)
    created_at = datetime.now().astimezone()
    if config.stability.enabled:
        decisions = discover_stable_queue(config)
        logger.info('Stage 7.1 stable mode enabled: SQLite state=%s, sleep=%s-%s sec, 429 backoff=%s sec, max_attempts=%s', paths.state_db, config.stability.video_sleep_min_seconds, config.stability.video_sleep_max_seconds, config.stability.rate_limit_backoff_seconds, config.stability.max_attempts_per_video)
        store = SQLiteStateStore(paths.state_db)
        store.save_run_info(run_id=paths.run_id, created_at=created_at, config=config, config_path=str(Path(args.config)), cli_overrides=visible_overrides, output_root=str(paths.root))
        store.replace_decisions(decisions)
        worker = StableWorker(config=config, paths=paths, store=store)
        decisions = worker.run()
    else:
        decisions = discover_and_filter(config)
        logger.info('Stage 7.1 regular mode: processing in a single pass without stable sleeps/resume.')
        decisions = download_subtitles(config=config, paths=paths, decisions=decisions)
        decisions = clean_transcripts(paths=paths, decisions=decisions)
    counters = build_final_artifacts(config=config, paths=paths, decisions=decisions, created_at=created_at, config_path=str(Path(args.config)), cli_overrides=visible_overrides, status='completed')
    print_summary(paths, counters, stable=config.stability.enabled)
    return 0

def run_resume(args: argparse.Namespace) -> int:
    paths = load_existing_run_paths(args.run_dir)
    setup_logging(paths.collect_log, verbose=args.verbose)
    store = SQLiteStateStore(paths.state_db)
    info = store.load_run_info()
    config = RunConfig.model_validate(info['config'])
    created_at = datetime.fromisoformat(info['created_at'])
    logger.info('Resuming stable run: %s', paths.root)
    worker = StableWorker(config=config, paths=paths, store=store)
    decisions = worker.run()
    counters = build_final_artifacts(config=config, paths=paths, decisions=decisions, created_at=created_at, config_path=str(info.get('config_path') or ''), cli_overrides=dict(info.get('cli_overrides') or {}), status='completed')
    print_summary(paths, counters, stable=True)
    return 0

def run_pack(args: argparse.Namespace) -> int:
    paths = load_existing_run_paths(args.run_dir)
    setup_logging(paths.collect_log, verbose=args.verbose)
    store = SQLiteStateStore(paths.state_db)
    info = store.load_run_info()
    config = RunConfig.model_validate(info['config'])
    created_at = datetime.fromisoformat(info['created_at'])
    decisions = store.load_decisions()
    logger.info('Building pack from existing stable run: %s', paths.root)
    counters = build_final_artifacts(config=config, paths=paths, decisions=decisions, created_at=created_at, config_path=str(info.get('config_path') or ''), cli_overrides=dict(info.get('cli_overrides') or {}), status='partial' if counters_have_failures(decisions) else 'completed')
    print_summary(paths, counters, stable=True)
    return 0

def counters_have_failures(decisions: list[CandidateDecision]) -> bool:
    return any((item.status == VideoStatus.FAILED for item in decisions))

def print_summary(paths: RunPaths, counters: dict[str, int], *, stable: bool) -> None:
    mode = 'Stage 7.1 stable' if stable else 'Stage 7.1 regular'
    print(f'OK: {mode} run completed at {paths.root}')
    print(f'Run ID: {paths.run_id}')
    print(f"Processed videos with clean transcripts: {counters['processed_count']}")
    print(f"Skipped videos: {counters['skipped_count']}")
    print(f"Failed videos: {counters['failed_count']}")
    print(f"Downloaded subtitles: {counters['downloaded_subtitles_count']}")
    print(f"Clean transcripts: {counters['clean_transcripts_count']}")
    print(f"Transcript documents: {counters['transcript_documents_count']}")
    print(f"Chunks: {counters['chunks_count']}")
    print(f'Videos CSV: {paths.videos_csv}')
    print(f'Manifest: {paths.manifest_json}')
    if paths.summary_input_md.exists():
        print(f'Summary input: {paths.summary_input_md}')
    if paths.research_pack_zip.exists():
        print(f'Research pack ZIP: {paths.research_pack_zip}')
    if paths.state_db.exists():
        print(f'SQLite state: {paths.state_db}')

def main(argv: list[str] | None=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'collect':
            return run_collect(args)
        if args.command == 'resume':
            return run_resume(args)
        if args.command == 'pack':
            return run_pack(args)
        parser.error(f'Unknown command: {args.command}')
        return 2
    except VidScribeError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted by user', file=sys.stderr)
        return 130
if __name__ == '__main__':
    raise SystemExit(main())
