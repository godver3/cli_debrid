#!/usr/bin/env bash
# Run every check before pushing or live-patching: the same checks every time.
#
#   scripts/check.sh            everything
#   scripts/check.sh --quick    skip slow tests (pytest -m "not slow") and the per-folder runs
#
# PYTHON picks the interpreter (default: python3, which needs requirements-linux.txt,
# pytest and pyflakes installed). CLI_MOUNT_DIR points at a cli_mount (decypharr)
# checkout to also vet and test it; by default ../decypharr is used when it exists.
set -uo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
CLI_MOUNT_DIR="${CLI_MOUNT_DIR:-../decypharr}"
PYTEST_ARGS=()
[[ "${1:-}" == "--quick" ]] && PYTEST_ARGS=(-m "not slow")

failed=()
skipped=()
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { failed+=("$1"); printf '\033[31mFAILED: %s\033[0m\n' "$1"; }
skip() { skipped+=("$1"); printf '\033[33mskipped: %s\033[0m\n' "$1"; }

# Tests write their files under a throwaway folder, never /user.
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
export USER_CONFIG="$scratch" USER_DB_CONTENT="$scratch" USER_LOGS="$scratch"

step "pytest"
"$PYTHON" -m pytest -q "${PYTEST_ARGS[@]}" || fail "pytest"

if [[ "${1:-}" != "--quick" ]]; then
    step "pytest: each test folder on its own"
    # The full run imports every file first, so a test that only passes because another
    # file loaded a real module (or a stub) for it goes unnoticed there.
    for dir in tests/*/; do
        [[ -f "$dir/__init__.py" ]] || continue
        "$PYTHON" -m pytest -q -p no:warnings "$dir" >"$scratch/area.log" 2>&1 \
            && echo "ok  $dir" || { tail -20 "$scratch/area.log"; fail "pytest $dir alone"; }
    done
fi

step "pyflakes: undefined names"
# Unfiltered on purpose: grepping for names you expect misses the ones you don't.
undefined="$(find . -name '*.py' -not -path './tests/*' -not -path './phalanx_db_hyperswarm/*' \
    -not -path './node_modules/*' -print0 | xargs -0 "$PYTHON" -m pyflakes 2>/dev/null | grep "undefined name '")"
if [[ -n "$undefined" ]]; then echo "$undefined"; fail "pyflakes"; else echo "none"; fi

step "ruff"
if "$PYTHON" -m ruff --version >/dev/null 2>&1; then
    "$PYTHON" -m ruff check . || fail "ruff"
else
    skip "ruff not installed ($PYTHON -m pip install ruff)"
fi

step "node --check: static JS"
if command -v node >/dev/null 2>&1; then
    js_failed=0
    for f in static/js/*.js; do node --check "$f" || js_failed=1; done
    node tests/content_sources/test_discover_addmedia.js || js_failed=1
    [[ $js_failed == 0 ]] && echo "ok" || fail "node --check"
else
    skip "node not installed"
fi

step "cli_mount: go vet + go test ($CLI_MOUNT_DIR)"
if [[ -f "$CLI_MOUNT_DIR/go.mod" ]] && command -v go >/dev/null 2>&1; then
    (cd "$CLI_MOUNT_DIR" && go vet ./... && go test ./...) || fail "cli_mount"
else
    skip "no cli_mount checkout at $CLI_MOUNT_DIR, or go not installed"
fi

echo
if (( ${#failed[@]} )); then
    printf '\033[31m%d check(s) failed: %s\033[0m\n' "${#failed[@]}" "${failed[*]}"
    exit 1
fi
if (( ${#skipped[@]} )); then
    printf '\033[33mAll checks that ran passed; skipped: %s\033[0m\n' "${skipped[*]%% (*}"
else
    printf '\033[32mAll checks passed.\033[0m\n'
fi
