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









