#!/usr/bin/env python3
"""FunctionFS daemon: exposes the USB interfaces defined in the JSON configuration.

Usage: ffs_daemon.py CONFIG.json FFS_MOUNT_DIR READY_FILE PID_FILE
       ffs_daemon.py --dump CONFIG.json DIR   (writes descriptors.bin and strings.bin
                                               without touching FunctionFS)
"""
import json
import os
import select
import struct
import sys
import threading
import time

FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_STRINGS_MAGIC = 2
FUNCTIONFS_HAS_FS_DESC = 1
FUNCTIONFS_HAS_HS_DESC = 2

USB_DT_INTERFACE = 4
USB_DT_ENDPOINT = 5
USB_DT_INTERFACE_ASSOCIATION = 11
USB_DT_CS_INTERFACE = 0x24
EP_TYPES = {"control": 0, "iso": 1, "bulk": 2, "interrupt": 3}

EVENT_NAMES = {0: "BIND", 1: "UNBIND", 2: "ENABLE", 3: "DISABLE",
               4: "SETUP", 5: "SUSPEND", 6: "RESUME"}
EVENT_SETUP = 4


def num(v):
    return int(v, 0) if isinstance(v, str) else int(v)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def build_layout(cfg):
    """Return (interfaces, endpoints in declaration order, strings, IADs)."""
    strings = []
    eps = []
    ifaces = []
    iads = []
    count = sum(1 for i in cfg["interfaces"] if num(i.get("alt", 0)) == 0)
    # FunctionFS matches endpoints by their full address (number + direction), so every
    # address must be unique. Up to 15 endpoints, numbers are sequential; beyond that they
    # are numbered per direction, like on real hardware (max 15 IN + 15 OUT).
    total_eps = sum(len(i["endpoints"]) for i in cfg["interfaces"])
    dir_count = {"in": 0, "out": 0}
    for iad in cfg.get("iads", []):
        first = num(iad["first_interface"])
        n_if = num(iad["interface_count"])
        if first < 0 or n_if < 1 or first + n_if > count:
            raise ValueError(f"IAD {first}+{n_if} is outside the interface range")
        sidx = 0
        if iad.get("name"):
            strings.append(iad["name"])
            sidx = len(strings)
        iads.append({"first": first, "count": n_if,
                     "cls": num(iad.get("class", 255)),
                     "sub": num(iad.get("subclass", 255)),
                     "proto": num(iad.get("protocol", 255)), "string": sidx})
    n = -1
    for itf in cfg["interfaces"]:
        # an entry with "alt" > 0 is an alternate setting of the previous interface
        alt = num(itf.get("alt", 0))
        if alt == 0:
            n += 1
        elif n < 0:
            raise ValueError("the first interface cannot be an alternate setting")
        name = itf.get("name")
        sidx = 0
        if name:
            strings.append(name)
            sidx = len(strings)
        item_eps = []
        # class-specific descriptors (hex strings) emitted right after the interface descriptor
        extra = []
        for hexstr in itf.get("extra_descriptors", []):
            raw = bytes.fromhex(hexstr)
            if len(raw) < 3 or raw[0] != len(raw) or raw[1] != USB_DT_CS_INTERFACE:
                raise ValueError(f"interface {n}: invalid extra descriptor '{hexstr}' "
                                 "(bLength must match and bDescriptorType must be 0x24)")
            extra.append(raw)
        for ep in itf["endpoints"]:
            direction = ep["dir"].lower()
            if direction not in ("in", "out"):
                raise ValueError(f"interface {n}: dir must be in|out")
            etype = ep["type"].lower()
            if etype not in EP_TYPES or etype == "control":
                raise ValueError(f"interface {n}: unsupported endpoint type {etype}")
            default_mp = 512 if etype == "bulk" else 64
            index = len(eps) + 1
            dir_count[direction] += 1
            number = index if total_eps <= 15 else dir_count[direction]
            if number > 15:
                raise ValueError(f"too many {direction.upper()} endpoints (max 15)")
            info = {
                "index": index,
                "address": number | (0x80 if direction == "in" else 0),
                "type": etype,
                "dir": direction,
                "maxpacket": num(ep.get("maxpacket", default_mp)),
                "interval": num(ep.get("interval", 0 if etype == "bulk" else 9)),
                "iface": n,
            }
            eps.append(info)
            item_eps.append(info)
        ifaces.append({"number": n, "alt": alt, "cls": num(itf.get("class", 255)),
                       "sub": num(itf.get("subclass", 255)),
                       "proto": num(itf.get("protocol", 255)),
                       "string": sidx, "eps": item_eps,
                       "behavior": itf.get("behavior", "none"),
                       "opts": itf.get("rndis", itf.get("mbim", {})),
                       "extra": extra})
    return ifaces, eps, strings, iads


def speed_descs(ifaces, iads, high_speed):
    out = b""
    count = 0
    for itf in ifaces:
        for iad in iads:
            if iad["first"] == itf["number"] and itf["alt"] == 0:
                out += struct.pack("<BBBBBBBB", 8, USB_DT_INTERFACE_ASSOCIATION,
                                   iad["first"], iad["count"], iad["cls"],
                                   iad["sub"], iad["proto"], iad["string"])
                count += 1
        out += struct.pack("<BBBBBBBBB", 9, USB_DT_INTERFACE, itf["number"], itf["alt"],
                           len(itf["eps"]), itf["cls"], itf["sub"], itf["proto"],
                           itf["string"])
        count += 1
        for raw in itf["extra"]:
            out += raw
            count += 1
        for ep in itf["eps"]:
            mp = ep["maxpacket"]
            if not high_speed and ep["type"] in ("bulk", "interrupt"):
                mp = min(mp, 64)
            out += struct.pack("<BBBBHB", 7, USB_DT_ENDPOINT, ep["address"],
                               EP_TYPES[ep["type"]], mp, ep["interval"])
            count += 1
    return count, out


def build_descriptors(ifaces, iads):
    fs_count, fs = speed_descs(ifaces, iads, False)
    hs_count, hs = speed_descs(ifaces, iads, True)
    body = struct.pack("<II", fs_count, hs_count) + fs + hs
    flags = FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
    length = 12 + len(body)
    return struct.pack("<III", FUNCTIONFS_DESCRIPTORS_MAGIC_V2, length, flags) + body


def build_strings(strings):
    if not strings:
        # the kernel requires str_count and lang_count to be zero together
        return struct.pack("<IIII", FUNCTIONFS_STRINGS_MAGIC, 16, 0, 0)
    body = struct.pack("<H", 0x0409) + b"".join(s.encode() + b"\0" for s in strings)
    length = 16 + len(body)
    return struct.pack("<IIII", FUNCTIONFS_STRINGS_MAGIC, length, len(strings), 1) + body


class AtResponder:
    def __init__(self, responses):
        self.responses = {k.upper(): v for k, v in responses.items()}
        self.buf = b""

    def feed(self, data):
        replies = []
        self.buf += data
        while True:
            pos = min((i for i in (self.buf.find(b"\r"), self.buf.find(b"\n")) if i >= 0),
                      default=-1)
            if pos < 0:
                break
            line = self.buf[:pos].strip().decode(errors="replace")
            self.buf = self.buf[pos + 1:]
            if not line:
                continue
            resp = self.responses.get(line.upper())
            text = f"\r\n{resp}\r\n\r\nOK\r\n" if resp else "\r\nOK\r\n"
            replies.append(text.encode())
        return replies


RNDIS_MSG_INIT = 0x2
RNDIS_MSG_HALT = 0x3
RNDIS_MSG_QUERY = 0x4
RNDIS_MSG_SET = 0x5
RNDIS_MSG_RESET = 0x6
RNDIS_MSG_KEEPALIVE = 0x8
RNDIS_COMPLETE = 0x80000000
RNDIS_STATUS_SUCCESS = 0
RNDIS_STATUS_NOT_SUPPORTED = 0xC00000BB
RNDIS_STATUS_INVALID_DATA = 0xC0010015

OID_GEN_SUPPORTED_LIST = 0x00010101
OID_GEN_CURRENT_PACKET_FILTER = 0x0001010E
OID_802_3_PERMANENT_ADDRESS = 0x01010101
OID_802_3_CURRENT_ADDRESS = 0x01010102

# Control requests of the RNDIS control interface (CDC encapsulated commands)
SEND_ENCAPSULATED_COMMAND = (0x21, 0x00)
GET_ENCAPSULATED_RESPONSE = (0xA1, 0x01)
RESPONSE_AVAILABLE = struct.pack("<II", 1, 0)


class RndisDevice:
    """Minimal RNDIS device: answers the control messages used by Linux rndis_host."""

    empty_response = b"\0"

    def __init__(self, mac="02:00:00:00:00:01", mtu=1500):
        self.mac = bytes(int(x, 16) for x in mac.split(":"))
        if len(self.mac) != 6:
            raise ValueError(f"invalid RNDIS MAC address: {mac}")
        self.mtu = mtu
        self.packet_filter = 0
        u32 = lambda v: struct.pack("<I", v)
        self.oids = {
            0x00010102: u32(0),                      # HARDWARE_STATUS
            0x00010103: u32(0),                      # MEDIA_SUPPORTED (802.3)
            0x00010104: u32(0),                      # MEDIA_IN_USE (802.3)
            0x00010105: u32(mtu + 14),               # MAXIMUM_LOOKAHEAD
            0x00010106: u32(mtu),                    # MAXIMUM_FRAME_SIZE
            0x00010107: u32(4800000),                # LINK_SPEED (100 bit/s units)
            0x0001010A: u32(mtu + 14),               # TRANSMIT_BLOCK_SIZE
            0x0001010B: u32(mtu + 14),               # RECEIVE_BLOCK_SIZE
            0x0001010C: u32(0x00001BC7),             # VENDOR_ID
            0x0001010D: b"Telit\0",                  # VENDOR_DESCRIPTION
            0x0001010F: u32(mtu + 14),               # CURRENT_LOOKAHEAD
            0x00010110: u32(1),                      # DRIVER_VERSION
            0x00010111: u32(mtu + 14),               # MAXIMUM_TOTAL_SIZE
            0x00010113: u32(0x12),                   # MAC_OPTIONS
            0x00010114: u32(0),                      # MEDIA_CONNECT_STATUS (connected)
            0x00010115: u32(1),                      # MAXIMUM_SEND_PACKETS
            0x00010116: u32(1),                      # VENDOR_DRIVER_VERSION
            0x00010202: u32(0),                      # PHYSICAL_MEDIUM
            0x00020101: u32(0), 0x00020102: u32(0), 0x00020103: u32(0),
            0x00020104: u32(0), 0x00020105: u32(0),  # XMIT/RCV statistics
            OID_802_3_PERMANENT_ADDRESS: self.mac,
            OID_802_3_CURRENT_ADDRESS: self.mac,
            0x01010103: u32(0xE0000000),             # MULTICAST_LIST
            0x01010104: u32(1),                      # MAXIMUM_LIST_SIZE
            0x01010105: u32(0),                      # MAC_OPTIONS
            0x01020101: u32(0), 0x01020102: u32(0), 0x01020103: u32(0),
        }
        self.oids[OID_GEN_SUPPORTED_LIST] = b"".join(
            u32(o) for o in sorted(self.oids))
        self.settable = {OID_GEN_CURRENT_PACKET_FILTER, 0x0001010F, 0x01010103}

    @staticmethod
    def _complete(mtype, body):
        return struct.pack("<II", mtype | RNDIS_COMPLETE, 8 + len(body)) + body

    def handle_command(self, msg):
        """Return the response for an RNDIS control message, or None when there is none."""
        if len(msg) < 8:
            return None
        mtype, mlen = struct.unpack_from("<II", msg)
        if mtype == RNDIS_MSG_HALT:
            self.packet_filter = 0
            return None
        if mtype == RNDIS_MSG_RESET:
            return self._complete(mtype, struct.pack("<II", RNDIS_STATUS_SUCCESS, 1))
        if len(msg) < 12:
            return None
        req_id = struct.unpack_from("<I", msg, 8)[0]
        if mtype == RNDIS_MSG_INIT:
            body = struct.pack("<11I", req_id, RNDIS_STATUS_SUCCESS, 1, 0,
                               1, 0, 1, self.mtu + 14 + 44 + 22, 0, 0, 0)
            return self._complete(mtype, body)
        if mtype == RNDIS_MSG_KEEPALIVE:
            return self._complete(mtype, struct.pack("<II", req_id, RNDIS_STATUS_SUCCESS))
        if mtype == RNDIS_MSG_QUERY and len(msg) >= 20:
            oid = struct.unpack_from("<I", msg, 12)[0]
            data = self.oids.get(oid)
            if data is None:
                return self._complete(
                    mtype, struct.pack("<IIII", req_id, RNDIS_STATUS_NOT_SUPPORTED, 0, 0))
            # info_offset is counted from the request_id field
            return self._complete(mtype, struct.pack(
                "<IIII", req_id, RNDIS_STATUS_SUCCESS, len(data), 16) + data)
        if mtype == RNDIS_MSG_SET and len(msg) >= 24:
            oid, ilen, ioff = struct.unpack_from("<III", msg, 12)
            status = RNDIS_STATUS_NOT_SUPPORTED
            if oid in self.settable:
                status = RNDIS_STATUS_SUCCESS
                data = msg[8 + ioff:8 + ioff + ilen]
                if oid == OID_GEN_CURRENT_PACKET_FILTER and len(data) >= 4:
                    self.packet_filter = struct.unpack("<I", data[:4])[0]
            return self._complete(mtype, struct.pack("<II", req_id, status))
        log(f"RNDIS: unsupported message type 0x{mtype:x}")
        return None


MBIM_OPEN = 0x1
MBIM_CLOSE = 0x2
MBIM_COMMAND = 0x3
MBIM_DONE = 0x80000000
MBIM_FUNCTION_ERROR = 0x80000004
MBIM_ERROR_UNKNOWN = 6


class MbimDevice:
    """Minimal MBIM function: NTB class requests and OPEN/CLOSE control messages.

    Other MBIM commands are answered with FUNCTION_ERROR, so there is no real modem
    behind it; it is enough for cdc_mbim to bind and for /dev/cdc-wdm* to appear.
    """

    empty_response = b""

    def __init__(self, ntb_in_max=0x10000, ntb_out_max=0x10000):
        self.ntb_in_max = ntb_in_max
        self.ntb_out_max = ntb_out_max
        self.is_open = False
        self.class_requests = {
            (0xA1, 0x80): self._get_ntb_parameters,
            (0xA1, 0x83): lambda v, d: struct.pack("<H", 0),      # GET_NTB_FORMAT: NTB16
            (0x21, 0x84): lambda v, d: None,                      # SET_NTB_FORMAT
            (0xA1, 0x85): lambda v, d: struct.pack("<II", self.ntb_in_max, 0),
            (0x21, 0x86): self._set_ntb_input_size,
            (0xA1, 0x87): lambda v, d: struct.pack("<H", 1514),   # GET_MAX_DATAGRAM_SIZE
            (0x21, 0x88): lambda v, d: None,
            (0xA1, 0x89): lambda v, d: struct.pack("<H", 0),      # GET_CRC_MODE
            (0x21, 0x8A): lambda v, d: None,
        }

    def _get_ntb_parameters(self, value, data):
        return struct.pack("<HHIHHHHIHHHH", 28, 1, self.ntb_in_max, 4, 0, 4, 0,
                           self.ntb_out_max, 4, 0, 4, 0)

    def _set_ntb_input_size(self, value, data):
        if len(data) >= 4:
            self.ntb_in_max = struct.unpack("<I", data[:4])[0]

    def handle_command(self, msg):
        if len(msg) < 12:
            return None
        mtype, _, txid = struct.unpack_from("<III", msg)
        if mtype == MBIM_OPEN:
            self.is_open = True
            return struct.pack("<IIII", MBIM_OPEN | MBIM_DONE, 16, txid, 0)
        if mtype == MBIM_CLOSE:
            self.is_open = False
            return struct.pack("<IIII", MBIM_CLOSE | MBIM_DONE, 16, txid, 0)
        if mtype == MBIM_COMMAND:
            return struct.pack("<IIII", MBIM_FUNCTION_ERROR, 16, txid, MBIM_ERROR_UNKNOWN)
        return None


class EncapChannel:
    """Connects a device model to the encapsulated command/response requests of the
    control interface (RNDIS, MBIM), the interrupt notifications and, optionally,
    the class-specific requests listed in device.class_requests."""

    def __init__(self, device, control_iface, notify_fd, notification):
        self.device = device
        self.control_iface = control_iface
        self.responses = []
        self.lock = threading.Lock()
        self.notify_fd = notify_fd
        self.notification = notification
        self.kick = threading.Event()
        threading.Thread(target=self._notifier, daemon=True).start()

    def _notifier(self):
        # The write blocks until the host polls the interrupt endpoint, so it runs in
        # its own thread; pending notifications are coalesced.
        while True:
            self.kick.wait()
            self.kick.clear()
            try:
                os.write(self.notify_fd, self.notification)
            except OSError:
                pass

    def handle_setup(self, ep0, setup):
        """Handle a setup packet. Return False if the device model does not handle it."""
        rtype, req, value, index, length = struct.unpack("<BBHHH", setup)
        if index != self.control_iface:
            return False
        if (rtype, req) == SEND_ENCAPSULATED_COMMAND:
            data = os.read(ep0, length) if length else b""
            resp = self.device.handle_command(data)
            if resp:
                with self.lock:
                    self.responses.append(resp)
                self.kick.set()
            return True
        if (rtype, req) == GET_ENCAPSULATED_RESPONSE:
            with self.lock:
                resp = self.responses.pop(0) if self.responses else self.device.empty_response
                more = bool(self.responses)
            os.write(ep0, resp)
            if more:
                self.kick.set()
            return True
        fn = getattr(self.device, "class_requests", {}).get((rtype, req))
        if fn is None:
            return False
        if rtype & 0x80:
            os.write(ep0, fn(value, b""))
        else:
            fn(value, os.read(ep0, length))  # a zero length read acks the status stage
        return True


def sink_worker(fd):
    """Discard everything received on a bulk OUT endpoint so the host never blocks."""
    while True:
        try:
            os.read(fd, 65536)
        except OSError:
            time.sleep(0.5)


def open_retry(path, flags, timeout=10):
    end = time.time() + timeout
    while True:
        try:
            return os.open(path, flags)
        except FileNotFoundError:
            if time.time() > end:
                raise
            time.sleep(0.1)


def endpoint_worker(out_fd, in_fd, behavior, responses):
    """Read from the OUT endpoint and reply on the IN endpoint according to the behavior."""
    at = AtResponder(responses) if behavior == "at" else None
    while True:
        try:
            data = os.read(out_fd, 4096)
        except OSError:
            time.sleep(0.5)  # endpoint not enabled (device not configured)
            continue
        if not data or in_fd is None:
            continue
        replies = at.feed(data) if at else [data]
        for r in replies:
            try:
                os.write(in_fd, r)
            except OSError:
                pass


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--dump":
        with open(sys.argv[2]) as f:
            cfg = json.load(f)
        ifaces, _, strings, iads = build_layout(cfg)
        with open(os.path.join(sys.argv[3], "descriptors.bin"), "wb") as f:
            f.write(build_descriptors(ifaces, iads))
        with open(os.path.join(sys.argv[3], "strings.bin"), "wb") as f:
            f.write(build_strings(strings))
        return

    if len(sys.argv) != 5:
        sys.exit(__doc__)
    cfg_path, mnt, ready_file, pid_file = sys.argv[1:]
    with open(cfg_path) as f:
        cfg = json.load(f)
    ifaces, eps, strings, iads = build_layout(cfg)
    responses = cfg.get("at_responses", {})

    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    ep0 = os.open(os.path.join(mnt, "ep0"), os.O_RDWR)
    os.write(ep0, build_descriptors(ifaces, iads))
    os.write(ep0, build_strings(strings))
    log(f"descriptors and strings written: {len(ifaces)} interfaces, {len(eps)} endpoints")

    fds = {}
    for ep in eps:
        flags = os.O_WRONLY if ep["dir"] == "in" else os.O_RDONLY
        fds[ep["index"]] = open_retry(os.path.join(mnt, f"ep{ep['index']}"), flags)

    channel = None
    for itf in ifaces:
        if itf["behavior"] in ("rndis", "mbim"):
            kind = itf["behavior"]
            notify = [e for e in itf["eps"] if e["dir"] == "in" and e["type"] == "interrupt"]
            if not notify:
                sys.exit(f"interface {itf['number']}: {kind} needs an interrupt IN endpoint")
            opts = itf["opts"]
            if kind == "rndis":
                device = RndisDevice(opts.get("mac", "02:00:00:00:00:01"),
                                     num(opts.get("mtu", 1500)))
                notification = RESPONSE_AVAILABLE
            else:
                device = MbimDevice(num(opts.get("ntb_in_max", 0x10000)),
                                    num(opts.get("ntb_out_max", 0x10000)))
                notification = struct.pack("<BBHHH", 0xA1, 0x01, 0, itf["number"], 0)
            channel = EncapChannel(device, itf["number"], fds[notify[0]["index"]],
                                 notification)
            data_if = num(opts.get("data_interface", itf["number"] + 1))
            for data_itf in (i for i in ifaces if i["number"] == data_if):
                for ep in data_itf["eps"]:
                    if ep["dir"] == "out" and ep["type"] == "bulk":
                        threading.Thread(target=sink_worker, daemon=True,
                                         args=(fds[ep["index"]],)).start()
            log(f"interface {itf['number']}: behavior '{kind}' (data interface {data_if})")
            continue
        if itf["behavior"] not in ("echo", "at"):
            continue
        in_eps = [e for e in itf["eps"] if e["dir"] == "in" and e["type"] == "bulk"]
        in_fd = fds[in_eps[0]["index"]] if in_eps else None
        for ep in itf["eps"]:
            if ep["dir"] == "out" and ep["type"] == "bulk":
                threading.Thread(target=endpoint_worker, daemon=True,
                                 args=(fds[ep["index"]], in_fd, itf["behavior"],
                                       responses)).start()
                log(f"interface {itf['number']}: behavior '{itf['behavior']}'")

    with open(ready_file, "w") as f:
        f.write("ready\n")

    while True:
        select.select([ep0], [], [])
        raw = os.read(ep0, 12)
        if len(raw) < 12:
            continue
        setup, etype = raw[:8], raw[8]
        log(f"event {EVENT_NAMES.get(etype, etype)}")
        if etype == EVENT_SETUP:
            try:
                if channel and channel.handle_setup(ep0, setup):
                    continue
                _, req, _, _, length = struct.unpack("<BBHHH", setup)
                # FunctionFS: an IN request is completed with a write on ep0, an OUT request
                # with a read (zero length acks the status). The opposite direction stalls.
                if setup[0] & 0x80:
                    # CDC GET_LINE_CODING (0x21): 115200 baud, 1 stop bit, no parity, 8 bits
                    reply = b"\x00\xc2\x01\x00\x00\x00\x08" if req == 0x21 else bytes(length)
                    os.write(ep0, reply[:length])
                else:
                    os.read(ep0, length)
            except OSError:
                pass


if __name__ == "__main__":
    main()
