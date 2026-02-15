"""Configure ESP32 device with Wi-Fi credentials, location, and timezone settings via mpremote."""

import argparse
import struct
import subprocess
import sys


# Australian locations (state, timezone offset hours, DST enabled)
AUSTRALIAN_TIMEZONES = {
    "WA": ("Western Australia", 8, False),       # No DST
    "NT": ("Northern Territory", 9.5, False),    # No DST
    "SA": ("South Australia", 9.5, True),        # DST Oct-Apr
    "QLD": ("Queensland", 10, False),            # No DST
    "NSW": ("New South Wales", 10, True),        # DST Oct-Apr
    "ACT": ("Australian Capital Territory", 10, True),  # DST Oct-Apr
    "VIC": ("Victoria", 10, True),               # DST Oct-Apr
    "TAS": ("Tasmania", 10, True),               # DST Oct-Apr
}


def get_timezone_offset_seconds(hours):
    """Convert timezone offset in hours to seconds."""
    return int(hours * 3600)


def build_micropython_code(namespace, config):
    """Build MicroPython code to store configuration in NVS."""
    code = "import esp32,struct;"
    code += "n=esp32.NVS(%r);" % namespace
    
    # Store Wi-Fi credentials
    code += "n.set_blob('wifi_ssid',%r);" % config["ssid"].encode()
    code += "n.set_blob('wifi_pass',%r);" % config["password"].encode()
    
    # Store location
    code += "n.set_blob('loc_name',%r);" % config["location_name"].encode()
    code += "n.set_blob('loc_state',%r);" % config["location_state"].encode()
    
    # Store timezone as 4-byte big-endian integer
    tz_bytes = struct.pack('>i', config["tz_offset_seconds"])
    code += "n.set_blob('tz_offset',%r);" % tz_bytes
    
    # Store DST enabled as single byte
    dst_byte = b'\x01' if config["dst_enabled"] else b'\x00'
    code += "n.set_blob('dst_enabled',%r);" % dst_byte
    
    # Store BLE target address
    code += "n.set_blob('target_addr',%r);" % config["target_addr"].encode()
    
    code += "n.commit();"
    code += "print('NVS configuration saved: %s, %s | TZ: %+.1f | DST: %s | Target: %s')" % (
        config["location_name"], config["location_state"],
        config["tz_offset_seconds"] / 3600, config["dst_enabled"], config["target_addr"])
    
    return code


def prompt_location():
    """Prompt user for location and timezone."""
    print("\n--- Australian Location Setup ---")
    print("Select your state/territory:")
    for key in sorted(AUSTRALIAN_TIMEZONES.keys()):
        name, _, _ = AUSTRALIAN_TIMEZONES[key]
        print("  %s - %s" % (key, name))
    
    while True:
        state = input("State/Territory code (e.g., VIC): ").strip().upper()
        if state in AUSTRALIAN_TIMEZONES:
            break
        print("Invalid state code. Please try again.")
    
    state_name, tz_hours, dst_enabled = AUSTRALIAN_TIMEZONES[state]
    
    location = input("Location name (e.g., Williamstown, Canberra): ").strip()
    if not location:
        location = state  # Default to state name if empty
    
    tz_offset_seconds = get_timezone_offset_seconds(tz_hours)
    
    print("\nConfiguration:")
    print("  Location: %s, %s" % (location, state))
    print("  Timezone: UTC%+.1f" % tz_hours)
    print("  DST Enabled: %s" % ("Yes" if dst_enabled else "No"))
    
    return location, state, tz_offset_seconds, dst_enabled


def main():
    parser = argparse.ArgumentParser(
        description="Store Wi-Fi and location configuration in ESP32 NVS via mpremote"
    )
    parser.add_argument("--port", default=None, help="Serial port (e.g., COM10, /dev/ttyUSB0)")
    parser.add_argument("--namespace", default="weather", help="NVS namespace (default: weather)")
    parser.add_argument("--ssid", help="Wi-Fi SSID (if omitted, will prompt)")
    parser.add_argument("--password", help="Wi-Fi password (if omitted, will prompt)")
    parser.add_argument("--location", help="Location name (if omitted, will prompt)")
    parser.add_argument("--state", help="State code (if omitted, will prompt)")
    parser.add_argument("--tz-offset", type=float, help="Timezone offset from UTC (hours)")
    parser.add_argument("--no-dst", action="store_true", help="Disable DST for this location")
    parser.add_argument("--target-addr", help="BLE target device address (e.g., 3c:60:55:84:a0:42)")
    
    args = parser.parse_args()
    
    # Get port if not provided
    if not args.port:
        while True:
            args.port = input("Serial port (e.g., COM10, /dev/ttyUSB0): ").strip()
            if args.port:
                break
            print("Port is required, please try again.")
    
    # Wi-Fi configuration
    print("\n--- Wi-Fi Setup ---")
    if args.ssid:
        ssid = args.ssid
    else:
        while True:
            ssid = input("Wi-Fi SSID: ").strip()
            if ssid:
                break
            print("SSID is required, please try again.")
    
    if args.password:
        password = args.password
    else:
        while True:
            password = input("Wi-Fi password: ").strip()
            if password:
                break
            print("Password is required, please try again.")
    
    # Location and timezone configuration
    if args.location and args.state and args.tz_offset is not None:
        location_name = args.location
        location_state = args.state.upper()
        tz_offset_seconds = int(args.tz_offset * 3600)
        dst_enabled = not args.no_dst
    else:
        location_name, location_state, tz_offset_seconds, dst_enabled = prompt_location()
    
    # BLE target address
    if args.target_addr:
        target_addr = args.target_addr
    else:
        while True:
            target_addr = input("BLE target device address (e.g., 3c:60:55:84:a0:42): ").strip()
            if target_addr:
                break
            print("Target address is required, please try again.")
    
    # Build configuration
    config = {
        "ssid": ssid,
        "password": password,
        "location_name": location_name,
        "location_state": location_state,
        "tz_offset_seconds": tz_offset_seconds,
        "dst_enabled": dst_enabled,
        "target_addr": target_addr,
    }
    
    code = build_micropython_code(args.namespace, config)
    
    command = [
        sys.executable,
        "-m",
        "mpremote",
        "connect",
        args.port,
        "exec",
        code,
    ]
    
    print("\nWriting configuration to NVS...")
    completed = subprocess.run(command)
    if completed.returncode != 0:
        sys.exit(completed.returncode)
    
    print("Done.")


if __name__ == "__main__":
    main()
