#!/usr/bin/env python3
"""Laptop telemetry and hardware controls for Linux."""
import argparse
import csv
import fcntl
import math
import os
import re
import shutil
from pathlib import Path
import signal
import subprocess
import sys
import time

SYS = Path("/sys")
ACER = SYS / "devices/platform/acer-wmi"
FAN = ACER / "nitro_sense/fan_speed"
COOLING_PROFILES = {
    "auto": (),
    "max": (),
    "quiet": ("quiet", "low-power"),
    "balanced": ("balanced",),
    "performance": ("performance", "balanced-performance"),
}
FIRMWARE_SETTINGS = {
    "battery_limiter": (0, 1),
    "backlight_timeout": (0, 1),
    "usb_charging": (0, 10, 20, 30),
}
SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin:/run/current-system/sw/bin"
GPU_FIELDS = {
    "Model": "name",
    "Temperature (°C)": "temperature.gpu",
    "Load (%)": "utilization.gpu",
    "Graphics clock (MHz)": "clocks.gr",
    "VRAM used (MiB)": "memory.used",
    "VRAM total (MiB)": "memory.total",
    "Draw (W)": "power.draw",
    "Requested limit (W)": "gpu.power.requested_limit",
    "Effective ceiling (W)": "enforced.power.limit",
    "Default limit (W)": "power.default_limit",
    "Minimum limit (W)": "power.min_limit",
    "Maximum limit (W)": "power.max_limit",
}


def read(path):
    return path.read_text().strip()


def system_command(name, *, trusted=False):
    path = shutil.which(name, path=SYSTEM_PATH if trusted or os.geteuid() == 0 else None)
    if path is None:
        raise RuntimeError(f"{name} is unavailable.")
    return path


def read_optional(path):
    try:
        return read(path)
    except OSError:
        return ""


def hwmon_devices():
    return sorted((SYS / "class/hwmon").glob("hwmon*"))


def sensor_readings(kind):
    for device in hwmon_devices():
        driver = read_optional(device / "name") or device.name
        for channel in sorted(device.glob(f"{kind}[0-9]*_input")):
            value = read_optional(channel)
            if value.lstrip("-").isdigit():
                label = read_optional(channel.with_name(channel.stem.removesuffix("_input") + "_label"))
                yield device, driver, channel, label or channel.stem, value


def cpu_temperature(temperatures):
    for _, driver, _, _, value in temperatures:
        if driver in {"coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal"}:
            return value
    for zone in sorted((SYS / "class/thermal").glob("thermal_zone*")):
        kind = read_optional(zone / "type").lower()
        value = read_optional(zone / "temp")
        if kind.startswith(("cpu", "x86_pkg_temp", "soc_thermal")) and value.lstrip("-").isdigit():
            return value
    return ""


def nvidia(*args):
    result = subprocess.run([system_command("nvidia-smi"), *args], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "nvidia-smi failed")
    return result.stdout.strip()


def gpu(fields, index):
    data = nvidia("-i", str(index), "--query-gpu=" + ",".join(fields),
                  "--format=csv,noheader,nounits")
    rows = list(csv.reader(data.splitlines(), skipinitialspace=True))
    if len(rows) != 1 or len(rows[0]) != len(fields):
        raise RuntimeError("Unexpected NVIDIA response")
    return rows[0]


def platform_profile():
    devices = []
    for device in sorted((SYS / "class/platform-profile").glob("platform-profile-*")):
        setting, choices = device / "profile", device / "choices"
        if setting.is_file() and read_optional(choices):
            if read_optional(device / "name") == "acer-wmi" and acer_model():
                return setting, choices
            devices.append((setting, choices))
    legacy = (SYS / "firmware/acpi/platform_profile",
              SYS / "firmware/acpi/platform_profile_choices")
    if read_optional(legacy[1]):
        return legacy
    if len(devices) == 1:
        return devices[0]
    return legacy


def pwm_channels():
    for device in hwmon_devices():
        for control in sorted(device.glob("pwm[0-9]*_enable")):
            channel = control.stem.removesuffix("_enable")
            if re.fullmatch(r"pwm[0-9]+", channel) and (device / channel).is_file():
                if control.stat().st_mode & 0o200:
                    yield f"{device.name}/{channel}", device, control


def fan_rpm():
    for device, driver, _, label, value in sensor_readings("fan"):
        print(f"  {device.name} ({driver}) {label}  {value} RPM")


def acer_model():
    dmi = SYS / "class/dmi/id"
    return (read_optional(dmi / "sys_vendor"), read_optional(dmi / "product_name")) == (
        "Acer", "Nitro ANV15-41")


def acer_fans_available():
    return acer_model() and FAN.is_file()


def status(index):
    product = read_optional(SYS / "class/dmi/id/product_name") or "Unknown system"
    bios = read_optional(SYS / "class/dmi/id/bios_version") or "unknown"
    print(f"{product} | BIOS {bios}")
    temperatures = list(sensor_readings("temp"))
    for device, driver, _, label, value in temperatures:
        print(f"  {device.name} ({driver}) {label}  {int(value) / 1000:.1f} °C")
    if not temperatures and (cpu := cpu_temperature(temperatures)):
        print(f"CPU temperature  {int(cpu) / 1000:.1f} °C")
    if acer_fans_available():
        print(f"Fan setting (CPU,GPU)  {read_optional(FAN) or 'unavailable'}")
    fan_rpm()
    channels = [f"{name} ({read_optional(device / 'name')})"
                for name, device, _ in pwm_channels()]
    if channels:
        print(f"PWM channels  {', '.join(channels)}")
    elif not acer_fans_available():
        print("Direct fan control  Unavailable")
    profile, choices = platform_profile()
    if profile.exists() and choices.exists():
        print(f"Platform profile  {read(profile)} ({read(choices)})")
    else:
        print("Platform profile  Unavailable")
    try:
        values = gpu(tuple(GPU_FIELDS.values()), index)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"NVIDIA telemetry unavailable: {error}")
        return
    for label, value in zip(GPU_FIELDS, values):
        print(f"{label}  {value}")


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError("Run this hardware change with sudo.")


def require_acer():
    if not acer_model():
        raise RuntimeError("Acer firmware writes are restricted to the Nitro ANV15-41.")


def driver_module():
    return Path(__file__).resolve().parent / ".build" / os.uname().release / "linuwu_sense.ko"


def load_driver():
    if FAN.exists():
        print("Acer fan driver is already active.")
        return
    module = driver_module()
    if not module.is_file():
        raise RuntimeError("Build the driver for the running kernel with ./build-driver.sh first.")
    if (SYS / "module/linuwu_sense").exists():
        raise RuntimeError("Linuwu-Sense is loaded but did not expose fan control.")
    modprobe, insmod, rmmod = map(system_command, ("modprobe", "insmod", "rmmod"))
    stock_loaded = (SYS / "module/acer_wmi").exists()
    if stock_loaded:
        subprocess.run([modprobe, "-r", "acer_wmi"], check=True)
    try:
        subprocess.run([modprobe, "-a", "sparse_keymap", "wmi", "rfkill", "video"], check=True)
        subprocess.run([insmod, str(module)], check=True)
        if not FAN.exists():
            subprocess.run([rmmod, "linuwu_sense"], check=True)
            raise RuntimeError("The replacement driver did not expose fan control.")
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        if stock_loaded:
            subprocess.run([modprobe, "acer_wmi"], check=True)
        raise
    print("Acer fan driver connected for this boot.")


def set_fans(cpu, graphics):
    value = f"{cpu},{graphics}"
    FAN.write_text(value + "\n")
    if read(FAN) != value:
        raise RuntimeError(f"Fan driver did not confirm {value}")


def write_confirmed(setting, value):
    setting.write_text(value + "\n")
    if read(setting) != value:
        raise RuntimeError(f"The driver did not confirm {value} for {setting.name}.")


def cooling_mode(mode):
    if mode in {"max", "auto"} and not acer_fans_available():
        raise RuntimeError("This mode requires the supported Acer fan interface.")
    if mode == "max":
        set_fans(100, 100)
        print("Max active. Both fans requested at full speed.")
        return
    if mode == "auto":
        set_fans(0, 0)
        print("Firmware Auto active. The current platform profile is retained.")
        return
    setting, choices = platform_profile()
    if not choices.exists() or not setting.exists():
        raise RuntimeError("Platform profile unavailable.")
    available = read(choices).split()
    name = next((item for item in COOLING_PROFILES[mode] if item in available), None)
    if name is None:
        raise RuntimeError(f"The driver does not expose a profile for {mode}.")
    if acer_fans_available():
        set_fans(0, 0)
    write_confirmed(setting, name)
    print(f"{mode.capitalize()} active · platform profile {name}.")


def interrupt(signum, frame):
    raise KeyboardInterrupt


def pwm_max(channel, seconds):
    control = next((path for name, _, path in pwm_channels() if name == channel), None)
    if control is None:
        raise RuntimeError("Unknown PWM channel. Run 'status' to list current channels.")
    control = control.resolve(strict=True)
    original = read(control)
    if not original.isdigit() or int(original) < 2:
        raise RuntimeError("The channel must be in an automatic mode before the test.")
    sigint = signal.getsignal(signal.SIGINT)
    sigterm = signal.signal(signal.SIGTERM, interrupt)
    try:
        write_confirmed(control, "0")
        print(f"Requested full speed on {channel} for {seconds}s.", flush=True)
        time.sleep(seconds)
    finally:
        # Finish restoring the controller even if another termination signal arrives.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            write_confirmed(control, original)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Could not restore {control} to {original}: {error}") from error
        finally:
            signal.signal(signal.SIGTERM, sigterm)
            signal.signal(signal.SIGINT, sigint)
        print(f"Restored {channel} to mode {original}.")


def fan_control(args):
    if not FAN.exists():
        raise RuntimeError("Fan interface unavailable. See README.md for the driver trial.")
    if args.mode == "auto":
        set_fans(0, 0)
        print("Firmware Auto requested.")
        return
    cpu, graphics = (100, 100) if args.mode == "max" else (args.cpu, args.graphics)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        set_fans(cpu, graphics)
        print(f"CPU {cpu}%, GPU {graphics}% for {args.seconds}s. Ctrl+C returns to Auto.", flush=True)
        fan_rpm()
        time.sleep(args.seconds)
    finally:
        try:
            set_fans(0, 0)
            print("Firmware Auto requested.")
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Could not restore Auto: {error}. Run 'fan auto' immediately.") from error


def set_power(args):
    index = args.gpu_index
    default, minimum, maximum = gpu(
        ("power.default_limit", "power.min_limit", "power.max_limit"), index)
    try:
        baseline, lower, upper = map(float, (default, minimum, maximum))
    except ValueError as error:
        raise RuntimeError("NVIDIA did not report a complete power-limit range.") from error
    watts = baseline if args.watts == "default" else float(args.watts)
    if not (all(map(math.isfinite, (lower, upper, baseline, watts))) and lower <= watts <= upper):
        raise RuntimeError(f"Power limit must be between {lower:g} and {upper:g} W.")
    print(nvidia("-i", str(index), "--power-limit", f"{watts:g}"))
    requested, effective = gpu(("gpu.power.requested_limit", "enforced.power.limit"), index)
    print(f"Requested {requested} W | effective ceiling {effective} W")
    if not any(matches_watts(value, watts) for value in (requested, effective)):
        raise RuntimeError("The driver accepted the command, but telemetry did not confirm the target.")


def matches_watts(value, target):
    try:
        return math.isclose(float(value), target, abs_tol=0.01)
    except ValueError:
        return False


def bounded(low, high, allow_auto=False):
    def parse(value):
        number = int(value)
        if low <= number <= high or (allow_auto and number == 0):
            return number
        suffix = " or 0 for Auto" if allow_auto else ""
        raise argparse.ArgumentTypeError(f"expected {low}–{high}{suffix}")
    return parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu-index", default=0, type=int)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show temperatures and available controls")
    commands.add_parser("load-driver", help="load the built Acer driver for this boot")
    mode = commands.add_parser("mode", help="select a platform profile or Acer fan mode")
    mode.add_argument("name", choices=COOLING_PROFILES)
    fan = commands.add_parser("fan", help="control Acer fans or test a Linux PWM channel")
    modes = fan.add_subparsers(dest="mode", required=True)
    modes.add_parser("auto", help="restore firmware fan control")
    for mode in ("max", "manual"):
        options = modes.add_parser(mode)
        options.add_argument("--seconds", default=30, type=bounded(1, 300))
        if mode == "manual":
            options.add_argument("cpu", type=bounded(60, 100, allow_auto=True))
            options.add_argument("graphics", type=bounded(60, 100, allow_auto=True))
    pwm = modes.add_parser("pwm-max", help="test one Linux hwmon PWM channel at full speed")
    pwm.add_argument("channel", help="channel from status, such as hwmon3/pwm1")
    pwm.add_argument("--seconds", default=15, type=bounded(1, 300))
    profile = commands.add_parser("profile", help="set an available platform profile")
    profile.add_argument("name")
    power = commands.add_parser("gpu-power", help="set an NVIDIA power limit in watts")
    power.add_argument("watts", help="watts or 'default'")
    firmware = commands.add_parser("firmware", help="set an Acer firmware option")
    firmware.add_argument("option", choices=FIRMWARE_SETTINGS)
    firmware.add_argument("value", type=int)
    args = parser.parse_args()

    if args.command == "status":
        status(args.gpu_index)
        return
    require_root()
    if args.command == "gpu-power":
        set_power(args)
        return
    if args.command in {"firmware", "load-driver"} or (
        args.command == "fan" and args.mode != "pwm-max"
    ):
        require_acer()
    with open("/run/nitro-control.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("A fan or profile command is active. Stop it before retrying.") from error
        if args.command == "fan":
            if args.mode == "pwm-max":
                pwm_max(args.channel, args.seconds)
            else:
                fan_control(args)
        elif args.command == "mode":
            cooling_mode(args.name)
        elif args.command == "load-driver":
            load_driver()
        elif args.command == "firmware":
            if args.value not in FIRMWARE_SETTINGS[args.option]:
                raise RuntimeError("Unsupported firmware option value.")
            setting = FAN.parent / args.option
            if not setting.exists():
                raise RuntimeError("This firmware option is unavailable.")
            setting.write_text(f"{args.value}\n")
            if read(setting) != str(args.value):
                raise RuntimeError("Firmware did not confirm the setting.")
            print("Firmware setting applied.")
        else:
            setting, choices = platform_profile()
            if not setting.exists() or not choices.exists():
                raise RuntimeError("Platform profile unavailable.")
            available = read(choices).split()
            if args.name not in available:
                raise RuntimeError(f"Available profiles: {', '.join(available)}")
            write_confirmed(setting, args.name)
            print(f"Profile set to {args.name}.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"nitro: {error}", file=sys.stderr)
        sys.exit(1)
