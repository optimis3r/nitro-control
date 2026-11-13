#!/usr/bin/env python3
"""Small GTK desktop client; hardware discovery and writes live in nitro.py."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

import nitro

HERE = Path(__file__).resolve().parent
HELPER = Path("/usr/local/lib/nitro-control/nitro-helper")
APP_ID = "io.local.NitroControl"
PRESETS = ("Quiet", "Balanced", "Performance (max)")
FAN_SPEEDS = (20, 50, 100)


def preset_picker(name):
    widget = Gtk.DropDown.new_from_strings(PRESETS)
    widget.set_selected(1)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [name])
    return widget


def hardware_command(arguments):
    """Check the installed helper before asking it to change hardware."""
    pkexec = nitro.system_command("pkexec")
    if HELPER.is_file():
        try:
            version = subprocess.run([str(HELPER), "--protocol-version"], capture_output=True,
                                     text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Cannot check the installed helper. Run ./install.sh --without-driver, then reopen Nitro Control.") from error
        if version.returncode or version.stdout.strip() != nitro.HELPER_API_VERSION:
            raise RuntimeError("Installed Nitro Control helper is outdated. Run ./install.sh --without-driver from the project folder, then reopen Nitro Control.")
        command = [str(HELPER)]
    else:
        command = [nitro.system_command("python3", trusted=True), str(HERE / "nitro.py")]
    return subprocess.run([pkexec, *command, *arguments], capture_output=True, text=True)


def sample():
    temperatures = list(nitro.sensor_readings("temp"))
    profile, choices = nitro.platform_profile()
    gpu, gpu_message = nitro.gpu_telemetry()
    return {
        "model": nitro.read_optional(nitro.SYS / "class/dmi/id/product_name"),
        "cpu": nitro.cpu_temperature(temperatures),
        "fans": nitro.fans(), "graphics": nitro.graphics_devices(),
        "gpu": gpu, "gpu_message": gpu_message,
        "profile": nitro.read_optional(profile),
        "choices": nitro.read_optional(choices).split(),
        "settings": {key: nitro.read_optional(nitro.FAN.parent / key)
                     if nitro.acer_model() else "" for key in nitro.FIRMWARE_SETTINGS},
    }


def temperature(value):
    try:
        return f"{float(value) / 1000:.1f} °C"
    except (TypeError, ValueError):
        return "Temperature unavailable"


def group(title, *rows, description=None):
    widget = Adw.PreferencesGroup(title=title)
    if description:
        widget.set_description(description)
    for row in rows:
        widget.add(row)
    return widget


def row(title, *controls, subtitle=""):
    widget = Adw.ActionRow(title=title, subtitle=subtitle)
    for control in controls:
        control.set_valign(Gtk.Align.CENTER)
        widget.add_suffix(control)
    return widget


def spin(low, high, value, accessible_name):
    widget = Gtk.SpinButton.new_with_range(low, high, 1)
    widget.set_value(value)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [accessible_name])
    return widget


class Dashboard(Adw.ApplicationWindow):
    def __init__(self, app, popup=False):
        super().__init__(application=app, title="Nitro Quick Controls" if popup else "Nitro Control",
                         default_width=720, default_height=640)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.pending = self.busy = False
        self.closed = False
        self.fan_ids = None
        self.syncing_fans = False
        self.fan_widgets = {}
        self.graphics_ids = None
        self.graphics_rows = {}
        self.data = {}
        self.actions = []
        self.options = ()
        self.profile_targets = {}
        self.power_targets = ()
        self.firmware_values = {}
        self.popup = popup

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{edge}")(24)
        self.identity = Gtk.Label(label="Detecting hardware…", xalign=0, wrap=True)
        self.identity.add_css_class("title-2")
        content.append(self.identity)
        self.cpu = row("CPU")
        self.graphics = group("Graphics")
        self.fan_group = group("Fans", description="Each control changes only its selected fan. Auto lets firmware manage cooling.")
        self.no_fans = row("No fan interface detected", subtitle="Install the supported Acer driver or check your laptop's Linux driver support.")
        self.fan_group.add(self.no_fans)
        self.fan_group.add(self.cpu)
        content.append(self.fan_group)

        self.profiles = Gtk.DropDown.new_from_strings([])
        self.profiles.update_property([Gtk.AccessibleProperty.LABEL], ["Performance profile"])
        self.profile_apply = self.action("Apply", lambda: ["profile", self.profile_targets[self.profiles.get_selected_item().get_string()]])
        self.profile_row = row("Performance profile", self.profiles, self.profile_apply)
        self.profile_group = group("Performance", self.profile_row,
            description="Changing an Acer platform profile can return both fans to firmware control.")
        content.append(self.profile_group)

        self.power_preset = preset_picker("GPU power preset")
        self.power_apply = self.action("Apply", lambda: ["gpu-power", self.power_targets[self.power_preset.get_selected()]])
        self.power_row = row("Power preset", self.power_preset, self.power_apply)
        self.power_group = group("GPU", self.power_row)
        if not popup:
            content.append(self.power_group)

        self.firmware = {}
        self.firmware_group = group("Battery and Keyboard")
        for key, title, choices in (
            ("battery_limiter", "Battery charge limit", {"Off": 0, "80%": 1}),
            ("backlight_timeout", "Keyboard backlight timeout", {"Off": 0, "On": 1}),
            ("usb_charging", "USB charging while off", {"Off": 0, "Stop at 10%": 10, "Stop at 20%": 20, "Stop at 30%": 30}),
        ):
            dropdown = Gtk.DropDown.new_from_strings(list(choices))
            dropdown.update_property([Gtk.AccessibleProperty.LABEL], [title])
            apply = self.action("Apply", lambda key=key, values=tuple(choices.values()), dropdown=dropdown:
                                ["firmware", key, str(values[dropdown.get_selected()])])
            setting = row(title, dropdown, apply)
            self.firmware[key] = (setting, dropdown, apply, tuple(choices.values()))
            self.firmware_group.add(setting)
        if not popup:
            content.append(self.firmware_group)
        content.append(self.graphics)

        self.notice = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.notice.set_visible(False)
        content.append(self.notice)
        header = Adw.HeaderBar()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(header)
        root.append(Gtk.ScrolledWindow(child=Adw.Clamp(child=content, maximum_size=900),
                    vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_content(root)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.key_pressed)
        self.add_controller(keys)
        self.connect("close-request", self.close_requested)
        self.timer = GLib.timeout_add_seconds(2, self.refresh)
        self.refresh()

    def key_pressed(self, _, key, _code, _modifiers):
        if self.popup and key == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def close_requested(self, _):
        if self.busy:
            self.message("Wait for the current fan timer or hardware operation to finish.")
            return True
        self.closed = True
        GLib.source_remove(self.timer)
        self.pool.shutdown(wait=False, cancel_futures=True)
        return False

    def action(self, title, command):
        widget = Gtk.Button(label=title, sensitive=False)
        widget.connect("clicked", lambda _: self.run(command()))
        self.actions.append(widget)
        return widget

    def message(self, text, error=False):
        self.notice.set_label(text)
        self.notice.set_visible(bool(text))
        if error:
            self.notice.add_css_class("error")
        else:
            self.notice.remove_css_class("error")

    def refresh(self):
        if not self.pending:
            self.pending = True
            self.pool.submit(sample).add_done_callback(lambda future: GLib.idle_add(self.render, future))
        return GLib.SOURCE_CONTINUE

    def render(self, future):
        self.pending = False
        if self.closed:
            return
        try:
            self.data = future.result()
            self.render_data()
        except (OSError, RuntimeError, ValueError) as error:
            for action in self.actions:
                action.set_sensitive(False)
            self.message(f"Hardware refresh failed: {error}", error=True)

    def render_data(self):
        data = self.data
        self.identity.set_label(data["model"] or "Nitro Control")
        self.cpu.set_subtitle(temperature(data["cpu"]))
        devices = data["graphics"]
        ids = tuple(device["id"] for device in devices)
        if ids != self.graphics_ids:
            for child in self.graphics_rows.values():
                self.graphics.remove(child)
            self.graphics_rows = {device["id"]: row(device["name"]) for device in devices}
            for child in self.graphics_rows.values():
                self.graphics.add(child)
            self.graphics_ids = ids
        self.graphics.set_visible(bool(devices))
        for device in devices:
            self.graphics_rows[device["id"]].set_title(device["name"])
            self.graphics_rows[device["id"]].set_subtitle(device["stats"] or device["state"].capitalize())
        readings = data["gpu"]
        nvidia_rows = [self.graphics_rows[device["id"]] for device in devices if device["vendor"] == "NVIDIA"]
        for widget in nvidia_rows:
            widget.set_subtitle(data["gpu_message"])
        if readings and len(nvidia_rows) == 1:
            nvidia_rows[0].set_title(readings[0])
            metrics = ((1, "°C"), (2, "% load"), (3, "MHz"), (6, "W"))
            details = [f"{readings[index]} {unit}" for index, unit in metrics
                       if readings[index] not in {"[N/A]", "N/A", "[Not Supported]", ""}]
            if all(value not in {"[N/A]", "N/A", "[Not Supported]", ""} for value in readings[4:6]):
                details.append(f"{readings[4]} / {readings[5]} MiB VRAM")
            nvidia_rows[0].set_subtitle(" · ".join(details))

        self.render_fans(data["fans"])
        targets = {}
        for label, mode in zip(PRESETS, ("quiet", "balanced", "performance")):
            target = next((name for name in nitro.COOLING_PROFILES[mode] if name in data["choices"]), None)
            if target:
                targets[label] = target
        choices = tuple(targets)
        if not self.busy:
            self.profile_targets = targets
            if choices != self.options:
                self.profiles.set_model(Gtk.StringList.new(choices))
                current = next((label for label, target in targets.items() if target == data["profile"]), None)
                if current:
                    self.profiles.set_selected(choices.index(current))
                self.options = choices
        current = next((label for label, target in targets.items() if target == data["profile"]), data["profile"])
        self.profile_row.set_subtitle(f"Current: {current}" if choices else "Unavailable with the current driver")
        for widget in (self.profiles, self.profile_apply):
            widget.set_sensitive(bool(choices) and not self.busy)
        self.profile_group.set_visible(bool(choices))

        limits = None
        if readings:
            try:
                default, low, high = map(float, readings[9:12])
                if 0 < low <= default <= high < float("inf"):
                    limits = (low, high)
            except ValueError:
                pass
        if limits:
            self.power_targets = (f"{max(low, round(default * .75, 1)):g}", "default", f"{high:g}")
        else:
            self.power_targets = ()
        for widget in (self.power_preset, self.power_apply):
            widget.set_sensitive(limits is not None and not self.busy)
        self.power_row.set_subtitle(f"Draw: {readings[6]} W · Ceiling: {readings[8]} W" if readings else data["gpu_message"])
        self.power_group.set_visible(bool(nvidia_rows))
        for key, (setting, dropdown, apply, values) in self.firmware.items():
            current = data["settings"][key]
            supported = current in tuple(map(str, values))
            setting.set_visible(supported)
            if supported and not self.busy and self.firmware_values.get(key) != current:
                dropdown.set_selected(values.index(int(current)))
                self.firmware_values[key] = current
            dropdown.set_sensitive(supported and not self.busy)
            apply.set_sensitive(supported and not self.busy)
        self.firmware_group.set_visible(any(data["settings"].values()))

    def render_fans(self, fans):
        ids = tuple((fan["id"], fan["backend"]) for fan in fans)
        if ids != self.fan_ids and not self.busy:
            for widgets in self.fan_widgets.values():
                self.fan_group.remove(widgets[0])
                for action in widgets[3]:
                    self.actions.remove(action)
            self.fan_widgets = {}
            for fan in fans:
                channel = fan["id"]
                seconds = spin(1, 300, 30, f"{fan['label']} duration in seconds")
                toggles = {}
                actions = []
                controls = []
                if fan["backend"] in {"acer", "pwm"}:
                    choices = list(zip(PRESETS, FAN_SPEEDS))
                    if fan["backend"] == "acer":
                        choices.insert(0, ("Auto", 0))
                    selector = Gtk.Box(spacing=0)
                    selector.add_css_class("linked")
                    for title, value in choices:
                        toggle = Gtk.ToggleButton(label=title, sensitive=False)
                        toggle.update_property([Gtk.AccessibleProperty.LABEL], [f"{fan['label']}: {title}"])
                        if actions:
                            toggle.set_group(actions[0])
                        toggle.connect("toggled", self.fan_toggled, fan, value, seconds)
                        toggles[value] = toggle
                        actions.append(toggle)
                        self.actions.append(toggle)
                        selector.append(toggle)
                    controls.append(selector)
                    if fan["backend"] == "pwm":
                        controls.append(seconds)
                widget = row(fan["label"], *controls)
                if fan["backend"] == "pwm":
                    widget.set_tooltip_text("Select a preset to start the timed override. Duration is in seconds; Auto is restored afterwards.")
                self.fan_widgets[channel] = (widget, toggles, seconds, actions)
                self.fan_group.add(widget)
            self.fan_ids = ids
        self.no_fans.set_visible(not fans)
        for fan in fans:
            if fan["id"] not in self.fan_widgets:
                continue
            widget, toggles, seconds, actions = self.fan_widgets[fan["id"]]
            detail = f"{int(fan['rpm']):,} RPM" if fan["rpm"] else "RPM unavailable"
            if fan["backend"] == "acer":
                setting = fan["setting"]
                detail += " · Auto" if setting == 0 else " · " + (PRESETS[FAN_SPEEDS.index(setting)] if setting in FAN_SPEEDS else "Custom (CLI)")
                enabled = True
            elif fan["backend"] == "pwm":
                detail = f"{fan['driver']} · {fan['id']} · timed control (seconds)"
                enabled = fan["automatic"]
                if not enabled:
                    detail += " · Restore Auto in the existing controller first"
            else:
                detail += " · Monitor only; the driver does not expose speed control here"
                enabled = False
            widget.set_subtitle(detail)
            seconds.set_sensitive(enabled and not self.busy)
            self.syncing_fans = True
            try:
                for value, toggle in toggles.items():
                    toggle.set_sensitive(enabled and fan.get("manual", True) and not self.busy)
                    if not self.busy:
                        toggle.set_active(fan.get("setting") == value)
            finally:
                self.syncing_fans = False

    def fan_toggled(self, toggle, fan, value, seconds):
        if not toggle.get_active() or self.syncing_fans or self.busy:
            return
        if fan["backend"] == "acer":
            arguments = ["fan", "set", fan["id"], "auto" if value == 0 else str(value)]
        else:
            arguments = ["fan", "pwm-set", fan["id"], str(value),
                         "--seconds", str(seconds.get_value_as_int()), "--device", fan["device"]]
        self.run(arguments)

    def run(self, arguments):
        if self.busy:
            return
        self.busy = True
        for action in self.actions:
            action.set_sensitive(False)
        self.message("Applying… Timed fan commands finish when their timer ends.")
        self.pool.submit(hardware_command, arguments).add_done_callback(
            lambda future: GLib.idle_add(self.complete, future))

    def complete(self, future):
        self.busy = False
        try:
            result = future.result()
            output = (result.stdout if result.returncode == 0 else result.stderr or result.stdout).strip()
            self.message(output or "Hardware command finished.", error=result.returncode != 0)
        except (OSError, RuntimeError) as error:
            self.message(str(error), error=True)
        self.refresh()


def activate(app, popup):
    window = app.get_active_window()
    if window is not None and popup:
        window.close()
        return
    if window is None:
        window = Dashboard(app, popup)
    window.present()


if __name__ == "__main__":
    popup = sys.argv[1:] == ["--popup"]
    app = Adw.Application(application_id=APP_ID + (".Popup" if popup else ""))
    app.connect("activate", activate, popup)
    sys.exit(app.run(sys.argv[:1] if popup else sys.argv))
