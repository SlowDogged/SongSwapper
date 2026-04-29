# -*- mode: python ; coding: utf-8 -*-
app_name = 'SongSwapper'
main_script = 'osu_song_swapper.py'

a = Analysis(
    [main_script],
    pathex=[],
    binaries=[],
    datas=[('ffmpeg_bin','ffmpeg_bin')],
    hiddenimports=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=False,
    name='SongSwapper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name='SongSwapper'
)