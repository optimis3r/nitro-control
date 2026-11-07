#!/usr/bin/env bash
# Install the desktop app, its dependencies, permissions and supported Acer driver.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

usage() {
    echo "Usage: ./install.sh [--without-driver | --remove]"
    echo "Run from your desktop account; sudo is requested when needed."
}
case ${1:-} in
    --help|-h) usage; exit 0 ;;
    ''|--without-driver|--remove) ;;
    *) usage >&2; exit 1 ;;
esac
(( $# <= 1 )) || { usage >&2; exit 1; }
if (( EUID != 0 )); then
    exec sudo -- "$PWD/install.sh" "$@"
fi
if [[ ! ${SUDO_USER:-} =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ || ${SUDO_UID:-0} == 0 ]] ||
   [[ $(id -u "$SUDO_USER") != "$SUDO_UID" ]]; then
    echo "Run ./install.sh from the desktop account that will use Nitro Control." >&2
    exit 1
fi
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
app_dir=/usr/local/lib/nitro-control
unit=nitro-control-driver.service
revision=4483b6c38b9bb963ac9ada9ca476578e0c63cc08
driver_version=${revision:0:12}
kernel=$(uname -r)
mode=${1:-}
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT
trap 'echo "Installation did not finish. Fix the error above and rerun ./install.sh." >&2' ERR

# Run desktop configuration as the account owner, never as root.
desktop_setup() {
    sudo -H -u "$SUDO_USER" /usr/bin/python3 - "$1" <<'PY'
from pathlib import Path
import shutil
import sys

remove = sys.argv[1] == "remove"
home = Path.home()
applications = home / ".local/share/applications"
launcher = applications / "io.local.NitroControl.desktop"
if remove:
    launcher.unlink(missing_ok=True)
else:
    applications.mkdir(parents=True, exist_ok=True)
    shutil.copyfile("/usr/local/share/applications/io.local.NitroControl.desktop", launcher)

hypr = home / ".config/hypr"
main = hypr / "hyprland.conf"
config = hypr / "nitro-control.conf"
source = "source = ~/.config/hypr/nitro-control.conf"
# Migrate the old project-path shortcuts while preserving unrelated bindings.
for path in (main, hypr / "UserConfigs/UserKeybinds.conf"):
    if not path.is_file():
        continue
    old = path.read_text()
    lines = [line for line in old.splitlines(keepends=True)
             if line.strip() != source and not
             (line.lstrip().startswith("bind") and "F23" in line and
              ("nitro_gui.py" in line or "/usr/local/bin/nitro-control --popup" in line))]
    new = "".join(lines)
    if path == main and not remove:
        new = new.rstrip("\n") + "\n" + source + "\n"
    if old != new:
        backup = path.with_name(path.name + ".before-nitro-control")
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(new)
if remove:
    config.unlink(missing_ok=True)
elif main.is_file():
    config.write_text(
        "# Managed by Nitro Control's installer.\n"
        "bind = SUPER SHIFT, F23, exec, /usr/local/bin/nitro-control --popup\n"
        "bind = , F23, exec, /usr/local/bin/nitro-control --popup\n")
PY
}

if [[ $mode == --remove ]]; then
    # Restore firmware Auto before stopping a driver used by this app.
    if [[ -f /sys/devices/platform/acer-wmi/nitro_sense/fan_speed && -f $app_dir/nitro.py ]]; then
        /usr/bin/python3 -E "$app_dir/nitro.py" fan auto
    fi
    systemctl disable --now "$unit" 2>/dev/null || true
    if command -v dkms >/dev/null && [[ -d /var/lib/dkms/nitro-control/$driver_version ]]; then
        dkms remove -m nitro-control -v "$driver_version" --all
    fi
    # Only unload a driver when this installer owned its boot service.
    if [[ -f /etc/systemd/system/$unit ]]; then
        if [[ -d /sys/module/linuwu_sense ]]; then
            modprobe -r linuwu_sense
        fi
        modprobe acer_wmi
    fi
    rm -f -- "/etc/systemd/system/$unit"
    rm -rf -- "/usr/src/nitro-control-$driver_version"
    if [[ -d /run/systemd/system ]]; then systemctl daemon-reload; fi
    ./install-permissions.sh --remove
    desktop_setup remove
    rm -f -- /usr/local/bin/nitro-control /usr/local/bin/nitro \
        /usr/local/share/applications/io.local.NitroControl.desktop
    rm -rf -- "$app_dir"
    echo "Nitro Control removed. Installed distribution packages were kept."
    exit 0
fi

install_driver=false
if [[ $mode != --without-driver && $(cat /sys/class/dmi/id/sys_vendor 2>/dev/null) == Acer &&
      $(cat /sys/class/dmi/id/product_name 2>/dev/null) == 'Nitro ANV15-41' ]]; then
    install_driver=true
    [[ -d /run/systemd/system ]] || { echo "Driver setup requires systemd." >&2; exit 1; }
fi

# Use distribution packages for PyGObject/GTK rather than pip.
. /etc/os-release
case " $ID ${ID_LIKE:-} " in
    *' arch '*)
        packages=(python gtk4 libadwaita python-gobject python-cairo polkit)
        if $install_driver; then
            # pkgbase identifies linux, linux-lts, linux-zen and custom kernels.
            if [[ -r /usr/lib/modules/$kernel/pkgbase ]]; then
                kernel_package=$(cat "/usr/lib/modules/$kernel/pkgbase")
            else
                echo "Cannot identify the running kernel's header package. Reboot after a kernel update and retry, or use --without-driver." >&2
                exit 1
            fi
            packages+=(git base-devel dkms kmod "$kernel_package-headers")
        fi
        pacman -S --needed --noconfirm "${packages[@]}"
        ;;
    *' debian '*|*' ubuntu '*)
        packages=(python3 python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0 gir1.2-adw-1 pkexec polkitd)
        if $install_driver; then packages+=(git build-essential dkms kmod "linux-headers-$kernel"); fi
        apt-get update
        apt-get install -y "${packages[@]}"
        ;;
    *' fedora '*)
        packages=(python3 python3-gobject python3-cairo gtk4 libadwaita polkit)
        if $install_driver; then packages+=(git gcc make dkms kmod "kernel-devel-$kernel"); fi
        dnf install -y "${packages[@]}"
        ;;
    *) echo "Automatic dependencies support Arch, Debian/Ubuntu and Fedora. Unsupported distribution: $ID" >&2; exit 1 ;;
esac
/usr/bin/python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    sys.exit("Nitro Control requires Python 3.9 or newer.")
import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Adw, GdkPixbuf
PY
command -v pkexec >/dev/null

if $install_driver; then
    if [[ ! -r /lib/modules/$kernel/build/Makefile ]]; then
        echo "Headers for running kernel $kernel are missing. Reboot into the installed kernel, then rerun ./install.sh." >&2
        exit 1
    fi
    git -C "$staging" init -q
    git -C "$staging" fetch -q --depth=1 https://github.com/0x7375646F/Linuwu-Sense.git "$revision"
    git -C "$staging" checkout -q --detach "$revision"
    # Own the DKMS registration; do not run upstream's unrelated app installer.
    cat > "$staging/dkms.conf" <<DKMS
PACKAGE_NAME="nitro-control"
PACKAGE_VERSION="$driver_version"
AUTOINSTALL="yes"
MAKE[0]="make -C /lib/modules/\${kernelver}/build M=\${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/build modules"
CLEAN="make -C /lib/modules/\${kernelver}/build M=\${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/build clean"
BUILT_MODULE_NAME[0]="linuwu_sense"
BUILT_MODULE_LOCATION[0]="src/"
DEST_MODULE_LOCATION[0]="/updates/dkms"
DKMS
    if [[ ! -d /var/lib/dkms/nitro-control/$driver_version ]]; then
        install -d -m 0755 "/usr/src/nitro-control-$driver_version"
        cp -R -- "$staging/src" "$staging/Makefile" "$staging/dkms.conf" "/usr/src/nitro-control-$driver_version/"
        dkms add -m nitro-control -v "$driver_version"
    fi
    dkms install -m nitro-control -v "$driver_version" -k "$kernel"
    depmod -a "$kernel"
fi

install -d -o root -g root -m 0755 "$app_dir/assets" /usr/local/bin /usr/local/share/applications
install -o root -g root -m 0644 nitro.py nitro_gui.py "$app_dir/"
install -o root -g root -m 0644 assets/*.png "$app_dir/assets/"
./install-permissions.sh
cat > "$staging/nitro-control" <<'SH'
#!/bin/sh
exec /usr/bin/python3 -E /usr/local/lib/nitro-control/nitro_gui.py "$@"
SH
cat > "$staging/nitro" <<'SH'
#!/bin/sh
exec /usr/bin/python3 -E /usr/local/lib/nitro-control/nitro.py "$@"
SH
install -o root -g root -m 0755 "$staging/nitro-control" "$staging/nitro" /usr/local/bin/
cat > "$staging/desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Nitro Control
Comment=Laptop cooling and performance controls
Exec=/usr/local/bin/nitro-control
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
DESKTOP
install -o root -g root -m 0644 "$staging/desktop" /usr/local/share/applications/io.local.NitroControl.desktop
desktop_setup install
if command -v update-desktop-database >/dev/null; then
    update-desktop-database /usr/local/share/applications
fi
if $install_driver; then
    cat > "$staging/service" <<'UNIT'
[Unit]
Description=Nitro Control Acer fan driver
After=systemd-modules-load.service
ConditionPathExists=/sys/class/dmi/id/product_name

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -E /usr/local/lib/nitro-control/nitro.py load-driver
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
    install -o root -g root -m 0644 "$staging/service" "/etc/systemd/system/$unit"
    systemctl daemon-reload
    # Load and verify first; a failed driver must not be enabled at boot.
    if ! /usr/bin/python3 -E "$app_dir/nitro.py" load-driver; then
        systemctl disable "$unit" 2>/dev/null || true
        echo "App files installed, but Acer driver setup failed. Check the kernel log (including Secure Boot signing), then rerun ./install.sh." >&2
        exit 1
    fi
    systemctl enable --now "$unit"
fi
/usr/local/bin/nitro status
echo "Nitro Control installed. Open Nitro Control in the app menu or run nitro-control."
echo "Hyprland shortcuts, when configured: F23 and Super+Shift+F23 (reload Hyprland to activate)."
echo "A desktop Polkit authentication agent is needed for operations that request a password."
