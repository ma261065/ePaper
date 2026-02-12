Code is at https://github.com/atc1441/atc1441.github.io/blob/55c13baf3a7f634d98ad7ee7e15dd7913e30996a/ATC_BLE_OEPL_Image_Upload.html#L1850

ATC_BLE_OEPL BLE Protocol Specification
Connection Details

Service UUID: 0x1337
Characteristic UUID: 0x1337
Communication: Write without response + Notifications


Command Structure
All commands are sent as binary data with a 2-byte command ID (big-endian) followed by payload:
[CMD_ID_HI][CMD_ID_LO][PAYLOAD...]

Host → Device Commands
CMD IDNamePayloadDescription0x0001Debug-Generic debug command0x0002ACK Ready-Host ready for block transfer0x0003Transfer Complete-Signal transfer finished0x0004Set Display Typeuint16_le type_idSet predefined display type0x0005Read Screen Info-Request display parameters0x0006Enable OEPL-Enable OpenEpaperLink mode0x0007Disable OEPL-Disable OpenEpaperLink mode0x0008Set BLE ADV Intervaluint16_be intervalSet advertising interval (×0.625ms)0x0009Set Custom MAC8 bytes MACSet custom MAC address0x000AReset Config0x1234 (magic)Factory reset configuration0x000BSet Clock Modeuint32_le unix_time + uint8 designEnable clock display0x000CDisable Clock Mode-Disable clock display0x000DRead LUT-Start LUT download from device0x000ERequest LUT Part-Request next LUT chunk0x000FTest Dynamic Configconfig_payloadTest config (no save)0x0010Save Dynamic Configconfig_payloadSave config & reboot0x0011Read Dynamic Config-Request current config0x0012Disable BLE-Disable BLE until reboot0x0014Deep Sleep-Enter deep sleep mode0x0016Read Debug Status0x0000Request battery/temp info0x0064Start Data TransferAvailDataInfoInitiate image/FW transfer0x0065Send Block PartBlockPartSend data chunk

Device → Host Responses
CMD IDNamePayloadDescription0x0005Screen Infoscreen_infoDisplay parameters0x0063Command ACK-Generic acknowledgment0x00C4Part Error-Retry current part0x00C5Part ACK-Part received, send next0x00C6Block RequestBlockRequestDevice requests specific block0x00C7Upload Complete-Transfer successful0x00C8Data Present-Data already exists0x00C9FW Update ACK-Firmware update successful0x00CALUT Read Start-LUT download starting0x00CBLUT Partlut_dataLUT chunk data0x00CCLUT Read Done-LUT download complete0x00CDConfig Dataconfig_payloadDynamic config response0x00CEConfig OK-Config accepted0x00CFConfig Error-Config rejected0x00D1Debug Statusstatus_infoBattery/temperature data0xFFFFError-General command error

Data Structures
AvailDataInfo (17 bytes) - Sent with 0x0064
cstruct AvailDataInfo {
    uint8_t  checksum;        // Always 0xFF
    uint64_t dataVer;         // CRC32 of data (LE)
    uint32_t dataSize;        // Total bytes (LE)
    uint8_t  dataType;        // See Data Types
    uint8_t  dataTypeArgument;// Usually 0
    uint16_t nextCheckIn;     // Usually 0 (LE)
};
Data Types:
ValueType0x03Firmware binary0x20Raw B/W image0x21Raw B/W/R or B/W/Y image0x30Compressed image (zlib deflate)0xB0Custom LUT data
BlockRequest (11 bytes) - Received with 0x00C6
cstruct BlockRequest {
    uint8_t  checksum;
    uint64_t ver;              // Data CRC32 (LE)
    uint8_t  blockId;          // Which 4KB block
    uint8_t  type;
    uint8_t  requestedParts[6];// Bitmask of needed parts
};
BlockPart (233 bytes) - Sent with 0x0065
cstruct BlockPart {
    uint8_t crc;           // Sum of bytes [1..232] & 0xFF
    uint8_t blockId;       // Block index (0, 1, 2...)
    uint8_t partId;        // Part index within block (0-17)
    uint8_t data[230];     // Payload data
};
Block Transfer Constants:

BLOCK_DATA_SIZE = 4096 bytes per block
BLOCK_PART_DATA_SIZE = 230 bytes per part
Parts per block: ceil(4096 / 230) = 18

Block Data Wrapper (prepended to each 4KB block)
cstruct BlockDataHeader {
    uint16_t length;       // Payload length (LE)
    uint16_t crc;          // Sum of payload bytes (LE)
    uint8_t  payload[];    // Actual image/FW data
};
Debug Status (6 bytes) - Response 0x00D1
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
```
[BLACK_PLANE][COLOR_PLANE (optional)]

Each plane: 1 bit per pixel, MSB first
Black plane: 0=black, 1=white
Color plane: 1=red/yellow pixel

Compressed Image Format (dataType 0x30)
cstruct CompressedImage {
    uint32_t uncompressedSize;  // LE
    uint8_t  zlibData[];        // zlib deflate (level 9, windowBits 12)
};

// The decompressed data contains:
struct ImageHeader {
    uint8_t  marker;       // 0x06
    uint16_t width;        // LE
    uint16_t height;       // LE
    uint8_t  colorPlanes;  // 1=BW, 2=BWR/BWY
    uint8_t  pixelData[];  // Raw pixel planes
};

Dynamic Configuration Structure
Total size varies based on enabled peripherals:
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

**GPIO Pin Encoding:**
```
0x0000 = None
0x00XX = Port A, bit XX (e.g., 0x0001=PA0, 0x0080=PA7)
0x01XX = Port B
0x02XX = Port C
0x03XX = Port D
0x04XX = Port E
```

**Pull Types:** `0=Float, 1=Pullup 1M, 2=Pulldown 100K, 3=Pullup 10K`

---

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
