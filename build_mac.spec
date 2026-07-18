# -*- mode: python ; coding: utf-8 -*-
# Feidi macOS 打包配置
# 用法（macOS）: pyinstaller build_mac.spec
# 产物: dist/Feidi.app
# H-9: 公证/签名是手动步骤；CI 中 ad-hoc sign；正式发布前请用 Developer ID + notarytool。

import os
import sys
from PyInstaller.utils.hooks import collect_submodules

if sys.platform != 'darwin':
    print("❌ 请在 macOS 上运行此 spec 文件")
    print("   Windows 打包请用: pyinstaller build.spec")
    sys.exit(1)

# H-8: 同 Windows，把 vendored qrcode_lib 一并打入
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
    upx=False,  # macOS 下 UPX 无意义
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # H-9: 留空让 PyInstaller 做 ad-hoc 签名；正式发布前改为
    #   codesign_identity="Developer ID Application: <Your Team>"
    #   entitlements_file="entitlements.plist"
    #   然后跑 xcrun notarytool submit --wait && xcrun stapler staple dist/Feidi.app
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name='Feidi.app',
    icon=None,
    bundle_identifier='com.feidi.app',
)
