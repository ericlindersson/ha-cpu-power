#!/bin/sh
# Installerar cpu-power-mqtt. Kör som root: sudo sh install.sh
set -e
DIR=/opt/cpu-power-mqtt

mkdir -p "$DIR"
cp cpu_power_mqtt.py "$DIR/"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q paho-mqtt

if [ ! -f /etc/cpu-power-mqtt.conf ]; then
    cp cpu-power-mqtt.conf /etc/cpu-power-mqtt.conf
    chmod 600 /etc/cpu-power-mqtt.conf
    echo "Redigera /etc/cpu-power-mqtt.conf (MQTT-adress, användare, lösenord)."
fi

cp cpu-power-mqtt.service /etc/systemd/system/
modprobe intel_rapl_msr 2>/dev/null || true
systemctl daemon-reload

echo
"$DIR/venv/bin/python" "$DIR/cpu_power_mqtt.py" --status -c /etc/cpu-power-mqtt.conf
echo
echo "Starta med: systemctl enable --now cpu-power-mqtt"
echo "Loggar:     journalctl -u cpu-power-mqtt -f"
