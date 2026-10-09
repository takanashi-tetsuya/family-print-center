#!/bin/sh
# Download the fonts listed in sources.txt that are not in cache/ yet.
# Files already present with the right checksum are never downloaded again,
# so a copied project builds without reaching these sites.
set -u

fonts_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cache="$fonts_dir/cache"
mkdir -p "$cache"
failed=0

download() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 20 --max-time 900 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 60 -O "$2" "$1"
    else
        printf '找不到 curl 或 wget，無法下載字體。\n' >&2
        return 1
    fi
}

while read -r sum name url; do
    case "$sum" in ''|'#'*) continue ;; esac
    target="$cache/$name"
    if [ -f "$target" ] && [ "$(sha256sum "$target" | cut -d ' ' -f 1)" = "$sum" ]; then
        continue
    fi
    printf '下載字體：%s\n' "$name"
    if download "$url" "$target.part" && [ "$(sha256sum "$target.part" | cut -d ' ' -f 1)" = "$sum" ]; then
        mv "$target.part" "$target"
    else
        rm -f "$target.part"
        printf '字體下載失敗或內容不符，已略過：%s\n' "$name" >&2
        failed=1
    fi
done < "$fonts_dir/sources.txt"

exit "$failed"
