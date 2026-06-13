# -*- mode: python ; coding: utf-8 -*-
# Feidi macOS 打包配置
# 用法: 在 Mac 终端运行 → pyinstaller build_mac.spec
# 打包后 dist/ 目录生成 Feidi.app

import sys

if sys.platform != 'darwin':
    print("❌ 请在 macOS 上运行此 spec 文件")
    print("   Windows 打包请用: pyinstaller build.spec")
    sys.exit(1)

a = Analysis(
    ['transfer.py'],
    pathex=[],
    datas=[
        # 包含 qrcode_lib 目录
        ('qrcode_lib', 'qrcode_lib'),
    ],
    hiddenimports=[
        'qrcode',
        'qrcode.util',
        'qrcode.base',
        'qrcode.image',
        'qrcode.image.svg',
    ],
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
    a.binaries,
    a.datas,
    [],
    name='Feidi',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name='Feidi.app',
    icon=None,
    bundle_identifier='com.feidi.app',
)
