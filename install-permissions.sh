#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if (( EUID != 0 )) || [[ ! ${SUDO_USER:-} =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ || ${SUDO_UID:-0} == 0 ]]; then
    echo "Run with sudo from the desktop account that uses Nitro Control." >&2
    exit 1
fi

helper_dir=/usr/local/lib/nitro-control
policy=/usr/share/polkit-1/actions/io.local.NitroControl.policy
rule=/etc/polkit-1/rules.d/49-nitro-control.rules

if [[ ${1:-} == --remove ]]; then
    rm -f -- "$rule" "$policy" "$helper_dir/nitro-helper" "$helper_dir/nitro.py"
    echo "Nitro Control desktop permission removed."
    exit
fi

if (( $# != 0 )); then
    echo "Usage: sudo ./install-permissions.sh [--remove]" >&2
    exit 1
fi

install -d -o root -g root -m 0755 "$helper_dir"
install -o root -g root -m 0644 nitro.py "$helper_dir/nitro.py"
install -o root -g root -m 0755 nitro-helper "$helper_dir/nitro-helper"
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT

cat > "$staging/policy" <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD polkit Policy Configuration 1.0//EN"
"http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="io.local.NitroControl.settings">
    <description>Change Nitro Control hardware settings</description>
    <message>Authentication is required to change Nitro Control settings</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>no</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/lib/nitro-control/nitro-helper</annotate>
  </action>
</policyconfig>
XML

cat > "$staging/rule" <<JS
polkit.addRule(function(action, subject) {
    if (action.id === "io.local.NitroControl.settings" &&
        subject.local && subject.active && subject.user === "$SUDO_USER") {
        return polkit.Result.YES;
    }
});
JS

install -o root -g root -m 0644 "$staging/policy" "$policy"
install -o root -g root -m 0644 "$staging/rule" "$rule"
echo "Nitro Control settings enabled for the active local session of $SUDO_USER."
