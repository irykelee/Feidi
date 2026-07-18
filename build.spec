# -*- mode: python ; coding: utf-8 -*-
# Feidi Windows 打包配置
# 用法（Windows）: pyinstaller build.spec
# 产物: dist/Feidi.exe

import os
from PyInstaller.utils.hooks import collect_submodules

# H-8: 把本地 vendored qrcode_lib 一并打入；collect_submodules 兜底 hidden imports
datas = [
    (os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'qrcode_lib'), 'qrcode_lib'),
]
hiddenimports = collect_submodules('qrcode')

a = Analysis(
    ['transfer.py'],
    pathex=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    datas=datas,
    hiddenimports=hiddenimports,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Feidi',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # B3/B17: silent no-op，且会触发 Windows Defender；显式关闭
    console=True,          # 显示黑色终端窗口（显示服务器日志）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # B4: 删掉死条件；icon.ico 不存在，保留 None
)
