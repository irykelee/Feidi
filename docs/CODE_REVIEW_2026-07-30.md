# Feidi 全面代码审查与架构分析

> **审查日期**：2026-07-30  
> **审查基线**：`main` / `26b97bc`（`fix(audit): Stage I - quick fixes`）  
> **审查性质**：只读审查；本报告不修改业务代码。  
> **覆盖范围**：`transfer.py`、嵌入式 PC/Mobile HTML/CSS/JS、`qrcode_lib/` 集成、启动脚本、PyInstaller 配置、CI/release、README/CHANGELOG。

## 1. 结论摘要

Feidi 的基本架构对“小规模、可信局域网内的临时传输”是可行的：标准库依赖少，文件下载路径、图片 MIME、密码 token、私聊历史过滤和部分并发保护已经有明确的安全/可靠性设计。但当前 HEAD 仍存在**发布阻断级的核心功能故障**和多个可复现的资源/安全问题：

1. **分块上传在当前代码中无法工作**：`_inflight_bytes` 在函数内被赋值但没有 `global` 声明，首个分块请求直接抛 `UnboundLocalError`；同一问题也会让启动恢复和退出清理抛异常。
2. **“断点续传”重启恢复逻辑自相矛盾**：启动时先删除所有分块目录，再读取 `.state.json`，因此状态显示“已收到”的 chunk 文件实际不存在。
3. **会话 token 没有绑定 `/send` 请求体中的 `device_id`**：持有任意有效 session 的客户端可以伪造消息来源设备。
4. **SSE 连接生命周期没有真正关闭旧连接**，且容量检查与 append 分离；重复重连可积累工作线程，容量上限也可被并发穿透。
5. **没有行为级自动化测试**；现有 pre-push 脚本能通过，但不会覆盖上述运行时故障。

### 1.1 Audit Health Score（嵌入式 UI 技术审查）

该分数只表示 `/audit` 要求的五个 UI/技术维度，不代表后端业务正确率。

| 维度 | 分数（0-4） | 关键依据 |
|---|---:|---|
| Accessibility | **1** | 设备列表使用可点击 `div`；对话框没有 role/focus 管理；多个 input 没有 label；移动端禁用缩放 |
| Performance | **1** | 分块路径当前崩溃；SSE 队列和线程无硬上限；大文件组装会整文件读入内存 |
| Theming | **2** | 有 light/dark token，但 PC/Mobile 两套 token 重复维护，仍有大量硬编码色值与对比度问题 |
| Responsive Design | **1** | PC 最小宽度约 640px，缺少 breakpoint；窄屏会横向溢出 |
| Anti-Patterns | **2** | 单文件 God Module、两套重复前端、内联路由/状态/持久化耦合明显 |
| **总分** | **7/20** | **Poor：需要先处理阻断问题，再做架构和 UI 收敛** |

### 1.2 问题数量

| 优先级 | 数量 | 含义 |
|---|---:|---|
| P0 | 1 | 阻断核心任务，必须在发布前修复 |
| P1 | 9 | 严重正确性、安全、资源或 WCAG A/AA 问题 |
| P2 | 15 | 应在下一轮修复的可靠性、维护性、UX 和工程问题 |
| P3 | 1 | 低影响的防御性/卫生问题 |
| **合计** | **26** | 已按证据等级过滤，未把纯风格偏好计入 |

## 2. 问题汇总表

> **证据等级**：`已确认` = 代码路径 + 静态检查或运行时复现；`条件性已确认` = 代码行为已确认，但需要特定环境/流量才能触发；`待核实` = 有明确代码依据，尚未在本机完整重现。

| ID | 优先级 | 证据 | 类别 | 位置 | 问题与影响 | 首选修复 |
|---|---|---|---|---|---|---|
| C-01 | **P0** | 已确认 | 正确性/资源 | `transfer.py:158,3363-3366,3470` | 分块上传首个 chunk 抛 `UnboundLocalError`；恢复/清理路径同样异常 | 将配额封装成对象，或在所有写入函数中正确声明 `global`；加端到端上传测试 |
| C-02 | **P1** | 已确认 | 正确性/数据完整性 | `transfer.py:415-425,3650-3652` | 启动先删 chunk 目录，再加载状态；恢复状态与实际文件不一致，后续组装失败 | 按 state 保留对应目录并校验文件，或先恢复再只清理孤儿目录 |
| S-01 | **P1** | 已确认 | 身份认证 | `transfer.py:3254-3278,3342-3352,3451,3490,3500` | `/send` 校验 token 存在但不比较 body `device_id`；可伪造来源设备/名称 | 使用 `session_dev_id` 作为唯一来源；拒绝不一致的 sender/name/device_id |
| C-03 | **P1** | 已确认 | 输入验证/异常处理 | `transfer.py:3226-3236,3273-3500` | JSON 顶层类型、密码类型、chunk 整数、base64、文件 schema 不合法时抛 500/断连接 | 边界 schema 校验，统一返回 400/413，不让异常穿出 handler |
| R-01 | **P1** | 已确认 | 并发/生命周期 | `transfer.py:3067-3070,3167-3204` | SSE 容量检查和 append 非原子；替换同设备连接时旧 handler 没有取消 | 预留连接槽；每个 session 用取消事件，替换时关闭旧连接 |
| R-02 | **P1** | 已确认 | 资源耗尽 | `transfer.py:2815-2817,3156,716-744,3192-3199` | 每个请求一个线程，SSE queue 无界；慢客户端可积累线程/事件内存 | 有界 worker、`Queue(maxsize=...)`、慢消费者断开策略 |
| S-02 | **P1** | 条件性已确认 | 网络暴露 | `transfer.py:448-490,3621` | 无私网地址时默认选择任意非回环公网地址并监听 | 默认仅允许 RFC1918/loopback；公网监听必须显式 opt-in |
| R-03 | **P1** | 代码已确认 | 性能/内存 | `transfer.py:3408-3463,3457-3460,679-684` | 500MB 组装文件整块 `read()` 到 RAM，再写第二份文件；配额低估峰值 | 流式复制 assembled 文件，或直接 rename/hardlink；按 scratch space 计量 |
| A-01 | **P1** | 已确认 | WCAG | `transfer.py:1343-1372,2470-2485` | 设备选择是可点击 `div`，键盘无法切换私聊目标（WCAG 2.1.1） | 改为 button，或补 role/tabindex/Enter/Space/aria 状态 |
| A-02 | **P1** | 已确认 | WCAG | `transfer.py:2021` | `maximum-scale=1,user-scalable=no` 禁止移动端缩放（WCAG 1.4.4 风险） | 删除两个限制，允许浏览器缩放 |
| R-04 | **P2** | 已确认 | 资源管理 | `transfer.py:561-574,147-149,3091-3154` | `_rate_limits`、`completed_transfers`、`identity_map` 缺少可靠 TTL/容量上限 | 统一 TTL/LRU/最大条数，并在周期清理线程执行 |
| D-01 | **P2** | 已确认 | 持久化 | `transfer.py:192-225,266-270,395,438-445` | 5 秒 debounce 的 identity 写盘没有退出 flush；立即退出可能丢改名/新身份 | shutdown 时 cancel timer 并同步 flush，记录失败 |
| S-03 | **P2** | 已确认 | CORS/隐私 | `transfer.py:3528-3545,3073-3076,3514-3523` | `_origin_allowed` 接受任意合法 IP；SSE/OPTIONS 会反射公网或其他 LAN IP | 仅允许实际 server host/受信 origin；对状态变更 API 统一检查 |
| D-02 | **P2** | 条件性已确认 | 数据持久化 | `transfer.py:663-699` | `fmeta` 在 `json.dump` 前 fsync，图片 MIME 文件没有 fsync；崩溃窗口可留下空 metadata | 写入后再 flush/fsync，并采用临时文件 + replace |
| S-04 | **P2** | 代码已确认 | 输入边界/配额 | `transfer.py:3491-3500,679-699` | legacy file 路径用客户端声明的 `size` 做 50MB 检查，实际 decoded bytes 可更大；文件元数据也缺少字段上限 | 用解码后实际大小校验；限制 name/mime/metadata 长度 |
| L-01 | **P2** | 已确认 | 生命周期 | `transfer.py:167,427-445,3659` | `_server_stop_event` 从未 set；`_server_stopped` 是未使用局部变量；shutdown 没有统一顺序 | `server.shutdown()` → 停止清理线程 → 等待 handler → cleanup |
| A-03 | **P2** | 已确认 | WCAG/交互 | `transfer.py:947-1029,1984-1992,2156-2164` | modal 没有 dialog role、aria-labelledby、focus trap、Escape；多个 input 只有 placeholder | 补语义、焦点保存/恢复、键盘关闭和 label |
| A-04 | **P2** | 已确认 | Responsive | `transfer.py:833-835,894` | PC panel 最小宽约 340 + gap 18 + QR 282，无 media query；窄于约 640px 横向溢出 | 增加 mobile breakpoint，允许 panel/QR 纵向布局 |
| A-05 | **P2** | 已确认 | i18n | `transfer.py:1917-1928,2766-2773` 及 UI 模板 | EN 字典只覆盖少数状态词，大量菜单、弹窗、toast 仍是中文 | 完整抽取字符串并加 `data-i18n`，或暂时移除未完成切换 |
| A-06 | **P2** | 已确认 | UX/性能 | `transfer.py:3445-3455` | 大图片超过约 5MB base64 阈值后变成 `file` 消息，不能内联预览且走额外内存复制 | 以文件路径/MIME 方式流式保存为 image，前端按 MIME 展示 |
| M-01 | **P2** | 已确认 | 架构 | `transfer.py:1-3666` | CLI、HTTP、SSE、消息存储、上传状态、身份、清理、两套前端全部在一个模块 | 分成 cli/server/session/store/upload/identity/static 层，保留单入口包装 |
| Q-01 | **P2** | 已确认 | 测试/质量门 | 仓库无 `tests/`、pytest 配置或行为测试 | 密码、SSE、chunk、文件引用等关键路径没有回归保护 | 先加 stdlib smoke test，再逐步补 pytest/integration |
| D-03 | **P2** | 已确认 | 文档/发布 | `transfer.py:33`, `README.md:17`, `CHANGELOG.md:42-44`, `start.bat:11-19` | 版本仍为 `1.0.1-audit`；README 保留错误 SHA-256 描述；CHANGELOG 标题重复；Windows launcher 不传递 CLI 参数 | 统一版本来源和文档，修正重复 heading，`start.bat` 使用 `%*` |
| Q-02 | **P2** | 已确认 | CI/供应链 | `.github/workflows/build.yml:10-12,16-23,35-40` | build job 继承 `contents: write`；actions 仅按 major tag，构建依赖无 hash lock | 按 job 缩小权限，pin action SHA，增加依赖审计/可复现构建 |
| S-05 | **P2** | 设计限制已确认 | 传输安全 | `transfer.py:2900,3158-3165,3254-3260` | 默认 HTTP 明文传输；密码 cookie、SSE session token 和消息可能被同 LAN 被动监听者读取 | 支持 HTTPS/反代并明确“LAN-only ≠ 加密”；敏感部署强制 HTTPS |
| S-06 | **P3** | 代码已确认 | 防御性卫生 | `transfer.py:3570-3581,2856-2892` | `kill_old_instance` 用 `"/feidi"`/`transfer.py` 子串匹配；响应缺少 CSP、nosniff、Referrer-Policy 等安全头 | 精确匹配真实 executable；补最小安全响应头 |

## 3. P0/P1 详细分析

### C-01 — `_inflight_bytes` 作用域错误使分块上传完全失效（P0）

**根因**：模块级有 `_inflight_bytes = 0`（`transfer.py:157-158`），但 `RequestHandler.do_POST` 同一函数内既读取又赋值（`transfer.py:3363,3366,3470`），没有 `global _inflight_bytes`。Python 因此把它当作局部变量，第一次读取就触发 `UnboundLocalError`。`_load_chunk_states()` 的 `:350` 和 `_cleanup_stale_chunks()` 的 `:379` 也有同样问题。

**实测**：向本地临时 HTTP server 建立 SSE session 后发送一个合法的单 chunk 请求，handler 输出：

```text
transfer.py:3363: UnboundLocalError:
  cannot access local variable '_inflight_bytes' where it is not associated with a value
```

客户端得到 `RemoteDisconnected`，没有 JSON 错误响应。`ruff check` 同时报告 `transfer.py:350` 与 `:379` 的 F823。调用退出清理时也会再次报同类异常，因此可能留下 chunk 目录。

**影响范围**：PC/Mobile 的所有大于 2MB、走 `sendFileChunked` 的文件、音视频、文档和大图片；README/CHANGELOG 宣称的 500MB 分块/断点续传功能在当前 HEAD 不可用。

**建议**：不要继续依靠散落的 `global`。优先引入 `InflightQuota`（`try_reserve()` / `release()`），将计数和锁封装；短期修复需在三个函数/方法中正确声明全局变量，并用真实 HTTP 上传测试覆盖首 chunk、完成、超限、超时、重启、退出。

### C-02 — 启动恢复先删分块目录，导致“状态恢复”变成伪恢复（P1）

**根因**：`main()` 的顺序是 `_startup_cleanup()` 再 `_load_chunk_states()`（`transfer.py:3650-3652`）。前者会删除 `CHUNK_DIR` 下全部子目录（`transfer.py:415-425`），后者只读取 `.state.json`，不会恢复已删除的 `.chunk` 文件。

**实测**：构造 `resume-test/0.chunk` 与对应 state 文件后调用 `_startup_cleanup()`，目录立即消失；随后 state 仍可被加载，内存中的 `chunks` 集合却继续声称已收到 `0`。下一次完成组装时 `transfer.py:3413-3418` 找不到 chunk，返回 `Missing or unreadable chunk during assembly`。

**建议**：启动时先读取并校验 state；仅保留 state 中存在且文件大小/索引一致的目录，清除无 state 的孤儿目录。若不打算跨进程保留 chunk，应删除“7 天服务端恢复”的文档承诺，而不是返回虚假的 received 列表。

### S-01 — `/send` 的 session token 没有绑定 body `device_id`（P1）

**根因**：`do_POST` 在 `transfer.py:3254-3260` 通过 `_check_session_token()` 得到 `session_dev_id`，但随后在 `:3277` 又从请求体读取 `dev_id = data.get("device_id", "")`，从未比较二者。该值被写入 chunk owner（`:3342`），并传入 `add_message`（`:3451,3490,3500`）。`/rename` 已有正确的比较（`:2943-2945`），但 `/send` 没有。

**实测**：建立一个合法 SSE session，带其 `X-Feidi-Session`，提交 `device_id="forged-device"` 的文本消息，响应 200；服务端 `messages[-1]["device_id"]` 为 `forged-device`，而不是 session 所属设备。

**影响**：任意已连接设备可伪造消息来源、绕过广播自排除逻辑，并在私聊/分块元数据中冒用其他设备。密码模式只能限制“谁能建立 session”，不能修复建立 session 后的身份绑定漏洞。

**建议**：服务端从 session registry 直接得到 `device_id`、`sender/type`、显示名；请求体中的值只作为一致性校验，不一致立即 403。目标设备仍需单独做在线性和授权策略检查。

### C-03 — 未验证 JSON schema，畸形输入直接打穿 handler（P1）

**已确认触发样例**：

- `/login` body `{"password": null}` 或数字：`secrets.compare_digest()` 抛 `TypeError`（`transfer.py:3236`）。
- `/send` body `[]`：`data.get()` 抛 `AttributeError`（`:3273`）。
- `/send` body `{"text": 123}`：`len(text)` 抛 `TypeError`（`:3478`）。
- `/send` chunk `chunk_index="x"`：`int()` 抛 `ValueError`（`:3287`）。
- `data:image/png;base64,abc`：`base64.b64decode()` 抛 padding 错误（`:652`）。

这些请求均导致连接被关闭/500，而不是可诊断的 4xx。攻击者可以制造线程异常和日志噪声，合法客户端也会得到不可理解的网络失败。

**建议**：在进入业务分支前验证“必须是 object”；对字符串、整数、布尔、`file_info` 字段、MIME、base64 和长度做显式检查。将 `ValueError/TypeError/binascii.Error/OSError` 映射为 400/413/415，并确保临时文件在失败路径回收。

### R-01 — SSE 容量检查和连接替换不是原子的（P1）

**根因**：`MAX_SSE_CLIENTS` 检查在 `:3067-3070` 的锁内完成，但释放锁后还要执行身份解析、响应头发送等工作，直到 `:3167-3173` 才 append。并发握手可同时通过检查。新连接替换同 `device_id` 的旧 `dev_info` 时，只从 `sse_clients` 列表删除旧字典，并没有向旧 handler 的阻塞循环发送取消信号或关闭 socket（`:3189-3204`）。

**实测**：50 个并发握手得到 21 个 200（上限为 20），`sse_clients` 长度为 21；同一 `pid` 连续建立 30 个 SSE 连接后，列表只有 1 个设备，但仍有约 30 个请求线程存活，短时间关闭客户端也不会立即回收。

**建议**：引入 `Session` 对象和 `cancel_event`；连接槽在握手前原子 reservation，失败时回滚；同设备重连时设置旧 session 的 event、关闭旧 wfile，并等待 handler 退出。不要用“从列表删除”代替连接生命周期管理。

### R-02 — 无界 SSE 队列 + 无界 ThreadingMixIn 形成资源耗尽面（P1）

`queue.Queue()` 默认 `maxsize=0`（`transfer.py:3156`），`broadcast_sse()` 用 `put_nowait()`（`:728-744`），慢客户端不会触发 `queue.Full`，事件会持续累积。与此同时 `ThreadingHTTPServer` 每请求创建一个线程（`:2815-2817`），SSE handler 会在 `queue.get(timeout=15)` 上长期阻塞（`:3192`）。

**影响**：在消息持续产生、客户端保持连接但不读取时，内存和线程数随时间增长。前述同 PID 重连还允许从 `sse_clients` 中“挤掉”旧项而不结束旧线程。

**建议**：限制 worker 数量；SSE 使用有界队列并定义丢弃/断开策略；发送失败或队列超限时主动清理 session；把 SSE 连接放到独立、可控的异步/连接层。

### S-02 — 默认地址选择可能把服务暴露在公网（P1，条件性）

`get_local_ip()` 只排除回环地址，并把不属于 `192.168/16`、`10/8`、`172.16/12` 的地址排在最后但仍会选择（`transfer.py:453-490`）。因此没有私网地址时，公网接口地址会成为默认 bind 地址；`main()` 随后在 `:3621` 监听该地址。用 mock `getaddrinfo()` 返回 `203.0.113.10` 时，函数确实选择了这个公网地址。

**建议**：默认候选集只允许 RFC1918、链路本地和 loopback；没有私网地址时回退 `127.0.0.1` 并明确警告。公网监听只接受显式 `--bind`，并在 README 中说明防火墙/HTTPS要求。

### R-03 — 大文件组装的内存/磁盘峰值远高于配额（P1）

分块完成后，代码在 `transfer.py:3410-3425` 生成 `assembled.bin`；随后 `:3457-3460`（非图片）或 `:3449-3455`（大图片）使用 `f.read()` 把整个 assembled 文件读入内存，再传给 `add_message()`。`add_message()` 又在 `:679-684` 写出另一份最终文件。500MB 文件在组装和复制期间同时存在多份磁盘/内存表示，而 `MAX_GLOBAL_INFLIGHT_BYTES` 只统计收到的 chunk 字节数。

**建议**：使用流式复制到最终路径，再原子 rename；图片直接保存二进制并以安全 MIME 路径服务；配额同时计入 chunk、assembled、最终文件和可用磁盘空间。增加 100MB/500MB 的峰值内存与磁盘测试。

## 4. P2 详细分析

### R-04 — 三类全局状态缺少容量/TTL治理（P2）

1. `_rate_limits` 的“stale 清理”只删除已经变成空列表的键（`:570-573`）。每个新 IP 第一次请求都会留下一个非空时间戳列表；本机测试 1000 个不同键后，字典仍有 1000 项。
2. `completed_transfers` 在完成时写入（`:3465-3470`），周期清理只遍历 `chunk_transfers`（`:369-376`），已完成项没有独立 TTL/LRU，因此长期运行会持续增长。
3. `/events` 的任意 `pid` 会成为 `identity_map` 键（`:3091-3154`），没有最大条数、过期回收或握手限流；当前未跟踪的本地 identity 文件已观察到 85 个条目，不能证明无限增长已造成故障，但足以说明需要治理。

**建议**：统一 `BoundedTTLMap` 或定期清理；对 `/events` 按 IP 和 pid 限速；对 identity 设最大条数和最近使用淘汰；对 completed transfer 只保留有限时间窗口。

### D-01 — debounce 身份写盘没有 shutdown flush（P2）

`save_identities()` 在 `transfer.py:217-225` 只启动 daemon `Timer(5s)`。`cleanup()`（`:266-270`）和 `signal_handler()`（`:437-441`）没有取消并立即执行 `_save_identities_flush()`。

**实测**：将 `IDENTITY_FILE` 指向临时文件，先写入 `{"old":1}`，修改内存为 `{"new":2}`，调用 `save_identities()` 后立即 `cleanup()` 并取消 timer，文件仍为旧内容。用户“改名后马上退出”会丢数据。

**建议**：将 flush 纳入统一 shutdown；写失败必须 stderr/log 记录，不能静默吞掉 `transfer.py:209-214` 的所有异常。

### S-03 — CORS Origin 检查过宽且未统一应用（P2）

`_origin_allowed()` 对任意可解析 IP 返回 True（`:3539-3545`）。实测 `Origin: http://8.8.8.8` 的 `/events` 获得 `Access-Control-Allow-Origin: http://8.8.8.8`；OPTIONS 也会对公网 IP 返回 200/反射 origin。当前 `Allow-Headers` 没有列出 `X-Feidi-Session`，限制了部分浏览器跨域 POST，但并不能把“任意 IP 都是受信 origin”变成正确策略；SSE 和无自定义头的接口仍暴露过宽。

**建议**：只允许当前服务实际 host/端口组合或明确配置的 origin；对 `/login`、`/rename`、`/send` 的 Origin/CSRF 策略统一；不要用“合法 IP”作为信任边界。

### D-02 — 附件 metadata 的崩溃一致性不足（P2，条件性）

在 `add_message()` 中，文件 metadata 的写入顺序是 `flush()`/`fsync()`（`:696-698`）后才 `json.dump()`（`:699`）；图片 MIME 文件（`:663-666`）没有 fsync。正常关闭时 `with` 会 flush，但进程在这两个操作之间崩溃时，二进制文件和 metadata 可能不一致，后续下载会得到 JSON 解析错误或 415。

**建议**：先完整写入，再 flush/fsync；metadata 使用独立临时文件和 `os.replace`，并在任一附件写失败时回滚已写文件和消息登记。

### S-04 — legacy 文件路径依赖客户端声明大小（P2）

`/send` 的非分块文件只用 `file_data.get("size", 0)` 与 50MB 比较（`:3491-3500`），而真正解码后大小在 `add_message()` 的 `:679-699` 才确定。客户端可报小 size 绕过该分支的 50MB 门槛；整体请求虽受 100MB body 限制，但仍可能存储超过文档宣称的 50MB。

**建议**：先解码或流式计数后按实际字节数限制；对 name/mime/target_id/device_name 设置明确长度和字符集上限。分块路径也应校验 `file_info.size` 与实际累计字节一致。

### L-01 — shutdown 状态机不完整（P2）

`_server_stop_event` 定义于 `:166-167`，只被周期线程读取（`:427-434`），整个仓库没有 `.set()`；`main()` finally 的 `_server_stopped = True`（`:3659`）既没有 `global` 也没有读取方。与此同时 signal handler 直接 `cleanup(); sys.exit(0)`，可能在下载/写盘 handler 仍运行时删除临时文件。

**建议**：集中为一个 `shutdown()`：停止接收新请求、调用 `server.shutdown()`、设置 event、等待清理线程/关键 handler，再清理 temp/chunk，并统一处理 SIGINT/SIGTERM/KeyboardInterrupt。

## 5. UI/可访问性/响应式详细分析

### A-01 — 设备列表不能用键盘操作（P1）

PC 的设备项在 `transfer.py:1343-1348` 是 `div`，点击逻辑在 `:1366-1372`；Mobile 对应 `:2470-2485`。没有 `tabindex`、`role`、`keydown` 或 `aria-pressed`。键盘和屏幕阅读器无法选择私聊对象。应优先改用原生 button，避免手工复刻键盘语义。

### A-02 — 移动端禁用缩放（P1）

`transfer.py:2021` 的 viewport 明确设置 `maximum-scale=1,user-scalable=no`。这会阻止低视力用户放大消息/输入区域；去掉限制不会破坏现有布局，是低成本修复。

### A-03 — 对话框、焦点和表单语义不完整（P2）

拖拽确认框、PC/Mobile 登录遮罩都是普通 `div`（`:947-1029,1984-1992,2156-2164`），没有 `role="dialog"`、`aria-modal`、标题关联、焦点陷阱、Escape 关闭和焦点恢复。密码、文本和隐藏文件 input 多数只有 placeholder，没有 label/aria-label。多个按钮也没有 `:focus-visible` 样式。

同时，PC 有 Enter 提交密码监听（`:1975-1980`），Mobile 没有等价监听；这会让典型手机用户必须先收起键盘再点击按钮。

### A-04 — PC 端窄屏溢出（P2）

`.panel` 的 `min-width:340px`（`:834`）与 `.qr-panel width:282px`（`:894`）加上 gap 后，容器至少约 640px。两个 HTML 模板都没有 `@media` breakpoint。应在窄屏切换为纵向布局、允许 panel `min-width:0`，并验证 320/375/768/1024/1440 宽度。

### A-05 — i18n 只覆盖少量字符串（P2）

虽然 `I18N` 字典在 `:1917-1928`、`:2766-2773` 存在，但模板中的菜单、按钮 title、空状态、文件确认框和多数 toast 没有 `data-i18n`，切换 EN 后仍显示中文。应采用共享字典/模板键，避免 PC/Mobile 两处继续漂移。

### A-06 — 大图片的用户体验和性能不一致（P2）

`transfer.py:3445-3455` 对较大图片走 `add_message("file", ...)`，前端收到的是文件卡片而不是图片预览；同时仍然会整块读取和复制。建议以受白名单保护的 `/img/<uuid>` 路径流式服务，不要把图片强行转换成大 base64 字符串。

## 6. 架构评估

### 6.1 当前数据流

```text
CLI/import side effects
  ├─ parse_args + TEMP_DIR + load_identities
  └─ main(): get_local_ip → kill_old_instance → ThreadingHTTPServer

Browser GET /events
  → identity_map / MAC check
  → sse_clients + per-session token + history
  → queue.Queue → SSE stream

Browser POST /send
  → rate limit + session token + JSON
  ├─ text/image/file legacy → messages + MSG_FILES/TEMP_DIR
  └─ chunked → chunk_transfers + feidi_chunks/*.chunk/state
                    → assemble → messages + TEMP_DIR
  → broadcast_sse

Browser GET /img/<uuid> or /file/<uuid>
  → file reference count → streamed disk file → release/delete

periodic/atexit cleanup
  → TEMP_DIR + stale chunk directories + identity timer (目前未 flush)
```

### 6.2 合理之处

- 运行时依赖基本为 Python 标准库，QR 库随仓库 vendored，部署门槛低。
- `/img/<uuid>`、`/file/<uuid>` 使用 canonical UUID v4 校验；文件名做 CR/LF/引号/分号清洗，并使用 RFC 5987 `filename*`（`transfer.py:3041-3055`）。
- 图片下载有 MIME 白名单，不允许 SVG/HTML 以可执行同源内容返回（`:3006-3018`）。
- 密码 token 使用 `secrets.token_hex`，Cookie 有 HttpOnly/SameSite；私聊历史过滤和文件引用计数已有明确实现。
- 分块状态、identity JSON 使用临时文件 + `os.replace` 的原子替换思路；CI 的 Windows/macOS 构建最近一次运行成功。
- `subprocess` 调用使用参数数组和 timeout，没有发现 `shell=True`、`eval`、`exec`、pickle 或硬编码密钥。

### 6.3 主要架构风险

1. **God Module**：`transfer.py` 3666 行同时承载 8 类职责；导入时就 parse CLI、创建 temp dir、加载身份、安装 signal handler，导致单元测试和复用困难。
2. **共享状态通过手工锁协议维护**：至少有 `_msg_lock`、`_file_ref_lock`、`_chunk_lock`、`_inflight_lock`、`_sse_lock`、`_rate_lock`、`_identity_lock`。锁的顺序和覆盖范围依赖注释，无法由类型/结构强制；本次 C-01 正是全局状态演进缺少测试的结果。
3. **前端协议重复**：PC/Mobile 各自维护一套约 65KB/43KB 的 HTML/JS/CSS，chunk 协议、i18n、去重、命名和通知逻辑需同步修改，容易产生已观察到的行为差异。
4. **持久化与请求处理直接耦合**：handler 直接操作 dict、文件和 timer，没有 repository/store 接口，也没有事务回滚；异常容易在“文件已写、消息未登记”之间留下孤儿。
5. **连接模型不适合恶意或高并发客户端**：SSE 长连接占用线程，队列无界，缺少 session cancellation 和 worker backpressure。

### 6.4 建议的目标边界

保持 `transfer.py` 作为可执行入口，但把实现拆成可测试的小模块：

```text
feidi/
  cli.py             # parse_args(argv), config validation
  server.py          # HTTP routing / response headers
  auth.py            # password + session registry + Origin policy
  sessions.py        # bounded SSE sessions + cancellation
  messages.py        # bounded message store + attachment refs
  uploads.py         # chunk state machine + quota + recovery
  identities.py      # locked TTL/LRU persistence
  static/             # shared JS/CSS + PC/Mobile shells
transfer.py          # thin launcher / PyInstaller entrypoint
```

## 7. 安全与性能专题

### 7.1 已有防护（应保留）

- canonical UUID 路径校验、文件名响应头清洗、图片 MIME 白名单。
- `secrets.compare_digest` 用于密码比较，token 使用 CSPRNG。
- `/rename` 已正确比较 session 与 device id；这是 `/send` 应复用的模式。
- chunk `transfer_id` 路径字符集限制、chunk 大小/总 chunk 数/文件大小上限、文件引用计数。
- runtime 文件在 `.gitignore` 中，未发现硬编码 API key/密码。

### 7.2 需要明确的安全假设

- `http://` 明文服务只能提供“局域网可达性”，不能提供传输保密性；密码和 session token 可被同一网络的被动监听者读取。README 应明确这一点，敏感环境需 HTTPS 反代或内置 TLS。
- “LAN-only” 不能仅由选择一个地址保证；需要拒绝公网默认 bind、显示绑定地址、建议防火墙和密码。
- CORS 不是认证机制；Origin 允许列表、session header、Cookie/CSRF 应分别设计并统一应用。

### 7.3 主要性能瓶颈

- 大文件当前有多份磁盘/内存副本，且分块路径在修复作用域错误前完全不可用。
- SSE 事件没有 backpressure；慢消费者影响的是内存而非仅网络吞吐。
- 每次新身份 flush 都序列化完整 JSON；identity 数量没有上限。
- `/status`、session 查找和设备广播使用共享列表；当前上限小，扩展时会成为串行热点。
- 两套 HTML 每次请求都重新做字符串替换，未 gzip/ETag；对小 LAN 不是首要问题，但可在静态资源拆分后处理。

## 8. 验证记录

| 检查 | 结果 | 解释 |
|---|---|---|
| `git rev-parse HEAD` / `git status` | HEAD=`26b97bc`；业务代码工作树干净 | 审查基线明确 |
| `python3 -m py_compile ...` | **通过** | Python 语法可编译，不代表运行时路径正确 |
| `ruff check transfer.py qrcode_lib` | **失败，4 项** | F823：`:350,379` 的 `_inflight_bytes`；F541：`:3609,3639` 无占位 f-string |
| `node --check`（提取 PC/Mobile script） | **通过** | 嵌入式 JS 语法有效 |
| `bash Scripts/pre_push_verify.sh` | **15 项通过** | 只做语法/文件存在/构建产物检查，不覆盖 HTTP、SSE、chunk、锁或输入边界 |
| pytest/bandit | **本机不可用/仓库无测试配置** | 没有 `tests/`、pytest 配置；bandit 未安装；不能据此宣称覆盖率达标 |
| GitHub Actions run `30440844612` | **构建成功** | 只证明 PyInstaller/release job 完成，不证明运行时功能 |
| 分块 POST smoke | **失败并复现 C-01** | handler `UnboundLocalError`，客户端断开 |
| `/send` device spoof smoke | **复现 S-01** | 合法 session + forged body device id 返回 200 并写入消息 |
| malformed JSON/type/base64/chunk smoke | **复现 C-03** | 多个输入直接断连接/500 |
| startup cleanup smoke | **复现 C-02** | state 对应 chunk 目录被删除 |
| 50 个并发 SSE handshake | **复现 R-01** | 21 个请求得到 200，超过配置上限 20 |
| 30 次同 pid SSE reconnect | **复现 R-01/R-02** | 列表只剩一个 session，但旧 worker 线程短时仍存活 |
| 1000 个 rate-limit key | **复现 R-04** | 清理后仍有 1000 个非空条目 |
| immediate identity cleanup | **复现 D-01** | 文件保持旧快照，pending timer 未 flush |
| arbitrary IP Origin | **复现 S-03** | SSE/OPTIONS 反射 `http://8.8.8.8` |

`Scripts/pre_push_verify.sh` 的“Working tree clean”使用 `git diff --quiet HEAD`，不会检查未跟踪文件；因此它不能作为完整发布质量门。

## 9. 修复路线图

### Phase 0：发布阻断（先做）

1. 修复 C-01，并先写失败测试：首 chunk、非最后 chunk、最后 chunk、超限、清理、恢复。
2. 修复 C-02：恢复前保留有效 chunk 文件，校验 state 与磁盘一致性；恢复失败必须清理并返回明确状态。
3. 修复 S-01：所有发送者身份从 session registry 派生，不信任 body 的 identity 字段。
4. 为 C-03 建立统一 request parser/错误响应，禁止异常穿出 `do_GET/do_POST`。

### Phase 1：资源与安全边界

1. 引入 bounded session registry、取消事件、有界 SSE queue 和 worker 上限。
2. 修复默认 bind 的公网回退；收紧 Origin；明确 HTTP 明文限制。
3. 将大文件组装改为流式/原子路径转移，按真实磁盘 scratch 和 decoded bytes 计费。
4. 为 identity、rate-limit、completed transfer 增加 TTL/LRU/上限。

### Phase 2：生命周期与持久化

1. 统一 shutdown 顺序并 flush identity timer；清理函数报告错误。
2. metadata/mime 使用“完整写入 → fsync → replace”；失败回滚附件登记。
3. 明确哪些运行时状态保留 7 天、哪些“关闭即焚”，同步更新 README。

### Phase 3：回归保护与架构

1. 先用标准库增加 `Scripts/smoke_test.py`：随机端口启动、SSE 握手、认证、文本、图片、文件下载、chunk 恢复、优雅退出。
2. 再把状态管理拆成 `messages/uploads/identities/sessions`，每个模块拥有自己的锁和不变量。
3. 抽取共享 JS/CSS，PC/Mobile 只保留布局差异；用同一 chunk client 和 i18n 字典。

### Phase 4：UI/发布卫生

1. 修复键盘/对话框/label/zoom/reduced-motion/focus 与窄屏布局。
2. 完成 i18n 或暂时隐藏未完成语言切换。
3. 同步 `__version__`、README、CHANGELOG、launcher 参数；收紧 CI permissions、pin action SHA。

## 10. 正向发现

- 最近几轮审计已经解决了私聊历史泄露、文件引用计数、session token、UUID 路径、图片 MIME、文件名响应头等真实问题，说明安全修复方向是有效的。
- `secrets.compare_digest`、`HttpOnly`、`SameSite=Lax`、原子 replace、chunk 字节配额等实现意图清晰。
- PC/Mobile 的用户输入展示大多使用 `textContent` 或 `escHtml`；没有发现 `eval`、`innerHTML` 直接拼接消息正文、`shell=True` 或硬编码秘密。
- CI 最近一次 Windows/macOS 构建成功，PyInstaller 产物上传和 SHA256SUMS release 流程已具备基础闭环。
- 这次审查期间通过必要的设计上下文流程在项目根目录生成了 `.impeccable.md`；它只描述 UI 设计基线，不改变业务逻辑。

## 11. 审查边界

- 未执行 500MB 实际压力上传，R-03 的峰值结论来自代码路径和内存副本分析；应在隔离环境用小比例/受控磁盘测试验证。
- 未安装 pytest、bandit 或浏览器自动化依赖；UI 可访问性结论来自静态 DOM/CSS/JS 审查，尚未用真实屏幕阅读器和多浏览器验证。
- `qrcode_lib/` 作为 vendored 第三方库未逐行重新审计；本次确认其集成路径未发现动态执行/命令执行。建议后续记录上游版本、许可证和依赖 provenance。
- 没有执行任何 release/tag/push 操作，也没有修改业务源文件。
