'''Tests for G-code archival, naming, and retention cleanup.

Uses real filesystem via pytest tmp_path (no mocking needed).
Retention tests are the highest-risk area: a bug silently deletes data.
'''

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.storage import GCodeStorage, _sanitize
from tests.conftest import make_gcode

_UTC = ZoneInfo('UTC')

# ===================================================================
# _sanitize
# ===================================================================


class TestSanitize:
  def test_clean_name_unchanged(self):
    assert _sanitize('benchy.gcode') == 'benchy.gcode'

  def test_spaces_become_underscores(self):
    assert _sanitize('my print file.gcode') == 'my_print_file.gcode'

  def test_slashes_and_colons_replaced(self):
    assert '/' not in _sanitize('path/to:file')
    assert ':' not in _sanitize('path/to:file')

  def test_dots_and_hyphens_preserved(self):
    assert _sanitize('my-file.v2.gcode') == 'my-file.v2.gcode'

  def test_parentheses_replaced(self):
    result = _sanitize('benchy (1).gcode')
    assert '(' not in result
    assert ')' not in result


# ===================================================================
# save_gcode (store_gcode=True via `storage` fixture)
# ===================================================================


class TestSaveGcode:
  def test_json_lands_in_date_directory(self, storage):
    data = make_gcode(input_filename_base='benchy')
    json_path, _ = storage.save_gcode(data)

    today = datetime.now(_UTC).strftime('%Y-%m-%d')
    assert json_path.parent.name == today
    assert json_path.suffix == '.json'

  def test_filename_starts_with_timestamp(self, storage):
    data = make_gcode(input_filename_base='benchy')
    json_path, _ = storage.save_gcode(data)

    assert re.match(r'\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}_', json_path.name)

  def test_metadata_filename_appears_in_path(self, storage):
    data = make_gcode(input_filename_base='widget')
    json_path, _ = storage.save_gcode(data)
    assert 'widget' in json_path.name

  def test_fallback_name_when_no_metadata(self, storage):
    data = make_gcode()
    json_path, _ = storage.save_gcode(data)
    assert 'upload' in json_path.stem

  def test_special_chars_sanitized(self, storage):
    data = make_gcode(
      input_filename_base='my model (v2)',
      filename_format='{input_filename_base}.gcode',
    )
    json_path, _ = storage.save_gcode(data)
    assert '(' not in json_path.name
    assert ' ' not in json_path.name

  def test_no_double_gcode_extension_in_basename(self, storage):
    data = make_gcode(
      input_filename_base='benchy',
      filename_format='{input_filename_base}.gcode',
    )
    json_path, _ = storage.save_gcode(data)
    assert '.gcode.gcode' not in json_path.stem

  def test_gcode_file_kept_when_store_gcode_true(self, storage):
    data = make_gcode(input_filename_base='benchy', total_grams=5.0)
    json_path, _ = storage.save_gcode(data)
    gcode_path = json_path.with_suffix('.gcode')
    assert gcode_path.exists()
    assert gcode_path.read_bytes() == data

  def test_returns_parsed_metadata(self, storage):
    data = make_gcode(
      input_filename_base='benchy',
      total_grams=12.5,
      per_slot_grams='8.0, 4.5',
    )
    _, meta = storage.save_gcode(data)
    assert meta.filename is not None
    assert meta.filament.total_grams == pytest.approx(12.5)


# ===================================================================
# save_gcode_file (zero-copy move from temp, store_gcode=True)
# ===================================================================


class TestSaveGcodeFile:
  def test_json_lands_in_date_directory(self, storage):
    data = make_gcode(input_filename_base='benchy')
    src = storage.temp_path('upload-move')
    src.write_bytes(data)

    json_path, _ = storage.save_gcode_file(src)

    today = datetime.now(_UTC).strftime('%Y-%m-%d')
    assert json_path.parent.name == today
    assert json_path.suffix == '.json'

  def test_source_file_removed_after_move(self, storage):
    data = make_gcode(input_filename_base='moved')
    src = storage.temp_path('upload-rm')
    src.write_bytes(data)

    storage.save_gcode_file(src)
    assert not src.exists()

  def test_gcode_contents_match_original(self, storage):
    data = make_gcode(input_filename_base='integrity', total_grams=5.0)
    src = storage.temp_path('upload-integrity')
    src.write_bytes(data)

    json_path, _ = storage.save_gcode_file(src)
    gcode_path = json_path.with_suffix('.gcode')
    assert gcode_path.read_bytes() == data

  def test_returns_parsed_metadata(self, storage):
    data = make_gcode(
      input_filename_base='meta',
      total_grams=12.5,
      per_slot_grams='8.0, 4.5',
    )
    src = storage.temp_path('upload-meta')
    src.write_bytes(data)

    _, meta = storage.save_gcode_file(src)
    assert meta.filename is not None
    assert meta.filament.total_grams == pytest.approx(12.5)

  def test_fallback_name_when_no_metadata(self, storage):
    data = make_gcode()
    src = storage.temp_path('upload-noname')
    src.write_bytes(data)

    json_path, _ = storage.save_gcode_file(src)
    assert 'upload' in json_path.stem

  def test_filename_hint_used_when_parser_returns_none(self, storage):
    '''X-File-Name header fills in filename when parser can't determine it.'''
    data = make_gcode()
    src = storage.temp_path('upload-hint')
    src.write_bytes(data)

    json_path, meta = storage.save_gcode_file(src, filename_hint='CC2_MyModel.gcode')
    assert meta.filename == 'CC2_MyModel.gcode'
    assert 'MyModel' in json_path.name

  def test_filename_hint_ignored_when_parser_succeeds(self, storage):
    '''Parser-derived filename takes priority over the HTTP header hint.'''
    data = make_gcode(input_filename_base='from_parser')
    src = storage.temp_path('upload-override')
    src.write_bytes(data)

    _, meta = storage.save_gcode_file(src, filename_hint='from_header.gcode')
    assert meta.filename == 'from_parser.gcode'

  def test_filename_hint_adds_gcode_suffix(self, storage):
    data = make_gcode()
    src = storage.temp_path('upload-suffix')
    src.write_bytes(data)

    _, meta = storage.save_gcode_file(src, filename_hint='MyModel')
    assert meta.filename == 'MyModel.gcode'


# ===================================================================
# JSON sidecar content (store_gcode=True)
# ===================================================================


class TestJsonSidecar:
  def test_json_written_alongside_gcode(self, storage):
    data = make_gcode(input_filename_base='benchy', total_grams=2.76)
    json_path, _ = storage.save_gcode(data)
    gcode_path = json_path.with_suffix('.gcode')

    assert json_path.exists()
    assert gcode_path.exists()

  def test_json_content_matches_metadata(self, storage):
    data = make_gcode(
      input_filename_base='benchy',
      total_grams=2.76,
      per_slot_grams='0.0, 0.0, 0.0, 2.76',
      per_slot_mm='0.0, 0.0, 0.0, 924.31',
    )
    json_path, _ = storage.save_gcode(data)
    payload = json.loads(json_path.read_text())

    assert payload['filename'] == 'benchy.gcode'
    assert payload['filament']['total_grams'] == pytest.approx(2.76)
    assert payload['filament']['per_slot_grams'] == pytest.approx([0.0, 0.0, 0.0, 2.76])

  def test_json_has_captured_at_timestamp(self, storage):
    data = make_gcode(input_filename_base='benchy')
    json_path, _ = storage.save_gcode(data)
    payload = json.loads(json_path.read_text())

    assert 'captured_at' in payload
    datetime.fromisoformat(payload['captured_at'])

  def test_json_has_slicer_version(self, storage):
    data = make_gcode(input_filename_base='benchy')
    json_path, _ = storage.save_gcode(data)
    payload = json.loads(json_path.read_text())

    assert payload['slicer_version'] == 'ElegooSlicer 1.3.2.9'


# ===================================================================
# JSON-only mode (store_gcode=False, production default)
# ===================================================================


class TestJsonOnlyMode:
  def test_no_gcode_file_on_disk(self, storage_json_only):
    data = make_gcode(input_filename_base='benchy', total_grams=5.0)
    json_path, _ = storage_json_only.save_gcode(data)

    assert json_path.exists()
    assert json_path.suffix == '.json'
    gcode_files = list(json_path.parent.glob('*.gcode'))
    assert gcode_files == []

  def test_temp_file_cleaned_up(self, storage_json_only):
    data = make_gcode(input_filename_base='cleanup')
    storage_json_only.save_gcode(data)

    tmp_dir = storage_json_only.base_dir / '.tmp'
    if tmp_dir.exists():
      tmp_files = list(tmp_dir.iterdir())
      assert tmp_files == []

  def test_json_content_correct(self, storage_json_only):
    data = make_gcode(
      input_filename_base='benchy',
      total_grams=2.76,
      per_slot_grams='0.0, 0.0, 0.0, 2.76',
    )
    json_path, _ = storage_json_only.save_gcode(data)
    payload = json.loads(json_path.read_text())

    assert payload['filename'] == 'benchy.gcode'
    assert payload['filament']['total_grams'] == pytest.approx(2.76)

  def test_save_gcode_file_json_only(self, storage_json_only):
    data = make_gcode(input_filename_base='chunked')
    src = storage_json_only.temp_path('upload-chunked')
    src.write_bytes(data)

    json_path, _ = storage_json_only.save_gcode_file(src)
    assert json_path.exists()
    assert not src.exists()
    gcode_files = list(json_path.parent.glob('*.gcode'))
    assert gcode_files == []


# ===================================================================
# find_metadata / get_latest_metadata
# ===================================================================


class TestFindMetadata:
  def test_finds_by_original_filename(self, storage):
    data = make_gcode(input_filename_base='benchy')
    storage.save_gcode(data)

    result = storage.find_metadata('benchy.gcode')
    assert result is not None
    assert result['filename'] == 'benchy.gcode'

  def test_returns_none_for_unknown_filename(self, storage):
    data = make_gcode(input_filename_base='benchy')
    storage.save_gcode(data)

    assert storage.find_metadata('nonexistent.gcode') is None

  def test_returns_most_recent_when_duplicates(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90, store_gcode=True, tz=_UTC)
    data_v1 = make_gcode(input_filename_base='benchy', total_grams=2.0)
    storage.save_gcode(data_v1)

    data_v2 = make_gcode(input_filename_base='benchy', total_grams=5.0)
    storage.save_gcode(data_v2)

    result = storage.find_metadata('benchy.gcode')
    assert result is not None
    assert result['filament']['total_grams'] == pytest.approx(5.0)

  def test_returns_none_on_empty_archive(self, storage):
    assert storage.find_metadata('anything.gcode') is None


class TestGetLatestMetadata:
  def test_returns_most_recent(self, storage):
    data_a = make_gcode(input_filename_base='first', total_grams=1.0)
    storage.save_gcode(data_a)

    data_b = make_gcode(input_filename_base='second', total_grams=2.0)
    storage.save_gcode(data_b)

    result = storage.get_latest_metadata()
    assert result is not None
    assert result['filament']['total_grams'] == pytest.approx(2.0)

  def test_returns_none_on_empty_archive(self, storage):
    assert storage.get_latest_metadata() is None


# ===================================================================
# temp_path / cleanup_temp
# ===================================================================


class TestTempFiles:
  def test_tmp_dir_created(self, storage):
    path = storage.temp_path('upload-1')
    assert path.parent.name == '.tmp'
    assert path.parent.exists()

  def test_same_id_returns_same_path(self, storage):
    assert storage.temp_path('abc') == storage.temp_path('abc')

  def test_cleanup_nonexistent_temp_no_error(self, storage):
    storage.cleanup_temp('does-not-exist')

  def test_cleanup_orphaned_temp_files_removes_tmp_files(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    (storage.base_dir / '.tmp').mkdir(parents=True, exist_ok=True)
    orphan_a = storage.base_dir / '.tmp' / 'orphan_a.tmp'
    orphan_b = storage.base_dir / '.tmp' / 'orphan_b.tmp'
    orphan_a.write_bytes(b'x')
    orphan_b.write_bytes(b'y')

    removed = storage.cleanup_orphaned_temp_files()
    assert removed == 2
    assert not orphan_a.exists()
    assert not orphan_b.exists()

  def test_cleanup_orphaned_temp_files_no_tmp_dir_returns_zero(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=90)
    assert not (storage.base_dir / '.tmp').exists()
    removed = storage.cleanup_orphaned_temp_files()
    assert removed == 0


# ===================================================================
# cleanup_old_files (retention)
# ===================================================================


def _create_date_dir(base: Path, date_str: str, file_count: int = 1) -> None:
  date_dir = base / date_str
  date_dir.mkdir(parents=True, exist_ok=True)
  for i in range(file_count):
    (date_dir / f'print_{i}.gcode').write_text('G28')


class TestCleanupOldFiles:
  def test_retention_zero_disables_cleanup(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=0, tz=_UTC)
    _create_date_dir(tmp_path, '2020-01-01')

    removed = storage.cleanup_old_files()
    assert removed == 0
    assert (tmp_path / '2020-01-01').exists()

  def test_expired_directory_removed(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=30, tz=_UTC)
    old_date = (datetime.now(_UTC) - timedelta(days=60)).strftime('%Y-%m-%d')
    _create_date_dir(tmp_path, old_date)

    removed = storage.cleanup_old_files()
    assert removed == 1
    assert not (tmp_path / old_date).exists()

  def test_current_directory_kept(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=30, tz=_UTC)
    today = datetime.now(_UTC).strftime('%Y-%m-%d')
    _create_date_dir(tmp_path, today)

    removed = storage.cleanup_old_files()
    assert removed == 0
    assert (tmp_path / today).exists()

  def test_day_within_retention_kept(self, tmp_path):
    '''A directory within the retention window is never cleaned up.'''
    storage = GCodeStorage(str(tmp_path), retention_days=30, tz=_UTC)
    within = (datetime.now(_UTC) - timedelta(days=29)).strftime('%Y-%m-%d')
    _create_date_dir(tmp_path, within)

    removed = storage.cleanup_old_files()
    assert removed == 0
    assert (tmp_path / within).exists()

  def test_day_at_exact_boundary_kept(self, tmp_path):
    '''A directory exactly retention_days old is kept (date comparison, not datetime).'''
    storage = GCodeStorage(str(tmp_path), retention_days=30, tz=_UTC)
    boundary = (datetime.now(_UTC) - timedelta(days=30)).strftime('%Y-%m-%d')
    _create_date_dir(tmp_path, boundary)

    removed = storage.cleanup_old_files()
    assert removed == 0
    assert (tmp_path / boundary).exists()

  def test_day_beyond_retention_removed(self, tmp_path):
    '''A directory older than retention_days is always removed.'''
    storage = GCodeStorage(str(tmp_path), retention_days=30, tz=_UTC)
    beyond = (datetime.now(_UTC) - timedelta(days=31)).strftime('%Y-%m-%d')
    _create_date_dir(tmp_path, beyond)

    removed = storage.cleanup_old_files()
    assert removed == 1
    assert not (tmp_path / beyond).exists()

  def test_hidden_directories_skipped(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=1, tz=_UTC)
    (tmp_path / '.tmp').mkdir()
    (tmp_path / '.tmp' / 'upload.tmp').write_text('data')

    removed = storage.cleanup_old_files()
    assert removed == 0
    assert (tmp_path / '.tmp').exists()

  def test_non_date_directories_skipped(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=1, tz=_UTC)
    (tmp_path / 'not-a-date').mkdir()
    (tmp_path / 'not-a-date' / 'file.txt').write_text('x')

    removed = storage.cleanup_old_files()
    assert removed == 0
    assert (tmp_path / 'not-a-date').exists()

  def test_multiple_files_in_expired_dir(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=10, tz=_UTC)
    old_date = (datetime.now(_UTC) - timedelta(days=30)).strftime('%Y-%m-%d')
    _create_date_dir(tmp_path, old_date, file_count=5)

    removed = storage.cleanup_old_files()
    assert removed == 5
    assert not (tmp_path / old_date).exists()

  def test_nested_directories_in_expired_dir(self, tmp_path):
    '''Nested subdirs and files are removed; rmtree handles hierarchy.'''
    storage = GCodeStorage(str(tmp_path), retention_days=10, tz=_UTC)
    old_date = (datetime.now(_UTC) - timedelta(days=30)).strftime('%Y-%m-%d')
    date_dir = tmp_path / old_date
    date_dir.mkdir(parents=True)
    (date_dir / 'top.gcode').write_text('G28')
    sub = date_dir / 'subdir'
    sub.mkdir()
    (sub / 'nested.gcode').write_text('G28')
    (sub / 'deep').mkdir()
    (sub / 'deep' / 'deep.gcode').write_text('G28')

    removed = storage.cleanup_old_files()
    assert removed == 3
    assert not date_dir.exists()

  def test_mix_of_old_and_new(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=30, tz=_UTC)
    old_date = (datetime.now(_UTC) - timedelta(days=60)).strftime('%Y-%m-%d')
    new_date = datetime.now(_UTC).strftime('%Y-%m-%d')
    _create_date_dir(tmp_path, old_date, file_count=3)
    _create_date_dir(tmp_path, new_date, file_count=2)

    removed = storage.cleanup_old_files()
    assert removed == 3
    assert not (tmp_path / old_date).exists()
    assert (tmp_path / new_date).exists()

  def test_empty_archive_returns_zero(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=30, tz=_UTC)
    assert storage.cleanup_old_files() == 0

  def test_count_matches_actual_deletions(self, tmp_path):
    storage = GCodeStorage(str(tmp_path), retention_days=10, tz=_UTC)
    for days_ago in [20, 25, 30]:
      date_str = (datetime.now(_UTC) - timedelta(days=days_ago)).strftime('%Y-%m-%d')
      _create_date_dir(tmp_path, date_str, file_count=2)

    removed = storage.cleanup_old_files()
    assert removed == 6

  def test_base_dir_created_on_init(self, tmp_path):
    new_dir = tmp_path / 'deep' / 'nested' / 'gcode'
    assert not new_dir.exists()
    GCodeStorage(str(new_dir), retention_days=90, tz=_UTC)
    assert new_dir.exists()
