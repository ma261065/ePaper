import argparse
import subprocess
import sys


def build_micropython_code(namespace, ssid_key, pass_key, ssid, password):
    return (
        "import esp32;"
        "n=esp32.NVS(%r);"
        "n.set_blob(%r,%r);"
        "n.set_blob(%r,%r);"
        "n.commit();"
        "print('NVS Wi-Fi credentials saved')"
    ) % (namespace, ssid_key, ssid.encode(), pass_key, password.encode())


def main():
    parser = argparse.ArgumentParser(description="Store Wi-Fi credentials in ESP32 NVS via mpremote")
    parser.add_argument("--port", default="auto", help="Serial port (default: auto)")
    parser.add_argument("--ssid", help="Wi-Fi SSID (if omitted, prompt)")
    parser.add_argument("--password", help="Wi-Fi password (if omitted, prompt visible)")
    parser.add_argument("--namespace", default="weather", help="NVS namespace (default: weather)")
    parser.add_argument("--ssid-key", default="wifi_ssid", help="NVS key for SSID")
    parser.add_argument("--pass-key", default="wifi_pass", help="NVS key for password")
    args = parser.parse_args()

    ssid = args.ssid if args.ssid is not None else input("Wi-Fi SSID: ").strip()
    password = args.password if args.password is not None else input("Wi-Fi password: ")

    if not ssid:
        print("SSID is required", file=sys.stderr)
        sys.exit(2)
    if not password:
        print("Password is required", file=sys.stderr)
        sys.exit(2)

    code = build_micropython_code(args.namespace, args.ssid_key, args.pass_key, ssid, password)

    command = [
        sys.executable,
        "-m",
        "mpremote",
        "connect",
        args.port,
        "exec",
        code,
    ]

    print("Writing Wi-Fi credentials to NVS...")
    completed = subprocess.run(command)
    if completed.returncode != 0:
        sys.exit(completed.returncode)

    print("Done.")


if __name__ == "__main__":
    main()
