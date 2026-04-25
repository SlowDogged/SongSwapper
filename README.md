# SongSwapper

A GUI based tool for swapping osu! beatmap audio with BPM based compatible songs.

## Features

- Scan osu! Songs folder
- Find same / half / double BPM maps
- AAuto align audio by first playable object
- Ignore spinners for first-note detection
- Preserve `.osu` AudioFilename
- Revert changed maps
- Fast cached scanning

## Requirements

- Windows
- ffmpeg.exe included beside the program
- some machienes may not work with the ffmpeg packaged inside the program, so try installing ffmpeg by following this guide: https://www.youtube.com/watch?v=JR36oH35Fgg

## Usage

1. Open osu-audio-swapper.exe
2. Select your osu! Songs folder
3. Click Fast Scan / Rescan
4. Pick Beatmap A
5. Pick Beatmap B
6. Click Create Changed Map Audio
