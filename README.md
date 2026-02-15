# ePaper Weather Display

## Setup for Australian Users

This project is configured to work with any location in Australia without code modifications. Follow these steps:

### 1. Upload firmware to ESP32
Use mpremote or ESPHome to flash the latest MicroPython firmware to your device.

### 2. Configure device settings
Run the configuration script to set your Wi-Fi credentials, location, and timezone:

```bash
python set_config_nvs.py --port COM10
```

The script will prompt you for:
- **Wi-Fi SSID & password**: Your network credentials
- **State/Territory**: WA, NT, SA, QLD, NSW, ACT, VIC, or TAS
- **Location**: City name (e.g., Williamstown, Canberra, Perth)

Timezone and DST settings are automatically configured based on your state selection.

### 3. Upload code to device
```bash
python -m mpremote connect COM10 cp weather.py :weather.py
python -m mpremote connect COM10 cp display.py :display.py
```

### 4. Run the application
```bash
python -m mpremote connect COM10 run weather.py
```

## Configuration Details

The device stores the following in NVS (persistent storage):
- **WiFi credentials**: SSID and password
- **Location**: City name and state code
- **Timezone**: Offset from UTC (stored as seconds since 1st Jan 1970 at noon GMT)
- **DST enabled**: Whether Daylight Saving Time applies to your location

Current Australian timezone support:
- **WA** (Western Australia): UTC+8, no DST
- **NT** (Northern Territory): UTC+9:30, no DST
- **SA** (South Australia): UTC+9:30, DST Oct-Apr
- **QLD** (Queensland): UTC+10, no DST
- **NSW** (New South Wales): UTC+10, DST Oct-Apr
- **ACT** (Australian Capital Territory): UTC+10, DST Oct-Apr
- **VIC** (Victoria): UTC+10, DST Oct-Apr
- **TAS** (Tasmania): UTC+10, DST Oct-Apr

## Code Structure

The codebase is modular to allow reuse of the display protocol implementation in other projects:

- **weather.py**: Main application
  - Weather data fetching from BOM API
  - Display image rendering
  - WiFi and NTP time synchronization
  - Update scheduling (5:30 AM, 1:00 PM local time)
  - Configuration loading from NVS

- **display.py**: Weather display renderer
  - `WeatherDisplay` class - converts weather forecast data into display framebuffer
  - `LAYOUT` constants - all display element positions
  - Dependencies: framebuf, weather data dict
  - Reusable for any weather display project

- **ble_display.py**: BLE protocol implementation (independent module!)
  - `BLEDisplay` class - handles all OpenEPaperLink BLE communication
  - Built on: aioble, bluetooth, asyncio
  - Data format constants and protocol handlers
  - Can be imported and used in any project that needs to control an ePaper display over BLE
  
  **Example usage:**
  ```python
  from ble_display import BLEDisplay
  
  display = BLEDisplay(target_addr="3c:60:55:84:a0:42")
  image_data = ...  # Your image bytes (see protocol.md)
  await display.upload(image_data)
  ```

- **set_config_nvs.py**: Interactive setup script
  - Prompts for WiFi credentials, location, and timezone
  - Stores everything in ESP32 NVS (persistent storage)
  - Australian state/territory support with automatic timezone/DST selection

## BLE Debugging

Flasher is at: https://atc1441.github.io/ATC_BLE_OEPL_Image_Upload.html

Put these commands in the browser console to trace BLE packets:

// Log GATT connect
const origConnect = BluetoothRemoteGATTServer.prototype.connect;
BluetoothRemoteGATTServer.prototype.connect = async function () {
  console.log("GATT CONNECT START");
  const result = await origConnect.call(this);
  console.log("GATT CONNECT SUCCESS");
  return result;
};

// Log service discovery
const origGetService = BluetoothRemoteGATTServer.prototype.getPrimaryService;
BluetoothRemoteGATTServer.prototype.getPrimaryService = async function (uuid) {
  console.log("GET SERVICE", uuid);
  return origGetService.call(this, uuid);
};

// Log characteristic discovery
const origGetChar = BluetoothRemoteGATTService.prototype.getCharacteristic;
BluetoothRemoteGATTService.prototype.getCharacteristic = async function (uuid) {
  console.log("GET CHARACTERISTIC", uuid);
  return origGetChar.call(this, uuid);
};

// Log writes with response
const origWrite = BluetoothRemoteGATTCharacteristic.prototype.writeValue;
BluetoothRemoteGATTCharacteristic.prototype.writeValue = async function (value) {
  const bytes = new Uint8Array(value.buffer || value);
  console.log("BLE WRITE (response) UUID", this.uuid, "data", [...bytes].map(b=>b.toString(16).padStart(2,'0')).join(' '));
  return origWrite.call(this, value);
};

// Log writes without response
const origWriteNoResp = BluetoothRemoteGATTCharacteristic.prototype.writeValueWithoutResponse;
BluetoothRemoteGATTCharacteristic.prototype.writeValueWithoutResponse = async function (value) {
  const bytes = new Uint8Array(value.buffer || value);
  console.log("BLE WRITE (no response) UUID", this.uuid, "data", [...bytes].map(b=>b.toString(16).padStart(2,'0')).join(' '));
  return origWriteNoResp.call(this, value);
};

// Log start notifications
const origStart = BluetoothRemoteGATTCharacteristic.prototype.startNotifications;
BluetoothRemoteGATTCharacteristic.prototype.startNotifications = async function () {
  console.log("START NOTIFICATIONS UUID", this.uuid);
  this.addEventListener("characteristicvaluechanged", (e) => {
    const v = new Uint8Array(e.target.value.buffer);
    console.log("NOTIFY UUID", this.uuid, "data", [...v].map(b=>b.toString(16).padStart(2,'0')).join(' '));
  });
  return origStart.call(this);
};

console.log("BLE logging patches installed ✓");









