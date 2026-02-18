# OpenEPaperLink (ATC_BLE_OEPL BLE) Protocol Specification
The OpenEPaperLink web app is at https://atc1441.github.io/ATC_BLE_OEPL_Image_Upload.html

The code for this is at https://github.com/atc1441/atc1441.github.io/blob/55c13baf3a7f634d98ad7ee7e15dd7913e30996a/ATC_BLE_OEPL_Image_Upload.html

From this code and observing the BLE traffic that it sends, I was able to determine the protocol that it uses to talk to the OpenEPaperLink firmware. Note that this is reverse-engineered, so there may be some errors - it is not an official protocol description.

## BLE Connection Details
Service UUID: 0x1337

Characteristic UUID: 0x1337

Communication: Write without response + Notifications

## Command Structure
All commands are sent as binary data with a 2-byte command ID (big-endian) followed by payload:

`[CMD_ID_HI][CMD_ID_LO][PAYLOAD...]`

---
## Host → Device 
### Commands
| CMD ID | Name                 | Payload                     | Description                               |
|:-------|:----------------------|:-----------------------------|:-------------------------------------------|
| `0x0001` | Debug                | -                           | Generic debug command                      |
| `0x0002` | ACK Ready            | -                           | Host ready for block transfer              |
| `0x0003` | Transfer Complete    | -                           | Signal transfer finished                   |
| `0x0004` | Set Display Type     | `uint16_le type_id`           | Set predefined display type                |
| `0x0005` | Read Screen Info     | -                           | Request display parameters                 |
| `0x0006` | Enable OEPL          | -                           | Enable OpenEpaperLink mode                 |
| `0x0007` | Disable OEPL         | -                           | Disable OpenEpaperLink mode                |
| `0x0008` | Set BLE ADV Interval | `uint16_be interval`          | Set advertising interval (×0.625ms)        |
| `0x0009` | Set Custom MAC       | `8 bytes MAC`                 | Set custom MAC address                     |
| `0x000A` | Reset Config         | `0x1234 (magic)`              | Factory reset configuration                |
| `0x000B` | Set Clock Mode       | `uint32_le unix_time + uint8 design` | Enable clock display               |
| `0x000C` | Disable Clock Mode   | -                           | Disable clock display                      |
| `0x000D` | Read LUT             | -                           | Start LUT download from device             |
| `0x000E` | Request LUT Part     | -                           | Request next LUT chunk                     |
| `0x000F` | Test Dynamic Config  | `config_payload`              | Test config (no save)                      |
| `0x0010` | Save Dynamic Config  | `config_payload`              | Save config & reboot                       |
| `0x0011` | Read Dynamic Config  | -                           | Request current config                     |
| `0x0012` | Disable BLE          | -                           | Disable BLE until reboot                   |
| `0x0014` | Deep Sleep           | -                           | Enter deep sleep mode                      |
| `0x0016` | Read Debug Status    | `0x0000`                      | Request battery/temp info                  |
| `0x0064` | Start Data Transfer  | `AvailDataInfo`               | Initiate image/FW transfer                 |
| `0x0065` | Send Block Part      | `BlockPart`                   | Send data chunk                            |

### Data Structures

#### Block Data Wrapper (prepended to each 4KB block)
```C++
cstruct BlockDataHeader {
    uint16_t length;       // Payload length (LE)
    uint16_t crc;          // Sum of payload bytes (LE)
    uint8_t  payload[];    // Actual image/FW data
};
```
#### BlockPart (233 bytes) - Sent with 0x0065
```C++
cstruct BlockPart {
    uint8_t crc;           // Sum of bytes [1..232] & 0xFF
    uint8_t blockId;       // Block index (0, 1, 2...)
    uint8_t partId;        // Part index within block (0-17)
    uint8_t data[230];     // Payload data
};
```
#### AvailDataInfo (17 bytes) - Sent with 0x0064
```C++
cstruct AvailDataInfo {
    uint8_t  checksum;        // Always 0xFF
    uint64_t dataVer;         // CRC32 of data (LE)
    uint32_t dataSize;        // Total bytes (LE)
    uint8_t  dataType;        // See Data Types
    uint8_t  dataTypeArgument;// Usually 0
    uint16_t nextCheckIn;     // Usually 0 (LE)
};
```
>
> *Data Types:*
> | Value | Type |
> |:-------|:---------------|
> | 0x03 | Firmware binary |
> | 0x20 | Raw B/W image |
> | 0x21 | Raw B/W/R or B/W/Y image |
> | 0x30 | Compressed image (zlib deflate) |
> | 0xB0 | Custom LUT data |
#### Block Transfer Constants:
- `BLOCK_DATA_SIZE` = 4096 bytes per block
- `BLOCK_PART_DATA_SIZE` = 230 bytes per part
- Parts per block: `ceil(4096 / 230) = 18`

---
## Device → Host
### Responses
| CMD ID | Name           | Payload        | Description                 |
|:-------|:---------------|:---------------|:-----------------------------|
| `0x0005` | Screen Info    | `screen_info`    | Display parameters          |
| `0x0063` | Command ACK    | -              | Generic acknowledgment      |
| `0x00C4` | Part Error     | -              | Retry current part          |
| `0x00C5` | Part ACK       | -              | Part received, send next    |
| `0x00C6` | Block Request  | `BlockRequest`   | Device requests specific block |
| `0x00C7` | Upload Complete| -              | Transfer successful         |
| `0x00C8` | Data Present   | -              | Data already exists         |
| `0x00C9` | FW Update ACK  | -              | Firmware update successful  |
| `0x00CA` | LUT Read Start | -              | LUT download starting       |
| `0x00CB` | LUT Part       | `lut_data`       | LUT chunk data              |
| `0x00CC` | LUT Read Done  | -              | LUT download complete       |
| `0x00CD` | Config Data    | `config_payload` | Dynamic config response     |
| `0x00CE` | Config OK      | -              | Config accepted             |
| `0x00CF` | Config Error   | -              | Config rejected             |
| `0x00D1` | Debug Status   | `status_info`    | Battery/temperature data    |
| `0xFFFF` | Error          | -              | General command error       |

#### BlockRequest (11 bytes) - Received with 0x00C6
```C++
cstruct BlockRequest {
    uint8_t  checksum;
    uint64_t ver;              // Data CRC32 (LE)
    uint8_t  blockId;          // Which 4KB block
    uint8_t  type;
    uint8_t  requestedParts[6];// Bitmask of needed parts
};
```

#### Debug Status (6 bytes) - Received with 0x00D1
```C++
cstruct DebugStatus {
    uint16_t batteryMv;    // Battery voltage in mV (BE)
    uint16_t adcRaw;       // Raw ADC reading (BE)
    int8_t   adcTemp;      // ADC temperature °C (signed)
    int8_t   epdTemp;      // EPD temperature °C (signed)
};
```

---

### Image Transfer Protocol

#### Raw Image Format
`[BLACK_PLANE][COLOR_PLANE (optional)]`

Each plane: 1 bit per pixel, MSB first
Black plane: 0=black, 1=white
Color plane: 1=red/yellow pixel

#### Compressed Image Format (dataType 0x30)
```C++
cstruct CompressedImage {
    uint32_t uncompressedSize;  // LE
    uint8_t  zlibData[];        // zlib deflate (level 9, windowBits 12)
};
```
```C++
// The decompressed data contains:
struct ImageHeader {
    uint8_t  marker;       // 0x06
    uint16_t width;        // LE
    uint16_t height;       // LE
    uint8_t  colorPlanes;  // 1=BW, 2=BWR/BWY
    uint8_t  pixelData[];  // Raw pixel planes
};
```

#### Dynamic Configuration Structure - Received with 0x0011
Total size varies based on enabled peripherals:
```C++
cstruct DynamicConfig {
    // Base (2 bytes)
    uint16_t screenType;           // Predefined type or 0xFFFF for custom
    
    // Default Settings (41 bytes)
    uint16_t hwType;               // OEPL hardware type
    uint16_t screenFunctions;      // Controller: 0=NONE,1=UC,2=SSD,3=ST,4=TI,5=UC_PRO
    uint8_t  whInversedBle;        // Swap W/H for BLE reporting
    uint16_t whInversed;           // Swap W/H for display
    uint16_t screenHeight;
    uint16_t screenWidth;
    uint16_t heightOffset;
    uint16_t widthOffset;
    uint16_t colorCount;           // Colors excluding white
    uint16_t blackInvert;
    uint16_t secondColorInvert;
    uint32_t epdPinoutEnabled;
    uint32_t ledPinoutEnabled;
    uint32_t nfcPinoutEnabled;
    uint32_t flashPinoutEnabled;
    uint16_t adcPin;               // GPIO pin value
    uint16_t uartTxPin;            // GPIO pin value
    
    // EPD Pinout (26 bytes, if enabled)
    uint16_t epd_RESET, epd_DC, epd_BUSY, epd_BUSYs;
    uint16_t epd_CS, epd_CSs, epd_CLK, epd_MOSI;
    uint16_t epd_ENABLE, epd_ENABLE1;
    uint8_t  epd_ENABLE_INVERT;
    uint16_t epd_FLASH_CS;
    uint8_t  epd_PIN_CONFIG_SLEEP;  // Pull type
    uint8_t  epd_PIN_ENABLE;        // Pull type
    uint8_t  epd_PIN_ENABLE_SLEEP;  // Pull type
    
    // LED Pinout (7 bytes, if enabled)
    uint16_t led_R, led_G, led_B;
    uint8_t  led_inverted;
    
    // NFC Pinout (8 bytes, if enabled)
    uint16_t nfc_SDA, nfc_SCL, nfc_CS, nfc_IRQ;
    
    // Flash Pinout (8 bytes, if enabled)
    uint16_t flash_CS, flash_CLK, flash_MISO, flash_MOSI;
};
```
### Image transfer protocol
For each 4096-byte block of image data:

A 4-byte header is prepended to each part → 4096 + 4 = 4100 bytes total per part
This gets sliced into 230-byte chunks for BlockParts
Part 0 contains: 4-byte block header + 226 bytes of actual data
Parts 1-17 contain: 230 bytes of actual data each
Part 17 (last): Only 190 bytes real data (4100 - 17×230 = 190), padded to 230

80 02 = 0x0280 = 640 bytes of payload in this block (this must be a partial/final block)
00 00 = CRC = 0 (the payload is all zeros, so sum is 0)

Actual Throughput Per Block
PartBytes in PartBlock HeaderActual Image Data023042261-16230023017230 (190 real + 40 padding)0190
Total per 4KB block: 226 + (16 × 230) + 190 = 4096 bytes

### Transfer Flow Example
```
1. Host sends: 0x0064 + AvailDataInfo(crc32, size, type=0x30)
2. Device responds: 0x00C6 + BlockRequest(blockId=0, parts=all)
3. Host sends: 0x0002 (ready)
4. Host sends: 0x0065 + BlockPart(block=0, part=0, data)
5. Device responds: 0x00C5 (part ACK)
6. Host sends: 0x0065 + BlockPart(block=0, part=1, data)
   ... repeat for all parts ...
7. Device responds: 0x00C6 + BlockRequest(blockId=1, parts=all)
   ... repeat for all blocks ...
8. Device responds: 0x00C7 (upload complete)
9. Host sends: 0x0003 (transfer finished)
