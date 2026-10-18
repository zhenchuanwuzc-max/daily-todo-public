#!/bin/bash
# Explicit release stages. No argument / --no-restart only runs isolated tests.
# --stage VERSION: test and build a draft from the existing VERSION/tag.
# --install VERSION: verify universal ZIP + SHA, then transactional local install.
# --publish VERSION: require exact local stable health and accepted ZIP, then publish latest.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GITHUB_REPO="zhenchuanwuzc-max/daily-todo-public"
PY="${DIR}/venv/bin/python"
[ -x "$PY" ] || PY="python3"

fail() { echo "✗ $*" >&2; exit 1; }

run_tests() {
    "$PY" - "$DIR" <<'PY'
import os, pathlib, subprocess, sys, tempfile
with tempfile.TemporaryDirectory(prefix='daily-todo-release-tests-') as temporary:
    root = pathlib.Path(temporary)
    for name in ('home', 'data'):
        (root / name).mkdir()
    env = dict(os.environ, HOME=str(root / 'home'), TODO_DATA_DIR=str(root / 'data'),
               TODO_DISABLE_SYNC='1', TODO_DISABLE_UPDATE='1')
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                            cwd=sys.argv[1], env=env)
    sys.exit(result.returncode)
PY
}

require_version() {
    VERSION_ARG="${1#v}"
    [[ "$VERSION_ARG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "请明确指定 x.y.z 版本号"
}

stage_release() {
    local tag="v${VERSION_ARG}" run_id="" i
    cd "$DIR"
    [ "$(cat VERSION)" = "$VERSION_ARG" ] || fail "VERSION 必须已经是 ${VERSION_ARG}；脚本不会自动改版号"
    [ -f "docs/RELEASE-${VERSION_ARG}.md" ] || fail "缺少该版本的发布说明"
    [ "$(git branch --show-current)" = main ] || fail "请从已整合并审查的 main 构建正式版草稿"
    [ -z "$(git status --porcelain)" ] || fail "工作区有未提交改动，先完成审查与提交"
    gh auth status >/dev/null 2>&1 || fail "gh 未登录"
    git fetch -q origin main --tags
    [ "$(git rev-list --count HEAD..origin/main)" = 0 ] || fail "本地 main 落后远端"
    if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
        [ "$(git rev-parse "$tag^{commit}")" = "$(git rev-parse HEAD)" ] || fail "已有同名标签指向不同提交"
    fi
    run_tests
    git push origin main
    if ! git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then git tag "$tag"; fi
    git push origin "$tag"
    for ((i = 0; i < 30; i++)); do
        run_id="$(gh run list --repo "$GITHUB_REPO" --workflow release.yml --branch "$tag" \
            --limit 1 --json databaseId -q '.[0].databaseId')"
        [ -n "$run_id" ] && break
        sleep 2
    done
    [ -n "$run_id" ] || fail "没有找到标签构建；未安装或公开 release"
    "$PY" - "$run_id" "$GITHUB_REPO" <<'PY'
import subprocess, sys
subprocess.run(['gh', 'run', 'watch', sys.argv[1], '--repo', sys.argv[2],
                '--exit-status', '--interval', '15'], check=True, timeout=3600)
PY
    gh release view "$tag" --repo "$GITHUB_REPO" --json isDraft,isPrerelease,assets
    echo "✓ ${tag} 草稿已构建；下一步运行 --install ${VERSION_ARG}，验收后运行 --publish ${VERSION_ARG}"
}

main() {
    case "${1:---no-restart}" in
        --no-restart)
            [ "$#" -le 1 ] || fail "--no-restart 不接受其他参数"
            run_tests ;;
        --stage|--install|--publish)
            [ "$#" = 2 ] || fail "$1 需要明确版本号，例如 $1 1.2.1"
            require_version "$2"
            case "$1" in
                --stage) stage_release ;;
                --install) "$PY" "$DIR/scripts/install-release.py" install "$VERSION_ARG" ;;
                --publish) "$PY" "$DIR/scripts/install-release.py" publish "$VERSION_ARG" ;;
            esac ;;
        -h|--help)
            sed -n '2,5p' "${BASH_SOURCE[0]}" ;;
        *) fail "支持 --no-restart / --stage VERSION / --install VERSION / --publish VERSION" ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
