"""Pydantic models used by VidScribe.

Stage 7 keeps the project local and narrow: CLI, YAML configs, yt-dlp based
YouTube metadata/subtitle collection, local transcript cleaning, local JSONL
chunking and stable overnight collection via SQLite state.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DurationMode(str, Enum):
    """Coarse duration mode kept in config for future provider-specific pre-filtering."""

    ANY = "any"
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


class YoutubeOrder(str, Enum):
    """Supported user-facing ordering values.

    Stage 2b+ maps relevance/date to yt-dlp search prefixes. Other values are
    accepted for config compatibility but treated as best-effort relevance.
    """

    RELEVANCE = "relevance"
    DATE = "date"
    VIEW_COUNT = "viewCount"
    RATING = "rating"
    TITLE = "title"


class VideoStatus(str, Enum):
    PROCESSED = "processed"
    SKIPPED = "skipped"
    FAILED = "failed"


class SkipReason(str, Enum):
    NO_SUBTITLES = "no_subtitles"
    DURATION_TOO_SHORT = "duration_too_short"
    DURATION_TOO_LONG = "duration_too_long"
    LIVE_VIDEO = "live_video"
    SHORTS = "shorts"
    DUPLICATE = "duplicate"
    YT_DLP_ERROR = "yt_dlp_error"
    SUBTITLE_CLEANING_ERROR = "subtitle_cleaning_error"
    RATE_LIMITED = "rate_limited"
    METADATA_ERROR = "metadata_error"


class SubtitleType(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class DurationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: DurationMode = DurationMode.MEDIUM
    min_seconds: int = Field(default=240, ge=0)
    max_seconds: int = Field(default=3600, gt=0)

    @model_validator(mode="after")
    def validate_duration_range(self) -> DurationConfig:
        if self.min_seconds >= self.max_seconds:
            raise ValueError("duration.min_seconds must be less than duration.max_seconds")
        return self


class LanguageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred: list[str] = Field(default_factory=lambda: ["ru", "en"], min_length=1)
    allow_auto_subtitles: bool = True
    allow_manual_subtitles: bool = True

    @field_validator("preferred")
    @classmethod
    def normalize_languages(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for lang in value:
            clean = lang.strip().lower()
            if not clean:
                continue
            normalized.append(clean)
        if not normalized:
            raise ValueError("languages.preferred must contain at least one non-empty language code")
        return normalized

    @model_validator(mode="after")
    def validate_subtitle_modes(self) -> LanguageConfig:
        if not self.allow_auto_subtitles and not self.allow_manual_subtitles:
            raise ValueError(
                "At least one subtitle mode must be enabled: "
                "languages.allow_auto_subtitles or languages.allow_manual_subtitles"
            )
        return self


class YoutubeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: YoutubeOrder = YoutubeOrder.RELEVANCE
    region_code: str | None = "RU"
    relevance_language: str | None = "ru"
    exclude_shorts: bool = True
    exclude_live: bool = True

    @field_validator("region_code")
    @classmethod
    def normalize_region_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip().upper()
        return clean or None

    @field_validator("relevance_language")
    @classmethod
    def normalize_relevance_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip().lower()
        return clean or None


class OutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory: str = "./output/default"
    build_zip: bool = True
    build_summary_md: bool = True
    build_chunks_jsonl: bool = True

    @field_validator("directory")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("output.directory must not be empty")
        return clean


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    download_video: Literal[False] = False
    download_audio: Literal[False] = False
    public_dataset: Literal[False] = False


class StabilityConfig(BaseModel):
    """Local overnight collection settings.

    This is intentionally not a distributed queue. SQLite keeps state on disk,
    and one slow worker processes videos one by one.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_attempts_per_video: int = Field(default=3, ge=1, le=10)
    video_sleep_min_seconds: int = Field(default=120, ge=0, le=3600)
    video_sleep_max_seconds: int = Field(default=180, ge=0, le=3600)
    rate_limit_backoff_seconds: int = Field(default=900, ge=0, le=3600)
    save_partial_results: bool = True
    build_pack_on_partial_success: bool = True

    @model_validator(mode="after")
    def validate_sleep_range(self) -> StabilityConfig:
        if self.video_sleep_min_seconds > self.video_sleep_max_seconds:
            raise ValueError("stability.video_sleep_min_seconds must be <= stability.video_sleep_max_seconds")
        return self


class RunConfig(BaseModel):
    """Validated run configuration loaded from YAML plus CLI overrides."""

    model_config = ConfigDict(extra="forbid")

    project_name: str = Field(min_length=1)
    query: str = Field(min_length=3)
    limit: int = Field(default=20, gt=0, le=500)
    candidate_pool_size: int = Field(default=60, gt=0, le=1000)
    duration: DurationConfig = Field(default_factory=DurationConfig)
    languages: LanguageConfig = Field(default_factory=LanguageConfig)
    youtube: YoutubeConfig = Field(default_factory=YoutubeConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    stability: StabilityConfig = Field(default_factory=StabilityConfig)

    @field_validator("project_name")
    @classmethod
    def normalize_project_name(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("project_name must not be empty")
        return clean

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        clean = " ".join(value.strip().split())
        if len(clean) < 3:
            raise ValueError("query must contain at least 3 characters")
        return clean

    @model_validator(mode="after")
    def validate_candidate_pool(self) -> RunConfig:
        if self.candidate_pool_size < self.limit:
            raise ValueError("candidate_pool_size must be greater than or equal to limit")
        return self


class VideoCandidate(BaseModel):
    """YouTube video candidate enriched with yt-dlp metadata."""

    model_config = ConfigDict(extra="forbid")

    video_id: str
    url: str
    title: str
    channel_id: str | None = None
    channel_title: str | None = None
    published_at: datetime | None = None
    description: str | None = None
    duration_seconds: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    language_hint: str | None = None
    search_query: str
    search_rank: int


class SubtitleInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    selected_language: str
    subtitle_type: SubtitleType
    subtitle_format: str
    raw_subtitle_path: str
    clean_transcript_path: str | None = None
    status: str


class TranscriptDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    source_type: Literal["youtube_video"] = "youtube_video"
    video_id: str
    title: str
    channel_title: str | None = None
    url: str
    language: str
    subtitle_type: SubtitleType
    text_path: str
    collected_at: datetime


class RagChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: str
    video_id: str
    title: str
    channel_title: str | None = None
    url: str
    language: str
    subtitle_type: SubtitleType
    chunk_index: int = Field(ge=0)
    start_time: str | None = None
    end_time: str | None = None
    text: str
    token_estimate: int = Field(ge=0)


class RunArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    path: str
    description: str


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    project_name: str
    status: Literal["initialized", "running", "partial", "completed", "failed"]
    stage: Literal["stage_1", "stage_2b", "stage_3", "stage_4", "stage_5", "stage_6", "stage_7"] = "stage_7"
    created_at: datetime
    config_path: str
    cli_overrides: dict[str, Any]
    output_root: str
    config: dict[str, Any]
    artifacts: list[RunArtifact]
    counters: dict[str, int] = Field(default_factory=dict)
