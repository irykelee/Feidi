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
import subprocess
import atexit
import signal
import threading
import webbrowser
import tempfile
import socketserver
import hashlib
import ipaddress
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

__version__ = "1.0.1-audit"

# --- QR 码生成（基于 qrcode 库，纯 Python） ---

# 尝试加载 qrcode 库
_qrcode_lib = None
_qrcode_path_vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qrcode_lib')
try:
    import qrcode as _qrlib
    _qrcode_lib = _qrlib
except ImportError:
    if os.path.isdir(_qrcode_path_vendor):
        sys.path.insert(0, _qrcode_path_vendor)
        try:
            import qrcode as _qrlib
            _qrcode_lib = _qrlib
        except ImportError:
            pass


def _get_qr_modules(data, ec="M"):
    """使用 qrcode 库生成 QR 模块矩阵."""
    ec_map = {"L": _qrlib.constants.ERROR_CORRECT_L, "M": _qrlib.constants.ERROR_CORRECT_M,
              "Q": _qrlib.constants.ERROR_CORRECT_Q, "H": _qrlib.constants.ERROR_CORRECT_H}
    qr = _qrlib.QRCode(box_size=1, border=0, error_correction=ec_map.get(ec, _qrlib.constants.ERROR_CORRECT_M))
    qr.add_data(data)
    qr.make(fit=True)
    return qr.modules


def generate_qr_svg(data, module_px=4, border=4):
    """使用 qrcode 库生成 QR 码 SVG 字符串。库不可用时显示文本链接。"""
    if _qrcode_lib is None:
        return (
            '<div style="padding:16px 8px;color:#c62828;font-size:13px;word-break:break-all;text-align:center">'
            'QR 库未加载，请在手机浏览器访问:<br>'
            '<b style="color:#2e7d32;font-size:14px">%s</b></div>' % data
        )
    try:
        modules = _get_qr_modules(data)
        size = len(modules)
        total = (size + 2 * border) * module_px
        margin = border * module_px
        lines = [
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
            'width="200" height="200" shape-rendering="crispEdges">' % (total, total),
            '<rect width="%d" height="%d" fill="#ffffff"/>' % (total, total)
        ]
        for r in range(size):
            for c in range(size):
                if modules[r][c]:
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


# --- 命令行参数 ---
parser = argparse.ArgumentParser(description="飞递 Feidi — 局域网传输工具")
parser.add_argument("--port", type=int, default=9876, help="HTTP 服务端口 (默认 9876)")
parser.add_argument("--pass", "--password", dest="password", type=str, default="", help="访问密码，为空则不设密码")
parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
# Stage G (M4): 自定义绑定地址（默认仅绑定探测到的 LAN IP，避免 0.0.0.0 暴露）
parser.add_argument("--bind", dest="bind", type=str, default="",
                    help="绑定地址，留空仅绑定局域网 IP；显式传 0.0.0.0 监听所有网卡")
args = parser.parse_args()

PORT = args.port
PASSWORD = args.password or os.environ.get("FEIDI_PASSWORD", "")
NO_BROWSER = args.no_browser
BIND_HOST = args.bind  # Stage G (M4)
TEMP_DIR = tempfile.mkdtemp(prefix="feidi_")
CHUNK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feidi_chunks")

# 允许端口复用，避免前次关闭后 TIME_WAIT 导致绑定失败
socketserver.TCPServer.allow_reuse_address = True

# --- 安全限制 ---
MAX_BODY_SIZE = 100 * 1024 * 1024   # POST body 最大 100MB
MAX_SSE_CLIENTS = 20                  # 最大并发 SSE 连接数
SSE_QUEUE_MAX = 256                    # 每连接 SSE 事件队列上限（R-05：防慢消费无限堆积）
ALLOWED_SENDERS = {"pc", "mobile"}    # 合法的发送者标识
AUTH_TOKEN = secrets.token_hex(16) if PASSWORD else ""  # 用随机 token 代替密码明文
LOCAL_IP = None  # 缓存，首次调用 get_local_ip() 后填充

# 消息存储: {id, type, data, sender, time}
messages = []
# 图片消息的文件路径: {msg_id: (bin_path, mime_path)}
MSG_FILES = {}
# 文件级引用计数：{msg_id: (ref_count, entry_paths)}，由 _acquire_file /
# _release_file 维护，防止淘汰与下载竞态（H-3）。_file_refs 与 MSG_FILES 删除路径
# 都在 _file_ref_lock 下；MSG_FILES 新增(add_message)在 _msg_lock 下。
_file_refs: dict = {}
_file_ref_lock = threading.Lock()
CHUNK_SIZE_LIMIT = 2 * 1024 * 1024  # 单块最大 2MB (base64 后 ~2.7MB JSON)
MAX_CHUNKED_FILE = 500 * 1024 * 1024  # 最大 500MB
COMPLETED_TRANSFERS_MAX = 2000       # R-07：completed_transfers 上限（FIFO 淘汰），防内存无限增长
MAX_MESSAGES = 200
# 速率限制
_rate_limits = {}  # {ip: [timestamps]} 滑动窗口
_rate_lock = threading.Lock()
RATE_LIMIT = 5     # 每秒最多 5 个请求
RATE_WINDOW = 1.0
# SSE 客户端列表: [{"queue": Queue, "device_id": str, "name": str, "type": str, "identity_key": str}, ...]
sse_clients = []
_sse_lock = threading.Lock()
_msg_lock = threading.Lock()
# 分块传输: transfer_id -> {chunks: set, total: int, info: dict, created: timestamp, sender, device_name, device_id, target_id}
chunk_transfers = {}
# Stage F (F4): 已完成的 transfer_id -> msg_id；重发同 transfer_id 时直接返回缓存，
# 避免重复 add_message 产生幽灵消息。守护 _chunk_lock。
completed_transfers: dict = {}
# 保护 chunk_transfers 的所有读写（包括 set.add、dict 覆盖、组装读 ct、删除）。BaseHTTPRequestHandler 在 ThreadingMixIn 线程中并发执行。
_chunk_lock = threading.Lock()
# 最大并发分块传输数（防止恶意客户端无限发起 transfer 撑爆内存 / 磁盘）
MAX_CONCURRENT_TRANSFERS = 100
# Stage F (F3/H8): 全局 in-flight 字节配额。一个 500MB 文件在装配期需要约 1.5GB 临时
# 磁盘（chunks + assembled + final copy），并发多文件时此上限防止 OOM/OOS。
MAX_GLOBAL_INFLIGHT_BYTES = 500 * 1024 * 1024
_inflight_bytes = 0
_inflight_lock = threading.Lock()

# --- 时序常量（曾散落在函数中,Stage A 集中）---
STALE_CHUNK_TIMEOUT = 600       # 单个分块传输超时未活动即清理（秒）
CLEANUP_INTERVAL = 300          # 定期清理线程间隔（秒）；原 1800，但 main() 注释声明"每 5 分钟"
SOCKET_TIMEOUT = 30             # HTTP handler socket 读超时（秒）；慢客户端卡死防护
SSE_KEEPALIVE_TIMEOUT = 15      # SSE 队列无事件时多久发一次心跳（秒）
POST_KILL_GRACE = 0.5           # 杀旧进程后等其释放端口（秒）
_server_stop_event = threading.Event()  # 干净关闭清理线程的信号
# 身份持久化: {identity_key: {device_id, name, hostname, last_ip, mac, type, first_seen, last_seen}}
IDENTITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feidi_identities.json")
identity_map = {}
# Stage G (H1): identity_map 加锁 — 之前跨 handler / SSE 重连并发读写 race
_identity_lock = threading.Lock()
_server_hostname = socket.gethostname()

# S-08：允许的 CORS Origin 主机集合（仅本机/局域网实际暴露的 host，避免对任意 IP 反射）。
# main() 启动时填入 LOCAL_IP / BIND_HOST / hostname；默认仅 localhost。
_allowed_origin_hosts = {"127.0.0.1", "localhost", "::1"}


def load_identities():
    global identity_map
    if not os.path.exists(IDENTITY_FILE):
        return
    try:
        with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            identity_map = data
    except Exception as e:
        # Stage H (L3): 保留旧值但记录错误（断电/半写时仍可恢复），不静默吞
        print(f"[feidi] load_identities: {e}", flush=True)


# L5: 5s debounce — 高频连接/重命名场景下, 5s 内的连续 save_identities() 合并为单次写盘,
# 避免每次 SSE handshake (rename / 新身份) 都同步全量 JSON 落盘阻塞 worker 线程。
_SAVE_IDENTITIES_DEBOUNCE = 5.0
_identities_save_timer = None  # type: threading.Timer | None


def _save_identities_flush():
    """实际写盘内部函数; 调度由 save_identities() 负责。"""
    global _identities_save_timer
    _identities_save_timer = None
    tmp = IDENTITY_FILE + ".tmp"
    try:
        with _identity_lock:
            snapshot = json.dumps(identity_map, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, IDENTITY_FILE)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def save_identities():
    """原子写: 写到 .tmp 再 os.replace, 防止断电时 JSON 截断。
    Stage G (H1): 持 _identity_lock 拍快照, 避免与 SSE handshake 写竞态。
    Stage I (L5): 5s debounce — 多次连续调用合并为单次写盘, 不阻塞 SSE handshake 线程。"""
    global _identities_save_timer
    if _identities_save_timer is not None:
        _identities_save_timer.cancel()
    _identities_save_timer = threading.Timer(_SAVE_IDENTITIES_DEBOUNCE, _save_identities_flush)
    _identities_save_timer.daemon = True
    _identities_save_timer.start()


def _hash_mac(mac: str) -> str:
    """M-5: 持久化前对 MAC 哈希，避免明文存储。"""
    if not mac:
        return ""
    salt = b"feidi-mac-v1"
    return hashlib.sha256(salt + mac.upper().encode("utf-8")).hexdigest()[:16]


# Stage G (M5): get_mac 结果缓存，避免每次 SSE 重连都跑 arp 子进程
_mac_cache: dict = {}  # ip -> (raw_mac_or_None, expire_ts)
_MAC_CACHE_TTL = 300  # 5 分钟


def get_mac(ip):
    """尝试通过 arp 表获取指定 IP 的 MAC 地址（原始值，调用方负责哈希）。
    Stage G (M5): 加 5 分钟缓存，避免反复 SSE 重连跑 subprocess。"""
    if ip in ("127.0.0.1", "::1"):
        return None
    now = time.time()
    cached = _mac_cache.get(ip)
    if cached is not None and cached[1] > now:
        return cached[0]  # 可能缓存 None（探测失败），避免重复探
    result = None
    try:
        proc = subprocess.run(["arp", "-a", ip], capture_output=True, text=True, timeout=3)
        match = re.search(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", proc.stdout)
        if match:
            result = match.group(0).replace("-", ":").upper()
    except Exception:
        pass
    _mac_cache[ip] = (result, now + _MAC_CACHE_TTL)
    return result


load_identities()


def cleanup():
    """退出时清理临时文件"""
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    _cleanup_stale_chunks(force=True)


def _ensure_chunk_dir():
    os.makedirs(CHUNK_DIR, exist_ok=True)


def _save_chunk_state(transfer_id):
    """Stage F (F6): 把 chunk_transfers 状态原子写到 feidi_chunks/<id>.state.json，
    供下次启动恢复，浏览器刷新后可断点续传。"""
    with _chunk_lock:
        ct = chunk_transfers.get(transfer_id)
        if not ct:
            return
        snapshot = {
            "chunks": sorted(ct["chunks"]),
            "total": ct["total"],
            "bytes_received": ct.get("bytes_received", 0),
            "info": ct.get("info", {}),
            "sender": ct.get("sender", ""),
            "device_name": ct.get("device_name", ""),
            "device_id": ct.get("device_id", ""),
            "target_id": ct.get("target_id"),
            "is_image": ct.get("is_image", False),
            "last_activity": ct.get("last_activity", time.time()),
        }
    path = os.path.join(CHUNK_DIR, transfer_id + ".state.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        pass


def _load_chunk_states():
    """启动时扫描 ``feidi_chunks/*.state.json`` 恢复 in-flight 传输。

    C-01 修复：函数内同时读+写模块级 ``_inflight_bytes``，必须声明 global
    否则 Python 视为新局部变量，第一次访问就抛 UnboundLocalError。

    C-02 修复：加载前先校验磁盘上每个声明 chunk_index 都有对应 ``.chunk``
    文件。任一缺失即视为不可恢复：删除 entry、目录和 state，避免"内存说有
    / 磁盘没有"的伪恢复。返回实际保留（state 与磁盘一致）的 tids。
    """
    global _inflight_bytes
    if not os.path.isdir(CHUNK_DIR):
        return []
    now = time.time()
    seven_days = 7 * 86400
    valid_tids: list[str] = []
    for name in os.listdir(CHUNK_DIR):
        if not name.endswith(".state.json"):
            continue
        path = os.path.join(CHUNK_DIR, name)
        try:
            mtime = os.path.getmtime(path)
            if now - mtime > seven_days:
                os.remove(path)
                continue
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            tid = name[: -len(".state.json")]
            # 校验必需字段
            chunks = set(state.get("chunks", []))
            total = int(state.get("total", 0))
            if not chunks or total <= 0:
                # 空 state：仅删 state 文件，目录留给 _startup_cleanup
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            # C-02 校验：所有声明的 chunk 文件都必须存在于磁盘上。
            # 任何缺失都视为不可恢复；清掉 state + 整个 transfer 目录，
            # 避免 _startup_cleanup 后内存中残留"已收到"的虚假状态。
            transfer_dir = os.path.join(CHUNK_DIR, tid)
            missing = [
                i for i in chunks
                if not os.path.isfile(os.path.join(transfer_dir, f"{i}.chunk"))
            ]
            if missing:
                shutil.rmtree(transfer_dir, ignore_errors=True)
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            with _chunk_lock:
                chunk_transfers[tid] = {
                    "chunks": chunks,
                    "total": total,
                    "info": state.get("info", {}),
                    "bytes_received": int(state.get("bytes_received", 0)),
                    "sender": state.get("sender", "unknown"),
                    "device_name": state.get("device_name", ""),
                    "device_id": state.get("device_id", ""),
                    "target_id": state.get("target_id"),
                    "is_image": bool(state.get("is_image", False)),
                    "last_activity": float(state.get("last_activity", now)),
                    "created": float(state.get("last_activity", now)),
                    "assembling": False,
                }
                # 计入 in-flight 配额
                with _inflight_lock:
                    _inflight_bytes += chunk_transfers[tid]["bytes_received"]
            valid_tids.append(tid)
        except (OSError, ValueError, json.JSONDecodeError):
            # 损坏的状态文件直接删
            try:
                os.remove(path)
            except OSError:
                pass
    if valid_tids:
        print(f"[feidi] recovered {len(valid_tids)} chunk transfer(s) from disk", flush=True)
    return valid_tids


def _cleanup_stale_chunks(force=False):
    """清理过期分块（默认超过 STALE_CHUNK_TIMEOUT 秒未活动的清理）。force=True 时不发通知（用于退出清理）。
    Stage F (F2): 持 _chunk_lock，基于 last_activity 而非 created。
    Stage F (F3): 清理时同步扣减全局 in-flight 字节。

    C-01 修复：见 ``_load_chunk_states`` 顶部说明。
    """
    global _inflight_bytes
    now = time.time()
    timeout = 0 if force else STALE_CHUNK_TIMEOUT
    dead = []
    with _chunk_lock:
        for tid, ct in list(chunk_transfers.items()):
            last_act = ct.get("last_activity", ct.get("created", now))
            if force or (now - last_act) > timeout:
                dead.append((tid, ct))
        for tid, ct in dead:
            chunk_transfers.pop(tid, None)
            completed_transfers.pop(tid, None)
            # Stage F (F3): 同步扣减 in-flight
            with _inflight_lock:
                _inflight_bytes = max(0, _inflight_bytes - ct.get("bytes_received", 0))
    for tid, ct in dead:
        cp = os.path.join(CHUNK_DIR, tid)
        # C-02 修复：即使 force=True（atexit 路径），只要磁盘上还存在
        # 对应的 ``<tid>.state.json``，就保留 ``.chunk`` 文件供下次启动
        # 通过 ``_load_chunk_states`` 校验并接续。无 state 的孤儿目录照删。
        if os.path.isdir(cp):
            if force and os.path.isfile(cp + ".state.json"):
                pass
            else:
                shutil.rmtree(cp, ignore_errors=True)
        # 超时通知发送者和接收者
        if not force:
            fname = ct.get("info", {}).get("name", "未知文件") if isinstance(ct.get("info"), dict) else "未知文件"
            fail_msg = json.dumps({
                "transfer_id": tid, "filename": fname,
                "error": "传输超时，对方可能已断开连接"
            }, ensure_ascii=False)
            broadcast_sse("transfer_timeout", fail_msg, target_id=ct.get("device_id", ""))
            if ct.get("target_id"):
                broadcast_sse("transfer_timeout", fail_msg, target_id=ct["target_id"])


atexit.register(cleanup)


def _cleanup_old_temp_files():
    """清理超过 1 小时的临时文件（图片、文件消息的二进制数据）"""
    now = time.time()
    cutoff = now - 3600
    try:
        for fname in os.listdir(TEMP_DIR):
            fp = os.path.join(TEMP_DIR, fname)
            try:
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    os.remove(fp)
            except OSError:
                pass
    except Exception:
        pass


def _startup_cleanup():
    """启动时清孤儿（无对应 ``.state.json`` 的残留 transfer 目录）。

    C-02 修复：跳过有同名 ``<tid>.state.json`` 的目录——它们由
    ``_load_chunk_states`` 校验后保留；只有真正孤儿才删。``state.json``
    文件本身不被这里删除，留给 ``_load_chunk_states`` 决定保留或丢弃。
    """
    if not os.path.isdir(CHUNK_DIR):
        return
    for d in os.listdir(CHUNK_DIR):
        if d.endswith(".state.json"):
            continue
        if os.path.isfile(os.path.join(CHUNK_DIR, d + ".state.json")):
            # 有 state 文件陪着 → 留给 _load_chunk_states 决定
            continue
        dp = os.path.join(CHUNK_DIR, d)
        if os.path.isdir(dp):
            try:
                shutil.rmtree(dp, ignore_errors=True)
            except OSError:
                pass


def _periodic_cleanup_loop():
    """定期清理线程：每 CLEANUP_INTERVAL 秒清理过期的临时文件和分块；收到停止信号即退出。"""
    while not _server_stop_event.is_set():
        # wait_for 返回 True 即收到停止信号，提前退出
        if _server_stop_event.wait(timeout=CLEANUP_INTERVAL):
            break
        _cleanup_old_temp_files()
        _cleanup_stale_chunks()


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

    # S-10：默认 bind 候选仅限私网/RFC1918 地址，排除公网地址，
    # 避免路由器/双网卡环境把服务默认暴露到公网（默认无密码）。
    private_candidates = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            try:
                if ipaddress.ip_address(ip).is_private:
                    private_candidates.append(ip)
            except ValueError:
                pass
    except Exception:
        pass

    # 如果没有找到，尝试通过创建 UDP socket 来探测（不实际发包）
    if not private_candidates:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.1.1", 1))
            probe = s.getsockname()[0]
            s.close()
            # S-10：探测结果同样只接受私网地址，公网探测结果绝不作为默认监听地址
            if probe and not probe.startswith("127."):
                try:
                    if ipaddress.ip_address(probe).is_private:
                        private_candidates.append(probe)
                except ValueError:
                    pass
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

    if private_candidates:
        private_candidates.sort(key=ip_priority)
        LOCAL_IP = private_candidates[0]
    else:
        # 无私网地址：回退 loopback 并警告，绝不默认监听公网
        print("  \033[93m警告:\033[0m 未检测到局域网/私网地址，服务仅监听 127.0.0.1（如需局域网访问请用 --bind 指定地址）", flush=True)
        LOCAL_IP = "127.0.0.1"
    return LOCAL_IP


# H-3: 文件级引用计数。_file_refs[msg_id] = (ref_count, entry_paths)。
# entry_paths 在第一次 acquire 时从 MSG_FILES 快照，淘汰可能摘 MSG_FILES 但不会
# 影响 entry_paths。_release_file 在 ref==0 时按 entry_paths 真删盘。
# 所有读写都在 _file_ref_lock 下。
def _acquire_file(msg_id):
    """下载入口：ref+1，返回 entry 路径元组。已被淘汰/未注册返回 None。
       M-1: 直接返回快照的 entry，handler 无需再读 MSG_FILES，关闭 TOCTOU 窗口。"""
    with _file_ref_lock:
        if msg_id not in MSG_FILES:
            return None
        entry = MSG_FILES[msg_id]
        cur = _file_refs.get(msg_id)
        if cur is None:
            _file_refs[msg_id] = (1, entry)
        else:
            _file_refs[msg_id] = (cur[0] + 1, cur[1])
        return entry


def _release_file(msg_id):
    """下载出口：ref-1。归零时按记录的 entry 真删盘。"""
    with _file_ref_lock:
        cur = _file_refs.get(msg_id)
        if cur is None:
            return  # 二次 release 安全
        refs, entry = cur
        refs -= 1
        if refs > 0:
            _file_refs[msg_id] = (refs, entry)
            return
        # refs == 0 → 真删盘
        _file_refs.pop(msg_id, None)
        # Stage D (C2): 与 _cleanup_msg_files 对称 — 下载结束后同步摘 MSG_FILES，
        # 否则后续 _acquire_file 仍会返回这个 msg_id 但路径已删，导致 404 之前打开 0 字节。
        MSG_FILES.pop(msg_id, None)
        if entry:
            for p in entry:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError as e:
                    # M-2: best-effort 但不静默——磁盘满/权限漂移需要能看见
                    print(f"[feidi] _release_file: cleanup failed: {p}: {e}", flush=True)


def _cleanup_msg_files(msg_id):
    """淘汰时只在无 in-flight 引用时才删盘；有引用时仅摘 MSG_FILES（拒绝新下载），
       盘留给 _release_file。与 _acquire_file / _release_file 共享同一把锁。"""
    with _file_ref_lock:
        cur = _file_refs.get(msg_id)
        if cur is not None and cur[0] > 0:
            # 仍有下载在进行；只摘 MSG_FILES 索引（new acquire → 404），盘留给 _release_file
            MSG_FILES.pop(msg_id, None)
            return
        # 无引用，直接摘 + 删
        _file_refs.pop(msg_id, None)
        entry = MSG_FILES.pop(msg_id, None)
        if entry:
            for p in entry:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError as e:
                    # M-2: best-effort 但不静默——磁盘满/权限漂移需要能看见
                    print(f"[feidi] _cleanup_msg_files: cleanup failed: {p}: {e}", flush=True)


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
        # 定期清理超过 1 小时未活跃的 IP 条目
        stale = [ip for ip, ts_list in _rate_limits.items() if not ts_list]
        for ip in stale:
            del _rate_limits[ip]
    return True


def _is_device_online(device_id):
    """Stage C：检查 SSE 客户端是否在线（私有 chat 离线目标前置检查，避免 ghost message 残留）。"""
    if not device_id:
        return False
    with _sse_lock:
        for c in sse_clients:
            if c.get("device_id") == device_id:
                return True
    return False


# Stage i18n: 探测 URL ?lang= 参数，返回 "zh" / "en"（默认 zh）。客户端
# 用 localStorage 在 JS 端覆盖。服务器只设 <html lang=""> 属性 + data-server-lang。
_LANG_DEFAULT = "zh"


def _pick_lang(query_string=""):
    import re as _re
    if query_string:
        m = _re.search(r"(?:^|[&;])lang=([a-zA-Z-]+)", query_string)
        if m:
            v = m.group(1).lower().split("-")[0]
            if v in ("zh", "en"):
                return v
    return _LANG_DEFAULT


def _check_session_token(token):
    """在 sse_clients 中按 token 反查 device_id。"""
    if not token:
        return None
    with _sse_lock:
        for c in sse_clients:
            if c.get("session_token") == token:
                return c.get("device_id")
    return None


def _session_identity(token):
    """通过 session token 反查 (device_id, name, type)；供 /send 等端点
    把发送者身份唯一来源限制到 SSE registry，避免 body 伪造（S-01）。

    返回 dict 或 None（无效/过期 token）。调用方须在持有锁的状态下使用
    返回值之前再校验一次，但 identity 字段不会再变。"""
    if not token:
        return None
    with _sse_lock:
        for c in sse_clients:
            if c.get("session_token") == token:
                return {
                    "device_id": c.get("device_id", ""),
                    "name": c.get("name", ""),
                    "type": c.get("type", "unknown"),
                    "identity_key": c.get("identity_key", ""),
                }
    return None


def _history_for_device(device_id):
    """Stage C：私聊历史过滤。返回该设备应看到的历史：
       - 广播消息 (无 target_id) — 所有人可见
       - 以此 device 为私聊目标的 (target_id == device_id)
       - 自己发出的消息 (device_id == self)
       其余私聊历史(发给别人的)严格不返回。
    """
    with _msg_lock:
        return [
            m for m in messages
            if "target_id" not in m
            or m.get("target_id") == device_id
            or m.get("device_id") == device_id
        ]


def add_message(msg_type, data, sender, device_name="", device_id="", target_id=None):
    """添加消息并通知所有 SSE 客户端。target_id 为 None 则广播，否则仅发送给指定设备。发送者始终排除。"""
    msg_id = str(uuid.uuid4())
    msg = {
        "id": msg_id,
        "type": msg_type,
        "sender": sender,
        "sender_name": device_name or sender,
        "device_id": device_id,
        "time": int(time.time() * 1000),
    }
    if target_id:
        msg["target_id"] = target_id
    msg_files = None  # (tuple of paths) or None
    if msg_type == "image":
        if data.startswith("data:"):
            # M4 修复: 用 partition 替代 split, 缺逗号的非法 data URI 不再 ValueError -> 500
            header, sep, b64 = data.partition(",")
            if sep:
                mime = header.split(";")[0][5:]
                img_bin = base64.b64decode(b64)
            else:
                # 缺逗号 (如 "data:image/png;base64" 无 payload), 降级为空 octet-stream
                print(f"[feidi] malformed image data URI (no comma), fallback: {header[:40]!r}", file=sys.stderr, flush=True)
                img_bin = b""
                mime = "application/octet-stream"
        else:
            img_bin = data if isinstance(data, bytes) else data.encode("utf-8")
            mime = "application/octet-stream"
        bin_path = os.path.join(TEMP_DIR, f"img_{msg_id}.bin")
        mime_path = os.path.join(TEMP_DIR, f"img_{msg_id}.mime")
        with open(bin_path, "wb") as f:
            f.write(img_bin)
        with open(mime_path, "w", encoding="utf-8") as f:
            f.write(mime)
        msg_files = (bin_path, mime_path)
        msg["data"] = f"/img/{msg_id}"
    elif msg_type == "file":
        # data 两种形态：
        #   1) {"name","size","mime","data": base64_str} —— 旧路径，POST /send 非分块模式
        #   2) {"name","size","mime","path": "/file/<id>"} —— 分块模式上层已写盘
        #   3) {"name","size","mime","bytes": bytes} —— 内存直传，chunked 组装完成流式落盘
        file_info = data if isinstance(data, dict) else {}
        fname = file_info.get("name", "unknown")
        fsize = file_info.get("size", 0)
        fmime = file_info.get("mime", "application/octet-stream")
        fpath = os.path.join(TEMP_DIR, f"file_{msg_id}.bin")
        fmeta = os.path.join(TEMP_DIR, f"file_{msg_id}.meta.json")
        if "bytes" in file_info and isinstance(file_info["bytes"], (bytes, bytearray)):
            with open(fpath, "wb") as f:
                f.write(file_info["bytes"])
                f.flush()
                os.fsync(f.fileno())
            fsize = len(file_info["bytes"])
        elif "src_path" in file_info and os.path.isfile(file_info["src_path"]):
            # R-06：分块组装完成文件已在磁盘上，直接 move/link 到最终路径，避免整块读内存。
            # 组装阶段已 fsync；move 后再次 fsync 保险（同盘 rename 极快，跨盘回退 copy）。
            try:
                os.replace(file_info["src_path"], fpath)
            except OSError:
                shutil.copyfile(file_info["src_path"], fpath)
            fsize = os.path.getsize(fpath)
            with open(fpath, "rb") as f:
                os.fsync(f.fileno())
        else:
            fb64 = file_info.get("data", "")
            try:
                fbin = base64.b64decode(fb64) if fb64 else b""
            except Exception:
                fbin = b""
            with open(fpath, "wb") as f:
                f.write(fbin)
                f.flush()
                os.fsync(f.fileno())
            fsize = len(fbin)
        # D-05：必须先写完整 JSON 再 flush + fsync（旧实现顺序反了：
        # 先 fsync 空文件、再 json.dump，崩溃窗口下 meta 为空/截断）。
        with open(fmeta, "w", encoding="utf-8") as f:
            json.dump({"name": fname, "size": fsize, "mime": fmime}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        msg_files = (fpath, fmeta)
        msg["data"] = {"name": fname, "size": fsize, "mime": fmime, "path": f"/file/{msg_id}"}
    else:
        msg["data"] = data
    with _msg_lock:
        if msg_files:
            MSG_FILES[msg_id] = msg_files
        messages.append(msg)
        if len(messages) > MAX_MESSAGES:
            old = messages.pop(0)
            _cleanup_msg_files(old["id"])
    target_ok = broadcast_sse("new_message", msg, exclude_device=device_id if not target_id and device_id else None, target_id=target_id)
    # 返回 (msg_id, target_ok): target_ok 为 None 表示广播模式, True/False 表示私聊目标是否找到
    return msg_id, target_ok if target_id else None


def broadcast_sse(event, data, exclude_device=None, target_id=None):
    """向 SSE 客户端广播事件。
    target_id: 仅发送给指定设备（私聊模式），返回 True/False 表示是否找到目标
    exclude_device: 排除指定设备（广播模式，排除发送者自身）
    两者互斥，target_id 优先。返回: 若指定了 target_id 则返回是否找到目标，否则返回 None"""
    dead = []
    found_target = False
    if isinstance(data, (dict, list)):
        json_data = json.dumps(data, ensure_ascii=False)
    elif isinstance(data, str):
        json_data = data
    else:
        json_data = json.dumps(data, ensure_ascii=False)
    with _sse_lock:
        for c in sse_clients:
            cid = c.get("device_id", "")
            if target_id:
                if cid != target_id:
                    continue
                found_target = True
            elif exclude_device and cid == exclude_device:
                continue
            try:
                c["queue"].put_nowait(f"event: {event}\ndata: {json_data}\n\n")
            except Exception:
                dead.append(c)
        for c in dead:
            if c in sse_clients:
                sse_clients.remove(c)
    return found_target if target_id else None


def broadcast_device_list():
    """广播当前连接的设备列表"""
    with _sse_lock:
        devices = [{"id": c["device_id"], "name": c["name"], "type": c["type"], "identity_key": c.get("identity_key", "")} for c in sse_clients]
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


# --- SVG 图标（内联，不依赖任何外部资源） ---
SVG = {
    # 发送 — 纸飞机
    "send": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13"/><path d="M22 2L15 22L11 13L2 9L22 2Z"/></svg>',
    # 图片 — 山水画框
    "image": '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
    # 文件 — 回形针
    "file": '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
    # 暗色模式 — 月牙
    "moon": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
    # 亮色模式 — 太阳
    "sun": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>',
    # 拍照 — 相机
    "camera": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>',
    # 电脑 — 显示器
    "monitor": '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
    # 手机 — 智能手机
    "phone": '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>',
    # 局域网 — 地球
    "globe": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
    # 警告 — 三角叹号
    "warn": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    # 信号 — WiFi 弧
    "signal": '<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><circle cx="12" cy="20" r="1" fill="currentColor" stroke="none"/></svg>',
    # 信封 — 邮件
    "mail": '<svg viewBox="0 0 24 24" width="44" height="44" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>',
    # 加号 — 附件入口
    "plus": '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
    # 音乐 — 音符 (菜单-音频)
    "music": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
    # 视频 — 播放按钮 (菜单-视频)
    "video": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>',
    # 文档 — 文本文件 (菜单-文档)
    "doc": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>',
}

# --- PC 端 HTML（完全离线，QR 码由服务端 SVG 直接嵌入） ---
PC_HTML = r"""<!DOCTYPE html>
<html lang="__LANG__">
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
  .msg{max-width:78%;padding:10px 15px;border-radius:var(--radius-xs);font-size:14px;line-height:1.65;word-break:break-word;animation:msgIn .25s cubic-bezier(.4,0,.2,1);position:relative;background:var(--c-msg-mobile);color:var(--c-text)}
  .msg.pc{align-self:flex-end;background:var(--c-msg-pc);color:#064e3b;border-bottom-right-radius:4px;border:1px solid #a7f3d0}
  [data-theme="dark"] .msg.pc{color:#6ee7b7;border-color:#047857;background:#134e4a}
  .msg.mobile{align-self:flex-start;background:var(--c-msg-mobile);color:var(--c-text);border-bottom-left-radius:4px;border:1px solid var(--c-border)}
  .msg img{max-width:220px;max-height:220px;border-radius:8px;cursor:pointer;display:block;margin-top:6px;transition:transform .15s}
  .msg img:hover{transform:scale(1.02)}
  .msg .meta{font-size:10px;color:var(--c-text3);margin-top:5px;display:flex;align-items:center;gap:4px}
  .msg.pc .meta{justify-content:flex-end}
  @keyframes msgIn{from{opacity:0;transform:translateY(12px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
  .input-area{padding:12px 16px 14px;border-top:1px solid var(--c-border);display:flex;gap:8px;align-items:flex-end;background:var(--c-surface);flex-shrink:0;position:relative}
  .input-area textarea{flex:1;border:1.5px solid var(--c-border);border-radius:var(--radius-xs);padding:10px 14px;font-size:14px;resize:none;outline:none;font-family:inherit;min-height:42px;max-height:120px;background:#f8fafc;color:var(--c-text);transition:border-color .2s,box-shadow .2s,background .2s}
  [data-theme="dark"] .input-area textarea{background:var(--c-surface)}
  .input-area textarea:focus{border-color:var(--c-primary);box-shadow:0 0 0 3px rgba(5,150,105,.12);background:var(--c-surface)}
  .input-area textarea::placeholder{color:var(--c-text3)}
  .input-area .btn-send{width:42px;height:42px;border:none;background:linear-gradient(135deg,var(--c-primary),#10b981);color:#fff;border-radius:50%;cursor:pointer;font-size:17px;flex-shrink:0;transition:all .2s;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(5,150,105,.25)}
  .input-area .btn-send:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(5,150,105,.35)}
  .input-area .btn-send:active{transform:scale(.95)}
  .input-area .btn-attach{width:38px;height:38px;border:1.5px dashed var(--c-border);background:transparent;color:var(--c-text2);border-radius:var(--radius-xs);cursor:pointer;font-size:20px;flex-shrink:0;transition:all .2s;display:flex;align-items:center;justify-content:center;position:relative}
  .input-area .btn-attach:hover,.input-area .btn-attach.active{background:var(--c-primary-light);border-color:var(--c-primary);color:var(--c-primary)}
  /* 附件弹出菜单 */
  .attach-backdrop{position:fixed;top:0;left:0;width:100%;height:100%;z-index:50;display:none}
  .attach-backdrop.show{display:block}
  .attach-menu{position:absolute;bottom:56px;left:14px;background:var(--c-surface);border:1px solid var(--c-border);border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.12);z-index:60;overflow:hidden;display:none;min-width:160px}
  .attach-menu.show{display:block;animation:menuIn .15s ease}
  @keyframes menuIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
  .attach-menu .menu-item{display:flex;align-items:center;gap:10px;padding:10px 16px;cursor:pointer;font-size:13px;color:var(--c-text);transition:background .15s;white-space:nowrap}
  .attach-menu .menu-item:hover{background:var(--c-primary-light);color:var(--c-primary)}
  .attach-menu .menu-item .mi-icon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .attach-menu .menu-item .mi-icon.img{background:#e8f5e9;color:#43a047}
  .attach-menu .menu-item .mi-icon.audio{background:#fff3e0;color:#ef6c00}
  .attach-menu .menu-item .mi-icon.video{background:#fce4ec;color:#e91e63}
  .attach-menu .menu-item .mi-icon.doc{background:#e3f2fd;color:#1e88e5}
  .attach-menu .menu-item .mi-icon.other{background:#f3e5f5;color:#8e24aa}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.img{background:#1b5e20;color:#81c784}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.audio{background:#4e342e;color:#ffb74d}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.video{background:#880e4f;color:#f48fb1}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.doc{background:#0d47a1;color:#90caf9}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.other{background:#4a148c;color:#ce93d8}
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
  .device-item .di-badge.remark{background:#fef3c7;color:#d97706}
  [data-theme="dark"] .device-item .di-badge.remark{background:#78350f;color:#fbbf24}
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
  /* 拖拽上传 */
  .drop-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(59,130,246,.12);z-index:90;display:none;align-items:center;justify-content:center;pointer-events:none;border:3px dashed var(--c-primary);border-radius:12px;margin:8px;box-sizing:border-box}
  .drop-overlay.show{display:flex}
  .drop-overlay .drop-hint{display:flex;align-items:center;gap:8px;background:var(--c-surface);padding:16px 28px;border-radius:16px;box-shadow:var(--shadow-lg);font-size:15px;font-weight:500;color:var(--c-primary)}
  /* 拖拽确认弹窗 */
  .confirm-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:200;display:none;align-items:center;justify-content:center}
  .confirm-overlay.show{display:flex}
  .confirm-dialog{background:var(--c-surface);border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.25);width:420px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
  .confirm-dialog .cd-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--c-border);font-size:15px;font-weight:600;color:var(--c-text)}
  .confirm-dialog .cd-close{width:28px;height:28px;border:none;background:var(--c-border);color:var(--c-text2);border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px}
  .confirm-dialog .cd-body{flex:1;overflow-y:auto;padding:12px 20px;max-height:320px}
  .confirm-dialog .cd-file{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;background:var(--c-border-light);margin-bottom:6px}
  .confirm-dialog .cd-file .cdf-icon{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
  .confirm-dialog .cd-file .cdf-info{flex:1;min-width:0}
  .confirm-dialog .cd-file .cdf-name{font-size:13px;font-weight:500;color:var(--c-text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .confirm-dialog .cd-file .cdf-size{font-size:11px;color:var(--c-text3)}
  .confirm-dialog .cd-footer{display:flex;gap:10px;justify-content:flex-end;padding:14px 20px;border-top:1px solid var(--c-border)}
  .confirm-dialog .cd-footer button{padding:8px 20px;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;transition:all .15s}
  .confirm-dialog .cd-footer .btn-cancel{border:1px solid var(--c-border);background:transparent;color:var(--c-text2)}
  .confirm-dialog .cd-footer .btn-confirm{border:none;background:var(--c-primary);color:#fff}
  .confirm-dialog .cd-footer .btn-confirm:hover{opacity:.9}
  /* 颜色头像 */
  .di-avatar{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;color:#fff;flex-shrink:0;text-transform:uppercase}
  /* 选中设备 */
  .device-item{cursor:pointer}
  .device-item.selected{background:var(--c-primary-light) !important;border-radius:8px}
  .device-item.me{cursor:default}
  /* 内联改名 */
  .di-name.editable{cursor:text;border-bottom:1px dashed transparent;transition:all .15s}
  .di-name.editable:hover{border-bottom-color:var(--c-border)}
  .di-name-input{font-size:13px;font-weight:500;color:var(--c-text);border:none;border-bottom:1.5px solid var(--c-primary);outline:none;padding:0;margin:0;width:100%;background:transparent;font-family:inherit}
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
      <span class="logo-text" id="chatTitle">飞递 Feidi</span>
      <span class="logo-dot"></span>
      <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()" title="切换深色模式">__ICON_MOON__</button>
    </div>
    <div class="status-row">
      <span class="status-dot offline" id="statusDot"></span>
      <span class="status-text" id="statusText">等待手机连接</span>
      <span style="margin-left:auto;font-size:12px;opacity:.6"><a href="#" onclick="window.__setLang('zh');return false" style="color:inherit;text-decoration:none" data-i18n-active="zh">中</a> | <a href="#" onclick="window.__setLang('en');return false" style="color:inherit;text-decoration:none" data-i18n-active="en">EN</a></span>
    </div>
    <div class="messages" id="messages">
      <div class="empty-state" id="emptyState">
        <div class="empty-icon">__ICON_SIGNAL__</div>
        <div>手机扫码后即可开始互传</div>
        <div style="font-size:11px;color:var(--c-text3)">文本、图片实时同步</div>
      </div>
    </div>
    <div class="input-area">
      <input type="file" id="imgInput" accept="image/*" multiple style="display:none">
      <input type="file" id="audioInput" accept="audio/*" multiple style="display:none">
      <input type="file" id="videoInput" accept="video/*" multiple style="display:none">
      <input type="file" id="docInput" accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.wps,.et,.dps,.csv,.rtf,.odt,.ods,.odp,.md" multiple style="display:none">
      <input type="file" id="otherInput" multiple style="display:none">
      <button class="btn-attach" id="btnAttach" onclick="toggleAttachMenu()" title="添加附件">__ICON_PLUS__</button>
      <div class="attach-backdrop" id="attachBackdrop" onclick="toggleAttachMenu()"></div>
      <div class="attach-menu" id="attachMenu">
        <div class="menu-item" onclick="pickFile('img')"><span class="mi-icon img">__ICON_IMAGE__</span>图片</div>
        <div class="menu-item" onclick="pickFile('audio')"><span class="mi-icon audio">__ICON_MUSIC__</span>音频</div>
        <div class="menu-item" onclick="pickFile('video')"><span class="mi-icon video">__ICON_VIDEO__</span>视频</div>
        <div class="menu-item" onclick="pickFile('doc')"><span class="mi-icon doc">__ICON_DOC__</span>文档</div>
        <div class="menu-item" onclick="pickFile('other')"><span class="mi-icon other">__ICON_FILE__</span>其他文件</div>
      </div>
      <textarea id="textInput" rows="1" placeholder="输入消息..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendText()}"></textarea>
      <button class="btn-send" onclick="sendText()" title="发送">__ICON_SEND__</button>
    </div>
    <div class="toast" id="toast"></div>
    <!-- 拖拽上传 -->
    <div class="drop-overlay" id="dropOverlay"><div class="drop-hint">&#x1F4E5; 释放文件即可发送</div></div>
    <div class="confirm-overlay" id="confirmOverlay">
      <div class="confirm-dialog">
        <div class="cd-header"><span>&#x1F4CE; 确认发送文件</span><button class="cd-close" onclick="closeConfirm()">&#x2715;</button></div>
        <div class="cd-body" id="confirmBody"></div>
        <div class="cd-footer">
          <button class="btn-cancel" onclick="closeConfirm()">取消</button>
          <button class="btn-confirm" id="btnConfirmSend" onclick="confirmAndSend()">发送</button>
        </div>
      </div>
    </div>
  </div>
  <div class="qr-panel">
    <div class="device-list" id="deviceList" style="display:none">
      <div class="dl-header">
        <span class="dl-title">__ICON_MONITOR__ 已连接设备</span>
        <span class="dl-count" id="dlCount">0</span>
      </div>
      <div class="dl-body" id="dlBody">
        <div class="device-empty">暂无设备连接</div>
      </div>
    </div>
    <div class="qr-section" id="qrSection">
      <div class="qr-box" id="qrBox">
        <div class="qr-title">__ICON_PHONE__ 手机扫码连接</div>
        <div class="qr-svg-wrapper">__QR_SVG__</div>
        <div class="qr-url">__MOBILE_URL__</div>
      </div>
      <div class="info" id="infoBox">
        <div class="info-item">
          <div class="info-icon green">__ICON_GLOBE__</div>
          <div class="info-text"><b>局域网传输</b>数据不经过外网，安全私密</div>
        </div>
        <div class="info-item">
          <div class="info-icon amber">__ICON_WARN__</div>
          <div class="info-text"><b>扫码提示</b>请用手机相机或浏览器扫码，微信内置浏览器可能打不开</div>
        </div>
      </div>
    </div>
    <button class="qr-toggle-btn" id="qrToggleBtn" onclick="toggleQr()">__ICON_PHONE__ 显示二维码</button>
  </div>
</div>
<script>
// 主题管理
(function(){
  const KEY = "feidi_theme";
  const toggleBtn = document.getElementById("themeToggle");
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    toggleBtn.innerHTML = theme === "dark" ? '__ICON_SUN__' : '__ICON_MOON__';
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
  var MY_SESSION = "";  // Stage E (H2/H10): SSE 握手下发的 per-session bearer
  var MY_NAME = "";
  var MY_TYPE = "pc";
  const SENDER = "pc";
  const msgContainer = document.getElementById("messages");
  let emptyState = document.getElementById("emptyState");
  const textInput = document.getElementById("textInput");
  const imgInput = document.getElementById("imgInput");
  const audioInput = document.getElementById("audioInput");
  const videoInput = document.getElementById("videoInput");
  const docInput = document.getElementById("docInput");
  const otherInput = document.getElementById("otherInput");
  const btnAttach = document.getElementById("btnAttach");
  const attachMenu = document.getElementById("attachMenu");
  const attachBackdrop = document.getElementById("attachBackdrop");
  const statusDot = document.getElementById("statusDot");
  const statusText = document.getElementById("statusText");
  const toastEl = document.getElementById("toast");
  const qrBox = document.getElementById("qrBox");
  const infoBox = document.getElementById("infoBox");
  const qrToggleBtn = document.getElementById("qrToggleBtn");
  const deviceList = document.getElementById("deviceList");
  const dlBody = document.getElementById("dlBody");
  const dlCount = document.getElementById("dlCount");
  var currentDeviceCount = 0;
  var prevDeviceIds = new Set();
  var selectedDevice = null; // 当前私聊目标 device_id，null=广播
  var allMessages = [];     // 所有消息缓存（用于切换会话重建）
  var pendingDropFiles = []; // 拖拽待发送文件

  // 拖拽相关元素
  var dropOverlay = document.getElementById("dropOverlay");
  var confirmOverlay = document.getElementById("confirmOverlay");
  var confirmBody = document.getElementById("confirmBody");
  var btnConfirmSend_ = document.getElementById("btnConfirmSend");

  // --- 身份与名称系统 ---
  var PERSISTENT_ID = "";
  try { PERSISTENT_ID = localStorage.getItem("feidi_pid"); } catch(e) {}
  if (!PERSISTENT_ID) {
    // Stage D (M1): 用 CSPRNG (crypto.getRandomValues) 生成 UUID v4，避免 Math.random()
    // 可被 LAN 攻击者预测/冒名。
    try {
      var _buf = new Uint8Array(16);
      (window.crypto || window.msCrypto).getRandomValues(_buf);
      _buf[6] = (_buf[6] & 0x0f) | 0x40; // version 4
      _buf[8] = (_buf[8] & 0x3f) | 0x80; // variant 1
      var _hex = Array.prototype.map.call(_buf, function(b) {
        return (b < 16 ? "0" : "") + b.toString(16);
      }).join("");
      PERSISTENT_ID = _hex.substr(0,8) + "-" + _hex.substr(8,4) + "-" + _hex.substr(12,4)
        + "-" + _hex.substr(16,4) + "-" + _hex.substr(20,12);
    } catch(_e) {
      // 极老浏览器降级到 crypto.randomUUID / Math.random（已知不可靠但能跑）
      if (window.crypto && window.crypto.randomUUID) {
        PERSISTENT_ID = window.crypto.randomUUID();
      } else {
        PERSISTENT_ID = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
          var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        });
      }
    }
    try { localStorage.setItem("feidi_pid", PERSISTENT_ID); } catch(e) {}
  }
  var MY_HOSTNAME = "电脑";
  try {
    var ua = navigator.userAgent;
    if (/Windows/.test(ua)) MY_HOSTNAME = "Windows";
    else if (/Mac/.test(ua)) MY_HOSTNAME = "Mac";
    else if (/Linux/.test(ua)) MY_HOSTNAME = "Linux";
  } catch(e) {}
  var MY_DISPLAY_NAME = "";
  try { MY_DISPLAY_NAME = localStorage.getItem("feidi_myname") || ""; } catch(e) {}
  var remarks = {}; // 我给其他设备的备注
  try { remarks = JSON.parse(localStorage.getItem("feidi_remarks") || "{}"); } catch(e) {}

  function getDisplayName(device) {
    if (remarks[device.id]) return remarks[device.id];
    return device.name || device.type;
  }
  function saveRemarks() {
    try { localStorage.setItem("feidi_remarks", JSON.stringify(remarks)); } catch(e) {}
  }
  function saveMyName(name) {
    MY_DISPLAY_NAME = name;
    try { localStorage.setItem("feidi_myname", name); } catch(e) {}
  }

  function getAvatarColor(name) {
    var hash = 0;
    for (var i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
    var h = ((hash % 360) + 360) % 360;
    return "hsl(" + h + ", 55%, 48%)";
  }
  function getAvatarLetter(name) { return (name || "?")[0].toUpperCase(); }

  function showToast(msg, isError) {
    toastEl.textContent = msg;
    toastEl.style.background = isError ? "rgba(211,47,47,.9)" : "rgba(0,0,0,.75)";
    toastEl.className = "toast show";
    setTimeout(function() { toastEl.className = "toast"; }, 2500);
  }

  // 新消息通知（非当前会话）
  var _notifyGranted = false;
  if ("Notification" in window && Notification.permission === "granted") _notifyGranted = true;
  function requestNotifyPermission() {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().then(function(p) { if (p === "granted") _notifyGranted = true; });
    }
  }
  var _blinkTimer = null, _blinkOrigTitle = "";
  function notifyMessage(msg) {
    var senderName = msg.sender_name || getDisplayName({id: msg.device_id, name: msg.device_id, type: "pc"});
    var preview = "";
    if (msg.type === "text") preview = msg.data || "";
    else if (msg.type === "image") preview = "[图片]";
    else if (msg.type === "file") preview = "[文件] " + (msg.data && msg.data.name || "");
    if (preview.length > 40) preview = preview.substring(0, 40) + "...";
    // 仅后台标签页发送系统通知
    if (_notifyGranted && document.hidden) {
      var avatarChar = (senderName[0] || "?").toUpperCase();
      try { new Notification(senderName + " 发来消息", {body: preview, icon: "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><circle cx="16" cy="16" r="14" fill="#3b82f6"/><text x="16" y="22" text-anchor="middle" fill="white" font-size="16" font-weight="bold">' + avatarChar + '</text></svg>')}); } catch(e) {}
    } else if (document.hidden) {
      showToast(senderName + ": " + (preview || "新消息"));
    }
    // 标题闪烁（去抖：新消息重置闪烁计时）
    if (_blinkTimer) clearInterval(_blinkTimer);
    _blinkOrigTitle = document.title;
    var blinkCount = 0;
    _blinkTimer = setInterval(function() {
      blinkCount++;
      document.title = (blinkCount % 2 === 0) ? _blinkOrigTitle : "\uD83D\uDD14 " + senderName + " \u00B7 " + _blinkOrigTitle;
      if (blinkCount >= 6) { clearInterval(_blinkTimer); _blinkTimer = null; document.title = _blinkOrigTitle; }
    }, 800);
  }

  function updateStatus(count) {
    currentDeviceCount = count;
    if (count > 0) {
      statusDot.className = "status-dot online";
      statusText.textContent = "已连接 (" + count + " 台设备)";
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
      qrToggleBtn.innerHTML = '__ICON_PHONE__ 收起二维码';
    } else {
      qrBox.classList.add("collapsed");
      if (infoBox) infoBox.style.display = "none";
      qrToggleBtn.innerHTML = '__ICON_PHONE__ 显示二维码';
    }
  };

  var chatTitle = document.getElementById("chatTitle");

  function updateChatTitle() {
    if (!chatTitle) return;
    if (selectedDevice) {
      var name = selectedDevice;
      for (var i = 0; i < (sse_clients_cache || []).length; i++) {
        if (sse_clients_cache[i].id === selectedDevice) { name = getDisplayName(sse_clients_cache[i]); break; }
      }
      chatTitle.textContent = name;
    } else {
      chatTitle.textContent = "飞递 Feidi";
    }
  }

  function switchConversation(deviceId) {
    if (deviceId === selectedDevice) return; // 同一个，不切换
    selectedDevice = deviceId;
    updateChatTitle();
    // 高亮选中设备
    var items = document.querySelectorAll(".device-item");
    items.forEach(function(el) { el.classList.remove("selected"); });
    if (deviceId) {
      var sel = document.getElementById("dev-" + deviceId);
      if (sel) sel.classList.add("selected");
    }
    // 重建消息列表
    rebuildMessages();
  }

  var sse_clients_cache = [];

  function rebuildMessages() {
    var list = msgContainer.querySelectorAll(".msg");
    list.forEach(function(el) { el.remove(); });
    var empty = document.getElementById("emptyState");
    if (empty && empty.parentNode) empty.parentNode.removeChild(empty);
    allMessages.forEach(function(m) {
      if (selectedDevice) {
        var fromSelected = m.device_id === selectedDevice;
        var toSelected = m.target_id === selectedDevice;
        if (!fromSelected && !((m.device_id === MY_ID) && toSelected)) return;
      }
      appendMessage(m, false);
    });
    if (!msgContainer.querySelector(".msg")) {
      if (emptyState) msgContainer.appendChild(emptyState);
    }
  }

  // 设备列表渲染
  function renderDeviceList(devices) {
    // 缓存设备列表供其他函数使用
    sse_clients_cache = devices;
    if (!deviceList || !dlBody) return;
    var count = devices.length;
    dlCount.textContent = count;
    if (count === 0) {
      deviceList.style.display = "none";
      updateStatus(0);
      prevDeviceIds = new Set();
      return;
    }
    deviceList.style.display = "block";
    var otherCount = 0;
    var html = "";
    // 如果选中设备已断开，自动回到广播
    if (selectedDevice) {
      var stillHere = false;
      devices.forEach(function(d) { if (d.id === selectedDevice) stillHere = true; });
      if (!stillHere) switchConversation(null);
    }
    // 检测新设备（跳过首次加载）
    var newDevices = [];
    if (prevDeviceIds.size > 0) {
      devices.forEach(function(d) {
        if (d.id !== MY_ID && !prevDeviceIds.has(d.id)) {
          newDevices.push(d);
        }
      });
    }
    devices.forEach(function(d) {
      var isMe = d.id === MY_ID;
      if (!isMe) otherCount++;
      var displayName = getDisplayName(d);
      var isRemark = !!remarks[d.id];
      var avatarColor = getAvatarColor(displayName);
      var avatarLetter = getAvatarLetter(displayName);
      var selCls = (d.id === selectedDevice) ? " selected" : "";
      var meCls = isMe ? " me" : "";
      var remarkTag = isRemark ? ' <span class="di-badge remark">备注</span>' : '';
      html += '<div class="device-item' + selCls + meCls + '" id="dev-' + d.id + '" data-device-id="' + d.id + '">' +
        '<div class="di-avatar" style="background:' + avatarColor + '">' + avatarLetter + '</div>' +
        '<div class="di-info"><div class="di-name' + (isMe ? " editable" : " editable") + '"' + ' onclick="event.stopPropagation();startRename(\'' + d.id + '\')"' + '>' + escHtml(displayName) + (isMe ? ' <span class="di-badge me">本机</span>' : '') + remarkTag + '</div>' +
        '<div class="di-type">' + (d.type === "mobile" ? "手机" : "电脑") + (isRemark ? ' — ' + escHtml(d.name || d.type) : '') + '</div></div>' +
        '<div class="di-status"></div>' +
        '</div>';
    });
    // 显示新设备连接提示
    newDevices.forEach(function(d) {
      showToast(d.name + " 已连接");
    });
    // 有新设备连入时才折叠二维码，给 3 秒让用户看到提示
    if (newDevices.length > 0) {
      setTimeout(function() {
        if (qrBox) { qrBox.classList.add("collapsed"); }
        if (infoBox) { infoBox.style.display = "none"; }
      }, 3000);
    }
    // 更新 prevDeviceIds
    prevDeviceIds = new Set();
    devices.forEach(function(d) { prevDeviceIds.add(d.id); });
    dlBody.innerHTML = html;
    // 设备点击：切换会话（非本机）
    dlBody.querySelectorAll(".device-item").forEach(function(el) {
      el.addEventListener("click", function() {
        var did = el.getAttribute("data-device-id");
        if (did === MY_ID) return; // 不能跟自己私聊
        switchConversation(did === selectedDevice ? null : did);
      });
    });
    updateStatus(otherCount);
  }

  window.startRename = function(deviceId) {
    var nameEl = document.querySelector("#dev-" + deviceId + " .di-name");
    if (!nameEl) return;
    var isMe = deviceId === MY_ID;
    var oldName = isMe ? (MY_DISPLAY_NAME || MY_HOSTNAME) : (remarks[deviceId] || "");
    var placeholder = isMe ? "给自己起个名字" : "添加备注（仅自己可见）";

    var input = document.createElement("input");
    input.className = "di-name-input";
    input.value = oldName;
    input.placeholder = placeholder;
    input.setAttribute("data-device-id", deviceId);

    var save = function() {
      var newName = input.value.trim().slice(0, 20);
      if (isMe) {
        // 改自己的对外名称
        if (newName && newName !== MY_DISPLAY_NAME) {
          saveMyName(newName);
          MY_NAME = newName;
          // 同步到服务端，所有人可见
          fetch("/rename?id=" + encodeURIComponent(MY_ID) + "&name=" + encodeURIComponent(newName), {headers: {"X-Feidi-Session": MY_SESSION}})
            .then(function(r) { return r.json(); })
            .then(function(d) {
              if (d.name) {
                for (var i = 0; i < (sse_clients_cache || []).length; i++) {
                  if (sse_clients_cache[i].id === MY_ID) { sse_clients_cache[i].name = d.name; break; }
                }
                updateChatTitle();
              }
            }).catch(function(){});
        }
      } else {
        // 给别人设备注（纯本地）
        if (newName) {
          remarks[deviceId] = newName;
        } else {
          delete remarks[deviceId];
        }
        saveRemarks();
        // 重建设备列表以显示备注
        if (sse_clients_cache) renderDeviceList(sse_clients_cache);
      }
      nameEl.style.display = "";
      input.remove();
    };
    input.addEventListener("blur", save);
    input.addEventListener("keydown", function(e) {
      if (e.key === "Enter") { input.blur(); }
      if (e.key === "Escape") { input.value = oldName; input.blur(); }
    });
    nameEl.style.display = "none";
    nameEl.parentNode.insertBefore(input, nameEl);
    input.focus();
    input.select();
  };

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

  // SSE — 带身份参数
  var seenMsgs = new Set();
  var evtSource = new EventSource("/events?type=" + MY_TYPE + "&pid=" + encodeURIComponent(PERSISTENT_ID) + "&name=" + encodeURIComponent(MY_HOSTNAME) + (MY_DISPLAY_NAME ? "&my_name=" + encodeURIComponent(MY_DISPLAY_NAME) : ""));

  // 密码保护开启时：SSE 被 403 关闭，弹登录框（对齐 MOBILE_HTML L1965-1974 行为）
  evtSource.onerror = function(e) {
    if (evtSource.readyState === EventSource.CLOSED) {
      fetch("/status").then(function(r) {
        if (r.status === 403) {
          var ov = document.getElementById("loginOverlay");
          if (ov) ov.style.display = "flex";
        }
      }).catch(function(){});
    }
  };

  evtSource.addEventListener("device_id", function(e) {
    var data = JSON.parse(e.data);
    MY_ID = data.device_id;
    MY_SESSION = data.session_token || "";  // Stage E
    MY_NAME = data.name;
    MY_TYPE = data.type;
    // 默认私聊：MY_ID 就绪后补选第一台非本机设备
    if (!selectedDevice && sse_clients_cache && sse_clients_cache.length > 0) {
      var others = sse_clients_cache.filter(function(d) { return d.id !== MY_ID; });
      if (others.length > 0) switchConversation(others[0].id);
    }
    // 如果有本地自命名，同步到服务端
    if (MY_DISPLAY_NAME && MY_DISPLAY_NAME !== data.name) {
      fetch("/rename?id=" + encodeURIComponent(MY_ID) + "&name=" + encodeURIComponent(MY_DISPLAY_NAME), {headers: {"X-Feidi-Session": MY_SESSION}});
    }
  });

  evtSource.addEventListener("history", function(e) {
    var msgs = JSON.parse(e.data);
    msgs.forEach(function(m) {
      if (!seenMsgs.has(m.id)) { seenMsgs.add(m.id); allMessages.push(m); appendMessage(m, false); }
    });
  });
  evtSource.addEventListener("new_message", function(e) {
    var msg = JSON.parse(e.data);
    if (seenMsgs.has(msg.id)) return;
    seenMsgs.add(msg.id);
    if (seenMsgs.size > 500) { seenMsgs.clear(); }
    allMessages.push(msg);
    // 通知：来自非当前会话且非自己的消息
    if (msg.device_id !== MY_ID && msg.device_id !== selectedDevice) {
      notifyMessage(msg);
    }
    // 私聊模式过滤：只显示与选中设备相关的消息
    if (selectedDevice) {
      var fromSelected = msg.device_id === selectedDevice;
      var toSelected = msg.target_id === selectedDevice;
      if (!fromSelected && !((msg.device_id === MY_ID) && toSelected)) return;
    }
    appendMessage(msg, true);
  });
  evtSource.addEventListener("transfer_timeout", function(e) {
    var info = JSON.parse(e.data);
    showToast("\u26A0\uFE0F 文件传输失败: " + (info.filename || "未知文件") + " — " + (info.error || "超时"));
  });
  evtSource.addEventListener("device_list", function(e) {
    var data = JSON.parse(e.data);
    renderDeviceList(data.devices || []);
    // 默认私聊模式：仅在 MY_ID 已知后才自动选设备
    if (!selectedDevice && MY_ID) {
      var others = (data.devices || []).filter(function(d) { return d.id !== MY_ID; });
      if (others.length > 0) switchConversation(others[0].id);
    }
  });
  evtSource.onopen = function() {
    fetch("/status").then(function(r) { return r.json(); }).then(function(d) {
      updateStatus(d.connections);
    }).catch(function(){});
  };

  function appendMessage(msg, animate) {
    // 去重存储到 allMessages
    var dup = false;
    for (var ai = 0; ai < allMessages.length; ai++) {
      if (allMessages[ai].id === msg.id) { dup = true; break; }
    }
    if (!dup) allMessages.push(msg);

    if (emptyState) { emptyState.remove(); emptyState = null; }
    var isMe = (msg.device_id && msg.device_id === MY_ID) || msg.sender === SENDER;
    var div = document.createElement("div");
    div.className = "msg " + msg.sender;
    if (msg.type === "text") {
      div.textContent = msg.data;
    } else if (msg.type === "image") {
      var img = document.createElement("img");
      img.src = msg.data;
      // M-6: 取消 window.open 防止 SVG/HTML 走新 tab 同源执行；改为下载原图
      var dl = document.createElement("a");
      dl.href = msg.data;
      dl.download = "image";
      dl.appendChild(img);
      div.appendChild(dl);
    } else if (msg.type === "file" && msg.data) {
      var fd = msg.data;
      var card = document.createElement("div");
      card.className = "file-card";
      // M-6: window.open 改为 <a download> 触发下载，避免新 tab 同源渲染任意 Content-Type
      var a = document.createElement("a");
      a.href = fd.path;
      a.download = fd.name || "download";
      a.appendChild(card);
      // 给内部元素保留 click（点击下载）
      var inner = document.createElement("div");
      inner.style.cursor = "pointer";
      inner.onclick = function() { a.click(); };
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
    requestNotifyPermission();
    if (currentDeviceCount === 0) {
      showToast("暂无设备连接，无法发送", true);
      return;
    }
    // M-2: 锁定发送时的私聊目标，避免 POST 与 localMsg 不一致
    var textTarget = selectedDevice || null;
    fetch("/send", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
      body: JSON.stringify({text: text, sender: SENDER, device_name: MY_NAME, device_id: MY_ID, target_id: textTarget})
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (!data.ok) throw new Error("发送失败");
      // 本地立即显示（因 exclude_device 不会通过 SSE 回传）
      var localMsg = {id: data.msg_id, type: "text", data: text, sender: SENDER, sender_name: "我", device_id: MY_ID, target_id: textTarget, time: Date.now()};
      seenMsgs.add(localMsg.id);
      appendMessage(localMsg, true);
    }).catch(function() {
      showToast("发送失败，请检查连接", true);
    });
    textInput.value = "";
    textInput.style.height = "";
  };

  // --- 附件菜单 ---
  window.toggleAttachMenu = function() {
    var show = !attachMenu.classList.contains("show");
    attachMenu.classList.toggle("show", show);
    attachBackdrop.classList.toggle("show", show);
    btnAttach.classList.toggle("active", show);
  };
  window.pickFile = function(type) {
    var inputMap = {img: imgInput, audio: audioInput, video: videoInput, doc: docInput, other: otherInput};
    var input = inputMap[type];
    if (input) input.click();
    // 关闭菜单
    attachMenu.classList.remove("show");
    attachBackdrop.classList.remove("show");
    btnAttach.classList.remove("active");
  };
  // 点击菜单外关闭（backdrop 已处理 onclick=toggleAttachMenu）

  // --- 拖拽文件发送 ---
  var dragCounter = 0;
  document.addEventListener("dragenter", function(e) {
    e.preventDefault(); dragCounter++;
    if (dragCounter === 1) dropOverlay.classList.add("show");
  });
  document.addEventListener("dragleave", function(e) {
    e.preventDefault(); dragCounter--;
    if (dragCounter <= 0) { dragCounter = 0; dropOverlay.classList.remove("show"); }
  });
  document.addEventListener("dragover", function(e) { e.preventDefault(); });
  document.addEventListener("drop", function(e) {
    e.preventDefault();
    dragCounter = 0;
    dropOverlay.classList.remove("show");
    var files = e.dataTransfer.files;
    if (!files || files.length === 0) return;
    if (currentDeviceCount === 0) {
      showToast("暂无设备连接，无法发送", true);
      return;
    }
    pendingDropFiles = [];
    for (var i = 0; i < files.length; i++) pendingDropFiles.push(files[i]);
    showConfirmDialog(pendingDropFiles);
  });

  // 粘贴图片发送 (Ctrl+V)
  document.addEventListener("paste", function(e) {
    var items = (e.clipboardData || window.clipboardData).items;
    if (!items) return;
    for (var i = 0; i < items.length; i++) {
      if (items[i].type.indexOf("image") !== 0) continue;
      e.preventDefault();
      if (currentDeviceCount === 0) { showToast("暂无设备连接，无法发送", true); return; }
      var blob = items[i].getAsFile();
      var reader = new FileReader();
      reader.onload = function(ev) {
        var dataUri = ev.target.result;
        if (dataUri.length > 5 * 1024 * 1024) {
          // 大图片走分块传输
          var fakeFile = new File([blob], "clipboard_" + Date.now() + ".png", {type: blob.type});
          sendFileChunked(fakeFile, true);
        } else {
          fetch("/send", {
            method: "POST",
            headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
            body: JSON.stringify({image: dataUri, sender: SENDER, device_name: MY_NAME, device_id: MY_ID, target_id: selectedDevice || null})
          }).then(function(r) { return r.json(); }).then(function(d) {
            if (!d.ok) throw new Error("发送失败");
            var localMsg = {id: d.msg_id, type: "image", data: dataUri, sender: SENDER, sender_name: "我", device_id: MY_ID, target_id: selectedDevice || null, time: Date.now()};
            seenMsgs.add(localMsg.id); allMessages.push(localMsg);
            appendMessage(localMsg, true);
          }).catch(function() { showToast("图片发送失败", true); });
        }
        showToast("已粘贴图片，正在发送...");
        requestNotifyPermission();
      };
      reader.readAsDataURL(blob);
      break;
    }
  });

  function showConfirmDialog(files) {
    var html = "";
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      var ext = (f.name.split(".").pop() || "").toLowerCase();
      var iconBg = "#f1f5f9", iconColor = "#64748b", iconText = "\uD83D\uDCC4";
      if (/^(png|jpg|jpeg|gif|webp|bmp|svg)$/i.test(ext)) { iconBg = "#dbeafe"; iconColor = "#3b82f6"; iconText = "\uD83D\uDDBC"; }
      else if (/^(mp3|wav|flac|aac|ogg|wma|m4a)$/i.test(ext)) { iconBg = "#ede9fe"; iconColor = "#7c3aed"; iconText = "\uD83C\uDFB5"; }
      else if (/^(mp4|avi|mkv|mov|wmv|flv|webm)$/i.test(ext)) { iconBg = "#fce4ec"; iconColor = "#e91e63"; iconText = "\uD83C\uDFA5"; }
      else if (/^(zip|rar|7z|tar|gz)$/i.test(ext)) { iconBg = "#fef3c7"; iconColor = "#d97706"; iconText = "\uD83D\uDCE6"; }
      else if (/^(pdf|doc|docx|xls|xlsx|ppt|pptx|txt|wps|et|dps|csv|rtf|odt|ods|odp|md)$/i.test(ext)) { iconBg = "#dbeafe"; iconColor = "#2563eb"; iconText = "\uD83D\uDCC3"; }
      html += '<div class="cd-file"><div class="cdf-icon" style="background:' + iconBg + ';color:' + iconColor + '">' + iconText + '</div>';
      html += '<div class="cdf-info"><div class="cdf-name">' + escHtml(f.name) + '</div><div class="cdf-size">' + formatSize(f.size) + '</div></div></div>';
    }
    confirmBody.innerHTML = html;
    confirmOverlay.classList.add("show");
  }

  window.closeConfirm = function() {
    confirmOverlay.classList.remove("show");
    pendingDropFiles = [];
  };

  window.confirmAndSend = function() {
    if (pendingDropFiles.length === 0) return;
    var files = pendingDropFiles.slice();
    confirmOverlay.classList.remove("show");
    pendingDropFiles = [];
    files.forEach(function(file) {
      var ext = (file.name.split(".").pop() || "").toLowerCase();
      var isImage = /^(png|jpg|jpeg|gif|webp|bmp|svg)$/i.test(ext);
      sendFileChunked(file, isImage);
    });
  };

  // --- 分块传输（断点续传）---
  var CHUNK_SIZE = 1024 * 1024;  // 1MB 每块
  function generateTransferId() {
    return "t" + Date.now().toString(36) + "_" + "xxxx".replace(/x/g, function() {
      return ((Math.random() * 16) | 0).toString(16);
    });
  }

  // 分块发送单个文件，返回 Promise
  function sendFileChunked(file, isImage) {
    var totalChunks = Math.ceil(file.size / CHUNK_SIZE);
    if (totalChunks === 0) totalChunks = 1;
    var transferId = generateTransferId();
    var targetAtStart = selectedDevice || null;
    var receivedSet = {};  // chunk_index -> true
    var progressToastId = null;

    function showProgress(received, total) {
      var pct = Math.round(received / total * 100);
      if (pct >= 100) {
        if (progressToastId) { clearTimeout(progressToastId); progressToastId = null; }
        return;
      }
      if (progressToastId) clearTimeout(progressToastId);
      showToast("发送中 " + file.name + " (" + received + "/" + total + " " + pct + "%)");
      progressToastId = setTimeout(function() { progressToastId = null; }, 3000);
    }

    function sendChunk(index, retryCount) {
      retryCount = retryCount || 0;
      if (receivedSet[index]) {
        // 已确认收到，跳过
        sendNext();
        return;
      }

      var start = index * CHUNK_SIZE;
      var end = Math.min(start + CHUNK_SIZE, file.size);
      var blob = file.slice(start, end);
      var reader = new FileReader();

      reader.onload = function() {
        var b64 = reader.result.split(",")[1] || reader.result;

        var body = {
          chunk_index: index,
          total_chunks: totalChunks,
          transfer_id: transferId,
          chunk_data: b64,
          file_info: {name: file.name, size: file.size, mime: file.type || "application/octet-stream"},
          sender: SENDER,
          device_name: MY_NAME,
          device_id: MY_ID,
          target_id: targetAtStart
        };
        if (isImage) body.image_type = true;

        fetch("/send", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
          body: JSON.stringify(body)
        }).then(function(r) { return r.json(); }).then(function(data) {
          if (data.ok && data.received) {
            data.received.forEach(function(i) { receivedSet[i] = true; });
            showProgress(Object.keys(receivedSet).length, totalChunks);
            if (data.complete) {
              if (progressToastId) { clearTimeout(progressToastId); progressToastId = null; }
              if (!isImage) {
                var localMsg = {
                  id: data.msg_id,
                  type: "file",
                  data: {
                    name: file.name,
                    size: file.size,
                    mime: file.type || "application/octet-stream",
                    path: "/file/" + data.msg_id
                  },
                  sender: SENDER,
                  sender_name: "我",
                  device_id: MY_ID,
                  target_id: targetAtStart,
                  time: Date.now()
                };
                seenMsgs.add(localMsg.id);
                appendMessage(localMsg, true);
              }
              showToast(file.name + " 发送成功");
            } else {
              sendNext();
            }
          } else {
            if (retryCount < 5) {
              setTimeout(function() { sendChunk(index, retryCount + 1); }, 1000 * (retryCount + 1));
            } else {
              showToast(file.name + " 分块发送失败");
            }
          }
        }).catch(function() {
          if (retryCount < 5) {
            setTimeout(function() { sendChunk(index, retryCount + 1); }, 1000 * (retryCount + 1));
          } else {
            showToast(file.name + " 发送失败（网络错误）");
          }
        });
      };

      reader.onerror = function() {
        if (retryCount < 3) {
          setTimeout(function() { sendChunk(index, retryCount + 1); }, 500);
        } else {
          showToast(file.name + " 读取失败");
        }
      };

      reader.readAsDataURL(blob);
    }

    function sendNext() {
      for (var i = 0; i < totalChunks; i++) {
        if (!receivedSet[i]) {
          sendChunk(i);
          return;
        }
      }
    }

    // 启动
    sendChunk(0);
  }

  // 图片 input — 作为图片消息发送（base64 data URI）
  imgInput.onchange = function() {
    if (currentDeviceCount === 0) {
      showToast("暂无设备连接，无法发送", true);
      imgInput.value = "";
      return;
    }
    for (var i = 0; i < imgInput.files.length; i++) {
      (function(file) {
        // 大于 2MB 的图片走分块传输
        if (file.size > 2 * 1024 * 1024) {
          sendFileChunked(file, true);
          return;
        }
        var reader = new FileReader();
        reader.onload = function() {
          var imgDataUri = reader.result;
          fetch("/send", {
            method: "POST",
            headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
            body: JSON.stringify({image: imgDataUri, sender: SENDER, device_name: MY_NAME, device_id: MY_ID, target_id: selectedDevice || null})
          }).then(function(r) { return r.json(); }).then(function(data) {
            if (!data.ok) throw new Error("发送失败");
            var path = "/img/" + data.msg_id;
            var localMsg = {id: data.msg_id, type: "image", data: path, sender: SENDER, sender_name: "我", device_id: MY_ID, target_id: selectedDevice || null, time: Date.now()};
            seenMsgs.add(localMsg.id);
            appendMessage(localMsg, true);
          }).catch(function() {
            showToast("发送失败，请检查连接", true);
          });
        };
        reader.readAsDataURL(file);
      })(imgInput.files[i]);
    }
    imgInput.value = "";
  };

  // 通用文件发送处理器
  function setupFileInput(input) {
    input.onchange = function() {
      if (currentDeviceCount === 0) {
        showToast("暂无设备连接，无法发送", true);
        input.value = "";
        return;
      }
      for (var i = 0; i < input.files.length; i++) {
        sendFileChunked(input.files[i], false);
      }
      input.value = "";
    };
  }
  setupFileInput(audioInput);
  setupFileInput(videoInput);
  setupFileInput(docInput);
  setupFileInput(otherInput);

  // Stage i18n: 客户端语种切换 (服务器在 <html lang> 提供初始值,localStorage 覆盖)
  (function() {
    var I18N = {
      zh: {
        devices: "在线设备", messages: "条消息", connect: "连接", password: "密码",
        wrongPassword: "密码错误", enterPassword: "请输入访问密码",
        offline: "目标设备不在线", noDevice: "暂无设备连接,无法发送", sendFail: "发送失败,请检查连接"
      },
      en: {
        devices: "Devices", messages: "messages", connect: "Connect", password: "Password",
        wrongPassword: "Wrong password", enterPassword: "Enter access password",
        offline: "Target device offline", noDevice: "No devices online", sendFail: "Send failed, check connection"
      }
    };
    function applyI18n(lang) {
      if (!I18N[lang]) lang = "zh";
      var dict = I18N[lang];
      document.querySelectorAll("[data-i18n]").forEach(function(el) {
        var key = el.getAttribute("data-i18n");
        if (dict[key]) el.textContent = dict[key];
      });
      document.documentElement.setAttribute("lang", lang === "zh" ? "zh-Hans" : "en");
    }
    var stored = null;
    try { stored = localStorage.getItem("feidi_lang"); } catch(e) {}
    var current = stored || document.documentElement.lang || "zh";
    if (!I18N[current]) current = "zh";
    applyI18n(current);
    window.__setLang = function(lang) {
      try { localStorage.setItem("feidi_lang", lang); } catch(e) {}
      applyI18n(lang);
    };
  })();
})();

// 密码登录：暴露到 window 以便 onclick 调用，对齐 MOBILE_HTML L2175-2191
window.doLogin = function() {
  var pwInput = document.getElementById("passwordInput");
  var errEl = document.getElementById("loginError");
  var ov = document.getElementById("loginOverlay");
  if (!pwInput || !ov) return;
  var pw = pwInput.value.trim();
  if (!pw) return;
  fetch("/login", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
    body: JSON.stringify({password: pw})
  }).then(function(r) { return r.json(); }).then(function(data) {
    if (data.ok) {
      ov.style.display = "none";
      location.reload();
    } else {
      if (errEl) errEl.style.display = "block";
      pwInput.value = "";
    }
  }).catch(function() {
    if (errEl) errEl.style.display = "block";
  });
};

// 回车键提交
document.addEventListener("keydown", function(e) {
  if (e.key === "Enter" && document.activeElement && document.activeElement.id === "passwordInput") {
    window.doLogin();
  }
});
</script>

<!-- 密码登录遮罩（Stage B / C4）：与 MOBILE_HTML loginOverlay 同结构 -->
<div class="login-overlay" id="loginOverlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:200;align-items:center;justify-content:center">
  <div style="background:#fff;border-radius:16px;padding:24px;width:280px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.2)">
    <div style="font-size:16px;font-weight:600;color:#3b82f6;margin-bottom:16px">飞递 Feidi</div>
    <div style="font-size:13px;color:#666;margin-bottom:12px">请输入访问密码</div>
    <input type="password" id="passwordInput" placeholder="密码" style="width:100%;padding:10px 14px;border:1px solid #ddd;border-radius:10px;font-size:15px;outline:none;text-align:center;margin-bottom:12px;box-sizing:border-box">
    <button onclick="doLogin()" style="width:100%;padding:10px;background:#3b82f6;color:#fff;border:none;border-radius:10px;font-size:15px;cursor:pointer">连接</button>
    <div id="loginError" style="color:#e53935;font-size:12px;margin-top:8px;display:none">密码错误</div>
  </div>
</div>
</body>
</html>
"""

# 用 SVG 图标替换所有占位符
PC_HTML = (PC_HTML
    .replace("__LANG__", "zh")  # 占位,运行时由 send_html 的 _pick_lang 覆盖
    .replace("__ICON_SEND__", SVG["send"])
    .replace("__ICON_IMAGE__", SVG["image"])
    .replace("__ICON_FILE__", SVG["file"])
    .replace("__ICON_MOON__", SVG["moon"])
    .replace("__ICON_SUN__", SVG["sun"])
    .replace("__ICON_MONITOR__", SVG["monitor"])
    .replace("__ICON_PHONE__", SVG["phone"])
    .replace("__ICON_GLOBE__", SVG["globe"])
    .replace("__ICON_WARN__", SVG["warn"])
    .replace("__ICON_SIGNAL__", SVG["signal"])
    .replace("__ICON_PLUS__", SVG["plus"])
    .replace("__ICON_MUSIC__", SVG["music"])
    .replace("__ICON_VIDEO__", SVG["video"])
    .replace("__ICON_DOC__", SVG["doc"])
)

# --- 手机端 HTML ---
MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="__LANG__">
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
  .msg{max-width:80%;padding:10px 14px;border-radius:12px;font-size:15px;line-height:1.6;word-break:break-word;animation:fadeIn .3s;background:var(--c-msg-other);color:var(--c-text)}
  .msg.mobile{align-self:flex-end;background:var(--c-msg-self);color:#1b5e20;border-bottom-right-radius:4px}
  [data-theme="dark"] .msg.mobile{color:#6ee7b7;background:#134e4a}
  .msg.pc{align-self:flex-start;background:var(--c-msg-other);color:var(--c-text);border-bottom-left-radius:4px}
  .msg img{max-width:200px;max-height:200px;border-radius:8px;cursor:pointer;display:block;margin-top:4px}
  .msg .meta{font-size:10px;opacity:.5;margin-top:4px}
  @keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
  .input-area{display:flex;gap:8px;padding:10px 14px;padding-bottom:max(10px,env(safe-area-inset-bottom));background:var(--c-surface);border-top:1px solid var(--c-border);align-items:flex-end}
  .input-area input[type=text]{flex:1;border:1.5px solid var(--c-border);border-radius:20px;padding:10px 16px;font-size:15px;outline:none;font-family:inherit;background:var(--c-input-bg);color:var(--c-text)}
  .input-area input[type=text]:focus{border-color:var(--c-pri)}
  .input-area button{width:40px;height:40px;border:none;background:var(--c-pri);color:#fff;border-radius:50%;cursor:pointer;font-size:18px;flex-shrink:0;display:flex;align-items:center;justify-content:center}
  .input-area .btn-attach{background:#64748b;position:relative}
  .input-area .btn-attach.active{background:var(--c-pri)}
  /* 附件弹出菜单 */
  .attach-backdrop{position:fixed;top:0;left:0;width:100%;height:100%;z-index:80;display:none}
  .attach-backdrop.show{display:block}
  .attach-menu{position:fixed;bottom:70px;left:12px;right:12px;max-width:280px;background:var(--c-surface);border:1px solid var(--c-border);border-radius:14px;box-shadow:0 4px 24px rgba(0,0,0,.15);z-index:90;overflow:hidden;display:none}
  .attach-menu.show{display:block;animation:menuIn .15s ease}
  @keyframes menuIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
  .attach-menu .menu-item{display:flex;align-items:center;gap:12px;padding:12px 18px;cursor:pointer;font-size:15px;color:var(--c-text);transition:background .15s}
  .attach-menu .menu-item:active{background:var(--c-primary-light);color:var(--c-primary)}
  .attach-menu .menu-item .mi-icon{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .attach-menu .menu-item .mi-icon.img{background:#e8f5e9;color:#43a047}
  .attach-menu .menu-item .mi-icon.audio{background:#fff3e0;color:#ef6c00}
  .attach-menu .menu-item .mi-icon.video{background:#fce4ec;color:#e91e63}
  .attach-menu .menu-item .mi-icon.doc{background:#e3f2fd;color:#1e88e5}
  .attach-menu .menu-item .mi-icon.other{background:#f3e5f5;color:#8e24aa}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.img{background:#1b5e20;color:#81c784}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.audio{background:#4e342e;color:#ffb74d}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.video{background:#880e4f;color:#f48fb1}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.doc{background:#0d47a1;color:#90caf9}
  [data-theme="dark"] .attach-menu .menu-item .mi-icon.other{background:#4a148c;color:#ce93d8}
  .empty-state{flex:1;display:flex;align-items:center;justify-content:center;color:var(--c-text3);font-size:15px;flex-direction:column;gap:8px}
  .toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);background:rgba(15,23,42,.85);color:#fff;padding:8px 16px;border-radius:20px;font-size:13px;z-index:100;opacity:0;transition:opacity .3s}
  .toast.show{opacity:1}
  /* 聊天标题栏 */
  .chat-title-bar{display:none;align-items:center;justify-content:center;gap:6px;padding:8px 14px;background:var(--c-surface);border-bottom:1px solid var(--c-border);font-size:13px;font-weight:500;color:var(--c-text);position:sticky;top:42px;z-index:9}
  .chat-title-bar.show{display:flex}
  .chat-title-bar .ct-dot{width:6px;height:6px;border-radius:50%;background:#10b981;flex-shrink:0}
  /* 设备按钮 */
  .header .devices-btn{position:absolute;left:10px;display:flex;align-items:center;gap:2px;background:rgba(255,255,255,.15);color:#fff;border:none;border-radius:14px;padding:3px 8px 3px 5px;font-size:12px;cursor:pointer}
  .header .devices-btn .db-count{background:rgba(255,255,255,.25);border-radius:10px;padding:0 5px;font-size:10px;min-width:18px;text-align:center;line-height:16px}
  /* 设备侧边栏（从左滑出）*/
  .sidebar-backdrop{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.35);z-index:100;display:none;transition:opacity .2s}
  .sidebar-backdrop.show{display:block}
  .sidebar{position:fixed;top:0;left:-280px;width:260px;height:100%;height:100dvh;background:var(--c-surface);z-index:110;transition:left .25s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;box-shadow:2px 0 16px rgba(0,0,0,.1);overflow:hidden}
  .sidebar.show{left:0}
  .sidebar .sb-header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--c-border);flex-shrink:0}
  .sidebar .sb-header .sb-title{font-size:15px;font-weight:600;color:var(--c-text);display:flex;align-items:center;gap:6px}
  .sidebar .sb-header .sb-close{width:28px;height:28px;border:none;background:var(--c-border);color:var(--c-text2);border-radius:50%;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center}
  .sidebar .sb-body{flex:1;overflow-y:auto;padding:8px}
  .sidebar .sb-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;cursor:pointer;transition:background .15s;margin-bottom:4px}
  .sidebar .sb-item:active,.sidebar .sb-item.selected{background:var(--c-primary-light)}
  .sidebar .sb-item.me{opacity:.7}
  .sidebar .sb-item .sb-avatar{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:600;color:#fff;flex-shrink:0}
  .sidebar .sb-item .sb-info{flex:1;min-width:0}
  .sidebar .sb-item .sb-name{font-size:14px;font-weight:500;color:var(--c-text);display:flex;align-items:center;gap:4px;flex-wrap:wrap}
  .sidebar .sb-item .sb-type{font-size:11px;color:var(--c-text3);margin-top:1px}
  .sidebar .sb-item .sb-badge{font-size:9px;padding:1px 5px;border-radius:8px;font-weight:500}
  .sidebar .sb-item .sb-badge.me-badge{background:var(--c-pri);color:#fff}
  .sidebar .sb-item .sb-badge.remark-badge{background:#f59e0b;color:#fff}
  /* 设备操作菜单 */
  .sidebar .sb-item-menu{display:flex;gap:4px;flex-shrink:0}
  .sidebar .sb-item-menu button{width:24px;height:24px;border:none;background:var(--c-border);color:var(--c-text2);border-radius:6px;cursor:pointer;font-size:11px;display:flex;align-items:center;justify-content:center}
  /* 侧边栏内输入框（改名） */
  .sidebar .sb-rename-input{width:100%;border:1px solid var(--c-pri);border-radius:6px;padding:3px 6px;font-size:13px;outline:none;font-family:inherit;background:var(--c-input-bg);color:var(--c-text)}
</style>
</head>
<body>
<div class="header">飞递 Feidi<button class="devices-btn" id="devicesBtn" onclick="toggleSidebar()"><span style="font-size:13px">&#x1F4F1;</span><span class="db-count" id="deviceCountBadge">0</span></button><button class="theme-btn" id="themeBtn" onclick="toggleTheme()">__ICON_MOON__</button><span style="font-size:11px;opacity:.7;margin-left:auto"><a href="#" onclick="window.__setLang('zh');return false" style="color:inherit;text-decoration:none">中</a>|<a href="#" onclick="window.__setLang('en');return false" style="color:inherit;text-decoration:none">EN</a></span><div class="sub">手机端</div></div>
<div class="status-bar connected" id="statusBar">
  <span class="dot green"></span><span>已连接</span>
</div>
<div class="chat-title-bar" id="chatTitleBar">
  <span class="ct-dot"></span><span id="chatTitleText">飞递 Feidi</span>
</div>
<div class="messages" id="messages">
    <div class="empty-state" id="emptyState">
      __ICON_MAIL__
      <div>发送第一条消息吧</div>
  </div>
</div>
<div class="input-area">
  <input type="file" id="imgInput" accept="image/*" multiple style="display:none">
  <input type="file" id="audioInput" accept="audio/*" multiple style="display:none">
  <input type="file" id="videoInput" accept="video/*" multiple style="display:none">
  <input type="file" id="docInput" accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.wps,.et,.dps,.csv,.rtf,.odt,.ods,.odp,.md" multiple style="display:none">
  <input type="file" id="otherInput" multiple style="display:none">
  <button class="btn-attach" id="btnAttach" onclick="toggleAttachMenu()" title="添加附件">__ICON_PLUS__</button>
  <input type="text" id="textInput" placeholder="输入文字..." onkeydown="if(event.key==='Enter'){event.preventDefault();sendText()}">
  <button onclick="sendText()" title="发送">__ICON_SEND__</button>
</div>
<div class="attach-backdrop" id="attachBackdrop" onclick="toggleAttachMenu()"></div>
<div class="attach-menu" id="attachMenu">
  <div class="menu-item" onclick="pickFile('img')"><span class="mi-icon img">__ICON_IMAGE__</span>图片</div>
  <div class="menu-item" onclick="pickFile('audio')"><span class="mi-icon audio">__ICON_MUSIC__</span>音频</div>
  <div class="menu-item" onclick="pickFile('video')"><span class="mi-icon video">__ICON_VIDEO__</span>视频</div>
  <div class="menu-item" onclick="pickFile('doc')"><span class="mi-icon doc">__ICON_DOC__</span>文档</div>
  <div class="menu-item" onclick="pickFile('other')"><span class="mi-icon other">__ICON_FILE__</span>其他文件</div>
</div>
<div class="toast" id="toast"></div>
<!-- 设备侧边栏 -->
<div class="sidebar-backdrop" id="sidebarBackdrop" onclick="toggleSidebar()"></div>
<div class="sidebar" id="sidebar">
  <div class="sb-header">
    <span class="sb-title">&#x1F4F1; 在线设备</span>
    <button class="sb-close" onclick="toggleSidebar()">✕</button>
  </div>
  <div class="sb-body" id="sbBody"></div>
</div>
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
    if (btn) { btn.innerHTML = t === "dark" ? '__ICON_SUN__' : '__ICON_MOON__'; }
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

  // --- 身份与持久化 ---
  var PERSISTENT_ID = "";
  try { PERSISTENT_ID = localStorage.getItem("feidi_pid"); } catch(e) {}
  if (!PERSISTENT_ID) {
    // Stage D (M1): 用 CSPRNG (crypto.getRandomValues) 生成 UUID v4（对齐 PC 端）
    try {
      var _buf = new Uint8Array(16);
      (window.crypto || window.msCrypto).getRandomValues(_buf);
      _buf[6] = (_buf[6] & 0x0f) | 0x40;
      _buf[8] = (_buf[8] & 0x3f) | 0x80;
      var _hex = Array.prototype.map.call(_buf, function(b) {
        return (b < 16 ? "0" : "") + b.toString(16);
      }).join("");
      PERSISTENT_ID = _hex.substr(0,8) + "-" + _hex.substr(8,4) + "-" + _hex.substr(12,4)
        + "-" + _hex.substr(16,4) + "-" + _hex.substr(20,12);
    } catch(_e) {
      if (window.crypto && window.crypto.randomUUID) {
        PERSISTENT_ID = window.crypto.randomUUID();
      } else {
        PERSISTENT_ID = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function(c) {
          var r = Math.random() * 16 | 0, v = c === "x" ? r : (r & 0x3 | 0x8);
          return v.toString(16);
        });
      }
    }
    try { localStorage.setItem("feidi_pid", PERSISTENT_ID); } catch(e) {}
  }
  var MY_DISPLAY_NAME = "";
  try { MY_DISPLAY_NAME = localStorage.getItem("feidi_myname") || ""; } catch(e) {}
  var remarks = {};
  try { remarks = JSON.parse(localStorage.getItem("feidi_remarks") || "{}"); } catch(e) {}
  var MY_ID = "";
  var MY_SESSION = "";  // Stage E
  var MY_NAME = "手机";
  var selectedDevice = null;
  var sse_clients_cache = [];
  var allMessages = [];

  function getDisplayName(device) {
    if (remarks[device.id]) return remarks[device.id];
    return device.name || device.type;
  }
  function saveRemarks() {
    try { localStorage.setItem("feidi_remarks", JSON.stringify(remarks)); } catch(e) {}
  }
  function saveMyName(name) {
    MY_DISPLAY_NAME = name;
    try { localStorage.setItem("feidi_myname", name); } catch(e) {}
  }
  function getAvatarColor(name) {
    var hash = 0;
    for (var i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
    var h = ((hash % 360) + 360) % 360;
    return "hsl(" + h + ", 55%, 48%)";
  }
  function getAvatarLetter(name) { return (name || "?")[0].toUpperCase(); }
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

  const seenMsgs = new Set();
  const evtSource = new EventSource("/events?type=mobile&pid=" + encodeURIComponent(PERSISTENT_ID) + "&name=" + encodeURIComponent("手机") + (MY_DISPLAY_NAME ? "&my_name=" + encodeURIComponent(MY_DISPLAY_NAME) : ""));
  
  evtSource.addEventListener("device_id", function(e) {
    var data = JSON.parse(e.data);
    MY_ID = data.device_id;
    MY_SESSION = data.session_token || "";  // Stage E
    MY_NAME = data.name;
    // 默认私聊：MY_ID 就绪后补选第一台非本机设备
    if (!selectedDevice && sse_clients_cache && sse_clients_cache.length > 0) {
      var others = sse_clients_cache.filter(function(d) { return d.id !== MY_ID; });
      if (others.length > 0) switchConversation(others[0].id);
    }
    if (MY_DISPLAY_NAME && MY_DISPLAY_NAME !== data.name) {
      fetch("/rename?id=" + encodeURIComponent(MY_ID) + "&name=" + encodeURIComponent(MY_DISPLAY_NAME), {headers: {"X-Feidi-Session": MY_SESSION}});
    }
  });

  evtSource.addEventListener("history", function(e) {
    const msgs = JSON.parse(e.data);
    msgs.forEach(function(m) {
      if (!seenMsgs.has(m.id)) { seenMsgs.add(m.id); allMessages.push(m); appendMessage(m, false); }
    });
  });
  evtSource.addEventListener("new_message", function(e) {
    const msg = JSON.parse(e.data);
    if (seenMsgs.has(msg.id)) return;
    seenMsgs.add(msg.id);
    if (seenMsgs.size > 500) { seenMsgs.clear(); }
    allMessages.push(msg);
    // 通知：来自非当前会话且非自己的消息
    if (msg.device_id !== MY_ID && msg.device_id !== selectedDevice) {
      notifyMessage(msg);
    }
    if (selectedDevice) {
      var fromSelected = msg.device_id === selectedDevice;
      var toSelected = msg.target_id === selectedDevice;
      if (!fromSelected && !((msg.device_id === MY_ID) && toSelected)) return;
    }
    appendMessage(msg, true);
  });
  evtSource.addEventListener("transfer_timeout", function(e) {
    var info = JSON.parse(e.data);
    showToast("\u26A0\uFE0F 文件传输失败: " + (info.filename || "未知文件") + " — " + (info.error || "超时"));
  });
  evtSource.addEventListener("device_list", function(e) {
    var data = JSON.parse(e.data);
    renderSidebar(data.devices || []);
    // 默认私聊模式：仅在 MY_ID 已知后才自动选设备
    if (!selectedDevice && MY_ID) {
      var others = (data.devices || []).filter(function(d) { return d.id !== MY_ID; });
      if (others.length > 0) switchConversation(others[0].id);
    }
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
  const imgInput = document.getElementById("imgInput");
  const audioInput = document.getElementById("audioInput");
  const videoInput = document.getElementById("videoInput");
  const docInput = document.getElementById("docInput");
  const otherInput = document.getElementById("otherInput");
  const btnAttach = document.getElementById("btnAttach");
  const attachMenu = document.getElementById("attachMenu");
  const attachBackdrop = document.getElementById("attachBackdrop");
  const toast = document.getElementById("toast");
  const loginOverlay = document.getElementById("loginOverlay");
  const passwordInput = document.getElementById("passwordInput");
  const loginError = document.getElementById("loginError");
  const sidebar = document.getElementById("sidebar");
  const sidebarBackdrop = document.getElementById("sidebarBackdrop");
  const sbBody = document.getElementById("sbBody");
  const deviceCountBadge = document.getElementById("deviceCountBadge");
  const chatTitleBar = document.getElementById("chatTitleBar");
  const chatTitleText = document.getElementById("chatTitleText");

  function showToast(msg) {
    toast.textContent = msg;
    toast.className = "toast show";
    setTimeout(function() { toast.className = "toast"; }, 2000);
  }

  // 新消息通知
  var _notifyGranted_m = false;
  if ("Notification" in window && Notification.permission === "granted") _notifyGranted_m = true;
  function requestNotifyPermission() {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission().then(function(p) { if (p === "granted") _notifyGranted_m = true; });
    }
  }
  var _blinkTimer_m = null, _blinkOrigTitle_m = "";
  function notifyMessage(msg) {
    var senderName = msg.sender_name || getDisplayName({id: msg.device_id, name: msg.device_id, type: "pc"});
    var preview = "";
    if (msg.type === "text") preview = msg.data || "";
    else if (msg.type === "image") preview = "[图片]";
    else if (msg.type === "file") preview = "[文件] " + (msg.data && msg.data.name || "");
    if (preview.length > 40) preview = preview.substring(0, 40) + "...";
    // 仅后台标签页发送系统通知
    if (_notifyGranted_m && document.hidden) {
      var avatarChar = (senderName[0] || "?").toUpperCase();
      try { new Notification(senderName + " 发来消息", {body: preview, icon: "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><circle cx="16" cy="16" r="14" fill="#2e7d32"/><text x="16" y="22" text-anchor="middle" fill="white" font-size="16" font-weight="bold">' + avatarChar + '</text></svg>')}); } catch(e) {}
    } else {
      showToast(senderName + ": " + (preview || "新消息"));
    }
    // 标题闪烁（去抖）
    if (_blinkTimer_m) clearInterval(_blinkTimer_m);
    _blinkOrigTitle_m = document.title;
    var blinkCount = 0;
    _blinkTimer_m = setInterval(function() {
      blinkCount++;
      document.title = (blinkCount % 2 === 0) ? _blinkOrigTitle_m : "\uD83D\uDD14 " + senderName + " \u00B7 " + _blinkOrigTitle_m;
      if (blinkCount >= 6) { clearInterval(_blinkTimer_m); _blinkTimer_m = null; document.title = _blinkOrigTitle_m; }
    }, 800);
  }

  // --- 侧边栏 ---
  window.toggleSidebar = function() {
    var show = !sidebar.classList.contains("show");
    sidebar.classList.toggle("show", show);
    sidebarBackdrop.classList.toggle("show", show);
  };

  function updateChatTitleBar() {
    if (selectedDevice) {
      var name = selectedDevice;
      for (var i = 0; i < (sse_clients_cache || []).length; i++) {
        if (sse_clients_cache[i].id === selectedDevice) { name = getDisplayName(sse_clients_cache[i]); break; }
      }
      chatTitleText.textContent = name;
      chatTitleBar.classList.add("show");
    } else {
      chatTitleBar.classList.remove("show");
    }
  }

  function switchConversation(deviceId) {
    if (deviceId === selectedDevice) return;
    selectedDevice = deviceId;
    updateChatTitleBar();
    // 高亮侧边栏
    var items = sbBody.querySelectorAll(".sb-item");
    items.forEach(function(el) { el.classList.remove("selected"); });
    if (deviceId) {
      var sel = sbBody.querySelector('[data-device-id="' + deviceId + '"]');
      if (sel) sel.classList.add("selected");
    }
    rebuildMessages();
  }

  function rebuildMessages() {
    var list = msgContainer.querySelectorAll(".msg");
    list.forEach(function(el) { el.remove(); });
    if (emptyState && emptyState.parentNode) emptyState.parentNode.removeChild(emptyState);
    allMessages.forEach(function(m) {
      if (selectedDevice) {
        var fromSelected = m.device_id === selectedDevice;
        var toSelected = m.target_id === selectedDevice;
        if (!fromSelected && !((m.device_id === MY_ID) && toSelected)) return;
      }
      appendMessage(m, false);
    });
    if (!msgContainer.querySelector(".msg")) {
      if (emptyState) msgContainer.appendChild(emptyState);
    }
  }

  function renderSidebar(devices) {
    sse_clients_cache = devices;
    var count = devices.length;
    var otherCount = 0;
    devices.forEach(function(d) { if (d.id !== MY_ID) otherCount++; });
    deviceCountBadge.textContent = otherCount;
    if (count === 0) return;
    // 如果选中设备已断开，回到广播，并自动选第一台
    if (selectedDevice) {
      var stillHere = false;
      devices.forEach(function(d) { if (d.id === selectedDevice) stillHere = true; });
      if (!stillHere) {
        selectedDevice = null;
        updateChatTitleBar();
        var others2 = devices.filter(function(d) { return d.id !== MY_ID; });
        if (others2.length > 0) switchConversation(others2[0].id);
        else rebuildMessages();
      }
    }
    var html = "";
    devices.forEach(function(d) {
      var isMe = d.id === MY_ID;
      var displayName = getDisplayName(d);
      var isRemark = !!remarks[d.id];
      var avatarColor = getAvatarColor(displayName);
      var avatarLetter = getAvatarLetter(displayName);
      var selCls = (d.id === selectedDevice) ? " selected" : "";
      var meCls = isMe ? " me" : "";
      var remarkTag = isRemark ? ' <span class="sb-badge remark-badge">备注</span>' : '';
      var meTag = isMe ? ' <span class="sb-badge me-badge">本机</span>' : '';
      html += '<div class="sb-item' + selCls + meCls + '" data-device-id="' + d.id + '">' +
        '<div class="sb-avatar" style="background:' + avatarColor + '">' + avatarLetter + '</div>' +
        '<div class="sb-info"><div class="sb-name">' + escHtml(displayName) + meTag + remarkTag + '</div>' +
        '<div class="sb-type">' + (d.type === "mobile" ? "手机" : "电脑") + (isRemark ? ' — ' + escHtml(d.name || d.type) : '') + '</div></div>' +
        '<div class="sb-item-menu"><button onclick="event.stopPropagation();startRenameMobile(\'' + d.id + '\')" title="' + (isMe ? '改名' : '备注') + '">✎</button></div>' +
        '</div>';
    });
    sbBody.innerHTML = html;
    sbBody.querySelectorAll(".sb-item").forEach(function(el) {
      el.addEventListener("click", function() {
        var did = el.getAttribute("data-device-id");
        if (did === MY_ID) return;
        toggleSidebar();
        switchConversation(did === selectedDevice ? null : did);
      });
    });
  }

  window.startRenameMobile = function(deviceId) {
    var item = sbBody.querySelector('[data-device-id="' + deviceId + '"]');
    if (!item) return;
    var nameEl = item.querySelector(".sb-name");
    if (!nameEl) return;
    var isMe = deviceId === MY_ID;
    var oldName = isMe ? (MY_DISPLAY_NAME || MY_NAME) : (remarks[deviceId] || "");
    var placeholder = isMe ? "给自己起个名字" : "添加备注（仅自己可见）";
    var input = document.createElement("input");
    input.className = "sb-rename-input";
    input.value = oldName;
    input.placeholder = placeholder;
    input.setAttribute("data-device-id", deviceId);
    input.addEventListener("blur", function() {
      var v = input.value.trim();
      if (isMe) {
        saveMyName(v);
        if (v && v !== MY_NAME) {
          fetch("/rename?id=" + encodeURIComponent(MY_ID) + "&name=" + encodeURIComponent(v), {headers: {"X-Feidi-Session": MY_SESSION}});
        }
      } else {
        if (v) { remarks[deviceId] = v; } else { delete remarks[deviceId]; }
        saveRemarks();
      }
      renderSidebar(sse_clients_cache);
      updateChatTitleBar();
    });
    input.addEventListener("keydown", function(e) {
      if (e.key === "Enter") { input.blur(); }
      if (e.key === "Escape") { input.value = oldName; input.blur(); }
    });
    nameEl.style.display = "none";
    nameEl.parentNode.insertBefore(input, nameEl);
    input.focus();
    input.select();
  };

  window.doLogin = function() {
    const pw = passwordInput.value.trim();
    if (!pw) return;
    fetch("/login", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
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
      // M-6: 改 a.download，禁掉 window.open
      const dlImg = document.createElement("a");
      dlImg.href = msg.data;
      dlImg.download = "image";
      dlImg.appendChild(img);
      div.appendChild(dlImg);
    } else if (msg.type === "file" && msg.data) {
      var fd = msg.data;
      var card = document.createElement("div");
      card.style.cssText = "display:flex;align-items:center;gap:8px;padding:8px;background:rgba(255,255,255,.4);border-radius:8px;cursor:pointer";
      card.onclick = function() {
        // M-6: 用 <a download> 触发下载，避免新 tab 渲染任意 Content-Type
        const a = document.createElement("a");
        a.href = fd.path;
        a.download = fd.name || "download";
        document.body.appendChild(a);
        a.click();
        a.remove();
      };
      var ficon = document.createElement("div");
      ficon.style.cssText = "width:32px;height:32px;background:rgba(0,0,0,.08);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:14px";
      ficon.textContent = "\uD83D\uDCC4";
      card.appendChild(ficon);
      var finfo = document.createElement("div");
      finfo.style.cssText = "min-width:0";
      finfo.innerHTML = '<div style="font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + escHtml(fd.name || "文件") + '</div><div style="font-size:10px;opacity:.5">' + formatSize(fd.size) + '</div>';
      card.appendChild(finfo);
      div.appendChild(card);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = new Date(msg.time).toLocaleTimeString("zh-CN", {hour:"2-digit",minute:"2-digit"});
    div.appendChild(meta);
    msgContainer.appendChild(div);
    msgContainer.scrollTop = msgContainer.scrollHeight;
  }

  // --- 分块传输（断点续传）---
  var CHUNK_SIZE_m = 1024 * 1024;
  function generateTransferId() {
    return "t" + Date.now().toString(36) + "_" + "xxxx".replace(/x/g, function() {
      return ((Math.random() * 16) | 0).toString(16);
    });
  }
  function sendFileChunked(file, isImage) {
    var totalChunks = Math.ceil(file.size / CHUNK_SIZE_m);
    if (totalChunks === 0) totalChunks = 1;
    var transferId = generateTransferId();
    var targetAtStart = selectedDevice || null;
    var receivedSet = {};
    function sendChunk(index, retryCount) {
      retryCount = retryCount || 0;
      if (receivedSet[index]) { sendNext(); return; }
      var start = index * CHUNK_SIZE_m;
      var end = Math.min(start + CHUNK_SIZE_m, file.size);
      var blob = file.slice(start, end);
      var reader = new FileReader();
      reader.onload = function() {
        var b64 = reader.result.split(",")[1] || reader.result;
        var body = {
          chunk_index: index, total_chunks: totalChunks, transfer_id: transferId, chunk_data: b64,
          file_info: {name: file.name, size: file.size, mime: file.type || "application/octet-stream"},
          sender: SENDER, device_name: MY_NAME, device_id: MY_ID, target_id: targetAtStart
        };
        if (isImage) body.image_type = true;
        fetch("/send", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
          body: JSON.stringify(body)
        }).then(function(r) { return r.json(); }).then(function(data) {
          if (data.ok && data.received) {
            data.received.forEach(function(i) { receivedSet[i] = true; });
            if (data.complete) {
              if (!isImage) {
                var localMsg = {
                  id: data.msg_id,
                  type: "file",
                  data: {
                    name: file.name,
                    size: file.size,
                    mime: file.type || "application/octet-stream",
                    path: "/file/" + data.msg_id
                  },
                  sender: SENDER,
                  sender_name: "我",
                  device_id: MY_ID,
                  target_id: targetAtStart,
                  time: Date.now()
                };
                seenMsgs.add(localMsg.id);
                appendMessage(localMsg, true);
              }
              showToast(file.name + " 发送成功");
            }
            else { sendNext(); }
          } else if (retryCount < 5) {
            setTimeout(function() { sendChunk(index, retryCount + 1); }, 1000 * (retryCount + 1));
          } else { showToast(file.name + " 发送失败"); }
        }).catch(function() {
          if (retryCount < 5) {
            setTimeout(function() { sendChunk(index, retryCount + 1); }, 1000 * (retryCount + 1));
          } else { showToast(file.name + " 发送失败"); }
        });
      };
      reader.onerror = function() {
        if (retryCount < 3) setTimeout(function() { sendChunk(index, retryCount + 1); }, 500);
        else showToast(file.name + " 读取失败");
      };
      reader.readAsDataURL(blob);
    }
    function sendNext() {
      for (var i = 0; i < totalChunks; i++) if (!receivedSet[i]) { sendChunk(i); return; }
    }
    sendChunk(0);
  }

  window.sendText = function() {
    const text = textInput.value.trim();
    if (!text) return;
    requestNotifyPermission();
    var textTarget = selectedDevice || null;
    fetch("/send", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
      body: JSON.stringify({text: text, sender: SENDER, device_name: MY_NAME, device_id: MY_ID, target_id: textTarget})
    }).then(function(r) { return r.json(); }).then(function(data) {
      if (!data.ok) throw new Error("发送失败");
      var localMsg = {id: data.msg_id, type: "text", data: text, sender: SENDER, sender_name: "我", device_id: MY_ID, target_id: textTarget, time: Date.now()};
      seenMsgs.add(localMsg.id);
      appendMessage(localMsg, true);
    }).catch(function() {
      showToast("发送失败");
    });
    textInput.value = "";
  };

  // --- 附件菜单 ---
  window.toggleAttachMenu = function() {
    var show = !attachMenu.classList.contains("show");
    attachMenu.classList.toggle("show", show);
    attachBackdrop.classList.toggle("show", show);
    btnAttach.classList.toggle("active", show);
  };
  window.pickFile = function(type) {
    var inputMap = {img: imgInput, audio: audioInput, video: videoInput, doc: docInput, other: otherInput};
    var input = inputMap[type];
    if (input) input.click();
    attachMenu.classList.remove("show");
    attachBackdrop.classList.remove("show");
    btnAttach.classList.remove("active");
  };

  // 图片 — 作为图片消息发送
  imgInput.onchange = function() {
    for (var i = 0; i < imgInput.files.length; i++) {
      (function(file) {
        if (file.size > 2 * 1024 * 1024) {
          sendFileChunked(file, true);
          return;
        }
        const reader = new FileReader();
        reader.onload = function() {
          fetch("/send", {
            method: "POST",
            headers: {"Content-Type": "application/json", "X-Feidi-Session": MY_SESSION},
            body: JSON.stringify({image: reader.result, sender: SENDER, device_name: MY_NAME, device_id: MY_ID, target_id: selectedDevice || null})
          }).then(function(r) { return r.json(); }).then(function(data) {
            if (!data.ok) throw new Error("发送失败");
            var path = "/img/" + data.msg_id;
            var localMsg = {id: data.msg_id, type: "image", data: path, sender: SENDER, sender_name: "我", device_id: MY_ID, target_id: selectedDevice || null, time: Date.now()};
            seenMsgs.add(localMsg.id);
            appendMessage(localMsg, true);
          }).catch(function() {
            showToast("图片过大，请压缩后重试");
          });
        };
        reader.readAsDataURL(file);
      })(imgInput.files[i]);
    }
    imgInput.value = "";
  };

  // 通用文件发送
  function setupFileInput(input) {
    input.onchange = function() {
      for (var i = 0; i < input.files.length; i++) {
        sendFileChunked(input.files[i], false);
      }
      input.value = "";
    };
  }
  setupFileInput(audioInput);
  setupFileInput(videoInput);
  setupFileInput(docInput);
  setupFileInput(otherInput);

  // 粘贴图片发送（移动端也支持）
  document.addEventListener("paste", function(e) {
    var items = (e.clipboardData || window.clipboardData).items;
    if (!items) return;
    for (var i = 0; i < items.length; i++) {
      if (items[i].type.indexOf("image") !== 0) continue;
      e.preventDefault();
      var blob = items[i].getAsFile();
      var f = new File([blob], "clipboard_" + Date.now() + ".png", {type: blob.type});
      sendFileChunked(f, true);
      showToast("已粘贴图片，正在发送...");
      requestNotifyPermission();
      break;
    }
  });

  // Stage i18n: 客户端语种切换
  (function() {
    var I18N = {
      zh: { devices: "在线设备", messages: "条消息", connect: "连接", password: "密码",
            wrongPassword: "密码错误", enterPassword: "请输入访问密码",
            offline: "目标设备不在线", noDevice: "暂无设备连接,无法发送", sendFail: "发送失败,请检查连接" },
      en: { devices: "Devices", messages: "messages", connect: "Connect", password: "Password",
            wrongPassword: "Wrong password", enterPassword: "Enter access password",
            offline: "Target device offline", noDevice: "No devices online", sendFail: "Send failed, check connection" }
    };
    function applyI18n(lang) {
      if (!I18N[lang]) lang = "zh";
      var dict = I18N[lang];
      document.querySelectorAll("[data-i18n]").forEach(function(el) {
        var key = el.getAttribute("data-i18n");
        if (dict[key]) el.textContent = dict[key];
      });
      document.documentElement.setAttribute("lang", lang === "zh" ? "zh-Hans" : "en");
    }
    var stored = null;
    try { stored = localStorage.getItem("feidi_lang"); } catch(e) {}
    var current = stored || document.documentElement.lang || "zh";
    if (!I18N[current]) current = "zh";
    applyI18n(current);
    window.__setLang = function(lang) {
      try { localStorage.setItem("feidi_lang", lang); } catch(e) {}
      applyI18n(lang);
    };
  })();
})();
</script>
</body>
</html>
"""

# 用 SVG 图标替换所有占位符
MOBILE_HTML = (MOBILE_HTML
    .replace("__LANG__", "zh")  # 占位,运行时由 send_html 的 _pick_lang 覆盖
    .replace("__ICON_SEND__", SVG["send"])
    .replace("__ICON_CAMERA__", SVG["camera"])
    .replace("__ICON_MOON__", SVG["moon"])
    .replace("__ICON_SUN__", SVG["sun"])
    .replace("__ICON_MAIL__", SVG["mail"])
    .replace("__ICON_PLUS__", SVG["plus"])
    .replace("__ICON_IMAGE__", SVG["image"])
    .replace("__ICON_FILE__", SVG["file"])
    .replace("__ICON_MUSIC__", SVG["music"])
    .replace("__ICON_VIDEO__", SVG["video"])
    .replace("__ICON_DOC__", SVG["doc"])
)

def _parse_content_length(headers, max_allowed=None):
    """C-05：安全解析 Content-Length。

    非法值（非数字 / 负数 / 超 max_allowed）返回 None，调用方应回 400。
    缺失或空头视为 0（兼容不发送该头的客户端）。BaseHTTPRequestHandler 不会
    解析该头，原始 socket 可发 'Content-Length: abc' 使 int() 抛 ValueError → 500，
    故必须在此兜底。
    """
    raw = headers.get("Content-Length") or "0"
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return None
    if val < 0:
        return None
    if max_allowed is not None and val > max_allowed:
        return None
    return val


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """支持多线程的 HTTP 服务器，每个请求在独立线程中处理。"""
    daemon_threads = True


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    # H-3 follow-up H-1: socket-level read timeout，防止慢客户端把 worker 线程
    # 永久卡在 copyfileobj 里、把 _file_refs 占住让淘汰失效。timeout 触发后
    # handler 抛 socket.timeout，被 finally 的 _release_file 兜底释放。
    timeout = SOCKET_TIMEOUT

    def log_message(self, format, *args):
        # L7 修复: 输出到 stderr, 至少排障可见; 异常时降级输出原始 format 避免递归
        try:
            print(f"[feidi] {format % args}", file=sys.stderr, flush=True)
        except Exception:
            print(f"[feidi] {format}", file=sys.stderr, flush=True)

    def check_password(self):
        """检查密码 — 通过 Cookie 中的 auth token"""
        if not PASSWORD:
            return True
        cookies = {}
        cookie_header = self.headers.get("Cookie", "")
        for item in cookie_header.split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                cookies[k.strip()] = v.strip()
        return secrets.compare_digest(cookies.get("feidi_auth", ""), AUTH_TOKEN)

    def set_auth_cookie(self):
        # H-6: HttpOnly 防 JS 读取；plaintext HTTP 不能加 Secure；SameSite=Lax 保持
        # Stage H (L8): HTTPS 反代场景下加 Secure
        cookie = f"feidi_auth={AUTH_TOKEN}; Path=/; Max-Age=86400; SameSite=Lax; HttpOnly"
        if self.headers.get("X-Forwarded-Proto") == "https":
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)

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

    def send_error_json(self, code, msg):
        """API 端点用：返回 JSON 格式错误"""
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
                mobile_url += "?auth=required"
            qr_svg = generate_qr_svg(mobile_url)
            # Stage i18n: 服务器探测语言,设入 <html lang> 属性供 JS 读取
            lang = _pick_lang(parsed.query)
            html_content = (PC_HTML
                .replace("__QR_SVG__", qr_svg)
                .replace("__MOBILE_URL__", mobile_url)
                .replace("__LANG__", lang))
            self.send_html(html_content)
            return

        if path == "/mobile":
            # Stage i18n: 注入 <html lang>
            lang = _pick_lang(parsed.query)
            self.send_html(MOBILE_HTML.replace("__LANG__", lang))
            return

        if not self.check_password():
            self.send_error_body(403, "Forbidden: wrong password")
            return

        if path.startswith("/rename"):
            params = parse_qs(parsed.query)
            dev_id = params.get("id", [""])[0]
            new_name = params.get("name", [""])[0]
            if not dev_id or not new_name:
                self.send_error_json(400, "Missing id or name")
                return
            new_name = new_name.strip()[:20]
            if not new_name:
                self.send_error_json(400, "Name cannot be empty")
                return

            # Stage E (H2/H10): session token 校验 — 不允许只凭 body 里的 device_id
            # 改别人名字；必须是持有该 device_id 对应 session token 的 SSE 客户端
            session_token = self.headers.get("X-Feidi-Session", "")
            session_dev_id = _check_session_token(session_token)
            if not session_dev_id:
                self.send_error_json(401, "Missing or invalid session token")
                return
            if session_dev_id != dev_id:
                self.send_error_json(403, "device_id does not match session")
                return

            # 通过 device_id 匹配（device_id 在 SSE 握手时分配，非公开）
            renamed = False
            with _sse_lock:
                for c in sse_clients:
                    if c.get("device_id") == dev_id:
                        c["name"] = new_name
                        renamed = True
                        break
            if not renamed:
                self.send_error_json(404, "Device not found")
                return

            # 持久化到 identity_map（C-06：持 _identity_lock，避免与 SSE 握手并发写
            # 触发 RuntimeError: dictionary changed size during iteration）
            with _identity_lock:
                for ikey, info in identity_map.items():
                    if info.get("device_id") == dev_id:
                        info["name"] = new_name
                        break
            save_identities()
            broadcast_device_list()
            self.send_json(200, {"ok": True, "name": new_name})

        elif path == "/status":
            self.send_json(200, {"connections": len(sse_clients), "messages": len(messages)})

        elif path.startswith("/upload/status/"):
            # Stage F (F6): 客户端断点续传查询 — 返回已收到的 chunk 索引列表与 total。
            upload_id = path[len("/upload/status/"):]
            if not re.match(r'^[a-zA-Z0-9\-_]+$', upload_id):
                self.send_error_body(400, "Invalid transfer_id")
                return
            with _chunk_lock:
                ct = chunk_transfers.get(upload_id)
                if not ct:
                    # 也查 completed_transfers（F4）
                    done = completed_transfers.get(upload_id)
                    if done:
                        self.send_json(200, {"ok": True, "complete": True, "msg_id": done})
                        return
                    self.send_error_body(404, "Transfer not found")
                    return
                received = sorted(ct["chunks"])
                total = ct["total"]
            self.send_json(200, {"ok": True, "complete": False, "received": received, "total": total})

        elif path.startswith("/img/"):
            # 服务图片二进制文件（仅允许 UUID 格式，防路径穿越）
            img_id = path[5:]
            if not re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$', img_id):
                self.send_error_body(400, "Invalid image id")
                return
            # H-3: 下载入口 ref+1 + 快照 entry（关闭 TOCTOU 窗口）
            entry = _acquire_file(img_id)
            if entry is None:
                self.send_error_body(404, "Not Found")
                return
            try:
                bin_path, mime_path = entry
                with open(mime_path, "r", encoding="utf-8") as f:
                    mime = f.read().strip()
                # C-4: 强校验 image/* 白名单，阻止 SVG/HTML 走 image 路径执行同源 JS
                if not re.match(r'^image/(png|jpe?g|gif|webp|bmp)$', mime, re.IGNORECASE):
                    self.send_error_body(415, f"Unsupported image type: {mime}")
                    return
                fsize = os.path.getsize(bin_path)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", 'attachment; filename="image"')  # 防 XSS 走 iframe
                self.send_header("Content-Length", str(fsize))
                self.send_header("Cache-Control", "no-store")
                # H-5: 拒绝跨域读图（避免外部页面 fetch 后渲染）
                self.send_header("Access-Control-Allow-Origin", "null")
                self.end_headers()
                # H-2: 流式复制
                with open(bin_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 64 * 1024)
                self.wfile.flush()
            finally:
                _release_file(img_id)

        elif path.startswith("/file/"):
            # 下载文件（仅允许 UUID 格式）
            file_id = path[6:]
            if not re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$', file_id):
                self.send_error_body(400, "Invalid file id")
                return
            # H-3: 下载入口 ref+1 + 快照 entry（关闭 TOCTOU 窗口）
            entry = _acquire_file(file_id)
            if entry is None:
                self.send_error_body(404, "Not Found")
                return
            try:
                fpath, fmeta = entry
                with open(fmeta, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                # C-5: filename 去 CRLF / 引号 / 分号，限制长度
                raw_name = meta.get("name", "download")
                safe_name = re.sub(r'[\r\n"\\;]', '_', str(raw_name))[:200] or "download"
                # Stage H (L10): RFC 5987 filename* 非 ASCII 百分号编码
                from urllib.parse import quote
                encoded_name = quote(safe_name, safe='')
                # 仅放行常见二进制 mime，避免任意 Content-Type 误用
                mime = meta.get("mime", "application/octet-stream")
                if not re.match(r'^(application|audio|video|text|image|font)/', mime, re.IGNORECASE):
                    mime = "application/octet-stream"
                fsize = os.path.getsize(fpath)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{encoded_name}')
                self.send_header("Content-Length", str(fsize))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "null")
                self.end_headers()
                with open(fpath, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 64 * 1024)
                self.wfile.flush()
            finally:
                _release_file(file_id)

        elif path == "/events":
            # Stage G (M7): SSE 容量检查移入锁内，避免并发穿透
            with _sse_lock:
                if len(sse_clients) >= MAX_SSE_CLIENTS:
                    self.send_error_body(503, "Too many connections")
                    return

            # H-5: SSE 拒绝跨域读取（无 Origin 表示 native / 同源；外部 Origin 一律拒绝）
            origin = self.headers.get("Origin")
            if origin and not self._origin_allowed(origin):
                self.send_error_body(403, "CORS denied")
                return

            # 解析设备信息
            params = parse_qs(parsed.query)
            dev_type = params.get("type", ["unknown"])[0]
            if dev_type not in ("pc", "mobile"):
                dev_type = "unknown"
            # Stage G (M2): dev_name 长度上限（与 /rename 一致），防持久化 JSON DoS
            dev_name = params.get("name", [dev_type])[0].strip()[:20]

            # 身份绑定：客户端传入 persistent_id 做身份 key
            client_ip = self.client_address[0]
            # R-08：pid 直接作为 identity_map 的持久化 key，限制长度避免 JSON 文件膨胀
            pid = params.get("pid", [""])[0][:128]
            my_name = params.get("my_name", [""])[0].strip()[:20]

            if pid:
                identity_key = pid
            else:
                # 兼容旧客户端：用 IP 做 fallback
                identity_key = f"{client_ip}_{dev_type}"

            # 查找或创建身份
            if identity_key in identity_map:
                info = identity_map[identity_key]
                device_id = info["device_id"]
                # Stage E: 即使身份复用，session_token 每次握手也是新 token
                # （旧连接 token 失效，新连接必须从 SSE event: device_id 取新值）
                session_token = secrets.token_hex(16)

                # Stage F (F5/H6): IP/MAC 校验 — 已知身份在换设备/IP 时拒绝
                # （article 飞递开发记_重写的版本.md 第 114-121 行承诺过 "IP 或 MAC
                # 对不上的话，不认"；此前从未实现）。
                old_ip = info.get("last_ip")
                old_mac = info.get("mac")
                current_mac_hash = None
                if client_ip not in ("127.0.0.1", "::1"):
                    raw_mac = get_mac(client_ip)
                    if raw_mac:
                        current_mac_hash = _hash_mac(raw_mac)
                # 本机测试 (loopback) 不强制 IP 校验；LAN 设备 IP 漂移是常态，
                # 若 last_ip 存在且不同则警告；MAC 严格（哈希必须匹配）
                if old_ip and old_ip != client_ip and client_ip not in ("127.0.0.1", "::1"):
                    print(f"[feidi] identity '{identity_key[:8]}' IP changed {old_ip} -> {client_ip}; accepting (LAN roaming)", flush=True)
                if old_mac and current_mac_hash and old_mac != current_mac_hash:
                    print(f"[feidi] identity '{identity_key[:8]}' MAC hash mismatch; refusing impersonation", flush=True)
                    self.send_error_body(403, "MAC mismatch for known identity")
                    return

                # C-06：更新已知身份字段必须持 _identity_lock，避免与 /rename 遍历、
                # _save_identities_flush 的快照并发 race（之前仅快照读持锁，写路径裸奔）。
                with _identity_lock:
                    if my_name:
                        dev_name = my_name
                        info["name"] = my_name
                    else:
                        dev_name = info.get("name", dev_name or dev_type)
                    # 更新元数据
                    info["last_ip"] = client_ip
                    info["last_seen"] = int(time.time())
                    info["type"] = dev_type
                    if current_mac_hash:
                        info["mac"] = current_mac_hash
            else:
                device_id = str(uuid.uuid4())[:8]
                # Stage E (H2/H10): 每个 SSE 连接发独立 128-bit session token，
                # 客户端后续 /send /rename 必须带 X-Feidi-Session: <token>，
                # 否则服务端不信任请求中的 device_id，避免 LAN 攻击者冒名发消息。
                session_token = secrets.token_hex(16)
                dev_name = my_name or dev_name or dev_type
                raw_mac = get_mac(client_ip) if client_ip not in ("127.0.0.1", "::1") else None
                mac = _hash_mac(raw_mac) if raw_mac else None
                # C-06：新增身份写入必须持 _identity_lock
                with _identity_lock:
                    identity_map[identity_key] = {
                        "device_id": device_id,
                        "name": dev_name,
                        "hostname": dev_name,
                        "last_ip": client_ip,
                        "mac": mac,
                        "type": dev_type,
                        "first_seen": int(time.time()),
                        "last_seen": int(time.time()),
                    }
            save_identities()

            # R-05：队列有上限（慢消费堆积到上限后广播侧会剔除该客户端）；
            # cancel 事件用于重连时取消旧连接；wfile 引用用于重连时主动断开旧 TCP。
            dev_info = {
                "queue": queue.Queue(maxsize=SSE_QUEUE_MAX),
                "device_id": device_id,
                "name": dev_name,
                "type": dev_type,
                "identity_key": identity_key,
                "session_token": session_token,
                "cancel": threading.Event(),
                "wfile": self.wfile,
            }

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            # H-5: 不再用 *；同源请求不带 Origin，反射请求的 Origin（如果允许）
            self.send_header("Access-Control-Allow-Origin", origin if origin and self._origin_allowed(origin) else "null")
            self.end_headers()
            self.wfile.flush()

            with _sse_lock:
                # 清除同 device_id 的旧连接（避免刷新/重连时设备列表出现重复）；
                # R-05：重连时置位旧连接的 cancel 事件并关闭其 wfile，旧 handler 线程
                # 不再空转直到 TCP 断开，可及时回收。
                for i in range(len(sse_clients) - 1, -1, -1):
                    if sse_clients[i].get("device_id") == device_id:
                        old = sse_clients.pop(i)
                        old["cancel"].set()
                        try:
                            old["wfile"].close()
                        except Exception:
                            pass
                sse_clients.append(dev_info)
            broadcast_device_list()

            # 发送设备身份和消息历史
            try:
                self.wfile.write(f"event: device_id\ndata: {json.dumps({'device_id': device_id, 'name': dev_name, 'type': dev_type, 'server_hostname': _server_hostname, 'identity_key': identity_key, 'session_token': session_token}, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
                history = json.dumps(_history_for_device(device_id), ensure_ascii=False)
                self.wfile.write(f"event: history\ndata: {history}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                with _sse_lock:
                    if dev_info in sse_clients:
                        sse_clients.remove(dev_info)
                broadcast_device_list()
                return

            try:
                while not dev_info["cancel"].is_set():
                    try:
                        data = dev_info["queue"].get(timeout=SSE_KEEPALIVE_TIMEOUT)
                        self.wfile.write(data.encode("utf-8"))
                        self.wfile.flush()
                    except queue.Empty:
                        if dev_info["cancel"].is_set():
                            break
                        self.wfile.write(": keepalive\n\n".encode("utf-8"))
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                dev_info["cancel"].set()
                with _sse_lock:
                    if dev_info in sse_clients:
                        sse_clients.remove(dev_info)
                broadcast_device_list()

        else:
            self.send_error_body(404, "Not Found")

    def do_POST(self):
        # C-01 修复：do_POST 内多处读+写模块级 ``_inflight_bytes``，需声明 global。
        global _inflight_bytes
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/login":
            # M-4: /login 同样加 rate-limit，限制 2 req/s/IP，避免弱密码被在线爆破
            client_ip = self.client_address[0]
            with _rate_lock:
                now = time.time()
                bucket = _rate_limits.get(client_ip, [])
                bucket = [t for t in bucket if now - t < RATE_WINDOW]
                if len(bucket) >= 2:
                    _rate_limits[client_ip] = bucket
                    self.send_error_json(429, "Too many requests")
                    return
                bucket.append(now)
                _rate_limits[client_ip] = bucket
            content_length = _parse_content_length(self.headers, max_allowed=1024)
            if content_length is None:
                # C-05：非法 Content-Length（如 'abc' / 负数 / 超限）直接 400，避免 int() 抛 500
                self.send_error_body(400, "Invalid or missing Content-Length")
                return
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_error_body(400, "Invalid JSON")
                return
            if not isinstance(data, dict):
                self.send_error_body(400, "Invalid JSON: expected object")
                return
            pw = data.get("password", "")
            if not isinstance(pw, str):
                # secrets.compare_digest raises TypeError on non-str;
                # reject here with a 400 so callers don't see a 500.
                self.send_error_body(400, "password must be a string")
                return
            if secrets.compare_digest(pw, PASSWORD):
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
                self.send_error_json(429, "Too many requests")
                return

            # Stage E (H2/H10): session token 必须先于 body 解析检查，
            # 减少无效请求的 CPU 开销（100MB body 在拒绝前不必读）。
            session_token = self.headers.get("X-Feidi-Session", "")
            # S-01：从 session 派生全部发送者身份（device_id/name/sender），
            # body 提供的身份字段必须与 session 一致，否则 403。
            sess = _session_identity(session_token)
            if not sess:
                self.send_error_body(401, "Missing or invalid session token")
                return

            content_length = _parse_content_length(self.headers, max_allowed=MAX_BODY_SIZE)
            if content_length is None:
                # C-05：非法 Content-Length 直接 400，避免 int() 抛 500
                self.send_error_json(400, "Invalid or missing Content-Length")
                return
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self.send_error_json(400, "Invalid JSON")
                return
            # C-03：JSON 顶层必须是 object（dict）。其它类型（旧 API
            # 直发 [] 或裸字符串）现在统一返回 400，不再让
            # data.get("...") 抛 AttributeError 杀掉连接。
            if not isinstance(data, dict):
                self.send_error_json(400, "Invalid JSON: expected object")
                return

            # S-01 校验：body 中若包含身份字段，必须等于 session 派生值。
            # 任一不一致立即 403，不进入业务分支。
            for field, sess_key in (("device_id", "device_id"),
                                    ("device_name", "name"),
                                    ("sender", "type")):
                provided = data.get(field)
                if provided is None:
                    continue
                if provided != sess[sess_key]:
                    self.send_error_json(
                        403, f"{field} does not match session")
                    return

            # 全部身份字段使用 session 派生值；body 即使未提供也安全。
            sender = sess["type"]
            dev_name = sess["name"]
            dev_id = sess["device_id"]
            target_id = data.get("target_id", None)  # None = 广播, str = 私聊目标
            # C-03：target_id 必须是 str 或 null；数字/数组/list
            # 都会在后续 _is_device_online(t) 抛 AttributeError。
            if target_id is not None and not isinstance(target_id, str):
                self.send_error_json(400, "target_id must be a string or null")
                return

            # Stage C (C1 续): 私聊目标离线前置检查 — 避免消息 append 后才发现离线而残留
            if target_id and not _is_device_online(target_id):
                self.send_error_json(404, "目标设备不在线")
                return

            # --- 分块传输模式 ---
            if "chunk_index" in data and "total_chunks" in data and "transfer_id" in data:
                # C-03：chunk_index 和 total_chunks 必须是整数；之前
                # int("x") 直接抛 ValueError 让 handler 死掉、连接断。
                try:
                    chunk_index = int(data["chunk_index"])
                    total_chunks = int(data["total_chunks"])
                except (ValueError, TypeError):
                    self.send_error_json(
                        400, "chunk_index/total_chunks must be integers")
                    return
                if not isinstance(data["transfer_id"], str):
                    self.send_error_json(400, "transfer_id must be a string")
                    return
                transfer_id = str(data["transfer_id"]).strip()
                chunk_b64 = data.get("chunk_data", "")

                # Stage F (F4): 同一 transfer_id 已完成则返回缓存的 msg_id，跳过整个
                # chunk 处理路径，避免重复 add_message 产生重复消息。
                with _chunk_lock:
                    cached_msg_id = completed_transfers.get(transfer_id)
                if cached_msg_id:
                    self.send_json(200, {"ok": True, "received": [], "complete": True, "msg_id": cached_msg_id, "duplicate": True})
                    return

                if not transfer_id or not re.match(r'^[a-zA-Z0-9\-_]+$', transfer_id):
                    self.send_error_json(400, "Invalid transfer_id")
                    return
                if chunk_index < 0 or chunk_index >= total_chunks or total_chunks > 10000:
                    self.send_error_json(400, "Invalid chunk index or total_chunks")
                    return
                # 按 decoded 字节数校验（之前只查 base64 长度，偏差约 33%）
                try:
                    chunk_bin = base64.b64decode(chunk_b64, validate=True) if chunk_b64 else b""
                except Exception:
                    self.send_error_json(400, "Invalid chunk base64")
                    return
                if len(chunk_bin) > CHUNK_SIZE_LIMIT:
                    self.send_error_json(413, "Chunk too large")
                    return

                _ensure_chunk_dir()
                transfer_dir = os.path.join(CHUNK_DIR, transfer_id)
                os.makedirs(transfer_dir, exist_ok=True)

                # 串行化对同一 transfer_id 的处理：避免 ct["chunks"].add 与 dict 覆盖在并发线程间损坏。
                # 也借此在新增 transfer 前拦截全局并发上限。所有写盘 / 状态翻转都在锁内，避免 2nd 审查的 HIGH：
                # 1) size-exceeded 清理后写盘导致 FileNotFoundError
                # 2) complete 在锁内算、锁外用导致双组装 → 重复消息
                with _chunk_lock:
                    if transfer_id not in chunk_transfers:
                        if len(chunk_transfers) >= MAX_CONCURRENT_TRANSFERS:
                            self.send_error_json(503, "Too many concurrent transfers")
                            return
                        # C-04：file_info 必须是 dict；size 必须是数字（bool 是 int 子类，需排除）。
                        # 畸形 body（file_info: "abc" 或 {"size": "abc"}）此前会让 ct_info.get() /
                        # 比较抛 AttributeError/TypeError → 500 断连。
                        ct_info = data.get("file_info")
                        if not isinstance(ct_info, dict):
                            ct_info = {"name": "unknown", "size": 0, "mime": "application/octet-stream"}
                        fsize = ct_info.get("size", 0)
                        if isinstance(fsize, bool) or not isinstance(fsize, (int, float)):
                            self.send_error_json(400, "file_info.size must be a number")
                            return
                        if fsize > MAX_CHUNKED_FILE:
                            self.send_error_json(413, f"File too large (max {MAX_CHUNKED_FILE // (1024*1024)}MB)")
                            return
                        # 防御性长度上限，避免超大 name/mime 撑爆持久化 JSON
                        ct_info["name"] = str(ct_info.get("name", "unknown"))[:200]
                        ct_info["mime"] = str(ct_info.get("mime", "application/octet-stream"))[:100]
                        if total_chunks > 10000:
                            self.send_error_json(400, "Too many chunks")
                            return
                        chunk_transfers[transfer_id] = {
                            "chunks": set(),
                            "total": total_chunks,
                            "info": ct_info,
                            "created": time.time(),
                            "sender": sender,
                            "device_name": dev_name,
                            "device_id": dev_id,
                            "target_id": target_id,
                            "is_image": data.get("image_type") is True,
                            "assembling": False,
                        }
                    ct = chunk_transfers[transfer_id]
                    if ct["device_id"] != dev_id:
                        self.send_error_json(403, "Transfer owned by another device")
                        return
                    if ct["assembling"]:
                        # 另一个线程已经判定 complete 并开始组装；这个请求视为重复 ACK
                        self.send_json(200, {"ok": True, "complete": True, "duplicate": True})
                        return
                    # Stage F (F1): 仅在 chunk_index 首次出现时累计 bytes_received，
                    # 避免重试 chunk 导致 size 虚高、超 MAX_CHUNKED_FILE 限。
                    is_new_chunk = chunk_index not in ct["chunks"]
                    # Stage F (F3): 全局字节配额。检查 + 增加原子（持 _inflight_lock）。
                    if is_new_chunk:
                        with _inflight_lock:
                            if _inflight_bytes + len(chunk_bin) > MAX_GLOBAL_INFLIGHT_BYTES:
                                self.send_error_json(503, "Global inflight byte quota exceeded")
                                return
                            _inflight_bytes += len(chunk_bin)
                    ct["chunks"].add(chunk_index)
                    received = sorted(ct["chunks"])
                    if is_new_chunk:
                        ct["bytes_received"] = ct.get("bytes_received", 0) + len(chunk_bin)
                    # Stage F (F2): 每次 chunk POST 刷新 last_activity，cleanup 基于此而非 created
                    ct["last_activity"] = time.time()
                    estimated_size = ct.get("bytes_received", 0)
                    if estimated_size > MAX_CHUNKED_FILE:
                        # 拒绝路径：清盘 + 摘条目 + 写盘仍在锁内所以安全
                        chunk_transfers.pop(transfer_id, None)
                        # 注意：要先发错误响应再 return 之前把 transfer_dir 删了；先写盘让目录存在
                        try:
                            with open(os.path.join(transfer_dir, f"{chunk_index}.chunk"), "wb") as f:
                                f.write(chunk_bin)
                                f.flush()
                                os.fsync(f.fileno())
                        except OSError:
                            pass
                        shutil.rmtree(transfer_dir, ignore_errors=True)
                        self.send_error_json(413, f"File too large (max {MAX_CHUNKED_FILE // (1024*1024)}MB)")
                        return
                    complete = len(received) == ct["total"]
                    is_image = ct["is_image"]
                    file_info = dict(ct["info"])
                    if complete:
                        # 立刻在锁内翻转 assembling 旗标，阻止后续重发的最后一块进入组装
                        ct["assembling"] = True

                # 写盘：先 fsync 再出锁
                with open(os.path.join(transfer_dir, f"{chunk_index}.chunk"), "wb") as f:
                    f.write(chunk_bin)
                    f.flush()
                    os.fsync(f.fileno())

                # Stage F (F6): 落盘后写 state.json，让重启/刷新可断点续传
                _save_chunk_state(transfer_id)

                if not complete:
                    self.send_json(200, {"ok": True, "received": received, "complete": False})
                    return

                # 组装：用流式拼接避免再读一遍（chunk 文件已是顺序的，append 即可）
                assembled_path = os.path.join(transfer_dir, "assembled.bin")
                total_size = 0
                try:
                    with open(assembled_path, "wb") as out:
                        for i in range(ct["total"]):
                            cp = os.path.join(transfer_dir, f"{i}.chunk")
                            if not os.path.isfile(cp):
                                raise FileNotFoundError(f"Missing chunk {i}")
                            with open(cp, "rb") as f:
                                while True:
                                    buf = f.read(1024 * 1024)
                                    if not buf:
                                        break
                                    out.write(buf)
                                    total_size += len(buf)
                        out.flush()
                        os.fsync(out.fileno())
                except Exception:
                    shutil.rmtree(transfer_dir, ignore_errors=True)
                    with _chunk_lock:
                        chunk_transfers.pop(transfer_id, None)
                    self.send_error_json(400, "Missing or unreadable chunk during assembly")
                    return

                # 最终 size 校验：实际字节数 vs 上限（C-3 第二道闸）
                if total_size > MAX_CHUNKED_FILE:
                    shutil.rmtree(transfer_dir, ignore_errors=True)
                    with _chunk_lock:
                        chunk_transfers.pop(transfer_id, None)
                    self.send_error_json(413, f"File too large (max {MAX_CHUNKED_FILE // (1024*1024)}MB)")
                    return

                finfo = dict(file_info)
                finfo["size"] = total_size

                if is_image:
                    mime = finfo.get("mime", "image/png")
                    approx_b64 = (total_size + 2) // 3 * 4
                    if approx_b64 + len(mime) + len("data:;base64,") > 5 * 1024 * 1024:
                        # R-06：大图经 src_path 交给 add_message 移动落盘，不再整块读内存
                        finfo["src_path"] = assembled_path
                        msg_id, _ = add_message("file", finfo, ct["sender"], ct["device_name"], ct["device_id"], ct["target_id"])
                    else:
                        with open(assembled_path, "rb") as f:
                            data_uri = f"data:{mime};base64,{base64.b64encode(f.read()).decode()}"
                        msg_id, _ = add_message("image", data_uri, ct["sender"], ct["device_name"], ct["device_id"], ct["target_id"])
                else:
                    # R-06：经 src_path 让 add_message 直接 move 组装文件到最终路径，
                    # 避免 500MB 整块读入内存（峰值仅磁盘一份副本）。
                    finfo["src_path"] = assembled_path
                    msg_id, _ = add_message("file", finfo, ct["sender"], ct["device_name"], ct["device_id"], ct["target_id"])

                # 收尾：删分块目录 + 摘条目
                shutil.rmtree(transfer_dir, ignore_errors=True)
                with _chunk_lock:
                    chunk_transfers.pop(transfer_id, None)
                    # Stage F (F4): 缓存完成状态，幂等接收重发请求
                    completed_transfers[transfer_id] = msg_id
                    # R-07：超过上限时淘汰最旧条目（dict 保序，FIFO），防内存无限增长
                    if len(completed_transfers) > COMPLETED_TRANSFERS_MAX:
                        completed_transfers.pop(next(iter(completed_transfers)), None)
                    # Stage F (F3): 扣减 in-flight
                    with _inflight_lock:
                        _inflight_bytes = max(0, _inflight_bytes - ct.get("bytes_received", 0))

                self.send_json(200, {"ok": True, "received": received, "complete": True, "msg_id": msg_id})
                return  # 分块模式直接返回，不走后续逻辑
            # --- 分块模式结束 ---

            if "text" in data and data["text"]:
                text = data["text"]
                # C-03：text 必须为字符串；数字/列表会让 len() 抛 TypeError。
                if not isinstance(text, str):
                    self.send_error_json(400, "text must be a string")
                    return
                if len(text) > 10000:
                    self.send_error_json(413, "Text too long (max 10000 chars)")
                    return
                msg_id, target_ok = add_message("text", text, sender, dev_name, dev_id, target_id)
            elif "image" in data and data["image"]:
                img_data = data["image"]
                # C-03：image 必须是字符串（data URI 形态）；数字/列表
                # 都会让 add_message 内的 startswith/base64 抛异常。
                if not isinstance(img_data, str):
                    self.send_error_json(400, "image must be a string")
                    return
                if len(img_data) > 5 * 1024 * 1024:
                    self.send_error_json(413, "Image too large (max 5MB)")
                    return
                # C-08：data:image 前缀大小写不敏感（与 add_message 的 partition 宽松路径对齐）
                if not img_data.lower().startswith("data:image/"):
                    self.send_error_json(400, "Only data:image/... URIs accepted")
                    return
                msg_id, target_ok = add_message("image", img_data, sender, dev_name, dev_id, target_id)
            elif "file" in data and data["file"]:
                file_data = data["file"]
                if not isinstance(file_data, dict):
                    self.send_error_json(400, "Invalid file data")
                    return
                # S-09：不再信任客户端声明的 size。base64 解码后按实际字节数限 50MB，
                # 否则可声明小 size 绕过门槛、实际存下约 75MB（受 100MB body 上限约束）。
                fb64 = file_data.get("data", "")
                try:
                    fbin = base64.b64decode(fb64) if fb64 else b""
                except Exception:
                    self.send_error_json(400, "Invalid base64 in file data")
                    return
                if len(fbin) > 50 * 1024 * 1024:
                    self.send_error_json(413, "File too large (max 50MB)")
                    return
                # 用实际解码字节数覆盖声明 size，并规范 name/mime 长度
                file_data = dict(file_data)
                file_data["size"] = len(fbin)
                file_data["name"] = str(file_data.get("name", "unknown"))[:200]
                file_data["mime"] = str(file_data.get("mime", "application/octet-stream"))[:100]
                msg_id, target_ok = add_message("file", file_data, sender, dev_name, dev_id, target_id)
            else:
                self.send_error_json(400, "No text, image or file")
                return

            if target_id and not target_ok:
                self.send_error_json(404, "目标设备不在线")
                return

            self.send_json(200, {"ok": True, "msg_id": msg_id})

        else:
            self.send_error_body(404, "Not Found")

    def do_OPTIONS(self):
        self.send_response(200)
        # H-5: 预检：仅当请求 Origin 命中允许列表时回显，否则一律拒绝
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.flush()

    @staticmethod
    def _origin_allowed(origin: str) -> bool:
        """仅允许本机/局域网实际 host 的 Origin，跨域一律拒绝（S-08 收紧）。

        旧实现 ipaddress.ip_address(host) 成功即返回 True —— 任意合法 IP（其他 LAN
        主机、公网 IP）托管的网页都能拿到 Access-Control-Allow-Origin 反射，并可跨域
        读取 /events SSE。现在只允许 _allowed_origin_hosts 中的主机（由 main() 在
        启动时填入 LOCAL_IP / BIND_HOST / hostname + localhost）。
        """
        if not origin:
            return False
        try:
            host = urlparse(origin).hostname
        except Exception:
            return False
        if not host:
            return False
        return host in _allowed_origin_hosts


def kill_old_instance(port):
    """检查端口是否被旧的飞递进程占用，是则自动终止。不会误杀其他程序（H-7：精确匹配）。"""
    feidi_names = {"feidi.exe", "feidi-macos", "feidi", "transfer.exe", "transfer.py"}
    try:
        if sys.platform == "win32":
            # Windows: netstat + tasklist（按 ImageName 精确匹配）
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = parts[-1]
                    r2 = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                        capture_output=True, text=True, timeout=5)
                    first_line = (r2.stdout.splitlines() or [""])[0]
                    image = first_line.split(",", 1)[0].strip().strip('"').lower()
                    if image in feidi_names:
                        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=5)
                        print(f"  \033[90m已终止旧的飞递进程 (PID: {pid}, {image})\033[0m")
                        time.sleep(POST_KILL_GRACE)
                        return True
        else:
            # macOS / Linux: lsof + ps 提取 argv[0]
            r = subprocess.run(["lsof", "-ti:%d" % port], capture_output=True, text=True, timeout=5)
            for pid in r.stdout.strip().split():
                if not pid:
                    continue
                r2 = subprocess.run(["ps", "-p", pid, "-o", "comm="], capture_output=True, text=True, timeout=3)
                comm = r2.stdout.strip().lower()
                r3 = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True, timeout=3)
                cmd = r3.stdout.strip().lower()
                # 仅精确匹配 comm / argv0 ∈ feidi_names（S-07：删除 "transfer.py" in cmd /
                # "/feidi" in cmd 子串分支，避免误杀同机其他含该子串的 Python 进程）。
                argv0 = cmd.split()[0] if cmd else ""
                if comm in feidi_names or argv0 in feidi_names:
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except (ProcessLookupError, PermissionError) as e:
                        # 进程在我们查 lsof 之后已经退出/被回收；不算错
                        print(f"[feidi] kill_old_instance: {e}", flush=True)
                        continue
                    print(f"  \033[90m已终止旧的飞递进程 (PID: {pid}, {comm or argv0})\033[0m")
                    time.sleep(POST_KILL_GRACE)
                    return True
    except Exception:
        pass
    return False


def main():
    global _allowed_origin_hosts
    local_ip = get_local_ip()
    # S-08：把本机实际暴露的 host 加入 CORS 允许列表（网页客户端同源 Origin 才被反射）
    _allowed_origin_hosts = {"127.0.0.1", "localhost", "::1", local_ip}
    if BIND_HOST:
        _allowed_origin_hosts.add(BIND_HOST)
    try:
        _allowed_origin_hosts.add(socket.gethostname())
    except Exception:
        pass
    url = f"http://{local_ip}:{PORT}"
    mobile_url = url + "/mobile"
    if PASSWORD:
        mobile_url += "?auth=required"

    print("-" * 52)
    print("  飞递 Feidi - 局域网传输工具")
    print("-" * 52)
    print(f"  电脑端:  {url}")
    print(f"  手机端:  {mobile_url}")
    if PASSWORD:
        print("  密码保护: 已启用（访问时需输入密码）")
    print(f"  按 Ctrl+C 停止")
    print("-" * 52)
    print("  \033[93m提示:\033[0m 手机扫码后若无法打开，请检查：")
    print("    1. 手机与电脑是否在同一 Wi-Fi")
    print("    2. Windows 防火墙是否放行了端口", PORT)
    print("       \033[90m(以管理员运行: netsh advfirewall firewall add rule")
    print(f"        name=\"Feidi\" dir=in action=allow protocol=TCP localport={PORT})\033[0m")
    print("-" * 52)

    kill_old_instance(PORT)

    try:
        server = ThreadingHTTPServer((BIND_HOST or get_local_ip(), PORT), RequestHandler)
        # 配置 TCP keepalive，快速检测断开的连接
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform == "darwin":
            # macOS: TCP_KEEPALIVE = 0x10 (idle seconds before probe)
            server.socket.setsockopt(socket.IPPROTO_TCP, 0x10, 30)   # 30秒空闲后开始探测
        elif sys.platform.startswith("linux"):
            server.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        # 探测间隔和次数（跨平台通用）
        if sys.platform in ("darwin", "linux"):
            try:
                server.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)   # 5秒间隔
                server.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)     # 3次探测失败即断开
            except (OSError, AttributeError):
                pass  # 某些 Python 版本可能不支持这些选项
    except OSError as e:
        if e.errno == 48 or e.errno == 10048:  # Address already in use
            print(f"\n  \033[91m端口 {PORT} 已被占用，且不是飞递进程。\033[0m")
            print(f"  请手动终止占用进程，或使用 --port 换个端口")
            sys.exit(1)
        raise
    if not NO_BROWSER:
        print("\n服务已启动，浏览器将自动打开...")
        webbrowser.open(url)
    else:
        print("\n服务已启动")

    try:
        # 启动过期分块清理线程（每 CLEANUP_INTERVAL 秒清理一次）
        _startup_cleanup()
        # Stage F (F6): 启动时恢复 in-flight 分块传输（按 7 天 TTL 过滤）
        _load_chunk_states()
        cleanup_thread = threading.Thread(target=_periodic_cleanup_loop, daemon=True)
        cleanup_thread.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _server_stopped = True
        server.server_close()
        print("\n已关闭，临时文件已清理")


if __name__ == "__main__":
    main()
