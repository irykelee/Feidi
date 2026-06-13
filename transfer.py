#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞递 Feidi — 局域网文本/图片互传工具
绿色单文件，零安装，纯局域网传输，关闭即焚。
"""

import os
import sys
import base64
import json
import uuid
import time
import socket
import argparse
import queue
import secrets
import shutil
import re
import atexit
import signal
import threading
import webbrowser
import tempfile
import socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# 内置 qrcode 库路径（vendored，无需 pip install）
if getattr(sys, 'frozen', False):
    # PyInstaller 打包后，资源文件在 _MEIPASS 临时目录
    _script_dir = sys._MEIPASS
else:
    _script_dir = os.path.dirname(os.path.abspath(__file__))
_qrcode_path = os.path.join(_script_dir, 'qrcode_lib')
if os.path.isdir(_qrcode_path):
    sys.path.insert(0, _qrcode_path)
    try:
        import qrcode as _qrcode
    except ImportError:
        _qrcode = None
else:
    _qrcode = None

# --- 命令行参数 ---
parser = argparse.ArgumentParser(description="飞递 Feidi — 局域网传输工具")
parser.add_argument("--port", type=int, default=9876, help="HTTP 服务端口 (默认 9876)")
parser.add_argument("--pass", dest="password", type=str, default="", help="访问密码，为空则不设密码")
parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
args = parser.parse_args()

PORT = args.port
PASSWORD = args.password or os.environ.get("FEIDI_PASSWORD", "")
NO_BROWSER = args.no_browser
TEMP_DIR = tempfile.mkdtemp(prefix="feidi_")

# 允许端口复用，避免前次关闭后 TIME_WAIT 导致绑定失败
socketserver.TCPServer.allow_reuse_address = True

# --- 安全限制 ---
MAX_BODY_SIZE = 100 * 1024 * 1024   # POST body 最大 100MB
MAX_SSE_CLIENTS = 20                  # 最大并发 SSE 连接数
ALLOWED_SENDERS = {"pc", "mobile"}    # 合法的发送者标识
AUTH_TOKEN = secrets.token_hex(16) if PASSWORD else ""  # 用随机 token 代替密码明文
LOCAL_IP = None  # 缓存，首次调用 get_local_ip() 后填充

# 消息存储: {id, type, data, sender, time}
messages = []
# 图片消息的文件路径: {msg_id: (bin_path, mime_path)}
MSG_FILES = {}
MAX_MESSAGES = 200
# 速率限制
_rate_limits = {}  # {ip: [timestamps]} 滑动窗口
_rate_lock = threading.Lock()
RATE_LIMIT = 5     # 每秒最多 5 个请求
RATE_WINDOW = 1.0
# SSE 客户端列表: [{"queue": Queue, "device_id": str, "name": str, "type": str}, ...]
sse_clients = []
_sse_lock = threading.Lock()
_msg_lock = threading.Lock()


def cleanup():
    """退出时清理临时文件"""
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)


atexit.register(cleanup)


def signal_handler(sig, frame):
    print("\n正在关闭...")
    cleanup()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def get_local_ip():
    """获取本机局域网 IP。遍历所有非回环接口，优先常用局域网段。"""
    global LOCAL_IP
    if LOCAL_IP is not None:
        return LOCAL_IP

    candidates = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            candidates.append(ip)
    except Exception:
        pass

    # 如果没有找到，尝试通过创建 UDP socket 来探测（不实际发包）
    if not candidates:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.1.1", 1))
            candidates.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass

    # 优先选择常见局域网段：192.168.x.x > 10.x.x.x > 172.16-31.x.x > 其他
    def ip_priority(ip):
        parts = ip.split(".")
        if len(parts) != 4:
            return 999
        a, b = int(parts[0]), int(parts[1])
        if a == 192 and b == 168:
            return 0
        if a == 10:
            return 1
        if a == 172 and 16 <= b <= 31:
            return 2
        return 3

    candidates.sort(key=ip_priority)
    LOCAL_IP = candidates[0] if candidates else "127.0.0.1"
    return LOCAL_IP


def _cleanup_msg_files(msg_id):
    """清理指定消息 ID 关联的临时文件"""
    entry = MSG_FILES.pop(msg_id, None)
    if entry:
        for p in entry:
            if os.path.exists(p):
                os.remove(p)


def check_rate_limit(client_ip):
    """滑动窗口速率限制，返回 True 表示未超限"""
    now = time.time()
    with _rate_lock:
        if client_ip not in _rate_limits:
            _rate_limits[client_ip] = []
        _rate_limits[client_ip] = [t for t in _rate_limits[client_ip] if now - t < RATE_WINDOW]
        if len(_rate_limits[client_ip]) >= RATE_LIMIT:
            return False
        _rate_limits[client_ip].append(now)
    return True


def add_message(msg_type, data, sender, device_name="", device_id=""):
    """添加消息并通知所有 SSE 客户端，排除发送者自身"""
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "type": msg_type,
        "sender": sender,
        "sender_name": device_name or sender,
        "device_id": device_id,
        "time": int(time.time() * 1000),
    }
    if msg_type == "image":
        if data.startswith("data:"):
            header, b64 = data.split(",", 1)
            mime = header.split(";")[0][5:]
            img_bin = base64.b64decode(b64)
        else:
            img_bin = data if isinstance(data, bytes) else data.encode("utf-8")
            mime = "application/octet-stream"
        bin_path = os.path.join(TEMP_DIR, f"img_{msg_id}.bin")
        mime_path = os.path.join(TEMP_DIR, f"img_{msg_id}.mime")
        with open(bin_path, "wb") as f:
            f.write(img_bin)
        with open(mime_path, "w", encoding="utf-8") as f:
            f.write(mime)
        MSG_FILES[msg_id] = (bin_path, mime_path)
        msg["data"] = f"/img/{msg_id}"
    elif msg_type == "file":
        # data = {"name": str, "size": int, "mime": str, "data": base64_str}
        file_info = data if isinstance(data, dict) else {}
        fname = file_info.get("name", "unknown")
        fsize = file_info.get("size", 0)
        fmime = file_info.get("mime", "application/octet-stream")
        fb64 = file_info.get("data", "")
        try:
            fbin = base64.b64decode(fb64) if fb64 else b""
        except Exception:
            fbin = b""
        fpath = os.path.join(TEMP_DIR, f"file_{msg_id}.bin")
        fmeta = os.path.join(TEMP_DIR, f"file_{msg_id}.meta.json")
        with open(fpath, "wb") as f:
            f.write(fbin)
        with open(fmeta, "w", encoding="utf-8") as f:
            json.dump({"name": fname, "size": fsize, "mime": fmime}, f)
        MSG_FILES[msg_id] = (fpath, fmeta)
        msg["data"] = {"name": fname, "size": fsize, "mime": fmime, "path": f"/file/{msg_id}"}
    else:
        msg["data"] = data
    with _msg_lock:
        messages.append(msg)
        if len(messages) > MAX_MESSAGES:
            old = messages.pop(0)
            _cleanup_msg_files(old["id"])
    broadcast_sse("new_message", msg, exclude_device=device_id if device_id else None)
    return msg_id


def broadcast_sse(event, data, exclude_device=None):
    """向所有 SSE 客户端广播事件。exclude_device 排除指定设备。"""
    dead = []
    if isinstance(data, (dict, list)):
        json_data = json.dumps(data, ensure_ascii=False)
    elif isinstance(data, str):
        json_data = data
    else:
        json_data = json.dumps(data, ensure_ascii=False)
    with _sse_lock:
        for c in sse_clients:
            if exclude_device and c.get("device_id") == exclude_device:
                continue
            try:
                c["queue"].put_nowait(f"event: {event}\ndata: {json_data}\n\n")
            except Exception:
                dead.append(c)
        for c in dead:
            if c in sse_clients:
                sse_clients.remove(c)


def broadcast_device_list():
    """广播当前连接的设备列表"""
    with _sse_lock:
        devices = [{"id": c["device_id"], "name": c["name"], "type": c["type"]} for c in sse_clients]
        data = json.dumps({"devices": devices, "count": len(devices)}, ensure_ascii=False)
        dead = []
        for c in sse_clients:
            try:
                c["queue"].put_nowait(f"event: device_list\ndata: {data}\n\n")
            except Exception:
                dead.append(c)
        for c in dead:
            if c in sse_clients:
                sse_clients.remove(c)


# --- QR 码 SVG 生成（基于 vendored qrcode 库，完全离线） ---
def generate_qr_svg(data, module_px=4, border=4):
    """使用内置 qrcode 库生成 QR 码 SVG 字符串。"""
    if _qrcode is None:
        return (
            '<div style="padding:16px 8px;color:#666;font-size:13px;word-break:break-all;text-align:center">'
            'QR 库未加载，请在手机浏览器访问:<br>'
            '<b style="color:#2e7d32;font-size:14px">%s</b></div>' % data
        )
    try:
        qr = _qrcode.QRCode(box_size=1, border=border)
        qr.add_data(data)
        qr.make(fit=True)
        mod = qr.modules
        size = len(mod)
        total = (size + 2 * border) * module_px
        margin = border * module_px
        lines = [
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
            'width="200" height="200" shape-rendering="crispEdges">' % (total, total),
            '<rect width="%d" height="%d" fill="#ffffff"/>' % (total, total)
        ]
        for r in range(size):
            for c in range(size):
                if mod[r][c]:
                    lines.append(
                        '<rect x="%d" y="%d" width="%d" height="%d" fill="#2e7d32"/>'
                        % (margin + c * module_px, margin + r * module_px, module_px, module_px)
                    )
        lines.append('</svg>')
        return '\n'.join(lines)
    except Exception as e:
        return (
            '<div style="padding:16px 8px;color:#c62828;font-size:13px;text-align:center">'
            'QR 生成失败: %s<br>请在手机浏览器访问:<br>'
            '<b style="color:#2e7d32;font-size:14px">%s</b></div>' % (str(e), data)
        )


# --- PC 端 HTML（完全离线，QR 码由服务端 SVG 直接嵌入） ---
PC_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>飞递 Feidi - 电脑端</title>
<style>
  :root{
    --c-primary:#059669;--c-primary-dark:#047857;--c-primary-light:#d1fae5;
    --c-accent:#f59e0b;--c-bg:#f1f5f9;--c-surface:#ffffff;
    --c-text:#0f172a;--c-text2:#64748b;--c-text3:#94a3b8;
    --c-border:#e2e8f0;--c-border-light:#f1f5f9;
    --c-msg-pc:#ecfdf5;--c-msg-mobile:#f8fafc;
    --radius:20px;--radius-sm:14px;--radius-xs:10px;
    --shadow:0 1px 3px rgba(0,0,0,.04),0 1px 2px rgba(0,0,0,.06);
    --shadow-md:0 4px 6px -1px rgba(0,0,0,.04),0 2px 4px -2px rgba(0,0,0,.04);
    --shadow-lg:0 10px 15px -3px rgba(0,0,0,.04),0 4px 6px -4px rgba(0,0,0,.04);
    --c-bg-img:linear-gradient(135deg,#f0fdf4 0%,#f8fafc 30%,#f1f5f9 100%);
  }
  /* 深色模式 */
  [data-theme="dark"]{
    --c-primary:#10b981;--c-primary-dark:#059669;--c-primary-light:#064e3b;
    --c-accent:#fbbf24;--c-bg:#0f172a;--c-surface:#1e293b;
    --c-text:#e2e8f0;--c-text2:#94a3b8;--c-text3:#64748b;
    --c-border:#334155;--c-border-light:#1e293b;
    --c-msg-pc:#064e3b;--c-msg-mobile:#1e293b;
    --c-bg-img:linear-gradient(135deg,#0f172a 0%,#0f172a 100%);
    --shadow:0 1px 3px rgba(0,0,0,.2);--shadow-md:0 4px 6px rgba(0,0,0,.2);--shadow-lg:0 10px 20px rgba(0,0,0,.3);
  }
  [data-theme="dark"] .panel{border-color:var(--c-border)}
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;background:var(--c-bg-img);height:100vh;display:flex;justify-content:center;align-items:stretch;padding:16px;-webkit-font-smoothing:antialiased;transition:background .3s}
  .container{display:flex;gap:18px;max-width:1100px;width:100%;align-items:stretch}
  .panel{flex:1;min-width:340px;max-width:560px;background:var(--c-surface);border-radius:var(--radius);box-shadow:var(--shadow-lg);display:flex;flex-direction:column;overflow:hidden;min-height:0;border:1px solid var(--c-border-light)}
  .panel-header{display:flex;align-items:center;justify-content:center;gap:6px;padding:14px 20px;background:linear-gradient(135deg,var(--c-primary),#10b981);color:#fff;flex-shrink:0;position:relative}
  .panel-header .logo-text{font-size:18px;font-weight:700;letter-spacing:.5px}
  .panel-header .logo-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:rgba(255,255,255,.7);margin-left:2px}
  .panel-header .sub{font-size:11px;opacity:.75;font-weight:400;margin-top:1px;position:absolute;right:20px}
  .status-row{display:flex;align-items:center;justify-content:center;gap:6px;padding:6px 16px;background:var(--c-border-light);border-bottom:1px solid var(--c-border);flex-shrink:0}
  .status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;position:relative}
  .status-dot.online{background:#10b981;box-shadow:0 0 6px rgba(16,185,129,.5)}
  .status-dot.offline{background:#94a3b8}
  .status-text{font-size:11px;color:var(--c-text2);font-weight:500}
  .messages{flex:1;overflow-y:auto;padding:16px;min-height:220px;display:flex;flex-direction:column;gap:10px;scroll-behavior:smooth}
  .msg{max-width:78%;padding:10px 15px;border-radius:var(--radius-xs);font-size:14px;line-height:1.65;word-break:break-word;animation:msgIn .25s cubic-bezier(.4,0,.2,1);position:relative}
  .msg.pc{align-self:flex-end;background:var(--c-msg-pc);color:#064e3b;border-bottom-right-radius:4px;border:1px solid #a7f3d0}
  [data-theme="dark"] .msg.pc{color:#6ee7b7;border-color:#047857}
  .msg.mobile{align-self:flex-start;background:var(--c-msg-mobile);color:var(--c-text);border-bottom-left-radius:4px;border:1px solid var(--c-border)}
  .msg img{max-width:220px;max-height:220px;border-radius:8px;cursor:pointer;display:block;margin-top:6px;transition:transform .15s}
  .msg img:hover{transform:scale(1.02)}
  .msg .meta{font-size:10px;color:var(--c-text3);margin-top:5px;display:flex;align-items:center;gap:4px}
  .msg.pc .meta{justify-content:flex-end}
  @keyframes msgIn{from{opacity:0;transform:translateY(12px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
  .input-area{padding:12px 16px 14px;border-top:1px solid var(--c-border);display:flex;gap:8px;align-items:flex-end;background:var(--c-surface);flex-shrink:0}
  .input-area textarea{flex:1;border:1.5px solid var(--c-border);border-radius:var(--radius-xs);padding:10px 14px;font-size:14px;resize:none;outline:none;font-family:inherit;min-height:42px;max-height:120px;background:#f8fafc;color:var(--c-text);transition:border-color .2s,box-shadow .2s,background .2s}
  [data-theme="dark"] .input-area textarea{background:var(--c-surface)}
  .input-area textarea:focus{border-color:var(--c-primary);box-shadow:0 0 0 3px rgba(5,150,105,.12);background:var(--c-surface)}
  .input-area textarea::placeholder{color:var(--c-text3)}
  .input-area .btn-send{width:42px;height:42px;border:none;background:linear-gradient(135deg,var(--c-primary),#10b981);color:#fff;border-radius:50%;cursor:pointer;font-size:17px;flex-shrink:0;transition:all .2s;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(5,150,105,.25)}
  .input-area .btn-send:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(5,150,105,.35)}
  .input-area .btn-send:active{transform:scale(.95)}
  .input-area .btn-img{width:38px;height:38px;border:1.5px dashed var(--c-border);background:transparent;color:var(--c-text2);border-radius:var(--radius-xs);cursor:pointer;font-size:20px;flex-shrink:0;transition:all .2s;display:flex;align-items:center;justify-content:center}
  .input-area .btn-img:hover{background:var(--c-primary-light);border-color:var(--c-primary);color:var(--c-primary)}
  .input-area .btn-file{width:38px;height:38px;border:1.5px dashed var(--c-border);background:transparent;color:var(--c-text2);border-radius:var(--radius-xs);cursor:pointer;font-size:16px;flex-shrink:0;transition:all .2s;display:flex;align-items:center;justify-content:center}
  .input-area .btn-file:hover{background:var(--c-primary-light);border-color:var(--c-primary);color:var(--c-primary)}
  /* 文件消息 */
  .msg .file-card{display:flex;align-items:center;gap:10px;padding:8px;background:rgba(255,255,255,.5);border-radius:8px;cursor:pointer;transition:background .15s}
  .msg .file-card:hover{background:rgba(255,255,255,.8)}
  .msg .file-icon{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
  .msg .file-icon.doc{background:#dbeafe;color:#2563eb}
  .msg .file-icon.audio{background:#ede9fe;color:#7c3aed}
  .msg .file-icon.zip{background:#fef3c7;color:#d97706}
  .msg .file-icon.other{background:#f1f5f9;color:#64748b}
  .msg .file-info{flex:1;min-width:0}
  .msg .file-name{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .msg .file-size{font-size:10px;color:var(--c-text3)}
  .qr-panel{width:282px;flex-shrink:0;align-self:stretch;display:flex;flex-direction:column;gap:14px}
  .qr-box{background:var(--c-surface);border-radius:var(--radius);box-shadow:var(--shadow-lg);padding:22px 20px;text-align:center;border:1px solid var(--c-border-light)}
  .qr-box .qr-title{font-size:13px;color:var(--c-text2);margin-bottom:16px;font-weight:500;display:flex;align-items:center;justify-content:center;gap:6px}
  .qr-box .qr-svg-wrapper{display:inline-block;padding:10px;background:#fff;border-radius:12px;border:1px solid var(--c-border);margin-bottom:10px}
  [data-theme="dark"] .qr-box .qr-svg-wrapper{background:#fff;padding:10px}
  .qr-box .qr-svg-wrapper svg{display:block}
  .qr-box .qr-url{font-size:11px;color:var(--c-text3);word-break:break-all;font-family:"SF Mono","Cascadia Code","Consolas",monospace}
  /* 设备列表 */
  .device-list{background:var(--c-surface);border-radius:var(--radius);box-shadow:var(--shadow-lg);border:1px solid var(--c-border-light);overflow:hidden}
  .device-list .dl-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--c-border-light)}
  .device-list .dl-header .dl-title{font-size:13px;font-weight:600;color:var(--c-text);display:flex;align-items:center;gap:6px}
  .device-list .dl-header .dl-count{font-size:11px;color:var(--c-text3);background:var(--c-border-light);padding:2px 8px;border-radius:10px}
  .device-list .dl-body{padding:8px;max-height:200px;overflow-y:auto}
  .device-item{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;transition:background .15s}
  .device-item:hover{background:var(--c-border-light)}
  .device-item .di-icon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
  .device-item .di-icon.pc{background:#dbeafe;color:#2563eb}
  .device-item .di-icon.mobile{background:var(--c-primary-light);color:var(--c-primary)}
  .device-item .di-info{flex:1;min-width:0}
  .device-item .di-name{font-size:13px;font-weight:500;color:var(--c-text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .device-item .di-type{font-size:10px;color:var(--c-text3)}
  .device-item .di-badge{font-size:9px;padding:1px 6px;border-radius:8px;font-weight:500}
  .device-item .di-badge.me{background:var(--c-primary-light);color:var(--c-primary)}
  .device-item .di-status{width:6px;height:6px;border-radius:50%;background:#10b981;flex-shrink:0}
  .device-empty{text-align:center;padding:16px;color:var(--c-text3);font-size:12px}
  /* QR 折叠 */
  .qr-section{display:flex;flex-direction:column;gap:14px}
  .qr-box.collapsed{display:none}
  .qr-toggle-btn{width:100%;padding:8px;border:1px dashed var(--c-border);border-radius:var(--radius-xs);background:transparent;color:var(--c-text2);cursor:pointer;font-size:12px;transition:all .2s;display:none;text-align:center}
  .qr-toggle-btn.visible{display:block}
  .qr-toggle-btn:hover{background:var(--c-primary-light);border-color:var(--c-primary);color:var(--c-primary)}
  .theme-toggle{position:absolute;right:12px;top:50%;transform:translateY(-50%);width:32px;height:32px;border:none;background:rgba(255,255,255,.15);color:#fff;border-radius:50%;cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center;transition:background .2s;padding:0;line-height:1}
  .theme-toggle:hover{background:rgba(255,255,255,.25)}
  .info{flex:1;display:flex;flex-direction:column;gap:8px;background:var(--c-surface);border-radius:var(--radius);box-shadow:var(--shadow-md);padding:16px;font-size:12px;color:var(--c-text2);line-height:1.8;border:1px solid var(--c-border-light)}
  .info-item{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--c-border-light)}
  .info-item:last-child{border-bottom:none}
  .info-icon{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
  .info-icon.green{background:var(--c-primary-light);color:var(--c-primary)}
  .info-icon.amber{background:#fef3c7;color:#d97706}
  .info-text{font-size:12px;line-height:1.5}
  .info-text b{display:block;font-size:13px;color:var(--c-text);margin-bottom:2px}
  .empty-state{flex:1;display:flex;align-items:center;justify-content:center;color:var(--c-text3);font-size:14px;flex-direction:column;gap:10px}
  .empty-state .empty-icon{width:64px;height:64px;border-radius:50%;background:var(--c-primary-light);display:flex;align-items:center;justify-content:center;font-size:30px}
  .toast{position:fixed;top:24px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.85);color:#fff;padding:10px 20px;border-radius:24px;font-size:13px;z-index:100;opacity:0;transition:all .3s cubic-bezier(.4,0,.2,1);pointer-events:none;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(2px)}
  #fileInput{display:none}
  ::-webkit-scrollbar{width:4px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--c-border);border-radius:10px}
  ::-webkit-scrollbar-thumb:hover{background:var(--c-text3)}
</style>
</head>
<body>
<div class="container">
  <div class="panel">
    <div class="panel-header">
      <span class="logo-text">飞递 Feidi</span>
      <span class="logo-dot"></span>
      <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="切换深色模式">&#x263E;</button>
    </div>
    <div class="status-row">
      <span class="status-dot offline" id="statusDot"></span>
      <span class="status-text" id="statusText">等待手机连接</span>
    </div>
    <div class="messages" id="messages">
      <div class="empty-state" id="emptyState">
        <div class="empty-icon">&#x1F4E1;</div>
        <div>手机扫码后即可开始互传</div>
        <div style="font-size:11px;color:var(--c-text3)">文本、图片实时同步</div>
      </div>
    </div>
    <div class="input-area">
      <input type="file" id="fileInput" accept="image/*" multiple>
      <input type="file" id="docInput" multiple>
      <button class="btn-img" onclick="pickImage()" title="发送图片">+</button>
      <button class="btn-file" onclick="pickFile()" title="发送文件">&#x1F4CE;</button>
      <textarea id="textInput" rows="1" placeholder="输入消息..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendText()}"></textarea>
      <button class="btn-send" onclick="sendText()" title="发送">&#8593;</button>
    </div>
    <div class="toast" id="toast"></div>
  </div>
  <div class="qr-panel">
    <div class="device-list" id="deviceList" style="display:none">
      <div class="dl-header">
        <span class="dl-title">&#x1F4BB; 已连接设备</span>
        <span class="dl-count" id="dlCount">0</span>
      </div>
      <div class="dl-body" id="dlBody">
        <div class="device-empty">暂无设备连接</div>
      </div>
    </div>
    <div class="qr-section" id="qrSection">
      <div class="qr-box" id="qrBox">
        <div class="qr-title">&#x1F4F1; 手机扫码连接</div>
        <div class="qr-svg-wrapper">__QR_SVG__</div>
        <div class="qr-url">__MOBILE_URL__</div>
      </div>
      <div class="info" id="infoBox">
        <div class="info-item">
          <div class="info-icon green">&#x1F310;</div>
          <div class="info-text"><b>局域网传输</b>数据不经过外网，安全私密</div>
        </div>
        <div class="info-item">
          <div class="info-icon amber">&#x26A0;</div>
          <div class="info-text"><b>扫码提示</b>请用手机相机或浏览器扫码，微信内置浏览器可能打不开</div>
        </div>
      </div>
    </div>
    <button class="qr-toggle-btn" id="qrToggleBtn" onclick="toggleQr()">&#x1F4F1; 显示二维码</button>
  </div>
</div>
<script>
// 主题管理
(function(){
  const KEY = "feidi_theme";
  const toggleBtn = document.getElementById("themeToggle");
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    toggleBtn.innerHTML = theme === "dark" ? "&#x2600;" : "&#x263E;";
    toggleBtn.title = theme === "dark" ? "切换亮色模式" : "切换深色模式";
    try { localStorage.setItem(KEY, theme); } catch(e) {}
  }
  // 初始化：优先用户选择 > 系统偏好
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch(e) {}
  if (saved) {
    setTheme(saved);
  } else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
    setTheme("dark");
  }
  window.toggleTheme = function() {
    var cur = document.documentElement.getAttribute("data-theme");
    setTheme(cur === "dark" ? "light" : "dark");
  };
})();
(function(){
  var MY_ID = "";
  var MY_NAME = "";
  var MY_TYPE = "pc";
  const SENDER = "pc";
  const msgContainer = document.getElementById("messages");
  let emptyState = document.getElementById("emptyState");
  const textInput = document.getElementById("textInput");
  const fileInput = document.getElementById("fileInput");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const toastEl = document.getElementById("toast");
  const qrBox = document.getElementById("qrBox");
  const infoBox = document.getElementById("infoBox");
  const qrToggleBtn = document.getElementById("qrToggleBtn");
  const deviceList = document.getElementById("deviceList");
  const dlBody = document.getElementById("dlBody");
  const dlCount = document.getElementById("dlCount");
  const docInput = document.getElementById("docInput");

  function showToast(msg, isError) {
    toastEl.textContent = msg;
    toastEl.style.background = isError ? "rgba(211,47,47,.9)" : "rgba(0,0,0,.75)";
    toastEl.className = "toast show";
    setTimeout(function() { toastEl.className = "toast"; }, 2500);
  }

  function updateStatus(count) {
    if (count > 0) {
      statusDot.className = "status-dot online";
      statusText.textContent = "已连接 (" + count + " 台设备)";
      // 折叠二维码
      if (qrBox) { qrBox.classList.add("collapsed"); }
      if (infoBox) { infoBox.style.display = "none"; }
      if (qrToggleBtn) { qrToggleBtn.classList.add("visible"); }
    } else {
      statusDot.className = "status-dot offline";
      statusText.textContent = "等待设备连接";
      if (qrBox) { qrBox.classList.remove("collapsed"); }
      if (infoBox) { infoBox.style.display = ""; }
      if (qrToggleBtn) { qrToggleBtn.classList.remove("visible"); }
    }
  }

  window.toggleQr = function() {
    if (qrBox.classList.contains("collapsed")) {
      qrBox.classList.remove("collapsed");
      if (infoBox) infoBox.style.display = "";
      qrToggleBtn.textContent = "收起二维码";
    } else {
      qrBox.classList.add("collapsed");
      if (infoBox) infoBox.style.display = "none";
      qrToggleBtn.textContent = "显示二维码";
    }
  };

  // 设备列表渲染
  function renderDeviceList(devices) {
    if (!deviceList || !dlBody) return;
    var count = devices.length;
    dlCount.textContent = count;
    if (count === 0) {
      deviceList.style.display = "none";
      updateStatus(0);
      return;
    }
    deviceList.style.display = "block";
    var otherCount = 0;
    var html = "";
    devices.forEach(function(d) {
      var isMe = d.id === MY_ID;
      if (!isMe) otherCount++;
      var icon = d.type === "mobile" ? "&#x1F4F1;" : "&#x1F4BB;";
      var iconCls = d.type === "mobile" ? "mobile" : "pc";
      html += '<div class="device-item">' +
        '<div class="di-icon ' + iconCls + '">' + icon + '</div>' +
        '<div class="di-info"><div class="di-name">' + escHtml(d.name || d.type) + (isMe ? ' <span class="di-badge me">本机</span>' : '') + '</div>' +
        '<div class="di-type">' + (d.type === "mobile" ? "手机" : "电脑") + '</div></div>' +
        '<div class="di-status"></div>' +
        '</div>';
    });
    dlBody.innerHTML = html;
    updateStatus(otherCount);
  }

  function escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function formatSize(bytes) {
    if (!bytes || bytes < 0) return "0 B";
    var units = ["B", "KB", "MB", "GB"];
    var i = 0;
    var s = bytes;
    while (s >= 1024 && i < units.length - 1) { s /= 1024; i++; }
    return (i === 0 ? s : s.toFixed(1)) + " " + units[i];
  }

  // SSE — 带设备参数
  var deviceName = "电脑";
  try { deviceName = /Mac|Win|Linux/.exec(navigator.userAgent) ? decodeURIComponent(/Mac|Win|Linux/.exec(navigator.userAgent)[0]) : "电脑"; } catch(e) {}
  var seenMsgs = new Set();
  var evtSource = new EventSource("/events?type=" + MY_TYPE + "&name=" + encodeURIComponent(deviceName));

  evtSource.addEventListener("device_id", function(e) {
    var data = JSON.parse(e.data);
    MY_ID = data.device_id;
    MY_NAME = data.name;
    MY_TYPE = data.type;
  });

  evtSource.addEventListener("history", function(e) {
    var msgs = JSON.parse(e.data);
    msgs.forEach(function(m) {
      if (!seenMsgs.has(m.id)) { seenMsgs.add(m.id); appendMessage(m, false); }
    });
  });
  evtSource.addEventListener("new_message", function(e) {
    var msg = JSON.parse(e.data);
    if (seenMsgs.has(msg.id)) return;
    seenMsgs.add(msg.id);
    if (seenMsgs.size > 500) { seenMsgs.clear(); }  // 防止内存无限增长
    appendMessage(msg, true);
  });
  evtSource.addEventListener("device_list", function(e) {
    var data = JSON.parse(e.data);
    renderDeviceList(data.devices || []);
  });
  evtSource.onopen = function() {
    fetch("/status").then(function(r) { return r.json(); }).then(function(d) {
      updateStatus(d.connections);
    }).catch(function(){});
  };

  function appendMessage(msg, animate) {
    if (emptyState) { emptyState.remove(); emptyState = null; }
    var isMe = (msg.device_id && msg.device_id === MY_ID) || msg.sender === SENDER;
    var div = document.createElement("div");
    div.className = "msg " + msg.sender;
    if (msg.type === "text") {
      div.textContent = msg.data;
    } else if (msg.type === "image") {
      var img = document.createElement("img");
      img.src = msg.data;
      img.onclick = function() { window.open(msg.data); };
      div.appendChild(img);
    } else if (msg.type === "file" && msg.data) {
      var fd = msg.data;
      var card = document.createElement("div");
      card.className = "file-card";
      card.onclick = function() { window.open(fd.path); };
      var ficon = document.createElement("div");
      var ext = (fd.name || "").split(".").pop().toLowerCase();
      var ic = "&#x1F4C4;"; var icCls = "other";
      if (/^(pdf|doc|docx|xls|xlsx|ppt|pptx|txt)$/.test(ext)) { ic = "&#x1F4C4;"; icCls = "doc"; }
      else if (/^(mp3|wav|flac|aac|ogg|wma|m4a)$/.test(ext)) { ic = "&#x1F3B5;"; icCls = "audio"; }
      else if (/^(zip|rar|7z|tar|gz|bz2|xz)$/.test(ext)) { ic = "&#x1F4E6;"; icCls = "zip"; }
      else if (/^(mp4|mkv|avi|mov|wmv|flv)$/.test(ext)) { ic = "&#x1F3AC;"; }
      else if (/^(jpg|jpeg|png|gif|bmp|webp|svg)$/.test(ext)) { ic = "&#x1F5BC;"; }
      ficon.className = "file-icon " + icCls;
      ficon.innerHTML = ic;
      card.appendChild(ficon);
      var finfo = document.createElement("div");
      finfo.className = "file-info";
      finfo.innerHTML = '<div class="file-name">' + escHtml(fd.name || "文件") + '</div><div class="file-size">' + formatSize(fd.size) + '</div>';
      card.appendChild(finfo);
      div.appendChild(card);
    }
    var meta = document.createElement("div");
    meta.className = "meta";
    var nameLabel = msg.sender_name || msg.sender;
    if (isMe) nameLabel = "我";
    meta.textContent = nameLabel + " · " + new Date(msg.time).toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit"});
    div.appendChild(meta);
    msgContainer.appendChild(div);
    msgContainer.scrollTop = msgContainer.scrollHeight;
  }

  window.sendText = function() {
    var text = textInput.value.trim();
    if (!text) return;
    fetch("/send", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: text, sender: SENDER, device_name: MY_NAME, device_id: MY_ID})
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (!data.ok) throw new Error("发送失败");
      // 本地立即显示（因 exclude_device 不会通过 SSE 回传）
      var localMsg = {id: data.msg_id, type: "text", data: text, sender: SENDER, sender_name: "我", device_id: MY_ID, time: Date.now()};
      seenMsgs.add(localMsg.id);
      appendMessage(localMsg, true);
    }).catch(function() {
      showToast("发送失败，请检查连接", true);
    });
    textInput.value = "";
    textInput.style.height = "";
  };

  window.pickImage = function() { fileInput.click(); };
  window.pickFile = function() { docInput.click(); };
  fileInput.onchange = function() {
    for (var i = 0; i < fileInput.files.length; i++) {
      (function(file) {
        var reader = new FileReader();
        reader.onload = function() {
          var imgDataUri = reader.result;
          fetch("/send", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({image: imgDataUri, sender: SENDER, device_name: MY_NAME, device_id: MY_ID})
          }).then(function(r) { return r.json(); }).then(function(data) {
            if (!data.ok) throw new Error("发送失败");
            // 本地立即显示
            var path = "/img/" + data.msg_id;
            var localMsg = {id: data.msg_id, type: "image", data: path, sender: SENDER, sender_name: "我", device_id: MY_ID, time: Date.now()};
            seenMsgs.add(localMsg.id);
            appendMessage(localMsg, true);
          }).catch(function() {
            showToast("发送失败，请检查连接", true);
          });
        };
        reader.readAsDataURL(file);
      })(fileInput.files[i]);
    }
    fileInput.value = "";
  };
  docInput.onchange = function() {
    for (var i = 0; i < docInput.files.length; i++) {
      (function(file) {
        var reader = new FileReader();
        reader.onload = function() {
          var fb64 = reader.result.split(",")[1] || reader.result;
          var fileInfo = {name: file.name, size: file.size, mime: file.type || "application/octet-stream", data: fb64};
          fetch("/send", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({file: fileInfo, sender: SENDER, device_name: MY_NAME, device_id: MY_ID})
          }).then(function(r) { return r.json(); }).then(function(data) {
            if (!data.ok) throw new Error("发送失败");
            var localMsg = {id: data.msg_id, type: "file", data: {name: file.name, size: file.size, path: "/file/" + data.msg_id}, sender: SENDER, sender_name: "我", device_id: MY_ID, time: Date.now()};
            seenMsgs.add(localMsg.id);
            appendMessage(localMsg, true);
            showToast("文件已发送");
          }).catch(function() {
            showToast("文件过大或发送失败", true);
          });
        };
        reader.readAsDataURL(file);
      })(docInput.files[i]);
    }
    docInput.value = "";
  };
})();
</script>
</body>
</html>
"""

# --- 手机端 HTML ---
MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>飞递 Feidi</title>
<style>
  :root{
    --c-pri:#059669;--c-pri-light:#d1fae5;--c-bg:#e8f5e9;--c-surface:#fff;
    --c-text:#1b5e20;--c-text2:#555;--c-text3:#999;--c-border:#e0e0e0;
    --c-msg-self:#c8e6c9;--c-msg-other:#fff;--c-input-bg:#fff;
  }
  [data-theme="dark"]{
    --c-pri:#10b981;--c-pri-light:#064e3b;--c-bg:#0f172a;--c-surface:#1e293b;
    --c-text:#e2e8f0;--c-text2:#94a3b8;--c-text3:#64748b;--c-border:#334155;
    --c-msg-self:#064e3b;--c-msg-other:#1e293b;--c-input-bg:#0f172a;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--c-bg);color:var(--c-text);min-height:100vh;min-height:100dvh;display:flex;flex-direction:column}
  .header{background:linear-gradient(135deg,var(--c-pri),#10b981);color:#fff;padding:10px 14px;font-size:16px;font-weight:600;text-align:center;letter-spacing:.5px;box-shadow:0 2px 8px rgba(0,0,0,.1);position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:center;gap:6px}
  .header .sub{font-size:10px;opacity:.8;font-weight:400}
  .theme-btn{position:absolute;right:10px;width:28px;height:28px;border:none;background:rgba(255,255,255,.15);color:#fff;border-radius:50%;cursor:pointer;font-size:13px;display:flex;align-items:center;justify-content:center}
  .status-bar{display:flex;align-items:center;justify-content:center;gap:6px;padding:6px;font-size:12px;background:var(--c-surface);border-bottom:1px solid var(--c-border)}
  .status-bar.connected{color:#10b981}
  .status-bar.disconnected{color:#ef4444}
  .dot{width:7px;height:7px;border-radius:50%}
  .dot.green{background:#10b981;box-shadow:0 0 5px rgba(16,185,129,.4)}
  .dot.red{background:#ef4444}
  .messages{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:8px;background:var(--c-bg)}
  .msg{max-width:80%;padding:10px 14px;border-radius:12px;font-size:15px;line-height:1.6;word-break:break-word;animation:fadeIn .3s}
  .msg.mobile{align-self:flex-end;background:var(--c-msg-self);color:#1b5e20;border-bottom-right-radius:4px}
  [data-theme="dark"] .msg.mobile{color:#6ee7b7}
  .msg.pc{align-self:flex-start;background:var(--c-msg-other);color:var(--c-text);border-bottom-left-radius:4px}
  .msg img{max-width:200px;max-height:200px;border-radius:8px;cursor:pointer;display:block;margin-top:4px}
  .msg .meta{font-size:10px;opacity:.5;margin-top:4px}
  @keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
  .input-area{display:flex;gap:8px;padding:10px 14px;padding-bottom:max(10px,env(safe-area-inset-bottom));background:var(--c-surface);border-top:1px solid var(--c-border);align-items:flex-end}
  .input-area input[type=text]{flex:1;border:1.5px solid var(--c-border);border-radius:20px;padding:10px 16px;font-size:15px;outline:none;font-family:inherit;background:var(--c-input-bg);color:var(--c-text)}
  .input-area input[type=text]:focus{border-color:var(--c-pri)}
  .input-area button{width:40px;height:40px;border:none;background:var(--c-pri);color:#fff;border-radius:50%;cursor:pointer;font-size:18px;flex-shrink:0;display:flex;align-items:center;justify-content:center}
  .input-area .btn-img{background:#64748b}
  #fileInput{display:none}
  .empty-state{flex:1;display:flex;align-items:center;justify-content:center;color:var(--c-text3);font-size:15px;flex-direction:column;gap:8px}
  .toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.85);color:#fff;padding:8px 16px;border-radius:20px;font-size:13px;z-index:100;opacity:0;transition:opacity .3s}
  .toast.show{opacity:1}
</style>
</head>
<body>
<div class="header">飞递 Feidi<button class="theme-btn" id="themeBtn" onclick="toggleTheme()">&#x263E;</button><div class="sub">手机端</div></div>
<div class="status-bar connected" id="statusBar">
  <span class="dot green"></span><span>已连接</span>
</div>
<div class="messages" id="messages">
  <div class="empty-state" id="emptyState">
    <div style="font-size:48px">&#x2709;</div>
    <div>发送第一条消息吧</div>
  </div>
</div>
<div class="input-area">
  <input type="file" id="fileInput" accept="image/*" capture="environment">
  <button class="btn-img" onclick="pickImage()" title="拍照/图片">&#x1F4F7;</button>
  <input type="text" id="textInput" placeholder="输入文字..." onkeydown="if(event.key==='Enter'){event.preventDefault();sendText()}">
  <button onclick="sendText()" title="发送">&#10148;</button>
</div>
<div class="toast" id="toast"></div>
<div class="login-overlay" id="loginOverlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:200;display:none;align-items:center;justify-content:center">
  <div style="background:#fff;border-radius:16px;padding:24px;width:280px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.2)">
    <div style="font-size:16px;font-weight:600;color:#2e7d32;margin-bottom:16px">飞递 Feidi</div>
    <div style="font-size:13px;color:#666;margin-bottom:12px">请输入访问密码</div>
    <input type="password" id="passwordInput" placeholder="密码" style="width:100%;padding:10px 14px;border:1px solid #ddd;border-radius:10px;font-size:15px;outline:none;text-align:center;margin-bottom:12px;box-sizing:border-box">
    <button onclick="doLogin()" style="width:100%;padding:10px;background:#43a047;color:#fff;border:none;border-radius:10px;font-size:15px;cursor:pointer">连接</button>
    <div id="loginError" style="color:#e53935;font-size:12px;margin-top:8px;display:none">密码错误</div>
  </div>
</div>
<script>
// 主题管理
(function(){
  var KEY = "feidi_theme";
  var btn = document.getElementById("themeBtn");
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    if (btn) { btn.innerHTML = t === "dark" ? "&#x2600;" : "&#x263E;"; }
    try { localStorage.setItem(KEY, t); } catch(e) {}
  }
  var saved; try { saved = localStorage.getItem(KEY); } catch(e) {}
  if (saved) setTheme(saved);
  else if (matchMedia("(prefers-color-scheme: dark)").matches) setTheme("dark");
  window.toggleTheme = function() {
    var cur = document.documentElement.getAttribute("data-theme");
    setTheme(cur === "dark" ? "light" : "dark");
  };
})();
(function(){
  const SENDER = "mobile";

  const seenMsgs = new Set();
  const evtSource = new EventSource("/events?type=mobile&name=" + encodeURIComponent("手机"));
  evtSource.addEventListener("history", function(e) {
    const msgs = JSON.parse(e.data);
    msgs.forEach(function(m) {
      if (!seenMsgs.has(m.id)) { seenMsgs.add(m.id); appendMessage(m, false); }
    });
  });
  evtSource.addEventListener("new_message", function(e) {
    const msg = JSON.parse(e.data);
    if (seenMsgs.has(msg.id)) return;
    seenMsgs.add(msg.id);
    appendMessage(msg, true);
  });

  evtSource.onopen = function() {
    document.getElementById("statusBar").className = "status-bar connected";
    document.getElementById("statusBar").innerHTML = '<span class="dot green"></span><span>已连接</span>';
  };
  evtSource.onerror = function(e) {
    if (evtSource.readyState === EventSource.CLOSED) {
      fetch("/status").then(function(r) {
        if (r.status === 403) {
          document.getElementById("loginOverlay").style.display = "flex";
        }
      });
    }
    document.getElementById("statusBar").className = "status-bar disconnected";
    document.getElementById("statusBar").innerHTML = '<span class="dot red"></span><span>连接断开，重连中...</span>';
  };

  const msgContainer = document.getElementById("messages");
  let emptyState = document.getElementById("emptyState");
  const textInput = document.getElementById("textInput");
  const fileInput = document.getElementById("fileInput");
  const toast = document.getElementById("toast");
  const loginOverlay = document.getElementById("loginOverlay");
  const passwordInput = document.getElementById("passwordInput");
  const loginError = document.getElementById("loginError");

  function showToast(msg) {
    toast.textContent = msg;
    toast.className = "toast show";
    setTimeout(function() { toast.className = "toast"; }, 2000);
  }

  window.doLogin = function() {
    const pw = passwordInput.value.trim();
    if (!pw) return;
    fetch("/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: pw})
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (data.ok) {
        loginOverlay.style.display = "none";
        location.reload();
      } else {
        loginError.style.display = "block";
        passwordInput.value = "";
      }
    });
  };

  function appendMessage(msg, animate) {
    if (emptyState) { emptyState.remove(); emptyState = null; }
    const div = document.createElement("div");
    div.className = "msg " + msg.sender;
    if (msg.type === "text") {
      div.textContent = msg.data;
    } else if (msg.type === "image") {
      const img = document.createElement("img");
      img.src = msg.data;
      img.onclick = function() { window.open(msg.data); };
      div.appendChild(img);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = new Date(msg.time).toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit"});
    div.appendChild(meta);
    msgContainer.appendChild(div);
    msgContainer.scrollTop = msgContainer.scrollHeight;
  }

  window.sendText = function() {
    const text = textInput.value.trim();
    if (!text) return;
    fetch("/send", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: text, sender: SENDER})
    });
    textInput.value = "";
  };

  window.pickImage = function() { fileInput.click(); };
  fileInput.onchange = function() {
    for (var i = 0; i < fileInput.files.length; i++) {
      (function(file) {
        const reader = new FileReader();
        reader.onload = function() {
          fetch("/send", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({image: reader.result, sender: SENDER})
          }).then(function(r) {
            if (!r.ok) showToast("图片过大，请压缩后重试");
          });
        };
        reader.readAsDataURL(file);
      })(fileInput.files[i]);
    }
    fileInput.value = "";
  };
})();
</script>
</body>
</html>
"""


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """支持多线程的 HTTP 服务器，每个请求在独立线程中处理。"""
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    def log_message(self, format, *args):
        pass

    def check_password(self):
        """检查密码 — 通过 Cookie 中的 auth token"""
        if not PASSWORD:
            return True
        cookies = {}
        cookie_header = self.headers.get("Cookie", "")
        for item in cookie_header.replace(" ", "").split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                cookies[k] = v
        return secrets.compare_digest(cookies.get("feidi_auth", ""), AUTH_TOKEN)

    def set_auth_cookie(self):
        self.send_header(
            "Set-Cookie",
            f"feidi_auth={AUTH_TOKEN}; Path=/; Max-Age=86400; SameSite=Lax",
        )

    def send_html(self, html_content):
        body = html_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def send_error_body(self, code, msg):
        body = msg.encode("utf-8") if isinstance(msg, str) else msg
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 登录页和静态 HTML 无需认证（否则密码保护会死锁）
        if path == "/" or path == "/pc":
            ip = get_local_ip()
            mobile_url = f"http://{ip}:{PORT}/mobile"
            if PASSWORD:
                mobile_url += f"?code={AUTH_TOKEN[:8]}"
            qr_svg = generate_qr_svg(mobile_url)
            html_content = PC_HTML.replace("__QR_SVG__", qr_svg).replace("__MOBILE_URL__", mobile_url)
            self.send_html(html_content)
            return

        if path == "/mobile":
            self.send_html(MOBILE_HTML)
            return

        if not self.check_password():
            self.send_error_body(403, "Forbidden: wrong password")
            return
            # 图片消息的 data 是 /img/{id}，前端直接用此 URL
            self.send_json(200, messages)

        elif path.startswith("/img/"):
            # 服务图片二进制文件（仅允许 UUID 格式，防路径穿越）
            img_id = path[5:]
            if not re.match(r'^[a-f0-9-]+$', img_id):
                self.send_error_body(400, "Invalid image id")
                return
            bin_path = os.path.join(TEMP_DIR, f"img_{img_id}.bin")
            mime_path = os.path.join(TEMP_DIR, f"img_{img_id}.mime")
            if not os.path.isfile(bin_path):
                self.send_error_body(404, "Not Found")
                return
            with open(mime_path, "r", encoding="utf-8") as f:
                mime = f.read().strip()
            with open(bin_path, "rb") as f:
                img_bin = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "public, max-age=60")
            self.send_header("Content-Length", str(len(img_bin)))
            self.end_headers()
            self.wfile.write(img_bin)
            self.wfile.flush()

        elif path.startswith("/file/"):
            # 下载文件（仅允许 UUID 格式）
            file_id = path[6:]
            if not re.match(r'^[a-f0-9-]+$', file_id):
                self.send_error_body(400, "Invalid file id")
                return
            fpath = os.path.join(TEMP_DIR, f"file_{file_id}.bin")
            fmeta = os.path.join(TEMP_DIR, f"file_{file_id}.meta.json")
            if not os.path.isfile(fpath) or not os.path.isfile(fmeta):
                self.send_error_body(404, "Not Found")
                return
            with open(fmeta, "r", encoding="utf-8") as f:
                meta = json.load(f)
            with open(fpath, "rb") as f:
                fbin = f.read()
            self.send_response(200)
            self.send_header("Content-Type", meta.get("mime", "application/octet-stream"))
            self.send_header("Content-Disposition", f'attachment; filename="{meta.get("name", "download")}"')
            self.send_header("Content-Length", str(len(fbin)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(fbin)
            self.wfile.flush()

        elif path == "/status":
            self.send_json(200, {"connections": len(sse_clients), "messages": len(messages)})

        elif path == "/events":
            if len(sse_clients) >= MAX_SSE_CLIENTS:
                self.send_error_body(503, "Too many connections")
                return

            # 解析设备信息
            params = parse_qs(parsed.query)
            dev_type = params.get("type", ["unknown"])[0]
            dev_name = params.get("name", [dev_type])[0]
            if dev_type not in ("pc", "mobile"):
                dev_type = "unknown"
            device_id = str(uuid.uuid4())[:8]
            dev_info = {"queue": queue.Queue(), "device_id": device_id, "name": dev_name, "type": dev_type}

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.flush()

            with _sse_lock:
                sse_clients.append(dev_info)
            broadcast_device_list()

            # 发送设备身份和消息历史
            try:
                self.wfile.write(f"event: device_id\ndata: {json.dumps({'device_id': device_id, 'name': dev_name, 'type': dev_type}, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                history = json.dumps(messages, ensure_ascii=False)
                self.wfile.write(f"event: history\ndata: {history}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                with _sse_lock:
                    if dev_info in sse_clients:
                        sse_clients.remove(dev_info)
                broadcast_device_list()
                return

            try:
                while True:
                    try:
                        data = dev_info["queue"].get(timeout=15)
                        self.wfile.write(data.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(": keepalive\n\n".encode("utf-8"))
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    if dev_info in sse_clients:
                        sse_clients.remove(dev_info)
                broadcast_device_list()

        else:
            self.send_error_body(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/login":
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 1024:
                self.send_error_body(413, "Request too large")
                return
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_error_body(400, "Invalid JSON")
                return
            if secrets.compare_digest(data.get("password", ""), PASSWORD):
                self.set_auth_cookie()
                self.send_json(200, {"ok": True})
            else:
                self.send_json(403, {"ok": False, "error": "wrong password"})
            return

        if not self.check_password():
            self.send_error_body(403, "Forbidden: wrong password")
            return

        if path == "/send":
            # 速率限制
            client_ip = self.client_address[0]
            if not check_rate_limit(client_ip):
                self.send_error_body(429, "Too many requests")
                return

            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > MAX_BODY_SIZE:
                self.send_error_body(413, "Request too large")
                return
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_error_body(400, "Invalid JSON")
                return

            sender = data.get("sender", "")
            if sender not in ALLOWED_SENDERS:
                sender = "unknown"
            dev_name = data.get("device_name", "")
            dev_id = data.get("device_id", "")

            if "text" in data and data["text"]:
                text = data["text"]
                if len(text) > 10000:
                    self.send_error_body(413, "Text too long (max 10000 chars)")
                    return
                msg_id = add_message("text", text, sender, dev_name, dev_id)
            elif "image" in data and data["image"]:
                img_data = data["image"]
                if len(img_data) > 5 * 1024 * 1024:
                    self.send_error_body(413, "Image too large (max 5MB)")
                    return
                if not img_data.startswith("data:image/"):
                    self.send_error_body(400, "Only data:image/... URIs accepted")
                    return
                msg_id = add_message("image", img_data, sender, dev_name, dev_id)
            elif "file" in data and data["file"]:
                file_data = data["file"]
                if not isinstance(file_data, dict):
                    self.send_error_body(400, "Invalid file data")
                    return
                fsize = file_data.get("size", 0)
                if fsize > 50 * 1024 * 1024:
                    self.send_error_body(413, "File too large (max 50MB)")
                    return
                msg_id = add_message("file", file_data, sender, dev_name, dev_id)
            else:
                self.send_error_body(400, "No text, image or file")
                return

            self.send_json(200, {"ok": True, "msg_id": msg_id})

        else:
            self.send_error_body(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.flush()


def main():
    local_ip = get_local_ip()
    url = f"http://{local_ip}:{PORT}"
    mobile_url = url + "/mobile"
    if PASSWORD:
        mobile_url += f"?code={AUTH_TOKEN[:8]}"

    print("-" * 52)
    print("  飞递 Feidi - 局域网传输工具")
    print("-" * 52)
    print(f"  电脑端:  {url}")
    print(f"  手机端:  {mobile_url}")
    if PASSWORD:
        print(f"  密码保护: 已启用 (连接码: {AUTH_TOKEN[:8]})")
    print(f"  按 Ctrl+C 停止")
    print("-" * 52)
    print("  \033[93m提示:\033[0m 手机扫码后若无法打开，请检查：")
    print("    1. 手机与电脑是否在同一 Wi-Fi")
    print("    2. Windows 防火墙是否放行了端口", PORT)
    print("       \033[90m(以管理员运行: netsh advfirewall firewall add rule")
    print(f"        name=\"Feidi\" dir=in action=allow protocol=TCP localport={PORT})\033[0m")
    print("-" * 52)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), RequestHandler)
    if not NO_BROWSER:
        print(f"\n服务已启动，浏览器将自动打开...")
        webbrowser.open(url)
    else:
        print(f"\n服务已启动")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\n已关闭，临时文件已清理")


if __name__ == "__main__":
    main()
