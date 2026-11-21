#!/usr/bin/env python3
"""
ha-cpu-power

Control CPU max frequency, turbo/boost, governor, energy performance preference
and RAPL power limit on a Linux machine, and expose it all as a device in
Home Assistant via MQTT Discovery.

  --status   show what was detected (read-only, no MQTT)
  --setup    interactive wizard that detects the hardware and writes the config
  (none)     run the service

Needs root to change settings and to read RAPL energy counters.
"""
import argparse
import configparser
import getpass
import glob
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:  # --status works without it
    mqtt = None

VERSION = "1.1.0"
SYS = os.environ.get("HCP_SYSFS", "/sys")
PROC = os.environ.get("HCP_PROC", "/proc")
DEFAULT_CONFIG = "/etc/ha-cpu-power.conf"

log = logging.getLogger("ha-cpu-power")

TEXT = {
    "en": {"freq_avg": "Frequency average", "freq_peak": "Frequency peak core", "load": "Load",
           "temp": "Temperature", "power_pkg": "CPU power", "power_dram": "RAM power",
           "max_freq": "Max frequency", "power_limit": "Power limit", "turbo": "Turbo",
           "governor": "Governor", "epp": "Energy preference", "profile": "Profile",
           "custom": "Custom", "profiles": ("Eco", "Balanced", "Max")},
    "sv": {"freq_avg": "Frekvens medel", "freq_peak": "Frekvens högsta kärna", "load": "Belastning",
           "temp": "Temperatur", "power_pkg": "Effekt CPU", "power_dram": "Effekt RAM",
           "max_freq": "Maxfrekvens", "power_limit": "Effekttak", "turbo": "Turbo",
           "governor": "Governor", "epp": "Energiprofil", "profile": "Profil",
           "custom": "Anpassad", "profiles": ("Eco", "Balans", "Max")},
}
CUSTOM_NAMES = {t["custom"] for t in TEXT.values()}


def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, TypeError):
        return default


def wr(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def r100(mhz):
    return int(round(mhz / 100.0) * 100)


# ==========================================================================
# Hardware
# ==========================================================================
class Cpu:
    def __init__(self, power_limit_min=20, power_limit_max=0, power_scale=1.0,
                 power_limit_control="auto", temp_sensor="", probe=True):
        self.cpus = sorted(glob.glob(f"{SYS}/devices/system/cpu/cpu[0-9]*/cpufreq"),
                           key=lambda p: int(re.search(r"cpu(\d+)/cpufreq", p).group(1)))
        # No cpufreq: frequency control is off (BIOS/BMC-controlled power management,
        # or a VM). Power reading, RAPL power limit and temperature can still work.
        self.has_cpufreq = bool(self.cpus)
        c0 = self.cpus[0] if self.cpus else "/nonexistent"
        self.driver = rd(f"{c0}/scaling_driver", "none")
        self.governors = (rd(f"{c0}/scaling_available_governors", "") or "").split()
        self.is_root = os.geteuid() == 0
        self.power_scale = power_scale or 1.0

        # Turbo / boost
        nt = f"{SYS}/devices/system/cpu/intel_pstate/no_turbo"
        bo = f"{SYS}/devices/system/cpu/cpufreq/boost"
        if os.path.exists(nt):
            self.turbo_path, self.turbo_inv = nt, True
        elif os.path.exists(bo):
            self.turbo_path, self.turbo_inv = bo, False
        else:
            self.turbo_path, self.turbo_inv = None, False

        # Frequency ranges (per CPU, hybrid CPUs have different ones)
        self.max_noturbo = max(self._cpuinfo_max(), default=None)
        if not self.has_cpufreq:
            maxes = []
        elif probe and self.turbo_path and self.is_root and self.turbo() is False:
            # Some drivers hide turbo frequencies while turbo is off: peek with turbo on
            try:
                self.set_turbo(True, quiet=True)
                time.sleep(0.2)
                maxes = self._cpuinfo_max()
            finally:
                self.set_turbo(False, quiet=True)
        else:
            maxes = self._cpuinfo_max()
        self.ranges = {c: (int(rd(f"{c}/cpuinfo_min_freq", "0")) // 1000, m)
                       for c, m in zip(self.cpus, maxes)}
        self.hw_min = min((r[0] for r in self.ranges.values()), default=None)
        self.hw_max = max((r[1] for r in self.ranges.values()), default=None)
        bf = rd(f"{c0}/base_frequency")
        self.base_freq = int(bf) // 1000 if bf else None

        # Energy performance preference (intel_pstate / amd-pstate active mode)
        self.epp_opts = (rd(f"{c0}/energy_performance_available_preferences", "") or "").split()
        if not os.path.exists(f"{c0}/energy_performance_preference"):
            self.epp_opts = []

        # RAPL
        self.pkgs, self.drams = [], []
        for d in sorted(glob.glob(f"{SYS}/class/powercap/intel-rapl:*")):
            name = rd(f"{d}/name", "")
            if rd(f"{d}/energy_uj") is None:
                continue
            if name.startswith("package"):
                self.pkgs.append(d)
            elif name == "dram":
                self.drams.append(d)
        self._last_energy = {}

        self.pl_ok = False
        self.pl_default = None
        if self.pkgs and os.path.exists(f"{self.pkgs[0]}/constraint_0_power_limit_uw"):
            cur = rd(f"{self.pkgs[0]}/constraint_0_power_limit_uw")
            mx = rd(f"{self.pkgs[0]}/constraint_0_max_power_uw")
            self.pl_default = round(int(mx) / 1e6) if mx and int(mx) > 0 else (
                round(int(cur) / 1e6) if cur else None)
            if power_limit_control == "on":
                self.pl_ok = True
            elif power_limit_control == "auto" and probe and self.is_root:
                self.pl_ok = self.probe_power_limit()
        self.pl_min = power_limit_min
        self.pl_max = power_limit_max or self.pl_default or 150

        self.temp_paths = find_temp_sensors(temp_sensor)
        self._last_stat = None

    def _cpuinfo_max(self):
        return [int(rd(f"{c}/cpuinfo_max_freq", "0")) // 1000 for c in self.cpus]

    # ---- frequency ----
    def freq_cur(self):
        if self.has_cpufreq:
            vals = [int(rd(f"{c}/scaling_cur_freq", "0") or 0) // 1000 for c in self.cpus]
        else:  # /proc/cpuinfo works without cpufreq on x86
            vals = [int(float(line.split(":")[1])) for line in (rd(f"{PROC}/cpuinfo", "") or "").splitlines()
                    if line.startswith("cpu MHz")]
        vals = [v for v in vals if v > 0]
        if not vals:
            return None, None
        return round(sum(vals) / len(vals)), max(vals)

    def max_freq(self):
        if not self.has_cpufreq:
            return None
        return max(int(rd(f"{c}/scaling_max_freq", "0")) // 1000 for c in self.cpus)

    def set_max_freq(self, mhz):
        if not self.has_cpufreq:
            raise ValueError("frequency control not available (no cpufreq)")
        mhz = int(mhz)
        for c, (lo, hi) in self.ranges.items():
            wr(f"{c}/scaling_max_freq", max(lo, min(hi, mhz)) * 1000)
        log.info("Max frequency set to %d MHz", max(self.hw_min, min(self.hw_max, mhz)))

    # ---- governor / EPP ----
    def governor(self):
        return rd(f"{self.cpus[0]}/scaling_governor") if self.cpus else None

    def set_governor(self, gov):
        if gov not in self.governors:
            raise ValueError(f"governor '{gov}' not available ({', '.join(self.governors)})")
        for c in self.cpus:
            wr(f"{c}/scaling_governor", gov)
        log.info("Governor set to %s", gov)

    def epp(self):
        return rd(f"{self.cpus[0]}/energy_performance_preference") if self.epp_opts else None

    def set_epp(self, value):
        if value not in self.epp_opts:
            raise ValueError(f"energy preference '{value}' not available ({', '.join(self.epp_opts)})")
        for c in self.cpus:
            wr(f"{c}/energy_performance_preference", value)
        log.info("Energy preference set to %s", value)

    # ---- turbo ----
    def turbo(self):
        if not self.turbo_path:
            return None
        v = rd(self.turbo_path) == "1"
        return (not v) if self.turbo_inv else v

    def set_turbo(self, on, quiet=False):
        if not self.turbo_path:
            return
        wr(self.turbo_path, ((0 if on else 1) if self.turbo_inv else (1 if on else 0)))
        if not quiet:
            log.info("Turbo %s", "on" if on else "off")

    # ---- RAPL ----
    def _power(self, domains):
        if not domains:
            return None
        now = time.monotonic()
        total, have = 0.0, False
        for d in domains:
            raw = rd(f"{d}/energy_uj")
            if raw is None:
                continue
            e = int(raw)
            last = self._last_energy.get(d)
            self._last_energy[d] = (e, now)
            if last is None:
                continue
            le, lt = last
            if e < le:  # counter wrapped
                e += int(rd(f"{d}/max_energy_range_uj", "0") or 0)
            if now > lt:
                total += (e - le) / 1e6 / (now - lt)
                have = True
        return round(total * self.power_scale, 1) if have else None

    def power_pkg(self):
        return self._power(self.pkgs)

    def power_dram(self):
        return self._power(self.drams)

    def power_limit(self):
        if not self.pkgs:
            return None
        v = rd(f"{self.pkgs[0]}/constraint_0_power_limit_uw")
        return round(int(v) / 1e6) if v else None

    def set_power_limit(self, watts):
        watts = max(self.pl_min, min(self.pl_max, int(watts)))
        for p in self.pkgs:
            wr(f"{p}/constraint_0_power_limit_uw", watts * 1_000_000)
            if rd(f"{p}/enabled") == "0":
                try:
                    wr(f"{p}/enabled", 1)
                except OSError:
                    pass
        log.info("Power limit set to %d W", watts)

    def probe_power_limit(self):
        """Write a slightly lower limit, read it back, restore. True if the CPU accepts it."""
        p = f"{self.pkgs[0]}/constraint_0_power_limit_uw"
        cur = rd(p)
        if not cur or int(cur) <= 2_000_000:
            return False
        cur = int(cur)
        test = cur - 1_000_000
        try:
            wr(p, test)
            return abs(int(rd(p, "0")) - test) < 250_000
        except OSError:
            return False
        finally:
            try:
                wr(p, cur)
            except OSError:
                pass

    # ---- misc ----
    def temp(self):
        vals = [int(v) / 1000 for v in (rd(p) for p in self.temp_paths) if v and v.lstrip("-").isdigit()]
        return round(max(vals), 1) if vals else None

    def load(self):
        line = (rd(f"{PROC}/stat", "") or "").splitlines()
        if not line:
            return None
        vals = list(map(int, line[0].split()[1:]))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        last, self._last_stat = self._last_stat, (idle, total)
        if not last or total == last[1]:
            return None
        return round(100 * (1 - (idle - last[0]) / (total - last[1])), 1)

    def pick_governor(self):
        """A governor that scales with load for this driver."""
        if self.driver in ("intel_pstate", "amd-pstate-epp") and "powersave" in self.governors:
            return "powersave"  # active mode: powersave is dynamic
        for g in ("schedutil", "ondemand", "conservative"):
            if g in self.governors:
                return g
        return self.governor()

    # ---- save / restore ----
    def snapshot(self):
        s = {"freq": [rd(f"{c}/scaling_max_freq") for c in self.cpus],
             "gov": self.governor(), "turbo": self.turbo(), "epp": self.epp()}
        if self.pkgs:
            s["pl"] = [rd(f"{p}/constraint_0_power_limit_uw") for p in self.pkgs]
        return s

    def restore(self, s):
        if s.get("gov"):
            for c in self.cpus:
                _safe(wr, f"{c}/scaling_governor", s["gov"])
        if s.get("epp"):
            for c in self.cpus:
                _safe(wr, f"{c}/energy_performance_preference", s["epp"])
        if s.get("turbo") is not None:
            _safe(self.set_turbo, s["turbo"], True)
        for c, v in zip(self.cpus, s["freq"]):
            if v:
                _safe(wr, f"{c}/scaling_max_freq", v)
        if self.pl_ok:
            for p, v in zip(self.pkgs, s.get("pl", [])):
                if v:
                    _safe(wr, f"{p}/constraint_0_power_limit_uw", v)
        log.info("Original settings restored")


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        log.warning("Restore failed: %s", e)


NO_CPUFREQ_HELP = """\
No cpufreq driver is loaded, so frequency, turbo and governor can't be controlled.
On a physical server this almost always means the BIOS handles power management itself.
To get full control, set it to OS control in the BIOS/UEFI and reboot:
  Dell PowerEdge   System Profile Settings > Custom, CPU Power Management: OS DBPM
  HPE ProLiant     Power and Performance Options > Power Regulator: OS Control Mode
  Supermicro       CPU Configuration > Advanced Power Management: Power Technology Custom,
                   EIST (P-states) Enable, Hardware P-States: Disable or Native
  Lenovo           Operating Modes > Custom, CPU P-state Control: Legacy/Autonomous
Then check: ls /sys/devices/system/cpu/cpu0/cpufreq
Until then the power limit (RAPL) can still be used if the CPU accepts it.
"""


def find_temp_sensors(override=""):
    if override:
        return [override] if os.path.exists(override) else []
    hwmons = {}
    for hw in glob.glob(f"{SYS}/class/hwmon/hwmon*"):
        hwmons.setdefault(rd(f"{hw}/name", ""), []).append(hw)
    # Intel: one "Package id N" per socket
    paths = [lbl[:-6] + "_input" for hw in hwmons.get("coretemp", [])
             for lbl in glob.glob(f"{hw}/temp*_label") if (rd(lbl, "") or "").startswith("Package")]
    if paths:
        return paths
    # AMD: Tdie (real) preferred over Tctl (may be offset)
    for name in ("zenpower", "k10temp"):
        for want in ("Tdie", "Tctl"):
            paths = [lbl[:-6] + "_input" for hw in hwmons.get(name, [])
                     for lbl in glob.glob(f"{hw}/temp*_label") if rd(lbl) == want]
            if paths:
                return paths
        paths = [f"{hw}/temp1_input" for hw in hwmons.get(name, []) if os.path.exists(f"{hw}/temp1_input")]
        if paths:
            return paths
    # Generic thermal zones (ARM SoCs, some laptops)
    wanted = ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "cpu0-thermal", "soc_thermal",
              "soc-thermal", "cpu", "acpitz")
    zones = {rd(f"{z}/type", ""): f"{z}/temp" for z in glob.glob(f"{SYS}/class/thermal/thermal_zone*")}
    for w in wanted:
        if w in zones:
            return [zones[w]]
    return []


def is_virtual():
    if os.path.exists(f"{SYS}/hypervisor/type"):
        return True
    for line in (rd(f"{PROC}/cpuinfo", "") or "").splitlines():
        if line.startswith("flags"):
            return " hypervisor" in line
    return False


def cpu_model():
    for line in (rd(f"{PROC}/cpuinfo", "") or "").splitlines():
        if line.split(":")[0].strip() in ("model name", "Model", "Hardware"):
            return line.split(":", 1)[1].strip()
    return "CPU"


# ==========================================================================
# Config
# ==========================================================================
def read_config(path):
    cfg = configparser.ConfigParser()
    cfg.read_dict({"general": {}, "mqtt": {}, "homeassistant": {}})
    found = cfg.read(path) if path else []
    return cfg, bool(found)


def load_profiles(cfg):
    profiles = {}
    for sec in cfg.sections():
        if sec.lower().startswith("profile "):
            p = cfg[sec]
            profiles[sec[8:].strip()] = {
                "max_freq": p.getint("max_freq", fallback=None),
                "turbo": p.getboolean("turbo", fallback=None),
                "power_limit": p.getint("power_limit", fallback=None),
                "governor": p.get("governor", fallback="").strip() or None,
                "epp": p.get("epp", fallback="").strip() or None,
            }
    return profiles


def cpu_from_config(cfg, probe=True):
    g = cfg["general"]
    return Cpu(power_limit_min=g.getint("power_limit_min", 20),
               power_limit_max=g.getint("power_limit_max", 0),
               power_scale=g.getfloat("power_scale", 1.0),
               power_limit_control=g.get("power_limit_control", "auto").strip().lower(),
               temp_sensor=g.get("temp_sensor", "").strip(),
               probe=probe)


def make_client(client_id):
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:  # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id)


def rc_failed(rc):
    if hasattr(rc, "is_failure"):
        return rc.is_failure
    return rc != 0


# ==========================================================================
# MQTT / Home Assistant
# ==========================================================================
class Bridge:
    def __init__(self, cfg, cpu):
        self.cfg, self.cpu = cfg, cpu
        m, ha, g = cfg["mqtt"], cfg["homeassistant"], cfg["general"]
        self.t = TEXT.get(g.get("language", "en"), TEXT["en"])
        self.node = re.sub(r"[^A-Za-z0-9_-]", "_", ha.get("node_id", "").strip() or socket.gethostname())
        self.dev_name = ha.get("device_name", "Server CPU")
        self.disc = ha.get("discovery_prefix", "homeassistant")
        self.base = f"cpu_power/{self.node}"
        self.interval = g.getfloat("interval", 5)
        self.profiles = load_profiles(cfg)
        self.profile = self.t["custom"]
        self.lock = threading.Lock()

        self.c = make_client(f"ha-cpu-power-{self.node}")
        if m.get("username"):
            self.c.username_pw_set(m.get("username"), m.get("password", ""))
        if m.getboolean("tls", False):
            self.c.tls_set()
        self.c.will_set(f"{self.base}/availability", "offline", retain=True)
        self.c.on_connect = self.on_connect
        self.c.on_message = self.on_message
        self.c.reconnect_delay_set(1, 60)
        self.host, self.port = m.get("host", "localhost"), m.getint("port", 1883)

    def device(self):
        return {"identifiers": [f"cpu_power_{self.node}"], "name": self.dev_name,
                "manufacturer": "ha-cpu-power", "model": cpu_model(),
                "sw_version": f"{VERSION} ({self.cpu.driver})"}

    def publish_discovery(self):
        st, t = f"{self.base}/state", self.t
        common = {"availability_topic": f"{self.base}/availability", "device": self.device(),
                  "state_topic": st}

        def ent(comp, key, **kw):
            payload = {"name": t[key], "unique_id": f"cpu_power_{self.node}_{key}", **common, **kw}
            self.c.publish(f"{self.disc}/{comp}/{self.node}/{key}/config", json.dumps(payload), retain=True)

        def remove(comp, key):
            self.c.publish(f"{self.disc}/{comp}/{self.node}/{key}/config", "", retain=True)

        def sensor(key, unit, dc=None, icon=None):
            kw = dict(value_template=f"{{{{ value_json.{key} }}}}", unit_of_measurement=unit,
                      state_class="measurement")
            if dc:
                kw["device_class"] = dc
            if icon:
                kw["icon"] = icon
            ent("sensor", key, **kw)

        sensor("freq_avg", "MHz", "frequency")
        sensor("freq_peak", "MHz", "frequency")
        sensor("load", "%", icon="mdi:cpu-64-bit")
        sensor("temp", "°C", "temperature") if self.cpu.temp_paths else remove("sensor", "temp")
        sensor("power_pkg", "W", "power") if self.cpu.pkgs else remove("sensor", "power_pkg")
        sensor("power_dram", "W", "power") if self.cpu.drams else remove("sensor", "power_dram")

        if self.cpu.has_cpufreq:
            ent("number", "max_freq", value_template="{{ value_json.max_freq }}",
                command_topic=f"{self.base}/set/max_freq", min=self.cpu.hw_min, max=self.cpu.hw_max,
                step=100, unit_of_measurement="MHz", mode="slider", icon="mdi:speedometer")
        else:
            remove("number", "max_freq")
        if self.cpu.pl_ok:
            ent("number", "power_limit", value_template="{{ value_json.power_limit }}",
                command_topic=f"{self.base}/set/power_limit", min=self.cpu.pl_min, max=self.cpu.pl_max,
                step=5, unit_of_measurement="W", mode="slider", icon="mdi:flash")
        else:
            remove("number", "power_limit")
        if self.cpu.turbo_path:
            ent("switch", "turbo", value_template="{{ 'ON' if value_json.turbo else 'OFF' }}",
                command_topic=f"{self.base}/set/turbo", icon="mdi:rocket-launch")
        else:
            remove("switch", "turbo")
        if not self.cpu.governors:
            remove("select", "governor")
        else:
            ent("select", "governor", value_template="{{ value_json.governor }}",
                command_topic=f"{self.base}/set/governor", options=self.cpu.governors, icon="mdi:tune")
        if self.cpu.epp_opts:
            ent("select", "epp", value_template="{{ value_json.epp }}",
                command_topic=f"{self.base}/set/epp", options=self.cpu.epp_opts, icon="mdi:leaf-circle")
        else:
            remove("select", "epp")
        if self.profiles:
            ent("select", "profile", value_template="{{ value_json.profile }}",
                command_topic=f"{self.base}/set/profile", options=list(self.profiles) + [t["custom"]],
                icon="mdi:leaf")
        else:
            remove("select", "profile")
        log.info("Discovery published (node %s)", self.node)

    def on_connect(self, client, userdata, flags, rc, *args):
        if rc_failed(rc):
            log.error("MQTT connection refused: %s", rc)
            return
        log.info("Connected to MQTT %s:%s", self.host, self.port)
        client.subscribe(f"{self.base}/set/#")
        client.subscribe(f"{self.disc}/status")
        self.publish_discovery()
        client.publish(f"{self.base}/availability", "online", retain=True)
        self.publish_state()

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore").strip()
        if msg.topic == f"{self.disc}/status":
            if payload == "online":  # Home Assistant restarted
                self.publish_discovery()
                client.publish(f"{self.base}/availability", "online", retain=True)
                self.publish_state()
            return
        key = msg.topic.rsplit("/", 1)[-1]
        try:
            with self.lock:
                if key == "profile":
                    self.apply_profile(payload)
                else:
                    {"max_freq": lambda: self.cpu.set_max_freq(float(payload)),
                     "power_limit": lambda: self.cpu.set_power_limit(float(payload)),
                     "turbo": lambda: self.cpu.set_turbo(payload.upper() in ("ON", "1", "TRUE")),
                     "governor": lambda: self.cpu.set_governor(payload),
                     "epp": lambda: self.cpu.set_epp(payload)}[key]()
                    self.profile = self.t["custom"]
        except KeyError:
            log.warning("Unknown command topic %s", msg.topic)
        except Exception as e:  # noqa: BLE001
            log.error("Could not set %s=%s: %s", key, payload, e)
        self.publish_state()

    def apply_profile(self, name):
        if name in CUSTOM_NAMES:
            self.profile = self.t["custom"]
            return
        p = self.profiles.get(name)
        if p is None:
            raise ValueError(f"unknown profile '{name}'")
        steps = []
        if p["governor"]:
            steps.append(("governor", lambda: self.cpu.set_governor(p["governor"])))
        if p["epp"] and self.cpu.epp_opts:
            steps.append(("epp", lambda: self.cpu.set_epp(p["epp"])))
        if p["turbo"] is not None and self.cpu.turbo_path:
            steps.append(("turbo", lambda: self.cpu.set_turbo(p["turbo"])))
        if p["max_freq"] is not None and self.cpu.has_cpufreq:
            steps.append(("max_freq", lambda: self.cpu.set_max_freq(p["max_freq"] or self.cpu.hw_max)))
        if p["power_limit"] is not None and self.cpu.pl_ok:
            steps.append(("power_limit", lambda: self.cpu.set_power_limit(p["power_limit"])))
        for what, fn in steps:  # one failing step must not stop the rest
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log.warning("Profile %s: could not set %s: %s", name, what, e)
        self.profile = name
        log.info("Profile '%s' applied", name)

    def read_state(self):
        avg, peak = self.cpu.freq_cur()
        return {"freq_avg": avg, "freq_peak": peak, "max_freq": self.cpu.max_freq(),
                "load": self.cpu.load(), "temp": self.cpu.temp(),
                "power_pkg": self.cpu.power_pkg(), "power_dram": self.cpu.power_dram(),
                "power_limit": self.cpu.power_limit(), "turbo": self.cpu.turbo(),
                "governor": self.cpu.governor(), "epp": self.cpu.epp(), "profile": self.profile}

    def publish_state(self):
        with self.lock:
            s = self.read_state()
        self.c.publish(f"{self.base}/state", json.dumps(s))
        return s

    def run(self, stop):
        start = self.cfg["general"].get("startup_profile", "").strip()
        if start:
            try:
                with self.lock:
                    self.apply_profile(start)
            except Exception as e:  # noqa: BLE001
                log.error("Startup profile: %s", e)
        self.c.connect_async(self.host, self.port, 60)
        self.c.loop_start()
        while not stop.wait(self.interval):
            if self.c.is_connected():
                self.publish_state()
        if self.c.is_connected():
            self.c.publish(f"{self.base}/availability", "offline", retain=True).wait_for_publish(2)
        self.c.loop_stop()
        self.c.disconnect()


# ==========================================================================
# Status
# ==========================================================================
def print_status(cpu, cfg):
    cpu.power_pkg(); cpu.power_dram(); cpu.load()
    time.sleep(1)
    avg, peak = cpu.freq_cur()
    yn = lambda b: "yes" if b else "no"  # noqa: E731
    print(f"CPU:            {cpu_model()}")
    print(f"Virtualized:    {yn(is_virtual())}")
    print(f"Driver:         {cpu.driver}")
    if cpu.has_cpufreq:
        print(f"Frequency:      {cpu.hw_min}-{cpu.hw_max} MHz"
              + (f" (base {cpu.base_freq} MHz)" if cpu.base_freq else f" (max without turbo {cpu.max_noturbo} MHz)")
              + f", limit now {cpu.max_freq()} MHz")
    else:
        print("Frequency:      no cpufreq driver, frequency control not available")
    print(f"Current:        avg {avg} MHz, peak {peak} MHz")
    if cpu.governors:
        print(f"Governors:      {', '.join(cpu.governors)} (active: {cpu.governor()}, recommended: {cpu.pick_governor()})")
    print(f"Energy pref.:   {', '.join(cpu.epp_opts) + f' (active: {cpu.epp()})' if cpu.epp_opts else 'not available'}")
    print(f"Turbo:          {'not available' if cpu.turbo_path is None else ('on' if cpu.turbo() else 'off')}")
    print(f"RAPL:           {'package ' + str(len(cpu.pkgs)) + (', dram' if cpu.drams else '') if cpu.pkgs else 'not available'}")
    pw = cpu.power_pkg()
    print(f"Power:          CPU {pw} W, RAM {cpu.power_dram()} W")
    if pw is not None and pw < 1:
        print("                ! CPU power looks implausibly low; RAPL readings may be broken on this board")
    pl = "not available" if cpu.power_limit() is None else f"{cpu.power_limit()} W, controllable: {yn(cpu.pl_ok)}"
    if cpu.pkgs and not cpu.is_root:
        pl += " (run as root to test)"
    print(f"Power limit:    {pl}")
    print(f"Temperature:    {cpu.temp()} °C ({', '.join(cpu.temp_paths) or 'no sensor found'})")
    print(f"Load:           {cpu.load()} %")
    print(f"Profiles:       {', '.join(load_profiles(cfg)) or '-'}")


# ==========================================================================
# Setup wizard
# ==========================================================================
def ask(prompt, default=""):
    shown = f" [{default}]" if default not in ("", None) else ""
    try:
        v = input(f"{prompt}{shown}: ").strip()
    except EOFError:
        print()
        v = ""
    return v if v else str(default)


def ask_yn(prompt, default=True):
    v = ask(prompt + (" (Y/n)" if default else " (y/N)"), "").lower()
    return default if not v else v.startswith(("y", "j"))


def ask_int(prompt, default):
    while True:
        v = ask(prompt, default)
        try:
            return int(float(v))
        except ValueError:
            print("  Please enter a number.")


def test_mqtt(host, port, user, password, tls):
    if mqtt is None:
        return "paho-mqtt is not installed"
    done, result = threading.Event(), {}
    c = make_client(f"ha-cpu-power-setup-{os.getpid()}")
    if user:
        c.username_pw_set(user, password)
    if tls:
        c.tls_set()

    def on_connect(client, userdata, flags, rc, *a):
        result["rc"] = rc
        done.set()
    c.on_connect = on_connect
    try:
        c.connect(host, port, 10)
    except Exception as e:  # noqa: BLE001
        return str(e)
    c.loop_start()
    done.wait(8)
    c.loop_stop()
    try:
        c.disconnect()
    except Exception:  # noqa: BLE001
        pass
    if "rc" not in result:
        return "no answer from broker"
    return f"broker refused: {result['rc']}" if rc_failed(result["rc"]) else None


def make_profiles(cpu, lang, tdp):
    eco, bal, mx = TEXT[lang]["profiles"]
    if not cpu.has_cpufreq:  # only the power limit can be controlled
        if not tdp:
            return {}
        return {eco: {"power_limit": round(tdp * 0.45 / 5) * 5}, bal: {"power_limit": round(tdp * 0.7 / 5) * 5},
                mx: {"power_limit": tdp}}
    gov = cpu.pick_governor()
    base = cpu.base_freq or cpu.max_noturbo or cpu.hw_max
    if base >= cpu.hw_max and cpu.turbo_path is None:  # no turbo: spread over the range
        base = r100(cpu.hw_min + (cpu.hw_max - cpu.hw_min) * 0.75)
    eco_f = max(cpu.hw_min, r100(cpu.hw_min + (base - cpu.hw_min) * 0.4))

    def epp(*prefs):
        return next((p for p in prefs if p in cpu.epp_opts), None)

    profiles = {
        eco: {"max_freq": eco_f, "turbo": False, "power_limit": round(tdp * 0.45 / 5) * 5 if tdp else None,
              "governor": gov, "epp": epp("power", "balance_power")},
        bal: {"max_freq": r100(base), "turbo": False, "power_limit": round(tdp * 0.7 / 5) * 5 if tdp else None,
              "governor": gov, "epp": epp("balance_power", "balance_performance")},
        mx: {"max_freq": 0, "turbo": True, "power_limit": tdp or None,
             "governor": gov, "epp": epp("balance_performance", "performance")},
    }
    return profiles


def render_config(v, profiles, cpu):
    b = lambda x: "on" if x else "off"  # noqa: E731
    lines = [
        "# ha-cpu-power configuration (generated by --setup, safe to edit)",
        f"# CPU: {cpu_model()}  driver: {cpu.driver}",
        (f"# Frequency range {cpu.hw_min}-{cpu.hw_max} MHz" + (f", base {cpu.base_freq} MHz" if cpu.base_freq else ""))
        if cpu.has_cpufreq else "# No cpufreq driver: frequency control not available",
        "",
        "[general]",
        "# Seconds between updates to Home Assistant",
        f"interval = {v['interval']}",
        "# Entity names in Home Assistant: en or sv",
        f"language = {v['language']}",
        "# Profile applied when the service starts (empty = leave settings alone)",
        f"startup_profile = {v['startup_profile']}",
        "# Put the original settings back when the service stops",
        f"restore_on_exit = {'true' if v['restore_on_exit'] else 'false'}",
        "# RAPL power limit control: auto (test at start), on, off",
        f"power_limit_control = {v['power_limit_control']}",
        f"power_limit_min = {v['power_limit_min']}",
        f"power_limit_max = {v['power_limit_max']}",
        "# Multiply RAPL power readings, e.g. to calibrate against a smart plug",
        f"power_scale = {v['power_scale']}",
        "# Temperature input file, empty = auto-detect",
        f"temp_sensor = {v['temp_sensor']}",
        "",
        "[mqtt]",
        f"host = {v['host']}",
        f"port = {v['port']}",
        f"username = {v['username']}",
        f"password = {v['password']}",
        f"tls = {'true' if v['tls'] else 'false'}",
        "",
        "[homeassistant]",
        f"device_name = {v['device_name']}",
        f"node_id = {v['node_id']}",
        "discovery_prefix = homeassistant",
        "",
        "# Profiles appear as a select entity in Home Assistant.",
        "#   max_freq     MHz, 0 = hardware maximum",
        "#   turbo        on / off",
        "#   power_limit  W (only used if the power limit is controllable)",
        "#   governor     " + ", ".join(cpu.governors),
    ]
    if cpu.epp_opts:
        lines.append("#   epp          " + ", ".join(cpu.epp_opts))
    for name, p in profiles.items():
        lines += ["", f"[profile {name}]"]
        if p.get("max_freq") is not None and cpu.has_cpufreq:
            lines.append(f"max_freq = {p['max_freq']}")
        if p.get("turbo") is not None and cpu.turbo_path:
            lines.append(f"turbo = {b(p['turbo'])}")
        if p.get("power_limit") and cpu.pkgs:
            lines.append(f"power_limit = {p['power_limit']}")
        if p.get("governor"):
            lines.append(f"governor = {p['governor']}")
        if p.get("epp") and cpu.epp_opts:
            lines.append(f"epp = {p['epp']}")
    return "\n".join(lines) + "\n"


def setup(path, defaults_from=None):
    print(f"\n=== ha-cpu-power {VERSION} setup ===\n")
    old, _ = read_config(defaults_from or path)
    og, om, oh = old["general"], old["mqtt"], old["homeassistant"]
    if os.geteuid() != 0:
        print("Note: not running as root, so the power limit can't be tested and the config can't\n"
              "      be written to /etc. Run with sudo for a complete setup.\n")

    print("Detecting hardware ...")
    try:
        cpu = Cpu(power_limit_control="auto", temp_sensor=og.get("temp_sensor", "").strip())
    except RuntimeError as e:
        print(f"\n{e}")
        return 1
    print_status(cpu, configparser.ConfigParser())
    print()
    if not cpu.has_cpufreq:
        print(NO_CPUFREQ_HELP)
        if not cpu.pl_ok:
            print("Nothing can be controlled right now, only monitored (power, temperature, load).\n")
    if is_virtual() and not ask_yn("This looks like a virtual machine; settings may have no effect. Continue?", False):
        return 1

    v = {}
    print("\n--- Home Assistant ---")
    lang = ask("Language for entity names (en/sv)", og.get("language", "en")).lower()
    v["language"] = lang if lang in TEXT else "en"
    v["device_name"] = ask("Device name", oh.get("device_name", "Server CPU"))
    v["node_id"] = ask("Node id (unique per server)", oh.get("node_id", "") or socket.gethostname())

    print("\n--- MQTT broker ---")
    while True:
        v["host"] = ask("Host", om.get("host", "localhost"))
        v["port"] = ask_int("Port", om.get("port", "1883"))
        v["username"] = ask("Username (empty = none)", om.get("username", ""))
        v["password"] = om.get("password", "")
        if v["username"]:
            pw = getpass.getpass("Password" + (" [keep existing]" if v["password"] else "") + ": ") \
                if sys.stdin.isatty() else ask("Password", "")
            v["password"] = pw or v["password"]
        v["tls"] = ask_yn("Use TLS?", om.getboolean("tls", False))
        print("Testing connection ...", end=" ", flush=True)
        err = test_mqtt(v["host"], v["port"], v["username"], v["password"], v["tls"])
        if err is None:
            print("OK")
            break
        print(f"failed: {err}")
        if not ask_yn("Try again?", True):
            break

    print("\n--- Power ---")
    v["power_limit_control"] = "auto" if cpu.pl_ok else ("off" if cpu.has_cpufreq else "auto")
    tdp = None
    if cpu.pl_ok:
        tdp = ask_int("Highest power limit you want to allow (W per CPU socket), used by the Max profile",
                      og.get("power_limit_max", "") or cpu.pl_default or 100)
    elif cpu.pkgs:
        print("The RAPL power limit is not controllable here (locked by BIOS/BMC or unsupported)."
              + ("\nProfiles will use max frequency and turbo instead." if cpu.has_cpufreq else ""))
    v["power_limit_min"] = og.get("power_limit_min", "20")
    v["power_limit_max"] = tdp or og.get("power_limit_max", "") or (cpu.pl_default or 0)
    v["power_scale"] = og.get("power_scale", "1.0")
    v["temp_sensor"] = og.get("temp_sensor", "")

    print("\n--- Profiles ---")
    old_profiles = load_profiles(old)
    profiles = make_profiles(cpu, v["language"], tdp)
    if old_profiles and not ask_yn("Replace existing profiles with ones generated for this hardware?", True):
        profiles = old_profiles
    for name, p in profiles.items():
        bits = [f"max {p['max_freq'] or cpu.hw_max} MHz"] if cpu.has_cpufreq and p.get("max_freq") is not None else []
        if cpu.turbo_path and p.get("turbo") is not None:
            bits.append("turbo " + ("on" if p["turbo"] else "off"))
        if p.get("power_limit") and cpu.pl_ok:
            bits.append(f"{p['power_limit']} W")
        if p.get("governor"):
            bits.append(p["governor"])
        if p.get("epp") and cpu.epp_opts:
            bits.append(p["epp"])
        print(f"  {name:9} {', '.join(bits)}")
    names = list(profiles)
    if not names:
        print("  (no profiles, nothing to control)")
    if names:
        default_start = og.get("startup_profile", "") if og.get("startup_profile", "") in names else names[min(1, len(names) - 1)]
        s = ask(f"Profile to apply at start ({'/'.join(names)}, or 'none')", default_start)
        v["startup_profile"] = "" if s.lower() == "none" else (s if s in names else default_start)
    else:
        v["startup_profile"] = ""
    v["restore_on_exit"] = ask_yn("Restore original settings when the service stops?", og.getboolean("restore_on_exit", True))
    v["interval"] = og.get("interval", "5")

    text = render_config(v, profiles, cpu)
    try:
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")
            print(f"\nOld config saved as {path}.bak")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"\nCould not write {path}: {e}\n\n{text}")
        return 1
    print(f"Config written to {path}")
    return 0


# ==========================================================================
def main():
    ap = argparse.ArgumentParser(description="Control CPU frequency/power from Home Assistant via MQTT")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("--status", action="store_true", help="show detected hardware and exit (no MQTT)")
    ap.add_argument("--setup", action="store_true", help="interactive setup wizard that writes the config")
    ap.add_argument("--defaults-from", help="(setup) read default answers from another config file")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if a.setup:
        sys.exit(setup(a.config, a.defaults_from))

    cfg, found = read_config(a.config)
    if a.status:
        try:
            print_status(cpu_from_config(cfg), cfg)
        except RuntimeError as e:
            print(e)
            sys.exit(1)
        return

    if not found:
        log.error("Config %s not found. Create it with --setup", a.config)
        sys.exit(1)
    if mqtt is None:
        log.error("paho-mqtt is not installed")
        sys.exit(1)
    if os.geteuid() != 0:
        log.warning("Not running as root: changing settings will fail")

    cpu = cpu_from_config(cfg)
    log.info("ha-cpu-power %s: driver %s, %d CPUs, %s-%s MHz, turbo %s, power limit %s, temp %s",
             VERSION, cpu.driver, len(cpu.cpus), cpu.hw_min, cpu.hw_max,
             "yes" if cpu.turbo_path else "no", "yes" if cpu.pl_ok else "no",
             "yes" if cpu.temp_paths else "no")
    snap = cpu.snapshot()
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    try:
        Bridge(cfg, cpu).run(stop)
    finally:
        if cfg["general"].getboolean("restore_on_exit", True):
            cpu.restore(snap)


if __name__ == "__main__":
    main()
