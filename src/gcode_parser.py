'''Extract metadata and filament-usage data from ElegooSlicer G-code files.

File layout:

  ; HEADER_BLOCK_START          ← slicer info, layer count, densities
  ; HEADER_BLOCK_END
  ; THUMBNAIL_BLOCK_START       ← base64 PNG thumbnail(s)
  ; THUMBNAIL_BLOCK_END
  <print parameters>
  ; EXECUTABLE_BLOCK_START      ← actual G-code moves
  ; EXECUTABLE_BLOCK_END
  ; filament used [mm] = ...    ← per-slot usage (4 slots for Canvas/AMS)
  ; filament used [g]  = ...
  ; total filament used [g] = …
  ; CONFIG_BLOCK_START          ← full slicer settings as key = value
  ; CONFIG_BLOCK_END

Tail read budget: 64 KB should cover CONFIG_BLOCK (~26 KB for multi-material
profiles) plus the filament summary lines that precede it.
'''

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class FilamentData:
  per_slot_mm: list[float] = field(default_factory=list)
  per_slot_cm3: list[float] = field(default_factory=list)
  per_slot_grams: list[float] = field(default_factory=list)
  per_slot_cost: list[float] = field(default_factory=list)
  filament_names: list[str] = field(default_factory=list)
  total_grams: float = 0.0
  total_cost: float = 0.0
  total_filament_changes: int = 0
  total_layers: int = 0
  estimated_time: str = ''


@dataclass
class GCodeMetadata:
  filename: str | None = None
  slicer_version: str | None = None
  generated_at: str | None = None
  filament: FilamentData = field(default_factory=FilamentData)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Size from the end of the file to read for metadata extraction
_TAIL_SIZE = 65_536

_RE_CSV = re.compile(r'=\s*(.+)')
_RE_SINGLE = re.compile(r'=\s*([\d.]+)')


def _parse_csv(line: str) -> list[float]:
  match = _RE_CSV.search(line)
  if not match:
    return []
  try:
    return [float(value.strip()) for value in match.group(1).split(',')]
  except ValueError:
    return []


def _parse_filament_names(line: str) -> list[str]:
  match = _RE_CSV.search(line)
  if not match:
    return []
  raw = match.group(1)
  return [name.strip().strip('"') for name in raw.split(';') if name.strip()]


def _parse_single(line: str) -> float:
  match = _RE_SINGLE.search(line)
  if not match:
    return 0.0
  try:
    return float(match.group(1))
  except ValueError:
    return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _extract_filename(tail_text: str) -> str | None:
  '''Core filename extraction from decoded tail text.

  Strategy (in order):
    1. CONFIG_BLOCK ``input_filename_base`` + ``filename_format`` template
    2. Bare ``input_filename_base`` (no format template)
    3. ``filename_format`` literal (when it contains no unresolved
       template variables — some slicer versions omit
       ``input_filename_base`` as a separate key)
    4. ``output_filename_format`` literal
  '''
  base_match = re.search(r';\s*input_filename_base\s*=\s*(.+)', tail_text)
  fmt_match = re.search(r';\s*filename_format\s*=\s*(.+)', tail_text)

  base = base_match.group(1).strip() if base_match else None
  fmt = fmt_match.group(1).strip() if fmt_match else None

  if base and fmt:
    name = fmt.replace('{input_filename_base}', base)
    if not name.endswith('.gcode'):
      name += '.gcode'
    return name

  if base:
    name = base.strip()
    if not name.endswith('.gcode'):
      name += '.gcode'
    return name

  if fmt and '{' not in fmt:
    if not fmt.endswith('.gcode'):
      fmt += '.gcode'
    return fmt

  out_match = re.search(r';\s*output_filename_format\s*=\s*(.+)', tail_text)
  if out_match:
    out_fmt = out_match.group(1).strip()
    if '{' not in out_fmt:
      if not out_fmt.endswith('.gcode'):
        out_fmt += '.gcode'
      return out_fmt

  return None


def _parse_filament_data(tail_text: str) -> FilamentData:
  '''Core filament parsing from decoded tail text.'''
  filament_data = FilamentData()

  for line in tail_text.splitlines():
    line = line.strip()
    if line.startswith('; filament used [mm]'):
      filament_data.per_slot_mm = _parse_csv(line)
    elif line.startswith('; filament used [cm3]'):
      filament_data.per_slot_cm3 = _parse_csv(line)
    elif line.startswith('; filament used [g]'):
      filament_data.per_slot_grams = _parse_csv(line)
    elif line.startswith('; total filament used [g]'):
      filament_data.total_grams = _parse_single(line)
    elif line.startswith('; filament cost') or line.startswith('; filament_cost'):
      filament_data.per_slot_cost = _parse_csv(line)
    elif line.startswith('; total filament cost'):
      filament_data.total_cost = _parse_single(line)
    elif line.startswith('; total filament change'):
      filament_data.total_filament_changes = int(_parse_single(line))
    elif line.startswith('; total layers count'):
      filament_data.total_layers = int(_parse_single(line))
    elif line.startswith('; estimated printing time'):
      match = re.search(r'=\s*(.+)', line)
      if match:
        filament_data.estimated_time = match.group(1).strip()
    elif line.startswith('; filament_settings_id'):
      filament_data.filament_names = _parse_filament_names(line)

  # Fallback: infer minimum filament changes from per-slot usage when slicer
  # omits '; total filament change' (e.g. by-object multicolor with one color
  # per object). Also intentionally overrides a slicer provided 0 if provided but
  # multiple colors are used, so that we can still show the correct number of changes.
  if filament_data.total_filament_changes == 0 and filament_data.per_slot_grams:
    nonzero_slots = sum(1 for g in filament_data.per_slot_grams if g > 0)
    filament_data.total_filament_changes = max(0, nonzero_slots - 1)

  return filament_data


def extract_filename(data: bytes) -> str | None:
  '''Best-effort filename extraction from G-code content.'''
  tail_text = data[-_TAIL_SIZE:].decode('utf-8', errors='ignore')
  return _extract_filename(tail_text)


def parse_filament_data(data: bytes) -> FilamentData:
  '''Parse per-slot and total filament usage near the end of the file.'''
  tail_text = data[-_TAIL_SIZE:].decode('utf-8', errors='ignore')
  return _parse_filament_data(tail_text)


def parse_gcode(data: bytes) -> GCodeMetadata:
  '''Return all extractable metadata from a raw G-code blob.'''
  meta = GCodeMetadata()
  meta.filename = extract_filename(data)
  meta.filament = parse_filament_data(data)

  header = data[:4096].decode('utf-8', errors='ignore')
  match = re.search(r'; generated by (.+?) on (.+)', header)
  if match:
    meta.slicer_version = match.group(1).strip()
    meta.generated_at = match.group(2).strip()

  return meta


def parse_gcode_file(filepath: Path) -> GCodeMetadata:
  '''Extract metadata reading only the file's head and tail — O(1) memory.

  Reads at most 4 KB from the start and 64 KB from the end, which is
  everything the parser actually inspects.  A 500 MB file uses ~68 KB.
  '''
  size = filepath.stat().st_size
  meta = GCodeMetadata()

  with open(filepath, 'rb') as f:
    head = f.read(min(4096, size))
    tail_size = min(_TAIL_SIZE, size)
    f.seek(size - tail_size)
    tail = f.read()

  header = head.decode('utf-8', errors='ignore')
  match = re.search(r'; generated by (.+?) on (.+)', header)
  if match:
    meta.slicer_version = match.group(1).strip()
    meta.generated_at = match.group(2).strip()

  tail_text = tail.decode('utf-8', errors='ignore')
  meta.filename = _extract_filename(tail_text)
  meta.filament = _parse_filament_data(tail_text)

  return meta
