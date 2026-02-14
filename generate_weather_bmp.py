
#!/usr/bin/env python3
"""Generate a landscape BMP with weather layout using bitmap fonts (no PIL dependency)."""

from bitmap_font import BITMAP_FONT, CHAR_WIDTH, CHAR_HEIGHT
import math

def render_bitmap_text_to_pixels(pixels, width, height, x, y, text, color):
    """Render bitmap font text directly to pixel array (RGB tuples)."""
    for char_idx, char in enumerate(text):
        if char not in BITMAP_FONT:
            char = ' '
        
        char_x = x + char_idx * CHAR_WIDTH
        font_data = BITMAP_FONT[char]
        
        # Draw each row of the character
        for row_idx, row_byte in enumerate(font_data):
            char_y = y + row_idx
            if char_y >= height:
                break
            # Draw each bit in the row (left to right), using bits 7-3
            for bit_idx in range(5):
                pixel_x = char_x + bit_idx
                if pixel_x >= width:
                    continue
                pixel = (row_byte >> (7 - bit_idx)) & 1
                if pixel:
                    pixels[pixel_x, char_y] = color

# Create landscape RGB image
width, height = 480, 176
pixels = {}
for y in range(height):
    for x in range(width):
        pixels[(x, y)] = (255, 255, 255)  # White background

# Define colors
BLACK = (0, 0, 0)
YELLOW = (255, 255, 0)

def draw_line(x1, y1, x2, y2, color):
    """Draw a simple line using Bresenham's algorithm."""
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx - dy
    
    x, y = x1, y1
    while True:
        if 0 <= x < width and 0 <= y < height:
            pixels[(x, y)] = color
        if x == x2 and y == y2:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy

def draw_circle(cx, cy, radius, color, thickness=1):
    """Draw a circle outline using Midpoint Circle Algorithm."""
    x = radius
    y = 0
    err = 0
    
    while x >= y:
        # 8-way symmetry
        for px, py in [(cx+x, cy+y), (cx+y, cy+x), (cx-y, cy+x), (cx-x, cy+y),
                       (cx-x, cy-y), (cx-y, cy-x), (cx+y, cy-x), (cx+x, cy-y)]:
            if 0 <= px < width and 0 <= py < height:
                pixels[(px, py)] = color
        
        y += 1
        if err <= 0:
            err += 2 * y + 1
        if err > 0:
            x -= 1
            err -= 2 * x + 1


def draw_hline(x, y, length, color):
    for xx in range(x, x + length):
        if 0 <= xx < width and 0 <= y < height:
            pixels[(xx, y)] = color


def draw_vline(x, y, length, color):
    for yy in range(y, y + length):
        if 0 <= x < width and 0 <= yy < height:
            pixels[(x, yy)] = color


def draw_rect(x, y, w, h, color):
    draw_hline(x, y, w, color)
    draw_hline(x, y + h - 1, w, color)
    draw_vline(x, y, h, color)
    draw_vline(x + w - 1, y, h, color)


def fill_rect(x, y, w, h, color):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            if 0 <= xx < width and 0 <= yy < height:
                pixels[(xx, yy)] = color


def draw_cloud(cx, cy, color):
    draw_circle(cx - 7, cy + 2, 5, color)
    draw_circle(cx, cy - 2, 7, color)
    draw_circle(cx + 8, cy + 2, 5, color)
    draw_hline(cx - 12, cy + 7, 25, color)


def draw_sun(cx, cy, radius, color):
    draw_circle(cx, cy, radius, color)
    for angle_deg in range(0, 360, 45):
        angle_rad = math.radians(angle_deg)
        x1 = cx + (radius + 2) * math.cos(angle_rad)
        y1 = cy + (radius + 2) * math.sin(angle_rad)
        x2 = cx + (radius + 9) * math.cos(angle_rad)
        y2 = cy + (radius + 9) * math.sin(angle_rad)
        draw_line(int(x1), int(y1), int(x2), int(y2), color)


def draw_rain(cx, cy, color):
    draw_line(cx - 8, cy, cx - 12, cy + 8, color)
    draw_line(cx, cy, cx - 4, cy + 8, color)
    draw_line(cx + 8, cy, cx + 4, cy + 8, color)


def draw_wind(cx, cy, color):
    draw_line(cx - 14, cy - 4, cx + 12, cy - 4, color)
    draw_line(cx - 10, cy + 1, cx + 14, cy + 1, color)
    draw_line(cx - 14, cy + 6, cx + 10, cy + 6, color)


def draw_icon_descriptor(x, y, icon_name, compact=False):
    icon = (icon_name or "cloudy").lower()

    if "sun" in icon or icon in ("clear", "mostly_sunny"):
        draw_circle(x + 10, y + 10, 7, YELLOW)
        ray_dx = [0, 4, 6, 4, 0, -4, -6, -4]
        ray_dy = [-8, -6, 0, 6, 8, 6, 0, -6]
        for idx in range(8):
            draw_line(x + 10, y + 10, x + 10 + ray_dx[idx], y + 10 + ray_dy[idx], YELLOW)

    if "cloud" in icon or "shower" in icon or "rain" in icon or "storm" in icon:
        if compact:
            draw_circle(x + 7, y + 10, 4, BLACK)
            draw_circle(x + 12, y + 8, 5, BLACK)
            draw_circle(x + 17, y + 10, 4, BLACK)
            draw_hline(x + 4, y + 14, 15, BLACK)
        else:
            fill_rect(x + 3, y + 10, 18, 8, BLACK)
            draw_circle(x + 7, y + 10, 4, BLACK)
            draw_circle(x + 12, y + 8, 5, BLACK)
            draw_circle(x + 17, y + 10, 4, BLACK)

    if "shower" in icon or "rain" in icon or "storm" in icon:
        if compact:
            draw_line(x + 7, y + 18, x + 5, y + 22, BLACK)
            draw_line(x + 12, y + 18, x + 10, y + 22, BLACK)
            draw_line(x + 17, y + 18, x + 15, y + 22, BLACK)
        else:
            draw_line(x + 6, y + 20, x + 4, y + 24, BLACK)
            draw_line(x + 12, y + 20, x + 10, y + 24, BLACK)
            draw_line(x + 18, y + 20, x + 16, y + 24, BLACK)

# Layout frames
draw_rect(0, 0, width, height, BLACK)
divider_x = 272
draw_vline(divider_x, 0, height, BLACK)

# 5-day strip on right (compact stacked cards)
panel_x = divider_x + 6
panel_w = width - panel_x - 8
card_h = 30
card_gap = 4
top_pad = 2

days = ["MON", "TUE", "WED", "THU", "FRI"]
highs = ["21°", "26°", "23°", "19°", "25°"]
lows = ["13°", "15°", "14°", "12°", "16°"]
icons = ["cloud", "sun", "rain", "cloud", "sun"]

for idx in range(5):
    y0 = top_pad + idx * (card_h + card_gap)
    draw_rect(panel_x, y0, panel_w, card_h, BLACK)
    day_x = panel_x + 6
    icon_x = panel_x + 66
    temp_x = panel_x + 96

    render_bitmap_text_to_pixels(pixels, width, height, day_x, y0 + 4, days[idx], BLACK)

    draw_icon_descriptor(icon_x - 10, y0 + 3, icons[idx], compact=True)

    render_bitmap_text_to_pixels(pixels, width, height, temp_x, y0 + 10, highs[idx], YELLOW)
    render_bitmap_text_to_pixels(pixels, width, height, temp_x + 30, y0 + 10, lows[idx], BLACK)

# Left "today" panel
render_bitmap_text_to_pixels(pixels, width, height, 14, 8, "TODAY", YELLOW)
draw_hline(14, 18, 64, BLACK)

# Big partly-cloudy-rain icon (same style as app icon builder, scaled by layering)
draw_sun(96, 62, 20, YELLOW)
draw_cloud(116, 70, BLACK)
draw_rain(120, 86, BLACK)

# Today temperature/rain/wind lines
render_bitmap_text_to_pixels(pixels, width, height, 14, 112, "High 24°", BLACK)
render_bitmap_text_to_pixels(pixels, width, height, 90, 112, "Low 14°", YELLOW)
render_bitmap_text_to_pixels(pixels, width, height, 14, 138, "Rain: 5mm", BLACK)
render_bitmap_text_to_pixels(pixels, width, height, 14, 152, "Wind: 18 km/h", BLACK)
draw_wind(220, 155, BLACK)

# Convert pixel dict to BMP
from struct import pack

# BMP header
bmp_data = bytearray()
bmp_data += b'BM'  # Signature
bmp_data += pack('<I', 54 + width * height * 3)  # File size
bmp_data += pack('<I', 0)  # Reserved
bmp_data += pack('<I', 54)  # Pixel data offset

# DIB header
bmp_data += pack('<I', 40)  # Header size
bmp_data += pack('<i', width)  # Width
bmp_data += pack('<i', -height)  # Height (negative = top-down)
bmp_data += pack('<H', 1)  # Planes
bmp_data += pack('<H', 24)  # Bits per pixel (24-bit RGB)
bmp_data += pack('<I', 0)  # Compression (none)
bmp_data += pack('<I', width * height * 3)  # Image size
bmp_data += pack('<i', 2835)  # X pixels per meter
bmp_data += pack('<i', 2835)  # Y pixels per meter
bmp_data += pack('<I', 0)  # Colors used
bmp_data += pack('<I', 0)  # Important colors

# Pixel data (BGR format for BMP)
for y in range(height):
    for x in range(width):
        r, g, b = pixels[(x, y)]
        bmp_data += pack('BBB', b, g, r)  # BMP uses BGR order

with open('weather_test.bmp', 'wb') as f:
    f.write(bmp_data)

print("Created weather_test.bmp (480x176 landscape, bitmap font, no PIL required)")
