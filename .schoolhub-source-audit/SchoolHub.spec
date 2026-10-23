hiddenimports = []
datas = [
    ("vendor/git", "vendor/git"),
    ("vendor/gcm", "vendor/gcm"),
    ("vendor/lfs", "vendor/lfs"),
    ("schoolhub_icon.png", "."),
    ("VERSION.txt", "."),
]

a = Analysis(
    ["src/schoolhub_gui.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SchoolHub",
    icon="schoolhub.ico",
    version="version_info.txt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

# Onedir ships Python DLLs alongside the EXE in _internal. No _MEI temp extraction.
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SchoolHub",
)
