#!/bin/bash
# Feidi pre-push verification script
# 飞递发版前本地预检查脚本
#
# Usage: bash Scripts/pre_push_verify.sh
# 在 push tag 前必跑本脚本
#
# Exit codes:
#   0 — all checks pass
#   1 — one or more checks failed

set -e

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
cd "$ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'  # no color

PASS=0
FAIL=0

check() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} $name"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}✗${NC} $name"
        echo "    cmd: $cmd"
        FAIL=$((FAIL + 1))
    fi
}

warn() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} $name"
        PASS=$((PASS + 1))
    else
        echo -e "${YELLOW}!${NC} $name (warning, not blocking)"
        echo "    cmd: $cmd"
        # warnings don't increment FAIL
    fi
}

echo "=== Feidi pre-push verification ==="
echo "Repo root: $ROOT"
echo ""

# --- A. Local prep ---
echo "[A] Local prep"
check "transfer.py syntax valid" "python3 -c 'import ast; ast.parse(open(\"transfer.py\").read())'"
check "transfer.py --help works" "python3 transfer.py --help"
check "CHANGELOG.md exists" "test -f CHANGELOG.md"
check "README.md exists (zh)" "test -f README.md"
check "README.en.md exists (en)" "test -f README.en.md"
check ".gitignore present" "test -f .gitignore"
check "feidi_identities.json NOT in git" "! git ls-files | grep -q 'feidi_identities.json'"
check "build/ NOT in git" "! git ls-files | grep -q '^build/'"
warn "build_mac.spec exists" "test -f build_mac.spec"
warn "build.spec exists" "test -f build.spec"

# --- B. Commit hygiene ---
echo ""
echo "[B] Commit hygiene"
check "Working tree clean" "git diff --quiet HEAD"
check "Pre-commit hook configured" "test -f ~/.claude/bin/git-hooks/pre-commit || test -f /Users/iryke/bin/git-hooks/pre-commit || git config --get core.hooksPath"

# --- D. Release assets ---
echo ""
echo "[D] Release assets (verify after build)"
warn "build/EXE-00.toc exists (recent build)" "test -f build/EXE-00.toc"
warn "build/PYZ-00.toc exists (recent build)" "test -f build/PYZ-00.toc"
warn "build/Feidi.pkg exists (recent build)" "test -f build/Feidi.pkg"

# --- Summary ---
echo ""
echo "=== Summary ==="
echo "  Passed: $PASS"
echo "  Failed: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}✗ Some checks failed. Fix before pushing tag.${NC}"
    exit 1
else
    echo -e "${GREEN}✓ All blocking checks passed. Safe to push tag.${NC}"
    exit 0
fi