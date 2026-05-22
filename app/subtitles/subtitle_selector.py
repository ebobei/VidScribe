from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from app.models import RunConfig, SubtitleType
PREFERRED_SUBTITLE_FORMATS = ('vtt', 'srt')

@dataclass(frozen=True)
class SubtitleSelection:
    requested_language: str
    selected_language: str
    subtitle_type: SubtitleType
    subtitle_format: str

class SubtitleSelector:

    def select(self, raw_metadata: dict[str, Any], config: RunConfig) -> SubtitleSelection | None:
        manual_subtitles = _normalize_subtitle_mapping(raw_metadata.get('subtitles'))
        automatic_captions = _normalize_subtitle_mapping(raw_metadata.get('automatic_captions'))
        for requested_language in config.languages.preferred:
            if config.languages.allow_manual_subtitles:
                selected = self._select_from_mapping(mapping=manual_subtitles, requested_language=requested_language, subtitle_type=SubtitleType.MANUAL)
                if selected is not None:
                    return selected
            if config.languages.allow_auto_subtitles:
                selected = self._select_from_mapping(mapping=automatic_captions, requested_language=requested_language, subtitle_type=SubtitleType.AUTO)
                if selected is not None:
                    return selected
        return None

    def _select_from_mapping(self, *, mapping: dict[str, list[dict[str, Any]]], requested_language: str, subtitle_type: SubtitleType) -> SubtitleSelection | None:
        selected_language = _find_language_key(mapping, requested_language)
        if selected_language is None:
            return None
        selected_format = _select_format(mapping[selected_language])
        if selected_format is None:
            return None
        return SubtitleSelection(requested_language=requested_language, selected_language=selected_language, subtitle_type=subtitle_type, subtitle_format=selected_format)

def _normalize_subtitle_mapping(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for language, entries in value.items():
        language_key = str(language).strip()
        if not language_key or not isinstance(entries, list):
            continue
        clean_entries = [entry for entry in entries if isinstance(entry, dict)]
        if clean_entries:
            result[language_key] = clean_entries
    return result

def _find_language_key(mapping: dict[str, list[dict[str, Any]]], requested_language: str) -> str | None:
    requested = requested_language.strip().lower()
    if not requested:
        return None
    lowered = {language.lower(): language for language in mapping.keys()}
    if requested in lowered:
        return lowered[requested]
    variants: list[str] = []
    for language in mapping.keys():
        language_lower = language.lower()
        if language_lower.startswith(f'{requested}-') or language_lower.startswith(f'{requested}_') or language_lower.startswith(f'{requested}.'):
            variants.append(language)
    if not variants:
        return None
    return sorted(variants, key=lambda item: (len(item), item.lower()))[0]

def _select_format(entries: list[dict[str, Any]]) -> str | None:
    available_formats: list[str] = []
    for entry in entries:
        ext = str(entry.get('ext') or '').strip().lower()
        if ext:
            available_formats.append(ext)
    for preferred_format in PREFERRED_SUBTITLE_FORMATS:
        if preferred_format in available_formats:
            return preferred_format
    return None
