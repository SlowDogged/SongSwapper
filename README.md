# SongSwapper

A GUI based tool for swapping osu! beatmap audio with BPM based compatible songs.

## Features

- Scan osu! Songs folder
- Find same / half / double BPM maps
- Auto speed up or slow-down songs to match the BPM
- AAuto align audio by first playable object
- Ignore spinners for first-note detection
- Preserve `.osu` AudioFilename
- Revert changed maps
- Fast cached scanning

## Requirements

- Windows
- ffmpeg.exe included beside the program
- some machienes may not work with the ffmpeg packaged inside the program, so try installing ffmpeg by following this guide: https://www.youtube.com/watch?v=JR36oH35Fgg

- Linux
- PySide6 (installed automatically on first run inside the local virtual enviroment)
- ffmpeg

## Linux Installation and Usage

Clone the repository and run the executable:

```
git clone https://github.com/SlowDogged/SongSwapper.git
cd SongSwapper
chmod +x osu_song_swapper
./osu_song_swapper
```

## Usage

1. Open osu-audio-swapper.exe or ./osu_song_swapper
2. Select your osu! Songs folder
3. Click Fast Scan / Rescan
4. Pick Beatmap A
5. Pick Beatmap B
6. Click Create Changed Map Audio
