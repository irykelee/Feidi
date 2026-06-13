# 飞递 Feidi

> 局域网文本/图片/文件互传工具 — 绿色单文件，零安装，纯局域网传输，关闭即焚。

## 特性

- 🌐 **纯局域网** — 数据不经过外网，安全私密
- 🪶 **零安装** — 单文件 exe，下载即用
- 📱 **跨平台** — PC 建服务，手机扫码即连（支持 Windows/macOS/Linux）
- 💬 **多设备** — 支持多台电脑 + 手机同时在线互传
- 📎 **任意文件** — 文本、图片、文档、音频、压缩包等
- 🌙 **深色模式** — 亮色/深色一键切换，自动跟随系统
- 🔐 **密码保护** — 可选访问密码，局域网内安全共享
- 🧹 **关闭即焚** — 退出自动清理所有临时文件

## 快速开始

### Windows

双击 `Feidi.exe`，用手机扫二维码即可连接。

也可以从源码运行：
```bash
# 安装依赖（仅 qrcode）
pip install qrcode

# 启动
python transfer.py
```

### macOS / Linux

```bash
# 安装依赖
pip3 install qrcode

# 启动
./start.sh
# 或
python3 transfer.py
```

### 打包为 exe

```bash
pip install pyinstaller
pyinstaller build.spec
# 输出在 dist/Feidi.exe
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--port PORT` | HTTP 服务端口 | `9876` |
| `--pass PASSWORD` | 访问密码（推荐用环境变量） | 无 |
| `--no-browser` | 不自动打开浏览器 | `false` |

环境变量 `FEIDI_PASSWORD` 也可设置密码（推荐，避免命令行暴露）。

## 使用说明

1. 电脑和手机连接**同一 Wi-Fi**
2. 电脑端启动 Feidi，终端会显示手机连接地址
3. 手机扫码或浏览器输入地址即可连接
4. 支持发送文字、图片、任意文件
5. 多台设备同时在线互传

### 手机无法连接？

1. 确认手机和电脑在同一 Wi-Fi
2. Windows 防火墙可能拦截，以管理员运行：
   ```
   netsh advfirewall firewall add rule name="Feidi" dir=in action=allow protocol=TCP localport=9876
   ```
3. 或双击项目中的 `allow_firewall.bat`（需管理员权限）

## 项目结构

```
Feidi/
├── transfer.py          # 主程序（单文件）
├── start.bat            # Windows 启动脚本
├── start.sh             # macOS/Linux 启动脚本
├── qrcode_lib/          # 内置 QR 码库（离线可用）
├── build.spec           # Windows PyInstaller 配置
├── build_mac.spec       # macOS PyInstaller 配置
└── allow_firewall.bat   # Windows 防火墙放行脚本
```

## 安全

- 所有数据仅在局域网内传输，不经过任何外网服务器
- 图片和文件存储于临时目录，程序退出自动清理
- 密码认证使用 SHA-256 哈希 + 时序安全比较
- 文件路径访问有 UUID 格式校验防止路径穿越
- 内置速率限制防止滥用

## License

MIT
