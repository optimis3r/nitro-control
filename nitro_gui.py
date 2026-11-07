#!/usr/bin/env python3
"""Nitro Control desktop app."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import subprocess
import sys

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

import nitro

HERE = Path(__file__).resolve().parent
PRIVILEGED_HELPER = Path("/usr/local/lib/nitro-control/nitro-helper")
APP_ID = "io.local.NitroControl"
POPUP_ID = APP_ID + ".Popup"
CSS = b"""
window { background: #191817; color: #f1eeea; font-family: sans-serif; }
.card { background: #22211f; border: 1px solid #383532; border-radius: 12px; padding: 16px; }
.caption { color: #c1bbb3; font-size: 12px; }
.hero { font-size: 28px; font-weight: 600; }
.title { font-size: 16px; font-weight: 600; }
.metric { font-family: monospace; font-size: 22px; font-weight: 500; }
.muted { color: #aaa39b; font-size: 12px; }
.badge { color: #d4cec5; font-size: 12px; }
.nav button { background: transparent; border: 0; border-bottom: 2px solid transparent;
              box-shadow: none; border-radius: 0; padding: 4px 16px; color: #c1bbb3; }
.nav button:checked { border-bottom-color: #dca877; color: #f1eeea; }
.processor-stats { border-top: 1px solid #383532; padding-top: 8px; }
button, dropdown > button, spinbutton { border-radius: 6px; }
button.suggested-action { background: #ede7de; color: #24211d; font-weight: 600; }
button.suggested-action:hover { background: #ffffff; }
button.suggested-action:disabled { background: #302d2a; color: #aaa39b; }
button:focus-visible { outline: 2px solid #dca877; outline-offset: -2px; }
flowbox, flowboxchild { padding: 0; margin: 0; }
button.mode { background: #2c2a27; color: #f1eeea; border: 1px solid #403c38;
              box-shadow: none; padding: 4px 8px;
              transition: background-color 120ms ease-out, color 120ms ease-out; }
button.mode:hover { background: #393531; }
button.mode.active-mode { background: #362d25; color: #f2bb8a; border-color: #98704f; }
button.mode:disabled { background: #242321; color: #aaa39b; }
.instrument { background: #1b1a19; border-radius: 8px; padding: 12px; }
.gpu-readouts .metric { font-size: 16px; }
button.mode.cooling-option { padding: 6px 12px; }
button.cooling-option .mode-name { font-weight: 600; }
.chart { background: #1b1a19; border-radius: 8px; padding: 12px; }
.settings-row { padding: 12px 0 0; border-top: 1px solid #403c38; }
.error { color: #ffae9e; }
"""


def box(*children, vertical=True, spacing=12):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                     spacing=spacing)
    for child in children:
        widget.append(child)
    return widget


def wrap_items(*children, columns, spacing=8):
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                       min_children_per_line=1, max_children_per_line=columns,
                       column_spacing=spacing, row_spacing=spacing)
    for child in children:
        flow.append(child)
        child.get_parent().set_focusable(False)
    return flow


def label(text="", style=None):
    widget = Gtk.Label(label=text, xalign=0, wrap=True)
    if style:
        widget.add_css_class(style)
    return widget


def card(*children):
    widget = box(*children)
    widget.add_css_class("card")
    return widget


def button(text, callback, primary=False):
    widget = Gtk.Button(label=text)
    widget.connect("clicked", lambda _: callback())
    if primary:
        widget.add_css_class("suggested-action")
    return widget


read_optional = nitro.read_optional


def cpu_times():
    fields = read_optional(Path("/proc/stat")).split(None, 9)
    if len(fields) < 9 or fields[0] != "cpu" or not all(value.isdigit() for value in fields[1:9]):
        return None
    counts = tuple(map(int, fields[1:9]))
    return sum(counts), counts[3] + counts[4]


def sample():
    snapshot = {"rpm": [], "settings": {}, "gpu": [],
                "model": read_optional(nitro.SYS / "class/dmi/id/product_name")}
    fans = list(nitro.sensor_readings("fan"))
    if nitro.acer_fans_available():
        fans = [row for row in fans if row[0].resolve().is_relative_to(nitro.ACER)]
    snapshot["rpm"] = [row[-1] for row in fans[:2]]
    temperatures = list(nitro.sensor_readings("temp"))
    snapshot["cpu"] = nitro.cpu_temperature(temperatures)
    snapshot["cpu_times"] = cpu_times()
    gpu_sensor = next((row for row in temperatures
                       if row[1] in {"amdgpu", "nouveau", "i915", "xe"}
                       and row[2].name == "temp1_input"), None)
    snapshot["gpu_temp"] = gpu_sensor[-1] if gpu_sensor else ""
    snapshot["gpu_load"] = (read_optional(gpu_sensor[0] / "device/gpu_busy_percent")
                            if gpu_sensor else "")
    batteries = sorted((nitro.SYS / "class/power_supply").glob("*"))
    battery = next((path for path in batteries if read_optional(path / "type") == "Battery"
                    and read_optional(path / "scope") != "Device"), None)
    charge = read_optional(battery / "capacity") if battery else ""
    status = read_optional(battery / "status") if battery else ""
    snapshot["battery"] = f"{charge}% · {status}" if charge else ""
    snapshot["acer"] = nitro.acer_model()
    snapshot["pwm"] = nitro.pwm_controls()
    snapshot["fan"] = read_optional(nitro.FAN) if snapshot["acer"] else ""
    profile, choices = nitro.platform_profile()
    snapshot["profile"] = read_optional(profile)
    snapshot["choices"] = read_optional(choices).split()
    for option in nitro.FIRMWARE_SETTINGS:
        snapshot["settings"][option] = read_optional(nitro.FAN.parent / option) if snapshot["acer"] else ""
    try:
        snapshot["gpu"] = nitro.gpu(tuple(nitro.GPU_FIELDS.values()), 0)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass
    return snapshot


class Rotor(Gtk.DrawingArea):
    def __init__(self, size):
        super().__init__(content_width=size, content_height=size)
        self.color = (.74, .70, .65)
        self.rpm = 0
        self.angle = 0
        self.flow = 0
        self.previous = 0
        self.set_draw_func(self.draw)
        self.add_tick_callback(self.tick)

    def tick(self, _, clock):
        now = clock.get_frame_time()
        elapsed = min((now - self.previous) / 1_000_000, 0.05)
        self.previous = now
        if self.rpm and self.get_settings().get_property("gtk-enable-animations"):
            self.angle = (self.angle + min(8, self.rpm / 550) * elapsed) % math.tau
            self.flow = (self.flow + min(1.9, self.rpm / 3500) * elapsed) % 1
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def draw(self, _, ctx, width, height):
        ctx.translate(width / 2, height / 2)
        ctx.scale(min(width, height) / 180, min(width, height) / 180)
        ctx.set_line_width(.75)
        ctx.set_source_rgb(.09, .084, .078)
        ctx.arc(0, 0, 67, 0, math.tau)
        ctx.fill()
        for radius, opacity in ((76, .12), (67, .38), (61, .13)):
            ctx.set_source_rgba(*self.color, opacity)
            ctx.arc(0, 0, radius, 0, math.tau)
            ctx.stroke()
        for tick in range(48):
            angle = tick * math.tau / 48
            inner = 72 if tick % 4 == 0 else 74
            ctx.set_source_rgba(*self.color, .31 if tick % 4 == 0 else .14)
            ctx.move_to(inner * math.cos(angle), inner * math.sin(angle))
            ctx.line_to(77 * math.cos(angle), 77 * math.sin(angle))
            ctx.stroke()
        ctx.save()
        ctx.rotate(self.angle)
        blade = cairo.LinearGradient(13, 10, 60, -15)
        blade.add_color_stop_rgba(0, *self.color, .27)
        blade.add_color_stop_rgba(1, *self.color, .69)
        for _ in range(9):
            ctx.move_to(12, 4)
            ctx.curve_to(23, -17, 42, -31, 53, -22)
            ctx.curve_to(60, -17, 62, -10, 59, -3)
            ctx.curve_to(38, -5, 23, 9, 13, 13)
            ctx.close_path()
            ctx.set_source(blade)
            ctx.fill()
            ctx.rotate(math.tau / 9)
        ctx.set_source_rgb(.12, .11, .10)
        ctx.arc(0, 0, 18, 0, math.tau)
        ctx.fill()
        ctx.set_source_rgba(*self.color, .85)
        ctx.set_line_width(2)
        ctx.arc(0, 0, 7, 0, math.tau)
        ctx.stroke()
        ctx.restore()
        if self.rpm:
            self.draw_airflow(ctx)

    def draw_airflow(self, ctx):
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_width(.85)
        for lane in range(9):
            phase = (self.flow + lane * .37) % 1
            angle = lane * math.tau / 9
            ctx.set_source_rgba(*self.color, .43 * math.sin(math.pi * phase))
            for segment in range(8):
                depth = max(0, phase - .22 + .22 * segment / 7)
                radius = 87 - 46 * depth
                twist = angle + .85 * depth * depth
                x = radius * math.cos(twist)
                y = radius * math.sin(twist) - 12 * (1 - depth)
                if segment == 0:
                    ctx.move_to(x, y)
                else:
                    ctx.line_to(x, y)
            ctx.stroke()


class GpuModel(Gtk.DrawingArea):
    # Pixel coordinates in gpu-housing.png and gpu-rotor.png, respectively.
    SOCKETS = ((410, 455), (407, 1143))
    ROTOR_HUB = (625, 630)
    ROTOR_SCALE = .46

    def __init__(self):
        super().__init__(content_width=192, content_height=400, vexpand=True)
        self.housing = GdkPixbuf.Pixbuf.new_from_file(str(HERE / "assets/gpu-housing.png"))
        self.rotor = GdkPixbuf.Pixbuf.new_from_file(str(HERE / "assets/gpu-rotor.png"))
        self.rpm = [0, 0]
        self.angles = [0.0, 0.0]
        self.previous_frame = None
        self.set_draw_func(self.draw)
        self.add_tick_callback(self.tick)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["GPU model with live fan animation"])

    def tick(self, _, clock):
        now = clock.get_frame_time()
        previous = self.previous_frame
        self.previous_frame = now
        if previous is not None and any(self.rpm) and self.get_settings().get_property("gtk-enable-animations"):
            elapsed = min((now - previous) / 1_000_000, .05)
            for index, rpm in enumerate(self.rpm):
                # Slow the visual rotation so individual blades remain legible.
                self.angles[index] = (self.angles[index] + rpm / 3000 * math.tau * elapsed) % math.tau
            self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def draw(self, _, ctx, width, height):
        image_width, image_height = self.housing.get_width(), self.housing.get_height()
        scale = min(width / image_width, height / image_height)
        ctx.translate((width - image_width * scale) / 2, (height - image_height * scale) / 2)
        ctx.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(ctx, self.housing, 0, 0)
        ctx.paint()
        for (x, y), angle in zip(self.SOCKETS, self.angles):
            ctx.save()
            ctx.translate(x, y)
            ctx.rotate(angle)
            ctx.scale(self.ROTOR_SCALE, self.ROTOR_SCALE)
            hub_x, hub_y = self.ROTOR_HUB
            Gdk.cairo_set_source_pixbuf(ctx, self.rotor, -hub_x, -hub_y)
            ctx.paint()
            ctx.restore()

class Dashboard(Gtk.ApplicationWindow):
    def __init__(self, app, popup=False):
        super().__init__(application=app, title="Nitro Quick Controls" if popup else "Nitro Control",
                         default_width=960 if popup else 1200, default_height=420 if popup else 780,
                         decorated=not popup)
        self.popup = popup
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.pending = False
        self.busy = False
        self.controls = []
        self.models = {}
        self.data = {}
        self.mode_buttons = {}
        self.auto_profile = None
        self.history = [deque(maxlen=60), deque(maxlen=60)]
        self.rotors = []
        self.rpm_labels = []
        self.temp_labels = []
        self.usage_labels = []
        self.previous_cpu_times = None
        self.badge = label("Connecting", "badge")
        self.badge.set_valign(Gtk.Align.CENTER)
        self.badge.set_wrap(False)
        self.notice = label("", "muted")
        self.notice.set_visible(False)
        self.identity = label("Nitro Control", "caption")
        self.identity.set_hexpand(True)
        header = box(self.identity, self.badge, vertical=False, spacing=8)
        fan_panel = self.fan_panel()
        cooling = self.cooling()
        if popup:
            close_button = button("×", self.close)
            close_button.set_tooltip_text("Close (Esc)")
            close_button.update_property([Gtk.AccessibleProperty.LABEL], ["Close"])
            header.append(close_button)
            keys = Gtk.EventControllerKey()
            keys.connect("key-pressed", self.popup_key_pressed)
            self.add_controller(keys)
            root = box(header, cooling, fan_panel, self.notice, spacing=12)
        else:
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                   hhomogeneous=False, vhomogeneous=False, vexpand=True)
            self.stack.add_titled(cooling, "cooling", "Cooling")
            self.stack.add_titled(self.system(), "system", "System")
            switcher = Gtk.StackSwitcher(stack=self.stack)
            switcher.add_css_class("nav")
            settings_title = label("Settings", "title")
            settings_title.set_hexpand(True)
            settings_header = card(box(settings_title, switcher, vertical=False))
            settings = box(settings_header, self.stack, self.temperature_history(), spacing=12)
            settings.set_hexpand(True)
            chassis = Gtk.Grid(column_homogeneous=True, vexpand=True)
            # Five equal tracks give the hardware and settings a 20:80 split.
            chassis.attach(fan_panel, 0, 0, 1, 1)
            settings.set_margin_start(16)
            chassis.attach(settings, 1, 0, 4, 1)
            root = box(header, label("Laptop cooling & power", "hero"), chassis,
                       self.notice, spacing=12)
        viewport = (Adw.Clamp(child=root, maximum_size=940, tightening_threshold=820)
                    if popup else root)
        for side in ("top", "bottom", "start", "end"):
            getattr(viewport, f"set_margin_{side}")(24 if not popup else 16)
        horizontal_policy = Gtk.PolicyType.NEVER if popup else Gtk.PolicyType.AUTOMATIC
        self.set_child(Gtk.ScrolledWindow(child=viewport, hscrollbar_policy=horizontal_policy,
                                          propagate_natural_width=False, propagate_natural_height=False))
        self.connect("close-request", self.close_requested)
        self.timer = GLib.timeout_add_seconds(2, self.refresh)
        self.refresh()

    def popup_key_pressed(self, _, key, _keycode, _modifiers):
        if key == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def close_requested(self, _):
        if self.busy:
            self.message("Wait for the current operation to finish before closing.")
            return True
        GLib.source_remove(self.timer)
        self.pool.shutdown(wait=False, cancel_futures=True)
        return False

    def action(self, title, feature, command, primary=False):
        widget = button(title, lambda: self.run(command()), primary)
        widget.set_sensitive(False)
        self.controls.append((widget, feature))
        return widget

    def fan_panel(self):
        fans = []
        readouts = []
        for index in (1, 2):
            speed = label("— RPM", "metric")
            temp = label("— °C", "muted")
            self.rpm_labels.append(speed)
            self.temp_labels.append(temp)
            readings = box(label(f"Fan {index}", "caption"), speed, spacing=4)
            if self.popup:
                rotor = Rotor(184)
                self.rotors.append(rotor)
                readings.append(temp)
                readings.set_valign(Gtk.Align.CENTER)
                panel = box(rotor, readings, vertical=False, spacing=24)
                panel.add_css_class("instrument")
                fans.append(panel)
            else:
                readouts.append(readings)
        if self.popup:
            return card(label("Fan telemetry", "title"), wrap_items(*fans, columns=2))
        self.gpu_model = GpuModel()
        readout_strip = box(*readouts, vertical=False, spacing=12)
        readout_strip.set_homogeneous(True)
        readout_strip.add_css_class("gpu-readouts")
        processor_stats = box(vertical=False, spacing=12)
        processor_stats.set_homogeneous(True)
        processor_stats.add_css_class("processor-stats")
        processor_stats.add_css_class("gpu-readouts")
        for name, temperature in zip(("CPU", "GPU"), self.temp_labels):
            temperature.add_css_class("metric")
            usage = label("Usage —", "muted")
            self.usage_labels.append(usage)
            processor_stats.append(box(label(name, "title"), temperature, usage, spacing=4))
        return card(label("Fan telemetry", "title"), self.gpu_model, readout_strip, processor_stats)

    def cooling(self):
        self.fan_state = label("Reading firmware mode…", "muted")
        modes = []
        for mode, description in (
            ("auto", "Firmware controls fan speed."),
            ("quiet", "Prioritize lower noise."),
            ("balanced", "Balance noise and performance."),
            ("performance", "Use the highest available performance profile."),
            ("max", "Run both fans at full speed."),
        ):
            widget = self.action(mode.capitalize(), mode, lambda name=mode: ["mode", name])
            widget.add_css_class("mode")
            widget.set_tooltip_text(description)
            widget.update_property([Gtk.AccessibleProperty.LABEL], [mode.capitalize()])
            self.mode_buttons[mode] = widget
            if not self.popup:
                widget.add_css_class("cooling-option")
                title = label(mode.capitalize(), "mode-name")
                title.set_hexpand(True)
                detail = label(description, "caption")
                detail.set_xalign(1)
                widget.set_child(box(title, detail, vertical=False, spacing=12))
            modes.append(widget)
        self.load_button = self.action("Load Acer driver", "load", lambda: ["load-driver"], True)
        self.load_button.set_halign(Gtk.Align.START)
        self.load_button.set_visible(False)
        options = (wrap_items(*modes, columns=5, spacing=4) if self.popup
                   else box(*modes, spacing=4))
        self.cooling_options = options
        self.pwm_targets = {}
        self.pwm_state = label("Looking for standard fan controls…", "muted")
        target = self.dropdown("pwm")
        target.set_hexpand(True)
        target.update_property([Gtk.AccessibleProperty.LABEL], ["Fan channel"])
        target.connect("notify::selected", lambda *_: self.pwm_selection_changed())
        self.pwm_speed = Gtk.SpinButton.new_with_range(60, 100, 5)
        self.pwm_speed.set_value(75)
        self.pwm_speed.update_property([Gtk.AccessibleProperty.LABEL], ["Fan speed percent"])
        self.pwm_seconds = Gtk.SpinButton.new_with_range(1, 300, 5)
        self.pwm_seconds.set_value(15)
        self.pwm_seconds.update_property([Gtk.AccessibleProperty.LABEL], ["Fan override duration in seconds"])
        manual = self.action("Apply timed speed", "pwm_manual", lambda: self.pwm_command())
        maximum = self.action("Timed full speed", "pwm", lambda: self.pwm_command(full=True))
        self.pwm_section = box(label("Standard fan control", "title"), self.pwm_state, target,
                               wrap_items(box(self.pwm_speed, label("%"), vertical=False, spacing=4),
                                          box(self.pwm_seconds, label("seconds"), vertical=False, spacing=4),
                                          manual, maximum, columns=4),
                               label("Restores the original automatic mode when the timer ends.", "caption"))
        self.pwm_section.add_css_class("settings-row")
        return card(label("Cooling mode", "title"), self.fan_state, options, self.load_button,
                    self.pwm_section)

    def pwm_command(self, full=False):
        target = self.pwm_targets[self.selected("pwm")]
        arguments = ["fan", "pwm-max" if full else "pwm-set", target["id"]]
        if not full:
            arguments.append(str(self.pwm_speed.get_value_as_int()))
        return arguments + ["--seconds", str(self.pwm_seconds.get_value_as_int()),
                            "--device", target["device"]]

    def pwm_selection_changed(self):
        item = self.models["pwm"][0].get_selected_item()
        target = self.pwm_targets.get(item.get_string()) if item else None
        automatic = bool(target and target["automatic"])
        manual = bool(automatic and target["manual"])
        available = {"pwm": automatic, "pwm_manual": manual}
        for widget, feature in self.controls:
            if feature in available:
                widget.set_sensitive(available[feature] and not self.busy)
        self.pwm_speed.set_sensitive(manual and not self.busy)
        self.pwm_seconds.set_sensitive(automatic and not self.busy)
        if target:
            state = (f"Automatic control · mode {target['mode']}" if automatic else
                     "Not in automatic mode. Restore Auto in the app managing this fan before retrying.")
        else:
            state = "No writable standard fan channels detected. Controls depend on the loaded driver."
        self.pwm_state.set_text(state)
        return available

    def render_pwm(self, data):
        if not self.busy:
            item = self.models["pwm"][0].get_selected_item()
            previous = item.get_string() if item else ""
            self.pwm_targets = {
                f"{target['driver']} · {target['label']} ({target['id']})": target
                for target in data["pwm"]
            }
            self.update_options("pwm", self.pwm_targets, previous)
        else:
            self.models["pwm"][0].set_sensitive(False)
        self.pwm_section.set_visible(bool(data["pwm"]) or not data["fan"])
        return self.pwm_selection_changed()

    def temperature_history(self):
        self.chart = Gtk.DrawingArea(content_height=112)
        self.chart.set_draw_func(self.draw_history)
        legend = label("CPU (solid)    GPU (dashed)    ·    °C    ·    Last 2 minutes", "muted")
        plot = box(legend, self.chart, spacing=8)
        plot.add_css_class("chart")
        return card(label("Temperature history", "title"), plot)

    def draw_history(self, _, ctx, width, height):
        left, right = 30, width - 5
        top, bottom = 8, height - 8
        ctx.select_font_face("monospace")
        ctx.set_font_size(11)
        for temperature in (100, 80, 60, 40, 20):
            y = top + (100 - temperature) / 80 * (bottom - top)
            ctx.set_source_rgba(.8, .8, .8, .65)
            ctx.move_to(0, y + 3)
            ctx.show_text(str(temperature))
            ctx.set_source_rgba(.8, .8, .8, .15)
            ctx.set_line_width(1)
            ctx.move_to(left, y)
            ctx.line_to(right, y)
            ctx.stroke()
        for index, values in enumerate(self.history):
            ctx.set_source_rgb(*((.92, .65, .43) if index == 0 else (.68, .71, .73)))
            ctx.set_line_width(1.75 if index == 0 else 1.25)
            ctx.set_dash([4, 4] if index else [])
            connected = False
            last_point = None
            for x, value in enumerate(values, start=60 - len(values)):
                if value is None:
                    connected = False
                    continue
                y = bottom - min(1, max(0, (value - 20) / 80)) * (bottom - top)
                point = (left + (right - left) * x / 59, y)
                if connected:
                    ctx.line_to(*point)
                else:
                    ctx.move_to(*point)
                connected = True
                last_point = point
            ctx.stroke()
            if last_point is not None:
                ctx.set_dash([])
                ctx.arc(*last_point, 2.5, 0, math.tau)
                ctx.fill()

    def dropdown(self, feature):
        dropdown = Gtk.DropDown.new_from_strings([])
        self.models[feature] = (dropdown, ())
        return dropdown

    def selected(self, feature):
        return self.models[feature][0].get_selected_item().get_string()

    def system(self):
        self.profile_state = label("—", "muted")
        profiles = card(label("Performance profile", "title"), self.profile_state,
                        wrap_items(self.dropdown("profile"), self.action("Apply profile", "profile",
                            lambda: ["profile", self.selected("profile")], True), columns=2))
        self.gpu_state = label("Waiting for NVIDIA telemetry", "muted")
        self.watts = Gtk.SpinButton.new_with_range(1, 65, 1)
        self.watts.set_digits(1)
        self.watts.set_value(45)
        self.watts.update_property([Gtk.AccessibleProperty.LABEL], ["Power limit in watts"])
        watts_input = box(self.watts, label("W", "caption"), vertical=False, spacing=8)
        self.watts.set_hexpand(True)
        power = card(label("NVIDIA power limit", "title"), self.gpu_state,
                     wrap_items(watts_input, self.action("Apply limit", "gpu", lambda: [
                         "gpu-power", str(self.watts.get_value())], True),
                         self.action("Restore default", "gpu", lambda: ["gpu-power", "default"]),
                         columns=3))
        self.firmware_rows = {}
        rows = []
        firmware_choices = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        firmware_actions = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for option, title, values in (
            ("battery_limiter", "Battery charge limit", {"Off": 0, "Limit to 80%": 1}),
            ("backlight_timeout", "Keyboard backlight timeout", {"Off": 0, "On": 1}),
            ("usb_charging", "USB charging while off", {"Off": 0, "Stop at 10%": 10,
                                                       "Stop at 20%": 20, "Stop at 30%": 30}),
        ):
            state = label("—", "muted")
            text = box(label(title), state, spacing=4)
            text.set_hexpand(True)
            dropdown = self.dropdown(option)
            action = self.action("Apply", option, lambda key=option, mapping=values: [
                "firmware", key, str(mapping[self.selected(key)])])
            self.firmware_rows[option] = (state, values)
            dropdown.update_property([Gtk.AccessibleProperty.LABEL], [title])
            action.update_property([Gtk.AccessibleProperty.LABEL], [f"Apply {title.lower()}"])
            firmware_choices.add_widget(dropdown)
            firmware_actions.add_widget(action)
            dropdown.set_valign(Gtk.Align.CENTER)
            action.set_valign(Gtk.Align.CENTER)
            row = box(text, dropdown, action, vertical=False, spacing=12)
            row.add_css_class("settings-row")
            rows.append(row)
        self.battery_state = label("Reading battery…", "muted")
        return box(profiles, power,
                   card(label("Battery and keyboard", "title"), self.battery_state, *rows),
                   spacing=12)

    def refresh(self):
        if not self.pending:
            self.pending = True
            future = self.pool.submit(sample)
            future.add_done_callback(lambda done: GLib.idle_add(self.render, done))
        return GLib.SOURCE_CONTINUE

    def update_options(self, key, options, current):
        dropdown, previous = self.models[key]
        options = tuple(options)
        if options != previous:
            dropdown.set_model(Gtk.StringList.new(options))
            if current in options:
                dropdown.set_selected(options.index(current))
            self.models[key] = (dropdown, options)
        dropdown.set_sensitive(bool(options) and not self.busy)

    def render(self, future):
        self.pending = False
        try:
            self.data = future.result()
        except (OSError, ValueError, RuntimeError) as error:
            self.message(str(error), error=True)
            return
        data = self.data
        available = self.render_cooling(data)
        self.render_telemetry(data)
        if not self.popup:
            self.render_system(data, available)
        self.update_controls(available)

    def render_cooling(self, data):
        available = {"fan": bool(data["fan"]), "profile": bool(data["choices"])}
        available.update(self.render_pwm(data))
        available["load"] = data["acer"] and not available["fan"] and nitro.driver_module().is_file()
        self.load_button.set_visible(available["load"])
        detected = available["fan"] or available["profile"] or bool(data["rpm"]) or bool(data["pwm"])
        model = data.get("model")
        self.identity.set_text(f"Nitro Control  /  {model}" if model else "Nitro Control")
        self.badge.set_text("Hardware detected" if detected else "Fan controls offline")
        state = data["fan"]
        profile = data["profile"]
        if self.auto_profile != profile:
            self.auto_profile = None
        active = "auto"
        if self.auto_profile is None:
            active = next(
                (name for name, aliases in nitro.COOLING_PROFILES.items() if profile in aliases),
                "auto",
            )

        choices = set(data["choices"])
        self.cooling_options.set_visible(bool(state) or bool(choices))
        for name, widget in self.mode_buttons.items():
            widget.set_visible(name not in {"auto", "max"} or bool(state) or data["acer"])
            aliases = nitro.COOLING_PROFILES[name]
            available[name] = bool(choices.intersection(aliases)) if aliases else bool(state)
            max_active = state == "100,100" and name == "max"
            profile_active = name == active and (
                state == "0,0" or (not state and available[name])
            )
            if max_active or profile_active:
                widget.add_css_class("active-mode")
            else:
                widget.remove_css_class("active-mode")

        if state == "0,0":
            profile_name = (profile or "unavailable").replace("-", " ").capitalize()
            message = f"Firmware control  ·  {profile_name}"
        elif state == "100,100":
            message = "Max cooling · Both fans requested at 100%"
        elif state:
            message = f"External override · CPU/GPU {state}%"
        elif available["load"]:
            message = "Driver available to load"
        elif profile:
            message = f"Platform profile · {profile}"
        elif data["pwm"]:
            count = len(data["pwm"])
            message = f"Standard fan control · {count} {'channel' if count == 1 else 'channels'} detected"
        else:
            message = "Direct fan control unavailable"
        self.fan_state.set_text(message)
        return available

    def render_telemetry(self, data):
        readings = data["gpu"]
        for index, speed in enumerate(self.rpm_labels):
            rpm = data["rpm"][index] if index < len(data["rpm"]) else ""
            value = int(rpm) if rpm.isdigit() else 0
            if self.popup:
                rotor = self.rotors[index]
                rotor.rpm = value
                rotor.queue_draw()
            else:
                self.gpu_model.rpm[index] = value
            speed.set_text(f"{value:,} RPM" if rpm.isdigit() else "— RPM")
        cpu = data["cpu"]
        cpu_temp = int(cpu) / 1000 if cpu.lstrip("-").isdigit() else None
        cpu_label = f"{cpu_temp:.1f} °C" if cpu_temp is not None else "— °C"
        self.temp_labels[0].set_text(f"CPU  {cpu_label}" if self.popup else cpu_label)
        gpu_temp = data["gpu_temp"]
        try:
            gpu_temp = float(readings[1]) if readings else int(gpu_temp) / 1000
        except ValueError:
            gpu_temp = int(gpu_temp) / 1000 if gpu_temp else None
        gpu_label = f"{gpu_temp:.1f} °C" if gpu_temp is not None else "— °C"
        gpu_usage = readings[2] if readings else data["gpu_load"]
        if self.popup:
            if gpu_usage.isdigit():
                gpu_label += f" · {gpu_usage}% load"
            self.temp_labels[1].set_text(f"GPU  {gpu_label}")
            return
        self.temp_labels[1].set_text(gpu_label)
        cpu_usage = None
        times = data["cpu_times"]
        if times and self.previous_cpu_times:
            total = times[0] - self.previous_cpu_times[0]
            idle = times[1] - self.previous_cpu_times[1]
            if total > 0 and 0 <= idle <= total:
                cpu_usage = round(100 * (total - idle) / total)
        self.previous_cpu_times = times
        self.usage_labels[0].set_text(f"Usage {cpu_usage}%" if cpu_usage is not None else "Usage —")
        self.usage_labels[1].set_text(f"Usage {gpu_usage}%" if gpu_usage.isdigit() else "Usage —")
        for history, temperature in zip(self.history, (cpu_temp, gpu_temp)):
            if temperature is not None and not math.isfinite(temperature):
                temperature = None
            history.append(temperature)
        self.chart.queue_draw()

    def render_system(self, data, available):
        readings = data["gpu"]
        self.battery_state.set_text(data["battery"] or "Battery telemetry unavailable")
        self.profile_state.set_text(f"Current · {data['profile']}" if data["profile"] else "Unavailable")
        self.update_options("profile", data["choices"], data["profile"])
        if readings:
            self.gpu_state.set_text(
                f"{readings[3]} MHz  ·  {readings[4]} / {readings[5]} MiB VRAM  ·  "
                f"{readings[6]} W draw  ·  {readings[8]} W ceiling")
        else:
            self.gpu_state.set_text("NVIDIA telemetry unavailable")
        available["gpu"] = False
        if readings:
            try:
                default, minimum, maximum = map(float, readings[9:12])
                if all(map(math.isfinite, (default, minimum, maximum))) and minimum <= default <= maximum:
                    self.watts.set_range(minimum, maximum)
                    available["gpu"] = True
            except ValueError:
                pass
        self.watts.set_sensitive(available["gpu"] and not self.busy)
        for key, (state_label, mapping) in self.firmware_rows.items():
            current = next((name for name, value in mapping.items()
                            if str(value) == data["settings"][key]), "")
            available[key] = bool(current)
            state_label.set_text(f"Current · {current}" if current else "Unavailable")
            self.update_options(key, mapping if current else (), current)

    def update_controls(self, available):
        for widget, feature in self.controls:
            widget.set_sensitive(available[feature] and not self.busy)

    def message(self, text, error=False):
        self.notice.set_text(text)
        self.notice.set_visible(bool(text))
        if error:
            self.notice.add_css_class("error")
        else:
            self.notice.remove_css_class("error")

    def run(self, arguments):
        if self.busy:
            return
        self.busy = True
        self.models["pwm"][0].set_sensitive(False)
        for widget, _ in self.controls:
            widget.set_sensitive(False)
        use_helper = arguments[0] != "load-driver" and PRIVILEGED_HELPER.is_file()
        try:
            pkexec = nitro.system_command("pkexec")
            command = [str(PRIVILEGED_HELPER)] if use_helper else [
                nitro.system_command("python3", trusted=True), str(HERE / "nitro.py")]
        except RuntimeError as error:
            self.busy = False
            self.message(str(error), error=True)
            self.refresh()
            return
        self.message("Applying hardware setting…" if use_helper else
                     "Waiting for administrator authentication and driver response…")
        future = self.pool.submit(subprocess.run, [pkexec, *command, *arguments],
                                  capture_output=True, text=True)
        future.add_done_callback(lambda done: GLib.idle_add(self.complete, done, arguments))

    def complete(self, future, arguments):
        self.busy = False
        try:
            result = future.result()
            if result.returncode == 0 and arguments[0] in ("mode", "profile"):
                self.auto_profile = self.data.get("profile", "") if arguments == ["mode", "auto"] else None
            output = (result.stdout if result.returncode == 0 else result.stderr or result.stdout).strip()
            self.message(output.splitlines()[-1] if output else
                         ("Applied." if result.returncode == 0 else "Authentication or hardware command failed."),
                         error=result.returncode != 0)
        except OSError as error:
            self.message(str(error), error=True)
        self.refresh()


def install_launcher():
    directory = Path(GLib.get_user_data_dir()) / "applications"
    directory.mkdir(parents=True, exist_ok=True)
    # Desktop Exec entries have their own quoting rules and percent field codes.
    path = str(Path(__file__).resolve()).replace("%", "%%")
    path = "".join("\\" + char if char in '"\\$`' else char for char in path)
    path = path.replace("\\", "\\\\")
    launcher = directory / f"{APP_ID}.desktop"
    launcher.write_text("[Desktop Entry]\nType=Application\nName=Nitro Control\n"
                        "Comment=Laptop cooling and performance controls\n"
                        f'Exec=python3 "{path}"\n'
                        "Icon=utilities-system-monitor\nTerminal=false\nCategories=System;Settings;\n")
    print(f"Installed {launcher}")


def activate(app, popup=False):
    window = app.get_active_window()
    if popup and window is not None:
        window.close()
        return
    if window is None:
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                   Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        window = Dashboard(app, popup=popup)
    window.present()


if __name__ == "__main__":
    if sys.argv[1:] == ["--install-launcher"]:
        install_launcher()
    else:
        popup = sys.argv[1:] == ["--popup"]
        app = Gtk.Application(application_id=POPUP_ID if popup else APP_ID)
        app.connect("activate", activate, popup)
        sys.exit(app.run(sys.argv[:1] if popup else sys.argv))
