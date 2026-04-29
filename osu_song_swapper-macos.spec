# -*- mode: python ; coding: utf-8 -*-
app_name = 'SongSwapper'
main_script = 'osu_song_swapper.py'

a = Analysis(
    [main_script],
    pathex=[],
    binaries=[],
    datas=datas=[('ffmpeg_bin','ffmpeg_bin')],
    hiddenimports[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SongSwapper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='',
)

app = BUNDLE(
    exe,
    name='SongSwapper' + '.app',
    icon='',
    bundle_identifier='com.paraliyzedevo.songswapper',
    info_plist={
        'CFBundleName': 'songswapper',
        'CFBundleDisplayName': 'songswapper',
        'CFBundleIdentifier': 'com.paraliyzedevo.songswapper',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': 'True',
    },
)