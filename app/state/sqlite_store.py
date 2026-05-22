from __future__ import annotations
import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from app.errors import VidScribeError
from app.models import RunConfig, SkipReason, SubtitleInfo, VideoCandidate, VideoStatus
from app.search.video_filter import CandidateDecision

class StateStoreError(VidScribeError):
    pass

class SQLiteStateStore:

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript('\n                    PRAGMA journal_mode=WAL;\n\n                    CREATE TABLE IF NOT EXISTS run_info (\n                        key TEXT PRIMARY KEY,\n                        value TEXT NOT NULL\n                    );\n\n                    CREATE TABLE IF NOT EXISTS videos (\n                        queue_index INTEGER PRIMARY KEY,\n                        video_id TEXT NOT NULL,\n                        candidate_json TEXT NOT NULL,\n                        raw_metadata_json TEXT NOT NULL,\n                        status TEXT NOT NULL,\n                        reason TEXT,\n                        subtitle_info_json TEXT,\n                        error_message TEXT,\n                        attempt_count INTEGER NOT NULL DEFAULT 0,\n                        last_error TEXT,\n                        created_at TEXT NOT NULL,\n                        updated_at TEXT NOT NULL\n                    );\n\n                    CREATE TABLE IF NOT EXISTS attempts (\n                        id INTEGER PRIMARY KEY AUTOINCREMENT,\n                        queue_index INTEGER NOT NULL,\n                        video_id TEXT NOT NULL,\n                        step TEXT NOT NULL,\n                        attempt_number INTEGER NOT NULL,\n                        started_at TEXT NOT NULL,\n                        finished_at TEXT,\n                        status TEXT NOT NULL,\n                        error_message TEXT\n                    );\n                    ')
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot initialize SQLite state at {self.db_path}: {exc}') from exc

    def save_run_info(self, *, run_id: str, created_at: datetime, config: RunConfig, config_path: str, cli_overrides: dict[str, Any], output_root: str) -> None:
        payload = {'run_id': run_id, 'created_at': created_at.isoformat(), 'config': config.model_dump(mode='json'), 'config_path': config_path, 'cli_overrides': cli_overrides, 'output_root': output_root}
        self._set_json('run_info', payload)

    def load_run_info(self) -> dict[str, Any]:
        return self._get_json('run_info')

    def load_config(self) -> RunConfig:
        info = self.load_run_info()
        return RunConfig.model_validate(info['config'])

    def _set_json(self, key: str, payload: dict[str, Any]) -> None:
        try:
            with self._connect() as connection:
                connection.execute('INSERT OR REPLACE INTO run_info(key, value) VALUES(?, ?)', (key, json.dumps(payload, ensure_ascii=False, default=str)))
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot write run_info[{key}] to {self.db_path}: {exc}') from exc

    def _get_json(self, key: str) -> dict[str, Any]:
        try:
            with self._connect() as connection:
                row = connection.execute('SELECT value FROM run_info WHERE key = ?', (key,)).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot read run_info[{key}] from {self.db_path}: {exc}') from exc
        if row is None:
            raise StateStoreError(f'Missing run_info[{key}] in {self.db_path}')
        return json.loads(row['value'])

    def replace_decisions(self, decisions: list[CandidateDecision]) -> None:
        now = datetime.now().astimezone().isoformat()
        try:
            with self._connect() as connection:
                connection.execute('DELETE FROM videos')
                connection.execute('DELETE FROM attempts')
                for index, decision in enumerate(decisions, start=1):
                    connection.execute('\n                        INSERT INTO videos(\n                            queue_index, video_id, candidate_json, raw_metadata_json,\n                            status, reason, subtitle_info_json, error_message,\n                            attempt_count, last_error, created_at, updated_at\n                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                        ', (index, decision.candidate.video_id, decision.candidate.model_dump_json(), json.dumps(decision.raw_metadata, ensure_ascii=False, default=str), decision.status.value, decision.reason.value if decision.reason else None, decision.subtitle_info.model_dump_json() if decision.subtitle_info else None, decision.error_message, 0, None, now, now))
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot replace decisions in {self.db_path}: {exc}') from exc

    def update_decision(self, queue_index: int, decision: CandidateDecision, *, attempt_count: int | None=None, last_error: str | None=None) -> None:
        now = datetime.now().astimezone().isoformat()
        try:
            with self._connect() as connection:
                current = connection.execute('SELECT attempt_count FROM videos WHERE queue_index = ?', (queue_index,)).fetchone()
                if current is None:
                    raise StateStoreError(f'Video queue_index={queue_index} does not exist in state store')
                new_attempt_count = attempt_count if attempt_count is not None else int(current['attempt_count'])
                connection.execute('\n                    UPDATE videos\n                    SET candidate_json = ?, raw_metadata_json = ?, status = ?, reason = ?,\n                        subtitle_info_json = ?, error_message = ?, attempt_count = ?,\n                        last_error = ?, updated_at = ?\n                    WHERE queue_index = ?\n                    ', (decision.candidate.model_dump_json(), json.dumps(decision.raw_metadata, ensure_ascii=False, default=str), decision.status.value, decision.reason.value if decision.reason else None, decision.subtitle_info.model_dump_json() if decision.subtitle_info else None, decision.error_message, new_attempt_count, last_error, now, queue_index))
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot update decision queue_index={queue_index}: {exc}') from exc

    def increment_attempt(self, queue_index: int, last_error: str | None=None) -> int:
        now = datetime.now().astimezone().isoformat()
        try:
            with self._connect() as connection:
                row = connection.execute('SELECT attempt_count FROM videos WHERE queue_index = ?', (queue_index,)).fetchone()
                if row is None:
                    raise StateStoreError(f'Video queue_index={queue_index} does not exist in state store')
                attempt_count = int(row['attempt_count']) + 1
                connection.execute('UPDATE videos SET attempt_count = ?, last_error = ?, updated_at = ? WHERE queue_index = ?', (attempt_count, last_error, now, queue_index))
                return attempt_count
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot increment attempt queue_index={queue_index}: {exc}') from exc

    def record_attempt(self, *, queue_index: int, video_id: str, step: str, attempt_number: int, started_at: datetime, finished_at: datetime | None, status: str, error_message: str | None=None) -> None:
        try:
            with self._connect() as connection:
                connection.execute('\n                    INSERT INTO attempts(\n                        queue_index, video_id, step, attempt_number,\n                        started_at, finished_at, status, error_message\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n                    ', (queue_index, video_id, step, attempt_number, started_at.isoformat(), finished_at.isoformat() if finished_at else None, status, error_message))
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot record attempt queue_index={queue_index}: {exc}') from exc

    def load_decisions(self) -> list[CandidateDecision]:
        try:
            with self._connect() as connection:
                rows = connection.execute('SELECT * FROM videos ORDER BY queue_index ASC').fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot load decisions from {self.db_path}: {exc}') from exc
        return [self._row_to_decision(row) for row in rows]

    def load_queue_rows(self) -> list[tuple[int, CandidateDecision, int]]:
        try:
            with self._connect() as connection:
                rows = connection.execute('SELECT * FROM videos ORDER BY queue_index ASC').fetchall()
        except sqlite3.Error as exc:
            raise StateStoreError(f'Cannot load queue rows from {self.db_path}: {exc}') from exc
        return [(int(row['queue_index']), self._row_to_decision(row), int(row['attempt_count'])) for row in rows]

    def _row_to_decision(self, row: sqlite3.Row) -> CandidateDecision:
        candidate = VideoCandidate.model_validate(json.loads(row['candidate_json']))
        raw_metadata = json.loads(row['raw_metadata_json'])
        subtitle_info = None
        if row['subtitle_info_json']:
            subtitle_info = SubtitleInfo.model_validate(json.loads(row['subtitle_info_json']))
        return CandidateDecision(candidate=candidate, raw_metadata=raw_metadata, status=VideoStatus(row['status']), reason=SkipReason(row['reason']) if row['reason'] else None, subtitle_info=subtitle_info, error_message=row['error_message'])

def is_fully_processed(decision: CandidateDecision) -> bool:
    return decision.status == VideoStatus.PROCESSED and decision.subtitle_info is not None and bool(decision.subtitle_info.clean_transcript_path)

def mark_failed(decision: CandidateDecision, reason: SkipReason, message: str) -> CandidateDecision:
    return replace(decision, status=VideoStatus.FAILED, reason=reason, error_message=message)
