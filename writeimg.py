import binascii
import math
import struct
import sys
import time

import aioble
import bluetooth
import esp32
import framebuf
import machine
import network
import urequests
import uasyncio as asyncio
try:
    import ntptime
except ImportError:
    ntptime = None

try:
    from bitmap_font import draw_text_bitmap
except ImportError:
    # Fallback to framebuf text if bitmap_font not available
    draw_text_bitmap = None


TARGET_ADDR = "3c:60:55:84:a0:42"
SERVICE_UUID = bluetooth.UUID(0x1337)
CHAR_UUID = bluetooth.UUID(0x1337)

CMD_ACK_READY = 0x0002
CMD_TRANSFER_COMPLETE = 0x0003
CMD_START_DATA_TRANSFER = 0x0064
CMD_SEND_BLOCK_PART = 0x0065

RSP_COMMAND_ACK = 0x0063
RSP_PART_ERROR = 0x00C4
RSP_PART_ACK = 0x00C5
RSP_BLOCK_REQUEST = 0x00C6
RSP_UPLOAD_COMPLETE = 0x00C7
RSP_DATA_PRESENT = 0x00C8
RSP_ERROR = 0xFFFF

BLOCK_DATA_SIZE = 4096
BLOCK_PART_DATA_SIZE = 230
PARTS_PER_BLOCK = 18

DEFAULT_DATA_TYPE = 0x21  # Raw B/W/R or B/W/Y image
BMP_PATH = "image.bmp"
BMP_WIDTH = 480  # BMP is landscape
BMP_HEIGHT = 176
DISPLAY_WIDTH = 176  # Display is portrait
DISPLAY_HEIGHT = 480
CONNECT_RETRIES = 200
CONNECT_RETRY_DELAY_MS = 1200

USE_WEATHER_SOURCE = True
WIFI_SSID = ""
WIFI_PASSWORD = ""
NVS_NAMESPACE = "weather"
NVS_KEY_WIFI_SSID = "wifi_ssid"
NVS_KEY_WIFI_PASSWORD = "wifi_pass"
WIFI_SWITCH_ENABLE_PIN = 3
WIFI_ANT_CONFIG_PIN = 14
BOM_API_BASE = "https://api.weather.bom.gov.au/v1"
BOM_LOCATION_QUERY = "Williamstown"
BOM_LOCATION_STATE = "VIC"
BOM_LOCATION_GEOHASH = ""
FORECAST_DAYS = 8


def cmd_packet(cmd_id, payload=b""):
    return struct.pack(">H", cmd_id) + payload


def sum8(data):
    return sum(data) & 0xFF


def sum16(data):
    return sum(data) & 0xFFFF


def parse_cmd(notification):
    if notification is None or len(notification) < 2:
        return None, b""
    cmd_id = (notification[0] << 8) | notification[1]
    return cmd_id, notification[2:]


def make_avail_data_info(image_data, data_type):
    crc32_value = binascii.crc32(image_data) & 0xFFFFFFFF
    data_size = len(image_data)
    return struct.pack("<BQIBBH", 0xFF, crc32_value, data_size, data_type, 0x00, 0x0000)


def urlencode_simple(value):
    return value.replace(" ", "%20")


def connect_wifi(ssid, password, timeout_s=30):
    wifi_switch_enable = machine.Pin(WIFI_SWITCH_ENABLE_PIN, machine.Pin.OUT)
    wifi_ant_config = machine.Pin(WIFI_ANT_CONFIG_PIN, machine.Pin.OUT)
    wifi_switch_enable.value(0)
    wifi_ant_config.value(1)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan

    wlan.connect(ssid, password)
    start = time.time()
    while not wlan.isconnected() and (time.time() - start) < timeout_s:
        time.sleep(0.25)

    if not wlan.isconnected():
        raise RuntimeError("Wi-Fi connect failed")
    return wlan


def _nvs_get_text(nvs_obj, key_name, max_len=96):
    raw = bytearray(max_len)
    try:
        nvs_obj.get_blob(key_name, raw)
    except OSError:
        return ""
    text_bytes = bytes(raw).split(b"\x00", 1)[0]
    if not text_bytes:
        return ""
    return text_bytes.decode()


def load_wifi_credentials():
    if WIFI_SSID and WIFI_PASSWORD:
        return WIFI_SSID, WIFI_PASSWORD

    nvs = esp32.NVS(NVS_NAMESPACE)
    ssid = _nvs_get_text(nvs, NVS_KEY_WIFI_SSID)
    password = _nvs_get_text(nvs, NVS_KEY_WIFI_PASSWORD)
    if not ssid or not password:
        raise RuntimeError(
            "Wi-Fi credentials missing in NVS (%s:%s,%s)"
            % (NVS_NAMESPACE, NVS_KEY_WIFI_SSID, NVS_KEY_WIFI_PASSWORD)
        )
    return ssid, password


def http_get_json(url, retries=3, delay=2):
    last_exc = None
    for attempt in range(retries):
        try:
            response = urequests.get(url)
            try:
                return response.json()
            finally:
                response.close()
        except OSError as e:
            print("HTTP GET failed (Attempt %d/%d): %s" % (attempt + 1, retries, e))
            last_exc = e
            if attempt < retries - 1:
                time.sleep(delay)
    
    if last_exc:
        raise last_exc
    return {}


def resolve_bom_location():
    if BOM_LOCATION_GEOHASH:
        return BOM_LOCATION_GEOHASH, BOM_LOCATION_QUERY

    search_url = "%s/locations?search=%s" % (BOM_API_BASE, urlencode_simple(BOM_LOCATION_QUERY))
    payload = http_get_json(search_url)
    locations = payload.get("data") or []
    if not locations:
        raise RuntimeError("No BOM location result for '%s'" % BOM_LOCATION_QUERY)

    preferred = None
    for location in locations:
        if (location.get("state") or "").upper() == BOM_LOCATION_STATE.upper():
            preferred = location
            break

    if preferred is None:
        preferred = locations[0]

    loc_name = preferred.get("name", BOM_LOCATION_QUERY)
    loc_state = preferred.get("state")
    if loc_state:
        loc_name = "%s, %s" % (loc_name, loc_state)

    return preferred.get("geohash"), loc_name


def _weekday_name(date_iso):
    if not date_iso or len(date_iso) < 10:
        return "---"
    # BOM dates "2026-02-13T13:00:00Z" are UTC.
    # For Australian East Coast, +10/11h pushes this into the NEXT day.
    # We simply add 1 day to the date parsed from the string.
    
    year = int(date_iso[0:4])
    month = int(date_iso[5:7])
    day = int(date_iso[8:10])

    # Simple approximate add-one-day (good enough for typical BOM usage)
    # Proper calendar logic would be better but keeps it small.
    # Let's use utime.mktime if we want to be safe, or just Zeller's with an offset?
    # Actually, let's use standard epoch math if possible, or just hack the day.
    
    # Let's rely on standard python logic if possible, or zeller.
    # Note: 31st + 1 -> 32nd. Zeller might handle it, but months need care.
    # Safer to convert to epoch, add 86400, convert back.
    
    try:
        t = time.mktime((year, month, day, 12, 0, 0, 0, 0))
        t += 86400 # Add 24 hours
        # localtime might not be timezone aware on ESP32 without config, 
        # but relative change is what matters.
        # However, mktime -> tuple is cleaner.
        import time as tt
        tm = tt.localtime(t)
        # tm is (year, month, day, hour, min, sec, wday, yday)
        # wday: 0=Mon, 6=Sun
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return names[tm[6]]
    except:
        return "---"

def _date_str_local(date_iso):
    if not date_iso or len(date_iso) < 10:
        return "", 0, 0
    
    year = int(date_iso[0:4])
    month = int(date_iso[5:7])
    day = int(date_iso[8:10])
    
    try:
        t = time.mktime((year, month, day, 12, 0, 0, 0, 0))
        t += 86400
        tm = time.localtime(t)
        # Return (day_of_month, month_num)
        return tm[2], tm[1]
    except:
        return 0, 0

def fetch_bom_daily_forecast(limit_days):
    geohash, location_name = resolve_bom_location()
    if not geohash:
        raise RuntimeError("BOM location geohash missing")

    daily_url = "%s/locations/%s/forecasts/daily" % (BOM_API_BASE, geohash)
    payload = http_get_json(daily_url)
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError("BOM daily forecast has no data")

    forecasts = []
    max_rows = min(limit_days, len(rows))
    for idx in range(max_rows):
        row = rows[idx]
        rain = row.get("rain") or {}
        amount = rain.get("amount") or {}
        date_value = row.get("date", "")
        
        # Calculate local date components
        d_num, m_num = _date_str_local(date_value)

        forecasts.append({
            "weekday": _weekday_name(date_value),
            "date_short": "{:02d}/{:02d}".format(d_num, m_num) if d_num else "--/--",
            "day_num": d_num,
            "month_num": m_num,
            "temp_min": row.get("temp_min"),
            "temp_max": row.get("temp_max"),
            "rain_chance": rain.get("chance"),
            "rain_lower": amount.get("lower_range", 0),
            "rain_upper": amount.get("upper_range", 0),
            "icon": row.get("icon_descriptor") or "cloudy",
            "now": row.get("now"),
        })

    return {
        "location": location_name,
        "days": forecasts,
    }


def _month_name(month_num):
    months = ["", "January", "February", "March", "April", "May", "June", 
              "July", "August", "September", "October", "November", "December"]
    if 1 <= month_num <= 12:
        return months[month_num]
    return ""

# Map BOM icon descriptors to local filenames
# Filenames are: sunny, partly, cloudy, rain, storm, fog
ICON_MAP = {
    "sunny": "sunny",
    "clear": "sunny",
    "mostly_sunny": "partly",
    "partly_cloudy": "partly",
    "cloudy": "cloudy",
    "hazy": "fog",
    "fog": "fog",
    "light_rain": "rain",
    "rain": "rain",
    "dusty": "fog",
    "shower": "rain",
    "light_shower": "rain",
    "heavy_shower": "rain", 
    "storm": "storm",
    "snow": "rain",  # Melbourne rarely gets snow, map to rain or cloudy
    "frost": "fog",
    "wind": "cloudy",
}

def draw_bmp_icon(fb_black, fb_yellow, x, y, icon_name, size_suffix="_s"):
    """
    Draw a BMP icon onto the framebuffers.
    x, y: Top-left coordinate.
    icon_name: BOM descriptor string.
    size_suffix: "_s" (32x32) or "_b" (100x100).
    """
    base_name = ICON_MAP.get(icon_name, "cloudy")
    filename = "icons/%s%s.bmp" % (base_name, size_suffix)
    
    try:
        with open(filename, "rb") as f:
            # Basic BMP Header Processing
            if f.read(2) != b'BM': return
            f.seek(10)
            pixel_offset = struct.unpack('<I', f.read(4))[0]
            f.seek(18)
            width = struct.unpack('<i', f.read(4))[0]
            height = struct.unpack('<i', f.read(4))[0]
            f.seek(28)
            bpp = struct.unpack('<H', f.read(2))[0]
            
            # Support 8-bit, 24-bit and 32-bit BMPs
            if bpp not in (8, 24, 32):
                print("Unsupported BPP %d for icon: %s" % (bpp, filename))
                return

            palette = None
            if bpp == 8:
                # Read palette. 
                # Assumes standard BITMAPINFOHEADER (40 bytes). 
                # Palette starts at 14 + 40 = 54. 
                # Or just read from 54 up to pixel_offset?
                # Ideally, read from end of header method.
                f.seek(14)
                dib_header_size = struct.unpack('<I', f.read(4))[0]
                palette_offset = 14 + dib_header_size
                f.seek(palette_offset)
                
                # Number of colors. If 0, then 2^bpp.
                # However, usually 256 for 8-bit.
                # Just read 256 entries (1024 bytes).
                palette_data = f.read(1024)
                # Convert to simple list of (b,g,r) tuples or bytearray
                # Each entry is B, G, R, Reserved
                
            bytes_per_pixel = bpp // 8
            if bytes_per_pixel < 1: bytes_per_pixel = 1 # Should not happen for 8bpp+
            
            row_size = ((width * bpp + 31) // 32) * 4
            
            is_top_down = height < 0
            height = abs(height)
            
            # Simple clipped constraints
            
            for r in range(height):
                src_y = r if is_top_down else (height - 1 - r)
                f.seek(pixel_offset + (src_y * row_size))
                row_data = f.read(row_size)
                
                draw_y = y + r
                if draw_y >= BMP_HEIGHT: continue # Clip Y
                
                for c in range(width):
                    draw_x = x + c
                    if draw_x >= BMP_WIDTH: continue # Clip X 
                    
                    if draw_x < 0 or draw_y < 0:
                        continue

                    # Get RGB
                    if bpp == 8:
                        idx = c
                        color_index = row_data[idx]
                        # Lookup in palette
                        p_idx = color_index * 4
                        if p_idx + 2 < len(palette_data):
                            b = palette_data[p_idx]
                            g = palette_data[p_idx+1]
                            r = palette_data[p_idx+2]
                        else:
                            r, g, b = 255, 255, 255 # Default white
                    else:
                        # 24 or 32 bit
                        idx = c * bytes_per_pixel
                        b = row_data[idx]
                        g = row_data[idx+1]
                        r = row_data[idx+2]
                    
                    # Color Mapping
                    # White (255,255,255) -> Background (Do nothing, as background is 1 - Black)
                    # Black (0,0,0) -> White Text/Lines -> fb_black pixel = 0
                    # Yellow/Red -> fb_yellow pixel = 1
                    
                    if r > 200 and g > 200 and b > 200:
                        # White - Transparent (Leaves BG which is now 1/Black)
                        continue
                    elif r < 50 and g < 50 and b < 50:
                        # Black Source -> White Ink (0)
                        fb_black.pixel(draw_x, draw_y, 0)
                    else:
                        # Assume color (Yellow/Red)
                        fb_yellow.pixel(draw_x, draw_y, 1)

    except OSError:
        # Fallback if file missing
        print("Icon not found:", filename)
        # Draw a placeholder rect
        fb_black.rect(x, y, 32, 32, 1)


def _draw_icon(fb_black, fb_yellow, x, y, icon_name, compact=False):
    # Wrapper to existing signature
    if compact:
        # Small icons are 32x32.
        # Check alignment. Original code drew circles at x+10.
        # We'll center the 32x32 image in the area.
        # The space width is ~40px?
        # Let's just draw at x.
        draw_bmp_icon(fb_black, fb_yellow, x, y, icon_name, "_s")
    else:
        # Big icon (Today).
        # Old code drew at 86, 52.
        # We have 100x100 space.
        draw_bmp_icon(fb_black, fb_yellow, x, y, icon_name, "_b")



def _draw_wind_icon(fb_black, x, y):
    fb_black.line(x - 14, y - 4, x + 12, y - 4, 1)
    fb_black.line(x - 10, y + 1, x + 14, y + 1, 1)
    fb_black.line(x - 14, y + 6, x + 10, y + 6, 1)


def _draw_text_compat(fb, x, y, text, color, scale=2):
    if draw_text_bitmap:
        draw_text_bitmap(fb, x, y, text, color, scale=scale)
    else:
        fb.text(text, x, y, color)


def _transpose_landscape_planes(land_black, land_color, bmp_width, bmp_height, display_width, display_height):
    plane_size = (display_width * display_height) // 8
    out_black = bytearray([0xFF] * plane_size)
    out_color = bytearray([0x00] * plane_size)

    fb_land_black = framebuf.FrameBuffer(land_black, bmp_width, bmp_height, framebuf.MONO_HMSB)
    fb_land_color = framebuf.FrameBuffer(land_color, bmp_width, bmp_height, framebuf.MONO_HMSB)

    for y in range(bmp_height):
        for x in range(bmp_width):
            is_yellow = fb_land_color.pixel(x, y) == 1
            is_black = fb_land_black.pixel(x, y) == 0

            display_x = y
            display_y = bmp_width - 1 - x
            bit_index = display_y * display_width + display_x
            byte_pos = bit_index // 8
            bit_in_byte = 7 - (bit_index % 8)

            if is_yellow:
                out_color[byte_pos] |= (1 << bit_in_byte)
            elif is_black:
                out_black[byte_pos] &= ~(1 << bit_in_byte)

    return bytes(out_black) + bytes(out_color)


def render_weather_to_raw(bmp_width, bmp_height, display_width, display_height, forecast):
    """Render live weather in landscape layout, then transpose to portrait display format."""
    plane_size_land = (bmp_width * bmp_height) // 8
    land_black = bytearray([0xFF] * plane_size_land)
    land_color = bytearray([0x00] * plane_size_land)

    fb_black = framebuf.FrameBuffer(land_black, bmp_width, bmp_height, framebuf.MONO_HMSB)
    fb_yellow = framebuf.FrameBuffer(land_color, bmp_width, bmp_height, framebuf.MONO_HMSB)

    # 1=White/Background, 0=Black/Ink?
    # User sees White Text on Black BG with: Fill(1) and Text(0).
    # This implies 1=Black, 0=White on User Device.
    # To get White BG, we need 0. To get Black Text we need 1.
    
    # INVERTING COLORS: Dark Mode (White Text on Black Background)
    # Background (Fill) = 1 (Black)
    # Text (Ink) = 0 (White)
    fb_black.fill(1)
    fb_yellow.fill(0)

    days = forecast.get("days") or []
    if not days:
        _draw_text_compat(fb_black, 12, 12, "No forecast", 0)
        return _transpose_landscape_planes(land_black, land_color, bmp_width, bmp_height, display_width, display_height)

    # BOM Index 0: Today (derived from date + 1 logic)
    # BOM Index 1: Tomorrow
    if len(days) < 2:
        # Not enough data, fallback
        today = days[0] if days else {}
        forecast_list = []
    else:
        # Today's detailed panel comes from Index 0
        today = days[0]
        # 5-day forecast starting from Tomorrow (Index 1)
        forecast_list = days[1:6]

    # Moved divider right to give Today panel more space (was 272)
    divider_x = 300
    panel_x = divider_x + 6
    panel_w = bmp_width - panel_x - 8
    card_h = 31
    card_gap = 4
    top_pad = 2

    # Vertical divider stopped slightly short of top/bottom
    v_pad = 10
    fb_black.vline(divider_x, v_pad, bmp_height - (v_pad * 2), 0)

    for idx, day in enumerate(forecast_list):
        y0 = top_pad + idx * (card_h + card_gap)
        
        # Draw horizontal delimiter line between days (but not after the last one)
        if idx < len(forecast_list) - 1:
            line_y = y0 + card_h + (int(card_gap / 2))
            fb_black.hline(panel_x, line_y, panel_w, 0)

        # Use the weekday from the corrected date logic
        weekday = day.get("weekday", "---")

        high_txt = "%s°" % ("--" if day.get("temp_max") is None else str(day.get("temp_max")))
        low_txt = "%s°" % ("--" if day.get("temp_min") is None else str(day.get("temp_min")))

        _draw_text_compat(fb_black, panel_x + 6, y0 + 12, weekday, 0)
        # Move icon up to be vertically centered in the ~32px slot. 
        # y0 is top. Icon is 32px. Slot is ~34px. y0 is good.
        # Adjusted X for larger font (Week=~36px). Icon at 50.
        # Moved back down 1 pixel per request (was y0 - 1)
        _draw_icon(fb_black, fb_yellow, panel_x + 50, y0, day.get("icon"), compact=True)
        # Low temp on left (black), High temp on right (yellow)
        # Move text down slightly to align with center of icon
        # Low at 90 (Ends 126). High at 134.
        _draw_text_compat(fb_black, panel_x + 90, y0 + 12, low_txt, 0)
        _draw_text_compat(fb_yellow, panel_x + 134, y0 + 12, high_txt, 1)

    # Prepare Today Text
    now_data = today.get("now")
    
    if now_data:
        # User requested dynamic labels from API
        label_1 = str(now_data.get("now_label", "Now"))
        val_1 = "%s°" % now_data.get("temp_now", "--")
        label_2 = str(now_data.get("later_label", "Later"))
        val_2 = "%s°" % now_data.get("temp_later", "--")
    else:
        # Fallback to standard High/Low
        label_1 = "High"
        val_1 = "%s°" % ("--" if today.get("temp_max") is None else str(today.get("temp_max")))
        label_2 = "Low"
        val_2 = "%s°" % ("--" if today.get("temp_min") is None else str(today.get("temp_min")))
    
    # Rain logic: "xx to yy mm" or "xx mm"
    r_low = today.get("rain_lower", 0)
    r_high = today.get("rain_upper", 0)
    # Check for None just in case, though get defaults to 0 above only if key missing
    if r_low is None: r_low = 0
    if r_high is None: r_high = 0
    
    rain_label = "Rain"
    if r_low == r_high:
        rain_val = "%s mm" % r_low
    else:
        rain_val = "%s to %s mm" % (r_low, r_high)

    # Format: TODAY - WeekdayFull DD MonthAbbr
    # e.g. TODAY - Sunday 14 Feb
    today_date_str = "TODAY"
    t_weekday = today.get("weekday", "")
    t_day_num = today.get("day_num", 0)
    t_month_num = today.get("month_num", 0)
    
    # Map short weekday to full
    full_days = {
        "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", 
        "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"
    }

    if t_weekday and t_day_num and t_month_num:
        full_day = full_days.get(t_weekday, t_weekday)
        abbr_month = _month_name(t_month_num)[:3]
        today_date_str += " - %s %d %s" % (full_day, t_day_num, abbr_month)

    _draw_text_compat(fb_yellow, 14, 8, today_date_str, 1)
    # fb_black.hline(14, 24, 64, 0) # Removed per request
    
    # Position for 64x64 icon. 
    # Moved big icon right to be under the day of month (~200px)
    _draw_icon(fb_black, fb_yellow, 200, 33, today.get("icon"))
    
    # Render Today Stats
    # Label 1 (Black) | Value 1 (Yellow)
    # Moved to 100 to fit larger fonts.
    # Icon ends at 34+64 = 98. 2px gap.
    y_stats = 100
    
    # Text Alignment Logic:
    # 1. Calculate width of both labels.
    # 2. Determine max width.
    # 3. Align both values to the same X position (max_w + padding).
    lbl1_w = len(label_1) * 12
    lbl2_w = len(label_2) * 12
    max_lbl_w = max(lbl1_w, lbl2_w)
    val_x = 14 + max_lbl_w + 12 # Padding
    
    # Gap between lines. 30px allows for (21px height + 9px gap)
    line_spacing = 30
    
    # Line 1
    # Label Scale 2 (H=14), Value Scale 3 (H=21). Centering fix y+3.
    _draw_text_compat(fb_black, 14, y_stats + 3, label_1, 0)
    _draw_text_compat(fb_yellow, val_x, y_stats, val_1, 1, scale=3)
    
    # Line 2
    y_line2 = y_stats + line_spacing
    _draw_text_compat(fb_black, 14, y_line2 + 3, label_2, 0)
    _draw_text_compat(fb_yellow, val_x, y_line2, val_2, 1, scale=3)

    # Rain Label (Black) | Value (Yellow)
    # Scale 2 (Height 14).
    y_line3 = y_line2 + line_spacing - 1
    _draw_text_compat(fb_black, 14, y_line3, rain_label, 0)
    # Align rain value with the temp values for consistency? 
    # Or just after "Rain"? Code used after label.
    # Let's keep it tight to "Rain" as requested previously ("Rain" then value).
    lbl_rain_w = len(rain_label) * 12
    _draw_text_compat(fb_yellow, 14 + lbl_rain_w + 12, y_line3, rain_val, 1)
    
    # Wind Removed per request
    # _draw_text_compat(fb_black, 14, 150, "Wind: -- km/h", 1)
    # _draw_wind_icon(fb_black, 220, 153)

    # Location Removed per request (doesn't fit with larger font)
    # location = forecast.get("location") or ""
    # if location:
    #     _draw_text_compat(fb_black, 12, 166, location[:20], 1)

    return _transpose_landscape_planes(land_black, land_color, bmp_width, bmp_height, display_width, display_height)


def read_bmp_info(path, expected_width, expected_height):
    with open(path, "rb") as file_obj:
        header = file_obj.read(54)

        if len(header) < 54 or header[0:2] != b"BM":
            raise ValueError("Not a BMP file")

        pixel_offset = struct.unpack_from("<I", header, 10)[0]
        dib_size = struct.unpack_from("<I", header, 14)[0]
        if dib_size < 40:
            raise ValueError("Unsupported BMP DIB header")

        width = struct.unpack_from("<i", header, 18)[0]
        height_signed = struct.unpack_from("<i", header, 22)[0]
        planes = struct.unpack_from("<H", header, 26)[0]
        bpp = struct.unpack_from("<H", header, 28)[0]
        compression = struct.unpack_from("<I", header, 30)[0]

        if planes != 1:
            raise ValueError("Unsupported BMP planes")
        if compression != 0:
            raise ValueError("Compressed BMP is not supported")
        if bpp not in (1, 4, 8, 24, 32):
            raise ValueError("Unsupported BMP bpp %d" % bpp)

        height = -height_signed if height_signed < 0 else height_signed
        if width != expected_width or height != expected_height:
            raise ValueError("Expected BMP %dx%d, got %dx%d" % (expected_width, expected_height, width, height))

        palette_rgb = None
        if bpp in (1, 4, 8):
            palette_start = 14 + dib_size
            palette_entries = (pixel_offset - palette_start) // 4
            if palette_entries <= 0:
                raise ValueError("Missing BMP palette")

            file_obj.seek(palette_start)
            palette_raw = file_obj.read(palette_entries * 4)
            if len(palette_raw) < palette_entries * 4:
                raise ValueError("Invalid BMP palette")

            palette_rgb = bytearray(palette_entries * 3)
            for idx in range(palette_entries):
                src = idx * 4
                dst = idx * 3
                palette_rgb[dst] = palette_raw[src + 2]
                palette_rgb[dst + 1] = palette_raw[src + 1]
                palette_rgb[dst + 2] = palette_raw[src]

    return {
        "pixel_offset": pixel_offset,
        "width": width,
        "height": height,
        "top_down": height_signed < 0,
        "bpp": bpp,
        "row_bytes": ((width * bpp + 31) // 32) * 4,
        "palette_rgb": palette_rgb,
    }


def bmp_to_raw_bw_color(path, bmp_width, bmp_height, display_width, display_height):
    """Convert BMP to bitplane, transposing from landscape to portrait."""
    info = read_bmp_info(path, bmp_width, bmp_height)
    bpp = info["bpp"]
    row_bytes = info["row_bytes"]
    pixel_offset = info["pixel_offset"]
    top_down = info["top_down"]
    palette_rgb = info["palette_rgb"]

    plane_size = (display_width * display_height) // 8
    black_plane = bytearray([0xFF] * plane_size)
    color_plane = bytearray([0x00] * plane_size)

    with open(path, "rb") as file_obj:
        for bmp_y in range(bmp_height):
            src_y = bmp_y if top_down else (bmp_height - 1 - bmp_y)
            row_start = pixel_offset + src_y * row_bytes
            file_obj.seek(row_start)
            row = file_obj.read(row_bytes)
            if len(row) < row_bytes:
                raise ValueError("Unexpected end of BMP pixel data")

            for bmp_x in range(bmp_width):
                # Read BMP pixel
                if bpp == 24:
                    i = bmp_x * 3
                    blue = row[i]
                    green = row[i + 1]
                    red = row[i + 2]

                elif bpp == 32:
                    i = bmp_x * 4
                    blue = row[i]
                    green = row[i + 1]
                    red = row[i + 2]

                elif bpp == 8:
                    idx = row[bmp_x]
                    p = idx * 3
                    red = palette_rgb[p]
                    green = palette_rgb[p + 1]
                    blue = palette_rgb[p + 2]

                elif bpp == 4:
                    packed = row[bmp_x // 2]
                    if (bmp_x % 2) == 0:
                        idx = (packed >> 4) & 0x0F
                    else:
                        idx = packed & 0x0F
                    p = idx * 3
                    red = palette_rgb[p]
                    green = palette_rgb[p + 1]
                    blue = palette_rgb[p + 2]

                else:  # bpp == 1
                    packed = row[bmp_x // 8]
                    idx = (packed >> (7 - (bmp_x % 8))) & 0x01
                    p = idx * 3
                    red = palette_rgb[p]
                    green = palette_rgb[p + 1]
                    blue = palette_rgb[p + 2]

                # Determine color
                gray = (red * 30 + green * 59 + blue * 11) // 100
                is_yellow = (
                    red > 160
                    and green > 120
                    and blue < 120
                    and (red - blue) > 60
                    and (green - blue) > 40
                )
                is_black = gray >= 128

                # Transpose: landscape (bmp_x, bmp_y) -> portrait (bmp_y, bmp_width - 1 - bmp_x)
                display_x = bmp_y
                display_y = bmp_width - 1 - bmp_x
                
                # Calculate bitplane position in display orientation
                bit_index = display_y * display_width + display_x
                byte_pos = bit_index // 8
                bit_in_byte = 7 - (bit_index % 8)

                if is_yellow:
                    color_plane[byte_pos] |= (1 << bit_in_byte)
                elif is_black:
                    black_plane[byte_pos] &= ~(1 << bit_in_byte)

    return bytes(black_plane) + bytes(color_plane)


def requested_parts_from_mask(mask_bytes):
    parts = []
    for part_id in range(PARTS_PER_BLOCK):
        byte_index = part_id // 8
        bit_index = part_id % 8
        if byte_index < len(mask_bytes):
            if (mask_bytes[byte_index] >> bit_index) & 0x01:
                parts.append(part_id)
    return parts


def build_block_part(image_data, block_id, part_id):
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


async def find_device(target_addr):
    print("Scanning for", target_addr)
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
            if addr == target_addr:
                print("\nFound", addr)
                return result.device
    print("\nScan timeout")
    return None


async def wait_notification(ch, timeout_s=10):
    return await asyncio.wait_for(ch.notified(), timeout=timeout_s)


async def send_cmd(ch, cmd_id, payload=b""):
    packet = cmd_packet(cmd_id, payload)
    await ch.write(packet, response=False)


async def send_part_wait_ack(ch, block_part_payload):
    while True:
        await send_cmd(ch, CMD_SEND_BLOCK_PART, block_part_payload)
        while True:
            raw = await wait_notification(ch, timeout_s=10)
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


async def wait_ready_ack(ch):
    while True:
        raw = await wait_notification(ch, timeout_s=10)
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


async def upload_image(ch, image_data, data_type):
    print("Image bytes:", len(image_data))
    total_blocks = int(math.ceil(len(image_data) / BLOCK_DATA_SIZE))
    print("Total blocks:", total_blocks)

    avail = make_avail_data_info(image_data, data_type)
    await send_cmd(ch, CMD_START_DATA_TRANSFER, avail)

    completed = False
    pending_raw = None
    while not completed:
        if pending_raw is not None:
            raw = pending_raw
            pending_raw = None
        else:
            raw = await wait_notification(ch, timeout_s=20)
        rsp_cmd, rsp_payload = parse_cmd(raw)

        if rsp_cmd == RSP_BLOCK_REQUEST:
            if len(rsp_payload) < 17:
                raise RuntimeError("Invalid BlockRequest payload")

            req_block_id = rsp_payload[9]
            req_type = rsp_payload[10]
            req_parts_mask = rsp_payload[11:17]
            req_parts = requested_parts_from_mask(req_parts_mask)

            print("Block request: block=%d type=0x%02X parts=%s" % (req_block_id, req_type, req_parts))
            if req_block_id >= total_blocks:
                raise RuntimeError("Device requested out-of-range block %d" % req_block_id)

            await send_cmd(ch, CMD_ACK_READY)
            pending_raw = await wait_ready_ack(ch)
            if pending_raw is not None:
                continue

            for part_id in req_parts:
                block_part = build_block_part(image_data, req_block_id, part_id)
                pending_raw = await send_part_wait_ack(ch, block_part)
                if pending_raw is not None:
                    break

        elif rsp_cmd == RSP_UPLOAD_COMPLETE:
            print("Upload complete (device confirmed)")
            await send_cmd(ch, CMD_TRANSFER_COMPLETE)
            completed = True

        elif rsp_cmd == RSP_DATA_PRESENT:
            print("Device reports identical data already present")
            await send_cmd(ch, CMD_TRANSFER_COMPLETE)
            completed = True

        elif rsp_cmd == RSP_COMMAND_ACK:
            pass

        elif rsp_cmd in (RSP_PART_ACK, RSP_PART_ERROR):
            pass

        elif rsp_cmd == RSP_ERROR:
            raise RuntimeError("Device returned protocol error (0xFFFF)")

        else:
            print("Ignoring notification 0x%04X" % (rsp_cmd if rsp_cmd is not None else -1))


async def run_update_cycle():
    target_addr = sys.argv[1].lower() if len(sys.argv) > 1 else TARGET_ADDR
    data_type = DEFAULT_DATA_TYPE

    if USE_WEATHER_SOURCE:
        print("Connecting Wi-Fi...")
        wifi_ssid, wifi_password = load_wifi_credentials()
        print("Wi-Fi SSID:", wifi_ssid)
        
        wifi_success = False
        for i in range(1, CONNECT_RETRIES + 1):
            try:
                # Use a shorter timeout per attempt since we have many retries
                connect_wifi(wifi_ssid, wifi_password, timeout_s=10)
                wifi_success = True
                break
            except Exception as e:
                print("Wi-Fi connect attempt %d/%d failed: %s" % (i, CONNECT_RETRIES, e))
                time.sleep(1)
        
        if not wifi_success:
            raise RuntimeError("Wi-Fi connection failed after %d attempts" % CONNECT_RETRIES)

        print("Fetching BOM daily forecast...")
        forecast = None
        for i in range(5):
            try:
                forecast = fetch_bom_daily_forecast(FORECAST_DAYS)
                break
            except Exception as e:
                print("Fetch BOM attempt %d failed: %s" % (i+1, e))
                time.sleep(2)
        
        if not forecast:
            raise RuntimeError("Failed to fetch BOM forecast after 5 attempts")

        print("--- DEBUG FORECAST DATA ---")
        for i, d in enumerate(forecast.get("days", [])):
             print("Day %d: %s %s Min:%s Max:%s" % (i, d.get("date_short"), d.get("weekday"), d.get("temp_min"), d.get("temp_max")))
        print("---------------------------")
        image_data = render_weather_to_raw(BMP_WIDTH, BMP_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT, forecast)
        print("Rendered weather -> raw bytes:", len(image_data))
    else:
        image_data = bmp_to_raw_bw_color(BMP_PATH, BMP_WIDTH, BMP_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT)
        print("Loaded BMP:", BMP_PATH, "-> raw bytes:", len(image_data))

    device = await find_device(target_addr)
    # Checks removed to allow loop to handle retry/scan
    
    conn = None
    last_error = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        if not device:
            print("Connect attempt %d/%d skipped (device not found)" % (attempt, CONNECT_RETRIES))
            if attempt < CONNECT_RETRIES:
                await asyncio.sleep_ms(CONNECT_RETRY_DELAY_MS)
                device = await find_device(target_addr)
            continue

        try:
            print("Connect attempt %d/%d" % (attempt, CONNECT_RETRIES))
            conn = await device.connect(timeout_ms=10000)
            break
        except Exception as exc:
            last_error = exc
            print("Connect failed:", exc)
            if attempt < CONNECT_RETRIES:
                await asyncio.sleep_ms(CONNECT_RETRY_DELAY_MS)
                device = await find_device(target_addr)
                if not device:
                    print("Retry scan did not find device")

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

        await upload_image(ch, image_data, data_type)
        print("Done")

    finally:
        if conn:
            await conn.disconnect()



def get_melbourne_time():
    # Returns (year, month, day, hour, minute, second, wday, yday) in Melbourne Time
    # DST Rules:
    # Starts 1st Sunday Oct @ 2am Standard (become 3am DST) -> UTC 16:00 previous day? No.
    # Ends 1st Sunday Apr @ 3am DST (become 2am Standard)
    # But easier to work with UTC timestamps.
    
    t_utc = time.time()
    tm = time.localtime(t_utc)
    year = tm[0]

    # Find 1st Sunday in April (End DST)
    # Find 1st Sunday in October (Start DST)
    
    def get_sunday(y, m):
        # Basic mktime for 1st of month to find weekday
        # time.localtime(time.mktime(...)) handles basic logic
        # 0=Mon, 6=Sun
        t_start = time.mktime((y, m, 1, 0, 0, 0, 0, 0))
        wday = time.localtime(t_start)[6]
        # Days to add to get to Sunday (6)
        # If 0 (Mon), add 6. If 6 (Sun), add 0.
        days = (6 - wday + 7) % 7
        return 1 + days

    apr_day = get_sunday(year, 4)
    oct_day = get_sunday(year, 10)
    
    # DST Ends: April [apr_day] at 03:00 AEDT (UTC+11) -> 16:00 UTC previous day (start of DST)
    # Actually, simpler:
    # 03:00 AEDT = 16:00 UTC (previous day)
    # 02:00 AEST = 16:00 UTC (previous day)
    
    # Let's compare seconds from epoch.
    # DST End: April apr_day 03:00 Local Daylight Time (UTC+11) -> (year, 4, apr_day, 3, 0, 0) in +11
    # UTC time = Local - 11h
    # 3am - 11h = 16:00 previous day.
    dst_end_utc = time.mktime((year, 4, apr_day, 3, 0, 0, 0, 0)) - 39600 # 11h
    
    # DST Start: Oct oct_day 02:00 Local Standard Time (UTC+10) -> (year, 10, oct_day, 2, 0, 0) in +10
    # UTC time = Local - 10h
    # 2am - 10h = 16:00 previous day.
    dst_start_utc = time.mktime((year, 10, oct_day, 2, 0, 0, 0, 0)) - 36000 # 10h
    
    # Check if current UTC time is within DST period
    # DST is active if: t_utc < dst_end_utc OR t_utc >= dst_start_utc (Southern Hemisphere Summer)
    is_dst = (t_utc < dst_end_utc) or (t_utc >= dst_start_utc)
    
    offset = 39600 if is_dst else 36000
    return time.localtime(t_utc + offset)


async def main():
    print("Device starting main loop...")
    while True:
        try:
            # 1. Connect Wi-Fi and Sync Time (Daily NTP)
            if USE_WEATHER_SOURCE:
                print("Connecting Wi-Fi for time sync...")
                wifi_ssid, wifi_password = load_wifi_credentials()
                
                connected = False
                for _ in range(3):
                    try:
                        connect_wifi(wifi_ssid, wifi_password)
                        connected = True
                        break
                    except Exception as e:
                        print("Wi-Fi retry:", e)
                        await asyncio.sleep(5)
                
                if connected and ntptime:
                    print("Syncing NTP...")
                    try:
                        ntptime.settime()
                        # Debug Print Local Time
                        t_mel = get_melbourne_time()
                        print("Time synced (Melbourne): %04d-%02d-%02d %02d:%02d:%02d" % (t_mel[0], t_mel[1], t_mel[2], t_mel[3], t_mel[4], t_mel[5]))
                    except Exception as e:
                        print("NTP sync failed:", e)
                
            # 2. Check Schedule
            tm = get_melbourne_time()
            hour = tm[3]
            minute = tm[4]
            
            print("Running scheduled update cycle at %02d:%02d..." % (hour, minute))
            success = False
            for attempt in range(3):
                try:
                    await run_update_cycle()
                    success = True
                    break
                except Exception as e:
                    print("Update cycle failed (attempt %d/3): %s" % (attempt + 1, e))
                    await asyncio.sleep(10)
            
            if not success:
               print("Update cycle failed after 3 attempts. Skipping to next scheduled slot.")
            
            # 3. Calculate sleep until next slot
            # Slots: 05:30, 13:00 (Local Time)
            tm = get_melbourne_time() # Refresh time after update
            h, m = tm[3], tm[4]
            current_minutes = h * 60 + m
            current_seconds_in_day = current_minutes * 60 + tm[5]
            
            slots = [(5, 30), (13, 0)]
            slot_minutes = [sh * 60 + sm for sh, sm in slots]
            slot_minutes.sort()
            
            next_slot_mins = None
            for slot in slot_minutes:
                if slot > current_minutes:
                    next_slot_mins = slot
                    break
            
            if next_slot_mins is None:
                # Tomorrow's first slot
                next_slot_mins = slot_minutes[0] + 1440 # Add 24 hours
                
            sleep_minutes = next_slot_mins - current_minutes
            sleep_seconds = sleep_minutes * 60 - tm[5]
            
            if sleep_seconds < 0: sleep_seconds = 0
            
            print("Sleeping for %d seconds (%d min) until next update..." % (sleep_seconds, sleep_seconds // 60))
            
            # Sleep Loop
            remaining = sleep_seconds
            while remaining > 0:
                chunk = 60 if remaining > 60 else remaining
                await asyncio.sleep(chunk)
                remaining -= chunk
                
        except Exception as e:
            print("Error in main loop:", e)
            print("Retrying in 60 seconds...")
            await asyncio.sleep(60)

try:
    asyncio.run(main())
except Exception as e:
    print("Fatal Error encountered: %s" % e)
    print("Rebooting in 10 seconds...")
    time.sleep(10)
    machine.reset()