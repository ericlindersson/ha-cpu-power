#!/bin/sh
# ha-cpu-power installer / setup guide
#   sudo sh install.sh              install or update, then run the setup wizard
#   sudo sh install.sh --uninstall  remove service, program and (optionally) config
# (as root, e.g. on Proxmox, leave out sudo)
set -e

DIR=${HCP_DIR:-/opt/ha-cpu-power}
CONF=${HCP_CONF:-/etc/ha-cpu-power.conf}
UNIT=${HCP_UNIT:-/etc/systemd/system/ha-cpu-power.service}
OLD_CONF=/etc/cpu-power-mqtt.conf
OLD_UNIT=/etc/systemd/system/cpu-power-mqtt.service
SRC=$(cd "$(dirname "$0")" && pwd)

TTY=/dev/tty
[ -r "$TTY" ] || TTY=/dev/stdin

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
yes_no() { # yes_no "question" Y|N
    def=$2
    if [ "$def" = Y ]; then hint="Y/n"; else hint="y/N"; fi
    printf '%s (%s): ' "$1" "$hint"
    read -r ans < "$TTY" || ans=""
    [ -z "$ans" ] && ans=$def
    case "$ans" in [YyJj]*) return 0 ;; *) return 1 ;; esac
}
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo sh install.sh (or as root: sh install.sh)"

# ---------------------------------------------------------------- uninstall
if [ "$1" = "--uninstall" ]; then
    step "Uninstalling ha-cpu-power"
    if have systemctl; then
        systemctl disable --now ha-cpu-power 2>/dev/null || true
    fi
    rm -f "$UNIT" /etc/modules-load.d/ha-cpu-power.conf
    have systemctl && systemctl daemon-reload
    rm -rf "$DIR"
    if [ -f "$CONF" ] && yes_no "Remove config $CONF too?" N; then rm -f "$CONF" "$CONF.bak"; fi
    say "Done. Entities stay in Home Assistant as unavailable; delete the device there if you want."
    exit 0
fi

say "ha-cpu-power installer"
say "This installs to $DIR, writes $CONF and adds a systemd service."

# ---------------------------------------------------------------- checks
step "Checking the system"
[ -d /sys/devices/system/cpu/cpu0/cpufreq ] || {
    say "No cpufreq interface found (/sys/devices/system/cpu/cpu0/cpufreq)."
    say "Virtual machines and containers usually lack it: install on the host instead."
    yes_no "Continue anyway?" N || exit 1
}
have systemctl || die "systemd is required for the service"
have python3 || die "python3 is required"
say "python3: $(python3 --version 2>&1)"

if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    say "Python venv support is missing."
    if   have apt-get; then PKG="apt-get install -y python3-venv"
    elif have dnf;     then PKG="dnf install -y python3"
    elif have pacman;  then PKG="pacman -S --noconfirm python"
    elif have zypper;  then PKG="zypper install -y python3"
    else PKG=""; fi
    [ -n "$PKG" ] || die "install the Python venv package for your distribution and run again"
    if yes_no "Install it now with: $PKG ?" Y; then
        # apt-get update may fail on one repo (e.g. Proxmox enterprise repo without subscription)
        if have apt-get; then apt-get update -qq || say "(apt-get update reported errors, trying anyway)"; fi
        $PKG || die "could not install Python venv support. On Proxmox without a subscription,
       disable the enterprise repo and enable the no-subscription repo, then run again."
    else
        die "Python venv support is needed"
    fi
fi

# ---------------------------------------------------------------- migrate old version
if [ -f "$OLD_UNIT" ]; then
    step "Found the older cpu-power-mqtt service, replacing it"
    systemctl disable --now cpu-power-mqtt 2>/dev/null || true
    rm -f "$OLD_UNIT"
    rm -rf /opt/cpu-power-mqtt
    systemctl daemon-reload
    say "Old service removed. Home Assistant keeps the same entities."
fi

# ---------------------------------------------------------------- install
step "Installing program to $DIR"
WAS_RUNNING=no
systemctl is-active --quiet ha-cpu-power 2>/dev/null && WAS_RUNNING=yes
mkdir -p "$DIR"
cp "$SRC/ha_cpu_power.py" "$DIR/"
[ -x "$DIR/venv/bin/python" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$DIR/venv/bin/pip" install -q --upgrade paho-mqtt
say "OK"

# RAPL power readings on Intel (and AMD on recent kernels)
if modprobe -q intel_rapl_msr 2>/dev/null; then
    echo intel_rapl_msr > /etc/modules-load.d/ha-cpu-power.conf
fi

# ---------------------------------------------------------------- configure
PY="$DIR/venv/bin/python $DIR/ha_cpu_power.py"
step "Setup"
if [ -f "$CONF" ]; then
    if yes_no "A config already exists. Run the setup wizard again?" N; then
        $PY --setup -c "$CONF" < "$TTY"
    fi
elif [ -f "$OLD_CONF" ]; then
    say "Using answers from your old config $OLD_CONF as defaults."
    $PY --setup -c "$CONF" --defaults-from "$OLD_CONF" < "$TTY" && mv "$OLD_CONF" "$OLD_CONF.migrated"
else
    $PY --setup -c "$CONF" < "$TTY"
fi
[ -f "$CONF" ] || die "no config written, run the installer again"

# ---------------------------------------------------------------- service
step "Installing systemd service"
cp "$SRC/ha-cpu-power.service" "$UNIT"
systemctl daemon-reload
if [ "$WAS_RUNNING" = yes ]; then
    systemctl restart ha-cpu-power
    say "Service restarted."
elif yes_no "Start ha-cpu-power now and at every boot?" Y; then
    systemctl enable --now ha-cpu-power
else
    say "Start later with: systemctl enable --now ha-cpu-power"
fi

if systemctl is-active --quiet ha-cpu-power; then
    sleep 3
    step "Log"
    journalctl -u ha-cpu-power -n 8 --no-pager -o cat 2>/dev/null || true
fi

cat <<EOF

Done!
  Status:    sudo $PY --status
  Settings:  sudo $PY --setup        (or edit $CONF)
  Log:       journalctl -u ha-cpu-power -f
  Remove:    sudo sh install.sh --uninstall

The device shows up in Home Assistant under Settings > Devices > MQTT.
EOF
