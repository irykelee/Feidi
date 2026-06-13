# -*- mode: python ; coding: utf-8 -*-
# Feidi PyInstaller 打包配置
# 用法:
#   Windows: pyinstaller build.spec
#   macOS:   pyinstaller build.spec
# 打包后 dist/ 目录下会生成 Feidi.exe (Windows) 或 Feidi.app (macOS)

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
    console=True,          # 显示黑色终端窗口（显示服务器日志）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if False else None,  # 如果有 icon.ico 可以取消注释
)
