# Feidi 代码与文档审查报告

> 审查日期：2026-08-01
> 审查基线：working tree（`26b97bc` + 未提交的审计修复 diff：`transfer.py` +186/-34，含 C-01/C-02/C-03/R-04/S-01 等修复）
> 审查性质：只读审查；本报告未修改任何源文件或文档。
> 覆盖范围：transfer.py、qrcode_lib/、README(.en).md、CHANGELOG.md、docs/、构建脚本、CI、tests/、开发记文章、feidi_identities.json。
> 相对 2026-07-30 报告：本文为增量复核 + 新发现；ID 编号接续 07-30 报告（C-04 起 / S-07 起 / R-05 起 / D-04 起 / A-07 起 / B-01 起 / Q-03 起 / L-02 起）。

## 1. 结论摘要

**好消息是 07-30 报告的 P0 已根治，P1 大部分已落地。** 工作区未提交的修复 diff 针对 C-01（`global _inflight_bytes` 三处）、C-02（启动顺序 + 磁盘一致性校验）、S-01（`_session_identity` 派生身份 + body 不一致 403）、C-03（JSON 顶层 / 密码 / chunk 整数 / target_id / text / image 类型校验）、R-04（rate-limit 容量治理）均有真实代码落地，且配套新增了 `tests/test_c01_inflight_quota.py`、`test_c02_startup_recovery.py`、`test_c03_r04_validation_and_limits.py`、`test_s01_session_identity.py` 四个回归测试文件（子进程 + 真实 HTTP 断言），质量门从"零测试"前进了一大步。CHANGELOG [1.1.0] 的 20+ 项声明中绝大部分可逐条定位到代码。

**但发布质量仍不合格，主要有三类问题：**

1. **P1 级残留未动**：`kill_old_instance` 的 `"transfer.py" in cmd` / `"/feidi" in cmd` 子串匹配（`transfer.py:3731`）仍在，可误杀同网段其他 Python 进程；`_origin_allowed` 仍对任意合法 IP 反射 origin（S-03 未修）；SSE 队列无界且同设备重连不取消旧连接（R-01/R-02 未修）；500MB 组装文件仍整块 `f.read()` 进内存（R-03 未修）；非分块文件仍信任客户端声明的 `size`（S-04 未修）。
2. **"修了一半"的项**：C-03 校验补齐了常见类型，但 `file_info` 非 dict / `file_info.size` 非数字（`transfer.py:3471-3474`）与非法 `Content-Length` 头（`:3324, :3371`）仍会抛 500；H1 的 `_identity_lock` 只覆盖了快照读取，SSE 握手与 `/rename` 的写路径完全无锁；CHANGELOG 声称"移除函数内重复 import"，`_pick_lang` 内 `import re as _re`（`:671`）仍在。
3. **文档与代码的偏差未收敛**：`__version__ = "1.0.1-audit"`（`:33`）与 CHANGELOG v1.1.0 不一致；README.md:17 仍写"SHA-256 Cookie 认证"（CHANGELOG C5 声称已删）；CHANGELOG [1.1.0] 标题重复（42/44 行）；`start.bat` 仍不传 CLI 参数（D-03 声称已修）；README.md 缺 `--bind` 参数表（en 版有）。

**结论**：核心功能（分块上传、断点续传、会话身份绑定）的架构性故障已修复，但"发布到 GitHub 正式 tag"前至少应先处理第 1、2 类的 P1 项与版本/文档同步；建议把工作区未提交 diff 先拆成原子 commit 落地，再执行 `Scripts/pre_push_verify.sh`（当前"Working tree clean"一项必然失败）。

## 2. 问题汇总表

> 证据等级：`已确认`（代码路径 + 静态检查）/ `条件性已确认`（需特定流量或环境触发）/ `待核实`。

| ID | 优先级 | 证据 | 类别 | 位置(file:line) | 问题与影响 | 首选修复建议 |
|---|---|---|---|---|---|---|
| C-04 | P1 | 已确认 | 输入验证 | transfer.py:3471-3474 | `file_info` 非 dict 或 `size` 非数字时 `ct_info.get()` / 比较抛 AttributeError/TypeError → 500 断连（C-03 修复不完整） | `isinstance(ct_info, dict)` + `isinstance(fsize, (int,float))` 校验，统一 400/413 |
| C-05 | P1 | 已确认 | 输入验证 | transfer.py:3324,3371 | 恶意 `Content-Length: abc` 使 `int()` 抛 ValueError → 500（同上，C-03 不完整） | try/except int() 或正则预校验，非法即 400 |
| C-06 | P1 | 已确认 | 并发/正确性 | transfer.py:3194-3250,3056-3059 | `identity_map` 写路径（SSE 握手新增/更新、/rename 遍历）无 `_identity_lock`；与 `_save_identities_flush` 的 `json.dumps` 快照竞态，`for ... items()` 迭代中改 dict 抛 RuntimeError（H1 部分实现） | 所有读写 `identity_map` 的路径统一持 `_identity_lock`（含握手与 /rename） |
| S-07 | P1 | 已确认 | 进程管理 | transfer.py:3731 | `kill_old_instance` 仍用 `"transfer.py" in cmd or "/feidi" in cmd` 子串匹配（S-06 声称修复未完全）；路径含 transfer.py 的其他 Python 进程会被 SIGTERM | 仅精确匹配 argv[0]/comm ∈ feidi_names，去掉子串分支 |
| S-08 | P1 | 条件性已确认 | CORS/隐私 | transfer.py:3680-3696 | `_origin_allowed` 对**任意合法 IP** 返回 True；其他 LAN 主机/公网 IP 上的网页可获 origin 反射与 SSE 读取（S-03 未修复） | 只允许 server 实际 host、localhost 与受信列表；不把"合法 IP"当信任边界 |
| R-05 | P1 | 已确认 | 资源/生命周期 | transfer.py:3252,3263-3268 | SSE `queue.Queue()` 无界（put_nowait 永不阻塞）+ 同 device_id 重连仅 pop 列表项、旧 handler 线程的 while 循环不取消（R-01/R-02 未修复） | 有界队列 + 慢消费断开；替换连接时 set cancel event 并关闭旧 wfile |
| R-06 | P1 | 已确认 | 性能/内存 | transfer.py:3591-3601 | 组装完成后 `f.read()` 整块读入内存（≤500MB），峰值 = 内存 500MB + 多份磁盘副本（R-03 未修复） | 流式复制/rename 到最终路径，add_message 支持直接引用 assembled 文件 |
| S-09 | P1 | 已确认 | 配额/输入 | transfer.py:3647-3649 | 非分块文件用客户端声明 `size` 做 50MB 检查，base64 实际解码可超（≤100MB body 硬顶内 ≈75MB）（S-04 未修复） | 解码后按实际字节数限制；name/mime 加长度上限 |
| S-10 | P1 | 条件性已确认 | 网络暴露 | transfer.py:540-542 | `get_local_ip` 无私网地址时仍选择公网地址作默认 bind（S-02 未修复；--bind 已提供 opt-in） | 默认候选仅 RFC1918/loopback；无私网回退 127.0.0.1 并警告 |
| A-07 | P1 | 已确认 | WCAG | transfer.py:1462-1468,2117,2080-2088 | 设备列表 `div` 无键盘语义（2.1.1）；Mobile viewport `maximum-scale=1,user-scalable=no`（1.4.4）；登录遮罩无 role/focus trap/Escape（A-01/02/03 未修复） | div→button；删缩放限制；补 dialog 语义 |
| L-02 | P2 | 已确认 | 生命周期 | transfer.py:3811,167 | `_server_stopped = True` 未定义也未读取（无 global、无读方）；`_server_stop_event` 从未 `.set()`，清理线程无法干净停止（L-01 未修复） | finally 中 `_server_stop_event.set()`；统一 shutdown 顺序 |
| D-04 | P2 | 已确认 | 持久化 | transfer.py:217-226,266-270 | `save_identities` 5s debounce 无 shutdown flush；立即退出丢改名/新身份（D-01 未修复） | 退出时 cancel timer + 同步 flush |
| R-07 | P2 | 已确认 | 资源 | transfer.py:3609 | `completed_transfers` 只增不减（清理只随 chunk_transfers 走，完成后永不命中），无 TTL/上限（R-04 部分） | 独立 TTL/上限，周期清理 |
| R-08 | P2 | 已确认 | 输入边界 | transfer.py:3184-3191 | `/events?pid=` 无长度限制，任意长字符串成为 `identity_map` key → `feidi_identities.json` 膨胀 | pid 上限（如 128 字符）+ 白名单字符集 |
| D-05 | P2 | 条件性已确认 | 持久化 | transfer.py:792-795 | `fmeta` 先 `flush/fsync` 再 `json.dump`，尾部无 fsync；崩溃窗口 metadata 空/截断（D-02 未修复） | 先写完再 fsync；临时文件 + os.replace |
| D-06 | P2 | 已确认 | 文档/发布 | transfer.py:33,README.md:17,CHANGELOG.md:42-44,start.bat:13-19 | `__version__="1.0.1-audit"` vs CHANGELOG 1.1.0；README 残留 "SHA-256"；CHANGELOG 标题重复；start.bat 不传 `%*`（D-03 部分） | 版本统一；README 改"随机 token"；去重复 heading；`%*` |
| B-01 | P2 | 已确认 | CI/供应链 | .github/workflows/build.yml:12,18-19,26,36-37,47,59,62,82 | workflow 全局 `contents: write`；actions 全部按 major tag 未 pin SHA（Q-02 未修复） | job 级最小权限；pin SHA |
| Q-03 | P2 | 已确认 | 测试/卫生 | tests/test_c01_*.py:167-173 等 | 回归测试经 SSE 握手会向 `feidi_identities.json` 追加真实条目（无恢复机制）；`tests/_logs/` 未 gitignore | 测试用 `--port` 独立身份文件副本，或握手后清理；`_logs` 加 gitignore |
| V-01 | P2 | 已确认 | vendored 合规 | qrcode_lib/（全部） | vendored qrcode（特征为 7.4.x，含 LUT.py/styles/release.py）无版本标识、无随附 LICENSE 文件（BSD-3-Clause 要求保留版权声明） | 记录上游版本 + 随附 LICENSE + provenance |
| A-08 | P2 | 已确认 | i18n | transfer.py:2013-2024,2862-2869 | I18N 字典仅 9 个 key；大量菜单/弹窗/toast/文件确认框仍是中文（A-05 未修复；CHANGELOG 已如实声明"留待后续"） | 全量抽取或暂隐藏切换 |
| A-09 | P2 | 已确认 | Responsive | transfer.py:930-999 | PC 无任何 `@media` breakpoint，窄于约 640px 横向溢出（A-04 未修复） | 加 breakpoint，纵向布局 |
| C-07 | P3 | 已确认 | 卫生 | transfer.py:671 | `_pick_lang` 内 `import re as _re` —— CHANGELOG "移除函数内重复 import"未完全（顶层已 import re） | 删函数内 import |
| B-02 | P3 | 已确认 | 构建 | build.spec:46, build_mac.spec:62 | 仓库有 `icon.svg` 但两个 spec 的 `icon=None` | 生成 .ico/.icns 并配置 |
| C-08 | P3 | 已确认 | 输入边界 | transfer.py:3638 vs 743 | 非分块 image 要求小写 `data:image/` 前缀，与 `add_message` 的 partition 宽松路径行为不一致（大写 DATA: 被 400） | 统一大小写处理 |
| D-07 | P3 | 已确认 | 文档 | README.md:42-50 vs README.en.md:42-51 | README.md 缺 `--bind` 参数（en 有）；en 版 "~3200 lines" 过时（实际 3817）；"1MB 分块"未说明服务端 2MB/块上限 | 中英参数表对齐；行数更新 |
| D-08 | P3 | 已确认 | 文档 | CHANGELOG.md:114-116 vs transfer.py:1549-1558 | C4 声称 "SSE 401 检测"，实现是 `/status` 403 检测 | 措辞改为 403 |
| Q-04 | P3 | 已确认 | 静态告警 | transfer.py:3760,3790 | ruff F541：两个无占位符 f-string | 去掉 f 前缀 |
| D-09 | P3 | 已确认 | 文档精度 | transfer.py:3314-3323,612-627 | `/login` 与 `/send` 共用 `_rate_limits` 滑动窗口（login 请求同时占 5 req/s 窗口），README "单独 2 req/s" 语义略超卖 | 独立 login 计数器或文档注明共享窗口 |

## 3. 详细分析

### C-04 — `file_info` 字段类型未校验，畸形 body 仍打穿 handler（P1）

- 根因：`do_POST` 分块分支读取 `data.get("file_info")` 后直接 `ct_info.get("size", 0)`，未校验 `file_info` 是 dict；`fsize > MAX_CHUNKED_FILE` 也未校验 `fsize` 数值类型。`file_info: "abc"` → `str.get` AttributeError；`file_info: {"size": "abc"}` → `"abc" > int` TypeError。均穿出 handler 导致 500/断连——C-03 的输入校验清单漏了这里。
- 证据：`transfer.py:3471-3475`
  ```python
  ct_info = data.get("file_info") or {"name": "unknown", "size": 0, "mime": "application/octet-stream"}
  fsize = ct_info.get("size", 0)
  if fsize > MAX_CHUNKED_FILE:
  ```
- 影响：畸形请求引发服务端异常与日志噪声；合法客户端不受影响。
- 修复建议：`if not isinstance(ct_info, dict): 400`；`fsize` 仅接受 `int`（`bool` 是 int 子类，注意）。`name`/`mime` 加长度上限。
- 置信度：已确认（代码路径直接抛出；与 07-30 报告 C-03 的已复现场景同类）。

### C-05 — 非法 `Content-Length` 头导致 500（P1）

- 根因：`/login` 与 `/send` 均 `int(self.headers.get("Content-Length", 0))`，无 try/except。HTTP 客户端可发送 `Content-Length: abc`（`BaseHTTPRequestHandler` 不解析该头，原样传递）。
- 证据：`transfer.py:3324`、`:3371`。
- 影响：一个畸形请求即可使 handler 线程抛 ValueError。
- 修复建议：包一层 int 转换工具函数，失败返回 400；或直接读 `rfile` 到 `MAX_BODY_SIZE` 上限。
- 置信度：已确认（纯静态可证；需原始 socket 触发）。

### C-06 — `identity_map` 写路径无锁（H1 部分实现）（P1）

- 根因：`_identity_lock` 只包住 `_save_identities_flush` 的 `json.dumps` 快照（`transfer.py:202-203`）；而写路径——SSE 握手的 `identity_map[identity_key] = {...}`（`:3240-3249`）、已有身份的字段更新（`:3220-3230`）、`/rename` 的 `for ikey, info in identity_map.items()` 遍历（`:3056-3059`）——均未持锁。`/rename` 的 `items()` 迭代与握手新增 key 并发时抛 `RuntimeError: dictionary changed size during iteration`；且快照与写互相交错，`feidi_identities.json` 可写旧。
- 证据：`transfer.py:3194-3250`（无锁写）、`:3056-3059`（无锁遍历）、`:202-203`（有锁读）。
- 影响：多个移动设备同时首次连接 + 另一设备改名时，有真实竞态窗口；HTTP 500。
- 修复建议：握手与 /rename 的全部 `identity_map` 访问包进 `_identity_lock`。
- 置信度：条件性已确认（需多设备并发触发）。

### S-07 — `kill_old_instance` 子串匹配残留（P1）

- 根因：注释声明"不会误杀其他程序（H-7：精确匹配）"，但 macOS/Linux 分支末尾仍有 `or "transfer.py" in cmd or "/feidi" in cmd`（`transfer.py:3731`）。任何命令行包含 `transfer.py` 或 `/feidi` 子串的进程（如 `python3 ~/dl/transfer.py.bak`、路径含 feidi 的无关服务）都会被 SIGTERM。
- 证据：`transfer.py:3729-3731`：
  ```python
  argv0 = cmd.split()[0] if cmd else ""
  if comm in feidi_names or argv0 in feidi_names or "transfer.py" in cmd or "/feidi" in cmd:
  ```
- 影响：同网段/同机运行的其他 Python 程序可能被误杀。
- 修复建议：删除两个子串分支，只保留 `comm`/`argv0` 精确 ∈ feidi_names。
- 置信度：已确认（代码路径明确；07-30 报告 S-06 即指出该问题，本次复核仍在）。

### S-08 — `_origin_allowed` 仍对任意合法 IP 反射 origin（P1）

- 根因：`ipaddress.ip_address(host)` 成功即返回 True，不区分本机/局域网/公网 IP。其他主机上托管的页面（Origin 为 `http://192.168.1.50:8080` 或公网 IP）能获得 `Access-Control-Allow-Origin` 反射，`/events` SSE 可被跨域读取（密码模式除外，但无密码模式是默认）。
- 证据：`transfer.py:3680-3696`。
- 影响：与 07-30 报告 S-03 完全相同的缺口，未修复。
- 修复建议：仅允许请求 Host 对应的 IP/主机名、localhost 与显式配置的受信 origin。
- 置信度：条件性已确认（需要其他主机页面；07-30 已实测反射 `http://8.8.8.8`）。

### R-05 — SSE 队列无界 + 重连不取消旧连接（P1）

- 根因：`dev_info = {"queue": queue.Queue(), ...}`（`transfer.py:3252`）无 maxsize；`broadcast_sse` 用 `put_nowait`（`:835`），慢客户端不消费时事件无限累积。同 device_id 重连仅 `sse_clients.pop(i)`（`:3265-3267`），旧 handler 的 `while True: queue.get(timeout=15)` 循环（`:3286-3293`）继续运行直到 TCP 断开，线程无法及时回收。
- 证据：`transfer.py:3252`、`:3263-3268`、`:3285-3300`。
- 影响：持续消息 + 慢客户端 = 内存/线程累积；大量重连可积压 worker。
- 修复建议：`queue.Queue(maxsize=N)` + put 超限时剔除慢消费者；Session 对象带 cancel_event，替换时置位并关闭旧 wfile。
- 置信度：已确认（07-30 实测 30 次重连 30 线程存活，本次代码未变）。

### R-06 — 大文件组装整块读内存（P1）

- 根因：组装完成后 `finfo["bytes"] = f.read()`（`transfer.py:3600-3601`，image 路径 `:3591-3593`）把 ≤500MB 文件整块载入 RAM，`add_message` 再写一份最终文件；峰值 = 500MB 内存 + chunks/assembled/final 三份磁盘。
- 证据：`transfer.py:3591-3601`。
- 影响：`MAX_GLOBAL_INFLIGHT_BYTES` 只按收到的 chunk 计费，未计入组装/复制峰值——内存与磁盘均可能超配。
- 修复建议：`add_message` 支持直接引用 assembled 文件路径（硬链接或 rename），避免第二份拷贝与整块读入。
- 置信度：已确认（代码路径明确；500MB 实测留给运行验证）。

### S-09 — 非分块文件大小信任客户端声明（P1）

- 根因：legacy `/send` 文件分支 `fsize = file_data.get("size", 0); if fsize > 50MB` 只信声明值；base64 解码后的实际字节数在 `add_message` 里才落盘，无大小闸。
- 证据：`transfer.py:3647-3649`、`add_message` `:783-791`。
- 影响：客户端可声明小 size 绕过 50MB 门槛，实际存下 ≈75MB（受 100MB body 上限约束）——文档"最大 50MB"名不副实（与 README 500MB 的渠道区分也模糊）。
- 修复建议：解码/流式计数后按实际字节限制；`name`/`mime` 长度上限。
- 置信度：已确认。

### S-10 — 默认 bind 无公网防护（P1，条件性）

- 根因：`get_local_ip` 候选不排除公网地址，无私网地址时选择公网 IP 作为默认监听地址（`transfer.py:540-542`）。`--bind` 已提供显式选项，但默认行为未收窄。
- 证据：`transfer.py:526-542`。
- 影响：路由器/双网卡环境可能把服务暴露到公网（默认无密码）。
- 修复建议：候选集仅 RFC1918/loopback；无私网时回退 127.0.0.1 + 警告。
- 置信度：条件性已确认（07-30 已用 mock 复现）。

### A-07 — 键盘可访问性三连未修（P1）

- 证据：
  - 设备列表为 `div.device-item` + click listener（`transfer.py:1462-1468`），无 `tabindex`/`role`/`keydown` —— WCAG 2.1.1 键盘可达失败。
  - Mobile viewport `maximum-scale=1,user-scalable=no`（`:2117`）—— WCAG 1.4.4 缩放失败；PC viewport（`:901`）无此问题，两套模板不一致。
  - 登录遮罩 `div.login-overlay`（`:2080-2088`）无 `role="dialog"`/aria-labelledby/focus trap/Escape 关闭；Mobile 无 Enter 提交（PC 有，`:2072-2076`）。
- 置信度：已确认（静态 DOM/CSS 审查）。

### L-02 — shutdown 状态机仍不完整（P2）

- 根因：`main()` finally 写 `_server_stopped = True`（`transfer.py:3811`）——该名字从未定义、从未读取、无 `global`，纯死代码；正确对象 `_server_stop_event`（`:167`）全仓库无 `.set()`，清理线程只能靠 daemon 强杀。
- 证据：`rg '_server_stop_event'` → 仅 167/479/481；`:3811`。
- 影响：`server_close()` 后清理线程仍可能在跑；逻辑意图（干净停止）未达成。
- 修复建议：finally 中 `_server_stop_event.set()`；统一 shutdown 顺序（停止接收 → 停清理线程 → 等待 handler → cleanup）。
- 置信度：已确认。

### D-04 — identity debounce 无退出 flush（P2）

- 根因：`save_identities` 只启动 5s daemon Timer（`transfer.py:222-226`）；`cleanup()`（`:266-270`）与 `signal_handler`（`:489-492`）不取消 timer、不 flush。改名/新身份后立即退出（Ctrl+C）丢数据。
- 证据：`transfer.py:217-226`、`:266-270`。
- 影响：07-30 已实测丢写；本次代码未变。
- 修复建议：shutdown 路径 `timer.cancel()` + 同步 `_save_identities_flush()`。
- 置信度：已确认（07-30 实测）。

### R-07 — `completed_transfers` 无 TTL（P2）

- 根因：`completed_transfers[transfer_id] = msg_id`（`transfer.py:3609`）只增不减；`_cleanup_stale_chunks` 的 `completed_transfers.pop`（`:409`）仅当同一 tid 仍在 `chunk_transfers` 时命中——完成即从 chunk_transfers 移除，故永不清理。
- 影响：长期运行（或攻击者刷唯一 transfer_id）内存持续增长；`/upload/status` 返回陈旧"已完成"。
- 修复建议：独立 TTL（如 1 小时）/ LRU 上限，进周期清理。
- 置信度：已确认。

### R-08 — `pid` 参数无长度限制（P2）

- 根因：`pid = params.get("pid", [""])[0]`（`transfer.py:3184`）无长度/字符集校验，直接成为 `identity_map` key 并持久化。
- 影响：超长 pid → JSON 文件膨胀（07-30 已观察到 86 条目，长度是另一维度）。
- 修复建议：与 name 对齐加长度上限（如 128）+ 合法字符集。
- 置信度：已确认。

### D-05 — metadata fsync 顺序错误（P2）

- 根因：`add_message` 文件分支：
  ```python
  with open(fmeta, "w", encoding="utf-8") as f:
      f.flush()
      os.fsync(f.fileno())          # 此时文件还是空的
      json.dump({...}, f, ensure_ascii=False)   # 写之后没有 flush/fsync
  ```
  （`transfer.py:792-795`）fsync 发生在写入之前，之后的数据依赖 close() 的缓冲冲刷，崩溃窗口下 `file_<id>.meta.json` 可为空/截断 → 下载 404/JSON 解析错误。
- 影响：与 D-02 同源，未修复。
- 修复建议：完整写入后统一 flush+fsync；用临时文件 + `os.replace`。
- 置信度：条件性已确认（崩溃时序才触发）。

### D-06 — 版本/文档三处不一致（P2）

- `transfer.py:33` `__version__ = "1.0.1-audit"`，CHANGELOG 已发布 1.1.0。
- `README.md:17` "🔐 密码保护 — 可选访问密码 + SHA-256 Cookie 认证"——C5 声称已删 SHA-256 描述，但特性列表残留（安全章节 `:99` 已改正，形成文档内部自相矛盾）。
- `CHANGELOG.md:42,44` [1.1.0] 标题重复。
- `start.bat:13,16,19` 三个 launcher 分支均不传 `%*`，`--port`/`--password` 等参数被吞。
- 置信度：已确认。

### B-01 — CI 权限与供应链（P2）

- `.github/workflows/build.yml:12` `permissions: contents: write` 对整个 workflow 生效（release job 需要，但 build job 不需要）；`actions/checkout@v4`、`setup-python@v5`、`upload-artifact@v4`、`download-artifact@v4`、`softprops/action-gh-release@v2` 均未 pin SHA。
- 置信度：已确认（07-30 Q-02 同项）。

### Q-03 — 测试会污染本地身份文件（P2）

- 四个回归测试经真实 SSE 握手（`/events?pid=c03-<uuid>` 等唯一 pid），服务端 `save_identities()` 会把测试身份写入项目根 `feidi_identities.json`（该文件已被 gitignore，但本地状态被污染；`test_c02` 只对 `feidi_chunks/` 做了快照恢复，身份文件无恢复机制）。`tests/_logs/` 目录亦未 gitignore。
- 置信度：已确认（测试代码 `tests/test_c03_r04_validation_and_limits.py:60` 与 `save_identities` 路径）。

### V-01 — vendored qrcode 无版本与许可信息（P2）

- `qrcode_lib/` 含 `LUT.py`、`image/styles/`、`release.py`（zest.releaser 入口）——对应上游 python-qrcode 约 7.4.x；仓库内无任何版本标识、无 LICENSE 文件（上游为 BSD-3-Clause，与项目 MIT 兼容，但 BSD-3 要求随附版权声明）。
- 置信度：已确认。

## 4. CHANGELOG v1.1.0 修复项核验

| CHANGELOG 项 | 声称修复 | 代码核验结论 | 证据(file:line) | 备注 |
|---|---|---|---|---|
| C1 | 私聊历史按 device_id 过滤 | **已实现** | `_history_for_device` transfer.py:712-725 | 广播 + 己发 + 己目标，逻辑正确 |
| C2 | 文件引用计数对称 | **已实现** | `_release_file` :564-588 与 `_cleanup_msg_files` :590-609 对称 pop MSG_FILES | ref=0 时同步摘索引，无 404-0 字节窗口 |
| H2/H10 | per-session SSE bearer token | **已实现**（且比声称更强） | `secrets.token_hex(16)` :3199,3236；`/send` :3363-3369、`/rename` :3034-3041 | 工作区 diff 追加 S-01：身份全部从 session 派生，body 不一致 403（:3390-3399） |
| H6 | 身份复用 IP/MAC 校验 | **已实现（MAC 严格 / IP 仅警告）** | :3207-3218 | MAC hash 不匹配 403；IP 漂移仅打印警告——README "校验 IP/MAC" 措辞略强于实现 |
| M5 | get_mac 5 分钟缓存 | **已实现** | `_mac_cache` :238-260 | TTL 300s，含 None 结果缓存 |
| M7 | MAX_SSE_CLIENTS 检查入锁 | **已实现** | :3163-3166 | 锁内检查 |
| M10 | UUID 收紧 canonical v4 | **已实现** | :3090, :3125 | `/img`/`/file` 均 canonical v4 正则 |
| L8 | 反代 HTTPS 加 Secure | **已实现** | :2948-2949 | `X-Forwarded-Proto=https` 时加 Secure |
| H3 | state.json 持久化 + 恢复 + /upload/status | **已实现** | `_save_chunk_state` :277-305、`_load_chunk_states` :308-388、`/upload/status` :3067-3085 | 工作区 diff 追加 C-02 磁盘一致性校验（:346-360），恢复为真恢复 |
| H4 | bytes_received 仅累计新 chunk | **已实现** | :3501-3512 | 重试 chunk 不虚高 |
| H5 | completed_transfers 幂等缓存 | **已实现（无 TTL，见 R-07）** | :3437-3440, :3609 | 重发返回缓存 msg_id；缓存永不清 → R-07 |
| H7 | chunk 清理持锁 + last_activity | **已实现** | :402-412 | 锁内判定 + last_activity 刷新 |
| H8 | 全局在途配额 | **已实现** | :3503-3508；恢复 :376-378；清理扣减 :411-412 | 检查+增加原子（_inflight_lock） |
| H1 | identity_map 受 _identity_lock 保护 | **部分实现** | 锁仅 :202-203（快照）；写路径 :3194-3250, :3056-3059 无锁 | 见 C-06 |
| M2 | dev_name 20 字上限 | **已实现** | :3180, :3185, :3027 | SSE/rename/my_name 三处一致 |
| C3 | --password 双标志 | **已实现** | :101 | `--pass`/`--password` 同 dest |
| C4 | PC 登录遮罩 + SSE 401 检测 | **已实现（检测用 403）** | loginOverlay :2080-2088；SSE 403 检测 :1549-1558 | CHANGELOG 措辞"401"与实际 403 不符（见 D-08） |
| D | crypto.getRandomValues UUID v4 | **已实现** | PC :1223-1246、Mobile :2286-2307 | 含 fallback（randomUUID/占位符） |
| L10 | Content-Disposition filename* | **已实现** | :3141-3142, :3150 | RFC 5987 编码 + 去分号 |
| C5 | README 安全章节改写 | **部分实现（已回归）** | README.md:17 仍 "SHA-256 Cookie 认证"；:99 已改对 | 特性列表残留 |
| M12/M13 | 下载文件名/打包命令修正 | **已实现** | README.md:26-32, :86-93 | Feidi.exe / Feidi-macos.zip / spec 命令 |
| 移除 ?code= | 连接码移除 | **已实现** | :2999 用 `?auth=required`；main 无横幅 | |
| 移除启动横幅 | 8hex 横幅移除 | **已实现** | main :3753-3767 | |
| 移除函数内 import | 重复 import 移除 | **部分实现** | `_pick_lang` :671 仍 `import re as _re` | 顶层已 import re |

**07-30 报告 P0/P1 回归复核**：C-01 ✅（三处 global + 测试）；C-02 ✅（顺序 + 校验 + 测试）；S-01 ✅（session 派生 + 测试）；C-03 ⚠️ 部分（见 C-04/C-05）；R-01/R-02 ⚠️ 部分（容量入锁 ✅，旧连接取消/有界队列 ❌）；S-02 ❌（见 S-10）；R-03 ❌（见 R-06）；A-01/A-02 ❌（见 A-07）。

## 5. 文档内部一致性问题

1. **README 特性列表与安全章节自相矛盾**：`README.md:17` 仍写 "SHA-256 Cookie 认证"，`:99` 已改为 "随机 128-bit token"（CHANGELOG C5 只改了一半）。
2. **CHANGELOG [1.1.0] 标题重复**（`CHANGELOG.md:42,44`）。
3. **版本号不统一**：`transfer.py:33` `1.0.1-audit` vs CHANGELOG 1.1.0 vs README 未标注版本。
4. **README 中英不对称**：`README.en.md:48` 有 `--bind`，`README.md` 无；en 版 "~3200 lines" 与实际 3817 不符。
5. **开发记文章**：`article_飞递开发记.md`（123 行，早期叙事）与 `article_飞递开发记_重写的版本.md`（241 行，更完整）均为 gitignored 个人文章，无权威性声明；重写版第 114-121 行承诺 "IP 或 MAC 对不上的话，不认" —— 代码已实现 MAC 严格 403，但 IP 漂移仅警告放行（LAN roaming），与文章"IP 对不上不认"的字面承诺有出入，建议文章措辞或代码二选一收敛。
6. **README "最大 500MB / 1MB 分块"**：与代码 `MAX_CHUNKED_FILE=500MB`、客户端 1MB 分块一致；但服务端单块上限实为 2MB（`CHUNK_SIZE_LIMIT`），文档未说明（无实际冲突，属精度问题）。
7. **CHANGELOG C4 措辞** "SSE 401 检测" vs 实现 403。
8. **README 防火墙命令** `localport=9876` 与默认端口一致 ✅；`allow_firewall.bat:33` 亦为 9876 ✅。

## 6. 未覆盖与局限

- **未运行测试与启动服务**（审查硬性约束）：`tests/` 四个回归测试与 `repro_*.py` 的断言结论来自代码阅读，未在本机执行；测试运行会写 `feidi_identities.json`/`feidi_chunks/`，符合约束。
- **R-06/S-09 的内存与配额量化**：500MB 级实测上传未执行；峰值内存结论来自代码路径分析。
- **UI/a11y 结论**：基于静态 DOM/CSS/JS 审查，未用真实屏幕阅读器与多浏览器验证。
- **qrcode_lib 未逐行审计**：确认无动态执行/命令执行集成路径；版本为按代码特征推断（7.4.x），未与上游源码 diff 比对。
- **工作区未提交 diff**（C-01/C-02/C-03/R-04/S-01 等修复）为审查基线一部分，其自身语义正确性已核验；建议尽快原子 commit，避免发布时丢失。
- **`/login` 与 `/send` 共享滑动窗口的叠加效果**未做流量实测，仅静态确认。
