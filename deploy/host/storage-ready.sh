#!/bin/sh
# OpenWrt storage guard. Suitable for the start of dockerd's start_service().
# Read-only: the mount and Docker data directory must already exist.
set -eu

fail() { printf 'Docker 儲存檢查失敗：%s\n' "$*" >&2; exit 1; }

mount_dir="${FPC_DOCKER_MOUNT:-/mnt/docker}"
data_root="${FPC_DOCKER_DATA_ROOT:-$mount_dir/data}"
for utility in awk block readlink sed tr uci; do
    command -v "$utility" >/dev/null 2>&1 || fail "找不到 $utility；請先安裝 OpenWrt 的掛載工具。"
done
case "$mount_dir" in /*) ;; *) fail 'FPC_DOCKER_MOUNT 必須是絕對路徑。' ;; esac
case "$mount_dir" in /|*/|*[[:space:]]*) fail '掛載路徑不可是根目錄、包含空白或結尾斜線。' ;; esac
case "$data_root" in "$mount_dir"/*) ;; *) fail 'Docker 資料目錄必須位於指定掛載之內。' ;; esac

expected_uuid="${FPC_DOCKER_UUID:-}"
if [ -z "$expected_uuid" ]; then
    expected_uuid="$(uci -q get fstab.docker.uuid 2>/dev/null || true)"
fi
printf '%s\n' "$expected_uuid" | awk -F- '
    NF != 5 || length($1) != 8 || length($2) != 4 || length($3) != 4 || length($4) != 4 || length($5) != 12 || /[^0-9a-fA-F-]/ {exit 1}
' || fail '請在 fstab.docker.uuid 或 FPC_DOCKER_UUID 設定預期分區 UUID。'

device="$(awk -v target="$mount_dir" '$2 == target && $3 == "ext4" && $4 ~ /(^|,)rw(,|$)/ {print $1; exit}' /proc/mounts)"
[ -n "$device" ] || fail "$mount_dir 尚未以可寫 ext4 掛載；先恢復永久掛載，勿建立替代目錄。"
actual_uuid="$(block info "$device" 2>/dev/null | sed -n 's/.* UUID="\([^"]*\)".*/\1/p' | tr 'A-F' 'a-f')"
expected_uuid="$(printf '%s' "$expected_uuid" | tr 'A-F' 'a-f')"
[ "$actual_uuid" = "$expected_uuid" ] || fail '掛載分區 UUID 不符；請檢查是否掛到另一顆磁碟。'
[ -d "$mount_dir" ] && [ -d "$data_root" ] || fail '掛載和 Docker 資料目錄必須先由管理員準備；此檢查不建立目錄。'
canonical_mount="$(readlink -f "$mount_dir")" || fail '無法確認掛載路徑。'
canonical_data="$(readlink -f "$data_root")" || fail '無法確認 Docker 資料路徑。'
[ "$canonical_mount" = "$mount_dir" ] || fail '掛載路徑不可經由符號連結。'
case "$canonical_data" in "$canonical_mount"/*) ;; *) fail 'Docker 資料路徑經由符號連結離開了指定分區。' ;; esac

configured_root="$(uci -q get dockerd.globals.data_root 2>/dev/null || true)"
[ "$configured_root" = "$data_root" ] || fail "dockerd.globals.data_root 必須是 $data_root；不要在已有資料時直接更改。"
alternative="$(uci -q get dockerd.globals.alt_config_file 2>/dev/null || true)"
[ -z "$alternative" ] || fail 'dockerd.globals.alt_config_file 覆蓋了 UCI 設定；請管理員先核對並移除覆蓋。'
