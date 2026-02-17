import struct
import sys
import time

import asyncio
import esp32
import framebuf
import machine
import network
import urequests
try:
    import ntptime
except ImportError:
    ntptime = None

try:
    from bitmap_font import draw_text_bitmap
except ImportError:
    # Fallback to framebuf text if bitmap_font not available
    draw_text_bitmap = None

try:
    from display import WeatherDisplay
except ImportError:
    WeatherDisplay = None

try:
    from ble_display import BLEDisplay
except ImportError as e:
    print("Warning: Failed to import BLEDisplay:", e)
    BLEDisplay = None


CONNECT_RETRIES = 200
CONNECT_RETRY_DELAY_MS = 1200
BMP_PATH = "image.bmp"
BMP_WIDTH = 480  # BMP is landscape
BMP_HEIGHT = 176
DISPLAY_WIDTH = 176  # Display is portrait
DISPLAY_HEIGHT = 480

USE_WEATHER_SOURCE = True
WIFI_SSID = ""
WIFI_PASSWORD = ""
NVS_NAMESPACE = "weather"
NVS_KEY_WIFI_SSID = "wifi_ssid"
NVS_KEY_WIFI_PASSWORD = "wifi_pass"
NVS_KEY_LOCATION_NAME = "loc_name"
NVS_KEY_LOCATION_STATE = "loc_state"
NVS_KEY_TIMEZONE_OFFSET = "tz_offset"
NVS_KEY_DST_ENABLED = "dst_enabled"
NVS_KEY_TARGET_ADDR = "target_addr"
WIFI_SWITCH_ENABLE_PIN = 3
WIFI_ANT_CONFIG_PIN = 14
BOM_API_BASE = "https://api.weather.bom.gov.au/v1"
BOM_LOCATION_QUERY = "Williamstown"  # Default, will be overridden by NVS
BOM_LOCATION_STATE = "VIC"  # Default, will be overridden by NVS
BOM_LOCATION_GEOHASH = ""
FORECAST_DAYS = 8
# Timezone defaults for Australian Eastern Time
DEFAULT_TZ_OFFSET_SECONDS = 36000  # UTC+10 (10 hours in seconds)
DEFAULT_DST_ENABLED = True





def urlencode_simple(value):
    return value.replace(" ", "%20")


def connect_wifi(ssid, password, timeout_s=30):
    # Enable external antenna on Seeed Studio ESP32-C6
    # Comment out the 4 lines below if you are not using a Seeed ESP32-C6, or if you want to use the internal antenna
    wifi_switch_enable = machine.Pin(WIFI_SWITCH_ENABLE_PIN, machine.Pin.OUT)
    wifi_ant_config = machine.Pin(WIFI_ANT_CONFIG_PIN, machine.Pin.OUT)
    wifi_switch_enable.value(0)
    wifi_ant_config.value(1)

    network.hostname("weather")

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        print('Wifi already connected as {}/{}, net={}, gw={}, dns={}'.format(network.hostname(), *wlan.ifconfig()))
        return wlan

    print("Connecting to WiFi...")
    wlan.connect(ssid, password)
    start = time.time()
    while not wlan.isconnected() and (time.time() - start) < timeout_s:
        time.sleep(0.25)

    if not wlan.isconnected():
        raise RuntimeError("Wi-Fi connect failed")
    
    print('Wifi connected as {}/{}, net={}, gw={}, dns={}'.format(network.hostname(), *wlan.ifconfig()))
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


def load_target_address():
    """Load BLE target address from NVS.
    
    Returns:
        Target BLE device MAC address (e.g., "3c:60:55:84:a0:42")
    
    Raises:
        RuntimeError: If target address missing from NVS
    """
    nvs = esp32.NVS(NVS_NAMESPACE)
    target_addr = _nvs_get_text(nvs, NVS_KEY_TARGET_ADDR)
    if not target_addr:
        raise RuntimeError(
            "Target BLE address missing in NVS (%s:%s)"
            % (NVS_NAMESPACE, NVS_KEY_TARGET_ADDR)
        )
    return target_addr.lower()


def load_location_config():
    """Load location and timezone configuration from NVS.
    
    Returns:
        (location_name, location_state, tz_offset_seconds, dst_enabled)
    
    Raises:
        RuntimeError: If any required configuration is missing from NVS
    """
    nvs = esp32.NVS(NVS_NAMESPACE)
    
    location_name = _nvs_get_text(nvs, NVS_KEY_LOCATION_NAME)
    if not location_name:
        raise RuntimeError(
            "Location name missing in NVS (%s:%s)"
            % (NVS_NAMESPACE, NVS_KEY_LOCATION_NAME)
        )
    
    location_state = _nvs_get_text(nvs, NVS_KEY_LOCATION_STATE)
    if not location_state:
        raise RuntimeError(
            "Location state missing in NVS (%s:%s)"
            % (NVS_NAMESPACE, NVS_KEY_LOCATION_STATE)
        )
    
    # NVS stores as blob, retrieve as int by unpacking
    try:
        tz_offset_buf = bytearray(4)
        nvs.get_blob(NVS_KEY_TIMEZONE_OFFSET, tz_offset_buf)
        tz_offset_seconds = struct.unpack('>i', bytes(tz_offset_buf))[0]
    except OSError:
        raise RuntimeError(
            "Timezone offset missing in NVS (%s:%s)"
            % (NVS_NAMESPACE, NVS_KEY_TIMEZONE_OFFSET)
        )
    
    try:
        dst_enabled_buf = bytearray(1)
        nvs.get_blob(NVS_KEY_DST_ENABLED, dst_enabled_buf)
        dst_enabled = dst_enabled_buf[0] != 0
    except OSError:
        raise RuntimeError(
            "DST enabled flag missing in NVS (%s:%s)"
            % (NVS_NAMESPACE, NVS_KEY_DST_ENABLED)
        )
    
    print("Config: %s, %s | TZ offset: %d sec (UTC%+.1f) | DST: %s" % (
        location_name, location_state, tz_offset_seconds, tz_offset_seconds / 3600, dst_enabled))
    
    return location_name, location_state, tz_offset_seconds, dst_enabled


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


def resolve_bom_location(location_query=None, location_state=None):
    """Resolve location name and geohash from BOM API.
    
    Args:
        location_query: Location name (default: from global config)
        location_state: State code (default: from global config)
    
    Returns:
        (geohash, location_name) tuple
    """
    if location_query is None:
        location_query = _LOCATION_CONFIG["name"]
    if location_state is None:
        location_state = _LOCATION_CONFIG["state"]
        
    if BOM_LOCATION_GEOHASH:
        return BOM_LOCATION_GEOHASH, location_query

    search_url = "%s/locations?search=%s" % (BOM_API_BASE, urlencode_simple(location_query))
    payload = http_get_json(search_url)
    locations = payload.get("data") or []
    if not locations:
        raise RuntimeError("No BOM location result for '%s'" % location_query)

    preferred = None
    for location in locations:
        if (location.get("state") or "").upper() == location_state.upper():
            preferred = location
            break

    if preferred is None:
        preferred = locations[0]

    loc_name = preferred.get("name", location_query)
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
    "snow": "rain", 
    "frost": "fog",
    "wind": "cloudy",
    "raindrops": "raindrops", 
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

    # Dark Mode (White Text on Black Background)
    fb_black.fill(1)
    fb_yellow.fill(0)

    # Use WeatherDisplay module if available
    if WeatherDisplay:
        display = WeatherDisplay(fb_black, fb_yellow, _draw_text_compat, _draw_icon,
                                draw_bmp_icon, _month_name, bmp_width, bmp_height)
        display.render(forecast)
    else:
        # Fallback for minimal rendering
        if not forecast.get("days"):
            _draw_text_compat(fb_black, 12, 12, "No forecast", 0)

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


async def run_update_cycle(wlan):
    """Fetch weather, render display, and upload to BLE device.
    
    Args:
        wlan: Connected WiFi network object (from connect_wifi)
    """
    # Load target address from NVS, or override via command-line argument
    if len(sys.argv) > 1:
        target_addr = sys.argv[1].lower()
    else:
        target_addr = load_target_address()

    # Fetch and render weather image
    if USE_WEATHER_SOURCE:
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

    # Upload to BLE display
    if BLEDisplay:
        try:
            display = BLEDisplay(target_addr, 
                               connect_retries=CONNECT_RETRIES,
                               connect_retry_delay_ms=CONNECT_RETRY_DELAY_MS)
            await display.upload(image_data)
            print("Weather display updated successfully.")
        except Exception as e:
            print("BLE upload failed:", e)
            raise
    else:
        print("Error: BLEDisplay module not available")
        print("Make sure ble_display.py is uploaded to the device")
        raise RuntimeError("Failed to import BLE display module")

# Global timezone config
_TZ_CONFIG = {
    "tz_offset": DEFAULT_TZ_OFFSET_SECONDS,
    "dst_enabled": DEFAULT_DST_ENABLED
}

# Global location config
_LOCATION_CONFIG = {
    "name": BOM_LOCATION_QUERY,
    "state": BOM_LOCATION_STATE
}


def set_timezone_config(tz_offset_seconds, dst_enabled):
    """Set global timezone configuration."""
    global _TZ_CONFIG
    _TZ_CONFIG["tz_offset"] = tz_offset_seconds
    _TZ_CONFIG["dst_enabled"] = dst_enabled


def set_location_config(location_name, location_state):
    """Set global location configuration."""
    global _LOCATION_CONFIG
    _LOCATION_CONFIG["name"] = location_name
    _LOCATION_CONFIG["state"] = location_state


def get_local_time():
    """Returns (year, month, day, hour, minute, second, wday, yday) in local time.
    
    Uses timezone offset and DST settings from configuration.
    For Australian locations with DST: activates 1st Sunday Oct to 1st Sunday Apr.
    For locations without DST: uses fixed offset year-round.
    """
    t_utc = time.time()
    year = time.localtime(t_utc)[0]
    
    tz_offset = _TZ_CONFIG["tz_offset"]
    dst_enabled = _TZ_CONFIG["dst_enabled"]
    
    if not dst_enabled:
        # Simple fixed offset (no DST)
        return time.localtime(t_utc + tz_offset)
    
    # Australian DST rules: 1st Sunday Oct @ 2am Standard -> 1st Sunday Apr @ 3am DST
    def get_sunday(y, m):
        t_start = time.mktime((y, m, 1, 0, 0, 0, 0, 0))
        wday = time.localtime(t_start)[6]
        days = (6 - wday + 7) % 7
        return 1 + days

    apr_day = get_sunday(year, 4)
    oct_day = get_sunday(year, 10)
    
    # DST End: April apr_day 03:00 DST (UTC+11) -> UTC 16:00 previous day
    dst_end_utc = time.mktime((year, 4, apr_day, 3, 0, 0, 0, 0)) - 39600  # 11h
    
    # DST Start: Oct oct_day 02:00 Standard (UTC+10) -> UTC 16:00 previous day
    dst_start_utc = time.mktime((year, 10, oct_day, 2, 0, 0, 0, 0)) - 36000  # 10h
    
    # DST active: Oct->Apr (Southern Hemisphere)
    is_dst = (t_utc < dst_end_utc) or (t_utc >= dst_start_utc)
    
    # Use DST offset (+1 hour) if active
    offset = tz_offset + (3600 if is_dst else 0)
    return time.localtime(t_utc + offset)


async def main():
    print("Device starting main loop...")
    
    # Load configuration from NVS
    location_name, location_state, tz_offset_sec, dst_enabled = load_location_config()
    set_location_config(location_name, location_state)
    set_timezone_config(tz_offset_sec, dst_enabled)
    
    while True:
        try:
            # 1. Connect Wi-Fi once per cycle
            wlan = None
            if USE_WEATHER_SOURCE:
                wifi_ssid, wifi_password = load_wifi_credentials()
                print("Wi-Fi SSID:", wifi_ssid)
                
                wifi_success = False
                for i in range(1, CONNECT_RETRIES + 1):
                    try:
                        wlan = connect_wifi(wifi_ssid, wifi_password, timeout_s=10)
                        wifi_success = True
                        break
                    except Exception as e:
                        print("Wi-Fi connect attempt %d/%d failed: %s" % (i, CONNECT_RETRIES, e))
                        time.sleep(1)
                
                if not wifi_success:
                    raise RuntimeError("Wi-Fi connection failed after %d attempts" % CONNECT_RETRIES)
                
                # 2. Sync NTP time
                if ntptime:
                    print("Syncing NTP...")
                    try:
                        ntptime.settime()
                        t_mel = get_local_time()
                        print("Time synced (local): %04d-%02d-%02d %02d:%02d:%02d" % (t_mel[0], t_mel[1], t_mel[2], t_mel[3], t_mel[4], t_mel[5]))
                    except Exception as e:
                        print("NTP sync failed:", e)
            
            # 3. Run scheduled update
            tm = get_local_time()
            hour = tm[3]
            minute = tm[4]
            
            print("Running scheduled update cycle at %02d:%02d..." % (hour, minute))
            success = False
            for attempt in range(3):
                try:
                    await run_update_cycle(wlan)
                    success = True
                    break
                except Exception as e:
                    print("Update cycle failed (attempt %d/3): %s" % (attempt + 1, e))
                    await asyncio.sleep(10)
            
            if not success:
                print("Update cycle failed after 3 attempts. Skipping to next scheduled slot.")
            
            # 4. Calculate sleep until next slot
            # Slots: 05:30, 13:00 (Local Time)
            tm = get_local_time() # Refresh time after update
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