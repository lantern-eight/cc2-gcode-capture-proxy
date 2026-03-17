'''G-code file archival: save, name, and age-out captured files.

Directory layout:
  <gcode_dir>/
  ├── .tmp/                              ← in-flight chunked uploads
  ├── 2026-03-06/
  │   ├── 2026-03-06T19-16-22_CC2_benchy.json   ← always written
  │   ├── 2026-03-06T19-16-22_CC2_benchy.gcode  ← only if store_gcode=True
  │   └── …
  └── 2026-03-07/
    └── …
'''

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .gcode_parser import GCodeMetadata, parse_gcode_file

logger = logging.getLogger(__name__)


class GCodeStorage:
  def __init__(
    self,
    base_dir: str,
    retention_days: int = 90,
    *,
    store_gcode: bool = False,
    tz: ZoneInfo | None = None,
  ) -> None:
    self.base_dir = Path(base_dir)
    self.retention_days = retention_days
    self.store_gcode = store_gcode
    self._tz = tz or ZoneInfo('UTC')
    self.base_dir.mkdir(parents=True, exist_ok=True)

  # ------------------------------------------------------------------
  # Public helpers
  # ------------------------------------------------------------------

  def save_gcode(
    self,
    data: bytes,
    filename_hint: str | None = None,
  ) -> tuple[Path, GCodeMetadata]:
    '''Write raw bytes to a temp file, then delegate to *save_gcode_file()*.

    Both single-shot and chunked paths converge through
    *save_gcode_file()* so the JSON-sidecar and store_gcode logic
    lives in one place.
    '''
    temp_id = f'single_{uuid.uuid4().hex[:12]}'
    temp_path = self.temp_path(temp_id)
    temp_path.write_bytes(data)
    return self.save_gcode_file(temp_path, filename_hint=filename_hint)

  def save_gcode_file(
    self,
    src_path: Path,
    filename_hint: str | None = None,
  ) -> tuple[Path, GCodeMetadata]:
    '''Archive a completed temp file — O(1) memory.

    Parses metadata from head/tail (~68 KB), writes a JSON sidecar,
    and conditionally keeps the raw .gcode file based on *store_gcode*.
    *filename_hint* (typically the HTTP ``X-File-Name`` header) is used
    when the parser cannot determine the filename from G-code content.
    Returns *(json_path, metadata)*.
    '''
    now = datetime.now(self._tz)
    file_size = src_path.stat().st_size
    metadata = parse_gcode_file(src_path)

    if metadata.filename is None and filename_hint:
      name = filename_hint.strip()
      if not name.endswith('.gcode'):
        name += '.gcode'
      metadata.filename = name

    basename = self._archive_basename(metadata, now)
    date_dir = self._date_dir(now)

    json_path = date_dir / f'{basename}.json'
    self._write_json_sidecar(json_path, metadata, now)

    if self.store_gcode:
      gcode_path = date_dir / f'{basename}.gcode'
      shutil.move(src_path, gcode_path)
    else:
      src_path.unlink(missing_ok=True)

    logger.info(
      'Saved metadata: %s (%d bytes, %.2f g total filament)',
      json_path.name,
      file_size,
      metadata.filament.total_grams,
    )
    if metadata.filament.per_slot_grams:
      logger.info('  Per-slot grams: %s', metadata.filament.per_slot_grams)

    return json_path, metadata

  def temp_path(self, upload_id: str) -> Path:
    '''Return the path used for in-flight chunked uploads.'''
    temp_dir = self.base_dir / '.tmp'
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir / f'{upload_id}.tmp'

  def cleanup_temp(self, upload_id: str) -> None:
    path = self.temp_path(upload_id)
    path.unlink(missing_ok=True)

  def cleanup_orphaned_temp_files(self) -> int:
    '''Remove orphaned *.tmp files from .tmp/ (e.g. from crashed sessions).

    Call at startup to recover from leaks when __del__ did not run or an
    exception occurred between session creation and dict assignment.
    Returns the number of files removed.
    '''
    temp_dir = self.base_dir / '.tmp'
    if not temp_dir.is_dir():
      return 0
    removed = 0
    for path in temp_dir.glob('*.tmp'):
      try:
        path.unlink(missing_ok=True)
        removed += 1
      except OSError:
        pass
    return removed

  # ------------------------------------------------------------------
  # Metadata lookup
  # ------------------------------------------------------------------

  def find_metadata(self, filename: str) -> dict | None:
    '''Return the most recent JSON sidecar matching *filename*, or *None*.

    Walks date directories in reverse chronological order so the newest
    match wins.
    '''
    for date_dir in sorted(self._date_dirs(), reverse=True):
      for json_file in sorted(date_dir.glob('*.json'), reverse=True):
        try:
          data = json.loads(json_file.read_text())
        except (json.JSONDecodeError, OSError):
          continue
        if data.get('filename') == filename:
          return data
    return None

  def get_latest_metadata(self) -> dict | None:
    '''Return the most recently captured JSON sidecar, or *None*.'''
    for date_dir in sorted(self._date_dirs(), reverse=True):
      json_files = sorted(date_dir.glob('*.json'), reverse=True)
      if not json_files:
        continue
      try:
        return json.loads(json_files[0].read_text())
      except (json.JSONDecodeError, OSError):
        continue
    return None

  # ------------------------------------------------------------------
  # Retention
  # ------------------------------------------------------------------

  def cleanup_old_files(self) -> int:
    '''Delete date-directories older than *retention_days*.  Returns file count.'''
    if self.retention_days <= 0:
      return 0

    cutoff_date = datetime.now(self._tz).date() - timedelta(days=self.retention_days)
    removed = 0

    for entry in sorted(self.base_dir.iterdir()):
      if not entry.is_dir() or entry.name.startswith('.'):
        continue
      try:
        dir_date = date.fromisoformat(entry.name)
      except ValueError:
        continue
      if dir_date >= cutoff_date:
        continue
      removed += sum(1 for path in entry.rglob('*') if path.is_file())
      shutil.rmtree(entry)
      logger.info('Removed old archive: %s', entry.name)

    return removed

  async def periodic_cleanup(self, interval_hours: int = 24) -> None:
    '''Background loop that prunes expired archives.'''
    while True:
      await asyncio.sleep(interval_hours * 3600)
      try:
        removed_count = self.cleanup_old_files()
        if removed_count:
          logger.info('Periodic cleanup removed %d file(s)', removed_count)
      except Exception:
        logger.exception('Error during periodic cleanup')

  # ------------------------------------------------------------------
  # Internal
  # ------------------------------------------------------------------

  def _archive_basename(self, metadata: GCodeMetadata, now: datetime) -> str:
    '''Build the timestamp + name stem shared by .json and .gcode files.'''
    timestamp = now.strftime('%Y-%m-%dT%H-%M-%S')
    if metadata.filename:
      sanitized = _sanitize(metadata.filename)
      if sanitized.endswith('.gcode'):
        sanitized = sanitized[: -len('.gcode')]
      return f'{timestamp}_{sanitized}'
    return f'{timestamp}_upload'

  def _write_json_sidecar(
    self,
    json_path: Path,
    metadata: GCodeMetadata,
    now: datetime,
  ) -> None:
    payload = {
      'filename': metadata.filename,
      'slicer_version': metadata.slicer_version,
      'generated_at': metadata.generated_at,
      'captured_at': now.isoformat(),
      'filament': asdict(metadata.filament),
    }
    json_path.write_text(json.dumps(payload, indent=2) + '\n')

  def _date_dir(self, dt: datetime | None = None) -> Path:
    date_string = (dt or datetime.now(self._tz)).strftime('%Y-%m-%d')
    date_path = self.base_dir / date_string
    date_path.mkdir(parents=True, exist_ok=True)
    return date_path

  def _date_dirs(self) -> list[Path]:
    '''Return all valid date-directories under *base_dir*.'''
    dirs = []
    for entry in self.base_dir.iterdir():
      if not entry.is_dir() or entry.name.startswith('.'):
        continue
      try:
        date.fromisoformat(entry.name)
      except ValueError:
        continue
      dirs.append(entry)
    return dirs


def _sanitize(name: str) -> str:
  return re.sub(r'[^\w.\-]', '_', name)
