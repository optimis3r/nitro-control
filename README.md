# Nitro Control for Linux

Nitro Control discovers the sensors and controls exposed by the running
Linux kernel. The CLI requires Python 3.9 or newer and uses the standard
library. Desktop and privileged operations have additional dependencies
described below.

```bash
python3 nitro.py status
```

Status runs without sudo and lists available hwmon temperatures, fan RPM,
PWM channels, platform profiles, and optional NVIDIA telemetry. Features
vary with the laptop, firmware, loaded drivers, and kernel configuration.
A system can expose RPM or platform profiles without exposing direct fan
control. If no control interface exists, the app reports it as unavailable.

If status lists a platform profile, select a reported name, such as
`balanced`:

```bash
sudo python3 nitro.py profile balanced
```

### Test a standard Linux PWM channel

Use the channel identifier shown by the current status output and check
the accompanying driver name. These identifiers can change after a reboot
or driver reload.

```bash
sudo python3 nitro.py fan pwm-max hwmon3/pwm1 --seconds 15
```

The test requires the channel to start in an automatic mode and its
driver to accept the standard hwmon full-speed mode. It restores the
original mode when the timer ends, on Ctrl+C, or on SIGTERM. If the
process is forcibly killed, reboot to reset the controller. Check
tachometer readings or listen for spin-up to confirm the physical fan
response.

## Enable the optional Acer Nitro ANV15-41 driver

The stock module on the target machine exposes neither fan control nor
platform profiles. The pinned [Linuwu-Sense source](https://github.com/0x7375646F/Linuwu-Sense/blob/4483b6c38b9bb963ac9ada9ca476578e0c63cc08/src/linuwu_sense.c)
has an explicit match for the ANV15-41. The build uses Git, the kernel
build toolchain and headers for the running kernel. It compiles without
sudo and produces a module for that kernel under `.build`.

```bash
./build-driver.sh
sudo modprobe -r acer_wmi &&
  { { sudo modprobe -a sparse_keymap wmi rfkill video &&
      sudo insmod ".build/$(uname -r)/linuwu_sense.ko"; } ||
    sudo modprobe acer_wmi; }
./nitro.py status
```

Load the dependencies after removing the stock module. Arch can unload
`sparse_keymap` along with `acer_wmi`, and `insmod` does not load dependencies.

Stop here if removal of `acer_wmi` fails. If Linuwu-Sense fails to load
or the fan interface is missing, use the rollback below and capture
`sudo journalctl -k -b -n 80`. This trial changes the running driver
until reboot. Rebuild after a kernel upgrade.

## Verify the fans and set a profile

Start with a short maximum-speed test at idle. Check for audible spin-up
and RPM changes if the driver publishes tachometer readings. The fan
setting in status is driver readback of the request. RPM or audible
spin-up is the physical check.

```bash
sudo ./nitro.py fan max --seconds 15
sudo ./nitro.py fan manual 75 85 --seconds 30
sudo ./nitro.py fan auto
```

Manual values are CPU then GPU, from 60 to 100 percent. Use 0 to leave a
channel under firmware control. Manual and maximum modes last up to 300
seconds and request firmware Auto when they finish, on Ctrl+C, or on
SIGTERM. After a forced kill or if the command reports a failed reset,
run `sudo ./nitro.py fan auto`.

## GPU power

The driver reported a 50 W default and a 65 W maximum on this machine.
The program reads the live limits for every change and NVIDIA decides
whether a write is supported. Acer firmware can apply its own ceiling.

```bash
sudo ./nitro.py gpu-power 45
sudo ./nitro.py gpu-power default
```

## Roll back the trial

End any active manual fan command first. If the fan interface is present,
request Auto before unloading the module.

```bash
sudo ./nitro.py fan auto
sudo rmmod linuwu_sense
sudo modprobe acer_wmi
```

Rebooting also restores the normal driver. The build and trial do not
install startup services or change the boot configuration.

## Desktop app

Open **Nitro Control** from the application menu. To launch it directly
or register the menu entry on another installation, run:

```bash
python3 nitro_gui.py
python3 nitro_gui.py --install-launcher
```

The app uses GTK 4, libadwaita, PyGObject and Pycairo. Desktop writes
require `pkexec` and a desktop Polkit agent. The included permissions
installer expects standard Polkit directories and Python at
`/usr/bin/python3`. The CLI also works without a desktop or Polkit.
On Arch the required libraries are provided by `gtk4`, `libadwaita`,
`python-gobject` and `python-cairo`.

Cooling shows up to two fan RPM readings, available CPU and GPU
temperatures and usage, and a rolling temperature chart. The main window
loads the GPU and rotor artwork from the bundled `assets/` directory. Quiet, Balanced and Performance select
the corresponding platform profiles where available. Auto and sustained
Max use the supported Acer driver. On that model, **Load Acer driver**
appears when a build exists for the running kernel and the fan interface
is offline. The timed standard PWM test is available through the CLI.

System shows the available performance profiles and NVIDIA power
controls. It also exposes the Acer battery charge limit, keyboard
backlight timeout and off-state USB charging where the driver reports
them.

To change settings without repeated password prompts, run once from the
account that uses the app:

```bash
sudo ./install-permissions.sh
```

This installs a root-owned helper and permits the active local desktop
session for that account to change cooling modes, performance profiles,
GPU power, and the supported Acer firmware options. Loading the kernel
driver still requires authentication. Rerun the installer to update the
helper after backend changes. To revoke access, run
`sudo ./install-permissions.sh --remove`.

On Hyprland, the Copilot binding opens **Nitro Quick Controls**. Press it
again or press Esc to close. The user keybinds accept Super+Shift+F23 and
bare F23. The popup can also be toggled with `python3 nitro_gui.py --popup`.
It runs as your desktop user and uses the same Polkit helper as the main
window for hardware changes.
