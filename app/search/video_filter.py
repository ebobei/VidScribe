from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from app.models import RunConfig, SkipReason, SubtitleInfo, VideoCandidate, VideoStatus

@dataclass(frozen=True)
class CandidateDecision:
    candidate: VideoCandidate
    raw_metadata: dict[str, Any]
    status: VideoStatus
    reason: SkipReason | None = None
    subtitle_info: SubtitleInfo | None = None
    error_message: str | None = None

class VideoFilter:

    def filter_candidates(self, candidates: list[tuple[VideoCandidate, dict[str, Any]]], config: RunConfig) -> list[CandidateDecision]:
        decisions: list[CandidateDecision] = []
        seen_video_ids: set[str] = set()
        accepted_count = 0
        for candidate, raw in candidates:
            reason = self._skip_reason(candidate, raw, config, seen_video_ids)
            if reason is not None:
                decisions.append(CandidateDecision(candidate=candidate, raw_metadata=raw, status=VideoStatus.SKIPPED, reason=reason))
                if candidate.video_id:
                    seen_video_ids.add(candidate.video_id)
                continue
            decisions.append(CandidateDecision(candidate=candidate, raw_metadata=raw, status=VideoStatus.PROCESSED, reason=None))
            seen_video_ids.add(candidate.video_id)
            accepted_count += 1
            if accepted_count >= config.limit:
                break
        return decisions

    def _skip_reason(self, candidate: VideoCandidate, raw: dict[str, Any], config: RunConfig, seen_video_ids: set[str]) -> SkipReason | None:
        if not candidate.video_id or not candidate.title or (not candidate.url):
            return SkipReason.METADATA_ERROR
        if candidate.video_id in seen_video_ids:
            return SkipReason.DUPLICATE
        if config.youtube.exclude_live and _is_live_or_upcoming(raw):
            return SkipReason.LIVE_VIDEO
        duration = candidate.duration_seconds
        if duration is None:
            return SkipReason.METADATA_ERROR
        if config.youtube.exclude_shorts and duration <= 60:
            return SkipReason.SHORTS
        if duration < config.duration.min_seconds:
            return SkipReason.DURATION_TOO_SHORT
        if duration > config.duration.max_seconds:
            return SkipReason.DURATION_TOO_LONG
        return None

def _is_live_or_upcoming(raw: dict[str, Any]) -> bool:
    if bool(raw.get('is_live')):
        return True
    live_status = str(raw.get('live_status') or '').strip().lower()
    return live_status in {'is_live', 'is_upcoming'}
