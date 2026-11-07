"""Driver-independent PWM behavior against isolated sysfs fixtures."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nitro


class PWMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.device = self.root / "class/hwmon/hwmon7"
        self.device.mkdir(parents=True)
        self.mode = self.device / "pwm3_enable"
        self.duty = self.device / "pwm3"
        self.mode.write_text("3\n")
        self.duty.write_text("100\n")
        (self.device / "name").write_text("example_driver\n")
        (self.device / "pwm3_label").write_text("Chassis fan\n")
        self.sysfs = patch.object(nitro, "SYS", self.root)
        self.sysfs.start()
        self.addCleanup(self.sysfs.stop)
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_discovery_accepts_unknown_driver_and_arbitrary_channel(self):
        controls = nitro.pwm_controls()
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0], {
            "id": "hwmon7/pwm3", "device": str(self.device.resolve()),
            "driver": "example_driver", "label": "Chassis fan", "mode": "3",
            "manual": True, "automatic": True,
        })

    def test_manual_speed_restores_driver_specific_automatic_mode(self):
        def during_sleep(seconds):
            self.assertEqual(seconds, 15)
            self.assertEqual(self.mode.read_text().strip(), "1")
            self.assertEqual(self.duty.read_text().strip(), "191")
        with patch.object(nitro.time, "sleep", side_effect=during_sleep):
            nitro.pwm_override("hwmon7/pwm3", 15, 75, str(self.device.resolve()))
        self.assertEqual(self.mode.read_text().strip(), "3")

    def test_full_speed_supports_mode_zero(self):
        def during_sleep(_):
            self.assertEqual(self.mode.read_text().strip(), "0")
        with patch.object(nitro.time, "sleep", side_effect=during_sleep):
            nitro.pwm_max("hwmon7/pwm3", 15)
        self.assertEqual(self.mode.read_text().strip(), "3")

    def test_full_speed_falls_back_for_drivers_without_mode_zero(self):
        original_write = nitro.write_confirmed
        def reject_zero(path, value):
            if path == self.mode and value == "0":
                raise OSError("Unsupported control mode")
            original_write(path, value)
        def during_sleep(_):
            self.assertEqual(self.mode.read_text().strip(), "1")
            self.assertEqual(self.duty.read_text().strip(), "255")
        with patch.object(nitro, "write_confirmed", side_effect=reject_zero), \
             patch.object(nitro.time, "sleep", side_effect=during_sleep):
            nitro.pwm_max("hwmon7/pwm3", 15)
        self.assertEqual(self.mode.read_text().strip(), "3")

    def test_interrupt_restores_automatic_mode(self):
        with patch.object(nitro.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                nitro.pwm_override("hwmon7/pwm3", 15, 75)
        self.assertEqual(self.mode.read_text().strip(), "3")

    def test_rejected_duty_write_restores_automatic_mode(self):
        original_write = nitro.write_confirmed
        def reject_duty(path, value):
            if path == self.duty:
                raise OSError("Driver rejected PWM duty")
            original_write(path, value)
        with patch.object(nitro, "write_confirmed", side_effect=reject_duty):
            with self.assertRaises(OSError):
                nitro.pwm_override("hwmon7/pwm3", 15, 75)
        self.assertEqual(self.mode.read_text().strip(), "3")

    def test_restore_failure_is_reported(self):
        original_write = nitro.write_confirmed
        def reject_restore(path, value):
            if path == self.mode and value == "3":
                raise OSError("Device disconnected")
            original_write(path, value)
        with patch.object(nitro, "write_confirmed", side_effect=reject_restore), \
             patch.object(nitro.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Could not restore"):
                nitro.pwm_max("hwmon7/pwm3", 15)

    def test_manual_channels_are_not_taken_over(self):
        self.mode.write_text("1\n")
        with self.assertRaisesRegex(RuntimeError, "automatic mode"):
            nitro.pwm_override("hwmon7/pwm3", 15, 75)
        self.assertEqual(self.duty.read_text(), "100\n")

    def test_reused_hwmon_number_is_rejected_before_writes(self):
        with self.assertRaisesRegex(RuntimeError, "device changed"):
            nitro.pwm_override("hwmon7/pwm3", 15, 75, "/old/device")
        self.assertEqual(self.mode.read_text(), "3\n")
        self.assertEqual(self.duty.read_text(), "100\n")

    def test_unknown_channel_cannot_access_arbitrary_files(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown PWM channel"):
            nitro.pwm_override("../../etc/passwd", 15, 75)

    def test_unbounded_overrides_are_rejected(self):
        for seconds, percent in ((0, 75), (301, 75), (15, 0), (15, 101)):
            with self.subTest(seconds=seconds, percent=percent):
                with self.assertRaises(RuntimeError):
                    nitro.pwm_override("hwmon7/pwm3", seconds, percent)
        self.assertEqual(self.mode.read_text(), "3\n")

    def test_helper_only_allows_the_standard_fan_subcommands(self):
        helper = Path(nitro.__file__).with_name("nitro-helper")
        for command in (("fan", "pwm-set"), ("fan", "pwm-max")):
            with patch.object(sys, "argv", [str(helper), *command]), patch.object(nitro, "main") as main:
                runpy.run_path(str(helper), run_name="__main__")
                main.assert_called_once()
        for command in (("fan", "manual"), ("fan", "auto"), ("load-driver",), ("fan",)):
            with patch.object(sys, "argv", [str(helper), *command]), patch.object(nitro, "main") as main:
                with self.assertRaises(SystemExit):
                    runpy.run_path(str(helper), run_name="__main__")
                main.assert_not_called()

    def test_cli_standard_controls_do_not_require_an_acer(self):
        with patch.object(sys, "argv", ["nitro", "fan", "pwm-set", "hwmon7/pwm3", "75"]), \
             patch.object(nitro, "require_root"), patch.object(nitro, "require_acer") as acer, \
             patch("builtins.open", mock_open()), patch.object(nitro.fcntl, "flock"), \
             patch.object(nitro, "pwm_override") as override:
            nitro.main()
            acer.assert_not_called()
            override.assert_called_once_with("hwmon7/pwm3", 30, 75, None)


if __name__ == "__main__":
    unittest.main()
