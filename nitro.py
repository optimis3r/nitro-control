#!/usr/bin/env python3
"""Laptop telemetry and hardware controls for Linux."""
import argparse
import csv
import fcntl
from functools import lru_cache
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
HELPER_API_VERSION = "2"
ACER = SYS / "devices/platform/acer-wmi"
FAN = ACER / "nitro_sense/fan_speed"
COOLING_PROFILES = {
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
    "Requested limit (W)": "power.limit",
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


@lru_cache(maxsize=32)
def graphics_name(vendor, model, brand):
    """Resolve a readable model using the local PCI database, without probing a GPU."""
    for path in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids"):
        database = read_optional(Path(path))
        if not database:
            continue
        section = False
        for line in database.splitlines():
            if line and not line.startswith(("#", "\t")):
                section = line.startswith(vendor.removeprefix("0x") + " ")
            elif section and line.startswith("\t" + model.removeprefix("0x") + " "):
                name = line.strip().split(None, 1)[1]
                friendly = re.findall(r"\[([^]]+)\]", name)
                return f"{brand} {friendly[-1] if friendly else name}"
    return f"{brand} graphics"


def graphics_stats(device):
    hwmon = next(iter((device / "hwmon").glob("hwmon*")), device / "hwmon")
    stats = []
    for path, scale, unit in (
        (hwmon / "temp1_input", 1000, "°C"),
        (device / "gpu_busy_percent", 1, "% load"),
        (hwmon / "freq1_input", 1000000, "MHz"),
        (device / "mem_info_vram_used", 1048576, "MiB VRAM used"),
        (hwmon / "power1_input", 1000000, "W package" if read_optional(hwmon / "power1_label") == "PPT" else "W"),
    ):
        try:
            value = float(read(path)) / scale
            if math.isfinite(value):
                stats.append(f"{value:.4g} {unit}")
        except (OSError, ValueError):
            pass
    return " · ".join(stats)


def graphics_devices():
    """Inspect PCI without waking GPUs or requiring a vendor utility."""
    devices = []
    for device in sorted((SYS / "bus/pci/devices").glob("*")):
        if not read_optional(device / "class").startswith("0x03"):
            continue
        vendor = read_optional(device / "vendor")
        name = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}.get(vendor, "GPU")
        driver = device / "driver"
        bound = driver.is_symlink()
        runtime = read_optional(device / "power/runtime_status")
        power = read_optional(device / "power_state")
        state = ("powered down" if runtime in {"suspended", "suspending"} or power in {"D3cold", "D3hot"}
                 else "driver unavailable" if not bound else "available")
        devices.append({"id": device.name, "vendor": name, "state": state,
                        "name": graphics_name(vendor, read_optional(device / "device"), name),
                        "stats": graphics_stats(device) if state == "available" else ""})
    return devices


def gpu_telemetry(index=0):
    devices = [device for device in graphics_devices() if device["vendor"] == "NVIDIA"]
    if not devices:
        return [], "NVIDIA GPU not detected (it may be disabled in firmware)."
    # nvidia-smi indices cannot safely be mapped to PCI order on multi-GPU systems.
    # Skip polling while any NVIDIA device is asleep rather than wake it accidentally.
    if any(device["state"] != "available" for device in devices):
        return [], "NVIDIA GPU " + ", ".join(sorted({device["state"] for device in devices})) + "."
    try:
        return gpu(tuple(GPU_FIELDS.values()), index), "NVIDIA telemetry available."
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        return [], f"NVIDIA telemetry unavailable: {error}"


def acer_fan_values():
    value = read_optional(FAN)
    if not re.fullmatch(r"\d{1,3},\d{1,3}", value):
        raise RuntimeError("Cannot read Acer fan settings. Refresh or reload the driver.")
    values = list(map(int, value.split(",")))
    if any(value > 100 for value in values):
        raise RuntimeError("Invalid Acer fan settings reported by the driver.")
    return values


def fans():
    """One inventory for CLI and desktop; never infer ownership from discovery order."""
    readings = list(sensor_readings("fan"))
    result = []
    if acer_fans_available():
        values = acer_fan_values()
        for index, name in enumerate(("cpu", "gpu"), start=1):
            rpm = next((row[-1] for row in readings
                        if row[0].resolve().is_relative_to(ACER.resolve())
                        and row[2].name == f"fan{index}_input"), "")
            result.append({"id": name, "label": f"{name.upper()} fan", "backend": "acer",
                           "rpm": rpm, "setting": values[index - 1]})
        readings = [row for row in readings if not row[0].resolve().is_relative_to(ACER.resolve())]
    for target in pwm_controls():
        result.append({**target, "backend": "pwm", "rpm": ""})
    for device, driver, channel, label, rpm in readings:
        result.append({"id": f"{device.name}/{channel.stem}", "label": f"{driver} · {label}",
                       "backend": "sensor", "rpm": rpm})
    return result


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
                try:
                    if control.stat().st_mode & 0o200:
                        yield f"{device.name}/{channel}", device, control
                except FileNotFoundError:
                    continue  # Devices can disappear during discovery.


def pwm_controls():
    """Describe standard channels without assuming fan order or driver names."""
    channels = []
    for name, device, control in pwm_channels():
        channel = control.name.removesuffix("_enable")
        mode = read_optional(control)
        duty = device / channel
        try:
            writable = bool(duty.stat().st_mode & 0o200)
            identity = str(device.resolve(strict=True))
        except FileNotFoundError:
            continue
        channels.append({"id": name, "device": identity,
                         "driver": read_optional(device / "name") or device.name,
                         "label": read_optional(device / f"{channel}_label") or channel,
                         "mode": mode, "manual": writable,
                         "automatic": mode.isdigit() and int(mode) >= 2})
    return channels


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
    detected = fans()
    for fan in detected:
        state = ("Auto" if fan.get("setting") == 0 else f"{fan['setting']}%") if fan["backend"] == "acer" else (
            "timed PWM control" if fan["backend"] == "pwm" else "monitor only")
        rpm = f" · {fan['rpm']} RPM" if fan["rpm"] else ""
        print(f"Fan {fan['id']}  {fan['label']} · {state}{rpm}")
    if not any(fan["backend"] != "sensor" for fan in detected):
        print("Direct fan control  Unavailable")
    profile, choices = platform_profile()
    if profile.exists() and choices.exists():
        print(f"Platform profile  {read(profile)} ({read(choices)})")
    else:
        print("Platform profile  Unavailable")
    for device in graphics_devices():
        print(f"{device['name']}  {device['stats'] or device['state']}")
    values, message = gpu_telemetry(index)
    print(message)
    for label, value in zip(GPU_FIELDS, values):
        print(f"{label}  {value}")


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError("Run this hardware change with sudo.")


def require_acer():
    if not acer_model():
        raise RuntimeError("Acer firmware writes are restricted to the Nitro ANV15-41.")


def load_driver():
    if FAN.exists():
        print("Acer fan driver is already active.")
        return
    if (SYS / "module/linuwu_sense").exists():
        raise RuntimeError("Linuwu-Sense is loaded but did not expose fan control.")
    modprobe, rmmod = map(system_command, ("modprobe", "rmmod"))
    stock_loaded = (SYS / "module/acer_wmi").exists()
    if stock_loaded:
        subprocess.run([modprobe, "-r", "acer_wmi"], check=True)
    try:
        subprocess.run([modprobe, "-a", "sparse_keymap", "wmi", "rfkill", "video"], check=True)
        subprocess.run([modprobe, "linuwu_sense"], check=True)
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


def set_fan(channel, value, seconds=None):
    """Acer has a paired register: preserve the other channel on every write."""
    if not acer_fans_available():
        raise RuntimeError("Acer fan control unavailable. Run ./install.sh and check 'nitro status'.")
    if channel not in {"cpu", "gpu"} or value not in (0, *range(20, 101)):
        raise RuntimeError("Choose cpu/gpu and Auto (0) or 20–100 percent.")
    if seconds is not None and not 1 <= seconds <= 300:
        raise RuntimeError("Use 1–300 seconds.")
    index = 0 if channel == "cpu" else 1
    values = acer_fan_values()
    values[index] = value
    if seconds is None:
        set_fans(*values)
        print(f"{channel.upper()} fan: {'Auto' if value == 0 else str(value) + '%'}.")
        return
    sigint = signal.getsignal(signal.SIGINT)
    sigterm = signal.signal(signal.SIGTERM, interrupt)
    try:
        set_fans(*values)
        print(f"{channel.upper()} fan: {value}% for {seconds}s.", flush=True)
        time.sleep(seconds)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            values = acer_fan_values()
            values[index] = 0
            set_fans(*values)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Could not restore {channel} Auto: {error}. Run 'fan auto'.") from error
        finally:
            signal.signal(signal.SIGINT, sigint)
            signal.signal(signal.SIGTERM, sigterm)
        print(f"{channel.upper()} fan restored to Auto.")


def write_confirmed(setting, value):
    setting.write_text(value + "\n")
    if read(setting) != value:
        raise RuntimeError(f"The driver did not confirm {value} for {setting.name}.")


def interrupt(signum, frame):
    raise KeyboardInterrupt


def pwm_override(channel, seconds, percent=None, expected_device=None):
    if not 1 <= seconds <= 300 or (percent is not None and not 20 <= percent <= 100):
        raise RuntimeError("Use 1–300 seconds and 20–100 percent.")
    selected = next(((device, path) for name, device, path in pwm_channels() if name == channel), None)
    if selected is None:
        raise RuntimeError("Unknown PWM channel. Run 'status' to list current channels.")
    device, control = selected
    if expected_device is not None and str(device.resolve(strict=True)) != expected_device:
        raise RuntimeError("The selected fan device changed. Refresh and select the channel again.")
    duty = (device / control.name.removesuffix("_enable")).resolve(strict=True)
    control = control.resolve(strict=True)
    original = read(control)
    if not original.isdigit() or int(original) < 2:
        raise RuntimeError("The channel must be in an automatic mode before the test.")
    sigint = signal.getsignal(signal.SIGINT)
    sigterm = signal.signal(signal.SIGTERM, interrupt)
    try:
        if percent is None:
            try:
                write_confirmed(control, "0")
            except (OSError, RuntimeError):
                # Some drivers expose manual/auto but do not implement mode 0.
                write_confirmed(control, "1")
                write_confirmed(duty, "255")
        else:
            write_confirmed(control, "1")
            write_confirmed(duty, str(round(percent * 255 / 100)))
        target = "full speed" if percent is None else f"{percent}%"
        print(f"Requested {target} on {channel} for {seconds}s.", flush=True)
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
    requested, effective = gpu(("power.limit", "enforced.power.limit"), index)
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
    commands.add_parser("load-driver", help="load the installed Acer driver")
    fan = commands.add_parser("fan", help="control Acer fans or a standard Linux PWM channel")
    modes = fan.add_subparsers(dest="mode", required=True)
    single = modes.add_parser("set", help="change one Acer fan without changing the other")
    single.add_argument("channel", choices=("cpu", "gpu"))
    single.add_argument("percent", type=lambda value: 0 if value.lower() == "auto" else bounded(20, 100, True)(value))
    single.add_argument("--seconds", type=bounded(1, 300), help="return this fan to Auto after 1–300 seconds")
    modes.add_parser("auto", help="restore firmware fan control")
    pwm = modes.add_parser("pwm-max", help="test one Linux hwmon PWM channel at full speed")
    pwm.add_argument("channel", help="channel from status, such as hwmon3/pwm1")
    pwm.add_argument("--seconds", default=15, type=bounded(1, 300))
    pwm.add_argument("--device", help="expected resolved hwmon device path")
    pwm_set = modes.add_parser("pwm-set", help="temporarily set a standard PWM fan speed")
    pwm_set.add_argument("channel", help="channel from status, such as hwmon3/pwm1")
    pwm_set.add_argument("percent", type=bounded(20, 100))
    pwm_set.add_argument("--seconds", default=30, type=bounded(1, 300))
    pwm_set.add_argument("--device", help="expected resolved hwmon device path")
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
        args.command == "fan" and args.mode not in {"pwm-max", "pwm-set"}
    ):
        require_acer()
    with open("/run/nitro-control.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("A fan or profile command is active. Stop it before retrying.") from error
        if args.command == "fan":
            if args.mode == "set":
                set_fan(args.channel, args.percent, args.seconds)
            elif args.mode == "pwm-max":
                pwm_override(args.channel, args.seconds, expected_device=args.device)
            elif args.mode == "pwm-set":
                pwm_override(args.channel, args.seconds, args.percent, args.device)
            else:
                set_fans(0, 0)
                print("Firmware Auto requested.")
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
