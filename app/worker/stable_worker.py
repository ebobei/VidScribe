from __future__ import annotations
import logging
import time
from dataclasses import replace
from datetime import datetime
from app.models import RunConfig, SkipReason, VideoStatus
from app.paths import RunPaths
from app.search.video_filter import CandidateDecision
from app.state.sqlite_store import SQLiteStateStore, is_fully_processed, mark_failed
from app.subtitles.subtitle_cleaner import SubtitleCleaner, SubtitleCleaningError
from app.subtitles.subtitle_selector import SubtitleSelector
from app.subtitles.ytdlp_client import YtDlpSubtitleDownloader, YtDlpSubtitleError, YtDlpSubtitleNoFileError
from app.worker.retry_policy import is_rate_limit_error, random_video_sleep
logger = logging.getLogger(__name__)

class StableWorker:

    def __init__(self, *, config: RunConfig, paths: RunPaths, store: SQLiteStateStore) -> None:
        self.config = config
        self.paths = paths
        self.store = store
        self.selector = SubtitleSelector()
        self.downloader = YtDlpSubtitleDownloader()
        self.cleaner = SubtitleCleaner()

    def run(self) -> list[CandidateDecision]:
        rows = self.store.load_queue_rows()
        total = len(rows)
        logger.info('Stable worker loaded %s queue rows from %s', total, self.paths.state_db)
        for row_position, (queue_index, decision, attempt_count) in enumerate(rows, start=1):
            if decision.status != VideoStatus.PROCESSED:
                logger.info('Skipping queue item %s/%s video_id=%s status=%s reason=%s', row_position, total, decision.candidate.video_id, decision.status.value, decision.reason.value if decision.reason else '')
                continue
            if is_fully_processed(decision):
                logger.info('Already processed queue item %s/%s video_id=%s', row_position, total, decision.candidate.video_id)
                continue
            updated = self._process_with_retries(queue_index, decision, attempt_count)
            self.store.update_decision(queue_index, updated)
            if row_position < total:
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
            except (YtDlpSubtitleError, SubtitleCleaningError) as exc:
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
        selection = self.selector.select(decision.raw_metadata, self.config)
        if selection is None:
            return replace(decision, status=VideoStatus.SKIPPED, reason=SkipReason.NO_SUBTITLES, error_message='No suitable subtitles found for preferred languages and allowed subtitle types.')
        try:
            subtitle_info = self.downloader.download(index=queue_index, candidate=decision.candidate, selection=selection, run_root=self.paths.root, subtitles_raw_dir=self.paths.subtitles_raw_dir)
        except YtDlpSubtitleNoFileError as exc:
            return replace(decision, status=VideoStatus.SKIPPED, reason=SkipReason.NO_SUBTITLES, error_message=str(exc))
        raw_path = self.paths.root / subtitle_info.raw_subtitle_path
        clean_path = self._build_clean_transcript_path(subtitle_info.raw_subtitle_path)
        result = self.cleaner.clean_file(raw_path, clean_path)
        updated_subtitle = subtitle_info.model_copy(update={'clean_transcript_path': result.clean_transcript_path.relative_to(self.paths.root).as_posix()})
        return replace(decision, status=VideoStatus.PROCESSED, reason=None, error_message=None, subtitle_info=updated_subtitle)

    def _build_clean_transcript_path(self, raw_subtitle_relative_path: str):
        from pathlib import Path
        raw_name = Path(raw_subtitle_relative_path).name
        stem = Path(raw_name).stem
        return self.paths.transcripts_clean_dir / f'{stem}.txt'

    def _reason_for_exception(self, exc: Exception) -> SkipReason:
        if isinstance(exc, SubtitleCleaningError):
            return SkipReason.SUBTITLE_CLEANING_ERROR
        return SkipReason.YT_DLP_ERROR
