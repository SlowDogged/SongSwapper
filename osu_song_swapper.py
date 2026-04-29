#!/usr/bin/env python3
"""
osu! Audio Swapper GUI / Midblock Helper

Install:
    pip install PySide6

Run:
    python osu_audio_swapper_gui.py

Needs ffmpeg + ffprobe in PATH, or ffmpeg.exe/ffprobe.exe beside the EXE.
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

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
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
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

CACHE_NAME = "osu_audio_swapper_cache.json"
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
    first_note_ms: Optional[int]
    audio_length_seconds: Optional[float]
    osu_path: str
    folder_path: str
    audio_filename: str
    audio_path: str

    @property
    def display(self) -> str:
        bpm = "?" if self.bpm is None else f"{self.bpm:.3f}"
        first = "?" if self.first_note_ms is None else f"{self.first_note_ms / 1000:.3f}s"
        return f"{self.artist} - {self.title} [{self.version}] | BPM {bpm} | first {first}"


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


def get_primary_bpm(lines: list[str]) -> Optional[float]:
    bpms: list[float] = []
    for line in section_lines(lines, "TimingPoints"):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            beat_len = float(parts[1])
            uninherited = True
            if len(parts) >= 7:
                uninherited = parts[6].strip() == "1"
            if beat_len > 0 and uninherited:
                bpms.append(60000.0 / beat_len)
        except ValueError:
            continue
    if not bpms:
        return None
    return max(set(round(x, 6) for x in bpms), key=lambda x: bpms.count(x))


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
    return BeatmapInfo(
        title=metadata.get("TitleUnicode") or metadata.get("Title", path.stem),
        artist=metadata.get("ArtistUnicode") or metadata.get("Artist", "Unknown Artist"),
        version=metadata.get("Version", "Unknown Difficulty"),
        creator=metadata.get("Creator", "Unknown Creator"),
        bpm=get_primary_bpm(lines),
        first_note_ms=get_first_note_ms(lines),
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
        return [BeatmapInfo(**x) for x in json.loads(p.read_text(encoding="utf-8"))]
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


def make_aligned_audio_file(src_b: Path, output_path: Path, offset_ms: int) -> None:
    """Create Beatmap B audio aligned to Beatmap A's first playable object.

    Output is always encoded to match Beatmap A's original file extension.
    This allows mp3 -> ogg, ogg -> mp3, etc. without changing the .osu file.
    """
    codec_args = ffmpeg_audio_codec_args(output_path)

    if offset_ms == 0:
        if src_b.suffix.lower() == output_path.suffix.lower():
            shutil.copy2(src_b, output_path)
        else:
            run_ffmpeg([bundled_tool("ffmpeg"), "-y", "-i", str(src_b), *codec_args, str(output_path)])
    elif offset_ms > 0:
        run_ffmpeg([
            bundled_tool("ffmpeg"), "-y",
            "-i", str(src_b),
            "-af", f"adelay={offset_ms}:all=1",
            *codec_args,
            str(output_path),
        ])
    else:
        trim_seconds = abs(offset_ms) / 1000.0
        run_ffmpeg([
            bundled_tool("ffmpeg"), "-y",
            "-ss", f"{trim_seconds:.3f}",
            "-i", str(src_b),
            *codec_args,
            str(output_path),
        ])


def make_aligned_audio(map_a: BeatmapInfo, map_b: BeatmapInfo) -> Path:
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
    offset_ms = (map_a.first_note_ms or 0) - (map_b.first_note_ms or 0)

    if not a_original_audio.exists():
        raise RuntimeError(f"Beatmap A audio file does not exist:\n{a_original_audio}")
    if not src_b.exists():
        raise RuntimeError(f"Beatmap B audio file does not exist:\n{src_b}")

    temp_output = a_folder / f".__osu_swapper_temp__{a_original_audio.suffix or '.mp3'}"
    if temp_output.exists():
        temp_output.unlink()

    try:
        make_aligned_audio_file(src_b, temp_output, offset_ms)

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
        layout = QVBoxLayout(self)
        self.label = QLabel(title)
        self.label.setObjectName("cardTitle")
        self.song = QLabel("No map selected")
        self.song.setWordWrap(True)
        self.song.setObjectName("songTitle")
        self.details = QLabel("BPM: -   First note: -   Length: -")
        self.details.setObjectName("details")
        layout.addWidget(self.label)
        layout.addWidget(self.song)
        layout.addWidget(self.details)

    def set_map(self, m: Optional[BeatmapInfo]):
        self.map = m
        if not m:
            self.song.setText("No map selected")
            self.details.setText("BPM: -   First note: -   Length: -")
            return
        if m.audio_length_seconds is None:
            m.audio_length_seconds = audio_length(Path(m.audio_path))
        length = "?" if m.audio_length_seconds is None else f"{m.audio_length_seconds:.1f}s"
        self.song.setText(f"{m.title}\n{m.artist} [{m.version}]")
        self.details.setText(f"BPM: {m.bpm:.3f}   First note: {m.first_note_ms/1000:.3f}s   Length: {length}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("osu! Audio Swapper GUI")
        self.resize(920, 760)
        self.maps: list[BeatmapInfo] = []
        self.filtered_a: list[BeatmapInfo] = []
        self.filtered_b: list[BeatmapInfo] = []
        self.map_a: Optional[BeatmapInfo] = None
        self.map_b: Optional[BeatmapInfo] = None
        self.revert_candidates: list[RevertCandidate] = []
        self.setup_ui()

    def setup_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        top = QHBoxLayout()
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
        self.tabs.addTab(swap_tab, "Swap")

        cards = QHBoxLayout()
        self.card_a = MapCard("Beatmap A - map to change")
        self.card_b = MapCard("Beatmap B - song to copy in")
        cards.addWidget(self.card_a)
        cards.addWidget(self.card_b)
        swap_layout.addLayout(cards)

        lists = QHBoxLayout()
        left = QVBoxLayout()
        right = QVBoxLayout()

        self.search_a = QLineEdit()
        self.search_a.setPlaceholderText("Search Beatmap A...")
        self.search_b = QLineEdit()
        self.search_b.setPlaceholderText("Search Beatmap B matches...")
        self.list_a = QListWidget()
        self.list_b = QListWidget()
        self.search_a.textChanged.connect(self.refresh_a_list)
        self.search_b.textChanged.connect(self.refresh_b_list)
        self.list_a.itemSelectionChanged.connect(self.select_a)
        self.list_b.itemSelectionChanged.connect(self.select_b)

        left.addWidget(QLabel("All maps"))
        left.addWidget(self.search_a)
        left.addWidget(self.list_a, 1)
        right.addWidget(QLabel("BPM-compatible candidates (same / half / double)"))
        right.addWidget(self.search_b)
        right.addWidget(self.list_b, 1)
        lists.addLayout(left)
        lists.addLayout(right)
        swap_layout.addLayout(lists, 1)

        controls = QHBoxLayout()
        self.tolerance = QSpinBox()
        self.tolerance.setRange(0, 1000)
        self.tolerance.setValue(30)
        self.tolerance.setSuffix(" / 1000 BPM tolerance")
        self.tolerance.valueChanged.connect(self.refresh_b_list)
        self.swap_btn = QPushButton("Create Changed Map Audio")
        self.swap_btn.clicked.connect(self.swap_clicked)
        controls.addWidget(QLabel("BPM match tolerance:"))
        controls.addWidget(self.tolerance)
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
            QWidget { background: #1f1830; color: #f4edff; font-size: 14px; }
            QLineEdit, QListWidget, QSpinBox { background: #2b2140; border: 1px solid #4f3a77; border-radius: 8px; padding: 7px; }
            QPushButton { background: #c93d91; color: white; border: 0; border-radius: 10px; padding: 10px 14px; font-weight: bold; }
            QPushButton:hover { background: #e04da6; }
            QPushButton:disabled { background: #5b4b68; color: #aaa; }
            QListWidget::item { padding: 7px; border-bottom: 1px solid #392c52; }
            QListWidget::item:selected { background: #7041c9; border-radius: 6px; }
            QFrame#card { background: #332544; border-radius: 16px; padding: 12px; border: 1px solid #5f4380; }
            QLabel#cardTitle { color: #ff66b6; font-weight: bold; }
            QLabel#songTitle { font-size: 22px; font-weight: bold; }
            QLabel#details { color: #c7b6ef; }
            QProgressBar { border: 1px solid #4f3a77; border-radius: 8px; text-align: center; background: #2b2140; }
            QProgressBar::chunk { background: #7d50d7; border-radius: 8px; }
        """)

    def folder(self) -> Path:
        return Path(self.folder_input.text().strip().strip('"'))

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select osu! Songs folder", self.folder_input.text())
        if folder:
            self.folder_input.setText(folder)
            save_songs_folder(folder)

    def set_maps(self, maps: list[BeatmapInfo]):
        self.maps = sorted(maps, key=lambda m: (m.artist.lower(), m.title.lower(), m.version.lower()))
        self.status.setText(f"Loaded {len(self.maps)} maps.")
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

    def make_item(self, m: BeatmapInfo) -> QListWidgetItem:
        item = QListWidgetItem(m.display)
        item.setData(Qt.UserRole, m)
        return item

    def text_match(self, m: BeatmapInfo, q: str) -> bool:
        if not q:
            return True
        hay = f"{m.artist} {m.title} {m.version} {m.creator} {m.bpm}".lower()
        return all(part in hay for part in q.lower().split())

    def bpm_match_label(self, candidate_bpm: float, target_bpm: float, tol: float) -> Optional[str]:
        """Allow normal BPM matches, plus musically-compatible half/double BPM matches.

        Example: if Beatmap A is 180 BPM, this accepts roughly 90, 180, and 360 BPM.
        The audio is not time-stretched; this only expands the Beatmap B candidate list.
        """
        checks = [
            (target_bpm, "same BPM"),
            (target_bpm / 2.0, "half BPM"),
            (target_bpm * 2.0, "double BPM"),
        ]
        best_label = None
        best_diff = None
        for expected, label in checks:
            diff = abs(candidate_bpm - expected)
            if diff <= tol and (best_diff is None or diff < best_diff):
                best_label = label
                best_diff = diff
        return best_label

    def make_b_item(self, m: BeatmapInfo, match_label: str) -> QListWidgetItem:
        item = QListWidgetItem(f"[{match_label}] {m.display}")
        item.setData(Qt.UserRole, m)
        return item

    def refresh_a_list(self):
        q = self.search_a.text().strip()
        self.list_a.clear()
        self.filtered_a = [m for m in self.maps if self.text_match(m, q)]
        for m in self.filtered_a:
            self.list_a.addItem(self.make_item(m))

    def refresh_b_list(self):
        self.list_b.clear()
        if not self.map_a or self.map_a.bpm is None:
            self.status.setText("Select Beatmap A to show BPM-compatible Beatmap B candidates.")
            return
        q = self.search_b.text().strip()
        tol = self.tolerance.value() / 1000.0

        matches: list[tuple[BeatmapInfo, str]] = []
        for m in self.maps:
            if m.osu_path == self.map_a.osu_path or m.bpm is None:
                continue
            if not self.text_match(m, q):
                continue
            label = self.bpm_match_label(m.bpm, self.map_a.bpm, tol)
            if label:
                matches.append((m, label))

        order = {"same BPM": 0, "half BPM": 1, "double BPM": 2}
        matches.sort(key=lambda pair: (order.get(pair[1], 99), pair[0].artist.lower(), pair[0].title.lower(), pair[0].version.lower()))
        self.filtered_b = [m for m, _label in matches]

        for m, label in matches:
            self.list_b.addItem(self.make_b_item(m, label))

        half = self.map_a.bpm / 2.0
        double = self.map_a.bpm * 2.0
        self.status.setText(
            f"Found {len(matches)} BPM-compatible candidates for Beatmap B "
            f"({half:.3f}, {self.map_a.bpm:.3f}, or {double:.3f} BPM)."
        )

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
        items = self.list_b.selectedItems()
        if not items:
            return
        self.map_b = items[0].data(Qt.UserRole)
        self.card_b.set_map(self.map_b)
        if self.map_a and self.map_b:
            delta = (self.map_a.first_note_ms or 0) - (self.map_b.first_note_ms or 0)
            action = "add silence" if delta > 0 else "trim start" if delta < 0 else "no offset needed"
            self.status.setText(f"Alignment delta: {delta} ms — {action}.")


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
        delta = (self.map_a.first_note_ms or 0) - (self.map_b.first_note_ms or 0)
        msg = (
            f"Beatmap A:\n{self.map_a.display}\n\n"
            f"Beatmap B:\n{self.map_b.display}\n\n"
            f"Offset: {delta} ms\n\n"
            "This will rename Beatmap A's original audio to '(Changed)', then create Beatmap B's aligned audio using Beatmap A's original filename. Beatmap A's .osu file will NOT be changed. Continue?"
        )
        if QMessageBox.question(self, "Confirm swap", msg) != QMessageBox.Yes:
            return
        try:
            out = make_aligned_audio(self.map_a, self.map_b)
            QMessageBox.information(self, "Complete", f"Done!\n\nCreated:\n{out}")
            self.status.setText("Complete! Beatmap A now keeps the same .osu AudioFilename, but uses Beatmap B audio.")
        except Exception as e:
            QMessageBox.critical(self, "Swap failed", str(e))


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
