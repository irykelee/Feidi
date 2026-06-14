# 飞递 Feidi

> 局域网文本/图片/文件互传工具 — 纯 Python 标准库，零 pip 依赖，关闭即焚。

## 特性

- 🌐 **纯局域网** — 数据不经过外网，安全私密
- 🪶 **零依赖** — 纯标准库，Python 3 即可运行
- 📱 **跨平台** — PC 建服务，手机扫码即连（Windows / macOS / Linux）
- 💬 **私聊 + 广播** — 支持多设备同时在线，点对点私聊
- 📎 **任意文件** — 文本、图片、文档、音频、视频等，最大 500MB
- 🔄 **断点续传** — 1MB 分块传输，网络中断自动恢复
- 🖱️ **拖拽发送** — 拖文件到页面直接发送
- 🌙 **深色模式** — 亮色/深色一键切换，自动跟随系统
- 🔐 **密码保护** — 可选访问密码 + SHA-256 Cookie 认证
- 🔔 **消息通知** — 浏览器通知 + 标题闪烁 + Toast
- 🏷️ **设备命名** — 给自己起名、给别设备注
- 🧹 **关闭即焚** — 退出自动清理所有临时文件

## 快速开始

### 下载即用

从 [Releases](https://github.com/irykelee/Feidi/releases) 或 [Actions](https://github.com/irykelee/Feidi/actions) 下载对应平台单文件：

| 平台 | 文件 |
|------|------|
| Windows | `Feidi-win.exe` |
| macOS | `Feidi-macos` |

双击运行，手机扫二维码即可连接。

### 从源码运行

```bash
# 零依赖，直接启动
python3 transfer.py
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--port PORT` | HTTP 服务端口 | `9876` |
| `--password PASSWORD` | 访问密码 | 无 |
| `--no-browser` | 不自动打开浏览器 | `false` |

也可通过环境变量 `FEIDI_PASSWORD` 设置密码。

## 使用说明

1. 电脑和手机连接**同一 Wi-Fi**
2. 电脑端启动 Feidi，终端显示手机连接地址和二维码
3. 手机扫码或浏览器输入地址即可连接
4. 支持文字（广播/私聊）、图片、任意文件
5. 点击左侧设备列表切换私聊对象，再次点击回到广播

### 手机无法连接？

1. 确认手机和电脑在同一 Wi-Fi
2. Windows 防火墙可能拦截：
   - 双击 `allow_firewall.bat`（需管理员权限）
   - 或手动执行：`netsh advfirewall firewall add rule name="Feidi" dir=in action=allow protocol=TCP localport=9876`

## 项目结构

```
Feidi/
├── transfer.py            # 主程序（单文件，零依赖）
├── build.spec             # Windows PyInstaller 打包配置
├── build_mac.spec         # macOS PyInstaller 打包配置
├── start.sh               # macOS/Linux 启动脚本
├── start.bat              # Windows 启动脚本
├── allow_firewall.bat     # Windows 防火墙放行脚本
└── .github/workflows/     # GitHub Actions 自动构建
```

## 自行打包

```bash
pip install pyinstaller
pyinstaller --onefile --name Feidi --console --clean transfer.py
# 输出在 dist/ 目录
```

## 安全

- 所有数据仅在局域网内传输，不经过任何外网服务器
- 图片和文件存储于临时目录，程序退出自动清理
- 密码认证使用 SHA-256 哈希 + 时序安全比较
- 文件路径 UUID 格式校验，防止路径穿越
- 内置速率限制（5 req/s/IP）
- 分块传输含发送者校验，防止数据注入

## License

MIT
