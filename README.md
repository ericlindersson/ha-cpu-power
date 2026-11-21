# ha-cpu-power

Control the CPU of a Linux server from Home Assistant – max frequency, turbo, governor and
Intel RAPL power limit – and see frequency, power, temperature and load. Exposed as a
Home Assistant device via MQTT Discovery, so you can automate it, e.g. on the electricity price.

*Svenska: Styr CPU-frekvens och effekt på en Linux-server från Home Assistant, t.ex. efter elpriset.*

## Features

| Entity | Type | What |
|---|---|---|
| Profil | select | Eco / Balans / Max / Anpassad (profiles defined in the config) |
| Maxfrekvens | number | CPU min–max MHz |
| Effekttak | number | RAPL package power limit (W), if writable |
| Turbo | switch | on/off |
| Governor | select | available cpufreq governors |
| Frekvens medel, högsta kärna | sensor | MHz |
| Effekt CPU, Effekt RAM | sensor | W (from RAPL, if available) |
| Temperatur, Belastning | sensor | °C, % |

- Single Python file, only dependency is `paho-mqtt`
- Republishes discovery when Home Assistant restarts
- Restores the original CPU settings when the service stops

## Requirements

- Linux with cpufreq (`intel_pstate`, `intel_cpufreq` or `acpi-cpufreq`)
- Intel CPU for RAPL power reading/limit (`intel_rapl_msr`); frequency and turbo control work without it
- An MQTT broker connected to Home Assistant (e.g. Mosquitto)
- Root (writes to `/sys`)

## Install

```sh
git clone https://github.com/ericlindersson/ha-cpu-power.git
cd ha-cpu-power
sudo sh install.sh
sudo nano /etc/cpu-power-mqtt.conf      # MQTT host, user, password, profiles
sudo systemctl enable --now cpu-power-mqtt
journalctl -u cpu-power-mqtt -f
```

`install.sh` runs `--status` at the end, which shows what was detected without touching anything:

```sh
sudo /opt/cpu-power-mqtt/venv/bin/python /opt/cpu-power-mqtt/cpu_power_mqtt.py --status
```

## Home Assistant automation

See [`ha_automation.yaml`](ha_automation.yaml) for an example that picks a profile from the
Nordpool electricity price. Replace the sensor entity with your own.

## Troubleshooting

- **CPU stuck at minimum frequency:** with the `intel_cpufreq` driver (intel_pstate in passive mode)
  the `powersave` governor pins the CPU to its lowest frequency. Use `schedutil` in the profiles.
- **No power limit entity:** the BIOS has locked the RAPL limit ("Package Power Limit Lock").
- **Power reads implausibly low (e.g. < 1 W):** some boards, especially with overclocking or Xeons on
  desktop boards, report broken RAPL values. Check with `sudo turbostat --show PkgWatt`. If it agrees,
  the RAPL power limit will not work reliably either – use max frequency and turbo instead, and a
  smart plug for real power readings.
- **No power at all:** `sudo modprobe intel_rapl_msr`.
- Power values cover the CPU package (and DRAM if exposed), not the whole server.

## License

MIT
