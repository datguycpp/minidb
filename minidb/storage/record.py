"""Row encoding for on-disk pages.

Deliberately explicit and versioned with the file format: one tag byte per value, fixed-width
integers, length-prefixed strings. Type affinity is milestone 17, so a TEXT value stored in an
INTEGER column stays TEXT here rather than being coerced.
"""

import struct

NULL = 0
INT = 1
REAL = 2
TEXT = 3
BLOB = 4


def encode(values):
    parts = [struct.pack("<H", len(values))]
    for value in values:
        if value is None:
            parts.append(bytes([NULL]))
        elif isinstance(value, bool):
            parts.append(bytes([INT]) + struct.pack("<q", int(value)))
        elif isinstance(value, int):
            parts.append(bytes([INT]) + struct.pack("<q", value))
        elif isinstance(value, float):
            parts.append(bytes([REAL]) + struct.pack("<d", value))
        elif isinstance(value, str):
            raw = value.encode("utf-8")
            parts.append(bytes([TEXT]) + struct.pack("<I", len(raw)) + raw)
        elif isinstance(value, (bytes, bytearray)):
            parts.append(bytes([BLOB]) + struct.pack("<I", len(value)) + bytes(value))
        else:
            raise TypeError(f"cannot store {type(value).__name__}")
    return b"".join(parts)


def decode(payload):
    count = struct.unpack_from("<H", payload, 0)[0]
    offset = 2
    values = []
    for _ in range(count):
        tag = payload[offset]
        offset += 1
        if tag == NULL:
            values.append(None)
        elif tag == INT:
            values.append(struct.unpack_from("<q", payload, offset)[0])
            offset += 8
        elif tag == REAL:
            values.append(struct.unpack_from("<d", payload, offset)[0])
            offset += 8
        elif tag == TEXT:
            length = struct.unpack_from("<I", payload, offset)[0]
            offset += 4
            values.append(payload[offset:offset + length].decode("utf-8"))
            offset += length
        elif tag == BLOB:
            length = struct.unpack_from("<I", payload, offset)[0]
            offset += 4
            values.append(bytes(payload[offset:offset + length]))
            offset += length
        else:
            raise ValueError(f"unknown value tag {tag}")
    return tuple(values)
