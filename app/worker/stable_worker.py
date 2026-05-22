from __future__ import annotations
import logging
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from app.models import RunConfig, SkipReason, VideoStatus
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision, VideoFilter
from app.search.ytdlp_search_provider import YtDlpMetadataError, YtDlpSearchProvider
from app.state.sqlite_store import SQLiteStateStore, is_fully_processed, mark_failed
from app.subtitles.subtitle_cleaner import SubtitleCleaner, SubtitleCleaningError
from app.subtitles.subtitle_selector import SubtitleSelector
from app.subtitles.ytdlp_client import YtDlpSubtitleDownloader, YtDlpSubtitleError, YtDlpSubtitleNoFileError
from app.worker.retry_policy import is_age_restricted_error, is_rate_limit_error, is_unavailable_error, random_video_sleep
logger = logging.getLogger(__name__)

class StableWorker:

    def __init__(self, *, config: RunConfig, paths: RunPaths, store: SQLiteStateStore) -> None:
        self.config = config
        self.paths = paths
        self.store = store
        self.metadata_provider = YtDlpSearchProvider()
        self.video_filter = VideoFilter()
        self.selector = SubtitleSelector()
        self.downloader = YtDlpSubtitleDownloader()
        self.cleaner = SubtitleCleaner()

    def run(self) -> list[CandidateDecision]:
        rows = self.store.load_queue_rows()
        total = len(rows)
        processed_success = sum((1 for _, decision, _ in rows if is_fully_processed(decision)))
        logger.info('Stable worker loaded %s queue rows from %s', total, self.paths.state_db)
        logger.info('Stable worker target clean transcripts: %s; already processed: %s', self.config.limit, processed_success)
        for row_position, (queue_index, decision, attempt_count) in enumerate(rows, start=1):
            if processed_success >= self.config.limit:
                if decision.status == VideoStatus.PROCESSED and (not is_fully_processed(decision)):
                    skipped = replace(decision, status=VideoStatus.SKIPPED, reason=SkipReason.LIMIT_REACHED, error_message='Target processed video limit reached before this queue item was processed.')
                    self.store.update_decision(queue_index, skipped, attempt_count=attempt_count)
                continue
            if decision.status != VideoStatus.PROCESSED:
                logger.info('Skipping queue item %s/%s video_id=%s status=%s reason=%s', row_position, total, decision.candidate.video_id, decision.status.value, decision.reason.value if decision.reason else '')
                continue
            if is_fully_processed(decision):
                logger.info('Already processed queue item %s/%s video_id=%s', row_position, total, decision.candidate.video_id)
                continue
            updated = self._process_with_retries(queue_index, decision, attempt_count)
            self.store.update_decision(queue_index, updated)
            if is_fully_processed(updated):
                processed_success += 1
            if row_position < total and processed_success < self.config.limit:
                sleep_seconds = random_video_sleep(self.config.stability.video_sleep_min_seconds, self.config.stability.video_sleep_max_seconds)
                if sleep_seconds > 0:
                    logger.info('Stable mode sleep between videos: %s seconds', sleep_seconds)
                    time.sleep(sleep_seconds)
        return self.store.load_decisions()

    def _process_with_retries(self, queue_index: int, decision: CandidateDecision, attempt_count: int) -> CandidateDecision:
        current = decision
        max_attempts = self.config.stability.max_attempts_per_video
        while attempt_count < max_attempts:
            started_at = datetime.now().astimezone()
            attempt_number = self.store.increment_attempt(queue_index)
            attempt_count = attempt_number
            logger.info('Processing video queue_index=%s video_id=%s attempt=%s/%s', queue_index, current.candidate.video_id, attempt_number, max_attempts)
            try:
                current = self._process_once(queue_index, current)
            except (YtDlpMetadataError, YtDlpSubtitleError, SubtitleCleaningError) as exc:
                finished_at = datetime.now().astimezone()
                message = str(exc)
                rate_limited = is_rate_limit_error(message)
                reason = SkipReason.RATE_LIMITED if rate_limited else self._reason_for_exception(exc)
                self.store.record_attempt(queue_index=queue_index, video_id=current.candidate.video_id, step='subtitle_pipeline', attempt_number=attempt_number, started_at=started_at, finished_at=finished_at, status='failed', error_message=message)
                self.store.update_decision(queue_index, mark_failed(current, reason, message), attempt_count=attempt_number, last_error=message)
                if rate_limited and attempt_number < max_attempts:
                    sleep_seconds = self.config.stability.rate_limit_backoff_seconds
                    logger.warning('Rate limit detected for video_id=%s. Sleeping %s seconds before retry.', current.candidate.video_id, sleep_seconds)
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    current = replace(current, status=VideoStatus.PROCESSED, reason=None, error_message=None)
                    self.store.update_decision(queue_index, current, attempt_count=attempt_number, last_error=message)
                    continue
                if not rate_limited and attempt_number < max_attempts:
                    sleep_seconds = min(60 * attempt_number, 180)
                    logger.warning('Video processing failed for video_id=%s. Sleeping %s seconds before retry.', current.candidate.video_id, sleep_seconds)
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    current = replace(current, status=VideoStatus.PROCESSED, reason=None, error_message=None)
                    self.store.update_decision(queue_index, current, attempt_count=attempt_number, last_error=message)
                    continue
                return mark_failed(current, reason, message)
            finished_at = datetime.now().astimezone()
            self.store.record_attempt(queue_index=queue_index, video_id=current.candidate.video_id, step='subtitle_pipeline', attempt_number=attempt_number, started_at=started_at, finished_at=finished_at, status='completed', error_message=None)
            self.store.update_decision(queue_index, current, attempt_count=attempt_number, last_error=None)
            return current
        return mark_failed(current, SkipReason.YT_DLP_ERROR, 'Maximum attempts exceeded')

    def _process_once(self, queue_index: int, decision: CandidateDecision) -> CandidateDecision:
        try:
            candidate, raw_metadata = self.metadata_provider.fetch_metadata(decision.candidate)
        except YtDlpMetadataError as exc:
            message = str(exc)
            if is_age_restricted_error(message):
                return replace(decision, status=VideoStatus.SKIPPED, reason=SkipReason.AGE_RESTRICTED, error_message=message)
            if is_unavailable_error(message):
                return replace(decision, status=VideoStatus.SKIPPED, reason=SkipReason.UNAVAILABLE, error_message=message)
            raise
        filtered = self.video_filter.filter_candidates([(candidate, raw_metadata)], self.config)[0]
        if filtered.status != VideoStatus.PROCESSED:
            return filtered
        selection = self.selector.select(raw_metadata, self.config)
        if selection is None:
            return replace(filtered, status=VideoStatus.SKIPPED, reason=SkipReason.NO_SUBTITLES, error_message='No suitable subtitles found for preferred languages and allowed subtitle types.')
        try:
            subtitle_info = self.downloader.download(index=queue_index, candidate=candidate, selection=selection, run_root=self.paths.root, subtitles_raw_dir=self.paths.subtitles_raw_dir)
        except YtDlpSubtitleNoFileError as exc:
            return replace(filtered, status=VideoStatus.SKIPPED, reason=SkipReason.NO_SUBTITLES, error_message=str(exc))
        raw_path = self.paths.root / subtitle_info.raw_subtitle_path
        clean_path = self._build_clean_transcript_path(subtitle_info.raw_subtitle_path)
        result = self.cleaner.clean_file(raw_path, clean_path)
        updated_subtitle = subtitle_info.model_copy(update={'clean_transcript_path': result.clean_transcript_path.relative_to(self.paths.root).as_posix()})
        return replace(filtered, status=VideoStatus.PROCESSED, reason=None, error_message=None, subtitle_info=updated_subtitle)

    def _build_clean_transcript_path(self, raw_subtitle_relative_path: str) -> Path:
        raw_name = Path(raw_subtitle_relative_path).name
        stem = Path(raw_name).stem
        return self.paths.transcripts_clean_dir / f'{stem}.txt'

    def _reason_for_exception(self, exc: Exception) -> SkipReason:
        if isinstance(exc, SubtitleCleaningError):
            return SkipReason.SUBTITLE_CLEANING_ERROR
        if isinstance(exc, YtDlpMetadataError):
            return SkipReason.METADATA_ERROR
        return SkipReason.YT_DLP_ERROR
