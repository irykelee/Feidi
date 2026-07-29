# Feidi Release Notes Template / 发布说明模板

> Copy this template to fill in for each new tag. Replace all `[brackets]`.
> 复制此模板为每个新 tag 填写,替换所有 `[方括号]` 占位符。

---

## Title / 标题

- Stable release / 正式版: `Feidi v[MAJOR.MINOR.PATCH]` (e.g. `Feidi v1.1.0`)
- Nightly / 夜间构建: `Nightly build [short-sha]` (auto-generated, do not edit)

## Tag / Tag

```
v[MAJOR].[MINOR].[PATCH]
```

## Body / 正文

### Summary / 概要

[One-paragraph English + Chinese summary of what changed in this release.]

[一段中文 + 英文简述本次发布改了什么。]

### Highlights / 要点

- **[EN]** Bullet 1
  **[ZH]** 要点 1
- **[EN]** Bullet 2
  **[ZH]** 要点 2

### Files / 文件

| Platform | File |
|----------|------|
| Windows | `Feidi.exe` |
| macOS   | `Feidi-macos.zip`（含 .app 目录） |

### Verify / 校验

```bash
sha256sum -c SHA256SUMS
```

### Changelog

See [CHANGELOG.md](https://github.com/irykelee/Feidi/blob/main/CHANGELOG.md) for full history.

详见 [CHANGELOG.md](https://github.com/irykelee/Feidi/blob/main/CHANGELOG.md) 获取完整历史。

### Auto-generated notes

GitHub Actions will append `generate_release_notes: true` content (commit list) automatically. Do not paste manually.

GitHub Actions 会自动追加 `generate_release_notes: true` 的 commit 列表,无需手填。

---

## Example / 示例

```markdown
## Feidi v1.1.0 — Security & Reliability Audit Remediation

Eight atomic commits landing a comprehensive code audit covering private-chat
history, file references, identity binding, resumable uploads, SSE security,
IP/MAC verification, memory quota, and documentation accuracy.

8 个原子 commit 完成全量代码审计,覆盖私聊历史、文件引用、身份认证、
断点续传、SSE 安全、IP/MAC 校验、内存配额、文档真实化。

### Highlights

- **C1 Private chat history no longer leaks** — server-side per-device filter
- **H2 device_id server-bound via SSE bearer** — prevents impersonation
- **H3 Real resumable upload** — server-side state.json with 7-day TTL
- **C4 PC login overlay** — parity with mobile client

| Platform | File |
|----------|------|
| Windows | `Feidi.exe` |
| macOS   | `Feidi-macos.zip`（含 .app 目录） |

```bash
sha256sum -c SHA256SUMS
```

See [CHANGELOG.md](https://github.com/irykelee/Feidi/blob/main/CHANGELOG.md).
```

---

## Checklist before publish / 发布前自检

- [ ] Title matches SemVer (no `nightly-` prefix on stable release)
- [ ] Body has ZH + EN summary or table
- [ ] 3 assets uploaded (`.exe`, `.zip`, `SHA256SUMS`)
- [ ] `prerelease` checkbox correct (FALSE for stable)
- [ ] `generate_release_notes: true` (auto commit list)
- [ ] Link to CHANGELOG present