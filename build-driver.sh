#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
kernel=$(uname -r)
revision=4483b6c38b9bb963ac9ada9ca476578e0c63cc08
output=".build/$kernel/linuwu_sense.ko"
headers="/lib/modules/$kernel/build"
if [[ ! -r "$headers/Makefile" ]]; then
    headers="/usr/lib/modules/$kernel/build"
fi
[[ -r "$headers/Makefile" ]] || {
    echo "Missing headers for running kernel $kernel." >&2
    exit 1
}

source_dir=$(mktemp -d)
trap 'rm -rf -- "$source_dir"' EXIT
git -C "$source_dir" init -q
git -C "$source_dir" fetch -q --depth=1 https://github.com/0x7375646F/Linuwu-Sense.git "$revision"
git -C "$source_dir" checkout -q --detach "$revision"
make -C "$headers" M="$source_dir" modules
mkdir -p -- "$(dirname -- "$output")"
install -m 0644 -- "$source_dir/src/linuwu_sense.ko" "$output"
printf 'Built %s\n' "$output"
