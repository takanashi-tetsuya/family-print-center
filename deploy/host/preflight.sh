#!/bin/sh
# Read-only OpenWrt checks before building or updating the print service.
set -eu

fail() { printf '部署前檢查失敗：%s\n' "$*" >&2; exit 1; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
sh "$script_dir/storage-ready.sh"
mount_dir="${FPC_DOCKER_MOUNT:-/mnt/docker}"
data_root="${FPC_DOCKER_DATA_ROOT:-$mount_dir/data}"
minimum_free="${FPC_MIN_FREE_KB:-4194304}"
guard_path="${FPC_DOCKER_GUARD:-/usr/libexec/fpc-docker-storage-ready}"
init_path="${FPC_DOCKER_INIT:-/etc/init.d/dockerd}"
for installed_path in "$guard_path" "$init_path"; do
    case "$installed_path" in /*) ;; *) fail 'FPC_DOCKER_GUARD 與 FPC_DOCKER_INIT 必須是絕對路徑。' ;; esac
    case "$installed_path" in *[[:space:]]*) fail 'guard 與啟動腳本路徑不可包含空白。' ;; esac
done
[ -f "$guard_path" ] && [ -x "$guard_path" ] || fail "尚未安裝可執行的 $guard_path；請依部署教程人工安裝 deploy/host/storage-ready.sh 並設為 755。"
[ -f "$init_path" ] && [ -r "$init_path" ] || fail "無法讀取 $init_path；請先安裝 OpenWrt dockerd 並核對啟動腳本。"
# Require the documented one-line, fail-closed hook as the first executable line.
# A comment, a hook later in the function, or a bare call that ignores failure
# must not pass after a package upgrade replaced the init script.
awk -v guard="$guard_path" '
    function check(line, prefix, quote) {
        sub(/^[ \t]+/, "", line)
        prefix = guard
        quote = substr(line, 1, 1)
        if (quote == "\"" || quote == sprintf("%c", 39)) prefix = quote guard quote
        return index(line, prefix) == 1 && substr(line, length(prefix) + 1) ~ /^[ \t]+\|\|[ \t]+\{/ && line ~ /[;{][ \t]*return[ \t]+1[ \t]*;[ \t]*\}[ \t]*(#.*)?$/
    }
    /^[ \t]*start_service[ \t]*\([ \t]*\)[ \t]*(\{|$)/ {
        in_start = 1
        if (index($0, "{")) {
            opened = 1
            line = substr($0, index($0, "{") + 1)
            if (line !~ /^[ \t]*($|#)/) {
                checked = 1
                valid = check(line)
                exit !valid
            }
        }
        next
    }
    in_start {
        line = $0
        if (line ~ /^[ \t]*($|#)/) next
        if (!opened) {
            if ($0 !~ /^[ \t]*\{/) exit 1
            opened = 1
            line = substr($0, index($0, "{") + 1)
        }
        if (line ~ /^[ \t]*($|#)/) next
        checked = 1
        valid = check(line)
        exit !valid
    }
    END {if (!checked || !valid) exit 1}
' "$init_path" || fail "dockerd 啟動 guard 遺失或位置不正確；請依部署教程將 $guard_path || { logger -t dockerd \"Docker storage unavailable; refusing start\"; return 1; } 放在 $init_path 的 start_service() 第一個有效行，再重試。"
case "$minimum_free" in ''|*[!0-9]*) fail 'FPC_MIN_FREE_KB 必須是正整數（單位 KiB）。' ;; esac
[ "$minimum_free" -gt 0 ] 2>/dev/null || fail 'FPC_MIN_FREE_KB 必須是正整數（單位 KiB）。'
for utility in df docker; do
    command -v "$utility" >/dev/null 2>&1 || fail "找不到 $utility；請先安裝 OpenWrt Docker 與 Compose 套件。"
done
available="$(df -Pk "$mount_dir" | awk 'NR == 2 {print $4}')"
case "$available" in ''|*[!0-9]*) fail '無法讀取 Docker 分區可用空間。' ;; esac
[ "$available" -ge "$minimum_free" ] 2>/dev/null || fail "Docker 分區僅剩 $available KiB，需要至少 $minimum_free KiB；請整理已確認可刪除的資料，或明確設定 FPC_MIN_FREE_KB。"

# Reject a remote Docker context: the partition checked above belongs to this host.
if [ -n "${DOCKER_CONTEXT:-}" ]; then
    endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || fail '無法確認 Docker context。'
elif [ -n "${DOCKER_HOST:-}" ]; then
    endpoint="$DOCKER_HOST"
else
    endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" || fail '無法確認 Docker context。'
fi
case "$endpoint" in unix:///*) ;; *) fail 'OpenWrt 更新入口只支援本機 Docker socket；請切回本機 context。' ;; esac
running_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" || fail 'Docker 尚未正常啟動；確認永久掛載與啟動 guard 後再啟動 dockerd。'
[ "$running_root" = "$data_root" ] || fail "執行中的 Docker 資料目錄不是 $data_root；請先核對設定並安全重啟 dockerd。"
docker compose version >/dev/null 2>&1 || fail '找不到可用的 Docker Compose；請安裝 OpenWrt 的 docker-compose 套件。'
printf '部署前檢查通過：%s，可用 %s KiB，Docker 資料目錄 %s。\n' "$mount_dir" "$available" "$running_root"
