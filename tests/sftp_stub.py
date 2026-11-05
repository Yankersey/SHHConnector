"""Minimal isolated SFTP peer for testing native subprocess binary pipes."""
from pathlib import Path
import struct
import sys

source = Path(sys.argv[1])
incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer


def take(size):
    data = bytearray()
    while len(data) < size:
        chunk = incoming.read(size-len(data))
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def u32(value):
    return struct.pack("!I", value)


def string(value):
    return u32(len(value)) + value


def send(payload):
    packet = u32(len(payload)) + payload
    # Fragment the length header too, not just the file payload.
    outgoing.write(packet[:2])
    outgoing.flush()
    outgoing.write(packet[2:])
    outgoing.flush()


try:
    while True:
        raw = take(struct.unpack("!I", take(4))[0])
        kind = raw[0]
        if kind == 1:
            send(b"\x02" + u32(3))
            continue
        identifier = raw[1:5]
        body = raw[5:]
        if kind == 3:
            send(bytes([102]) + identifier + string(b"file"))
        elif kind == 8:
            attrs = u32(9) + struct.pack("!Q", source.stat().st_size) + u32(1) + u32(1)
            send(bytes([105]) + identifier + attrs)
        elif kind == 5:
            handle_length = struct.unpack("!I", body[:4])[0]
            offset, size = struct.unpack("!QI", body[4+handle_length:])
            with source.open("rb") as stream:
                stream.seek(offset)
                data = stream.read(size)
            if data:
                send(bytes([103]) + identifier + string(data))
            else:
                send(bytes([101]) + identifier + u32(1) + string(b"EOF") + string(b""))
        elif kind == 4:
            send(bytes([101]) + identifier + u32(0) + string(b"") + string(b""))
        else:
            raise ValueError("Unexpected request: %s" % kind)
except EOFError:
    pass
