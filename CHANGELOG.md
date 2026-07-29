# Changelog / 更新日志

All notable changes to Feidi will be documented in this file.

Feidi 的所有重要变更都记录在此文件。

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
项目遵循 [语义化版本](https://semver.org/lang/zh-CN/spec/v2.0.0.html)。

---

## [Unreleased]

### Added / 新增

- **UI language toggle (zh / en)** / **UI 双语切换 (中 / EN)**
  - **ZH**: 状态栏右上角加 `中 | EN` 切换按钮,选择存 `localStorage`;首次访问根据 URL `?lang=` 选择;客户端 JS 字典即时替换 `data-i18n` 标注的字符串。服务器端 `_pick_lang(query)` 探测初始语言。
  - **EN**: `中 | EN` switcher in status bar; choice persisted in `localStorage`; first-visit lang from URL `?lang=`; client-side JS dictionary swaps `data-i18n` strings on load. Server-side `_pick_lang(query)` picks initial lang.
  - 当前覆盖关键错误/状态字符串(密码、登录、连接、设备、离线);完整字符串翻译留待后续 PR。

### Changed / 变更

- **README 双语化** / **README bilingual**: `README.md` (zh) + `README.en.md` (en),互相链向
- **`<html lang>` 改为 `__LANG__` 占位符**,运行时由服务器注入;客户端 JS 也会刷新该属性

### Infrastructure / 基础设施

- **正式 release 流程** / **Formal release process** (对齐 ClipMemory):
  - `CHANGELOG.md` 双语 Keep-a-Changelog 格式
  - `docs/RELEASE_PUSH_CHECKLIST.md` 5 段勾选清单 (A/B/C/D/E)
  - `docs/release-notes-template.md` 模板
  - `Scripts/pre_push_verify.sh` 本地预检查脚本
  - `.github/workflows/build.yml`: push → nightly, tag `v*` → 正式 release (prerelease=false)
  - pre-commit hook 收紧: `password` 字段收窄为字面量匹配;非交互场景自动放行
  - release `89feefa` + `1f6c77e` 落地;`v1.1.0` 已发布 (8 个 audit-fix commit + 文档基础设施)

---

## [1.1.0] - 2026-07-29 — Security & Reliability Audit Remediation / 安全与可靠性审计修复

## [1.1.0] - 2026-07-29 — Security & Reliability Audit Remediation / 安全与可靠性审计修复

> **ZH** 一次全量代码审计后落地 8 个原子 commit，覆盖私聊历史、文件引用、身份认证、断点续传、SSE 安全、IP/MAC 校验、内存配额、密钥存储、文档真实化。
>
> **EN** Eight atomic commits landed after a comprehensive code audit, covering private-chat history, file references, identity binding, resumable uploads, SSE security, IP/MAC verification, memory quota, secret storage, and documentation accuracy.

### Security / 安全

#### Critical

- **C1 — Private chat history no longer leaks to other devices** (`6a565fd`)
  - **ZH**: 私聊消息历史对全员可见 → 服务端 `_history_for_device` 按 device_id 过滤(广播 + 自己发出的 + 以此为私聊目标)
  - **EN**: Private messages were visible in every SSE `event: history` → server now filters `_history_for_device` by device_id (broadcast + own messages + private targets only)
- **C2 — File ref-count no longer deletes attachment after first download** (`ca5c756`)
  - **ZH**: `_release_file` 在 ref=0 时无条件删盘,导致多设备文件传输失效 → 与 `_cleanup_msg_files` 对称同步 pop MSG_FILES
  - **EN**: `_release_file` deleted backing files on ref=0 unconditionally, breaking multi-device file transfer → now symmetric with `_cleanup_msg_files`, pops MSG_FILES on completion
- **H2 + H10 — `device_id` server-bound via per-session SSE bearer token** (`ee79c91`)
  - **ZH**: `device_id` 仅 32-bit 熵且完全客户端断言 → SSE 握手下发 `secrets.token_hex(16)` session_token,`/send` 与 `/rename` 必须携带 `X-Feidi-Session` 头
  - **EN**: `device_id` was 32-bit entropy and client-asserted → SSE handshake issues `secrets.token_hex(16)` session_token; `/send` and `/rename` now require `X-Feidi-Session` header
- **H6 — IP/MAC verification on identity reuse** (`185e006`)
  - **ZH**: 文章承诺过 "IP 或 MAC 对不上的话不认",从未实现 → MAC hash 不匹配返 403
  - **EN**: The dev-article promised IP/MAC mismatch refusal but it was never implemented → MAC hash mismatch now returns 403

#### Medium / 中等

- **M5 — `get_mac()` result cached for 5 minutes** (`59da3a4`)
  - **ZH**: 每次 SSE 重连都跑 `arp` 子进程 → 5 分钟 TTL 缓存
  - **EN**: Each SSE reconnect spawned an `arp` subprocess → 5-min TTL cache
- **M7 — `MAX_SSE_CLIENTS` check moved inside `_sse_lock`** (`59da3a4`)
  - **ZH**: 容量检查在锁外,多个连接同时通过 → 移入锁内
  - **EN**: Capacity check was outside the lock; concurrent connections could pass → moved inside
- **M10 — UUID regex tightened to canonical v4 layout** (`59da3a4`)
  - **ZH**: 旧正则 `^[a-f0-9-]+$` 接受任意长度 → 改为完整 v4 布局校验
  - **EN**: Old regex `^[a-f0-9-]+$` accepted any length → canonical v4 layout enforced
- **L8 — `Secure` cookie flag when behind HTTPS reverse proxy** (`9997587`)
  - **ZH**: Cookie 永远无 Secure → 检测 `X-Forwarded-Proto=https` 自动加 Secure
  - **EN**: Cookie never had `Secure` → now added when `X-Forwarded-Proto=https` detected

### Reliability / 可靠性

- **H3 — Real resumable upload (server-side state persistence)** (`185e006`)
  - **ZH**: "断点续传" 只是页内短时重试 → 服务端 state.json 持久化,7 天 TTL,启动时 `_load_chunk_states` 恢复,新增 `GET /upload/status/<id>` 端点
  - **EN**: "Resumable upload" was just in-page retry → server-side `state.json` with 7-day TTL, startup recovery via `_load_chunk_states`, new `GET /upload/status/<id>` endpoint
- **H4 — `bytes_received` only accumulates for new chunks** (`185e006`)
  - **ZH**: 重试 chunk 重复累计 size 虚高,可突破 500MB 上限 → 仅在 chunk_index 首次出现时累计
  - **EN**: Retried chunks inflated size, could exceed 500MB limit → only first occurrence counts
- **H5 — Transfer idempotency via `completed_transfers` cache** (`185e006`)
  - **ZH**: 完成响应丢失 → 客户端重发同 transfer_id 产生重复消息 → 缓存 `transfer_id → msg_id`,重发直接返回
  - **EN**: Lost completion response → client retry created duplicate messages → cache `transfer_id → msg_id`; replays return cached
- **H7 — Chunk cleanup holds `_chunk_lock` and uses `last_activity`** (`185e006`)
  - **ZH**: 清理无锁可与活跃上传竞态,且基于 `created` → 持锁 + 基于 `last_activity`(每次 chunk 刷新)
  - **EN**: Cleanup was unlocked and `created`-based → now locked and `last_activity`-based (refreshed per chunk)
- **H8 — Global in-flight byte quota** (`185e006`)
  - **ZH**: 并发大文件可撑爆内存/磁盘 → `MAX_GLOBAL_INFLIGHT_BYTES=500MB`,`_inflight_lock` 原子追踪
  - **EN**: Concurrent large files could OOM → `MAX_GLOBAL_INFLIGHT_BYTES=500MB`, atomic `_inflight_lock` tracking

### Architecture / 架构

- **H1 — `identity_map` access protected by `_identity_lock`** (`59da3a4`)
  - **ZH**: 多线程跨 handler 自由读写 race → 锁保护持久化
  - **EN**: Multi-thread cross-handler race → locked persistence
- **M2 — SSE `dev_name` length cap (20 chars)** (`59da3a4`)
  - **ZH**: 持久化前无长度上限,大字符串可撑爆 `feidi_identities.json`
  - **EN**: No length cap before persistence; large strings could bloat `feidi_identities.json`

### Code Hygiene / 卫生

- **C3 — CLI `--password` flag added** (`932b102`)
  - **ZH**: README 写 `--password`,实为 `--pass` → 双标志(同 dest)
  - **EN**: README says `--password`, code only accepts `--pass` → both flags, same dest
- **C4 — PC login overlay added** (`7bda743`)
  - **ZH**: PC 端无密码输入 UI,只能从 `/mobile` 登录 → PC HTML 加入遮罩 + SSE onerror 401 检测
  - **EN**: PC client had no login UI → overlay added; SSE 401 detection triggers it
- **D — `Math.random()` replaced with `crypto.getRandomValues` UUID v4** (`ca5c756`)
  - **ZH**: `PERSISTENT_ID` 可预测 → CSPRNG
  - **EN**: `PERSISTENT_ID` was predictable → CSPRNG
- **L10 — `Content-Disposition` filename with RFC 5987 `filename*=`** (`9997587`)
  - **ZH**: 仅 `filename=`,非 ASCII 乱码 → 加 `filename*=UTF-8''<encoded>`;并去掉 `;`
  - **EN**: Only `filename=`, non-ASCII garbled → added `filename*=UTF-8''<encoded>`; `;` stripped

### Documentation / 文档

- **C5 — README security section rewritten** (`932b102`)
  - **ZH**: 删 "SHA-256 哈希" 错误描述,改为"随机 token + HttpOnly Cookie"
  - **EN**: Removed false "SHA-256" claim, now "random token + HttpOnly Cookie"
- **M12/M13 — Download filenames and pack commands fixed** (`932b102`)
  - **ZH**: `Feidi-win.exe` / `Feidi-macos` 改为实际产物 `Feidi.exe` / `Feidi-macos.zip`;打包命令改为用 `build.spec` / `build_mac.spec`
  - **EN**: `Feidi-win.exe` / `Feidi-macos` → `Feidi.exe` / `Feidi-macos.zip`; pack commands use `build.spec` / `build_mac.spec`

### Removed / 移除

- **`?code=<8hex>` QR connection code** (`932b102`)
  - **ZH**: 服务端从未校验,改文案为 `?auth=required`
  - **EN**: Never validated server-side; replaced with `?auth=required` hint
- **"连接码: 8hex" 启动横幅** (`932b102`)
  - **ZH**: 误导用户以为 8 字符是密码;实际只是 token 截断
  - **EN**: Misled users into thinking the 8-char string was the password
- **Inner duplicate `import base64` / `import threading`** (`932b102`)
  - **ZH**: 函数内重复 import 顶层已导入的模块
  - **EN**: Functions re-imported top-level modules

---

## [1.0.0] - 2026-06-14 — Initial Public Release / 首次公开发布

- **EN** First public release on GitHub.
- **ZH** 在 GitHub 上首次公开发布。

Features:
- LAN-only HTTP server (zero pip runtime deps, vendored QR library)
- PC serves / mobile scans QR to connect
- Cross-platform: Windows / macOS / Linux (via PyInstaller)
- Private chat + broadcast, with per-device session targeting
- Arbitrary file transfer up to 500 MB, chunked 1 MB per request
- Drag-and-drop file send on PC
- Dark mode (manual toggle or system preference)
- Browser notifications + title flash + Toast on incoming messages
- Device naming (self + remarks for others)
- Ephemeral temp files (auto-clean on exit)
- Optional password protection via random token + HttpOnly cookie

---

[Unreleased]: https://github.com/irykelee/Feidi/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/irykelee/Feidi/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/irykelee/Feidi/releases/tag/v1.0.0