#!/usr/bin/env python3
"""
cpu-power-mqtt
Styr CPU-frekvens, turbo, governor och RAPL-effekttak på en Linux-server
och exponerar allt som en enhet i Home Assistant via MQTT Discovery.

Kräver root (skriver i /sys) och paho-mqtt.
"""
import argparse
import configparser
import glob
import json
import logging
import os
import signal
import socket
import sys
import threading
import time

import paho.mqtt.client as mqtt

SYS = os.environ.get("CPM_SYSFS", "/sys")
PROC = os.environ.get("CPM_PROC", "/proc")
CUSTOM = "Anpassad"

log = logging.getLogger("cpu-power-mqtt")


def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def wr(path, value):
    with open(path, "w") as f:
        f.write(str(value))


# --------------------------------------------------------------------------
# Hårdvara
# --------------------------------------------------------------------------
class Cpu:
    def __init__(self, power_limit_min, power_limit_max):
        self.cpufreq = sorted(glob.glob(f"{SYS}/devices/system/cpu/cpu[0-9]*/cpufreq"))
        if not self.cpufreq:
            raise RuntimeError("Hittar ingen cpufreq i sysfs – är cpufreq-drivrutinen laddad?")
        c0 = self.cpufreq[0]
        self.hw_min = int(rd(f"{c0}/cpuinfo_min_freq")) // 1000  # MHz
        self.hw_max = int(rd(f"{c0}/cpuinfo_max_freq")) // 1000
        self.driver = rd(f"{c0}/scaling_driver", "?")
        self.governors = (rd(f"{c0}/scaling_available_governors", "") or "").split()

        # Turbo: intel_pstate/no_turbo (1 = av) eller cpufreq/boost (1 = på)
        nt = f"{SYS}/devices/system/cpu/intel_pstate/no_turbo"
        bo = f"{SYS}/devices/system/cpu/cpufreq/boost"
        if os.path.exists(nt):
            self.turbo_path, self.turbo_inv = nt, True
        elif os.path.exists(bo):
            self.turbo_path, self.turbo_inv = bo, False
        else:
            self.turbo_path, self.turbo_inv = None, False

        # RAPL-domäner
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

        self.pl_writable = False
        if self.pkgs:
            p = self.pkgs[0]
            hw_pl_max = rd(f"{p}/constraint_0_max_power_uw")
            self.pl_max = power_limit_max or (int(hw_pl_max) // 1_000_000 if hw_pl_max and int(hw_pl_max) > 0 else 150)
            self.pl_min = power_limit_min
            self.pl_writable = os.access(f"{p}/constraint_0_power_limit_uw", os.W_OK)

        # Temperatur (coretemp, "Package id 0")
        self.temp_path = None
        for hw in glob.glob(f"{SYS}/class/hwmon/hwmon*"):
            if rd(f"{hw}/name") == "coretemp":
                for lbl in glob.glob(f"{hw}/temp*_label"):
                    if rd(lbl, "").startswith("Package"):
                        self.temp_path = lbl.replace("_label", "_input")
                        break
            if self.temp_path:
                break

        self._last_stat = None

    # ---- frekvens ----
    def freq_cur(self):
        vals = [int(rd(f"{c}/scaling_cur_freq", "0")) // 1000 for c in self.cpufreq]
        vals = [v for v in vals if v > 0]
        if not vals:
            return None, None
        return round(sum(vals) / len(vals)), max(vals)

    def max_freq(self):
        return int(rd(f"{self.cpufreq[0]}/scaling_max_freq")) // 1000

    def set_max_freq(self, mhz):
        mhz = max(self.hw_min, min(self.hw_max, int(mhz)))
        for c in self.cpufreq:
            wr(f"{c}/scaling_max_freq", mhz * 1000)
        log.info("Maxfrekvens satt till %d MHz", mhz)

    # ---- governor ----
    def governor(self):
        return rd(f"{self.cpufreq[0]}/scaling_governor")

    def set_governor(self, gov):
        if gov not in self.governors:
            raise ValueError(f"Okänd governor {gov}")
        for c in self.cpufreq:
            wr(f"{c}/scaling_governor", gov)
        log.info("Governor satt till %s", gov)

    # ---- turbo ----
    def turbo(self):
        if not self.turbo_path:
            return None
        v = rd(self.turbo_path) == "1"
        return (not v) if self.turbo_inv else v

    def set_turbo(self, on):
        if not self.turbo_path:
            return
        v = (0 if on else 1) if self.turbo_inv else (1 if on else 0)
        wr(self.turbo_path, v)
        log.info("Turbo %s", "på" if on else "av")

    # ---- RAPL ----
    def _power(self, domains):
        if not domains:
            return None
        now = time.monotonic()
        total, have = 0.0, False
        for d in domains:
            e = int(rd(f"{d}/energy_uj", "0"))
            last = self._last_energy.get(d)
            self._last_energy[d] = (e, now)
            if last is None:
                continue
            le, lt = last
            if e < le:  # räknaren har slagit runt
                e += int(rd(f"{d}/max_energy_range_uj", "0"))
            dt = now - lt
            if dt > 0:
                total += (e - le) / 1e6 / dt
                have = True
        return round(total, 1) if have else None

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
            if os.path.exists(f"{p}/enabled"):
                try:
                    wr(f"{p}/enabled", 1)
                except OSError:
                    pass
        log.info("Effekttak satt till %d W", watts)

    # ---- övrigt ----
    def temp(self):
        v = rd(self.temp_path) if self.temp_path else None
        return round(int(v) / 1000, 1) if v else None

    def load(self):
        line = rd(f"{PROC}/stat", "").splitlines()[0].split()[1:]
        vals = list(map(int, line))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        last, self._last_stat = self._last_stat, (idle, total)
        if not last or total == last[1]:
            return None
        return round(100 * (1 - (idle - last[0]) / (total - last[1])), 1)

    # ---- spara/återställ ----
    def snapshot(self):
        s = {"freq": [rd(f"{c}/scaling_max_freq") for c in self.cpufreq],
             "gov": self.governor(), "turbo": self.turbo()}
        if self.pkgs:
            s["pl"] = [rd(f"{p}/constraint_0_power_limit_uw") for p in self.pkgs]
        return s

    def restore(self, s):
        for c, v in zip(self.cpufreq, s["freq"]):
            _safe(wr, f"{c}/scaling_max_freq", v)
        if s.get("gov"):
            for c in self.cpufreq:
                _safe(wr, f"{c}/scaling_governor", s["gov"])
        if s.get("turbo") is not None:
            _safe(self.set_turbo, s["turbo"])
        for p, v in zip(self.pkgs, s.get("pl", [])):
            if v:
                _safe(wr, f"{p}/constraint_0_power_limit_uw", v)
        log.info("Ursprungliga inställningar återställda")


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        log.warning("Kunde inte återställa: %s", e)


# --------------------------------------------------------------------------
# Profiler
# --------------------------------------------------------------------------
def load_profiles(cfg):
    profiles = {}
    for sec in cfg.sections():
        if sec.lower().startswith("profile "):
            name = sec[8:].strip()
            p = cfg[sec]
            profiles[name] = {
                "max_freq": p.getint("max_freq", fallback=None),
                "turbo": p.getboolean("turbo", fallback=None),
                "power_limit": p.getint("power_limit", fallback=None),
                "governor": p.get("governor", fallback=None),
            }
    return profiles


# --------------------------------------------------------------------------
# MQTT / Home Assistant
# --------------------------------------------------------------------------
class Bridge:
    def __init__(self, cfg, cpu):
        self.cfg, self.cpu = cfg, cpu
        m = cfg["mqtt"]
        ha = cfg["homeassistant"]
        self.node = ha.get("node_id", socket.gethostname()).replace("-", "_").replace(".", "_")
        self.dev_name = ha.get("device_name", "Server CPU")
        self.disc = ha.get("discovery_prefix", "homeassistant")
        self.base = f"cpu_power/{self.node}"
        self.interval = cfg["general"].getfloat("interval", 5)
        self.profiles = load_profiles(cfg)
        self.profile = CUSTOM
        self.lock = threading.Lock()

        try:
            self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"cpu-power-{self.node}")
        except AttributeError:  # paho-mqtt 1.x
            self.c = mqtt.Client(client_id=f"cpu-power-{self.node}")
        if m.get("username"):
            self.c.username_pw_set(m.get("username"), m.get("password", ""))
        if m.getboolean("tls", False):
            self.c.tls_set()
        self.c.will_set(f"{self.base}/availability", "offline", retain=True)
        self.c.on_connect = self.on_connect
        self.c.on_message = self.on_message
        self.c.reconnect_delay_set(1, 60)
        self.host, self.port = m.get("host", "localhost"), m.getint("port", 1883)

    # ---- discovery ----
    def device(self):
        model = "CPU"
        for line in (rd(f"{PROC}/cpuinfo", "") or "").splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
        return {"identifiers": [f"cpu_power_{self.node}"], "name": self.dev_name,
                "manufacturer": "cpu-power-mqtt", "model": model, "sw_version": self.cpu.driver}

    def publish_discovery(self):
        st = f"{self.base}/state"
        common = {"availability_topic": f"{self.base}/availability", "device": self.device()}

        def ent(comp, key, **kw):
            payload = {"name": kw.pop("name"), "unique_id": f"cpu_power_{self.node}_{key}", **common, **kw}
            self.c.publish(f"{self.disc}/{comp}/{self.node}/{key}/config", json.dumps(payload), retain=True)

        def sensor(key, name, unit, dc=None, icon=None, sc="measurement"):
            kw = dict(name=name, state_topic=st, value_template=f"{{{{ value_json.{key} }}}}",
                      unit_of_measurement=unit, state_class=sc)
            if dc:
                kw["device_class"] = dc
            if icon:
                kw["icon"] = icon
            ent("sensor", key, **kw)

        sensor("freq_avg", "Frekvens medel", "MHz", "frequency")
        sensor("freq_peak", "Frekvens högsta kärna", "MHz", "frequency")
        sensor("load", "Belastning", "%", icon="mdi:cpu-64-bit")
        if self.cpu.temp_path:
            sensor("temp", "Temperatur", "°C", "temperature")
        if self.cpu.pkgs:
            sensor("power_pkg", "Effekt CPU", "W", "power")
        if self.cpu.drams:
            sensor("power_dram", "Effekt RAM", "W", "power")

        ent("number", "max_freq", name="Maxfrekvens", state_topic=st,
            value_template="{{ value_json.max_freq }}", command_topic=f"{self.base}/set/max_freq",
            min=self.cpu.hw_min, max=self.cpu.hw_max, step=100, unit_of_measurement="MHz",
            mode="slider", icon="mdi:speedometer")
        if self.cpu.pkgs and self.cpu.pl_writable:
            ent("number", "power_limit", name="Effekttak", state_topic=st,
                value_template="{{ value_json.power_limit }}", command_topic=f"{self.base}/set/power_limit",
                min=self.cpu.pl_min, max=self.cpu.pl_max, step=5, unit_of_measurement="W",
                mode="slider", icon="mdi:flash")
        if self.cpu.turbo_path:
            ent("switch", "turbo", name="Turbo", state_topic=st,
                value_template="{{ 'ON' if value_json.turbo else 'OFF' }}",
                command_topic=f"{self.base}/set/turbo", icon="mdi:rocket-launch")
        if self.cpu.governors:
            ent("select", "governor", name="Governor", state_topic=st,
                value_template="{{ value_json.governor }}", command_topic=f"{self.base}/set/governor",
                options=self.cpu.governors, icon="mdi:tune")
        if self.profiles:
            ent("select", "profile", name="Profil", state_topic=st,
                value_template="{{ value_json.profile }}", command_topic=f"{self.base}/set/profile",
                options=list(self.profiles) + [CUSTOM], icon="mdi:leaf")
        log.info("Discovery publicerad (%s)", self.node)

    # ---- callbacks ----
    def on_connect(self, client, userdata, flags, rc, *args):
        log.info("Ansluten till MQTT %s:%s (%s)", self.host, self.port, rc)
        client.subscribe(f"{self.base}/set/#")
        client.subscribe(f"{self.disc}/status")
        self.publish_discovery()
        client.publish(f"{self.base}/availability", "online", retain=True)
        self.publish_state()

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore").strip()
        if msg.topic == f"{self.disc}/status":
            if payload == "online":  # HA har startat om
                self.publish_discovery()
                client.publish(f"{self.base}/availability", "online", retain=True)
                self.publish_state()
            return
        key = msg.topic.rsplit("/", 1)[-1]
        try:
            with self.lock:
                if key == "max_freq":
                    self.cpu.set_max_freq(float(payload)); self.profile = CUSTOM
                elif key == "power_limit":
                    self.cpu.set_power_limit(float(payload)); self.profile = CUSTOM
                elif key == "turbo":
                    self.cpu.set_turbo(payload.upper() in ("ON", "1", "TRUE")); self.profile = CUSTOM
                elif key == "governor":
                    self.cpu.set_governor(payload); self.profile = CUSTOM
                elif key == "profile":
                    self.apply_profile(payload)
        except Exception as e:  # noqa: BLE001
            log.error("Kunde inte sätta %s=%s: %s", key, payload, e)
        self.publish_state()

    def apply_profile(self, name):
        if name == CUSTOM:
            self.profile = CUSTOM
            return
        p = self.profiles.get(name)
        if p is None:
            raise ValueError(f"Okänd profil {name}")
        if p["governor"]:
            if p["governor"] in self.cpu.governors:
                self.cpu.set_governor(p["governor"])
            else:
                log.warning("Governor %s finns inte (%s), hoppar över", p["governor"], self.cpu.governors)
        if p["turbo"] is not None:
            self.cpu.set_turbo(p["turbo"])
        if p["max_freq"] is not None:
            self.cpu.set_max_freq(p["max_freq"] or self.cpu.hw_max)
        if p["power_limit"] is not None and self.cpu.pkgs and self.cpu.pl_writable:
            self.cpu.set_power_limit(p["power_limit"])
        self.profile = name
        log.info("Profil '%s' aktiverad", name)

    def read_state(self):
        avg, peak = self.cpu.freq_cur()
        return {"freq_avg": avg, "freq_peak": peak, "max_freq": self.cpu.max_freq(),
                "load": self.cpu.load(), "temp": self.cpu.temp(),
                "power_pkg": self.cpu.power_pkg(), "power_dram": self.cpu.power_dram(),
                "power_limit": self.cpu.power_limit(), "turbo": self.cpu.turbo(),
                "governor": self.cpu.governor(), "profile": self.profile}

    def publish_state(self):
        with self.lock:
            s = self.read_state()
        self.c.publish(f"{self.base}/state", json.dumps(s), retain=False)
        return s

    def run(self, stop):
        self.c.connect_async(self.host, self.port, 60)
        self.c.loop_start()
        start = self.cfg["general"].get("startup_profile", "").strip()
        if start:
            try:
                with self.lock:
                    self.apply_profile(start)
            except Exception as e:  # noqa: BLE001
                log.error("Startprofil: %s", e)
        while not stop.wait(self.interval):
            if self.c.is_connected():
                self.publish_state()
        self.c.publish(f"{self.base}/availability", "offline", retain=True).wait_for_publish(2)
        self.c.loop_stop()
        self.c.disconnect()


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default="/etc/cpu-power-mqtt.conf")
    ap.add_argument("--status", action="store_true", help="Visa vad som hittas och avsluta (ingen MQTT)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = configparser.ConfigParser()
    cfg.read_dict({"general": {}, "mqtt": {}, "homeassistant": {}})
    if not cfg.read(a.config) and not a.status:
        log.error("Hittar inte konfigurationsfilen %s", a.config)
        sys.exit(1)
    g = cfg["general"]
    cpu = Cpu(g.getint("power_limit_min", 30), g.getint("power_limit_max", 0))

    if a.status:
        cpu.power_pkg(); cpu.power_dram(); cpu.load(); time.sleep(1)
        avg, peak = cpu.freq_cur()
        print(f"Drivrutin:      {cpu.driver}")
        print(f"Frekvensområde: {cpu.hw_min}–{cpu.hw_max} MHz (max nu {cpu.max_freq()} MHz)")
        print(f"Frekvens nu:    medel {avg} MHz, högsta {peak} MHz")
        print(f"Governors:      {', '.join(cpu.governors)} (aktiv: {cpu.governor()})")
        print(f"Turbo:          {cpu.turbo()} ({cpu.turbo_path})")
        print(f"RAPL paket:     {cpu.pkgs or 'saknas'}")
        print(f"Effekt CPU:     {cpu.power_pkg()} W, RAM: {cpu.power_dram()} W")
        print(f"Effekttak:      {cpu.power_limit()} W, skrivbart: {cpu.pl_writable}")
        print(f"Temperatur:     {cpu.temp()} °C")
        print(f"Belastning:     {cpu.load()} %")
        print(f"Profiler:       {', '.join(load_profiles(cfg)) or '-'}")
        return

    if os.geteuid() != 0:
        log.warning("Kör inte som root – skrivningar till /sys kommer att misslyckas")

    snap = cpu.snapshot()
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    try:
        Bridge(cfg, cpu).run(stop)
    finally:
        if g.getboolean("restore_on_exit", True):
            cpu.restore(snap)


if __name__ == "__main__":
    main()
