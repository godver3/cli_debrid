"""
Plex Collection Poster Renderer
Generates custom poster artwork for Plex collections using PIL/Pillow.
Supports 8 design templates with configurable accent color, eyebrow text, and icon.
"""

import hashlib
import io
import logging
import math
import os
import requests
import threading
from pathlib import Path
from typing import Optional, Tuple, List

try:
    from PIL import Image, ImageDraw, ImageFilter
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("[CollectionPoster] PIL/Pillow not available — poster generation disabled")

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent.parent
ASSETS_LOGOS = APP_DIR / 'overlays' / 'assets' / 'logos'
STATIC_DIR = APP_DIR / 'static'
PREVIEW_DIR = STATIC_DIR / 'img' / 'collection_poster_previews'

# Poster canvas size — 1000x1500 (2:3, matches Agregarr specs)
W, H = 1000, 1500

# Default source icons
SOURCE_ICONS = {
    'Trakt Lists':   str(ASSETS_LOGOS / 'rating' / 'trakt_square.png'),
    'MDBList':       str(ASSETS_LOGOS / 'rating' / 'MDBList.png'),
    'Adaptive List': str(STATIC_DIR / 'color-icon.png'),
}

# Design metadata
DESIGNS = {
    0: {'name': 'Plex Default', 'preview': 'default.jpg',  'default_accent': ''},
    1: {'name': 'Layout 1',     'preview': 'design_1.jpg', 'default_accent': '#E6A800'},
    3: {'name': 'Layout 2',     'preview': 'design_3.jpg', 'default_accent': '#039900'},
    4: {'name': 'Layout 3',     'preview': 'design_4.jpg', 'default_accent': '#DC3C64'},
    6: {'name': 'Layout 4',     'preview': 'design_6.jpg', 'default_accent': '#FF0000'},
    8: {'name': 'Layout 5',     'preview': 'design_8.jpg', 'default_accent': '#50B4FF'},
}


# ── Colour helpers ─────────────────────────────────────────────────────────────

def _hex(h: str) -> Tuple[int, int, int]:
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _rgba(h: str, a: int = 255) -> Tuple[int, int, int, int]:
    r, g, b = _hex(h)
    return (r, g, b, a)


def _darken(color: Tuple, factor: float = 0.5) -> Tuple:
    return tuple(max(0, int(c * factor)) for c in color)


def _lighten(color: Tuple, factor: float = 1.5) -> Tuple:
    return tuple(min(255, int(c * factor)) for c in color)


# ── Drawing primitives ─────────────────────────────────────────────────────────

def _radial_glow(size: Tuple[int, int], color_rgb: Tuple, radius: float = 0.4,
                 cx: float = 0.5, cy: float = 0.5, opacity: int = 80) -> Image.Image:
    """Create a radial gradient glow layer."""
    glow = Image.new('RGBA', size, (0, 0, 0, 0))
    pix = glow.load()
    sw, sh = size
    r, g, b = color_rgb
    for py in range(sh):
        for px in range(sw):
            dx = (px / sw - cx) / radius
            dy = (py / sh - cy) / (radius * sh / sw)
            d = math.sqrt(dx*dx + dy*dy)
            alpha = max(0, int(opacity * (1 - min(1.0, d))))
            pix[px, py] = (r, g, b, alpha)
    return glow


def _rounded_rect(draw: ImageDraw.Draw, xy: Tuple, radius: int,
                  fill=None, outline=None, width: int = 1):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    if fill:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill,
                               outline=outline, width=width)
    elif outline:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=None,
                               outline=outline, width=width)


def _linear_gradient(size: Tuple, c1: Tuple, c2: Tuple, angle: float = 150) -> Image.Image:
    """Create a linear gradient image."""
    img = Image.new('RGBA', size)
    pix = img.load()
    w, h = size
    rad = math.radians(angle - 90)
    dx, dy = math.cos(rad), math.sin(rad)
    for py in range(h):
        for px in range(w):
            nx = px / (w - 1) if w > 1 else 0.5
            ny = py / (h - 1) if h > 1 else 0.5
            t = max(0.0, min(1.0, (w*(nx-0.5)*dx + h*(ny-0.5)*dy) / math.sqrt(w*w+h*h) + 0.5))
            pix[px, py] = tuple(int(c1[i] + (c2[i]-c1[i])*t) for i in range(4))
    return img


def _load_icon(path: str, size: int = 76) -> Optional[Image.Image]:
    """Load and resize an icon image."""
    try:
        img = Image.open(path).convert('RGBA')
        if size > 0:
            img.thumbnail((size, size), Image.LANCZOS)
        return img
    except Exception as e:
        logger.warning(f"[CollectionPoster] Could not load icon {path}: {e}")
        return None


_FONT_CACHE_DIR = Path(os.environ.get('USER_CONFIG', '/user/config')) / 'overlay_fonts_cache'

# Map family names to cached TTF filenames (already downloaded by overlay system)
_FONT_FILE_MAP = {
    ('DM Sans', False):           'DM_Sans-Regular.ttf',
    ('DM Sans', True):            'DM_Sans-Bold.ttf',
    ('Bebas Neue', False):        'Bebas_Neue-Regular.ttf',
    ('Bebas Neue', True):         'Bebas_Neue-Bold.ttf',
    ('Oswald', False):            'Oswald-Bold.ttf',
    ('Oswald', True):             'Oswald-Bold.ttf',
    ('Nunito', False):            'Nunito-Bold.ttf',
    ('Nunito', True):             'Nunito-Bold.ttf',
    ('Playfair Display', False):  'Playfair_Display-Bold.ttf',
    ('Playfair Display', True):   'Playfair_Display-Bold.ttf',
    # Fallbacks for unavailable fonts
    ('Cormorant Garamond', False): 'Playfair_Display-Bold.ttf',
    ('Cormorant Garamond', True):  'Playfair_Display-Bold.ttf',
    ('Syne', False):               'DM_Sans-Bold.ttf',
    ('Syne', True):                'DM_Sans-Bold.ttf',
    ('Barlow Condensed', False):   'Barlow_Condensed-Bold.ttf',
    ('Barlow Condensed', True):    'Barlow_Condensed-Bold.ttf',
    ('Yeseva One', False):         'Playfair_Display-Bold.ttf',
    ('Yeseva One', True):          'Playfair_Display-Bold.ttf',
}


def _load_font(family: str, size: int, bold: bool = False):
    """Load a font from the overlay font cache, falling back to font_manager then default."""
    from PIL import ImageFont
    # Try cached TTF first
    key = (family, bold) if (family, bold) in _FONT_FILE_MAP else (family, False)
    filename = _FONT_FILE_MAP.get(key)
    if filename:
        font_path = _FONT_CACHE_DIR / filename
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size)
            except Exception:
                pass
    # Try font_manager
    try:
        from overlays.font_manager import get_pil_font
        return get_pil_font(family, size, bold=bold)
    except Exception:
        pass
    # Last resort: any available TTF in cache
    try:
        fallback = next(iter(_FONT_CACHE_DIR.glob('DM_Sans*.ttf')), None)
        if fallback:
            return ImageFont.truetype(str(fallback), size)
    except Exception:
        pass
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.Draw, text: str, font) -> Tuple[int, int]:
    """Get text bounding box size."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        return len(text) * 10, 20


def _draw_text_shadow(draw: ImageDraw.Draw, pos: Tuple, text: str, font,
                      fill: Tuple, shadow_offset: int = 2, shadow_opacity: int = 80):
    """Draw text with a drop shadow."""
    sx, sy = pos[0] + shadow_offset, pos[1] + shadow_offset
    draw.text((sx, sy), text, font=font, fill=(0, 0, 0, shadow_opacity))
    draw.text(pos, text, font=font, fill=fill)


def _paste_icon(canvas: Image.Image, icon: Image.Image, x: int, y: int):
    """Paste an RGBA icon onto canvas."""
    if icon.mode != 'RGBA':
        icon = icon.convert('RGBA')
    canvas.paste(icon, (x, y), icon)


def _draw_circle_icon_ring(canvas: Image.Image, draw: ImageDraw.Draw,
                           cx: int, cy: int, r: int,
                           ring_color: Tuple, fill_color: Tuple,
                           icon: Optional[Image.Image] = None,
                           letter: str = '', letter_color: Tuple = (255,255,255,255),
                           font=None):
    """Draw a circle ring with optional icon or letter inside."""
    # Fill
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=fill_color)
    # Ring border
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=ring_color, width=3)
    if icon:
        icon_size = int(r * 1.1)
        icon_r = icon.resize((icon_size, icon_size), Image.LANCZOS)
        _paste_icon(canvas, icon_r, cx - icon_size//2, cy - icon_size//2)
    elif letter:
        if font:
            tw, th = _text_size(draw, letter, font)
            draw.text((cx - tw//2, cy - th//2), letter, font=font, fill=letter_color)


def _card_gradient_colors(card_index: int, accent_rgb: Tuple) -> Tuple[Tuple, Tuple]:
    """Generate per-card gradient colors based on index and accent."""
    palettes = [
        ((8, 18, 32), (20, 50, 80)),    # dark blue
        ((20, 10, 30), (45, 15, 60)),   # dark purple
        ((30, 8, 12), (65, 15, 25)),    # dark red
        ((10, 18, 12), (20, 40, 25)),   # dark green
    ]
    c1, c2 = palettes[card_index % 4]
    # Blend a hint of accent color into c2
    a = accent_rgb
    c2_blended = (
        min(255, c2[0] + a[0]//8),
        min(255, c2[1] + a[1]//8),
        min(255, c2[2] + a[2]//8),
    )
    return (c1[0], c1[1], c1[2], 255), (c2_blended[0], c2_blended[1], c2_blended[2], 255)


def _card_glow_color(card_index: int, accent_rgb: Tuple) -> Tuple:
    """Get per-card glow color."""
    bases = [
        (40, 100, 200),  # blue
        (120, 60, 220),  # purple
        (200, 40, 60),   # red
        (40, 160, 80),   # green
    ]
    b = bases[card_index % 4]
    # Mix with accent
    a = accent_rgb
    return (
        (b[0] + a[0]) // 2,
        (b[1] + a[1]) // 2,
        (b[2] + a[2]) // 2,
    )


def _build_card_grid(canvas: Image.Image, movie_thumbs: List[Optional[Image.Image]],
                     accent_rgb: Tuple, grid_y: int, card_w: int, card_h: int,
                     pad_x: int = 40, gap: int = 20,
                     overlay_opacity: int = 60,
                     show_numbers: bool = False, number_font=None,
                     corner_symbol: str = '', symbol_font=None,
                     show_tag: bool = False, tags: List[str] = None,
                     tag_font=None, tag_color: Tuple = None,
                     bottom_title_font=None, titles: List[str] = None,
                     title_color: Tuple = (255, 255, 255, 255),
                     sub_color: Tuple = None):
    """Draw the 2x2 movie card grid."""
    if tag_color is None:
        tag_color = (accent_rgb[0], accent_rgb[1], accent_rgb[2], 255)
    if sub_color is None:
        sub_color = tag_color

    positions = [
        (pad_x, grid_y),
        (pad_x + card_w + gap, grid_y),
        (pad_x, grid_y + card_h + gap),
        (pad_x + card_w + gap, grid_y + card_h + gap),
    ]

    for i, (cx, cy) in enumerate(positions):
        has_thumb = i < len(movie_thumbs) and movie_thumbs[i] is not None

        if has_thumb:
            # Real poster: fill the card cleanly
            thumb = movie_thumbs[i].copy()
            thumb = thumb.resize((card_w, card_h), Image.LANCZOS)
            card_img = thumb.convert('RGBA')
        else:
            # No thumb: plain dark card, no colored gradient
            card_img = Image.new('RGBA', (card_w, card_h), (18, 18, 22, 255))

        canvas.paste(card_img.convert('RGB'), (cx, cy))

        draw = ImageDraw.Draw(canvas)

        # 1px white border on every card
        draw.rectangle([cx, cy, cx + card_w - 1, cy + card_h - 1],
                       outline=(255, 255, 255, 60), width=1)

        # Numbers: left-center vertically, with black stroke
        if show_numbers and number_font:
            num_text = str(i + 1)
            try:
                bbox = draw.textbbox((0, 0), num_text, font=number_font)
                # bbox is (left, top, right, bottom) — top/bottom are relative to baseline
                text_h = bbox[3] - bbox[1]
                text_top_offset = bbox[1]  # offset from (0,0) to actual top of glyph
            except Exception:
                text_h, text_top_offset = 60, 0
            nx = cx + 16
            ny = cy + card_h // 2 - text_h // 2 - text_top_offset
            # Black border (stroke)
            for dx, dy in [(-1,0),(1,0),(0,-1),(0,1)]:
                draw.text((nx + dx, ny + dy), num_text, font=number_font, fill=(0, 0, 0, 220))
            draw.text((nx, ny), num_text, font=number_font, fill=(255, 255, 255, 220))

        # Only draw overlays on cards with real poster art
        if has_thumb:

            # Bottom gradient overlay + title
            fade_h = 140
            fade = Image.new('RGBA', (card_w, fade_h), (0, 0, 0, 0))
            fade_draw = ImageDraw.Draw(fade)
            max_alpha = int(255 * overlay_opacity / 100)
            for row in range(fade_h):
                alpha = int(max_alpha * (1 - row / fade_h))
                fade_draw.line([(0, fade_h - 1 - row), (card_w, fade_h - 1 - row)],
                               fill=(4, 5, 10, alpha))
            canvas.paste(fade, (cx, cy + card_h - fade_h), fade)

            # Card title text
            if bottom_title_font and titles and i < len(titles):
                title = titles[i][:22] + '…' if len(titles[i]) > 22 else titles[i]
                draw.text((cx + 12, cy + card_h - 48), title,
                          font=bottom_title_font, fill=title_color)



def _draw_plex_logo_box(canvas: Image.Image, draw: ImageDraw.Draw,
                        x: int, y: int, size: int, bg_color: Tuple):
    """Draw a Plex-style logo box (rounded square with chevron)."""
    # Box
    draw.rounded_rectangle([x, y, x+size, y+size], radius=size//4, fill=bg_color)
    # Chevron arrow pointing right
    bg = (bg_color[0]//4, bg_color[1]//4, bg_color[2]//4, 255)
    cx, cy = x + size//2, y + size//2
    s = size // 4
    chevron = [
        (cx - s, cy - s*2),
        (cx + s, cy),
        (cx - s, cy + s*2),
        (cx - s + s//2, cy + s*2),
        (cx + s + s//2, cy),
        (cx - s + s//2, cy - s*2),
    ]
    draw.polygon(chevron, fill=bg)


def _draw_pill_badge(draw: ImageDraw.Draw, x: int, y: int, text: str,
                     font, text_color: Tuple, bg_color: Tuple, border_color: Tuple):
    """Draw a rounded pill badge with text."""
    if not font:
        return
    tw, th = _text_size(draw, text, font)
    px, py = 16, 8
    draw.rounded_rectangle([x, y, x+tw+px*2, y+th+py*2], radius=24,
                            fill=bg_color, outline=border_color, width=1)
    draw.text((x+px, y+py), text, font=font, fill=text_color)


def _draw_eyebrow(draw: ImageDraw.Draw, x: int, y: int, text: str,
                  font, color: Tuple) -> int:
    """Draw eyebrow text, return height used."""
    if not text or not font:
        return 0
    # Simulate letter-spacing by drawing chars
    draw.text((x, y), text.upper(), font=font, fill=color)
    _, h = _text_size(draw, text.upper(), font)
    return h + 12


def _draw_title(draw: ImageDraw.Draw, x: int, y: int, text: str,
                font, color: Tuple, max_width: int) -> int:
    """Draw collection title, return height used. Two lines if long."""
    if not font:
        return 0
    words = text.split()
    if len(words) >= 2:
        mid = len(words) // 2
        line1 = ' '.join(words[:mid])
        line2 = ' '.join(words[mid:])
    else:
        line1 = text
        line2 = ''

    _, h1 = _text_size(draw, line1, font)
    draw.text((x, y), line1, font=font, fill=color)
    total = h1
    if line2:
        _, h2 = _text_size(draw, line2, font)
        line2_y = y + h1 + 40  # 40px gap between lines
        draw.text((x, line2_y), line2, font=font, fill=(
            min(255, color[0]+40), min(255, color[1]+40), min(255, color[2]+60),
            color[3] if len(color) > 3 else 255))
        total += h2 + 20
    return total


# ── Poster generators (one per design) ────────────────────────────────────────

def _base_canvas(bg_color: str) -> Image.Image:
    canvas = Image.new('RGBA', (W, H), _rgba(bg_color))
    return canvas


def _top_glow(canvas: Image.Image, accent_rgb: Tuple, opacity: int = 80,
              cx: float = 0.5, cy: float = -0.1, radius: float = 0.55):
    glow = _radial_glow((W, H), accent_rgb, radius=radius, cx=cx, cy=cy, opacity=opacity)
    return Image.alpha_composite(canvas, glow)


def _bottom_right_glow(canvas: Image.Image, accent_rgb: Tuple, opacity: int = 50):
    glow = _radial_glow((W, H), accent_rgb, radius=0.4, cx=1.1, cy=1.1, opacity=opacity)
    return Image.alpha_composite(canvas, glow)


def _footer_pips(draw: ImageDraw.Draw, accent_rgb: Tuple, y: int):
    """Draw pagination pip dots in footer."""
    px = W - 48
    # Active pill
    draw.rounded_rectangle([px-28, y, px, y+8], radius=4,
                            fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))
    # Inactive dots
    for i in range(2):
        ox = px - 36 - i*16
        draw.ellipse([ox-4, y, ox+4, y+8], fill=(30, 35, 50, 200))


def _footer_line(draw: ImageDraw.Draw, y: int):
    draw.line([(40, y), (W-40, y)], fill=(20, 28, 45, 120), width=1)


def render_design_1(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """All Time Favs — left-aligned, circle logo ring, stars, 2x2 grid."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#06080f')
    canvas = _top_glow(canvas, accent_rgb, opacity=glow_opacity, cx=0.5, cy=-0.05, radius=glow_radius/100)
    canvas = _bottom_right_glow(canvas, (220, 60, 80), opacity=30)
    draw = ImageDraw.Draw(canvas)

    # Fonts
    f_title = _load_font('Cormorant Garamond', 90, bold=True)
    f_eyebrow = _load_font('DM Sans', 32, bold=True)
    f_card_title = _load_font('Cormorant Garamond', 32, bold=True)
    f_num = _load_font('Cormorant Garamond', 110, bold=True)
    f_letter = _load_font('Cormorant Garamond', 34, bold=True)

    # Top bar — circle logo ring (left) + stars (right)
    icon = _load_icon(icon_path, 0) if icon_path else None
    if icon:
        # Scale icon to fit within 400x124 preserving aspect ratio (scale up or down)
        max_w, max_h = 400, 124
        ratio = min(max_w / icon.width, max_h / icon.height)
        new_w = int(icon.width * ratio)
        new_h = int(icon.height * ratio)
        icon = icon.resize((new_w, new_h), Image.LANCZOS)
        _paste_icon(canvas, icon, W//2 - icon.width//2, 44 + (max_h - icon.height)//2)

    # Stars top-right
    star_y = 62
    for i in range(5):
        sx = W - 48 - (4-i)*22
        pts = []
        for j in range(5):
            angle = math.pi * (2*j/5 - 0.5)
            r2 = 9 if j == 0 else 4
            pts.extend([sx + 9*math.cos(angle), star_y + 9*math.sin(angle)])
            angle2 = math.pi * ((2*j+1)/5 - 0.5)
            pts.extend([sx + 4*math.cos(angle2), star_y + 4*math.sin(angle2)])
        draw.polygon(pts, fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 220))

    # Text block
    tx, ty = 40, 150
    ey_h = _draw_eyebrow(draw, tx, ty, eyebrow, f_eyebrow,
                         (accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))
    ty += ey_h
    t_h = _draw_title(draw, tx, ty, collection_name, f_title,
                      (255, 255, 255, 255), W-80)

    # Card grid
    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     show_numbers=True, number_font=f_num,
                     bottom_title_font=f_card_title, titles=[''] * 4,
                     title_color=(255, 255, 255, 200),
                     sub_color=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))

    # Footer
    footer_y = grid_y + card_h*2 + 32 + 24
    _footer_line(draw, footer_y)
    _footer_pips(draw, accent_rgb, footer_y + 14)

    return canvas.convert('RGB')


def render_design_2(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """Animation — left-aligned, Plex logo box, genre pill, sparkle symbols."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#06040e')
    canvas = _top_glow(canvas, accent_rgb, opacity=glow_opacity, cx=0.0, cy=-0.1, radius=glow_radius/100)
    canvas = _bottom_right_glow(canvas, (255, 180, 0), opacity=40)
    draw = ImageDraw.Draw(canvas)

    f_title = _load_font('Nunito', 86, bold=True)
    f_eyebrow = _load_font('DM Sans', 32, bold=True)
    f_pill = _load_font('DM Sans', 18)
    f_card_title = _load_font('Nunito', 30, bold=True)
    f_symbol = _load_font('DM Sans', 28)

    # Logo box top-left
    box_size = 76
    draw.rounded_rectangle([40, 52, 40+box_size, 52+box_size],
                            radius=18, fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 255))
    icon = _load_icon(icon_path, 50) if icon_path else None
    if icon:
        _paste_icon(canvas, icon.resize((50, 50), Image.LANCZOS), 40+13, 52+13)
    else:
        # Chevron
        _draw_plex_logo_box(canvas, draw, 40, 52, box_size,
                            (accent_rgb[0], accent_rgb[1], accent_rgb[2], 255))

    # Genre pill top-right
    _draw_pill_badge(draw, W-200, 58, 'Collection', f_pill,
                     (accent_rgb[0], accent_rgb[1], accent_rgb[2], 220),
                     (accent_rgb[0], accent_rgb[1], accent_rgb[2], 25),
                     (accent_rgb[0], accent_rgb[1], accent_rgb[2], 60))

    # Text
    tx, ty = 40, 155
    ey_h = _draw_eyebrow(draw, tx, ty, eyebrow, f_eyebrow,
                         (accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))
    ty += ey_h
    _draw_title(draw, tx, ty, collection_name, f_title, (255, 255, 255, 255), W-80)

    # Grid with sparkle
    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     corner_symbol='✦', symbol_font=f_symbol,
                     bottom_title_font=f_card_title, titles=[''] * 4)

    footer_y = grid_y + card_h*2 + 32 + 24
    _footer_line(draw, footer_y)
    _footer_pips(draw, accent_rgb, footer_y + 14)
    return canvas.convert('RGB')


def render_design_3(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """Foreign Films — left-aligned, Plex box, globe pill, language tags."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#080a06')
    canvas = _top_glow(canvas, accent_rgb, opacity=glow_opacity, cx=0.5, cy=-0.08, radius=glow_radius/100)
    draw = ImageDraw.Draw(canvas)

    f_title = _load_font('Syne', 88, bold=True)
    f_eyebrow = _load_font('DM Sans', 32, bold=True)
    f_pill = _load_font('DM Sans', 18)
    f_card_title = _load_font('Syne', 30, bold=True)
    f_tag = _load_font('Syne', 20, bold=True)

    # Logo box centered
    icon = _load_icon(icon_path, 0) if icon_path else None
    if icon:
        # Scale icon to fit within 400x124 preserving aspect ratio (scale up or down)
        max_w, max_h = 400, 124
        ratio = min(max_w / icon.width, max_h / icon.height)
        new_w = int(icon.width * ratio)
        new_h = int(icon.height * ratio)
        icon = icon.resize((new_w, new_h), Image.LANCZOS)
        _paste_icon(canvas, icon, W//2 - icon.width//2, 44 + (max_h - icon.height)//2)

    # Eyebrow top-right (clear of icon)
    if eyebrow and f_eyebrow:
        ew, eh = _text_size(draw, eyebrow.upper(), f_eyebrow)
        draw.text((W - ew - 40, 58), eyebrow.upper(), font=f_eyebrow,
                  fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 220))

    tx, ty = 40, 155
    _draw_title(draw, tx, ty, collection_name, f_title, (255, 255, 255, 255), W-80)

    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     show_tag=True, tags=['ES', 'FR', 'JP', 'KO'], tag_font=f_tag,
                     tag_color=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 220),
                     bottom_title_font=f_card_title, titles=[''] * 4)

    footer_y = grid_y + card_h*2 + 32 + 24
    _footer_line(draw, footer_y)
    _footer_pips(draw, accent_rgb, footer_y + 14)
    return canvas.convert('RGB')


def render_design_4(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """Romance — centered vertical header, heart symbols, pink tones."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#1a0a14')
    canvas = _top_glow(canvas, accent_rgb, opacity=glow_opacity, cx=0.5, cy=-0.05, radius=glow_radius/100)
    canvas = _bottom_right_glow(canvas, (255, 140, 180), opacity=30)
    draw = ImageDraw.Draw(canvas)

    f_title = _load_font('Playfair Display', 72, bold=True)
    f_eyebrow = _load_font('DM Sans', 32, bold=True)
    f_service = _load_font('DM Sans', 20)
    f_card_title = _load_font('Playfair Display', 30, bold=True)
    f_heart = _load_font('DM Sans', 32)

    # Centered logo ring
    icon = _load_icon(icon_path, 0) if icon_path else None
    if icon:
        # Scale icon to fit within 400x124 preserving aspect ratio (scale up or down)
        max_w, max_h = 400, 124
        ratio = min(max_w / icon.width, max_h / icon.height)
        new_w = int(icon.width * ratio)
        new_h = int(icon.height * ratio)
        icon = icon.resize((new_w, new_h), Image.LANCZOS)
        _paste_icon(canvas, icon, W//2 - icon.width//2, 44 + (max_h - icon.height)//2)

    # Eyebrow below icon
    ey_y = 182
    if eyebrow and f_eyebrow:
        ew, eh = _text_size(draw, eyebrow.upper(), f_eyebrow)
        draw.text((W//2 - ew//2, ey_y), eyebrow.upper(), font=f_eyebrow,
                  fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 220))
        ey_y += eh + 12

    # Centered title below eyebrow
    if f_title:
        tw, th = _text_size(draw, collection_name, f_title)
        draw.text((W//2 - tw//2, ey_y), collection_name, font=f_title,
                  fill=(255, 255, 255, 255))

    # Grid with hearts
    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     corner_symbol='♥', symbol_font=f_heart,
                     bottom_title_font=f_card_title, titles=[''] * 4,
                     sub_color=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))

    # Centered footer pips
    footer_y = grid_y + card_h*2 + 32 + 24
    pill_x = W//2 - 24
    draw.rounded_rectangle([pill_x, footer_y, pill_x+48, footer_y+8], radius=4,
                            fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))
    for i in range(2):
        ox = pill_x - 24 - i*18
        draw.ellipse([ox-6, footer_y, ox+6, footer_y+8],
                     fill=(60, 20, 40, 200))
    return canvas.convert('RGB')


def render_design_5(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """Indian Cinema — saffron accent, tricolor stripe, language tags."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#09060e')
    canvas = _top_glow(canvas, (255, 100, 0), opacity=80, cx=0.5, cy=-0.08)
    canvas = _bottom_right_glow(canvas, (200, 0, 80), opacity=35)
    draw = ImageDraw.Draw(canvas)

    f_title = _load_font('DM Sans', 86, bold=True)
    f_eyebrow = _load_font('DM Sans', 32, bold=True)
    f_card_title = _load_font('DM Sans', 30, bold=True)
    f_tag = _load_font('DM Sans', 20, bold=True)

    # Logo box
    icon = _load_icon(icon_path, 0) if icon_path else None
    if icon:
        # Scale icon to fit within 400x124 preserving aspect ratio (scale up or down)
        max_w, max_h = 400, 124
        ratio = min(max_w / icon.width, max_h / icon.height)
        new_w = int(icon.width * ratio)
        new_h = int(icon.height * ratio)
        icon = icon.resize((new_w, new_h), Image.LANCZOS)
        _paste_icon(canvas, icon, W//2 - icon.width//2, 44 + (max_h - icon.height)//2)

    # Tricolor flag strip top-right
    flag_colors = [(255, 153, 51), (255, 255, 255), (19, 136, 8)]
    for i, fc in enumerate(flag_colors):
        fx = W - 52 - (2-i)*22
        draw.rounded_rectangle([fx, 60, fx+14, 104], radius=2, fill=(*fc, 220))

    tx, ty = 40, 155
    ey_h = _draw_eyebrow(draw, tx, ty, eyebrow, f_eyebrow,
                         (accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))
    ty += ey_h
    _draw_title(draw, tx, ty, collection_name, f_title, (255, 255, 255, 255), W-80)

    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     show_tag=True, tags=['HI', 'TA', 'TE', 'HI'], tag_font=f_tag,
                     tag_color=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 220),
                     bottom_title_font=f_card_title, titles=[''] * 4)

    footer_y = grid_y + card_h*2 + 32 + 24
    _footer_line(draw, footer_y)
    _footer_pips(draw, accent_rgb, footer_y + 14)
    return canvas.convert('RGB')


def render_design_6(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """Top 10 / Netflix style — header bar, Bebas Neue, rank numbers."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#0a0a0a')
    canvas = _top_glow(canvas, accent_rgb, opacity=glow_opacity, cx=-0.1, cy=-0.1, radius=glow_radius/100)
    draw = ImageDraw.Draw(canvas)

    f_header = _load_font('Bebas Neue', 52, bold=False)
    f_section = _load_font('DM Sans', 22)
    f_rank_label = _load_font('DM Sans', 32, bold=True)
    f_title_main = _load_font('Bebas Neue', 96)
    f_num = _load_font('Bebas Neue', 128)
    f_card_title = _load_font('Bebas Neue', 40)

    # Header bar — logo | divider | section label
    if f_header:
        draw.text((40, 52), 'TOP 10', font=f_header,
                  fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 255))
        hw, _ = _text_size(draw, 'TOP 10', f_header)
    else:
        hw = 140
    draw.line([(40+hw+20, 60), (40+hw+20, 100)], fill=(60, 60, 60, 200), width=2)
    icon = _load_icon(icon_path, 0) if icon_path else None
    if icon:
        # Scale icon to fit within 400x124 preserving aspect ratio (scale up or down)
        max_w, max_h = 400, 124
        ratio = min(max_w / icon.width, max_h / icon.height)
        new_w = int(icon.width * ratio)
        new_h = int(icon.height * ratio)
        icon = icon.resize((new_w, new_h), Image.LANCZOS)
        _paste_icon(canvas, icon, W//2 - icon.width//2, 44 + (max_h - icon.height)//2)
    elif f_section:
        draw.text((40+hw+36, 66), 'Collection', font=f_section, fill=(120, 120, 120, 200))

    # Rank label + main title
    ry = 148
    if f_rank_label and eyebrow:
        draw.text((40, ry), eyebrow.upper(), font=f_rank_label,
                  fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 220))
        _, _ey_h = _text_size(draw, eyebrow.upper(), f_rank_label)
        ry += _ey_h + 8
    if f_title_main:
        _draw_title(draw, 40, ry, collection_name, f_title_main,
                    (255, 255, 255, 255), W-80)

    # Grid with large rank numbers
    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     show_numbers=True, number_font=f_num,
                     bottom_title_font=f_card_title, titles=[''] * 4,
                     title_color=(255, 255, 255, 220))

    # Footer — dots only (no pill)
    footer_y = grid_y + card_h*2 + 32 + 24
    draw.line([(40, footer_y), (W-40, footer_y)], fill=(30, 30, 30, 180), width=1)
    for i in range(3):
        cx2 = W - 48 - i*18
        if i == 0:
            draw.ellipse([cx2-5, footer_y+10, cx2+5, footer_y+20],
                         fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))
        else:
            draw.ellipse([cx2-5, footer_y+10, cx2+5, footer_y+20], fill=(50, 50, 50, 200))
    return canvas.convert('RGB')


def render_design_7(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """Curated List / Plex Foreign redesign — centered vertical, globe emoji, ordinal numbers."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#111008')
    canvas = _top_glow(canvas, accent_rgb, opacity=glow_opacity, cx=0.5, cy=-0.08, radius=glow_radius/100)
    draw = ImageDraw.Draw(canvas)

    f_title = _load_font('Syne', 76, bold=True)
    f_service = _load_font('DM Sans', 20)
    f_card_title = _load_font('Syne', 30, bold=True)
    f_ord = _load_font('Syne', 22, bold=True)
    f_glob = _load_font('DM Sans', 26)

    # Centered logo box
    box_size = 88
    bx = W//2 - box_size//2
    draw.rounded_rectangle([bx, 44, bx+box_size, 44+box_size],
                            radius=20, fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 255))
    icon = _load_icon(icon_path, 60) if icon_path else None
    if icon:
        _paste_icon(canvas, icon.resize((60, 60), Image.LANCZOS), bx+14, 44+14)

    # Service tag centered
    svc = eyebrow or 'Curated Collection'
    if f_service:
        sw, sh = _text_size(draw, svc.upper(), f_service)
        draw.text((W//2 - sw//2, 148), svc.upper(), font=f_service,
                  fill=(accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))

    # Centered title
    if f_title:
        tw, th = _text_size(draw, collection_name, f_title)
        draw.text((W//2 - tw//2, 190), collection_name, font=f_title,
                  fill=(255, 255, 255, 255))

    # Grid with ordinal numbers + globe
    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     corner_symbol='🌍', symbol_font=f_glob,
                     show_numbers=True, number_font=f_ord,
                     bottom_title_font=f_card_title, titles=[''] * 4)

    footer_y = grid_y + card_h*2 + 32 + 24
    _footer_pips(draw, accent_rgb, footer_y + 14)
    return canvas.convert('RGB')


def render_design_8(collection_name: str, eyebrow: str, accent: str,
                    icon_path: str, movie_thumbs: List, source_type: str,
                    overlay_opacity: int = 60, glow_opacity: int = 80,
                    glow_radius: int = 55) -> Image.Image:
    """4K Premium — K ring + brand name, resolution pill, resolution tags per card."""
    accent_rgb = _hex(accent)
    canvas = _base_canvas('#050508')
    canvas = _top_glow(canvas, accent_rgb, opacity=glow_opacity, cx=0.5, cy=-0.08, radius=glow_radius/100)
    draw = ImageDraw.Draw(canvas)

    f_title = _load_font('Oswald', 90, bold=True)
    f_eyebrow = _load_font('DM Sans', 32, bold=True)
    f_brand = _load_font('DM Sans', 20)
    f_pill = _load_font('Oswald', 22, bold=True)
    f_card_title = _load_font('Oswald', 34, bold=True)
    f_tag = _load_font('Oswald', 20, bold=True)

    # Top-left: circle ring + brand name
    icon = _load_icon(icon_path, 0) if icon_path else None
    if icon:
        # Scale icon to fit within 400x124 preserving aspect ratio (scale up or down)
        max_w, max_h = 400, 124
        ratio = min(max_w / icon.width, max_h / icon.height)
        new_w = int(icon.width * ratio)
        new_h = int(icon.height * ratio)
        icon = icon.resize((new_w, new_h), Image.LANCZOS)
        _paste_icon(canvas, icon, W//2 - icon.width//2, 44 + (max_h - icon.height)//2)
    tx, ty = 40, 155
    ey_h = _draw_eyebrow(draw, tx, ty, eyebrow, f_eyebrow,
                         (accent_rgb[0], accent_rgb[1], accent_rgb[2], 200))
    ty += ey_h
    _draw_title(draw, tx, ty, collection_name, f_title, (255, 255, 255, 255), W-80)

    grid_y = 454
    card_w, card_h = 308, 462
    _build_card_grid(canvas, movie_thumbs, accent_rgb, grid_y,
                     card_w, card_h, pad_x=182, gap=32, overlay_opacity=overlay_opacity,
                     bottom_title_font=f_card_title, titles=[''] * 4)

    footer_y = grid_y + card_h*2 + 32 + 24
    _footer_line(draw, footer_y)
    _footer_pips(draw, accent_rgb, footer_y + 14)
    return canvas.convert('RGB')


DESIGN_RENDERERS = {
    1: render_design_1,
    3: render_design_3,
    4: render_design_4,
    6: render_design_6,
    8: render_design_8,
}


# ── Public API ─────────────────────────────────────────────────────────────────

def get_source_icon(source_type: str, override_icon: str = '') -> str:
    """Return icon path for a source type, with optional override."""
    if override_icon:
        # Strip leading /logos/ prefix if present (saved by icon picker)
        clean = override_icon.lstrip('/')
        if clean.startswith('logos/'):
            clean = clean[len('logos/'):]
        for base in [ASSETS_LOGOS, Path('/user/config/overlay_assets/logos')]:
            full = base / clean
            if full.exists():
                return str(full)
    return SOURCE_ICONS.get(source_type, SOURCE_ICONS.get('Adaptive List', ''))


def compute_poster_hash(design_id: int, accent: str, eyebrow: str,
                        icon: str, collection_name: str,
                        first_4_ms_ids: List[str],
                        overlay_opacity: int = 60,
                        glow_opacity: int = 80,
                        glow_radius: int = 55) -> str:
    """Compute a hash to detect when poster needs re-rendering."""
    key = f"{design_id}|{accent}|{eyebrow}|{icon}|{collection_name}|{overlay_opacity}|{glow_opacity}|{glow_radius}|{'|'.join(first_4_ms_ids)}"
    return hashlib.md5(key.encode()).hexdigest()


def fetch_movie_thumbs(plex_url: str, plex_token: str,
                       collection_ratingkey: str,
                       limit: int = 4,
                       tmdb_map: dict = None) -> List[Optional[Image.Image]]:
    """tmdb_map: optional {ms_item_id: (tmdb_id, media_type)} from sync_collection_for_source."""
    """Fetch first N movie poster thumbnails from a Plex collection."""
    thumbs = []
    try:
        headers = {'X-Plex-Token': plex_token, 'Accept': 'application/json'}
        resp = requests.get(
            f"{plex_url}/library/collections/{collection_ratingkey}/children",
            headers=headers, timeout=30
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get('MediaContainer', {}).get('Metadata', [])[:limit]
        for item in items:
            rk = item.get('ratingKey', '')
            thumb_url = None
            # Try to get the original (non-overlay) poster
            try:
                pr = requests.get(f"{plex_url}/library/metadata/{rk}/posters",
                                  headers=headers, timeout=20)
                if pr.status_code == 200:
                    posters = pr.json().get('MediaContainer', {}).get('Metadata', [])
                    # Prefer metadata:// (original) over upload:// (overlay)
                    original = next((p['key'] for p in posters if 'metadata' in p.get('key', '')), None)
                    if not original:
                        # Fall back to any non-upload, non-selected poster key
                        original = next((p['key'] for p in posters
                                         if not p.get('ratingKey', '').startswith('upload://')
                                         and not p.get('selected')), None)
                    if original:
                        thumb_url = original
            except Exception:
                pass
            # Fall back to current thumb (overlay) if no original found
            if not thumb_url:
                thumb_url = item.get('thumb', '')

            img = None

            # 1. Try Plex original/metadata poster URL
            if thumb_url:
                try:
                    tr = requests.get(f"{plex_url}{thumb_url}",
                                      headers={'X-Plex-Token': plex_token},
                                      timeout=20)
                    if tr.status_code == 200 and len(tr.content) > 1024:
                        img = Image.open(io.BytesIO(tr.content)).convert('RGBA')
                except Exception:
                    pass

            # 2. Fall back to TMDB poster cache (populated by library/discover, always available)
            if img is None:
                try:
                    import pickle
                    from database.core import get_db_connection as _get_db
                    _cache_path = Path(os.environ.get('USER_CONFIG', '/user/config')).parent / 'db_content' / 'poster_cache.pkl'
                    if _cache_path.exists():
                        with open(str(_cache_path), 'rb') as _pf:
                            _pcache = pickle.load(_pf)
                        # Use passed tmdb_map if available, otherwise fall back to DB
                        _tid, _mtype = None, 'movie'
                        if tmdb_map and rk in tmdb_map:
                            _tid, _mtype = str(tmdb_map[rk][0]), tmdb_map[rk][1]
                        else:
                            _conn = _get_db()
                            try:
                                _row = _conn.execute('SELECT tmdb_id, type FROM media_items WHERE ms_item_id=? LIMIT 1', (rk,)).fetchone()
                            finally:
                                _conn.close()
                            if _row and _row[0]:
                                _tid, _mtype = str(_row[0]), (_row[1] or 'movie')
                        if _tid:
                            _suffix = f'{_tid}_movie' if _mtype == 'movie' else f'{_tid}_tv'
                            _entry = _pcache.get(_suffix) or _pcache.get(f'{_tid}_movie') or _pcache.get(f'{_tid}_tv')
                            if _entry and isinstance(_entry, tuple) and _entry[0]:
                                _tr = requests.get(_entry[0], timeout=20)
                                if _tr.status_code == 200 and len(_tr.content) > 1024:
                                    img = Image.open(io.BytesIO(_tr.content)).convert('RGBA')
                except Exception:
                    pass

            # 3. Last resort: local overlay backup if overlays are enabled
            if img is None:
                try:
                    _backup = Path(os.environ.get('USER_CONFIG', '/user/config')) / 'poster_backups' / f"{rk}_original.jpg"
                    if _backup.exists():
                        img = Image.open(str(_backup)).convert('RGBA')
                except Exception:
                    pass

            thumbs.append(img)
    except Exception as e:
        logger.warning(f"[CollectionPoster] fetch_movie_thumbs error: {e}")
    while len(thumbs) < limit:
        thumbs.append(None)
    return thumbs


def render_collection_poster(design_id: int, collection_name: str,
                              eyebrow: str, accent: str, icon_override: str,
                              source_type: str,
                              movie_thumbs: List[Optional[Image.Image]],
                              overlay_opacity: int = 60,
                              glow_opacity: int = 80,
                              glow_radius: int = 55) -> Optional[bytes]:
    """
    Render a collection poster and return JPEG bytes.
    design_id: 1-8 (0 = no custom poster)
    """
    if not PIL_AVAILABLE:
        logger.error("[CollectionPoster] PIL not available")
        return None
    if design_id not in DESIGN_RENDERERS:
        return None

    icon_path = get_source_icon(source_type, icon_override)
    renderer = DESIGN_RENDERERS[design_id]

    try:
        img = renderer(
            collection_name=collection_name or 'My Collection',
            eyebrow=eyebrow or '',
            accent=accent or '#E6A800',
            icon_path=icon_path,
            movie_thumbs=movie_thumbs,
            source_type=source_type,
            overlay_opacity=overlay_opacity,
            glow_opacity=glow_opacity,
            glow_radius=glow_radius,
        )
        import time as _time
        rgb = img.convert('RGB')
        # Set 1 bottom-right pixel to a unique value based on timestamp
        # This prevents Plex from deduplicating identical uploads
        ts = int(_time.time())
        r_val = ts & 0xFF
        g_val = (ts >> 8) & 0xFF
        b_val = (ts >> 16) & 0xFF
        rgb.putpixel((rgb.width - 1, rgb.height - 1), (r_val, g_val, b_val))
        buf = io.BytesIO()
        rgb.save(buf, format='JPEG', quality=92)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"[CollectionPoster] render error design {design_id}: {e}", exc_info=True)
        return None


def upload_collection_poster(plex_url: str, plex_token: str,
                             collection_ratingkey: str,
                             poster_bytes: bytes) -> str | None:
    """Upload poster JPEG to a Plex collection and set it as the active poster.

    Returns the bare hash (the part after 'upload://posters/') of the newly
    selected poster on success, or None on failure.  Callers that only need a
    bool can treat any truthy return value as success.
    """
    try:
        import requests as _req
        posters_url = f"{plex_url}/library/metadata/{collection_ratingkey}/posters?X-Plex-Token={plex_token}"
        select_url  = f"{plex_url}/library/metadata/{collection_ratingkey}/poster?X-Plex-Token={plex_token}"

        # Step 1: Snapshot existing upload:// keys before uploading
        existing_keys = set()
        snap = _req.get(posters_url, headers={'Accept': 'application/json'}, timeout=15)
        if snap.status_code == 200:
            for p in snap.json().get('MediaContainer', {}).get('Metadata', []):
                rk = p.get('ratingKey', '')
                if rk.startswith('upload://'):
                    existing_keys.add(rk)

        # Step 2: Upload the poster image
        resp = _req.post(
            posters_url,
            data=poster_bytes,
            headers={'Content-Type': 'image/jpeg', 'Accept': 'application/json'},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.error(f"[CollectionPoster] Upload returned {resp.status_code} for {collection_ratingkey}")
            return None

        # Step 3: Find the newly added upload:// key (not in the pre-upload snapshot).
        # If Plex deduplicates (same image already uploaded), no new key appears —
        # in that case use the selected poster key if it's an upload://, since Plex
        # will have auto-selected the matching existing poster.
        after = _req.get(posters_url, headers={'Accept': 'application/json'}, timeout=15)
        new_key = None
        selected_key = None
        if after.status_code == 200:
            for p in after.json().get('MediaContainer', {}).get('Metadata', []):
                rk = p.get('ratingKey', '')
                if rk.startswith('upload://') and rk not in existing_keys:
                    new_key = rk
                    break
                if p.get('selected') and rk.startswith('upload://'):
                    selected_key = rk

        if not new_key:
            # Plex deduplicated — reuse the existing upload key that is now selected
            if selected_key:
                logger.info(f"[CollectionPoster] Plex deduplicated upload for {collection_ratingkey}, reusing selected key")
                new_key = selected_key
            else:
                logger.warning(f"[CollectionPoster] Could not identify new upload key for {collection_ratingkey}")
                return None

        # Step 4: SELECT the new poster as active
        sel_resp = _req.put(
            select_url,
            params={'url': new_key},
            headers={'Accept': 'application/json'},
            timeout=15,
        )
        logger.info(f"[CollectionPoster] Uploaded and selected poster for {collection_ratingkey} (select status={sel_resp.status_code})")

        # Return bare hash (strip 'upload://posters/' prefix) so callers can
        # store it for cleanup-safe preservation.
        prefix = 'upload://posters/'
        return new_key[len(prefix):] if new_key.startswith(prefix) else new_key
    except Exception as e:
        logger.error(f"[CollectionPoster] Upload failed: {e}", exc_info=True)
        return None


_preview_generation_lock = threading.Lock()
_previews_generated = False

def generate_preview_images():
    """
    Generate static preview PNG files for all 8 designs.
    Only runs once per process — guarded by lock to prevent repeated calls.
    """
    global _previews_generated
    if not PIL_AVAILABLE:
        return
    # Fast check without lock
    if _previews_generated:
        return
    with _preview_generation_lock:
        # Double-check inside lock
        if _previews_generated:
            return
        _previews_generated = True  # Set before generation to prevent re-entry

    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

    # Copy default poster
    # Use the app's own plexcollection.png as the default preview
    default_src = STATIC_DIR / 'image' / 'plexcollection.png'
    if not default_src.exists():
        default_src = Path('/home/mash2k3/Documents/Projects/Tangerine_CLI/posters/transcode.jpeg')
    if default_src.exists():
        from PIL import Image as _Image
        _img = _Image.open(str(default_src))
        _img.save(str(PREVIEW_DIR / 'default.jpg'), 'JPEG', quality=92)
    else:
        # Create a placeholder
        img = Image.new('RGB', (W, H), (20, 20, 30))
        draw = ImageDraw.Draw(img)
        draw.text((W//2-60, H//2-15), 'Plex Default', fill=(100, 100, 120))
        img.save(str(PREVIEW_DIR / 'default.jpg'), 'JPEG', quality=85)

    # Preview configs for active designs only
    # Designs 1 and 6 show numbers on cards — pass placeholder thumbs as None (dark cards shown)
    # The numbered designs render numbers even on dark placeholders via show_numbers param
    preview_configs = [
        # (design_id, eyebrow, source_type) — accent uses each design's default_accent
        (1, 'MY COLLECTION', 'Trakt Lists'),
        (3, 'WORLD CINEMA',  'Trakt Lists'),
        (4, 'ROMANCE',       'Adaptive List'),
        (6, 'THIS WEEK',     'Trakt Lists'),
        (8, 'PREMIUM',       'Adaptive List'),
    ]

    for design_id, eyebrow, source_type in preview_configs:
        accent = DESIGNS.get(design_id, {}).get('default_accent', '#E6A800') or '#E6A800'
        out_path = PREVIEW_DIR / f'design_{design_id}.jpg'
        if out_path.exists():
            continue  # Skip if already generated
        try:
            icon_path = get_source_icon(source_type, '')
            renderer = DESIGN_RENDERERS[design_id]
            img = renderer(
                collection_name='My Collection',
                eyebrow=eyebrow,
                accent=accent,
                icon_path=icon_path,
                movie_thumbs=[None, None, None, None],
                source_type=source_type,
            )
            img.convert('RGB').save(str(out_path), 'JPEG', quality=95)
            logger.info(f"[CollectionPoster] Generated preview design_{design_id}.jpg")
        except Exception as e:
            logger.error(f"[CollectionPoster] Preview gen failed design {design_id}: {e}", exc_info=True)


def regenerate_all_previews():
    """Force regeneration of all preview images (for admin use)."""
    if PREVIEW_DIR.exists():
        import shutil
        for f in PREVIEW_DIR.glob('design_*.jpg'):
            f.unlink(missing_ok=True)
    generate_preview_images()
