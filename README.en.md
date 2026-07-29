# Feidi — 飞递

> LAN text / image / file transfer tool — zero pip runtime dependencies (QR library vendored in-tree), burns-on-close.

[中文](README.md) · [Changelog](CHANGELOG.md) · [Release checklist](docs/RELEASE_PUSH_CHECKLIST.md)

## Features

- 🌐 **LAN-only** — data never leaves your network; private by design
- 🪶 **Zero deps** — pure Python standard library; runs on Python 3 alone
- 📱 **Cross-platform** — PC runs the server, mobile scans the QR to connect (Windows / macOS / Linux)
- 💬 **Private chat + broadcast** — multiple devices online simultaneously; point-to-point private chat
- 📎 **Any file** — text, images, documents, audio, video, etc., up to 500 MB per transfer
- 🔄 **Resumable upload** — 1 MB chunks; auto-resume after network interruption (server-side state persisted for 7 days)
- 🖱️ **Drag-and-drop** — drop files onto the page to send
- 🌙 **Dark mode** — light / dark toggle; follows system preference
- 🔐 **Password protection** — optional access password + random bearer cookie
- 🔔 **Notifications** — browser notifications + title flash + in-page Toast
- 🏷️ **Device naming** — name yourself + leave remarks for other devices (persisted across reconnects)
- 🧹 **Burns on close** — all temp files auto-clean on exit

## Quick Start

### Download & run

Grab the right artifact from [Releases](https://github.com/irykelee/Feidi/releases) or [Actions](https://github.com/irykelee/Feidi/actions):

| Platform | File |
|----------|------|
| Windows  | `Feidi.exe` |
| macOS    | `Feidi-macos.zip` |

Double-click to run, scan the QR with your phone, you're connected.

### Run from source

```bash
# Zero deps — just start it
python3 transfer.py
```

## Command-line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--port PORT` | HTTP service port | `9876` |
| `--pass PASSWORD`, `--password PASSWORD` | Access password (both flags accepted) | _(none — no auth required)_ |
| `--bind HOST` | Bind address (LAN IP only by default; use `0.0.0.0` to listen on all interfaces) | LAN-detected IP |
| `--no-browser` | Don't auto-open the browser | `false` |

You can also set the password via the `FEIDI_PASSWORD` environment variable.

## Usage

1. Connect both PC and phone to the **same Wi-Fi**
2. Start Feidi on the PC — terminal shows the mobile URL and QR code
3. Scan the QR (or type the URL into your phone's browser) to connect
4. Supports text (broadcast / private chat), images, any file
5. Click a device in the left sidebar to switch to private chat; click again to return to broadcast

### Phone can't connect?

1. Confirm phone and PC are on the same Wi-Fi network
2. Windows Firewall may be blocking inbound — either:
   - Double-click `allow_firewall.bat` (requires admin)
   - Or run manually: `netsh advfirewall firewall add rule name="Feidi" dir=in action=allow protocol=TCP localport=9876`

## Project Structure

```
Feidi/
├── transfer.py            # Main program (single file, ~3200 lines)
├── qrcode_lib/            # Vendored QR library (no pip needed)
├── build.spec             # Windows PyInstaller spec
├── build_mac.spec         # macOS PyInstaller spec
├── start.sh               # macOS/Linux launch script
├── start.bat              # Windows launch script
├── allow_firewall.bat     # Windows firewall allow script
├── requirements-build.txt # Build-only dep (pyinstaller)
├── CHANGELOG.md           # Version history
├── docs/                  # Release docs
└── .github/workflows/     # GitHub Actions CI
```

## Building from Source

Use the in-repo spec files (already include vendored `qrcode_lib` and signing/notarization config):

```bash
pip install pyinstaller
# Windows
pyinstaller --noconfirm --clean build.spec
# macOS
pyinstaller --noconfirm --clean build_mac.spec
# Output goes to dist/ (Feidi.app on macOS / Feidi.exe on Windows)
```

## Security

- All data stays on the LAN; never touches an external server
- Images and files live in a temp directory; auto-cleaned on exit
- **Password auth**: server generates a random 128-bit bearer token, set in an HttpOnly + SameSite=Lax cookie. The submitted password is compared using `secrets.compare_digest` in `/login` POST (constant-time)
- File paths validated as canonical UUID v4 (prevents path traversal)
- Built-in rate limit (5 req/s/IP; `/login` separately capped at 2 req/s/IP)
- Chunked uploads carry a per-session bearer token + sender ownership check (device_id is bound to SSE session)
- SSE handshake issues a per-session token; `/send` and `/rename` require `X-Feidi-Session` header (prevents `device_id` spoofing)
- Known identity reuse verifies IP/MAC (prevents device-handoff impersonation)

> **Note**: Feidi binds to the detected LAN IP by default. If you run it on a public / guest Wi-Fi, use `--bind 0.0.0.0` explicitly to listen on all interfaces (and consider enabling password protection).

## License

MIT — see [LICENSE](LICENSE).

## 简体中文

For Chinese-language documentation, see [README.md](README.md) (中文).