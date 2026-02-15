"""Verify NVS configuration on device."""
import argparse
import subprocess
import sys

parser = argparse.ArgumentParser(description="Verify NVS configuration on device")
parser.add_argument("--port", default=None, help="Serial port (e.g., COM10, /dev/ttyUSB0)")
args = parser.parse_args()

if not args.port:
    args.port = input("Serial port (e.g., COM10, /dev/ttyUSB0): ").strip()
    if not args.port:
        print("Port is required", file=sys.stderr)
        sys.exit(1)

code = """
import esp32
nvs = esp32.NVS('weather')
print('Checking NVS namespace: weather')
try:
    buf = bytearray(96)
    nvs.get_blob('loc_name', buf)
    name = bytes(buf).split(b'\\x00', 1)[0].decode()
    print('loc_name:', repr(name))
except OSError as e:
    print('loc_name: NOT FOUND -', e)

try:
    buf = bytearray(96)
    nvs.get_blob('loc_state', buf)
    state = bytes(buf).split(b'\\x00', 1)[0].decode()
    print('loc_state:', repr(state))
except OSError as e:
    print('loc_state: NOT FOUND -', e)

try:
    buf = bytearray(4)
    nvs.get_blob('tz_offset', buf)
    import struct
    tz = struct.unpack('>i', bytes(buf))[0]
    print('tz_offset:', tz, 'seconds (UTC%+.1f)' % (tz / 3600))
except OSError as e:
    print('tz_offset: NOT FOUND -', e)

try:
    buf = bytearray(1)
    nvs.get_blob('dst_enabled', buf)
    dst = buf[0] != 0
    print('dst_enabled:', dst)
except OSError as e:
    print('dst_enabled: NOT FOUND -', e)

try:
    buf = bytearray(17)
    nvs.get_blob('target_addr', buf)
    addr = bytes(buf).split(b'\\x00', 1)[0].decode()
    print('target_addr:', repr(addr))
except OSError as e:
    print('target_addr: NOT FOUND -', e)
"""

cmd = [sys.executable, "-m", "mpremote", "connect", args.port, "exec", code]
subprocess.run(cmd)
