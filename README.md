# Nitro Control for Linux

## Install everything

From your desktop account, run:

```bash
./install.sh
```

The installer requests sudo, installs distribution dependencies, copies the
app and artwork into `/usr/local/lib/nitro-control`, registers **Nitro
Control** in the application menu, and configures the privileged helper
for your active local desktop session. Launch it from the menu or with
`nitro-control`; use `nitro status` for the CLI. The project folder is no
longer needed after installation. Rerun the same command to update the app.

Automatic dependency installation supports Arch Linux, Debian/Ubuntu and
Fedora, with Python 3.9 or newer. An internet connection and access to the
distribution package repositories are required. On Arch, update your system
normally before installing. If the running kernel's headers are no longer
available after an update, reboot into the installed kernel and rerun.

On the **Acer Nitro ANV15-41**, installation also builds the pinned
Linuwu-Sense driver with DKMS, loads and verifies it immediately, and
enables `nitro-control-driver.service` to load it at boot. DKMS rebuilds
the module on kernel upgrades when matching headers are installed. A
driver load failure restores the stock driver and leaves the boot service
disabled. Secure Boot systems may require enrolling the DKMS signing key
before the kernel will accept the module; installation reports failure
instead of claiming hardware setup succeeded.

Other models receive the app and use their existing kernel interfaces.
To install the app without changing drivers on the supported Acer, run
`./install.sh --without-driver`. This option leaves any previously
installed driver setup in place.

For Hyprland installations with `~/.config/hypr/hyprland.conf`, the
installer adds F23 and Super+Shift+F23 shortcuts for Quick Controls and
migrates the old project-folder bindings. Original configuration files
are backed up with a `.before-nitro-control` suffix. Reload Hyprland or
log in again to activate them.

To remove the installed app, its permissions, shortcuts and managed driver:

```bash
./install.sh --remove
```

Removal restores firmware Auto and the stock Acer driver where applicable.
Distribution packages installed as dependencies are kept. Desktop setup
and cleanup apply to the account running the installer; the app and
driver installation are system-wide.

## Driver compatibility

The app follows the [Linux hwmon interface](https://docs.kernel.org/hwmon/sysfs-interface.html)
and discovers capabilities instead of requiring a particular driver
name. It reads temperatures and fan RPM from Linux hwmon drivers, offers
the platform profiles reported by the kernel, and exposes standard PWM
fan channels in the desktop app. Channels are identified by driver,
label and channel ID; their order does not imply CPU or GPU ownership.

In **Cooling → Standard fan control**, choose a detected channel, a speed
from 60–100%, and a duration from 1–300 seconds. **Apply timed speed**
temporarily uses manual PWM control. **Timed full speed** requests full
speed, with a manual 100% fallback for drivers without full-speed mode.
Both restore the channel's original automatic mode when the timer ends
or the command receives Ctrl+C/SIGTERM. Channels already under manual
control are left to their existing controller. The selected physical
device is checked again before a desktop command to detect hotplug changes.

The same controls are available from the CLI:

```bash
nitro status
sudo nitro fan pwm-set hwmon3/pwm1 75 --seconds 30
sudo nitro fan pwm-max hwmon3/pwm1 --seconds 15
```

Use the channel ID reported on your machine. Firmware or the driver can
reject individual modes or writes; the app reports that failure and
attempts to restore the original automatic mode. A forced kill or device
removal can prevent restoration; reboot if control cannot be recovered.

This supports drivers implementing the standard Linux interfaces, not
every possible vendor protocol. A driver exposing only RPM cannot gain
fan-speed controls through app installation. Vendor-specific interfaces
need a dedicated backend; Acer firmware options and automatic installation
of Linuwu-Sense remain restricted to the verified ANV15-41 model. Other
drivers are supplied and selected by the kernel/distribution; the installer
does not guess or install arbitrary vendor modules. NVIDIA power controls
still require a working NVIDIA driver with `nvidia-smi`.

## CLI and manual driver trial

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
driver to accept standard hwmon full-speed or manual PWM control. It restores the
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

After `./install.sh`, open **Nitro Control** from the application menu or
run `nitro-control`. For development directly from the project folder:

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
is offline. Standard PWM controls are available in the desktop app and CLI
for drivers exposing the corresponding Linux interfaces.

System shows the available performance profiles and NVIDIA power
controls. It also exposes the Acer battery charge limit, keyboard
backlight timeout and off-state USB charging where the driver reports
them.

The full installer configures permissions automatically. To update only
the privileged helper during development, run from the account using the app:

```bash
sudo ./install-permissions.sh
```

This installs a root-owned helper and permits the active local desktop
session for that account to change cooling modes, performance profiles,
GPU power, and the supported Acer firmware options. Loading the kernel
driver still requires authentication. Rerun the installer to update the
helper after backend changes. To revoke access, run
`sudo ./install-permissions.sh --remove`.

On Hyprland, the installed Copilot binding opens **Nitro Quick Controls**. Press it
again or press Esc to close. The user keybinds accept Super+Shift+F23 and
bare F23. The popup can also be toggled with `nitro-control --popup`
(or `python3 nitro_gui.py --popup` during development).
It runs as your desktop user and uses the same Polkit helper as the main
window for hardware changes.
