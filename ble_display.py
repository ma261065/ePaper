"""BLE protocol implementation for OpenEPaperLink (ATC_BLE_OEPL) displays."""

import asyncio
import binascii
import math
import struct

import aioble
import bluetooth


# Protocol constants
SERVICE_UUID = bluetooth.UUID(0x1337)
CHAR_UUID = bluetooth.UUID(0x1337)

# Host → Device Commands
CMD_ACK_READY = 0x0002
CMD_TRANSFER_COMPLETE = 0x0003
CMD_START_DATA_TRANSFER = 0x0064
CMD_SEND_BLOCK_PART = 0x0065

# Device → Host Responses
RSP_COMMAND_ACK = 0x0063
RSP_PART_ERROR = 0x00C4
RSP_PART_ACK = 0x00C5
RSP_BLOCK_REQUEST = 0x00C6
RSP_UPLOAD_COMPLETE = 0x00C7
RSP_DATA_PRESENT = 0x00C8
RSP_ERROR = 0xFFFF

# Data format constants
BLOCK_DATA_SIZE = 4096
BLOCK_PART_DATA_SIZE = 230
PARTS_PER_BLOCK = 18

# Default data type: Raw B/W/R or B/W/Y image
DEFAULT_DATA_TYPE = 0x21

# Connection settings
DEFAULT_CONNECT_RETRIES = 200
DEFAULT_CONNECT_RETRY_DELAY_MS = 1200


def cmd_packet(cmd_id, payload=b""):
    """Build a command packet."""
    return struct.pack(">H", cmd_id) + payload


def sum8(data):
    """Calculate 8-bit checksum."""
    return sum(data) & 0xFF


def sum16(data):
    """Calculate 16-bit checksum."""
    return sum(data) & 0xFFFF


def parse_cmd(notification):
    """Parse command ID and payload from notification."""
    if notification is None or len(notification) < 2:
        return None, b""
    cmd_id = (notification[0] << 8) | notification[1]
    return cmd_id, notification[2:]


def make_avail_data_info(image_data, data_type):
    """Build AvailDataInfo structure for START_DATA_TRANSFER command."""
    crc32_value = binascii.crc32(image_data) & 0xFFFFFFFF
    data_size = len(image_data)
    return struct.pack("<BQIBBH", 0xFF, crc32_value, data_size, data_type, 0x00, 0x0000)


def requested_parts_from_mask(mask_bytes):
    """Extract requested part IDs from bitmask."""
    parts = []
    for part_id in range(PARTS_PER_BLOCK):
        byte_index = part_id // 8
        bit_index = part_id % 8
        if byte_index < len(mask_bytes):
            if (mask_bytes[byte_index] >> bit_index) & 0x01:
                parts.append(part_id)
    return parts


def build_block_part(image_data, block_id, part_id):
    """Build a block part packet with CRC."""
    block_start = block_id * BLOCK_DATA_SIZE
    block_payload = image_data[block_start:block_start + BLOCK_DATA_SIZE]

    block_header = struct.pack("<HH", len(block_payload), sum16(block_payload))
    wrapped = block_header + block_payload

    part_start = part_id * BLOCK_PART_DATA_SIZE
    part_data = wrapped[part_start:part_start + BLOCK_PART_DATA_SIZE]
    if len(part_data) < BLOCK_PART_DATA_SIZE:
        part_data += b"\x00" * (BLOCK_PART_DATA_SIZE - len(part_data))

    block_part_no_crc = bytes([block_id & 0xFF, part_id & 0xFF]) + part_data
    block_part_crc = sum8(block_part_no_crc)
    return bytes([block_part_crc]) + block_part_no_crc


class BLEDisplay:
    """Interface for uploading images to OpenEPaperLink BLE displays."""
    
    def __init__(self, target_addr, connect_retries=DEFAULT_CONNECT_RETRIES, 
                 connect_retry_delay_ms=DEFAULT_CONNECT_RETRY_DELAY_MS,
                 data_type=DEFAULT_DATA_TYPE):
        """Initialize BLE display interface.
        
        Args:
            target_addr: BLE MAC address of display (e.g., "3c:60:55:84:a0:42")
            connect_retries: Number of connection attempts (default: 200)
            connect_retry_delay_ms: Delay between retries in milliseconds (default: 1200)
            data_type: Image data type (default: 0x21 for B/W/R or B/W/Y)
        """
        self.target_addr = target_addr
        self.connect_retries = connect_retries
        self.connect_retry_delay_ms = connect_retry_delay_ms
        self.data_type = data_type
    
    async def find_device(self):
        """Scan for the BLE display device.
        
        Returns:
            Device object if found, None on timeout.
        """
        print("Scanning for", self.target_addr)
        async with aioble.scan(
            duration_ms=10000,
            interval_us=30000,
            window_us=30000,
            active=True,
        ) as scanner:
            async for result in scanner:
                addr = result.device.addr_hex()
                print(" ", addr, end="\r")
                await asyncio.sleep_ms(20)
                if addr == self.target_addr:
                    print("\nFound", addr)
                    return result.device
        print("\nScan timeout")
        return None
    
    async def _wait_notification(self, ch, timeout_s=10):
        """Wait for a notification from the device."""
        return await asyncio.wait_for(ch.notified(), timeout=timeout_s)
    
    async def _send_cmd(self, ch, cmd_id, payload=b""):
        """Send a command to the device."""
        packet = cmd_packet(cmd_id, payload)
        await ch.write(packet, response=False)
    
    async def _send_part_wait_ack(self, ch, block_part_payload):
        """Send a block part and wait for acknowledgment."""
        while True:
            await self._send_cmd(ch, CMD_SEND_BLOCK_PART, block_part_payload)
            while True:
                raw = await self._wait_notification(ch, timeout_s=10)
                rsp_cmd, _ = parse_cmd(raw)
                if rsp_cmd == RSP_PART_ACK:
                    return None
                if rsp_cmd == RSP_PART_ERROR:
                    break
                if rsp_cmd == RSP_COMMAND_ACK:
                    continue
                if rsp_cmd in (RSP_BLOCK_REQUEST, RSP_UPLOAD_COMPLETE, RSP_DATA_PRESENT):
                    return raw
                if rsp_cmd == RSP_ERROR:
                    raise RuntimeError("Device returned protocol error (0xFFFF)")
                print("Ignoring part-wait notification 0x%04X" % (rsp_cmd if rsp_cmd is not None else -1))
    
    async def _wait_ready_ack(self, ch):
        """Wait for ready acknowledgment from device."""
        while True:
            raw = await self._wait_notification(ch, timeout_s=10)
            rsp_cmd, _ = parse_cmd(raw)
            if rsp_cmd == RSP_COMMAND_ACK:
                return None
            if rsp_cmd in (RSP_BLOCK_REQUEST, RSP_UPLOAD_COMPLETE, RSP_DATA_PRESENT):
                return raw
            if rsp_cmd in (RSP_PART_ACK, RSP_PART_ERROR):
                continue
            if rsp_cmd == RSP_ERROR:
                raise RuntimeError("Device returned protocol error (0xFFFF)")
            print("Ignoring ready-wait notification 0x%04X" % (rsp_cmd if rsp_cmd is not None else -1))
    
    async def upload(self, image_data, data_type=None):
        """Upload image data to the display.
        
        Args:
            image_data: Raw image bytes to upload
            data_type: Image data type (default: use instance data_type)
        """
        if data_type is None:
            data_type = self.data_type
        
        target_addr = self.target_addr
        
        # Render image
        print("Image bytes:", len(image_data))
        total_blocks = int(math.ceil(len(image_data) / BLOCK_DATA_SIZE))
        print("Total blocks:", total_blocks)

        avail = make_avail_data_info(image_data, data_type)
        
        # Find and connect device
        device = None
        conn = None
        last_error = None
        
        for attempt in range(1, self.connect_retries + 1):
            if not device:
                print("Connect attempt %d/%d (device scan)" % (attempt, self.connect_retries))
                device = await self.find_device()
                if not device:
                    if attempt < self.connect_retries:
                        await asyncio.sleep_ms(self.connect_retry_delay_ms)
                    continue

            try:
                print("Connect attempt %d/%d" % (attempt, self.connect_retries))
                conn = await device.connect(timeout_ms=10000)
                break
            except Exception as exc:
                last_error = exc
                print("Connect failed:", exc)
                if attempt < self.connect_retries:
                    await asyncio.sleep_ms(self.connect_retry_delay_ms)
                    device = None

        if conn is None:
            raise RuntimeError("Unable to connect after retries: %s" % last_error)

        try:
            await conn.exchange_mtu(247)

            service = await conn.service(SERVICE_UUID)
            if not service:
                raise RuntimeError("Service 0x1337 not found")

            ch = await service.characteristic(CHAR_UUID)
            if not ch:
                raise RuntimeError("Characteristic 0x1337 not found")

            await ch.subscribe(notify=True)
            await asyncio.sleep_ms(300)

            # Start transfer and handle block requests
            await self._send_cmd(ch, CMD_START_DATA_TRANSFER, avail)

            completed = False
            pending_raw = None
            while not completed:
                if pending_raw is not None:
                    raw = pending_raw
                    pending_raw = None
                else:
                    raw = await self._wait_notification(ch, timeout_s=20)
                rsp_cmd, rsp_payload = parse_cmd(raw)

                if rsp_cmd == RSP_BLOCK_REQUEST:
                    if len(rsp_payload) < 17:
                        raise RuntimeError("Invalid BlockRequest payload")

                    req_block_id = rsp_payload[9]
                    req_type = rsp_payload[10]
                    req_parts_mask = rsp_payload[11:17]
                    req_parts = requested_parts_from_mask(req_parts_mask)

                    print("Block %d: requesting %d parts" % (req_block_id, len(req_parts)))
                    if req_block_id >= total_blocks:
                        raise RuntimeError("Device requested out-of-range block %d" % req_block_id)

                    await self._send_cmd(ch, CMD_ACK_READY)
                    pending_raw = await self._wait_ready_ack(ch)
                    if pending_raw is not None:
                        continue

                    for part_id in req_parts:
                        block_part = build_block_part(image_data, req_block_id, part_id)
                        pending_raw = await self._send_part_wait_ack(ch, block_part)
                        print("  Part %d/%d sent" % (part_id + 1, PARTS_PER_BLOCK))
                        if pending_raw is not None:
                            break

                elif rsp_cmd == RSP_UPLOAD_COMPLETE:
                    print("Upload complete (device confirmed)")
                    await self._send_cmd(ch, CMD_TRANSFER_COMPLETE)
                    completed = True

                elif rsp_cmd == RSP_DATA_PRESENT:
                    print("Device reports identical data already present")
                    await self._send_cmd(ch, CMD_TRANSFER_COMPLETE)
                    completed = True

                elif rsp_cmd == RSP_COMMAND_ACK:
                    pass

                elif rsp_cmd in (RSP_PART_ACK, RSP_PART_ERROR):
                    pass

                elif rsp_cmd == RSP_ERROR:
                    raise RuntimeError("Device returned protocol error (0xFFFF)")

                else:
                    print("Ignoring notification 0x%04X" % (rsp_cmd if rsp_cmd is not None else -1))

            print("Upload complete (device confirmed)")
            print("Done")

        finally:
            if conn:
                await conn.disconnect()
