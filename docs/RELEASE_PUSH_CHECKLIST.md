# Feidi Release & Push Checklist / 发布推送检查清单

> **Use this before any `git tag` / `git push tag` / `gh release create` operation.**
> 在执行任何 `git tag` / `git push tag` / `gh release create` 前必走本清单。
>
> Single-page checkbox version (ClipMemory pattern). For long-form, see `docs/RELEASE.md`.
> 单页勾选版(沿用 ClipMemory 模式)。长流程见 `docs/RELEASE.md`。

---

## A. Local Prep / 本地准备

- [ ] **A1 — version decided** / 版本号已定 (SemVer: `MAJOR.MINOR.PATCH`)
- [ ] **A2 — `CHANGELOG.md` updated** / `CHANGELOG.md` 已更新 (bilingual ZH + EN sections)
- [ ] **A3 — `README.md` H1 + body updated** if API/CLI changed / 如有 API/CLI 改动,`README.md` H1 + 正文已更新
- [ ] **A3b — `README.en.md` updated** (translates A3) / `README.en.md` 已更新 (同步 A3)
- [ ] **A4 — `build.spec` / `build_mac.spec` icon paths / app version** verified / PyInstaller spec 文件检查通过
- [ ] **A5 — preflight script** (if `Scripts/pre_push_verify.sh` exists) / 跑 `Scripts/pre_push_verify.sh`
- [ ] **A6 — `transfer.py --help`** tested locally / 本地 `python3 transfer.py --help` 通过

## B. Commit / 提交

- [ ] **B1 — atomic commits per logical concern** / 每个关注点一个原子 commit
- [ ] **B2 — conventional commit prefixes** (`feat:` / `fix:` / `chore:` / `docs:`) / commit message 规范
- [ ] **B3 — pre-commit hook passes** (no `--no-verify` bypass) / pre-commit hook 通过 (禁止 `--no-verify` 绕路)

## C. Push & Tag / 推送与打 tag

- [ ] **C1 — `git pull --rebase`** before push (avoid CI re-runs with stale tree) / push 前先 `git pull --rebase`
- [ ] **C2 — `git push origin main`** succeeds / `git push origin main` 成功
- [ ] **C3 — wait for CI green** on push-to-main (nightly build + release job) / 等 main 上 CI 转绿
- [ ] **C4 — `git tag -a vX.Y.Z -m "<message>"`** with annotated tag / 用 annotated tag
- [ ] **C5 — `git push origin vX.Y.Z`** triggers tag workflow / `git push origin vX.Y.Z` 触发 tag workflow
- [ ] **C6 — confirm CI run with `GITHUB_REF_TYPE=tag`** / 确认 CI 运行在 `GITHUB_REF_TYPE=tag` 模式下

## D. GitHub Release Page / 发布页

- [ ] **D1 — release title** is `Feidi vX.Y.Z` (not `Nightly build ...`) / 发布标题为正式版本号
- [ ] **D2 — `prerelease` flag is FALSE** for tag releases / tag release 不勾 prerelease
- [ ] **D3 — release body** has ZH table (Platform | File) + SHA256SUMS verify snippet / 正文有中文表格 + 校验命令
- [ ] **D4 — assets** = `Feidi.exe`, `Feidi-macos.zip`, `SHA256SUMS` (3 files) / 三个产物
- [ ] **D5 — auto-generated release notes** enabled (`generate_release_notes: true`) / 启用自动生成 release notes
- [ ] **D6 — CHANGELOG link** present in body or notes / 正文或 notes 中有 CHANGELOG 链接

## E. Local Cleanup / 本地清理

- [ ] **E1 — `git fetch --tags`** to sync local tag list / `git fetch --tags` 同步本地 tag
- [ ] **E2 — worktree / branch cleanup** if used / 若用 worktree/分支则清理
- [ ] **E3 — `feidi_identities.json`** still gitignored / `feidi_identities.json` 仍在 .gitignore
- [ ] **E4 — session memory** updated with release SHA + any new gotchas / session memory 更新发布 SHA + 新坑
- [ ] **E5 — `STATUS.md`** (if exists) updated with version / 若有 STATUS.md,更新版本

---

## Anti-Patterns to Avoid / 避免的反模式

| ❌ 反模式 | ✅ 正确做法 |
|---|---|
| 标题还是 "Nightly build xxx" (用了旧 workflow) | 标题 = `Feidi vX.Y.Z` (新的 tag 分支逻辑生效) |
| `prerelease: true` 但不是夜间构建 | tag release 必须是 `prerelease: false` |
| Release 没 SHA256SUMS | 校验文件是 release 完整性的关键,不可省 |
| `CHANGELOG.md` 只更新 ZH,EN 没改 | EN 必须同步 (v1.1.0+ 引入) |
| `--no-verify` 绕过 pre-commit | 一律 hook 通过,绝对不绕 |
| 多人 fork 的 SHA 引用没标注 | release notes 标 `自动构建于 ${{ github.sha }}` 即可溯源 |

---

## Reference / 参考

- `CHANGELOG.md` — version history
- `docs/RELEASE.md` (TODO if needed) — long-form 12-step prose
- `docs/release-notes-template.md` — release body template
- `Scripts/pre_push_verify.sh` — local preflight
- `~/.claude/projects/-Users-iryke/memory/feedback/release-push-checklist.md` (ClipMemory source pattern)