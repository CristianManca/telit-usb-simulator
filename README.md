# Telit USB Simulator

A Linux tool that creates a virtual USB device whose descriptors (VID, PID, configuration,
interfaces, endpoints, AT responses) are read from a JSON file. The device is exported over
loopback with `usbip` and re-attached to the local machine through `vhci-hcd`, so the host sees
it as a real USB device. It is meant to validate Telit drivers and tools without the hardware
module connected.

The simulator uses a single backend: a FunctionFS gadget (`ffs_daemon.py`) exposed through
`usbip-vudc`.

---

## 1. Requirements

- Linux with root access (`sudo`).
- Kernel modules: `libcomposite`, `usb_f_fs` (FunctionFS), `usbip-core`, `usbip-vudc`, `vhci-hcd`.
  The `usbip` and `usbipd` tools (for example `linux-tools-$(uname -r)`).
- `python3` (stdlib only), `jq` (to validate JSON files).
- `usbip-vudc` must be the patched build in `vudc_fix/` (see section 9). The start script refuses
  to run with the unpatched module unless `SKIP_VUDC_CHECK=1` is set.

---

## 2. Quick start

```bash
# Start a profile (the JSON file is required)
sudo ./telit_vudc.sh start json/1bc7.10a9.json

# Another profile
sudo ./telit_vudc.sh start json/1bc7.10b0.json

# Stop the simulated device
sudo ./telit_vudc.sh stop
```

`start` always stops any running simulation first. After a successful start the script runs
`lsusb`, so you can check the new device. Serial ports appear as `/dev/ttyUSB*`, RNDIS as a
`enx*` network interface.

Runtime files:

| Path | Content |
|---|---|
| `/run/telit_vudc/ffs.log` | Daemon log (events, errors, behaviors) |
| `/run/telit_vudc/ffs.pid` | Daemon PID |
| `/run/telit_vudc/ready` | Created when the gadget is bound |
| `/dev/ffs-telit` | FunctionFS mount point |
| `/sys/kernel/config/usb_gadget/telit` | Configfs gadget |

To validate a profile without touching the USB stack:

```bash
python3 ffs_daemon.py --dump json/1bc7.10a9.json /tmp/out
# writes /tmp/out/descriptors.bin and /tmp/out/strings.bin
```

---

## 3. Folder layout

```
.
├── telit_vudc.sh          # start/stop script (run with sudo)
├── ffs_daemon.py          # FunctionFS daemon: descriptors, behaviors, AT responder
├── json/                  # simulation profiles (one per VID/PID, plus -dump variants)
├── vudc_fix/              # patched usbip-vudc module (CVE fix, see section 9)
└── README.md
```

Profile naming: `json/<vid>.<pid>.json`, for example `1bc7.10a9.json`, `1e2d.0100.json` or
`37b8.a005.json`. Suffixes:
- `.0`, `.1`: PIDs that the CSV lists with an index (`1bc7.1064.0.json`, `1bc7.1065.1.json`).
- `-dump`: variant profiles that contain only a subset of the interfaces (for example
  `1bc7.1031-dump.json` with just DIAG, or `1bc7.8900-dump.json` for the SBL port).

---

## 4. JSON reference

A profile has five top-level keys. Only `device`, `configuration` and `interfaces` are required.

```json
{
  "device": { ... },
  "configuration": { ... },
  "iads": [ ... ],
  "interfaces": [ ... ],
  "at_responses": { ... }
}
```

### 4.1 `device`

All numeric values are strings in hexadecimal (`"0x1bc7"`).

| Key | Example | Notes |
|---|---|---|
| `bcdUSB` | `"0x0200"` | Use `"0x0210"` for USB 2.10 (as on the FE990). |
| `bDeviceClass` | `"0x00"` | Use `"0x00"` when interfaces define the classes (usual for Telit). |
| `bDeviceSubClass` | `"0x00"` | |
| `bDeviceProtocol` | `"0x00"` | |
| `idVendor` | `"0x1bc7"` | Telit VID. |
| `idProduct` | `"0x10a9"` | PID of the module. |
| `bcdDevice` | `"0x0515"` | Firmware revision (5.15). |
| `manufacturer` | `"Telit Cinterion"` | String shown by `lsusb`. |
| `product` | `"FN920"` | String shown by `lsusb`. |
| `serialnumber` | `"00000000"` | Serial string. Use a generic value, never a real device serial. |

### 4.2 `configuration`

| Key | Example | Notes |
|---|---|---|
| `name` | `"RNDIS_NMEA_DUN_DIAG"` | Configuration string (`iConfiguration`). |
| `bmAttributes` | `"0xe0"` | Self-powered + remote wakeup, as on the real modules. |
| `max_power_mA` | `500` | Maximum power in mA. |

### 4.3 `iads` (Interface Association Descriptors)

Optional. Groups consecutive interfaces, typically for RNDIS (class 239/4/1) or MBIM (2/14/0).

| Key | Notes |
|---|---|
| `first_interface` | Number of the first interface in the group (0-based, counts only alternate setting 0). |
| `interface_count` | Number of interfaces in the group. |
| `class`, `subclass`, `protocol` | Function class (for example `239`, `4`, `1` for RNDIS). |
| `name` | Optional string. |

### 4.4 `interfaces`

The list is in **USB interface order**. Each entry is one interface (alternate setting 0) or one
alternate setting of the previous interface.

| Key | Required | Default | Notes |
|---|---|---|---|
| `name` | no | none | Interface string (`iInterface`). Without it the host shows no name. |
| `class` | no | `255` | Decimal. |
| `subclass` | no | `255` | Decimal. |
| `protocol` | no | `255` | Decimal. |
| `alt` | no | `0` | Alternate setting. An entry with `alt` > 0 belongs to the previous interface number. The first entry must have `alt` 0. |
| `behavior` | no | `"none"` | `none`, `at`, `echo`, `rndis`, `mbim` (section 5). |
| `rndis` | only for `behavior: "rndis"` | | Options object (section 5.4). |
| `mbim` | only for `behavior: "mbim"` | | Options object (section 5.5). |
| `endpoints` | yes | | List of endpoints (section 4.5). May be `[]`. |
| `extra_descriptors` | no | | Hex strings for class-specific descriptors. FunctionFS rejects CDC descriptors (section 8). |

Interface numbers are assigned automatically in order (first `alt` 0 entry = 0, next = 1, …).

**Placeholder interfaces.** The kernel renumbers the interfaces from 0, so a profile cannot force
an interface to a given number. To reproduce a real `MI_xx` (for example a DIAG on MI_02), insert
empty placeholder interfaces before it:

```json
{ "name": "placeholder", "class": 254, "subclass": 1, "protocol": 1, "endpoints": [] }
```

Use class 254 (`0xFE`), not 255: the `option` driver binds vendor-specific (255) interfaces even
when they are empty, and each bind reserves a `ttyUSB` number, which leaves gaps in the numbering.
For example, `json/1bc7.1203-dump.json` has two placeholders before `QHSUSB__BULK`.

### 4.5 `endpoints`

| Key | Values | Default |
|---|---|---|
| `type` | `bulk`, `interrupt` | required |
| `dir` | `in`, `out` | required |
| `maxpacket` | integer | `512` for bulk, `64` for interrupt |
| `interval` | integer (ms, for interrupt) | `9` for interrupt, `0` for bulk |

Limits (enforced by the daemon):
- Maximum 15 IN and 15 OUT endpoints per profile.
- With 15 endpoints or fewer, addresses are numbered sequentially.
- With more, addresses are numbered per direction (like the real module).

### 4.6 `at_responses`

Map of AT command to response text. The daemon matches the whole received line
(case-insensitive, without `\r`/`\n`) and replies with:

```
\r\n<value>\r\n\r\nOK\r\n
```

Unknown commands get only `\r\nOK\r\n`. Put the full line in the key, for example `"ATI"` or
`"AT#USBCFG?"`. Use `\r\n` inside values for multi-line answers.

```json
"at_responses": {
  "ATI": "Telit Cinterion\r\nFN920C04\r\nRevision: SIMULATED",
  "AT+CGMI": "Telit Cinterion",
  "AT+CGMM": "FN920C04",
  "AT+CGMR": "SIMULATED",
  "AT+CGSN": "000000000000000",
  "AT#USBCFG?": "#USBCFG: 8",
  "AT#SWPKGV": "SIMULATED"
}
```

---

## 5. Behaviors

`behavior` decides what the daemon does with data on an interface.

| Behavior | Typical interface | Requirements | What it does |
|---|---|---|---|
| `none` | any | | Descriptors only. Data is not answered. |
| `at` | AT/modem serial | at least one bulk OUT and one bulk IN | Answers each line with `at_responses`. |
| `echo` | test serial | bulk OUT and bulk IN | Returns the received bytes unchanged. |
| `rndis` | RNDIS control | one interrupt IN (notification) | Full RNDIS handshake, Ethernet over the data interface. |
| `mbim` | MBIM control | one interrupt IN | MBIM protocol. Not usable with FunctionFS (section 8). |

Only one `rndis` or `mbim` interface is supported per profile. Interfaces with `behavior: "at"`
use the first bulk IN endpoint of the same interface for replies.

### 5.1 Control requests

Class control requests the daemon does not know (for example `SET_CONTROL_LINE_STATE` 0x22 and
`SET_LINE_CODING` 0x20) are completed with the correct FunctionFS direction: OUT requests read
their data stage, IN requests write their reply. `GET_LINE_CODING` (0x21) returns
115200 8N1. Wrong directions cause a stall, which pyserial reports as `Broken pipe` when it
opens the port.

### 5.2 `at` example

```json
{
  "name": "CDEV Serial",
  "class": 255,
  "subclass": 255,
  "protocol": 64,
  "behavior": "at",
  "endpoints": [
    { "type": "interrupt", "dir": "in", "maxpacket": 10, "interval": 9 },
    { "type": "bulk", "dir": "in" },
    { "type": "bulk", "dir": "out" }
  ]
}
```

### 5.3 `echo` example

```json
{
  "name": "Echo test",
  "class": 255,
  "subclass": 255,
  "protocol": 0,
  "behavior": "echo",
  "endpoints": [
    { "type": "bulk", "dir": "in" },
    { "type": "bulk", "dir": "out" }
  ]
}
```

### 5.4 `rndis` options

| Key | Default | Notes |
|---|---|---|
| `mac` | `"02:00:00:00:00:01"` | Host-side MAC address (`02:` prefix = locally administered). |
| `mtu` | `1500` | |
| `data_interface` | control interface + 1 | Interface number of the data interface. |

### 5.5 `mbim` options

| Key | Default | Notes |
|---|---|---|
| `ntb_in_max` | `65536` | Maximum NTB size host to device. |
| `ntb_out_max` | `65536` | Maximum NTB size device to host. |
| `data_interface` | control interface + 1 | |

---

## 6. Interface examples by function

Every example below is an interface object for the `interfaces` list. The class/protocol values
match the ones used by the generated profiles in `json/`.

### 6.1 DIAG (Qualcomm diagnostic)

```json
{
  "name": "diag",
  "class": 255,
  "subclass": 255,
  "protocol": 48,
  "behavior": "none",
  "endpoints": [
    { "type": "bulk", "dir": "out" },
    { "type": "bulk", "dir": "in" }
  ]
}
```

### 6.2 SAHARA (boot/download port)

Same descriptor as DIAG (`protocol` 48). Used in the SBL profiles (`1bc7.8900-dump.json`).

```json
{
  "name": "sahara",
  "class": 255,
  "subclass": 255,
  "protocol": 48,
  "behavior": "none",
  "endpoints": [
    { "type": "bulk", "dir": "out" },
    { "type": "bulk", "dir": "in" }
  ]
}
```

### 6.3 ADB

```json
{
  "name": "ADB Interface",
  "class": 255,
  "subclass": 66,
  "protocol": 1,
  "behavior": "none",
  "endpoints": [
    { "type": "bulk", "dir": "out" },
    { "type": "bulk", "dir": "in" }
  ]
}
```

### 6.4 DPL (ADPL data)

Bulk IN only, as on the real module (`protocol` 128).

```json
{
  "name": "DPL Data",
  "class": 255,
  "subclass": 255,
  "protocol": 128,
  "behavior": "none",
  "endpoints": [
    { "type": "bulk", "dir": "in" }
  ]
}
```

### 6.5 QDSS

```json
{
  "name": "QDSS DATA",
  "class": 255,
  "subclass": 255,
  "protocol": 112,
  "behavior": "none",
  "endpoints": [
    { "type": "bulk", "dir": "in" }
  ]
}
```

### 6.6 RMNET (QMI over vendor interface)

On Linux, `qmi_wwan` binds to this interface and creates `wwan*` and `/dev/cdc-wdm*`. There is
no QMI responder in the simulator, so no QMI request is answered (section 8).

Layout used by the profiles: class/subclass/protocol `255/255/80`, three endpoints. The first
(interrupt IN, 8 bytes) is the QMI notification endpoint; the two bulk endpoints carry the QMI
requests and responses. The name is `RMNET wwan`.

```json
{
  "name": "RMNET wwan",
  "class": 255,
  "subclass": 255,
  "protocol": 80,
  "behavior": "none",
  "endpoints": [
    { "type": "interrupt", "dir": "in", "maxpacket": 8, "interval": 9 },
    { "type": "bulk", "dir": "in" },
    { "type": "bulk", "dir": "out" }
  ]
}
```

### 6.7 NMEA (GNSS serial)

```json
{
  "name": "CDEV Serial",
  "class": 255,
  "subclass": 255,
  "protocol": 96,
  "behavior": "none",
  "endpoints": [
    { "type": "interrupt", "dir": "in", "maxpacket": 10, "interval": 9 },
    { "type": "bulk", "dir": "in" },
    { "type": "bulk", "dir": "out" }
  ]
}
```

### 6.8 Modem / auxiliary serial

`protocol` 64 is the generic modem/auxiliary port. Give `behavior: "at"` to the first modem port
only, so it answers AT commands.

```json
{
  "name": "modem",
  "class": 255,
  "subclass": 255,
  "protocol": 64,
  "behavior": "at",
  "endpoints": [
    { "type": "interrupt", "dir": "in", "maxpacket": 10, "interval": 9 },
    { "type": "bulk", "dir": "in" },
    { "type": "bulk", "dir": "out" }
  ]
}
```

### 6.9 Serial with no replies

Same as 6.8 with `"behavior": "none"` (for example the second `modem` port or `aux (trace)`).

### 6.10 CDC ACM (mapped as vendor serial)

The generator maps `ACM` to a vendor serial (`protocol` 64), not to CDC ACM. Without the CDC
descriptors `cdc_acm` does not bind, so the port is exposed as a plain vendor serial.

### 6.11 MUX / SAP

Vendor bulk pair. Used by the profiles with `MUX` or `SAP` in the CSV (the `SAP` port is
mapped to the same descriptor as DIAG, section 6.2).

```json
{
  "name": "MUX",
  "class": 255,
  "subclass": 255,
  "protocol": 255,
  "behavior": "none",
  "endpoints": [
    { "type": "bulk", "dir": "out" },
    { "type": "bulk", "dir": "in" }
  ]
}
```

### 6.12 Audio (control only)

Class 1, subclass 1 (AudioControl). No streaming interface and no endpoints.

```json
{
  "name": "audio",
  "class": 1,
  "subclass": 1,
  "protocol": 0,
  "behavior": "none",
  "endpoints": []
}
```

### 6.13 ECM (CDC Ethernet, two interfaces)

Control interface (class 2/6) with an interrupt IN, followed by the data interface with
alternate setting 0 (no endpoints) and alternate setting 1 (bulk IN/OUT). Without CDC
functional descriptors the host does not create a network interface (section 8).

```json
[
  {
    "name": "CDC Ethernet Control Model (ECM)",
    "class": 2,
    "subclass": 6,
    "protocol": 0,
    "behavior": "none",
    "endpoints": [
      { "type": "interrupt", "dir": "in", "maxpacket": 16, "interval": 9 }
    ]
  },
  {
    "class": 10,
    "subclass": 0,
    "protocol": 0,
    "alt": 0,
    "behavior": "none",
    "endpoints": []
  },
  {
    "name": "CDC Ethernet Data",
    "class": 10,
    "subclass": 0,
    "protocol": 0,
    "alt": 1,
    "behavior": "none",
    "endpoints": [
      { "type": "bulk", "dir": "in" },
      { "type": "bulk", "dir": "out" }
    ]
  }
]
```

### 6.14 MBIM (two interfaces with IAD)

Same structure as ECM, with class 2/14 and data `protocol` 2, plus an IAD. The `behavior` is
`none` in the generated profiles. `behavior: "mbim"` exists in the daemon but the host cannot bind
without CDC descriptors (section 8).

```json
{
  "iads": [
    { "first_interface": 0, "interface_count": 2, "class": 2, "subclass": 14, "protocol": 0, "name": "MBIM" }
  ],
  "interfaces": [
    {
      "name": "MBIM Control",
      "class": 2,
      "subclass": 14,
      "protocol": 0,
      "behavior": "none",
      "endpoints": [
        { "type": "interrupt", "dir": "in", "maxpacket": 64, "interval": 9 }
      ]
    },
    { "class": 10, "subclass": 0, "protocol": 2, "alt": 0, "behavior": "none", "endpoints": [] },
    {
      "name": "MBIM Data",
      "class": 10,
      "subclass": 0,
      "protocol": 2,
      "alt": 1,
      "behavior": "none",
      "endpoints": [
        { "type": "bulk", "dir": "in" },
        { "type": "bulk", "dir": "out" }
      ]
    }
  ]
}
```

### 6.15 RNDIS (working)

IAD for class 239/4/1, control interface with `behavior: "rndis"`, and a data interface with a
single entry (no alternate setting). This is the only network function that works end to end.

```json
{
  "iads": [
    { "first_interface": 0, "interface_count": 2, "class": 239, "subclass": 4, "protocol": 1, "name": "RNDIS" }
  ],
  "interfaces": [
    {
      "name": "RNDIS Communications Control",
      "class": 239,
      "subclass": 4,
      "protocol": 1,
      "behavior": "rndis",
      "rndis": {
        "mac": "02:00:00:00:10:a1",
        "mtu": 1500,
        "data_interface": 1
      },
      "endpoints": [
        { "type": "interrupt", "dir": "in", "maxpacket": 8, "interval": 9 }
      ]
    },
    {
      "name": "RNDIS Ethernet Data",
      "class": 10,
      "subclass": 0,
      "protocol": 0,
      "behavior": "none",
      "endpoints": [
        { "type": "bulk", "dir": "in" },
        { "type": "bulk", "dir": "out" }
      ]
    }
  ]
}
```

---

## 7. Complete example

A minimal but complete profile: an AT port, a DIAG port and an ADB interface.

```json
{
  "device": {
    "bcdUSB": "0x0200",
    "bDeviceClass": "0x00",
    "bDeviceSubClass": "0x00",
    "bDeviceProtocol": "0x00",
    "idVendor": "0x1bc7",
    "idProduct": "0x1234",
    "bcdDevice": "0x0515",
    "manufacturer": "Telit Cinterion",
    "product": "FN920C04",
    "serialnumber": "00000000"
  },
  "configuration": {
    "name": "DIAG_MODEM_ADB",
    "bmAttributes": "0xe0",
    "max_power_mA": 500
  },
  "interfaces": [
    {
      "name": "diag",
      "class": 255,
      "subclass": 255,
      "protocol": 48,
      "behavior": "none",
      "endpoints": [
        { "type": "bulk", "dir": "out" },
        { "type": "bulk", "dir": "in" }
      ]
    },
    {
      "name": "modem",
      "class": 255,
      "subclass": 255,
      "protocol": 64,
      "behavior": "at",
      "endpoints": [
        { "type": "interrupt", "dir": "in", "maxpacket": 10, "interval": 9 },
        { "type": "bulk", "dir": "in" },
        { "type": "bulk", "dir": "out" }
      ]
    },
    {
      "name": "ADB Interface",
      "class": 255,
      "subclass": 66,
      "protocol": 1,
      "behavior": "none",
      "endpoints": [
        { "type": "bulk", "dir": "out" },
        { "type": "bulk", "dir": "in" }
      ]
    }
  ],
  "at_responses": {
    "ATI": "Telit Cinterion\r\nFN920C04\r\nRevision: SIMULATED",
    "AT+CGMI": "Telit Cinterion",
    "AT+CGMM": "FN920C04",
    "AT+CGMR": "SIMULATED",
    "AT+CGSN": "000000000000000",
    "AT#SWPKGV": "SIMULATED"
  }
}
```

Validate it:

```bash
jq -e . json/my_profile.json
python3 -c "import json, ffs_daemon as f; f.build_layout(json.load(open('json/my_profile.json')))"
```

---

## 8. Limitations

| Feature | Status | Why |
|---|---|---|
| Serial ports (AT, NMEA, DIAG, modem) | Works | Vendor interfaces, bound by `option` or used raw. |
| RNDIS network | Works | `rndis_host` falls back to fixed interfaces 0/1 if CDC descriptors are missing. |
| ECM network | Not working | `cdc_ether` needs CDC union/header/ethernet descriptors. FunctionFS does not pass them. |
| MBIM (`cdc_mbim`, `/dev/cdc-wdm*`) | Not working | Same as ECM. `extra_descriptors` are rejected by FunctionFS. |
| CDC ACM | Not working | Mapped to vendor serial instead. |
| RMNET / QMI | Interface created, no answers | `qmi_wwan` creates `wwan*` and `/dev/cdc-wdm*`, but there is no QMI responder. |
| Audio | Control interface only | No streaming endpoints. |
| AT commands | Fixed answers | Only the keys in `at_responses` are answered. Unknown commands get `OK`. |
| Serial numbers, IMEI | Placeholders | Use `"00000000"` or `"000000000000000"`. Telit tools that check the serial may need a real value. |

Earlier attempts with Raw Gadget (kernel `raw_gadget`) did not work with `usbip-vudc`: the device
descriptor was never exposed, so the backend was removed.

---

## 9. Kernel patch (usbip-vudc)

`vudc_fix/` contains the patched `usbip-vudc` module, which fixes a NULL pointer dereference in
`vep_dequeue` when an endpoint is stopped. It is installed as:

```
/lib/modules/$(uname -r)/updates/usbip-vudc.ko
```

After copying it, run `sudo depmod -a`. Check it with `modinfo -F filename usbip-vudc`
(the path must contain `/updates/`). To revert, delete the file and run `depmod -a`.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| `Cannot read vudc device descr file` | The gadget is not bound yet. Look at `/run/telit_vudc/ffs.log`. |
| `two fullspeed descriptors for EP N` / `failed to start ... -22` | Duplicate endpoint address in the profile. Run the `build_layout` check from section 7. |
| More than 15 endpoints in one direction | The daemon refuses the profile. Reduce the endpoints of that direction. |
| `Broken pipe` when opening `/dev/ttyUSB*` | A control request was stalled. Check the daemon, then restart. |
| Gaps in `ttyUSB` numbering, or an interface does not have the expected MI | Vendor-specific placeholder interfaces bound by `option`, or missing placeholders. See section 4.4. |
| `OSError: [Errno 22] Invalid argument` writing strings | Only with an old daemon: the strings table must be `0/0` when a profile has no strings. Current `ffs_daemon.py` does this. |
| No `/dev/ttyUSB*` | Run `lsusb -t` to see which driver bound each interface. |
| Serial port silent | `grep -v "event SETUP" /run/telit_vudc/ffs.log` and check the `at` behavior. |

Useful commands:

```bash
lsusb -d 1bc7:
ls -l /dev/ttyUSB* /sys/class/net | grep -i enx
grep -v "event SETUP" /run/telit_vudc/ffs.log | tail -30
sudo dmesg | tail -40
```

---

## 11. Adding a profile

1. Copy the closest existing profile from `json/`.
2. Change `idProduct`, `configuration.name`, `product` and the `interfaces` list.
3. Validate with `jq` and `build_layout` (section 7).
4. Start it with `sudo ./telit_vudc.sh start json/<file>.json`.
