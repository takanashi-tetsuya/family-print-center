#!/bin/sh
# Image build step: unpack verified font downloads and the private fonts into one tree.
# A missing or altered download is skipped, never installed.
set -eu

src=$1
out=$2
mkdir -p "$out"
out=$(CDPATH= cd -- "$out" && pwd)

while read -r sum name url; do
    case "$sum" in ''|'#'*) continue ;; esac
    file="$src/cache/$name"
    if [ ! -f "$file" ]; then
        printf 'font archive missing, skipped: %s\n' "$name" >&2
        continue
    fi
    if [ "$(sha256sum "$file" | cut -d ' ' -f 1)" != "$sum" ]; then
        printf 'font archive checksum mismatch, skipped: %s\n' "$name" >&2
        continue
    fi
    case "$name" in
        *.exe)
            mkdir -p "$out/microsoft-core"
            # Extract accompanying licence text too; executables and other
            # installer files are removed below and are never executed.
            cabextract -q -L -d "$out/microsoft-core" "$file" ;;
        *.zip)
            mkdir -p "$out/${name%.zip}"
            unzip -q -o -j "$file" -d "$out/${name%.zip}" ;;
    esac
done < "$src/sources.txt"

# Keep font licences/copyright notices from checksum-verified public archives.
# Other metadata and installer files are not part of the font installation.
find "$out" -type f ! \( -iname '*.ttf' -o -iname '*.ttc' -o -iname '*.otf' -o -iname '*.otc' \
    -o -iname 'license' -o -iname 'licence' -o -iname 'copying' -o -iname 'copyright' \
    -o -iname 'ofl' -o -iname 'eula' -o -iname 'notice' \
    -o -iname '*licen[cs]e*.txt' -o -iname '*licen[cs]e*.md' \
    -o -iname 'copying*.txt' -o -iname 'copying*.md' \
    -o -iname '*copyright*.txt' -o -iname '*copyright*.md' \
    -o -iname 'ofl*.txt' -o -iname 'ofl*.md' -o -iname '*eula*.txt' -o -iname '*eula*.md' \
    -o -iname 'notice*.txt' -o -iname 'notice*.md' \) -delete

# Private files are filtered separately: even a file named LICENSE.txt here
# must not accidentally be copied into an image alongside household fonts.
if [ -d "$src/private" ]; then
    (cd "$src/private" && find . -type f \
        \( -iname '*.ttf' -o -iname '*.ttc' -o -iname '*.otf' -o -iname '*.otc' \) \
        -exec sh -c '
            out=$1
            shift
            for font; do
                relative=${font#./}
                mkdir -p "$out/private/$(dirname "$relative")"
                cp "$font" "$out/private/$relative"
            done
        ' sh "$out" {} +)
fi
chmod -R a+rX "$out"
