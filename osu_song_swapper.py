#!/usr/bin/env python3
"""
osu! Audio Swapper GUI / Midblock Helper

Install:
    pip install PySide6

Run:
    python osu_audio_swapper_gui.py

Needs ffmpeg + ffprobe in PATH, bundled into the EXE, or ffmpeg.exe/ffprobe.exe beside the EXE.
Remembers your osu! Songs folder between launches using AppData settings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

CACHE_NAME = "osu_audio_swapper_cache_sync_toggle_v1.json"
DEFAULT_BPM_TOLERANCE = 0.03
APP_NAME = "osu-audio-swapper"
CONFIG_NAME = "config.json"


def app_dir() -> Path:
    """Folder where bundled tools/files live.

    For normal Python runs, this is the script folder.
    For a PyInstaller EXE, this is the EXE folder.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Find bundled UI resources such as favicon.ico."""
    if hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / name
        if p.exists():
            return p
    return app_dir() / name


def settings_dir() -> Path:
    """Stable settings folder that survives app relaunches and Windows reboots."""
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def config_path() -> Path:
    return settings_dir() / CONFIG_NAME


def load_app_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_app_config(data: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_songs_folder(path: str) -> None:
    path = path.strip().strip('"')
    if not path:
        return
    data = load_app_config()
    data["songs_folder"] = path
    save_app_config(data)


def default_songs_folder() -> str:
    cfg = load_app_config()
    saved = str(cfg.get("songs_folder", "")).strip()
    if saved:
        return saved

    guesses = []
    local = os.getenv("LOCALAPPDATA")
    if local:
        guesses.append(Path(local) / "osu!" / "Songs")
    guesses.append(Path.home() / "AppData" / "Local" / "osu!" / "Songs")

    for g in guesses:
        if g.exists():
            return str(g)
    return str(guesses[0]) if guesses else str(Path.home())


def bundled_tool(name: str) -> str:
    """Find ffmpeg/ffprobe from PATH or beside this script/EXE."""
    exe_name = name + (".exe" if sys.platform == "win32" else "")
    beside = app_dir() / exe_name
    if beside.exists():
        return str(beside)
    found = shutil.which(exe_name) or shutil.which(name)
    return found or name



@dataclass
class BeatmapInfo:
    title: str
    artist: str
    version: str
    creator: str
    bpm: Optional[float]
    first_note_ms: Optional[int]  # main-BPM sync note
    map_start_note_ms: Optional[int]  # first playable map object, ignoring spinners
    audio_length_seconds: Optional[float]
    osu_path: str
    folder_path: str
    audio_filename: str
    audio_path: str

    @property
    def display(self) -> str:
        bpm = "?" if self.bpm is None else f"{self.bpm:.3f}"
        first = "?" if self.first_note_ms is None else f"{self.first_note_ms / 1000:.3f}s"
        start = "?" if self.map_start_note_ms is None else f"{self.map_start_note_ms / 1000:.3f}s"
        return f"{self.artist} - {self.title} [{self.version}] | BPM {bpm} | BPM-note {first} | start {start}"


@dataclass
class RevertCandidate:
    title: str
    artist: str
    version: str
    osu_path: str
    folder_path: str
    audio_filename: str
    current_audio_path: str
    changed_audio_path: str

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title} [{self.version}] | restore {Path(self.changed_audio_path).name}"


def read_text(path: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            pass
    return path.read_text(errors="replace")


def section_lines(lines: list[str], section_name: str) -> list[str]:
    wanted = f"[{section_name}]"
    inside = False
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            inside = line == wanted
            continue
        if inside:
            out.append(raw.rstrip("\n"))
    return out


def key_values(section: list[str]) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in section:
        if not line.strip() or line.lstrip().startswith("//") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        data[k.strip()] = v.strip()
    return data


def get_hitobject_times(lines: list[str], ignore_spinners: bool = True) -> list[int]:
    """Return hit object start times used for BPM weighting.

    Spinners are ignored by default because they are often off-beat / not useful
    for finding the actual playable BPM of a map.
    """
    times: list[int] = []
    for line in section_lines(lines, "HitObjects"):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            obj_time = int(parts[2])
            obj_type = int(parts[3])
        except ValueError:
            continue

        if ignore_spinners and (obj_type & 8):
            continue
        if obj_type & 1 or obj_type & 2 or (not ignore_spinners and obj_type & 8):
            times.append(obj_time)
    return times


def parse_uninherited_timing_points(lines: list[str]) -> list[tuple[int, float]]:
    """Return red/uninherited timing points as (time_ms, bpm)."""
    timing_points: list[tuple[int, float]] = []
    for line in section_lines(lines, "TimingPoints"):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            time_ms = int(round(float(parts[0])))
            beat_len = float(parts[1])
            uninherited = True
            if len(parts) >= 7:
                uninherited = parts[6].strip() == "1"
            if beat_len > 0 and uninherited:
                timing_points.append((time_ms, 60000.0 / beat_len))
        except ValueError:
            continue
    timing_points.sort(key=lambda x: x[0])
    return timing_points


def active_bpm_at_time(timing_points: list[tuple[int, float]], obj_time: int) -> Optional[float]:
    """Return the active red-line BPM at a hit object time."""
    if not timing_points:
        return None
    from bisect import bisect_right
    starts = [t for t, _bpm in timing_points]
    idx = bisect_right(starts, obj_time) - 1
    if idx < 0:
        idx = 0
    return timing_points[idx][1]


def get_primary_bpm(lines: list[str]) -> Optional[float]:
    """Find the main gameplay BPM based on where hit objects actually are.

    This avoids picking tiny intro/slowdown BPM sections. The map's BPM is
    chosen by assigning every playable hit object to the active red timing point
    and selecting the BPM with the most objects. Ties prefer the higher BPM.
    """
    timing_points = parse_uninherited_timing_points(lines)
    if not timing_points:
        return None

    hit_times = get_hitobject_times(lines, ignore_spinners=True)
    if not hit_times:
        return round(timing_points[0][1], 6)

    from collections import defaultdict
    bpm_scores: dict[float, int] = defaultdict(int)
    for obj_time in hit_times:
        bpm = active_bpm_at_time(timing_points, obj_time)
        if bpm is None:
            continue
        bpm_scores[round(bpm, 3)] += 1

    if bpm_scores:
        best_bpm = max(bpm_scores.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return round(best_bpm, 6)

    return round(timing_points[0][1], 6)


def get_first_note_ms_for_bpm(lines: list[str], target_bpm: Optional[float]) -> Optional[int]:
    """Return the first playable object that belongs to the chosen main BPM.

    This is the sync point used by the audio swapper. It prevents maps with a
    short low-BPM intro from syncing against an intro note instead of the first
    real note of the main BPM section. Spinners are ignored.
    """
    if target_bpm is None:
        return get_first_note_ms(lines)

    timing_points = parse_uninherited_timing_points(lines)
    if not timing_points:
        return get_first_note_ms(lines)

    wanted = round(target_bpm, 3)
    best: Optional[int] = None
    for line in section_lines(lines, "HitObjects"):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            obj_time = int(parts[2])
            obj_type = int(parts[3])
        except ValueError:
            continue
        if obj_type & 8:
            continue
        if not (obj_type & 1 or obj_type & 2):
            continue

        active = active_bpm_at_time(timing_points, obj_time)
        if active is None:
            continue
        if round(active, 3) == wanted:
            best = obj_time
            break

    # Fallback keeps unusual maps usable instead of making them disappear.
    return best if best is not None else get_first_note_ms(lines)

def get_first_note_ms(lines: list[str]) -> Optional[int]:
    # Ignores spinners because they are often off-beat as the first object.
    # Keeps circles and sliders.
    for line in section_lines(lines, "HitObjects"):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            obj_time = int(parts[2])
            obj_type = int(parts[3])
        except ValueError:
            continue
        if obj_type & 8:  # spinner
            continue
        if obj_type & 1 or obj_type & 2:  # circle or slider
            return obj_time
    return None


def audio_length(path: Path) -> Optional[float]:
    try:
        r = subprocess.run(
            [bundled_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            text=True,
            capture_output=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def parse_osu(path: Path, include_audio_length: bool = False) -> Optional[BeatmapInfo]:
    try:
        text = read_text(path)
    except Exception:
        return None
    lines = text.splitlines()
    general = key_values(section_lines(lines, "General"))
    metadata = key_values(section_lines(lines, "Metadata"))
    audio_filename = general.get("AudioFilename", "").strip()
    if not audio_filename:
        return None
    audio_path = path.parent / audio_filename
    primary_bpm = get_primary_bpm(lines)
    map_start_note_ms = get_first_note_ms(lines)
    sync_note_ms = get_first_note_ms_for_bpm(lines, primary_bpm)
    return BeatmapInfo(
        title=metadata.get("Title") or metadata.get("TitleUnicode") or path.stem,
        artist=metadata.get("Artist") or metadata.get("ArtistUnicode") or "Unknown Artist",
        version=metadata.get("Version", "Unknown Difficulty"),
        creator=metadata.get("Creator", "Unknown Creator"),
        bpm=primary_bpm,
        first_note_ms=sync_note_ms,
        map_start_note_ms=map_start_note_ms,
        audio_length_seconds=audio_length(audio_path) if include_audio_length and audio_path.exists() else None,
        osu_path=str(path),
        folder_path=str(path.parent),
        audio_filename=audio_filename,
        audio_path=str(audio_path),
    )


def cache_path(songs_folder: Path) -> Path:
    return songs_folder / CACHE_NAME


def scan_songs(songs_folder: Path, progress_cb=None, include_audio_length: bool = False) -> list[BeatmapInfo]:
    """Fast scan the osu! Songs folder.

    The slow part of the old scanner was calling ffprobe once for every audio
    file. By default, this scanner skips audio length and parses .osu files in
    parallel. Swapping still works because it only needs BPM, first note time,
    audio filename, and paths.
    """
    osu_files = list(songs_folder.rglob("*.osu"))
    maps: list[BeatmapInfo] = []
    total = max(1, len(osu_files))
    done = 0
    workers = min(32, max(4, (os.cpu_count() or 4) * 2))

    def parse_one(osu_file: Path) -> Optional[BeatmapInfo]:
        info = parse_osu(osu_file, include_audio_length=include_audio_length)
        if info and info.bpm is not None and info.first_note_ms is not None and Path(info.audio_path).exists():
            return info
        return None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(parse_one, f) for f in osu_files]
        for fut in as_completed(futures):
            done += 1
            try:
                info = fut.result()
                if info:
                    maps.append(info)
            except Exception:
                pass

            if progress_cb and (done % 50 == 0 or done == total):
                progress_cb(int(done / total * 100), f"Fast scanning {done}/{total} maps...")

    # De-dupe exact .osu paths. This fixes duplicate entries in the GUI if
    # a cache or weird folder setup returns the same difficulty twice.
    deduped: dict[str, BeatmapInfo] = {}
    for m in maps:
        deduped[str(Path(m.osu_path).resolve()).lower()] = m
    maps = list(deduped.values())

    cache_path(songs_folder).write_text(
        json.dumps([asdict(m) for m in maps], separators=(",", ":")),
        encoding="utf-8",
    )
    return maps

def load_cache(songs_folder: Path) -> list[BeatmapInfo]:
    p = cache_path(songs_folder)
    if not p.exists():
        return []
    try:
        raw_items = json.loads(p.read_text(encoding="utf-8"))
        for x in raw_items:
            if "map_start_note_ms" not in x:
                x["map_start_note_ms"] = x.get("first_note_ms")
        loaded = [BeatmapInfo(**x) for x in raw_items]
        deduped: dict[str, BeatmapInfo] = {}
        for m in loaded:
            deduped[str(Path(m.osu_path).resolve()).lower()] = m
        return list(deduped.values())
    except Exception:
        return []


def unique_changed_name(folder: Path, original_name: str) -> Path:
    src = Path(original_name)
    base = src.stem
    suffix = src.suffix or ".mp3"
    candidate = folder / f"{base} (Changed){suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{base} (Changed {n}){suffix}"
        n += 1
    return candidate



def parse_changed_suffix(stem: str) -> Optional[tuple[str, int]]:
    """Return (original_stem, changed_number) for swapper backups.

    Supports:
      audio (Changed).mp3      -> number 1
      audio (Changed 2).mp3    -> number 2
      audio (Changed 3).mp3    -> number 3
    """
    lower = stem.lower()
    if lower.endswith(" (changed)"):
        return stem[:-10], 1
    import re
    m = re.match(r"^(.*) \(changed (\d+)\)$", stem, flags=re.IGNORECASE)
    if m:
        return m.group(1), int(m.group(2))
    return None


def remove_changed_suffix(stem: str) -> Optional[str]:
    parsed = parse_changed_suffix(stem)
    return parsed[0] if parsed else None


def unique_swapped_name(folder: Path, original_name: str) -> Path:
    src = Path(original_name)
    base = src.stem
    suffix = src.suffix
    candidate = folder / f"{base} (Swapped Out){suffix}"
    n = 2
    while candidate.exists():
        candidate = folder / f"{base} (Swapped Out {n}){suffix}"
        n += 1
    return candidate


def find_revert_candidates(songs_folder: Path) -> list[RevertCandidate]:
    audio_exts = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"}
    best_backup_for_original: dict[str, tuple[int, Path, str]] = {}

    for changed in songs_folder.rglob("*(Changed)*"):
        if not changed.is_file() or changed.suffix.lower() not in audio_exts:
            continue
        parsed = parse_changed_suffix(changed.stem)
        if not parsed:
            continue
        original_stem, changed_number = parsed
        original_name = original_stem + changed.suffix
        current_audio = changed.parent / original_name
        key = str(current_audio).lower()
        old = best_backup_for_original.get(key)
        if old is None or changed_number > old[0]:
            best_backup_for_original[key] = (changed_number, changed, original_name)

    candidates: list[RevertCandidate] = []
    for _key, (_num, changed, original_name) in best_backup_for_original.items():
        current_audio = changed.parent / original_name
        for osu_file in changed.parent.glob("*.osu"):
            try:
                lines = read_text(osu_file).splitlines()
            except Exception:
                continue
            general = key_values(section_lines(lines, "General"))
            metadata = key_values(section_lines(lines, "Metadata"))
            if general.get("AudioFilename", "").strip().lower() != original_name.lower():
                continue
            candidates.append(RevertCandidate(
                title=metadata.get("TitleUnicode") or metadata.get("Title", osu_file.stem),
                artist=metadata.get("ArtistUnicode") or metadata.get("Artist", "Unknown Artist"),
                version=metadata.get("Version", "Unknown Difficulty"),
                osu_path=str(osu_file),
                folder_path=str(changed.parent),
                audio_filename=original_name,
                current_audio_path=str(current_audio),
                changed_audio_path=str(changed),
            ))
            break

    return sorted(candidates, key=lambda c: (c.artist.lower(), c.title.lower(), c.version.lower()))


def revert_candidate(c: RevertCandidate, keep_swapped_copy: bool = True) -> Path:
    current_audio = Path(c.current_audio_path)
    changed_audio = Path(c.changed_audio_path)
    folder = Path(c.folder_path)

    if not changed_audio.exists():
        raise RuntimeError(f"Original changed/backup audio no longer exists:\n{changed_audio}")

    if current_audio.exists():
        if keep_swapped_copy:
            current_audio.rename(unique_swapped_name(folder, current_audio.name))
        else:
            current_audio.unlink()

    changed_audio.rename(current_audio)
    return current_audio

def ffmpeg_audio_codec_args(output_path: Path) -> list[str]:
    """Return safe ffmpeg codec args based on the target file extension.

    This matters when Beatmap A uses .ogg but Beatmap B uses .mp3, etc.
    The produced file must match Beatmap A's original filename/extension because
    Beatmap A's .osu AudioFilename is intentionally left unchanged.
    """
    ext = output_path.suffix.lower()
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    if ext == ".ogg":
        return ["-c:a", "libvorbis", "-q:a", "5"]
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext in (".m4a", ".aac"):
        return ["-c:a", "aac", "-b:a", "192k"]
    return []


def run_ffmpeg(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed.\n\n"
            f"Command:\n{' '.join(cmd)}\n\n"
            f"ffmpeg error:\n{result.stderr.strip() or result.stdout.strip()}"
        )


def atempo_chain(speed_factor: float) -> str:
    """Build a pitch-preserving ffmpeg atempo chain.

    atempo filters are safest between 0.5 and 2.0, so big changes are chained.
    """
    if speed_factor <= 0:
        speed_factor = 1.0
    parts: list[str] = []
    remaining = speed_factor
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.8f}")
    return ",".join(parts)


def speed_filter(speed_factor: float, preserve_pitch: bool) -> str:
    """Return an ffmpeg audio filter for speed changes.

    preserve_pitch=True keeps the pitch mostly unchanged.
    preserve_pitch=False changes pitch naturally with speed, like a record.
    """
    if abs(speed_factor - 1.0) < 0.0001:
        return ""
    if preserve_pitch:
        return atempo_chain(speed_factor)
    return f"asetrate=44100*{speed_factor:.8f},aresample=44100"


def make_aligned_audio_file(
    src_b: Path,
    output_path: Path,
    offset_ms: int,
    target_length_seconds: Optional[float] = None,
    speed_factor: float = 1.0,
    preserve_pitch: bool = True,
) -> None:
    """Create Beatmap B audio aligned to Beatmap A's first playable object.

    Output is always encoded to match Beatmap A's original file extension.
    This allows mp3 -> ogg, ogg -> mp3, etc. without changing the .osu file.

    If target_length_seconds is provided, the result is padded with silence
    and/or trimmed so it matches Beatmap A's original audio length.
    """
    codec_args = ffmpeg_audio_codec_args(output_path)

    filters: list[str] = []
    sf = speed_filter(speed_factor, preserve_pitch)
    if sf:
        filters.append(sf)

    if offset_ms > 0:
        filters.append(f"adelay={offset_ms}:all=1")
    elif offset_ms < 0:
        filters.append(f"atrim=start={abs(offset_ms) / 1000.0:.6f},asetpts=PTS-STARTPTS")

    if target_length_seconds and target_length_seconds > 0:
        # apad adds dead silence if the new audio is shorter than Beatmap A.
        # -t below also trims if it ends up longer.
        filters.append("apad")

    cmd = [bundled_tool("ffmpeg"), "-y", "-i", str(src_b)]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += codec_args
    if target_length_seconds and target_length_seconds > 0:
        cmd += ["-t", f"{target_length_seconds:.6f}"]
    cmd += [str(output_path)]
    run_ffmpeg(cmd)


def make_aligned_audio(
    map_a: BeatmapInfo,
    map_b: BeatmapInfo,
    speed_factor: float = 1.0,
    preserve_pitch: bool = True,
    map_a_sync_ms: Optional[int] = None,
    map_b_sync_ms: Optional[int] = None,
) -> Path:
    """
    Correct swap behaviour:
      1. Keep Beatmap A's .osu AudioFilename unchanged.
      2. Rename Beatmap A's original audio to '<name> (Changed).mp3'.
      3. Create Beatmap B's aligned audio using Beatmap A's original audio filename.
    """
    a_original_audio = Path(map_a.audio_path)
    a_folder = Path(map_a.folder_path)
    backup_path = unique_changed_name(a_folder, map_a.audio_filename)
    src_b = Path(map_b.audio_path)
    # If Beatmap B is being sped up/slowed down, its first note happens at
    # first_note / speed_factor in the produced audio.
    a_sync = map_a_sync_ms if map_a_sync_ms is not None else (map_a.first_note_ms or 0)
    b_sync = map_b_sync_ms if map_b_sync_ms is not None else (map_b.first_note_ms or 0)
    transformed_b_first = int(round(b_sync / max(speed_factor, 0.0001)))
    offset_ms = a_sync - transformed_b_first

    if not a_original_audio.exists():
        raise RuntimeError(f"Beatmap A audio file does not exist:\n{a_original_audio}")
    if not src_b.exists():
        raise RuntimeError(f"Beatmap B audio file does not exist:\n{src_b}")

    temp_output = a_folder / f".__osu_swapper_temp__{a_original_audio.suffix or '.mp3'}"
    if temp_output.exists():
        temp_output.unlink()

    try:
        if map_a.audio_length_seconds is None:
            map_a.audio_length_seconds = audio_length(a_original_audio)
        b_length_seconds = audio_length(src_b)

        # Keep Beatmap B from being cut off after alignment/speed changes.
        # If we add silence at the start, the output must be longer by that
        # delay so the song can still reach its end. If we trim the start to
        # line up the BPM-sync note, we cannot preserve the trimmed intro, but
        # we still keep the rest of Beatmap B through to its ending.
        target_length_seconds = map_a.audio_length_seconds
        if b_length_seconds and b_length_seconds > 0:
            transformed_b_length = b_length_seconds / max(speed_factor, 0.0001)
            if offset_ms >= 0:
                needed = transformed_b_length + (offset_ms / 1000.0)
            else:
                needed = max(0.0, transformed_b_length - (abs(offset_ms) / 1000.0))
            if target_length_seconds is None:
                target_length_seconds = needed
            else:
                target_length_seconds = max(target_length_seconds, needed)

        make_aligned_audio_file(
            src_b,
            temp_output,
            offset_ms,
            target_length_seconds=target_length_seconds,
            speed_factor=speed_factor,
            preserve_pitch=preserve_pitch,
        )

        # Move A's original audio out of the way, then put B's aligned audio
        # into the exact filename that Beatmap A's .osu already references.
        a_original_audio.rename(backup_path)

        try:
            temp_output.rename(a_original_audio)
        except Exception:
            if not a_original_audio.exists() and backup_path.exists():
                backup_path.rename(a_original_audio)
            raise

        return a_original_audio
    except PermissionError as e:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except Exception:
                pass
        raise PermissionError(
            "Windows is blocking the original Beatmap A audio file because osu! or another program is using it.\n\n"
            "Stop previewing/playing the song in osu!, switch to another map, or close osu!, then try again.\n\n"
            f"Locked file:\n{a_original_audio}\n\nOriginal error:\n{e}"
        ) from e
    except Exception:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except Exception:
                pass
        raise


class WorkerSignals(QObject):
    progress = Signal(int, str)
    done = Signal(object)
    error = Signal(str)


class MapCard(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("card")
        self.map: Optional[BeatmapInfo] = None
        self.use_bpm_sync = True
        layout = QVBoxLayout(self)
        self.label = QLabel(title)
        self.label.setObjectName("cardTitle")
        self.song = QLabel("No map selected")
        self.song.setWordWrap(True)
        self.song.setObjectName("songTitle")
        self.details = QLabel("BPM: -   Sync note: -   Length: -")
        self.details.setObjectName("details")
        layout.addWidget(self.label)
        layout.addWidget(self.song)
        layout.addWidget(self.details)

    def set_sync_mode(self, use_bpm_sync: bool):
        self.use_bpm_sync = use_bpm_sync
        self.set_map(self.map)

    def chosen_note_ms(self, m: BeatmapInfo) -> Optional[int]:
        if self.use_bpm_sync:
            return m.first_note_ms if m.first_note_ms is not None else m.map_start_note_ms
        return m.map_start_note_ms if m.map_start_note_ms is not None else m.first_note_ms

    def set_map(self, m: Optional[BeatmapInfo]):
        self.map = m
        if not m:
            self.song.setText("No map selected")
            self.details.setText("BPM: -   Sync note: -   Length: -")
            return
        if m.audio_length_seconds is None:
            m.audio_length_seconds = audio_length(Path(m.audio_path))
        length = "?" if m.audio_length_seconds is None else f"{m.audio_length_seconds:.1f}s"
        self.song.setText(f"{m.title}\n{m.artist} [{m.version}]")
        note_ms = self.chosen_note_ms(m)
        note_label = "BPM note" if self.use_bpm_sync else "Map start"
        note_text = "?" if note_ms is None else f"{note_ms/1000:.3f}s"
        self.details.setText(f"BPM: {m.bpm:.3f}   {note_label}: {note_text}   Length: {length}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SongSwapper")
        icon_file = resource_path("favicon.ico")
        if icon_file.exists():
            self.setWindowIcon(QIcon(str(icon_file)))
        self.resize(820, 560)
        self.setMinimumSize(520, 360)
        self.maps: list[BeatmapInfo] = []
        self.filtered_a: list[BeatmapInfo] = []
        self.filtered_b: list[BeatmapInfo] = []
        self.map_a: Optional[BeatmapInfo] = None
        self.map_b: Optional[BeatmapInfo] = None
        self.selected_b_category: str = ""
        self.selected_speed_factor: float = 1.0
        self.revert_candidates: list[RevertCandidate] = []
        self.setup_ui()

    def setup_ui(self):
        # Put the whole UI inside a scroll area so the window can shrink much
        # smaller than the full layout. When space is tight, scrollbars appear
        # instead of forcing a huge minimum window size.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.setCentralWidget(scroll)

        root = QWidget()
        scroll.setWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.folder_input = QLineEdit(default_songs_folder())
        self.folder_input.editingFinished.connect(lambda: save_songs_folder(self.folder_input.text()))
        browse = QPushButton("Browse")
        scan = QPushButton("Fast Scan / Rescan")
        load = QPushButton("Load Cache")
        browse.clicked.connect(self.browse_folder)
        scan.clicked.connect(self.scan_folder)
        load.clicked.connect(self.load_cache_clicked)
        top.addWidget(QLabel("Songs folder:"))
        top.addWidget(self.folder_input, 1)
        top.addWidget(browse)
        top.addWidget(load)
        top.addWidget(scan)
        layout.addLayout(top)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        swap_tab = QWidget()
        swap_layout = QVBoxLayout(swap_tab)
        swap_layout.setContentsMargins(6, 6, 6, 6)
        swap_layout.setSpacing(6)
        self.tabs.addTab(swap_tab, "Swap")

        cards = QHBoxLayout()
        cards.setSpacing(6)
        self.card_a = MapCard("Beatmap A - map to change")
        self.card_b = MapCard("Beatmap B - song to copy in")
        cards.addWidget(self.card_a)
        cards.addWidget(self.card_b)
        swap_layout.addLayout(cards)

        lists = QHBoxLayout()
        lists.setSpacing(6)
        left = QVBoxLayout()
        right = QVBoxLayout()

        self.search_a = QLineEdit()
        self.search_a.setPlaceholderText("Search Beatmap A...")
        self.search_b = QLineEdit()
        self.search_b.setPlaceholderText("Search Beatmap B matches...")
        self.list_a = QListWidget()
        self.list_a.setMinimumHeight(120)
        # Debounce search so typing does not rebuild huge lists on every keypress.
        # This removes the little stutter between letters on large osu! libraries.
        self.search_a_timer = QTimer(self)
        self.search_a_timer.setSingleShot(True)
        self.search_a_timer.setInterval(180)
        self.search_a_timer.timeout.connect(self.refresh_a_list)

        self.search_b_timer = QTimer(self)
        self.search_b_timer.setSingleShot(True)
        self.search_b_timer.setInterval(180)
        self.search_b_timer.timeout.connect(self.refresh_b_list)

        self.search_a.textChanged.connect(self.schedule_refresh_a_list)
        self.search_b.textChanged.connect(self.schedule_refresh_b_list)
        self.list_a.itemSelectionChanged.connect(self.select_a)

        # Beatmap B is split into tabs so each match type has its own page.
        self.b_categories = ["Same BPM", "Half BPM", "Double BPM", "Sped Up", "Slowed Down"]
        self.b_tabs = QTabWidget()
        self.b_lists: dict[str, QListWidget] = {}
        for category in self.b_categories:
            lst = QListWidget()
            lst.setMinimumHeight(120)
            lst.itemSelectionChanged.connect(self.select_b)
            self.b_lists[category] = lst
            self.b_tabs.addTab(lst, category)

        left.addWidget(QLabel("All maps"))
        left.addWidget(self.search_a)
        left.addWidget(self.list_a, 1)
        right.addWidget(QLabel("Beatmap B candidates by category"))
        right.addWidget(self.search_b)
        right.addWidget(self.b_tabs, 1)
        lists.addLayout(left)
        lists.addLayout(right)
        swap_layout.addLayout(lists, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.tolerance = QSpinBox()
        self.tolerance.setRange(0, 1000)
        self.tolerance.setValue(30)
        self.tolerance.setSuffix(" / 1000 BPM tolerance")
        self.tolerance.valueChanged.connect(self.schedule_refresh_b_list)

        self.allow_speed_change = QCheckBox("Allow speed up / slow down for any BPM")
        self.allow_speed_change.stateChanged.connect(self.schedule_refresh_b_list)

        self.preserve_pitch = QCheckBox("Pitch correction")
        self.preserve_pitch.setChecked(True)

        self.use_bpm_sync_note = QCheckBox("Sync to main-BPM note")
        self.use_bpm_sync_note.setChecked(True)
        self.use_bpm_sync_note.stateChanged.connect(self.sync_mode_changed)

        self.swap_btn = QPushButton("Create Changed Map Audio")
        self.swap_btn.clicked.connect(self.swap_clicked)
        controls.addWidget(QLabel("BPM match tolerance (100 = ±0.100 BPM):"))
        controls.addWidget(self.tolerance)
        controls.addWidget(self.allow_speed_change)
        controls.addWidget(self.preserve_pitch)
        controls.addWidget(self.use_bpm_sync_note)
        controls.addStretch(1)
        controls.addWidget(self.swap_btn)
        swap_layout.addLayout(controls)

        self.progress = QProgressBar()
        self.status = QLabel("Load cache or fast scan your osu! Songs folder.")
        swap_layout.addWidget(self.progress)
        swap_layout.addWidget(self.status)

        revert_tab = QWidget()
        revert_layout = QVBoxLayout(revert_tab)
        self.tabs.addTab(revert_tab, "Revert")

        revert_top = QHBoxLayout()
        self.scan_revert_btn = QPushButton("Find Changed Audio")
        self.scan_revert_btn.clicked.connect(self.scan_reverts_clicked)
        self.revert_btn = QPushButton("Revert Selected")
        self.revert_btn.clicked.connect(self.revert_selected_clicked)
        revert_top.addWidget(QLabel("Find maps that have an original audio backup named (Changed)."))
        revert_top.addStretch(1)
        revert_top.addWidget(self.scan_revert_btn)
        revert_top.addWidget(self.revert_btn)
        revert_layout.addLayout(revert_top)

        self.revert_list = QListWidget()
        self.revert_list.itemSelectionChanged.connect(self.select_revert)
        revert_layout.addWidget(self.revert_list, 1)

        self.revert_details = QLabel("Press Find Changed Audio to scan for revertable swaps.")
        self.revert_details.setWordWrap(True)
        self.revert_details.setObjectName("details")
        revert_layout.addWidget(self.revert_details)


        self.setStyleSheet("""
            QWidget { background: #1f1830; color: #f4edff; font-size: 12px; }
            QLineEdit, QListWidget, QSpinBox { background: #2b2140; border: 1px solid #4f3a77; border-radius: 7px; padding: 5px; }
            QPushButton { background: #c93d91; color: white; border: 0; border-radius: 8px; padding: 8px 10px; font-weight: bold; }
            QPushButton:hover { background: #e04da6; }
            QPushButton:disabled { background: #5b4b68; color: #aaa; }
            QListWidget::item { padding: 5px; border-bottom: 1px solid #392c52; }
            QListWidget::item:selected { background: #7041c9; border-radius: 6px; }
            QFrame#card { background: #332544; border-radius: 16px; padding: 8px; border: 1px solid #5f4380; }
            QLabel#cardTitle { color: #ff66b6; font-weight: bold; }
            QLabel#songTitle { font-size: 18px; font-weight: bold; }
            QLabel#details { color: #c7b6ef; }
            QProgressBar { border: 1px solid #4f3a77; border-radius: 8px; text-align: center; background: #2b2140; }
            QProgressBar::chunk { background: #7d50d7; border-radius: 8px; }
            QTabWidget::pane { border: 1px solid #4f3a77; border-radius: 8px; }
            QTabBar::tab { background: #2b2140; color: #f4edff; padding: 6px 9px; border: 1px solid #4f3a77; border-bottom: 0; border-top-left-radius: 7px; border-top-right-radius: 7px; }
            QTabBar::tab:selected { background: #7041c9; font-weight: bold; }
        """)

    def folder(self) -> Path:
        return Path(self.folder_input.text().strip().strip('"'))

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select osu! Songs folder", self.folder_input.text())
        if folder:
            self.folder_input.setText(folder)
            save_songs_folder(folder)

    def set_maps(self, maps: list[BeatmapInfo]):
        # Keep the full cache internally, but remove visible duplicates later using
        # the same text-based key the GUI shows. This catches copied .osu files
        # and stale cache duplicates that have different paths but identical rows.
        path_deduped: dict[str, BeatmapInfo] = {}
        for m in maps:
            path_deduped[str(Path(m.osu_path).resolve()).lower()] = m
        self.maps = sorted(path_deduped.values(), key=lambda m: (m.artist.lower(), m.title.lower(), m.version.lower()))
        visible_count = len(self.visible_unique_maps(self.maps))
        removed = len(self.maps) - visible_count
        extra = f" ({removed} visible duplicate(s) hidden)" if removed else ""
        self.status.setText(f"Loaded {visible_count} maps{extra}.")
        self.refresh_a_list()
        self.refresh_b_list()

    def load_cache_clicked(self):
        folder = self.folder()
        if folder.exists():
            save_songs_folder(str(folder))
        maps = load_cache(folder)
        if not maps:
            QMessageBox.warning(self, "No cache", "No cache found. Press Fast Scan / Rescan first.")
            return
        self.swap_btn.setEnabled(True)
        self.set_maps(maps)

    def scan_folder(self):
        folder = self.folder()
        if not folder.exists():
            QMessageBox.warning(self, "Missing folder", "That Songs folder does not exist.")
            return
        save_songs_folder(str(folder))
        self.progress.setValue(0)
        self.status.setText("Fast scanning... lengths load when you select a map.")
        self.swap_btn.setDisabled(True)
        self.scan_signals = WorkerSignals()
        signals = self.scan_signals
        signals.progress.connect(lambda p, s: (self.progress.setValue(p), self.status.setText(s)))

        def scan_done(maps):
            self.swap_btn.setEnabled(True)
            self.set_maps(maps)
            self.progress.setValue(100)

        def scan_error(e):
            self.swap_btn.setEnabled(True)
            QMessageBox.critical(self, "Scan error", e)

        signals.done.connect(scan_done)
        signals.error.connect(scan_error)

        def job():
            try:
                maps = scan_songs(folder, lambda p, s: signals.progress.emit(p, s), include_audio_length=False)
                signals.done.emit(maps)
            except Exception as e:
                signals.error.emit(str(e))

        threading.Thread(target=job, daemon=True).start()


    def schedule_refresh_a_list(self):
        self.search_a_timer.start()

    def schedule_refresh_b_list(self):
        self.search_b_timer.start()

    def use_bpm_sync(self) -> bool:
        return not hasattr(self, "use_bpm_sync_note") or self.use_bpm_sync_note.isChecked()

    def sync_note_ms(self, m: BeatmapInfo) -> int:
        if self.use_bpm_sync():
            value = m.first_note_ms if m.first_note_ms is not None else m.map_start_note_ms
        else:
            value = m.map_start_note_ms if m.map_start_note_ms is not None else m.first_note_ms
        return int(value or 0)

    def map_display(self, m: BeatmapInfo) -> str:
        bpm = "?" if m.bpm is None else f"{m.bpm:.3f}"
        note_ms = self.sync_note_ms(m)
        label = "BPM-note" if self.use_bpm_sync() else "start"
        return f"{m.artist} - {m.title} [{m.version}] | BPM {bpm} | {label} {note_ms / 1000:.3f}s"

    def sync_mode_changed(self):
        mode = self.use_bpm_sync()
        self.card_a.set_sync_mode(mode)
        self.card_b.set_sync_mode(mode)
        self.refresh_a_list()
        self.refresh_b_list()
        if self.map_a and self.map_b:
            self.update_alignment_status()

    def update_alignment_status(self):
        if not self.map_a or not self.map_b:
            return
        transformed_b_first = int(round(self.sync_note_ms(self.map_b) / max(self.selected_speed_factor, 0.0001)))
        delta = self.sync_note_ms(self.map_a) - transformed_b_first
        action = "add silence" if delta > 0 else "trim start" if delta < 0 else "no offset needed"
        speed = "" if abs(self.selected_speed_factor - 1.0) < 0.0001 else f" | speed x{self.selected_speed_factor:.4f}"
        mode = "main-BPM note" if self.use_bpm_sync() else "map start note"
        self.status.setText(f"{self.selected_b_category}: alignment delta {delta} ms — {action}. Sync mode: {mode}.{speed}")

    def make_item(self, m: BeatmapInfo) -> QListWidgetItem:
        item = QListWidgetItem(self.map_display(m))
        item.setData(Qt.UserRole, m)
        return item

    def text_match(self, m: BeatmapInfo, q: str) -> bool:
        if not q:
            return True
        hay = f"{m.artist} {m.title} {m.version} {m.creator} {m.bpm}".lower()
        return all(part in hay for part in q.lower().split())

    def display_duplicate_key(self, m: BeatmapInfo) -> tuple:
        """Key used as a final GUI-side duplicate filter.

        This intentionally matches what the user actually sees in the list.
        If two entries display as the exact same song/difficulty/BPM/first-note,
        only one should appear, even if they came from two copied .osu files or
        stale cache records with different hidden file paths.
        """
        def clean(value) -> str:
            return str(value or "").strip().casefold()

        bpm_key = "?" if m.bpm is None else f"{float(m.bpm):.3f}"
        note_key = f"{self.sync_note_ms(m) / 1000:.3f}s"

        # Same fields as the visible row, normalized.
        return (
            clean(m.artist),
            clean(m.title),
            clean(m.version),
            bpm_key,
            note_key,
        )

    def visible_unique_maps(self, maps: list[BeatmapInfo]) -> list[BeatmapInfo]:
        seen: set[tuple] = set()
        unique: list[BeatmapInfo] = []
        for m in maps:
            key = self.display_duplicate_key(m)
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        return unique

    def bpm_match_label(self, candidate_bpm: float, target_bpm: float, tol: float) -> Optional[str]:
        """Return exact musical BPM category without time-stretching."""
        checks = [
            (target_bpm, "Same BPM"),
            (target_bpm / 2.0, "Half BPM"),
            (target_bpm * 2.0, "Double BPM"),
        ]
        best_label = None
        best_diff = None
        for expected, label in checks:
            diff = abs(candidate_bpm - expected)
            if diff <= tol and (best_diff is None or diff < best_diff):
                best_label = label
                best_diff = diff
        return best_label

    def match_category_and_speed(self, candidate_bpm: float, target_bpm: float, tol: float) -> Optional[tuple[str, float]]:
        """Classify Beatmap B and return (category, speed_factor).

        Same/Half/Double do not stretch audio. If the speed-change toggle is on,
        every other BPM becomes either Sped Up or Slowed Down and gets stretched
        exactly to Beatmap A's BPM.
        """
        exact = self.bpm_match_label(candidate_bpm, target_bpm, tol)
        if exact:
            return exact, 1.0

        if not getattr(self, "allow_speed_change", None) or not self.allow_speed_change.isChecked():
            return None

        if candidate_bpm <= 0 or target_bpm <= 0:
            return None

        speed_factor = target_bpm / candidate_bpm
        if abs(speed_factor - 1.0) < 0.0001:
            return "Same BPM", 1.0
        if speed_factor > 1.0:
            return "Sped Up", speed_factor
        return "Slowed Down", speed_factor

    def make_b_item(self, m: BeatmapInfo, match_label: str, speed_factor: float) -> QListWidgetItem:
        speed_note = ""
        if abs(speed_factor - 1.0) >= 0.0001:
            speed_note = f" | speed x{speed_factor:.4f}"
        item = QListWidgetItem(f"{self.map_display(m)}{speed_note}")
        item.setData(Qt.UserRole, m)
        item.setData(Qt.UserRole + 1, match_label)
        item.setData(Qt.UserRole + 2, speed_factor)
        return item

    def refresh_a_list(self):
        q = self.search_a.text().strip()

        # Freeze repainting while we clear/rebuild the list. Without this, Qt may
        # repaint thousands of rows repeatedly while the user is still typing.
        self.list_a.setUpdatesEnabled(False)
        self.list_a.blockSignals(True)
        try:
            self.list_a.clear()

            # Final safety pass: remove duplicated difficulties right before they are
            # shown in the GUI. This fixes caches/folder scans that surface the same
            # diff twice.
            self.filtered_a = self.visible_unique_maps([m for m in self.maps if self.text_match(m, q)])

            # addItems is much faster than addItem in a huge loop, but we still need
            # to attach BeatmapInfo objects, so build items first then add them.
            for m in self.filtered_a:
                self.list_a.addItem(self.make_item(m))
        finally:
            self.list_a.blockSignals(False)
            self.list_a.setUpdatesEnabled(True)

    def refresh_b_list(self):
        # Freeze every Beatmap B list while rebuilding. This prevents visible
        # repaint churn when there are thousands of candidate rows.
        for lst in self.b_lists.values():
            lst.setUpdatesEnabled(False)
            lst.blockSignals(True)
            lst.clear()

        try:
            if not self.map_a or self.map_a.bpm is None:
                self.status.setText("Select Beatmap A to show Beatmap B candidates.")
                return
            q = self.search_b.text().strip()
            tol = self.tolerance.value() / 1000.0

            categories: dict[str, list[tuple[BeatmapInfo, float]]] = {label: [] for label in self.b_categories}

            seen_b: set[tuple] = set()
            map_a_key = self.display_duplicate_key(self.map_a)

            for m in self.maps:
                if m.bpm is None:
                    continue

                key = self.display_duplicate_key(m)
                if key == map_a_key or key in seen_b:
                    continue
                seen_b.add(key)

                if not self.text_match(m, q):
                    continue
                match = self.match_category_and_speed(m.bpm, self.map_a.bpm, tol)
                if match:
                    label, speed_factor = match
                    categories[label].append((m, speed_factor))

            self.filtered_b = []
            total = 0
            counts: dict[str, int] = {}
            for label in self.b_categories:
                items = categories[label]
                items.sort(key=lambda pair: (pair[0].artist.lower(), pair[0].title.lower(), pair[0].version.lower()))
                counts[label] = len(items)
                target_list = self.b_lists[label]
                for m, speed_factor in items:
                    target_list.addItem(self.make_b_item(m, label, speed_factor))
                    self.filtered_b.append(m)
                    total += 1

            # Put the counts directly on the tab labels.
            for i, label in enumerate(self.b_categories):
                self.b_tabs.setTabText(i, f"{label} ({counts.get(label, 0)})")

            half = self.map_a.bpm / 2.0
            double = self.map_a.bpm * 2.0
            speed_text = " Speed matching is ON." if self.allow_speed_change.isChecked() else " Speed matching is OFF."
            self.status.setText(
                f"Found {total} Beatmap B candidates. Exact categories: "
                f"{half:.3f}, {self.map_a.bpm:.3f}, or {double:.3f} BPM."
                f"{speed_text}"
            )
        finally:
            for lst in self.b_lists.values():
                lst.blockSignals(False)
                lst.setUpdatesEnabled(True)

    def select_a(self):
        items = self.list_a.selectedItems()
        if not items:
            return
        self.map_a = items[0].data(Qt.UserRole)
        self.map_b = None
        self.card_a.set_map(self.map_a)
        self.card_b.set_map(None)
        self.refresh_b_list()

    def select_b(self):
        sender = self.sender()
        if isinstance(sender, QListWidget):
            active_list = sender
        else:
            active_list = self.b_tabs.currentWidget() if hasattr(self, "b_tabs") else None
        if not isinstance(active_list, QListWidget):
            return

        # Clear selections in the other Beatmap B tabs so only one B map is active.
        for lst in getattr(self, "b_lists", {}).values():
            if lst is not active_list:
                lst.blockSignals(True)
                lst.clearSelection()
                lst.blockSignals(False)

        items = active_list.selectedItems()
        if not items:
            return
        selected_map = items[0].data(Qt.UserRole)
        if selected_map is None:
            return
        self.map_b = selected_map
        self.selected_b_category = items[0].data(Qt.UserRole + 1) or ""
        self.selected_speed_factor = float(items[0].data(Qt.UserRole + 2) or 1.0)
        self.card_b.set_map(self.map_b)
        if self.map_a and self.map_b:
            self.update_alignment_status()


    def scan_reverts_clicked(self):
        folder = self.folder()
        if not folder.exists():
            QMessageBox.warning(self, "Missing folder", "That Songs folder does not exist.")
            return
        save_songs_folder(str(folder))
        self.revert_list.clear()
        self.revert_details.setText("Scanning for (Changed) audio files...")
        QApplication.processEvents()
        try:
            self.revert_candidates = find_revert_candidates(folder)
        except Exception as e:
            QMessageBox.critical(self, "Revert scan failed", str(e))
            return
        for c in self.revert_candidates:
            item = QListWidgetItem(c.display)
            item.setData(Qt.UserRole, c)
            self.revert_list.addItem(item)
        self.revert_details.setText(f"Found {len(self.revert_candidates)} revertable swapped audio file(s).")

    def select_revert(self):
        items = self.revert_list.selectedItems()
        if not items:
            return
        c: RevertCandidate = items[0].data(Qt.UserRole)
        current_status = "exists" if Path(c.current_audio_path).exists() else "missing"
        self.revert_details.setText(
            f"Map:\n{c.artist} - {c.title} [{c.version}]\n\n"
            f"Current swapped audio ({current_status}):\n{c.current_audio_path}\n\n"
            f"Original backup to restore:\n{c.changed_audio_path}\n\n"
            "Reverting will rename the current swapped audio to '(Swapped Out)' and rename the '(Changed)' file back to the original name."
        )

    def revert_selected_clicked(self):
        items = self.revert_list.selectedItems()
        if not items:
            QMessageBox.warning(self, "Pick a revert", "Select a map/audio backup to revert first.")
            return
        c: RevertCandidate = items[0].data(Qt.UserRole)
        msg = (
            f"Restore original audio for:\n{c.display}\n\n"
            f"Backup to restore:\n{c.changed_audio_path}\n\n"
            f"Back to original name:\n{c.current_audio_path}\n\n"
            "The current swapped audio will be kept as '(Swapped Out)' if it exists. Continue?"
        )
        if QMessageBox.question(self, "Confirm revert", msg) != QMessageBox.Yes:
            return
        try:
            restored = revert_candidate(c, keep_swapped_copy=True)
            QMessageBox.information(self, "Reverted", f"Restored:\n{restored}")
            self.scan_reverts_clicked()
        except PermissionError as e:
            QMessageBox.critical(
                self,
                "Revert failed",
                "Windows is blocking one of the audio files. Stop previewing/playing the map in osu!, switch maps, or close osu!, then try again.\n\n"
                + str(e),
            )
        except Exception as e:
            QMessageBox.critical(self, "Revert failed", str(e))

    def swap_clicked(self):
        if not self.map_a or not self.map_b:
            QMessageBox.warning(self, "Pick maps", "Select Beatmap A and Beatmap B first.")
            return

        a_sync_ms = self.sync_note_ms(self.map_a)
        b_sync_ms = self.sync_note_ms(self.map_b)
        transformed_b_first = int(round(b_sync_ms / max(self.selected_speed_factor, 0.0001)))
        delta = a_sync_ms - transformed_b_first
        speed_line = "No speed change"
        if abs(self.selected_speed_factor - 1.0) >= 0.0001:
            speed_line = (
                f"Speed change: x{self.selected_speed_factor:.6f} "
                f"({'pitch corrected' if self.preserve_pitch.isChecked() else 'pitch changes with speed'})"
            )

        msg = (
            f"Beatmap A:\n{self.map_display(self.map_a)}\n\n"
            f"Beatmap B:\n{self.map_display(self.map_b)}\n\n"
            f"Category: {self.selected_b_category or 'Unknown'}\n"
            f"Sync mode: {'main-BPM note' if self.use_bpm_sync() else 'map start note'}\n"
            f"{speed_line}\n"
            f"Offset after speed adjustment: {delta} ms\n\n"
            "This will rename Beatmap A's original audio to '(Changed)', then create Beatmap B's aligned audio using Beatmap A's original filename. "
            "Beatmap A's .osu file will NOT be changed. If Beatmap B's audio is shorter, silence will be added at the end to match Beatmap A's original length. Continue?"
        )
        if QMessageBox.question(self, "Confirm swap", msg) != QMessageBox.Yes:
            return
        try:
            out = make_aligned_audio(
                self.map_a,
                self.map_b,
                speed_factor=self.selected_speed_factor,
                preserve_pitch=self.preserve_pitch.isChecked(),
                map_a_sync_ms=a_sync_ms,
                map_b_sync_ms=b_sync_ms,
            )
            QMessageBox.information(self, "Complete", f"Done!\n\nCreated:\n{out}")
            self.status.setText("Complete! Beatmap A's .osu AudioFilename was not changed.")
        except Exception as e:
            QMessageBox.critical(self, "Swap failed", str(e))


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    icon_file = resource_path("favicon.ico")
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()