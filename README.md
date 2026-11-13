# Nitro Control for Linux

A small GTK app and CLI for independent laptop fan control, performance
profiles, GPU telemetry and supported Acer firmware settings.

## Install

Download and extract this repository, or clone it:

```bash
git clone https://github.com/optimis3r/nitro-control.git
cd nitro-control
./install.sh
```

Run from your normal desktop account. The installer requests sudo, installs
system packages and desktop permissions, and adds **Nitro Control** to the app
menu. Launch with `nitro-control`; inspect hardware with `nitro status`.
Rerun the installer to update.

Automatic installation supports **Arch Linux, Debian/Ubuntu and Fedora**, with
Python 3.9+, internet access and distribution repositories. The desktop uses
GTK 4, libadwaita and PyGObject. No pip environment is needed. Source launches
may request authentication through your desktop Polkit agent.

On the **Acer Nitro ANV15-41**, installation also builds the pinned
[Linuwu-Sense driver](https://github.com/0x7375646F/Linuwu-Sense/tree/4483b6c38b9bb963ac9ada9ca476578e0c63cc08)
with DKMS, verifies fan control and enables loading at boot. This requires
systemd and headers matching the running kernel. DKMS rebuilds for kernel
updates when matching headers are installed. If headers for an old running
kernel are unavailable after an update, reboot and rerun installation.
Secure Boot may require enrolling the DKMS signing key. A failed load restores
the stock driver where it was previously loaded and leaves boot loading disabled.

Other laptops use existing Linux drivers; unverified vendor modules are not
installed. To update the app and helper without changing drivers:

```bash
./install.sh --without-driver
```

To remove the app, permissions and managed driver:

```bash
./install.sh --remove
```

Removal requests Acer Auto and restores the stock driver when this installer
owned its setup. Installed distribution packages are kept. App and driver setup
are system-wide; desktop setup applies to the installing account. On Hyprland,
F23 and Super+Shift+F23 open Quick Controls. Edited configuration files receive
`.before-nitro-control` backups; reload Hyprland to activate the shortcuts.

## Controls

Sections: **Fans → Performance → GPU → Battery and Keyboard → Graphics**.

- **Fans:** each Acer fan has immediate Auto, Quiet (20%), Balanced (50%) and
  Performance (100%) toggles. A change preserves the other fan's request.
  Manual settings remain after closing the app; Auto returns control to firmware.
- **Performance:** Quiet, Balanced and Performance (max), where the driver
  exposes corresponding profiles. Acer profile changes can reset both fans to Auto.
- **GPU:** Quiet requests 75% of the default power limit, clamped to the driver
  minimum; Balanced restores the default; Performance requests the driver maximum.
  Firmware may reject changes or enforce its own ceiling.
- **Battery and Keyboard:** charge limit, keyboard timeout and off-state USB
  charging appear only when supported by the Acer driver.
- **Graphics:** GPU names, temperature, load, clock, memory and power readings
  where available. Names use NVIDIA telemetry or the local PCI database, with
  a vendor-name fallback. AMD package power is labelled separately from GPU power.

Hardware refreshes every two seconds. CPU fan controls remain available when
the GPU is off or lacks a driver. Suspended NVIDIA GPUs are not polled, although
a power-state change can race with a poll. A GPU hidden from Linux is reported
as not detected; this cannot reliably distinguish firmware disabling from absence.
Fan availability is independent of GPU activity. Zero RPM means stopped.

Other laptops show standard writable
[Linux hwmon PWM channels](https://docs.kernel.org/hwmon/sysfs-interface.html)
with individual timed controls. Driver labels are retained rather than assuming
CPU/GPU ownership. RPM-only sensors are monitor only. Timed overrides require
an automatic starting mode and restore it after 1–300 seconds, Ctrl+C or SIGTERM.
Driver readback confirms a request; RPM or audible response confirms rotation.
Forced termination, device removal or firmware failure can prevent restoration;
reboot if control cannot be recovered. Timed operations serialize hardware changes.

## GPU drivers

**CPU and Acer fan control do not require an NVIDIA graphics driver.** NVIDIA
telemetry and power controls require a working driver and `nvidia-smi`.
Follow your distribution's installation guide:

- [Arch Linux](https://wiki.archlinux.org/title/NVIDIA)
- [Ubuntu](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/)
- [Debian](https://wiki.debian.org/NvidiaGraphicsDrivers)
- [Fedora / RPM Fusion](https://rpmfusion.org/Howto/NVIDIA)

The installer does not install or switch GPU drivers. Keep a deliberately
disabled GPU disabled. AMD and Intel use their distribution/kernel drivers.
A graphics driver alone does not add laptop fan control; that requires a
compatible firmware driver or writable hwmon interface.

## CLI and source

Status needs no sudo; hardware changes do:

```bash
nitro status
sudo nitro fan set cpu 20               # CPU Quiet; retain GPU request
sudo nitro fan set gpu 50               # GPU Balanced; retain CPU request
sudo nitro fan set cpu auto
sudo nitro fan set cpu 100 --seconds 30  # Restore only CPU to Auto afterwards
sudo nitro fan auto                     # Both Acer fans to Auto
sudo nitro fan pwm-set hwmon3/pwm1 50 --seconds 30
sudo nitro fan pwm-max hwmon3/pwm2 --seconds 15
sudo nitro profile balanced             # Choose a name reported by status
sudo nitro gpu-power 45                 # Watts, within live driver limits
sudo nitro gpu-power default
```

Use PWM IDs from your current status output; they can change after reboot or
reload. The desktop rechecks the device identity before a PWM write.

`nitro.py` contains hardware discovery and the CLI; `nitro_gui.py` is the desktop;
`nitro-helper` restricts privileged desktop commands. `install.sh` handles all setup.
To run from source, use `python3 nitro.py status` or `python3 nitro_gui.py`.

After backend changes, rerun `./install.sh --without-driver` and reopen the app
to update the root-owned helper. This also resolves **Unsupported Nitro Control
helper command** or **outdated helper** errors from an older installed copy.
