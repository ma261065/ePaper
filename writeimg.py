import sys
import uasyncio as asyncio
import aioble
import bluetooth

TARGET_ADDR = "3c:60:55:84:a0:42"

# The Service UUID
SERVICE_UUID = bluetooth.UUID(0x1337)

# The Write Characteristic UUID
CHAR_UUID = bluetooth.UUID(0x1337)

async def find_device():
    print("Here")
    async with aioble.scan(
        duration_ms=10000,
        interval_us=30000,
        window_us=30000,
        active=True
    ) as scanner:
        async for r in scanner:
            print(r.device.addr_hex(), end='\r')
            await asyncio.sleep_ms(20)
            if r.device.addr_hex() == TARGET_ADDR:
                print("Found:", r.device.addr_hex())
                return r.device
    return None

async def send_data(ch, data):
    print("Sending", data)

    notify_task = asyncio.create_task(ch.notified())
    await ch.write(data, response=True)

    try:
        resp = await asyncio.wait_for(notify_task, timeout=3)
        print("Response:", resp.hex())
    except asyncio.TimeoutError:
        print("Timeout")

async def main():
    print("Starting")

    device = await find_device()
    
    if not device:
        print("Device not found")
        return
        
    conn = await device.connect(timeout_ms=10000)

    # Must do this or no response will be received
    await conn.exchange_mtu(247)

    try:
        service = await conn.service(SERVICE_UUID)
        if not service:
            print("Service not found")
            return

        ch = await service.characteristic(CHAR_UUID)
        if not ch:
            print("Characteristic not found")
            return

        print("Subscribing...")
        await ch.subscribe(True)
        await asyncio.sleep(1.0)

        await send_data(ch, bytes.fromhex("0011"))
        await send_data(ch, bytes.fromhex("0064FF95E83640000000008052000021000000"))
        # 00 c6 00 95 e8 36 40 00 00 00 00 00 21 ff ff ff ff ff ff
        # Checksum: 0, Version: 9BBBAD03, Block ID: 0, Type: 33, Requested Parts: 111111111111111111111111111111111111111111111111 (48)
        await send_data(ch, bytes.fromhex("0002"))
        for i in range(108):
            await send_data(ch, bytes.fromhex("00651400000010B94B00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"))
        await send_data(ch, bytes.fromhex("0003"))
        # 00 c6 00 03 ad bb 9b 00 00 00 00 01 21 ff ff
        # Checksum: 0, Version: 9BBBAD03, Block ID: 1, Type: 33, Requested Parts: 111111111111111111111111111111111111111111111111
        # Should get back 0xc7 for last packet, not 0xc5
    finally:
        await conn.disconnect()
    

# Run the async loop
asyncio.run(main())
