# ha-cpu-power

Control the CPU of a Linux server from Home Assistant and save power when electricity is expensive.

ha-cpu-power is a small service that exposes your CPU as a Home Assistant device over MQTT Discovery.
You get sensors for frequency, power, temperature and load, and controls for max frequency, turbo,
governor, energy preference and the RAPL power limit, plus profiles (Eco / Balanced / Max) that you
can switch from an automation, e.g. based on the Nordpool spot price.

*Svenska: Styr CPU-frekvens och effekt på en Linux-server från Home Assistant, t.ex. efter elpriset.
Välj `sv` i installationsguiden för svenska entitetsnamn.*

## Features

| Entity | Type | Notes |
|---|---|---|
| Profile | select | Profiles from the config, plus *Custom* when you change something by hand |
| Max frequency | number | Hardware min–max MHz |
| Turbo | switch | Intel turbo / AMD boost |
| Governor | select | Available cpufreq governors |
| Energy preference | select | EPP, when the driver supports it (intel_pstate, amd-pstate) |
| Power limit | number | RAPL package limit in W, only shown if the CPU accepts it |
| Frequency average / peak core | sensor | MHz |
| CPU power / RAM power | sensor | W, from RAPL |
| Temperature | sensor | °C |
| Load | sensor | % |

- One Python file, only dependency `paho-mqtt`
- Guided installer that detects the hardware and writes a matching config
- Republishes discovery when Home Assistant restarts, marks the device unavailable when stopped
- Puts the original CPU settings back when the service stops

## Hardware support

| | Frequency, governor | Turbo | Energy pref. | Power reading | Power limit | Temperature |
|---|---|---|---|---|---|---|
| Intel | ✅ | ✅ | ✅ (HWP CPUs) | ✅ | ✅ unless locked in BIOS | ✅ coretemp |
| AMD Zen | ✅ | ✅ | ✅ (amd-pstate) | ✅ recent kernels | ❌ | ✅ k10temp / zenpower |
| ARM (e.g. Raspberry Pi) | ✅ | ❌ | ❌ | ❌ | ❌ | ✅ thermal zone |
| Virtual machine / container | ❌ usually no cpufreq – run it on the host | | | | | |

Hybrid CPUs (P/E cores) and multi-socket systems are supported. Entities that the hardware can't
provide are simply not created.

## Install

Needs Linux with systemd, Python 3 and an MQTT broker that Home Assistant uses (e.g. the Mosquitto add-on).

```sh
git clone https://github.com/ericlindersson/ha-cpu-power.git
cd ha-cpu-power
sudo sh install.sh
```

The installer:

1. checks the system and installs Python venv support if it's missing
2. installs the program to `/opt/ha-cpu-power` in its own virtual environment
3. runs the setup wizard, which
   - detects driver, frequency range, turbo, EPP, RAPL and temperature sensor
   - tests whether the power limit can really be changed
   - asks for the MQTT broker and tests the connection
   - generates Eco / Balanced / Max profiles for your CPU
   - writes `/etc/ha-cpu-power.conf` (readable by root only)
4. installs and starts the `ha-cpu-power` systemd service

The device then shows up in Home Assistant under *Settings → Devices & services → MQTT*.

**Update:** `git pull && sudo sh install.sh` (keeps your config, restarts the service).
**Uninstall:** `sudo sh install.sh --uninstall`.

## Usage

```sh
sudo /opt/ha-cpu-power/venv/bin/python /opt/ha-cpu-power/ha_cpu_power.py --status   # what is detected
sudo /opt/ha-cpu-power/venv/bin/python /opt/ha-cpu-power/ha_cpu_power.py --setup    # run the wizard again
journalctl -u ha-cpu-power -f                                                        # log
```

`--status` doesn't connect to MQTT. It briefly tests turbo and the power limit and puts them back.

## Configuration

`/etc/ha-cpu-power.conf` is written by the wizard and can be edited by hand
(see [`ha-cpu-power.conf.example`](ha-cpu-power.conf.example)). Restart the service after editing.

```ini
[profile Eco]
max_freq = 1900        # MHz, 0 = hardware max
turbo = off
power_limit = 65       # W, ignored if the limit isn't controllable
governor = schedutil
epp = power            # only on CPUs with energy performance preference
```

Useful `[general]` options:

| Option | Default | |
|---|---|---|
| `language` | `en` | `en` or `sv` entity names |
| `startup_profile` | | Profile applied when the service starts |
| `restore_on_exit` | `true` | Restore original settings on stop |
| `power_limit_control` | `auto` | `auto` tests the RAPL limit at start, `on` / `off` force it |
| `power_scale` | `1.0` | Multiply power readings, e.g. to calibrate against a smart plug |
| `temp_sensor` | auto | Path to a `temp*_input` file to override detection |
| `interval` | `5` | Seconds between updates |

## Home Assistant automation

[`ha_automation.yaml`](ha_automation.yaml) picks a profile from the electricity price.
Replace the price sensor and the select entity with your own.

## MQTT topics

| Topic | |
|---|---|
| `cpu_power/<node>/state` | JSON with all values |
| `cpu_power/<node>/availability` | `online` / `offline` |
| `cpu_power/<node>/set/<key>` | `profile`, `max_freq`, `turbo` (ON/OFF), `governor`, `epp`, `power_limit` |

## Troubleshooting

- **CPU stuck at its lowest frequency:** with `intel_cpufreq` or `acpi-cpufreq` the `powersave`
  governor pins the CPU to minimum. Use `schedutil` (the wizard picks the right one).
- **No power limit entity:** the BIOS has locked the RAPL limit, or the CPU doesn't support it (AMD).
- **Power reads implausibly low (< 1 W):** some boards, especially overclocked or Xeons on desktop
  boards, report broken RAPL values. Check with `sudo turbostat --show PkgWatt`. If it agrees, don't
  trust the power sensor or the power limit; use max frequency and turbo, and a smart plug for real watts.
- **No power reading at all:** `sudo modprobe intel_rapl_msr`.
- **Overclocking:** use adaptive or offset voltage and keep C-states enabled, otherwise lowering the
  frequency saves little because the voltage stays high.
- Power values cover the CPU package (and RAM if exposed), not the whole machine.

## License

MIT
