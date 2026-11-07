"""Offline installer tests. All system paths and privileged commands are isolated."""
import os
import re
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nitro


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.system = self.base / "system"
        self.project = self.base / "project"
        self.bin = self.base / "bin"
        self.home = self.base / "home"
        self.log = self.base / "commands"
        for path in (self.project, self.bin, self.home):
            path.mkdir()
        # Rewrite every system destination before executing the real installer.
        for name in ("install.sh", "install-permissions.sh"):
            source = (ROOT / name).read_text().replace("EUID", "fixture_euid")
            source = re.sub(r"(?:/usr/local|/usr/share|/usr/src|/usr/lib/modules|/lib/modules|/var/lib|/etc|/sys|/run)\b",
                            lambda match: str(self.system / match[0].lstrip("/")), source)
            source = source.replace("/usr/bin/python3", str(self.bin / "fixture-python"))
            source = source.replace("export PATH=", f"export PATH={self.bin}:")
            path = self.project / name
            path.write_text(source)
            path.chmod(0o755)
        for name in ("nitro.py", "nitro_gui.py", "nitro-helper"):
            (self.project / name).write_text((ROOT / name).read_text())
        (self.project / "assets").mkdir()
        (self.project / "assets/test.png").write_bytes(b"fixture")
        self.write("etc/os-release", "ID=arch\n")
        self.write("sys/class/dmi/id/sys_vendor", "Acer\n")
        self.write("sys/class/dmi/id/product_name", "Nitro ANV15-41\n")
        self.write("usr/lib/modules/fixture-kernel/pkgbase", "linux\n")
        self.write("lib/modules/fixture-kernel/build/Makefile", "# headers\n")
        (self.system / "run/systemd/system").mkdir(parents=True)
        (self.system / "etc/systemd/system").mkdir(parents=True)
        hypr = self.home / ".config/hypr"
        (hypr / "UserConfigs").mkdir(parents=True)
        (hypr / "hyprland.conf").write_text("# existing config\n")
        (hypr / "UserConfigs/UserKeybinds.conf").write_text(
            "bind = SUPER, Q, exec, terminal\n"
            "bindd = , F23, Nitro, exec, python '/old project/nitro_gui.py' --popup\n")
        self.command("uname", 'echo fixture-kernel\n')
        self.command("install", '''
args=()
while (( $# )); do
    case $1 in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
done
exec /usr/bin/install "${args[@]}"
''')
        self.command("id", 'echo 1000\n')
        self.command("sudo", 'shift 3\nHOME="$FIXTURE_HOME" exec "$@"\n')
        self.command("fixture-python", '''
if [[ ${1:-} == - && $# == 1 ]]; then cat >/dev/null; exit 0; fi
if [[ ${1:-} == -E ]]; then
    if [[ ${3:-} == load-driver && ${FAIL_DRIVER:-0} == 1 ]]; then exit 1; fi
    exit 0
fi
exec /usr/bin/python3 "$@"
''')
        self.command("git", '''
if [[ ${3:-} == checkout ]]; then
    mkdir -p "$2/src"
    touch "$2/src/linuwu_sense.c" "$2/Makefile"
fi
''')
        self.command("dkms", '''
if [[ ${1:-} == add ]]; then mkdir -p "$FIXTURE_SYSTEM/var/lib/dkms/$3/$5"; fi
''')
        for name in ("pacman", "apt-get", "dnf", "systemctl", "depmod", "modprobe", "pkexec", "update-desktop-database"):
            self.command(name, ":\n")
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", fixture_euid="0",
                        SUDO_USER="fixture", SUDO_UID="1000", FIXTURE_HOME=str(self.home),
                        FIXTURE_SYSTEM=str(self.system), FIXTURE_LOG=str(self.log))

    def write(self, relative, content):
        path = self.system / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def command(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\nprintf "%s\\n" "${0##*/} $*" >> "$FIXTURE_LOG"\n' + body)
        path.chmod(0o755)

    def run_installer(self, *args, fail_driver=False):
        result = subprocess.run([str(self.project / "install.sh"), *args],
                                env=dict(self.env, FAIL_DRIVER=str(int(fail_driver))),
                                capture_output=True, text=True)
        return result

    def test_full_install_repeat_and_remove(self):
        for _ in range(2):
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        app = self.system / "usr/local/lib/nitro-control"
        self.assertTrue((app / "assets/test.png").is_file())
        self.assertTrue((app / "nitro-helper").is_file())
        self.assertTrue((self.system / "etc/systemd/system/nitro-control-driver.service").is_file())
        main = self.home / ".config/hypr/hyprland.conf"
        self.assertEqual(main.read_text().count("source ="), 1)
        binds = self.home / ".config/hypr/UserConfigs/UserKeybinds.conf"
        self.assertEqual(binds.read_text(), "bind = SUPER, Q, exec, terminal\n")
        self.assertTrue(binds.with_name(binds.name + ".before-nitro-control").is_file())
        desktop = self.home / ".local/share/applications/io.local.NitroControl.desktop"
        self.assertIn(str(self.system / "usr/local/bin/nitro-control"), desktop.read_text())
        if shutil.which("desktop-file-validate"):
            subprocess.run(["desktop-file-validate", str(desktop)], check=True)
        log = self.log.read_text()
        self.assertEqual(log.count("dkms add "), 1)
        self.assertIn("systemctl enable --now nitro-control-driver.service", log)
        result = self.run_installer("--remove")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(app.exists())
        self.assertFalse(desktop.exists())
        self.assertNotIn("nitro-control.conf", main.read_text())
        self.assertIn("modprobe acer_wmi", self.log.read_text())

    def test_without_driver(self):
        result = self.run_installer("--without-driver")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("dkms ", self.log.read_text())
        self.assertNotIn("systemctl enable", self.log.read_text())

    def test_other_model_skips_driver(self):
        self.write("sys/class/dmi/id/product_name", "Another laptop\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("dkms ", self.log.read_text())

    def test_failed_driver_is_not_enabled(self):
        result = self.run_installer(fail_driver=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("systemctl enable", self.log.read_text())
        self.assertIn("systemctl disable nitro-control-driver.service", self.log.read_text())
        self.assertIn("driver setup failed", result.stderr)

    def test_other_distribution_package_branches(self):
        for distro, manager in (("debian", "apt-get"), ("ubuntu", "apt-get"), ("fedora", "dnf")):
            with self.subTest(distro=distro):
                self.write("etc/os-release", f"ID={distro}\n")
                result = self.run_installer("--without-driver")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"{manager} install -y", self.log.read_text())

    def test_missing_headers_stops_before_boot_setup(self):
        (self.system / "lib/modules/fixture-kernel/build/Makefile").unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Reboot", result.stderr)
        self.assertNotIn("dkms install", self.log.read_text())


class DriverTests(unittest.TestCase):
    def test_installed_driver_failure_restores_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = Path(directory)
            (sysfs / "module/acer_wmi").mkdir(parents=True)
            error = subprocess.CalledProcessError(1, ["modprobe", "linuwu_sense"])
            with patch.object(nitro, "SYS", sysfs), patch.object(nitro, "FAN", sysfs / "missing"), \
                 patch.object(nitro, "driver_module", return_value=Path(__file__)), \
                 patch.object(nitro, "system_command", side_effect=lambda name: name), \
                 patch.object(nitro.subprocess, "run", side_effect=[None, None, error, None]) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    nitro.load_driver()
                self.assertEqual(run.call_args_list[-1].args[0], ["modprobe", "acer_wmi"])

    def test_successful_installed_driver_uses_modprobe(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = Path(directory)
            fan = sysfs / "fan"
            def run(command, **kwargs):
                if command == ["modprobe", "linuwu_sense"]:
                    fan.touch()
            with patch.object(nitro, "SYS", sysfs), patch.object(nitro, "FAN", fan), \
                 patch.object(nitro, "driver_module", return_value=Path(__file__)), \
                 patch.object(nitro, "system_command", side_effect=lambda name: name), \
                 patch.object(nitro.subprocess, "run", side_effect=run) as mocked:
                nitro.load_driver()
                self.assertIn(["modprobe", "linuwu_sense"], [call.args[0] for call in mocked.call_args_list])


if __name__ == "__main__":
    unittest.main()
