#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

case "${1-}" in
    install|update|rollback) action=$1; shift ;;
    -h|--help)
        printf '%s\n' 'Usage: sh install.sh [install|update|rollback] [--check] [deploy options]'
        exit 0
        ;;
    ''|-*) action=install ;;
    *) fail "Unknown action: $1. Use install, update, or rollback." ;;
esac

for utility in wget tar mktemp rm; do
    command -v "$utility" >/dev/null 2>&1 || fail "Missing required command: $utility"
done

python=/usr/share/klippy-env/bin/python
if [ ! -x "$python" ]; then
    python=$(command -v python3) || fail 'Python 3.8 or later is required.'
fi
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || fail 'Python 3.8 or later is required.'

tmp_parent=$(CDPATH= cd "${TMPDIR:-/tmp}" && pwd -P) || fail 'Cannot resolve the temporary directory.'
work=$(mktemp -d "${tmp_parent%/}/cfs-install.XXXXXX") || fail 'Cannot create an installation directory.'
case "$work" in
    "${tmp_parent%/}"/cfs-install.*) ;;
    *) fail 'mktemp returned a path outside the temporary directory.' ;;
esac
trap 'rm -rf -- "$work"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

archive=$work/release.tar.gz
wget -O "$archive" 'https://codeload.github.com/truestealth/cfs_klipper/tar.gz/refs/heads/main' || fail 'Could not download the CFS release.'
tar -xzf "$archive" -C "$work" || fail 'Could not extract the CFS release.'
deploy=$work/cfs_klipper-main/tools/deploy.py
[ -f "$deploy" ] || fail 'The CFS archive does not contain tools/deploy.py.'

"$python" "$deploy" "$action" \
    --extras /usr/share/klipper/klippy/extras \
    --state-dir /usr/data/cfs-deployment "$@"
