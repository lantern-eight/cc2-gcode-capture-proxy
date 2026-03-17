'''Tests for G-code metadata extraction.

Focuses on edge cases in filename resolution, filament CSV parsing,
and handling of truncated / malformed / empty input.
'''

from __future__ import annotations

import pytest

from src.gcode_parser import (
  _TAIL_SIZE,
  FilamentData,
  extract_filename,
  parse_filament_data,
  parse_gcode,
  parse_gcode_file,
)
from tests.conftest import make_gcode

# ===================================================================
# extract_filename
# ===================================================================


class TestExtractFilename:
  '''Filename resolution follows a three-level fallback chain.'''

  def test_template_substitution(self):
    data = make_gcode(
      input_filename_base='benchy',
      filename_format='{input_filename_base}.gcode',
    )
    assert extract_filename(data) == 'benchy.gcode'

  def test_template_with_prefix(self):
    data = make_gcode(
      input_filename_base='benchy',
      filename_format='CC2_{input_filename_base}.gcode',
    )
    assert extract_filename(data) == 'CC2_benchy.gcode'

  def test_no_double_gcode_suffix(self):
    data = make_gcode(
      input_filename_base='benchy',
      filename_format='{input_filename_base}.gcode',
    )
    name = extract_filename(data)
    assert name == 'benchy.gcode'
    assert not name.endswith('.gcode.gcode')

  def test_gcode_suffix_added_when_missing(self):
    data = make_gcode(
      input_filename_base='benchy',
      filename_format='{input_filename_base}',
    )
    assert extract_filename(data) == 'benchy.gcode'

  def test_fallback_to_input_filename_base_only(self):
    data = make_gcode(input_filename_base='benchy')
    assert extract_filename(data) == 'benchy.gcode'

  def test_fallback_to_output_filename_format(self):
    data = make_gcode(output_filename_format='my_print.gcode')
    assert extract_filename(data) == 'my_print.gcode'

  def test_no_filename_patterns_returns_none(self):
    data = make_gcode()
    assert extract_filename(data) is None

  def test_empty_input(self):
    assert extract_filename(b'') is None

  def test_data_shorter_than_tail_slice(self):
    data = make_gcode(input_filename_base='tiny')
    assert len(data) < _TAIL_SIZE
    assert extract_filename(data) == 'tiny.gcode'

  def test_whitespace_around_values_stripped(self):
    data = make_gcode(
      input_filename_base='  benchy  ',
      filename_format='  {input_filename_base}.gcode  ',
    )
    name = extract_filename(data)
    assert name is not None
    assert not name.startswith(' ')
    assert not name.endswith(' ')

  def test_non_utf8_bytes_do_not_crash(self):
    gcode = make_gcode(input_filename_base='benchy')
    corrupted = gcode + b'\xff\xfe\xfd' * 100
    assert extract_filename(corrupted) == 'benchy.gcode'

  def test_filename_format_literal_fallback(self):
    '''When filename_format has no template vars, use it as-is.'''
    data = make_gcode(filename_format='CC2_my_model.gcode')
    assert extract_filename(data) == 'CC2_my_model.gcode'

  def test_filename_format_literal_adds_gcode_suffix(self):
    data = make_gcode(filename_format='my_model')
    assert extract_filename(data) == 'my_model.gcode'

  def test_filename_format_with_unresolved_template_skipped(self):
    '''Template vars without input_filename_base fall through.'''
    data = make_gcode(filename_format='CC2_{input_filename_base}.gcode')
    assert extract_filename(data) is None

  def test_filename_metadata_outside_tail_window_not_found(self):
    '''If config block is beyond the tail window, it won't be found.'''
    padding = b'\x00' * (_TAIL_SIZE + 1024)
    data = make_gcode(input_filename_base='benchy') + padding
    assert extract_filename(data) is None


# ===================================================================
# parse_filament_data
# ===================================================================


class TestParseFilamentData:
  '''Filament usage is parsed from comment lines near the file tail.'''

  def test_full_four_slot_data(self):
    data = make_gcode(
      per_slot_mm='1000.0, 2000.0, 500.0, 0.0',
      per_slot_cm3='3.5, 7.0, 1.75, 0.0',
      per_slot_grams='4.2, 8.4, 2.1, 0.0',
      total_grams=14.7,
      total_cost=1.23,
      total_layers=150,
      estimated_time='1h 30m 15s',
    )
    filament_data = parse_filament_data(data)
    assert filament_data.per_slot_mm == [1000.0, 2000.0, 500.0, 0.0]
    assert filament_data.per_slot_grams == [4.2, 8.4, 2.1, 0.0]
    assert filament_data.total_grams == pytest.approx(14.7)
    assert filament_data.total_layers == 150
    assert filament_data.estimated_time == '1h 30m 15s'

  def test_single_slot(self):
    data = make_gcode(per_slot_grams='6.5', total_grams=6.5)
    filament_data = parse_filament_data(data)
    assert filament_data.per_slot_grams == [6.5]
    assert filament_data.total_grams == pytest.approx(6.5)

  def test_no_filament_lines_returns_defaults(self):
    data = make_gcode()
    filament_data = parse_filament_data(data)
    assert filament_data.per_slot_mm == []
    assert filament_data.per_slot_grams == []
    assert filament_data.total_grams == 0.0
    assert filament_data.total_layers == 0
    assert filament_data.estimated_time == ''

  def test_empty_input_returns_defaults(self):
    filament_data = parse_filament_data(b'')
    assert filament_data == FilamentData()

  def test_csv_with_extra_spaces(self):
    data = make_gcode(per_slot_grams='  1.0 ,  2.0 ,  3.0  ')
    filament_data = parse_filament_data(data)
    assert filament_data.per_slot_grams == [1.0, 2.0, 3.0]

  def test_total_layers_from_float_string(self):
    '''Slicer may emit layers as '42.0', must become int 42.'''
    data = make_gcode(total_layers=42)
    raw = data.replace(b'; total layers count = 42', b'; total layers count = 42.0')
    filament_data = parse_filament_data(raw)
    assert filament_data.total_layers == 42
    assert isinstance(filament_data.total_layers, int)

  def test_partial_data_fills_only_present_fields(self):
    data = make_gcode(total_grams=5.0, estimated_time='20m')
    filament_data = parse_filament_data(data)
    assert filament_data.total_grams == pytest.approx(5.0)
    assert filament_data.estimated_time == '20m'
    assert filament_data.per_slot_mm == []
    assert filament_data.per_slot_grams == []
    assert filament_data.total_cost == 0.0

  def test_filament_data_beyond_tail_window_not_found(self):
    '''Filament data beyond the tail window is outside the parse window.'''
    padding = b'\n' * (_TAIL_SIZE + 1024)
    data = make_gcode(total_grams=99.0) + padding
    filament_data = parse_filament_data(data)
    assert filament_data.total_grams == 0.0

  def test_malformed_float_returns_zero(self):
    '''Regex can match 1.2.3.4; float() raises ValueError; return 0.0.'''
    data = make_gcode(total_grams=5.0)
    raw = data.replace(
      b'; total filament used [g] = 5.0',
      b'; total filament used [g] = 1.2.3.4',
    )
    filament_data = parse_filament_data(raw)
    assert filament_data.total_grams == 0.0

  def test_per_slot_cost(self):
    data = make_gcode(per_slot_cost='0.41, 0.24, 0.00, 0.00')
    filament_data = parse_filament_data(data)
    assert filament_data.per_slot_cost == [0.41, 0.24, 0.0, 0.0]

  def test_per_slot_cost_filament_cost_format(self):
    '''ElegooSlicer writes ; filament_cost = ... in CONFIG_BLOCK (underscore).'''
    data = make_gcode(per_slot_cost='12.6,17.99,0,0')
    raw = data.replace(b'; filament cost = ', b'; filament_cost = ')
    filament_data = parse_filament_data(raw)
    assert filament_data.per_slot_cost == [12.6, 17.99, 0.0, 0.0]

  def test_total_filament_changes(self):
    data = make_gcode(total_filament_changes=46)
    filament_data = parse_filament_data(data)
    assert filament_data.total_filament_changes == 46

  def test_total_filament_changes_inferred_from_per_slot_usage(self):
    '''When slicer omits ; total filament change, infer from nonzero slots.'''
    # Two slots used -> at least 1 change (e.g. by-object multicolor)
    data = make_gcode(per_slot_grams='1.0, 2.0, 0.0, 0.0')
    filament_data = parse_filament_data(data)
    assert filament_data.total_filament_changes == 1

  def test_total_filament_changes_inferred_three_slots(self):
    data = make_gcode(per_slot_grams='1.0, 2.0, 0.5, 0.0')
    filament_data = parse_filament_data(data)
    assert filament_data.total_filament_changes == 2

  def test_total_filament_changes_inferred_single_slot_stays_zero(self):
    data = make_gcode(per_slot_grams='0.0, 0.0, 5.0, 0.0')
    filament_data = parse_filament_data(data)
    assert filament_data.total_filament_changes == 0

  def test_total_filament_changes_explicit_overrides_inference(self):
    '''When slicer outputs the value, use it (do not overwrite with inference).'''
    data = make_gcode(
      per_slot_grams='1.0, 2.0, 0.0, 0.0',
      total_filament_changes=46,
    )
    filament_data = parse_filament_data(data)
    assert filament_data.total_filament_changes == 46

  def test_filament_settings_id_simple(self):
    data = make_gcode(
      filament_settings_id='ElegooPLA-Basic-White;ElegooPLA-Basic-Black;ElegooPLA-Basic-Black;ElegooPLA-Metallic-Blue',
    )
    filament_data = parse_filament_data(data)
    assert filament_data.filament_names == [
      'ElegooPLA-Basic-White',
      'ElegooPLA-Basic-Black',
      'ElegooPLA-Basic-Black',
      'ElegooPLA-Metallic-Blue',
    ]

  def test_filament_settings_id_with_quoted_names(self):
    data = make_gcode(
      filament_settings_id='ElegooPLA-Basic-White;"ElegooPLA-Matte-Ruby Red";ElegooPLA-Basic-Black;ElegooPLA-Metallic-Blue',
    )
    filament_data = parse_filament_data(data)
    assert filament_data.filament_names == [
      'ElegooPLA-Basic-White',
      'ElegooPLA-Matte-Ruby Red',
      'ElegooPLA-Basic-Black',
      'ElegooPLA-Metallic-Blue',
    ]

  def test_no_new_fields_returns_defaults(self):
    data = make_gcode()
    filament_data = parse_filament_data(data)
    assert filament_data.per_slot_cost == []
    assert filament_data.filament_names == []
    assert filament_data.total_filament_changes == 0


# ===================================================================
# parse_gcode (full metadata)
# ===================================================================


class TestParseGcode:
  '''End-to-end metadata extraction combining header + tail parsing.'''

  def test_extracts_slicer_version_and_date(self):
    data = make_gcode(
      slicer_version='ElegooSlicer 1.3.2.9',
      generated_date='2026-03-06 at 19:16:22 UTC',
    )
    meta = parse_gcode(data)
    assert meta.slicer_version == 'ElegooSlicer 1.3.2.9'
    assert meta.generated_at == '2026-03-06 at 19:16:22 UTC'

  def test_missing_header_leaves_fields_none(self):
    data = make_gcode(slicer_version=None, generated_date=None)
    meta = parse_gcode(data)
    assert meta.slicer_version is None
    assert meta.generated_at is None

  def test_combines_header_and_tail_metadata(self):
    data = make_gcode(
      input_filename_base='widget',
      per_slot_grams='3.0, 1.5',
      total_grams=4.5,
      slicer_version='ElegooSlicer 1.3.2.9',
      generated_date='2026-03-06 at 19:16:22 UTC',
    )
    meta = parse_gcode(data)
    assert meta.filename == 'widget.gcode'
    assert meta.slicer_version is not None
    assert meta.filament.total_grams == pytest.approx(4.5)

  def test_data_smaller_than_header_window(self):
    tiny = b'; generated by Slicer 1.0 on 2026-01-01'
    meta = parse_gcode(tiny)
    assert meta.slicer_version == 'Slicer 1.0'

  def test_empty_data(self):
    meta = parse_gcode(b'')
    assert meta.filename is None
    assert meta.slicer_version is None
    assert meta.filament.total_grams == 0.0


# ===================================================================
# parse_gcode_file (file-based, O(1) memory)
# ===================================================================


class TestParseGcodeFile:
  '''File-based parser must produce identical results to the in-memory version.'''

  def test_matches_in_memory_parse(self, tmp_path):
    data = make_gcode(
      input_filename_base='widget',
      per_slot_grams='3.0, 1.5',
      total_grams=4.5,
      slicer_version='ElegooSlicer 1.3.2.9',
      generated_date='2026-03-06 at 19:16:22 UTC',
    )
    filepath = tmp_path / 'test.gcode'
    filepath.write_bytes(data)

    mem = parse_gcode(data)
    disk = parse_gcode_file(filepath)

    assert disk.filename == mem.filename
    assert disk.slicer_version == mem.slicer_version
    assert disk.generated_at == mem.generated_at
    assert disk.filament.total_grams == mem.filament.total_grams
    assert disk.filament.per_slot_grams == mem.filament.per_slot_grams

  def test_small_file_under_tail_window(self, tmp_path):
    '''Files smaller than the tail window should parse without error.'''
    data = make_gcode(input_filename_base='tiny', total_grams=1.0)
    assert len(data) < _TAIL_SIZE
    filepath = tmp_path / 'tiny.gcode'
    filepath.write_bytes(data)

    meta = parse_gcode_file(filepath)
    assert meta.filename == 'tiny.gcode'
    assert meta.filament.total_grams == pytest.approx(1.0)

  def test_large_file_only_reads_edges(self, tmp_path):
    '''Metadata at head/tail is found even with a large middle section.'''
    data = make_gcode(
      input_filename_base='big',
      total_grams=99.0,
      slicer_version='ElegooSlicer 2.0',
      generated_date='2026-01-01 at 00:00:00 UTC',
    )
    header, _, tail = data.partition(b'; EXECUTABLE_BLOCK_START')
    padding = b'G1 X0 Y0\n' * 100_000
    big_data = header + b'; EXECUTABLE_BLOCK_START' + padding + tail
    filepath = tmp_path / 'big.gcode'
    filepath.write_bytes(big_data)

    meta = parse_gcode_file(filepath)
    assert meta.slicer_version == 'ElegooSlicer 2.0'
    assert meta.filename == 'big.gcode'
    assert meta.filament.total_grams == pytest.approx(99.0)

  def test_no_metadata_returns_defaults(self, tmp_path):
    data = make_gcode(slicer_version=None, generated_date=None)
    filepath = tmp_path / 'bare.gcode'
    filepath.write_bytes(data)

    meta = parse_gcode_file(filepath)
    assert meta.filename is None
    assert meta.slicer_version is None
    assert meta.filament.total_grams == 0.0
