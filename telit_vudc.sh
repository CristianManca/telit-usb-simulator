#!/usr/bin/env bash
# Usage: sudo ./telit_vudc.sh start config.json | stop
# Creates a virtual USB device (FunctionFS gadget on usbip-vudc) described by a JSON file,
# exports it over loopback with usbip and re-attaches it as a local USB device.
# The JSON file is required, for example: sudo ./telit_vudc.sh start json/1bc7.10a9.json
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

VUDC=usbip-vudc.0
G=/sys/kernel/config/usb_gadget/telit
FFS_NAME=telit
FFS_MNT=/dev/ffs-telit
RUNDIR=/run/telit_vudc
PIDFILE=$RUNDIR/ffs.pid
READYFILE=$RUNDIR/ready
LOGFILE=$RUNDIR/ffs.log

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo" >&2
    exit 1
fi

# The Ubuntu usbipd/usbip wrapper looks for linux-tools of the exact running kernel (not
# available here): use the user-space binaries of the newest installed linux-tools instead
find_tool() {
    local bin
    bin=$(ls /usr/lib/linux-tools-*/"$1" 2>/dev/null | sort -V | tail -n1 || true)
    echo "${bin:-$1}"
}
USBIP=$(find_tool usbip)
USBIPD=$(find_tool usbipd)

detach_ports() {
    "$USBIP" port 2>/dev/null | awk -v b="$VUDC" '
        /^Port/ { p = $2; sub(/:$/, "", p) }
        index($0, b) { print p }' | sort -u | while read -r p; do
        "$USBIP" detach -p "$p" || true
    done
}

stop() {
    detach_ports || true
    if [ -d "$G" ]; then
        echo "" > "$G/UDC" 2>/dev/null || true
    fi
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
    fi
    rm -f "$READYFILE"
    if mountpoint -q "$FFS_MNT"; then
        umount "$FFS_MNT" || true
    fi
    rmdir "$FFS_MNT" 2>/dev/null || true
    if [ -d "$G" ]; then
        find "$G/configs" -mindepth 2 -maxdepth 2 -type l -delete 2>/dev/null || true
        rmdir "$G"/configs/*/strings/* "$G"/configs/* "$G"/functions/* "$G"/strings/* 2>/dev/null || true
        rmdir "$G" 2>/dev/null || true
    fi
    for pid in $(pgrep -x usbipd || true); do
        kill "$pid"
    done
}

# Write the given JSON field to the configfs file, if present
set_attr() {
    local filter=$1 dest=$2 value
    value=$(jq -r "$filter // empty" "$CONFIG")
    if [ -n "$value" ]; then
        echo "$value" > "$dest"
    fi
}

start() {
    local config=${1:-}
    [ -n "$config" ] || { echo "Usage: sudo $0 start config.json" >&2; exit 1; }
    CONFIG=$(readlink -f "$config")

    command -v jq >/dev/null || { echo "jq not installed (sudo apt install jq)" >&2; exit 1; }
    [ -f "$CONFIG" ] || { echo "Config not found: $config" >&2; exit 1; }
    jq -e '.device.idVendor and .device.idProduct and (.interfaces | length > 0)' "$CONFIG" >/dev/null \
        || { echo "Invalid JSON: device.idVendor, device.idProduct and interfaces[] are required" >&2; exit 1; }

    stop
    # With this kernel's stock vudc (bug CVE-2026-64331) stopping the gadget causes a kernel oops
    if [ -z "${SKIP_VUDC_CHECK:-}" ] && ! modinfo -F filename usbip-vudc | grep -q '/updates/'; then
        echo "The installed usbip-vudc is the stock one with the vep_dequeue bug (oops on stop)." >&2
        echo "Install the fixed version: see vudc_fix/ (make; sudo make install-fix)." >&2
        echo "To skip this check: sudo SKIP_VUDC_CHECK=1 $0 start" >&2
        exit 1
    fi
    modprobe libcomposite
    modprobe usbip-vudc
    modprobe usbip-core
    modprobe vhci-hcd
    if [ ! -e "/sys/class/udc/$VUDC" ]; then
        echo "UDC $VUDC not available after modprobe" >&2
        exit 1
    fi

    mkdir "$G"
    local k
    for k in bcdUSB bDeviceClass bDeviceSubClass bDeviceProtocol idVendor idProduct bcdDevice; do
        set_attr ".device.$k" "$G/$k"
    done
    mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409"
    for k in manufacturer product serialnumber; do
        set_attr ".device.$k" "$G/strings/0x409/$k"
    done
    set_attr '.configuration.bmAttributes' "$G/configs/c.1/bmAttributes"
    set_attr '.configuration.max_power_mA' "$G/configs/c.1/MaxPower"
    set_attr '.configuration.name' "$G/configs/c.1/strings/0x409/configuration"

    mkdir "$G/functions/ffs.$FFS_NAME"
    ln -s "$G/functions/ffs.$FFS_NAME" "$G/configs/c.1/"

    mkdir -p "$FFS_MNT" "$RUNDIR"
    mount -t functionfs "$FFS_NAME" "$FFS_MNT"
    rm -f "$READYFILE"
    python3 "$SCRIPT_DIR/ffs_daemon.py" "$CONFIG" "$FFS_MNT" "$READYFILE" "$PIDFILE" \
        > "$LOGFILE" 2>&1 &

    local i
    for i in $(seq 1 100); do
        [ -e "$READYFILE" ] && break
        if ! kill -0 $! 2>/dev/null; then
            echo "ffs_daemon exited, log ($LOGFILE):" >&2
            cat "$LOGFILE" >&2
            stop
            exit 1
        fi
        sleep 0.1
    done
    if [ ! -e "$READYFILE" ]; then
        echo "ffs_daemon not ready after 10s, log ($LOGFILE):" >&2
        cat "$LOGFILE" >&2
        stop
        exit 1
    fi

    echo "$VUDC" > "$G/UDC"

    "$USBIPD" -D --device
    sleep 1
    "$USBIP" attach --remote 127.0.0.1 --busid "$VUDC"

    sleep 1
    local vid pid
    vid=$(printf '%04x' "$(jq -r '.device.idVendor' "$CONFIG")")
    pid=$(printf '%04x' "$(jq -r '.device.idProduct' "$CONFIG")")
    lsusb -d "$vid:$pid" || echo "device not visible in lsusb" >&2
}

case "${1:-}" in
    start) start "${2:-}" ;;
    stop) stop; echo "Gadget stopped." ;;
    *) echo "Usage: sudo $0 start config.json | stop" >&2; exit 1 ;;
esac
