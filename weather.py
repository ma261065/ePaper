import gc
import struct
import sys
import time

import asyncio
import esp32
import framebuf
import machine
import network
import socket
import ujson
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
WIFI_CONNECT_RETRIES = 20
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
BOM_API_BASE = "http://api.weather.bom.gov.au/v1"
BOM_LOCATION_QUERY = "Williamstown"  # Default, will be overridden by NVS
BOM_LOCATION_STATE = "VIC"  # Default, will be overridden by NVS
BOM_LOCATION_GEOHASH = ""
FORECAST_DAYS = 8
# Timezone defaults for Australian Eastern Time
DEFAULT_TZ_OFFSET_SECONDS = 36000  # UTC+10 (10 hours in seconds)
DEFAULT_DST_ENABLED = True

# Pre-allocate HTTP response buffer once at startup (heap is clean).
# Reused every cycle — never freed — so it can't cause fragmentation.
_HTTP_BUF = bytearray(24576)  # 24 KB, plenty for BOM API responses


def urlencode_simple(value):
    for ch, enc in [(" ", "%20"), ("&", "%26"), ("+", "%2B"), ("#", "%23")]:
        value = value.replace(ch, enc)
    return value


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
        gc.collect()
        try:
            # Parse "http://host/path"
            host_start = url.index('//') + 2
            path_start = url.index('/', host_start)
            host = url[host_start:path_start]
            path = url[path_start:]

            s = socket.socket()
            s.settimeout(15)
            try:
                addr = socket.getaddrinfo(host, 80, 0, socket.SOCK_STREAM)[0][-1]
                s.connect(addr)

                # Send request in small pieces (no string concat allocation)
                s.send(b'GET ')
                s.send(path.encode())
                s.send(b' HTTP/1.0\r\nHost: ')
                s.send(host.encode())
                s.send(b'\r\n\r\n')

                # Skip response headers
                while True:
                    line = s.readline()
                    if not line or line == b'\r\n':
                        break

                # Read body into pre-allocated buffer (no large heap allocation)
                mv = memoryview(_HTTP_BUF)
                total = 0
                while total < len(_HTTP_BUF):
                    n = s.readinto(mv[total:])
                    if not n:
                        break
                    total += n
            finally:
                s.close()

            return ujson.loads(mv[:total])

        except OSError as e:
            print("HTTP GET failed (Attempt %d/%d): %s" % (attempt + 1, retries, e))
            last_exc = e
            # ENOMEM (errno 12) means heap is fragmented; retrying won't help
            if e.args and e.args[0] == 12:
                break
            gc.collect()
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

    # Return cached geohash if available for this location
    cache_key = "%s_%s" % (location_query.lower(), location_state.lower())
    if cache_key in _GEOHASH_CACHE:
        return _GEOHASH_CACHE[cache_key]

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

    geohash = preferred.get("geohash")
    _GEOHASH_CACHE[cache_key] = (geohash, loc_name)
    return geohash, loc_name


def _get_tz_offset(t_utc=None):
    """Get timezone offset in seconds, including DST if applicable.
    
    Uses Australian DST rules (1st Sunday Oct to 1st Sunday Apr).
    
    Args:
        t_utc: UTC epoch time to evaluate DST for (default: current time)
    
    Returns:
        Offset in seconds to add to UTC to get local time.
    """
    tz_offset = _TZ_CONFIG["tz_offset"]
    if not _TZ_CONFIG["dst_enabled"]:
        return tz_offset
    
    if t_utc is None:
        t_utc = time.time()
    year = time.localtime(t_utc)[0]
    
    def get_first_sunday(y, m):
        t_start = time.mktime((y, m, 1, 0, 0, 0, 0, 0))
        wday = time.localtime(t_start)[6]
        days = (6 - wday + 7) % 7
        return 1 + days
    
    apr_day = get_first_sunday(year, 4)
    oct_day = get_first_sunday(year, 10)
    
    # DST End: 1st Sunday Apr @ 3:00 AM DST → convert to UTC
    dst_end_utc = time.mktime((year, 4, apr_day, 3, 0, 0, 0, 0)) - (tz_offset + 3600)
    # DST Start: 1st Sunday Oct @ 2:00 AM standard → convert to UTC
    dst_start_utc = time.mktime((year, 10, oct_day, 2, 0, 0, 0, 0)) - tz_offset
    
    # DST active: Oct→Apr (Southern Hemisphere)
    is_dst = (t_utc < dst_end_utc) or (t_utc >= dst_start_utc)
    return tz_offset + (3600 if is_dst else 0)


def _utc_date_to_local(date_iso):
    """Convert a BOM ISO date/datetime string to local time tuple.
    
    Handles multiple ISO 8601 formats from BOM API:
      - Date only:          "2026-02-24"          (treated as local date)
      - UTC datetime:       "2026-02-23T13:00:00Z"
      - Offset datetime:    "2026-02-24T00:00:00+11:00"
    
    Returns:
        time tuple (year, month, day, hour, min, sec, wday, yday) or None on error.
    """
    if not date_iso or len(date_iso) < 10:
        return None
    
    year = int(date_iso[0:4])
    month = int(date_iso[5:7])
    day = int(date_iso[8:10])
    
    # Parse time component if present (e.g., "2026-02-22T13:00:00Z")
    hour, minute, second = 0, 0, 0
    has_time = len(date_iso) >= 19 and date_iso[10] == 'T'
    if has_time:
        hour = int(date_iso[11:13])
        minute = int(date_iso[14:16])
        second = int(date_iso[17:19])
    
    try:
        # Build epoch from parsed date/time components
        t_parsed = time.mktime((year, month, day, hour, minute, second, 0, 0))
        
        if not has_time:
            # Date-only string (e.g., "2026-02-24") — treat as local date.
            # Return local time tuple directly; no UTC→local conversion needed.
            return time.localtime(t_parsed)
        
        # Check for timezone info after the time component
        tz_part = date_iso[19:]  # e.g., "Z", "+11:00", "+1100", "-05:00"
        
        if tz_part == 'Z' or tz_part == '':
            # UTC timestamp or no timezone — treat as UTC
            t_utc = t_parsed
        else:
            # Parse timezone offset: +HH:MM, -HH:MM, +HHMM, -HHMM
            sign = 1 if tz_part[0] == '+' else -1
            tz_str = tz_part[1:].replace(':', '')
            tz_h = int(tz_str[0:2])
            tz_m = int(tz_str[2:4]) if len(tz_str) >= 4 else 0
            # Convert parsed local time to UTC by subtracting the source offset
            t_utc = t_parsed - sign * (tz_h * 3600 + tz_m * 60)
        
        return time.localtime(t_utc + _get_tz_offset(t_utc))
    except Exception:
        return None


def _weekday_name(date_iso):
    tm = _utc_date_to_local(date_iso)
    if tm is None:
        return "---"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return names[tm[6]]

def _date_str_local(date_iso):
    tm = _utc_date_to_local(date_iso)
    if tm is None:
        return 0, 0
    return tm[2], tm[1]

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

            palette_data = None
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



def _draw_text_compat(fb, x, y, text, color, scale=2):
    if draw_text_bitmap:
        draw_text_bitmap(fb, x, y, text, color, scale=scale)
    else:
        fb.text(text, x, y, color)


def _transpose_landscape_planes(land_black, land_color, bmp_width, bmp_height, display_width, display_height):
    plane_size = (display_width * display_height) // 8
    # Single output buffer: black plane then color plane (avoids extra copy from concatenation)
    out = bytearray(plane_size * 2)
    # Initialize black plane to 0xFF, color plane stays 0x00
    for i in range(plane_size):
        out[i] = 0xFF

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
                out[plane_size + byte_pos] |= (1 << bit_in_byte)
            elif is_black:
                out[byte_pos] &= ~(1 << bit_in_byte)

    return out


def render_weather_to_raw(bmp_width, bmp_height, display_width, display_height, forecast):
    """Render live weather in landscape layout, then transpose to portrait display format."""
    gc.collect()
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
        del display
    else:
        # Fallback for minimal rendering
        if not forecast.get("days"):
            _draw_text_compat(fb_black, 12, 12, "No forecast", 0)

    del fb_black, fb_yellow
    result = _transpose_landscape_planes(land_black, land_color, bmp_width, bmp_height, display_width, display_height)
    del land_black, land_color
    gc.collect()
    return result


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


async def run_update_cycle(wlan=None):
    """Fetch weather, render display, and upload to BLE device."""
    # Load target address from NVS, or override via command-line argument
    if len(sys.argv) > 1:
        target_addr = sys.argv[1].lower()
    else:
        target_addr = load_target_address()

    # Fetch and render weather image
    if USE_WEATHER_SOURCE:
        print("Fetching BOM daily forecast...")
        gc.collect()
        print("Free memory before HTTP: %d bytes" % gc.mem_free())
        forecast = None
        last_err = None
        for i in range(5):
            try:
                forecast = fetch_bom_daily_forecast(FORECAST_DAYS)
                break
            except Exception as e:
                last_err = e
                print("Fetch BOM attempt %d failed: %s" % (i+1, e))
                # ENOMEM = heap fragmented, further retries are useless
                if isinstance(e, OSError) and e.args and e.args[0] == 12:
                    break
                gc.collect()
                time.sleep(2)
        
        if not forecast:
            if isinstance(last_err, OSError) and last_err.args and last_err.args[0] == 12:
                print("Heap fragmented (ENOMEM). Rebooting to reclaim memory...")
                time.sleep(2)
                machine.reset()
            raise RuntimeError("Failed to fetch BOM forecast after 5 attempts")

        # Disconnect WiFi before rendering to free SSL/socket buffers
        if wlan:
            try:
                wlan.disconnect()
                wlan.active(False)
            except Exception:
                pass
            gc.collect()
            print("Free memory after WiFi off: %d bytes" % gc.mem_free())

        image_data = render_weather_to_raw(BMP_WIDTH, BMP_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT, forecast)
        del forecast
        gc.collect()
        print("Rendered weather -> raw bytes:", len(image_data))
    else:
        image_data = bmp_to_raw_bw_color(BMP_PATH, BMP_WIDTH, BMP_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT)
        print("Loaded BMP:", BMP_PATH, "-> raw bytes:", len(image_data))

    # Upload to BLE display
    gc.collect()
    print("Free memory before BLE: %d bytes" % gc.mem_free())
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

# Geohash cache: {"location_state": (geohash, display_name)}
_GEOHASH_CACHE = {}


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
    
    Uses timezone offset and DST settings from _get_tz_offset().
    """
    t_utc = time.time()
    return time.localtime(t_utc + _get_tz_offset(t_utc))


def _calc_sleep_seconds():
    """Calculate seconds until next scheduled slot (05:30 or 13:00 local)."""
    tm = get_local_time()
    h, m = tm[3], tm[4]
    current_minutes = h * 60 + m

    slots = [(5, 30), (13, 0)]
    slot_minutes = sorted(sh * 60 + sm for sh, sm in slots)

    next_slot_mins = None
    for slot in slot_minutes:
        if slot > current_minutes:
            next_slot_mins = slot
            break

    if next_slot_mins is None:
        next_slot_mins = slot_minutes[0] + 1440  # Tomorrow's first slot

    sleep_seconds = (next_slot_mins - current_minutes) * 60 - tm[5]
    if sleep_seconds < 60:
        sleep_seconds = 60
    return sleep_seconds


async def main():
    print("Device starting main loop...")

    # Trigger GC more frequently to prevent fragmentation
    gc.threshold(gc.mem_free() // 4 + gc.mem_alloc() // 4)

    # Load configuration from NVS
    location_name, location_state, tz_offset_sec, dst_enabled = load_location_config()
    set_location_config(location_name, location_state)
    set_timezone_config(tz_offset_sec, dst_enabled)

    # Cache Wi-Fi credentials once at startup
    wifi_ssid = None
    wifi_password = None
    if USE_WEATHER_SOURCE:
        wifi_ssid, wifi_password = load_wifi_credentials()
        print("Wi-Fi SSID:", wifi_ssid)

    while True:
        # Force-clean any stale WiFi state from previous cycle
        try:
            _wlan = network.WLAN(network.STA_IF)
            _wlan.disconnect()
            _wlan.active(False)
        except Exception:
            pass
        gc.collect()
        free = gc.mem_free()
        print("Free memory: %d bytes" % free)
        if free < 100000:
            print("Memory critically low (%d bytes). Rebooting to reclaim..." % free)
            time.sleep(2)
            machine.reset()
        wlan = None
        try:
            # 1. Connect Wi-Fi once per cycle
            if USE_WEATHER_SOURCE:
                wifi_success = False
                for i in range(1, WIFI_CONNECT_RETRIES + 1):
                    try:
                        wlan = connect_wifi(wifi_ssid, wifi_password, timeout_s=10)
                        wifi_success = True
                        break
                    except Exception as e:
                        print("Wi-Fi connect attempt %d/%d failed: %s" % (i, WIFI_CONNECT_RETRIES, e))
                        time.sleep(1)

                if not wifi_success:
                    print("Wi-Fi connection failed after %d attempts. Rebooting..." % WIFI_CONNECT_RETRIES)
                    time.sleep(5)
                    machine.reset()

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
                    wlan = None  # run_update_cycle disconnects WiFi
                    success = True
                    break
                except Exception as e:
                    print("Update cycle failed (attempt %d/3): %s" % (attempt + 1, e))
                    gc.collect()
                    await asyncio.sleep(10)

            if not success:
                print("Update cycle failed after 3 attempts. Skipping to next scheduled slot.")

            # 4. Calculate sleep until next slot
            sleep_seconds = _calc_sleep_seconds()

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
        finally:
            # Disconnect Wi-Fi to save power during sleep
            if wlan:
                try:
                    wlan.active(False)
                    print("Wi-Fi disabled for sleep.")
                except Exception:
                    pass
            gc.collect()

try:
    asyncio.run(main())
except Exception as e:
    print("Fatal Error encountered: %s" % e)
    print("Rebooting in 10 seconds...")
    time.sleep(10)
    machine.reset()