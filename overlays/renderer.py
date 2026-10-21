"""
Overlay Renderer

Renders overlays onto media server posters based on templates.
"""

import logging
import os
from io import BytesIO
from typing import Dict, Any, Optional, Union
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None
    logging.warning("PIL/Pillow not installed. Overlay rendering will not work.")

# Logo library directories (two-tier: user first, system fallback)
_SYSTEM_LOGO_DIR = Path(__file__).parent / 'assets' / 'logos'
_USER_LOGO_DIR = Path('/user/config/overlay_assets/logos')


class OverlayRenderer:
    """
    Renders overlay elements onto poster images.

    For POC: Uses hardcoded template with simple text rendering.
    Later: Will support JSON templates with image assets.
    """

    def __init__(self, asset_dir: str = "/user/config/overlay_assets"):
        """
        Initialize the overlay renderer.

        Args:
            asset_dir: Path to overlay assets directory
        """
        self.asset_dir = Path(asset_dir)
        self.logger = logging.getLogger(__name__)

        if not Image:
            raise ImportError("PIL/Pillow is required for overlay rendering. Install with: pip install Pillow")

    def render_overlay_poc(self, base_image: Union[str, Path, Image.Image],
                           media_info: Dict[str, Any]) -> Image.Image:
        """
        POC: Render hardcoded overlay template onto base poster.

        This is a proof-of-concept implementation with a hardcoded template.
        It renders resolution and HDR badges as text in the top-left corner.

        Args:
            base_image: Path to base poster image or PIL Image object
            media_info: Dictionary containing media information:
                - resolution: str (e.g., "2160p", "1080p")
                - hdr: bool (True if HDR content)
                - dolby_vision: bool (True if Dolby Vision)
                - audio_codec: str (e.g., "TrueHD Atmos", "DTS-X")
                - video_codec: str (e.g., "HEVC", "AVC")

        Returns:
            PIL Image object with overlay applied
        """
        # Load base image
        if isinstance(base_image, (str, Path)):
            img = Image.open(base_image)
        else:
            img = base_image.copy()

        # Convert to RGBA for transparency support
        if img.mode != 'RGBA':
            img = img.convert('RGBA')

        # Create drawing context
        draw = ImageDraw.Draw(img)

        # Get image dimensions
        width, height = img.size

        # Calculate badge sizes (proportional to poster size)
        badge_height = int(height * 0.08)  # 8% of poster height
        badge_width = int(width * 0.35)    # 35% of poster width
        margin = int(width * 0.02)         # 2% margin

        # Try to load a nice font, fallback to default
        try:
            # Try to use a bold sans-serif font
            font_size = int(badge_height * 0.5)
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        except (OSError, IOError) as _font_err:
            self.logger.warning(f"Could not load DejaVuSans-Bold for render_overlay_poc, using PIL default: {_font_err}")
            font = ImageFont.load_default()

        # Build badge text
        badges = []

        # Resolution badge
        resolution = media_info.get('resolution', '')
        if resolution:
            badges.append(resolution.upper())

        # HDR badges
        if media_info.get('dolby_vision'):
            badges.append('DV')
        elif media_info.get('hdr'):
            badges.append('HDR')

        # Audio codec badge
        audio_codec = media_info.get('audio_codec', '')
        if 'atmos' in audio_codec.lower():
            badges.append('ATMOS')
        elif 'dts-x' in audio_codec.lower() or 'dtsx' in audio_codec.lower():
            badges.append('DTS:X')
        elif 'truehd' in audio_codec.lower():
            badges.append('TrueHD')
        elif 'dts-hd ma' in audio_codec.lower():
            badges.append('DTS-HD MA')

        # Render badges
        y_offset = margin
        for badge_text in badges:
            # Calculate text size
            bbox = draw.textbbox((0, 0), badge_text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]

            # Badge background position
            x1 = margin
            y1 = y_offset
            x2 = x1 + text_width + margin * 2
            y2 = y1 + text_height + margin

            # Draw semi-transparent background
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rounded_rectangle(
                [(x1, y1), (x2, y2)],
                radius=5,
                fill=(0, 0, 0, 180)  # Semi-transparent black
            )
            img = Image.alpha_composite(img, overlay)

            # Redraw context after composite
            draw = ImageDraw.Draw(img)

            # Draw text (white)
            text_x = x1 + margin
            text_y = y1 + margin // 2
            draw.text((text_x, text_y), badge_text, font=font, fill=(255, 255, 255, 255))

            # Move to next badge position
            y_offset = y2 + margin // 2

        self.logger.info(f"Rendered POC overlay with badges: {', '.join(badges)}")
        return img

    def render_from_template(self, base_image: Union[str, Path, Image.Image],
                            layout_data: Dict[str, Any],
                            media_info: Dict[str, Any]) -> Image.Image:
        """
        Render overlay from JSON layout definition.

        Supports two formats:
        - v2 (badge-centric): layout_data contains ``badges`` list
        - legacy: layout_data contains ``elements`` list

        Args:
            base_image: Path to base poster image or PIL Image object
            layout_data: JSON layout definition
            media_info: Media information dictionary

        Returns:
            PIL Image object with overlay applied
        """
        # Load base image
        if isinstance(base_image, (str, Path)):
            img = Image.open(base_image)
        else:
            img = base_image.copy()

        # Convert to RGBA for transparency support
        if img.mode != 'RGBA':
            img = img.convert('RGBA')

        width, height = img.size

        # ── v2 badge format ──────────────────────────────────
        if 'badges' in layout_data:
            badge_list = layout_data.get('badges', [])
            if not badge_list:
                self.logger.warning("Layout has no badges, returning base image")
                return img
            self.logger.info(f"Rendering {len(badge_list)} badges (v2 format)")
            for idx, badge in enumerate(badge_list):
                try:
                    if not self._evaluate_condition_badge(badge, media_info):
                        self.logger.debug(f"Badge {idx} condition not met, skipping")
                        continue
                    btype = badge.get('type')
                    if btype == 'background_panel':
                        img = self._render_background_panel(img, badge)
                    elif btype == 'smart_badge':
                        img = self._render_smart_badge_element(img, badge, media_info, width, height)
                    elif btype == 'designed_badge':
                        img = self._render_designed_badge(img, badge, media_info)
                    elif btype == 'title_logo':
                        img = self._render_title_logo(img, badge, media_info, width, height)
                    elif btype == 'file_match':
                        img = self._render_file_match_badge(img, badge, media_info, width, height)
                    else:
                        img = self.render_badge(img, badge, media_info, width, height)
                except Exception as e:
                    self.logger.error(f"Failed to render badge {idx}: {e}", exc_info=True)
            self.logger.info("Badge rendering complete")
            return img

        # ── legacy elements format ───────────────────────────
        elements = layout_data.get('elements', [])
        if not elements:
            self.logger.warning("Template has no elements, returning base image")
            return img

        self.logger.info(f"Rendering template with {len(elements)} elements (legacy format)")

        for idx, element in enumerate(elements):
            try:
                if not self._evaluate_condition(element, media_info):
                    self.logger.debug(f"Element {idx} condition not met, skipping")
                    continue

                element_type = element.get('type')
                if element_type == 'raster':
                    img = self._render_raster_element(img, element, width, height)
                elif element_type == 'text':
                    img = self._render_text_element(img, element, media_info, width, height)
                elif element_type == 'smart_badge':
                    img = self._render_smart_badge_element(img, element, media_info, width, height)
                elif element_type == 'designed_badge':
                    img = self._render_designed_badge(img, element, media_info)
                else:
                    self.logger.warning(f"Unknown element type: {element_type}")

            except Exception as e:
                self.logger.error(f"Failed to render element {idx}: {e}", exc_info=True)

        self.logger.info("Template rendering complete")
        return img

    def render_badge(self, base_img: Image.Image, badge: Dict[str, Any],
                     media_info: Dict[str, Any],
                     poster_width: int, poster_height: int) -> Image.Image:
        """
        Render a v2 badge (background + icon + text) onto the poster.

        Args:
            base_img: Base RGBA image
            badge: Badge dict with background, icon, text sub-objects
            media_info: Media information for variable substitution
            poster_width: Poster width in pixels
            poster_height: Poster height in pixels

        Returns:
            Updated image
        """
        s  = max(0.1, poster_width  / 600.0)
        sy = max(0.1, poster_height / 900.0)
        x = int(badge.get('x', 20) * s)
        y = int(badge.get('y', 20) * sy)

        bg   = badge.get('background', {})
        icon = badge.get('icon', {})
        text = badge.get('text', {})

        bg_enabled   = bg.get('enabled', True)
        bg_pad   = max(0, int(bg.get('padding', 8)))
        bg_pad_s = max(0, int(bg_pad * s))
        icon_enabled = icon.get('enabled', False)
        text_enabled = text.get('enabled', True)

        # Resolve dimensions — 0 = auto-size from content
        raw_w = int(bg.get('width',  0))
        raw_h = int(bg.get('height', 0))

        is_vert_stack = bool(text.get('stackEnabled', False)) and text_enabled

        # Font size and icon size are computed independently — they must not constrain each other
        font_size_auto = max(8, int(text.get('size', 24) * s)) if text_enabled else max(8, int(24 * s))
        _iw_cfg = int(icon.get('width', 0)); _ih_cfg = int(icon.get('height', 0))
        # Explicit icon height (scaled); 0 means "auto-fit to badge height" later
        explicit_icon_h_s = max(1, int(_ih_cfg * s)) if _ih_cfg else 0

        if bg_enabled and (raw_w == 0 or raw_h == 0):
            if raw_h == 0:
                if is_vert_stack:
                    # icon on top + text on bottom — use explicit icon height if set
                    est_icon_h = explicit_icon_h_s if (icon_enabled and explicit_icon_h_s) else \
                                 (max(int((_iw_cfg or 32) * s), 1) if icon_enabled else 0)
                    _stack_gap = max(0, int(text.get('stackGap', 4)))
                    gap_s = max(0, int(_stack_gap * s)) if (icon_enabled and text_enabled) else 0
                    total_h = max(int(40 * s), est_icon_h + gap_s + font_size_auto + 2 * bg_pad_s)
                else:
                    # Horizontal: badge height = max(font, explicit_icon_h) + padding
                    content_h = font_size_auto
                    if icon_enabled and explicit_icon_h_s:
                        content_h = max(content_h, explicit_icon_h_s)
                    total_h = max(int(24 * s), content_h + 2 * bg_pad_s)
            else:
                total_h = int(raw_h * s)
            if raw_w == 0:
                pad_2 = max(4, 2 * bg_pad_s)
                _dummy = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
                if is_vert_stack:
                    # Width = widest of icon or text (no side-by-side)
                    icon_w = max(int((_iw_cfg or _ih_cfg or 36) * s), 1) if icon_enabled else 0
                    text_w = 0
                    if text_enabled:
                        rv = self._interpolate_badge_text(text.get('value', ''), media_info,
                                                          badge.get('ratingFormat', 'auto'),
                                                          bool(badge.get('percentUnit', False)))
                        rendered_auto = rv or text.get('fallback', '')
                        if rendered_auto:
                            _t_bold = text.get('fontWeight', 'normal') == 'bold'
                            af = self._load_google_font(text.get('font', 'DejaVuSans-Bold'), font_size_auto, bold=_t_bold)
                            bb = _dummy.textbbox((0, 0), rendered_auto, font=af)
                            text_w = bb[2] - bb[0]
                    total_w = max(int(40 * s), max(icon_w, text_w) + pad_2)
                else:
                    rendered_auto = ''
                    if text_enabled:
                        rv = self._interpolate_badge_text(text.get('value', ''), media_info,
                                                          badge.get('ratingFormat', 'auto'),
                                                          bool(badge.get('percentUnit', False)))
                        rendered_auto = rv or text.get('fallback', '')
                    icon_extra = (max(int((_iw_cfg or _ih_cfg or 36) * s), 1) + 6) if icon_enabled else 0
                    text_w = 0
                    if rendered_auto:
                        _t_bold = text.get('fontWeight', 'normal') == 'bold'
                        af = self._load_google_font(text.get('font', 'DejaVuSans-Bold'), font_size_auto, bold=_t_bold)
                        bb = _dummy.textbbox((0, 0), rendered_auto, font=af)
                        text_w = bb[2] - bb[0]
                    total_w = max(40, text_w + icon_extra + pad_2)
            else:
                total_w = int(raw_w * s)
        else:
            if bg_enabled:
                total_w = int(raw_w * s)
                total_h = int(raw_h * s)
            else:
                # No background — size from content so text/icon aren't clamped
                content_h = font_size_auto
                if icon_enabled and explicit_icon_h_s:
                    content_h = max(content_h, explicit_icon_h_s)
                total_h = content_h + 2 * bg_pad_s
                total_w = int(raw_w * s) if raw_w else int(100 * s)

        x = max(0, min(x, max(0, poster_width  - total_w)))
        y = max(0, min(y, max(0, poster_height - total_h)))

        # ── Background ────────────────────────────────────────
        if bg_enabled:
            bg_color = self._parse_color(bg.get('color', '#000000CC'))
            radius = max(1, int(bg.get('borderRadius', 8) * s))

            overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
            ov_draw = ImageDraw.Draw(overlay)
            ov_draw.rounded_rectangle(
                [(x, y), (x + total_w, y + total_h)],
                radius=max(0, radius),
                fill=bg_color
            )
            # ── Border stroke ─────────────────────────────────
            bw_raw = int(bg.get('borderWidth', 0))
            if bw_raw > 0:
                bw = max(1, int(bw_raw * s))
                _bc_raw = bg.get('borderColor', '#ffffff')
                bc = self._parse_color(_bc_raw if len(_bc_raw) == 9 else _bc_raw[:7] + 'FF')
                half = bw // 2
                ov_draw.rounded_rectangle(
                    [(x + half, y + half), (x + total_w - 1 - half, y + total_h - 1 - half)],
                    radius=max(0, radius - half),
                    outline=bc,
                    width=bw
                )
            base_img = Image.alpha_composite(base_img, overlay)

        # ── Helper: load and resolve icon dimensions ──────────
        def _resolve_icon(hint_total_h):
            icon_path = icon.get('path', '')
            raw_i_w = int(icon.get('width',  0))
            raw_i_h = int(icon.get('height', 0))
            if (raw_i_w == 0 or raw_i_h == 0) and icon_path:
                path_strip = icon_path.lstrip('/')
                if path_strip.startswith('logos/'):
                    logo_rel  = path_strip[len('logos/'):]
                    icon_file = _USER_LOGO_DIR / logo_rel
                    if not icon_file.exists():
                        icon_file = _SYSTEM_LOGO_DIR / logo_rel
                else:
                    icon_file = self.asset_dir / path_strip
                try:
                    with Image.open(icon_file) as _probe:
                        nat_w, nat_h = _probe.size
                    aspect = nat_w / nat_h if nat_h else 1.0
                    if raw_i_w == 0 and raw_i_h == 0:
                        raw_i_h = max(1, int(hint_total_h / s) - 2 * bg_pad)
                        raw_i_w = round(raw_i_h * aspect)
                    elif raw_i_w == 0:
                        raw_i_w = round(raw_i_h * aspect)
                    else:
                        raw_i_h = round(raw_i_w / aspect)
                except Exception:
                    raw_i_w = raw_i_w or 36
                    raw_i_h = raw_i_h or 24
            i_w = max(1, int(raw_i_w * s)) if raw_i_w else max(1, int(36 * s))
            i_h = max(1, int(raw_i_h * s)) if raw_i_h else max(1, int(24 * s))
            return icon_path, i_w, i_h

        def _paste_icon(icon_path, i_x, i_y, i_w, i_h):
            if not icon_path:
                return
            path_strip = icon_path.lstrip('/')
            if path_strip.startswith('logos/'):
                logo_rel = path_strip[len('logos/'):]
                asset_path = _USER_LOGO_DIR / logo_rel
                if not asset_path.exists():
                    asset_path = _SYSTEM_LOGO_DIR / logo_rel
            else:
                asset_path = self.asset_dir / path_strip
            if asset_path.exists():
                try:
                    icon_img = Image.open(asset_path)
                    if icon_img.mode != 'RGBA':
                        icon_img = icon_img.convert('RGBA')
                    icon_img = icon_img.resize((i_w, i_h), Image.Resampling.LANCZOS)
                    base_img.paste(icon_img, (i_x, i_y), icon_img)
                except Exception as e:
                    self.logger.error(f"Failed to load badge icon '{icon_path}': {e}")
            else:
                self.logger.debug(f"Badge icon not found: {icon_path}")

        if is_vert_stack:
            # ── Vertical stack: icon on top, text on bottom ───────────────
            icon_path, i_w, i_h = _resolve_icon(total_h // 2) if icon_enabled else ('', 0, 0)
            font_name   = text.get('font', 'DejaVuSans-Bold')
            font_size_v = max(8, int(text.get('size', 24) * s))
            text_color  = self._parse_color(text.get('color', '#FFFFFF'))
            ratingfmt   = badge.get('ratingFormat', 'auto')
            pct_unit    = bool(badge.get('percentUnit', False))
            rendered    = self._interpolate_badge_text(text.get('value', ''), media_info, ratingfmt, pct_unit) \
                          or text.get('fallback', '') if text_enabled else ''

            _stack_gap2 = max(0, int(text.get('stackGap', 4)))
            gap_s = max(0, int(_stack_gap2 * s)) if (icon_enabled and rendered) else 0
            total_content_h = (i_h if icon_enabled else 0) + gap_s + (font_size_v if rendered else 0)
            cur_y = y + (total_h - total_content_h) // 2

            if icon_enabled:
                i_x = x + max(0, (total_w - i_w) // 2)
                _paste_icon(icon_path, i_x, cur_y, i_w, i_h)
                cur_y += i_h + gap_s

            if rendered:
                _v_bold = text.get('fontWeight', 'normal') == 'bold'
                font = self._load_google_font(font_name, font_size_v, bold=_v_bold)
                draw = ImageDraw.Draw(base_img)
                bb = draw.textbbox((0, 0), rendered, font=font)
                t_w = bb[2] - bb[0]
                t_y = cur_y - bb[1]
                t_x = x + (total_w - t_w) // 2  # always centred in vertical stack
                draw.text((t_x, t_y), rendered, font=font, fill=text_color)

        else:
            # ── Horizontal layout ─────────────────────────────────────────
            text_x = x + bg_pad_s
            text_x_right = x + total_w - bg_pad_s
            if icon_enabled:
                icon_path, i_w, i_h = _resolve_icon(total_h)
                i_side = icon.get('side', 'left')
                i_y = y + (total_h - i_h) // 2
                if i_side == 'none':
                    # Independent — icon centered in badge, no text anchor adjustment
                    i_x = x + (total_w - i_w) // 2
                elif i_side == 'right':
                    i_x = x + total_w - i_w - bg_pad_s
                    text_x_right = i_x - 6
                else:
                    i_x = x + bg_pad_s
                    text_x = i_x + i_w + 6
                _paste_icon(icon_path, i_x, i_y, i_w, i_h)

            if text_enabled:
                font_name  = text.get('font', 'DejaVuSans-Bold')
                font_size1 = max(8, int(text.get('size', 24) * s))  # not clamped by total_h
                text_color1 = self._parse_color(text.get('color', '#FFFFFF'))
                text_align  = text.get('align', 'left')
                ratingfmt   = badge.get('ratingFormat', 'auto')
                pct_unit    = bool(badge.get('percentUnit', False))
                rendered1   = self._interpolate_badge_text(text.get('value', ''), media_info, ratingfmt, pct_unit) \
                              or text.get('fallback', '')
                if rendered1:
                    _h_bold = text.get('fontWeight', 'normal') == 'bold'
                    font = self._load_google_font(font_name, font_size1, bold=_h_bold)
                    draw = ImageDraw.Draw(base_img)
                    bbox = draw.textbbox((0, 0), rendered1, font=font)
                    t_w = bbox[2] - bbox[0]
                    t_h = bbox[3] - bbox[1]
                    x_off = int(text.get('xOffset', 0) or 0)
                    y_off = int(text.get('yOffset', 0) or 0)
                    text_y = y + (total_h - t_h) // 2 - bbox[1] + y_off
                    # 'none' align or any align: anchor from badge center + xOffset
                    if text_align in ('none', 'center'):
                        final_x = x + (total_w - t_w) // 2 + x_off
                    elif text_align == 'right':
                        final_x = text_x_right - t_w + x_off
                    else:
                        final_x = text_x + x_off
                    draw.text((final_x, text_y), rendered1, font=font, fill=text_color1)

        return base_img

    def _render_title_logo(self, base_img: Image.Image, badge: Dict[str, Any],
                           media_info: Dict[str, Any],
                           poster_width: int, poster_height: int) -> Image.Image:
        """Render the title clearlogo (TMDB PNG) or a text fallback onto the poster.

        Badge JSON shape:
            { "type": "title_logo", "x": 20, "y": 750,
              "width": 300, "height": 80, "opacity": 1.0 }

        If no clearlogo is available, falls back to rendering the title text
        at the same position using the font/color settings.
        """
        import requests as _req
        from io import BytesIO as _BytesIO

        # Skip title_logo entirely when textless poster setting is on but no
        # textless poster was found — the fallback English poster already has
        # the title baked in, so compositing a logo would duplicate it.
        from utilities.settings import load_config as _lc_tl
        _textless_setting = _lc_tl().get('Overlay Settings', {}).get('textless_posters', False)
        if _textless_setting and not media_info.get('textless_poster_used', False):
            self.logger.info(
                f"Skipping title_logo for '{media_info.get('title', '?')}' — "
                f"textless poster not available, poster has baked-in title")
            return base_img

        opacity = float(badge.get('opacity', 1.0))
        mode    = badge.get('positionMode', 'anchor')

        if mode == 'anchor':
            # Anchor mode: position by percentage of poster dimensions
            anchor_x_side  = badge.get('anchorX', 'center')   # left / center / right
            anchor_y_pct   = float(badge.get('anchorY',      85))   # % from top
            max_w_pct      = float(badge.get('maxWidthPct',  60))   # % of poster width
            max_h_pct      = float(badge.get('maxHeightPct', 12))   # % of poster height
            max_logo_w = max(1, int(poster_width  * max_w_pct  / 100))
            max_logo_h = max(1, int(poster_height * max_h_pct  / 100))
            anchor_cy  = int(poster_height * anchor_y_pct / 100)
        else:
            # Pixel mode: legacy x/y/width/height in canvas-space (600×900 base)
            s  = max(0.1, poster_width  / 600.0)
            sy = max(0.1, poster_height / 900.0)
            px = int(badge.get('x', 20)  * s)
            py = int(badge.get('y', 750) * sy)
            raw_w = int(badge.get('width',  300))
            raw_h = int(badge.get('height', 80))
            max_logo_w = int(raw_w * s) if raw_w else int(300 * s)
            max_logo_h = int(raw_h * s) if raw_h else int(80  * sy)

        # ── Poster scrim / blur (drawn before logo) ───────────────────────
        if badge.get('scrimEnabled', False):
            try:
                from PIL import ImageDraw as _ID2, ImageFilter as _IFF
                scrim_mode  = badge.get('scrimMode', 'gradient')
                scrim_dir   = badge.get('scrimDirection', 'bottom')
                start_pct   = float(badge.get('scrimStart',   55)) / 100
                end_pct     = float(badge.get('scrimEnd',    100)) / 100
                scrim_rgb   = self._parse_color(badge.get('scrimColor', '#000000'))[:3]
                max_opacity = float(badge.get('scrimOpacity', 0.85))

                if scrim_mode == 'blur':
                    # Blur the region and feather the transition
                    blur_radius = max(1, int(badge.get('scrimBlurRadius', 20)))
                    w, h = base_img.size

                    # Determine region extents in pixels
                    if scrim_dir == 'bottom':
                        region_start_y = int(h * (1 - end_pct))
                        region_end_y   = h
                        feather_start  = int(h * (1 - end_pct))
                        feather_end    = int(h * (1 - start_pct))
                    elif scrim_dir == 'top':
                        region_start_y = 0
                        region_end_y   = int(h * end_pct)
                        feather_start  = region_end_y
                        feather_end    = int(h * start_pct)
                    elif scrim_dir == 'right':
                        region_start_x = int(w * (1 - end_pct))
                        region_end_x   = w
                        feather_start  = region_start_x
                        feather_end    = int(w * (1 - start_pct))
                    else:  # left
                        region_start_x = 0
                        region_end_x   = int(w * end_pct)
                        feather_start  = region_end_x
                        feather_end    = int(w * start_pct)

                    # Blur entire poster then mask back in with gradient alpha
                    blurred_full = base_img.filter(_IFF.GaussianBlur(blur_radius))
                    feather_mask = Image.new('L', base_img.size, 0)
                    feather_draw = _ID2.Draw(feather_mask)
                    feather_len  = max(1, abs(feather_end - feather_start))

                    for i in range(feather_len):
                        alpha = int(255 * i / feather_len)
                        pos   = feather_start + i
                        if scrim_dir == 'bottom':
                            feather_draw.line([(0, pos), (w, pos)], fill=alpha)
                        elif scrim_dir == 'top':
                            feather_draw.line([(0, feather_start + feather_len - 1 - i),
                                               (w, feather_start + feather_len - 1 - i)], fill=alpha)
                        elif scrim_dir == 'right':
                            feather_draw.line([(pos, 0), (pos, h)], fill=alpha)
                        else:
                            feather_draw.line([(feather_start + feather_len - 1 - i, 0),
                                               (feather_start + feather_len - 1 - i, h)], fill=alpha)

                    # Fill fully-blurred zone
                    if scrim_dir == 'bottom' and feather_end < h:
                        feather_draw.rectangle([(0, feather_end), (w, h)], fill=255)
                    elif scrim_dir == 'top' and feather_end > 0:
                        feather_draw.rectangle([(0, 0), (w, feather_end)], fill=255)
                    elif scrim_dir == 'right' and feather_end < w:
                        feather_draw.rectangle([(feather_end, 0), (w, h)], fill=255)
                    elif scrim_dir == 'left' and feather_end > 0:
                        feather_draw.rectangle([(0, 0), (feather_end, h)], fill=255)

                    base_img = Image.composite(blurred_full, base_img, feather_mask)

                else:
                    # Gradient mode
                    scrim_layer = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
                    w, h = base_img.size

                    band_len = h if scrim_dir in ('bottom', 'top') else w
                    start_px = int(band_len * (1 - end_pct))
                    end_px   = int(band_len * (1 - start_pct))
                    grad_len = max(1, end_px - start_px)

                    scrim_draw = _ID2.Draw(scrim_layer)
                    for i in range(grad_len):
                        t     = i / grad_len
                        alpha = int(255 * max_opacity * t)
                        pos   = start_px + i
                        if scrim_dir == 'bottom':
                            scrim_draw.line([(0, pos), (w, pos)], fill=scrim_rgb + (alpha,))
                        elif scrim_dir == 'top':
                            scrim_draw.line([(0, band_len - 1 - pos), (w, band_len - 1 - pos)], fill=scrim_rgb + (alpha,))
                        elif scrim_dir == 'right':
                            scrim_draw.line([(pos, 0), (pos, h)], fill=scrim_rgb + (alpha,))
                        else:
                            scrim_draw.line([(band_len - 1 - pos, 0), (band_len - 1 - pos, h)], fill=scrim_rgb + (alpha,))

                    full_alpha = int(255 * max_opacity)
                    if scrim_dir == 'bottom' and end_px < h:
                        scrim_draw.rectangle([(0, end_px), (w, h)], fill=scrim_rgb + (full_alpha,))
                    elif scrim_dir == 'top' and end_px < h:
                        scrim_draw.rectangle([(0, 0), (w, h - end_px)], fill=scrim_rgb + (full_alpha,))
                    elif scrim_dir == 'right' and end_px < w:
                        scrim_draw.rectangle([(end_px, 0), (w, h)], fill=scrim_rgb + (full_alpha,))
                    elif end_px < w:
                        scrim_draw.rectangle([(0, 0), (w - end_px, h)], fill=scrim_rgb + (full_alpha,))

                    base_img = Image.alpha_composite(base_img, scrim_layer)

            except Exception as _se:
                self.logger.warning(f"Scrim render failed: {_se}")

        clearlogo_url = media_info.get('clearlogo_url')
        if clearlogo_url:
            try:
                resp = _req.get(clearlogo_url, timeout=10)
                resp.raise_for_status()
                logo_img = Image.open(_BytesIO(resp.content)).convert('RGBA')

                lw, lh = logo_img.size
                if lw > 0 and lh > 0:
                    scale   = min(max_logo_w / lw, max_logo_h / lh)
                    new_w   = max(1, int(lw * scale))
                    new_h   = max(1, int(lh * scale))
                    logo_img = logo_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                # Apply opacity
                if opacity < 1.0:
                    r, g, b, a = logo_img.split()
                    a = a.point(lambda p: int(p * opacity))
                    logo_img = Image.merge('RGBA', (r, g, b, a))

                # Resolve paste position
                if mode == 'anchor':
                    # Horizontal anchor
                    if anchor_x_side == 'left':
                        paste_x = 0
                    elif anchor_x_side == 'right':
                        paste_x = poster_width - logo_img.width
                    else:  # center
                        paste_x = (poster_width - logo_img.width) // 2
                    # Vertical: centred on anchor_cy
                    paste_y = anchor_cy - logo_img.height // 2
                else:
                    paste_x = px
                    paste_y = py

                paste_x = max(0, min(paste_x, poster_width  - logo_img.width))
                paste_y = max(0, min(paste_y, poster_height - logo_img.height))

                lw, lh = logo_img.width, logo_img.height

                # ── Background pill ───────────────────────────────────────
                if badge.get('pillEnabled', False):
                    pill_pad     = max(0, int(badge.get('pillPadding', 12)))
                    pill_radius  = max(0, int(badge.get('pillRadius',  10)))
                    pill_opacity = float(badge.get('pillOpacity', 0.8))
                    pill_rgb     = self._parse_color(badge.get('pillColor', '#000000'))[:3]
                    pill_rgba    = pill_rgb + (int(255 * pill_opacity),)
                    pill_layer   = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
                    pill_draw    = ImageDraw.Draw(pill_layer)
                    rx = paste_x - pill_pad
                    ry = paste_y - pill_pad
                    rw = lw + pill_pad * 2
                    rh = lh + pill_pad * 2
                    pill_draw.rounded_rectangle(
                        [(max(0, rx), max(0, ry)),
                         (min(poster_width, rx + rw), min(poster_height, ry + rh))],
                        radius=pill_radius, fill=pill_rgba
                    )
                    base_img = Image.alpha_composite(base_img, pill_layer)

                # ── Drop shadow ───────────────────────────────────────────
                if badge.get('shadowEnabled', False):
                    from PIL import ImageFilter as _IF
                    sh_blur    = max(1, int(badge.get('shadowBlur',    8)))
                    sh_opacity = float(badge.get('shadowOpacity', 0.6))
                    sh_ox      = int(badge.get('shadowOffsetX', 0))
                    sh_oy      = int(badge.get('shadowOffsetY', 3))
                    sh_hex     = badge.get('shadowColor', '#000000')
                    sh_rgb     = self._parse_color(sh_hex)[:3]

                    # Extract logo alpha channel — shadow follows exact logo shape
                    _, _, _, logo_a = logo_img.split()

                    # Build a same-size shadow image: shadow colour + logo-shaped alpha * opacity
                    # Use a solid colour layer masked by the scaled logo alpha — no pixel loop needed
                    shadow_colour = Image.new('RGBA', logo_img.size,
                                              (sh_rgb[0], sh_rgb[1], sh_rgb[2], 255))
                    scaled_alpha  = logo_a.point(lambda p: int(p * sh_opacity))
                    shadow_logo   = Image.new('RGBA', logo_img.size, (0, 0, 0, 0))
                    shadow_logo.paste(shadow_colour, mask=scaled_alpha)

                    # Place shadow on a full-poster canvas at offset position, then blur
                    shadow_canvas = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
                    sx = max(0, min(paste_x + sh_ox, poster_width  - logo_img.width))
                    sy = max(0, min(paste_y + sh_oy, poster_height - logo_img.height))
                    shadow_canvas.paste(shadow_logo, (sx, sy), shadow_logo)
                    shadow_blurred = shadow_canvas.filter(_IF.GaussianBlur(sh_blur))
                    base_img = Image.alpha_composite(base_img, shadow_blurred)

                base_img.paste(logo_img, (paste_x, paste_y), logo_img)
                self.logger.debug(f"Title logo rendered at ({paste_x},{paste_y}) "
                                  f"{logo_img.width}×{logo_img.height} [{mode}]")
                return base_img
            except Exception as _e:
                self.logger.warning(f"Clearlogo render failed ({clearlogo_url}): {_e} — falling back to text")

        # ── Fallback: render title text ───────────────────────────────────
        title = media_info.get('title') or ''
        if not title:
            return base_img

        font_cfg     = badge.get('font', 'DejaVuSans-Bold')
        raw_fs       = badge.get('fontSize', 'auto')
        color        = self._parse_color(badge.get('color', '#FFFFFFDD'))
        bold         = badge.get('fontWeight', 'bold') == 'bold'
        border_width = max(0, int(badge.get('borderWidth', 0)))
        border_color = self._parse_color(badge.get('borderColor', '#000000'))

        draw = ImageDraw.Draw(base_img)

        # Resolve font size — 'auto' fits the text into the container
        if raw_fs == 'auto' or raw_fs is None:
            max_text_w = max(10, max_logo_w - 16)
            max_text_h = max(8,  max_logo_h - 8)
            font_size  = max_text_h
            for _try in range(max_text_h, 7, -1):
                _f  = self._load_google_font(font_cfg, _try, bold=bold)
                _bb = draw.textbbox((0, 0), title, font=_f)
                if (_bb[2] - _bb[0]) <= max_text_w and (_bb[3] - _bb[1]) <= max_text_h:
                    font_size = _try
                    break
        else:
            _s = max(0.1, poster_width / 600.0)
            font_size = max(8, int(float(raw_fs) * _s))

        font = self._load_google_font(font_cfg, font_size, bold=bold)
        bb   = draw.textbbox((0, 0), title, font=font)
        tw   = bb[2] - bb[0]
        th   = bb[3] - bb[1]

        # Resolve text position matching the anchor/pixel mode
        if mode == 'anchor':
            if anchor_x_side == 'left':
                tx = 8
            elif anchor_x_side == 'right':
                tx = poster_width - tw - 8
            else:
                tx = (poster_width - tw) // 2
            ty = anchor_cy - th // 2 - bb[1]
        else:
            tx = px + max(0, (max_logo_w - tw) // 2)
            ty = py  + max(0, (max_logo_h - th) // 2) - bb[1]

        tx = max(0, min(tx, poster_width  - tw))
        ty = max(0, min(ty, poster_height - th))

        # ── Background pill behind text ───────────────────────────────
        if badge.get('pillEnabled', False):
            pill_pad     = max(0, int(badge.get('pillPadding', 12)))
            pill_radius  = max(0, int(badge.get('pillRadius',  10)))
            pill_opacity = float(badge.get('pillOpacity', 0.8))
            pill_rgb     = self._parse_color(badge.get('pillColor', '#000000'))[:3]
            pill_color   = pill_rgb + (int(255 * pill_opacity),)
            pill_layer   = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
            pill_draw_t  = ImageDraw.Draw(pill_layer)
            pill_draw_t.rounded_rectangle(
                [(max(0, tx - bb[0] - pill_pad), max(0, ty + bb[1] - pill_pad)),
                 (min(poster_width,  tx - bb[0] + tw + pill_pad),
                  min(poster_height, ty + bb[1] + th + pill_pad))],
                radius=pill_radius, fill=pill_color
            )
            base_img = Image.alpha_composite(base_img, pill_layer)
            draw = ImageDraw.Draw(base_img)

        # ── Drop shadow behind text ───────────────────────────────────
        if badge.get('shadowEnabled', False):
            from PIL import ImageFilter as _IF
            sh_opacity = float(badge.get('shadowOpacity', 0.6))
            sh_color   = self._parse_color(badge.get('shadowColor', '#000000'))
            sh_ox      = int(badge.get('shadowOffsetX', 0))
            sh_oy      = int(badge.get('shadowOffsetY', 3))
            sh_blur    = max(1, int(badge.get('shadowBlur', 8)))
            sh_fill    = sh_color[:3] + (int(255 * sh_opacity),)
            sh_layer   = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
            sh_draw    = ImageDraw.Draw(sh_layer)
            sh_draw.text((tx + sh_ox, ty + sh_oy), title, font=font, fill=sh_fill)
            sh_layer   = sh_layer.filter(_IF.GaussianBlur(sh_blur))
            base_img   = Image.alpha_composite(base_img, sh_layer)
            draw = ImageDraw.Draw(base_img)

        # Draw border/stroke by drawing text offset in border colour
        if border_width > 0:
            for dx in range(-border_width, border_width + 1):
                for dy in range(-border_width, border_width + 1):
                    if dx == 0 and dy == 0:
                        continue
                    draw.text((tx + dx, ty + dy), title, font=font, fill=border_color)

        draw.text((tx, ty), title, font=font, fill=color)
        self.logger.debug(f"Title text fallback rendered at ({tx},{ty}): '{title}'")
        return base_img

    def _evaluate_condition_badge(self, badge: Dict[str, Any],
                                   media_info: Dict[str, Any]) -> bool:
        """Evaluate a badge's show condition (supports camelCase field names)."""
        condition = badge.get('condition')
        if not condition:
            return True
        try:
            ctx = {
                # camelCase (badge format)
                'imdbRating':    media_info.get('imdb_rating'),
                'tmdbRating':    media_info.get('tmdb_rating'),
                'traktRating':   media_info.get('trakt_rating'),
                'rtCriticsScore': media_info.get('rt_critics_score'),
                'rtUserScore':   media_info.get('rt_user_score'),
                'resolution':    media_info.get('resolution'),
                'hdr':           media_info.get('hdr', False),
                'hdrFormat':     media_info.get('hdr_format'),
                'hdrLine1':      media_info.get('hdr_line1'),
                'hdrLine2':      media_info.get('hdr_line2'),
                'audioCodec':    media_info.get('audio_codec'),
                'videoCodec':    media_info.get('video_codec'),
                'format':        media_info.get('format'),
                'network':       media_info.get('network'),
                'studio':        media_info.get('studio'),
                'contentRating': media_info.get('content_rating'),
                'status':        media_info.get('status'),
                'year':          media_info.get('year'),
                'versionCount':  media_info.get('version_count', 1),
                # snake_case aliases
                'audio_codec':   media_info.get('audio_codec'),
                'video_codec':   media_info.get('video_codec'),
                'hdr_format':    media_info.get('hdr_format'),
                'hdr_line1':     media_info.get('hdr_line1'),
                'hdr_line2':     media_info.get('hdr_line2'),
                'true': True, 'false': False, 'True': True, 'False': False,
            }
            cond = condition
            cond = cond.replace(' AND ', ' and ').replace(' OR ', ' or ')
            cond = cond.replace(' IS NOT NULL', ' is not None')
            cond = cond.replace(' IS NULL', ' is None')
            return bool(eval(cond, {"__builtins__": {}}, ctx))
        except Exception as e:
            self.logger.error(f"Badge condition eval error '{condition}': {e}")
            return False

    def _render_file_match_badge(self, base_img, badge, media_info, poster_width, poster_height):
        """
        Render a file_match badge — only displays when the item's filename contains
        the user-specified search term (case-insensitive substring match).

        If matched:
          - useIcon=True  → render with icon only (text disabled)
          - useIcon=False → render with displayText (or searchTerm if displayText is empty)
        If not matched: returns base_img unchanged.
        """
        fm = badge.get('filenameMatch') or {}
        search_term = (fm.get('searchTerm') or '').strip()
        if not search_term:
            return base_img  # no search term configured — skip

        # Check filename: try filled_by_file, then location_on_disk, then location_basename
        filename = (
            media_info.get('filled_by_file') or
            media_info.get('location_on_disk') or
            media_info.get('location_basename') or
            ''
        )
        if search_term.lower() not in filename.lower():
            self.logger.debug(f"File match badge: '{search_term}' not found in '{filename}' — skipping")
            return base_img

        self.logger.debug(f"File match badge: '{search_term}' matched in '{filename}'")

        use_icon = bool(fm.get('useIcon', False))
        display_text = (fm.get('displayText') or '').strip() or search_term

        # Build a modified badge copy for rendering via the standard render_badge path
        import copy
        render_badge = copy.deepcopy(badge)

        if use_icon:
            # Show icon only — disable text
            render_badge['text']['enabled'] = False
            render_badge['icon']['enabled'] = True
        else:
            # Show text — inject the resolved display text as a literal value
            render_badge['text']['enabled'] = True
            render_badge['text']['value'] = display_text
            render_badge['text']['fallback'] = ''

        return self.render_badge(base_img, render_badge, media_info, poster_width, poster_height)

    # Rating variable names that can be reformatted
    _RATING_VARS = frozenset({'imdbRating', 'tmdbRating', 'traktRating', 'rtCriticsScore', 'rtUserScore'})

    @staticmethod
    def _format_rating(raw: str, fmt: str) -> str:
        """
        Format a raw rating string according to the display format:
          'decimal'    → e.g. 8.5  (divide by 10 if > 10)
          'percentage' → e.g. 85   (multiply by 10 if ≤ 10, round to int)
          'auto'       → pass through unchanged
        """
        if not fmt or fmt == 'auto':
            return raw
        try:
            n = float(raw)
        except (ValueError, TypeError):
            return raw
        if fmt == 'decimal':
            v = n / 10 if n > 10 else n
            if v >= 10:
                return '10'
            return f'{v:.1f}'
        if fmt == 'percentage':
            v = round(n * 10) if n <= 10 else round(n)
            return str(v)
        return raw

    def _interpolate_badge_text(self, template: str, media_info: Dict[str, Any],
                                rating_format: str = 'auto',
                                percent_unit: bool = False) -> str:
        """Replace {{variable}} placeholders with values from media_info."""
        import re
        # Strip any legacy bare % appended directly to RT variable tokens
        template = re.sub(r'(\{\{(?:rtCriticsScore|rtUserScore)\}\})%', r'\1', template)

        FIELD_MAP = {
            'imdbRating':    'imdb_rating',
            'tmdbRating':    'tmdb_rating',
            'traktRating':   'trakt_rating',
            'rtCriticsScore': 'rt_critics_score',
            'rtUserScore':   'rt_user_score',
            'resolution':    'resolution',
            'hdr':           'hdr_line1',   # abbreviated primary HDR label (DV / HDR10+)
            'hdrLine1':      'hdr_line1',   # explicit primary HDR line
            'hdrLine2':      'hdr_line2',   # secondary HDR line (HDR10+ when DV+HDR both present)
            'audioCodec':    'audio_codec',
            'audioChannels': 'audio_channels',
            'videoCodec':    'video_codec',
            'format':        'format',
            'network':       'network',
            'studio':        'studio',
            'contentRating': 'content_rating',
            'status':        'status',
            'title':         'title',
            'year':          'year',
            'definition':    'definition',
            'versionCount':  'version_count',
        }

        _CR_NORMALIZE = {'not rated': 'NR', 'unrated': 'NR', 'nr': 'NR'}

        def replace(m):
            key = m.group(1)
            mapped = FIELD_MAP.get(key, key)
            value = media_info.get(mapped, media_info.get(key))
            if value is None:
                return ''
            text = str(value)
            # For rating values ≥ 10, always strip the trailing .0 regardless of format
            # (str(10.0) produces "10.0" which should always display as "10")
            if key in self._RATING_VARS and isinstance(value, float) and value >= 10 and value == int(value):
                text = str(int(value))
            if key == 'contentRating':
                text = _CR_NORMALIZE.get(text.lower(), text)
            if rating_format and rating_format != 'auto' and key in self._RATING_VARS:
                text = self._format_rating(text, rating_format)
                if percent_unit and rating_format == 'percentage' and key in self._RATING_VARS:
                    text = text + '%'
            return text

        return re.sub(r'\{\{(\w+)\}\}', replace, template)

    def _evaluate_condition(self, element: Dict[str, Any], media_info: Dict[str, Any]) -> bool:
        """
        Evaluate element condition expression.

        Args:
            element: Element dictionary
            media_info: Media information dictionary

        Returns:
            True if condition passes (or no condition), False otherwise
        """
        condition = element.get('condition')
        if not condition:
            return True  # No condition = always render

        try:
            # Create safe evaluation context with media_info
            context = {
                'resolution': media_info.get('resolution'),
                'hdr': media_info.get('hdr', False),
                'dolby_vision': media_info.get('dolby_vision', False),
                'hdr_format': media_info.get('hdr_format'),
                'hdr_line1': media_info.get('hdr_line1'),
                'hdr_line2': media_info.get('hdr_line2'),
                'audio_codec': media_info.get('audio_codec'),
                'audio_channels': media_info.get('audio_channels'),
                'video_codec': media_info.get('video_codec'),
                'container': media_info.get('container'),
                'bitrate': media_info.get('bitrate'),
                'format': media_info.get('format'),
                'imdb_rating': media_info.get('imdb_rating'),
                'tmdb_rating': media_info.get('tmdb_rating'),
                'trakt_rating': media_info.get('trakt_rating'),
                'true': True,
                'false': False,
                'True': True,
                'False': False,
            }

            # Replace string operators with Python equivalents
            condition_py = condition
            condition_py = condition_py.replace(' AND ', ' and ')
            condition_py = condition_py.replace(' OR ', ' or ')
            condition_py = condition_py.replace(' NOT ', ' not ')
            condition_py = condition_py.replace(' IS NULL', ' is None')
            condition_py = condition_py.replace(' IS NOT NULL', ' is not None')

            # Evaluate in restricted context
            result = eval(condition_py, {"__builtins__": {}}, context)
            return bool(result)

        except Exception as e:
            self.logger.error(f"Failed to evaluate condition '{condition}': {e}")
            return False  # Fail safe: don't render if condition error

    def _render_smart_badge_element(self, base_img: Image.Image, element: Dict[str, Any],
                                    media_info: Dict[str, Any],
                                    poster_width: int, poster_height: int) -> Image.Image:
        """
        Render a smart_badge element: auto-selects a badge PNG from the badge library
        based on the media item's metadata and the element's badge_type slug.

        Args:
            base_img: Base RGBA image to render onto
            element: Smart badge element dict (badge_type, x, y, width, height, opacity)
            media_info: Media information dictionary
            poster_width: Poster width in pixels
            poster_height: Poster height in pixels

        Returns:
            Updated image (unchanged if no matching asset found)
        """
        badge_type_slug = element.get('badge_type')
        if not badge_type_slug:
            self.logger.warning("smart_badge element missing badge_type")
            return base_img

        # Build ms_* keyed dict from media_info for BadgeManager
        ms_item = {
            'ms_audio_codec':    media_info.get('audio_codec', ''),
            'ms_audio_channels': media_info.get('audio_channels'),
            'ms_resolution':     media_info.get('resolution', ''),
            'ms_hdr':            1 if media_info.get('hdr') else 0,
            'ms_dolby_vision':   1 if media_info.get('dolby_vision') else 0,
        }

        try:
            from overlays.badge_manager import BadgeManager
            bm = BadgeManager(None)
            asset_path = bm.get_variation_asset_for_media(badge_type_slug, ms_item)
        except Exception as e:
            self.logger.error(f"BadgeManager lookup failed for '{badge_type_slug}': {e}")
            return base_img

        if not asset_path:
            self.logger.debug(f"No badge asset for type='{badge_type_slug}' with media_info={ms_item}")
            return base_img

        asset_file = Path(asset_path)
        if not asset_file.exists():
            self.logger.warning(f"Badge asset file not found: {asset_path}")
            return base_img

        try:
            badge_img = Image.open(asset_file)
            if badge_img.mode != 'RGBA':
                badge_img = badge_img.convert('RGBA')

            _scale   = poster_width  / 600.0
            _scale_y = poster_height / 900.0
            _raw_w = element.get('width') or element.get('_previewW')
            _raw_h = element.get('height') or element.get('_previewH')
            if _raw_h:
                target_h = max(1, int(float(_raw_h) * _scale))
                target_w = (max(1, int(float(_raw_w) * _scale)) if _raw_w
                            else max(1, int(badge_img.width * (target_h / badge_img.height))))
            elif _raw_w:
                target_w = max(1, int(float(_raw_w) * _scale))
                target_h = max(1, int(badge_img.height * (target_w / badge_img.width)))
            else:
                target_h = max(1, int(badge_img.height * _scale))
                target_w = max(1, int(badge_img.width  * _scale))
            if (target_w, target_h) != badge_img.size:
                badge_img = badge_img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            opacity = float(element.get('opacity', 1.0))
            if opacity < 1.0:
                badge_img = self._apply_opacity(badge_img, opacity)

            x = int(element.get('x', 0) * _scale)
            y = int(element.get('y', 0) * _scale_y)
            x = max(0, min(x, max(0, base_img.width  - target_w)))
            y = max(0, min(y, max(0, base_img.height - target_h)))

            # ── Style overlay ──────────────────────────────────────────
            style = element.get('styleOverlay', {})
            style_sp = 0  # outer padding offset for final placement
            if style.get('enabled'):
                W, H = target_w, target_h
                R = max(0, int((style.get('borderRadius', 8)) * _scale))
                style_sp = max(0, int(style.get('padding', 8) * _scale))
                fW, fH = W + 2 * style_sp, H + 2 * style_sp  # outer frame dims

                # Base background on outer frame
                bg_type  = style.get('bgType', 'solid')
                bg_color = self._color_with_opacity(style.get('bgColor',  '#ffffff'),
                                                    float(style.get('bgOpacity', 0.03)))
                bg_color2 = self._color_with_opacity(style.get('bgColor2', '#ffffff'),
                                                     float(style.get('bgOpacity', 0.03)))
                style_img = Image.new('RGBA', (fW, fH), (0, 0, 0, 0))
                if bg_type == 'gradient':
                    grad = self._make_gradient(fW, fH, bg_color, bg_color2,
                                               float(style.get('bgAngle', 135)))
                    mask = self._rounded_mask(fW, fH, R)
                    style_img.paste(grad, (0, 0), mask)
                else:
                    ov = Image.new('RGBA', (fW, fH), (0, 0, 0, 0))
                    ImageDraw.Draw(ov).rounded_rectangle([(0, 0), (fW - 1, fH - 1)],
                                                         radius=R, fill=bg_color)
                    style_img = Image.alpha_composite(style_img, ov)

                # Composite: background then badge PNG at (style_sp, style_sp)
                frame = Image.new('RGBA', (fW, fH), (0, 0, 0, 0))
                frame = Image.alpha_composite(frame, style_img)
                padded = Image.new('RGBA', (fW, fH), (0, 0, 0, 0))
                padded.paste(badge_img, (style_sp, style_sp))
                frame = Image.alpha_composite(frame, padded)

                # Border on outer frame
                bw = max(0, int(style.get('borderWidth', 1)))
                if bw > 0:
                    bc = self._color_with_opacity(style.get('borderColor', '#ffffff'),
                                                  float(style.get('borderOpacity', 0.08)))
                    b_ov = Image.new('RGBA', (fW, fH), (0, 0, 0, 0))
                    half = bw // 2
                    ImageDraw.Draw(b_ov).rounded_rectangle(
                        [(half, half), (fW - 1 - half, fH - 1 - half)],
                        radius=max(0, R - half), outline=bc, width=bw)
                    frame = Image.alpha_composite(frame, b_ov)

                # Top highlight on outer frame
                hl_op = float(style.get('highlightOpacity', 0.09))
                if hl_op > 0:
                    hl_img = Image.new('RGBA', (fW, fH), (0, 0, 0, 0))
                    hl_pix = hl_img.load()
                    hx1 = int(fW * 0.12)
                    hx2 = int(fW * 0.88)
                    span = max(1, hx2 - hx1)
                    for px in range(hx1, min(hx2, fW)):
                        t     = (px - hx1) / span
                        alpha = int(255 * hl_op * (1 - abs(2 * t - 1)))
                        hl_pix[px, 0] = (255, 255, 255, alpha)
                    frame = Image.alpha_composite(frame, hl_img)

                badge_img = frame

            overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
            overlay.paste(badge_img, (x - style_sp, y - style_sp), badge_img)
            base_img = Image.alpha_composite(base_img, overlay)
            self.logger.debug(f"Rendered smart_badge '{badge_type_slug}' at ({x},{y}) size {target_w}x{target_h}")

        except Exception as e:
            self.logger.error(f"Failed to render smart_badge '{badge_type_slug}': {e}")

        return base_img

    def _render_raster_element(self, base_img: Image.Image, element: Dict[str, Any],
                               poster_width: int, poster_height: int) -> Image.Image:
        """
        Render raster (image) element.

        Args:
            base_img: Base image to render onto
            element: Raster element dictionary
            poster_width: Poster width
            poster_height: Poster height

        Returns:
            Updated image
        """
        # Get image path
        image_path = element.get('imagePath', '')
        if not image_path:
            self.logger.warning("Raster element missing imagePath")
            return base_img

        # Load asset image
        asset_path = self.asset_dir / image_path.lstrip('/')
        if not asset_path.exists():
            self.logger.warning(f"Asset not found: {image_path}")
            return base_img

        try:
            asset_img = Image.open(asset_path)
            if asset_img.mode != 'RGBA':
                asset_img = asset_img.convert('RGBA')

            # Scale if width/height specified
            target_width = element.get('width')
            target_height = element.get('height')

            if target_width or target_height:
                orig_width, orig_height = asset_img.size
                aspect = orig_width / orig_height

                if target_width and target_height:
                    new_size = (int(target_width), int(target_height))
                elif target_width:
                    new_size = (int(target_width), int(target_width / aspect))
                else:
                    new_size = (int(target_height * aspect), int(target_height))

                asset_img = asset_img.resize(new_size, Image.Resampling.LANCZOS)

            # Calculate position
            x, y = self._calculate_position(element, poster_width, poster_height,
                                           asset_img.width, asset_img.height)

            # Apply opacity
            opacity = element.get('opacity', 1.0)
            if opacity < 1.0:
                asset_img = self._apply_opacity(asset_img, opacity)

            # Composite onto base image
            base_img.paste(asset_img, (x, y), asset_img)

            self.logger.debug(f"Rendered raster element: {image_path} at ({x}, {y})")

        except Exception as e:
            self.logger.error(f"Failed to render raster element: {e}")

        return base_img

    def _render_text_element(self, base_img: Image.Image, element: Dict[str, Any],
                            media_info: Dict[str, Any],
                            poster_width: int, poster_height: int) -> Image.Image:
        """
        Render text element.

        Args:
            base_img: Base image to render onto
            element: Text element dictionary
            media_info: Media info for text interpolation
            poster_width: Poster width
            poster_height: Poster height

        Returns:
            Updated image
        """
        # Get text (with variable substitution)
        text_template = element.get('text', '')
        text = self._interpolate_text(text_template, media_info)

        if not text:
            return base_img

        # Get font
        font_name = element.get('font', 'DejaVuSans-Bold')
        font_size = element.get('size', int(poster_height * 0.05))  # 5% of poster height
        font = self._load_font(font_name, int(font_size))

        # Parse colors
        text_color = self._parse_color(element.get('color', '#FFFFFF'))
        background_color = element.get('background')

        # Create drawing context
        draw = ImageDraw.Draw(base_img)

        # Calculate text bounding box
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        # Calculate position
        x, y = self._calculate_position(element, poster_width, poster_height,
                                       text_width, text_height)

        # Apply X/Y offset nudge
        x += int(element.get('xOffset', 0) or 0)
        y += int(element.get('yOffset', 0) or 0)

        # Draw background if specified
        if background_color:
            bg_color = self._parse_color(background_color)
            padding = int(poster_width * 0.01)

            # Create overlay for semi-transparent background
            overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rounded_rectangle(
                [(x - padding, y - padding),
                 (x + text_width + padding, y + text_height + padding)],
                radius=5,
                fill=bg_color
            )
            base_img = Image.alpha_composite(base_img, overlay)

            # Redraw context
            draw = ImageDraw.Draw(base_img)

        # Apply opacity to text color
        opacity = element.get('opacity', 1.0)
        if opacity < 1.0:
            r, g, b, a = text_color
            text_color = (r, g, b, int(a * opacity))

        # Draw text
        draw.text((x, y), text, font=font, fill=text_color)

        self.logger.debug(f"Rendered text element: '{text}' at ({x}, {y})")

        return base_img

    def _calculate_position(self, element: Dict[str, Any],
                           poster_width: int, poster_height: int,
                           element_width: int, element_height: int) -> tuple:
        """
        Calculate element position based on template settings.

        Args:
            element: Element dictionary
            poster_width: Poster width
            poster_height: Poster height
            element_width: Element width
            element_height: Element height

        Returns:
            Tuple of (x, y) coordinates
        """
        position = element.get('position', 'top_left')
        margin = int(poster_width * 0.02)  # 2% margin

        if position == 'custom':
            x = element.get('x', margin)
            y = element.get('y', margin)
        elif position == 'top_left':
            x = margin
            y = margin
        elif position == 'top_right':
            x = poster_width - element_width - margin
            y = margin
        elif position == 'bottom_left':
            x = margin
            y = poster_height - element_height - margin
        elif position == 'bottom_right':
            x = poster_width - element_width - margin
            y = poster_height - element_height - margin
        elif position == 'center':
            x = (poster_width - element_width) // 2
            y = (poster_height - element_height) // 2
        else:
            x = margin
            y = margin

        return int(x), int(y)

    def _interpolate_text(self, text_template: str, media_info: Dict[str, Any]) -> str:
        """
        Interpolate variables in text template.

        Replaces {variable_name} with values from media_info.

        Args:
            text_template: Text with {variables}
            media_info: Media information dictionary

        Returns:
            Interpolated text
        """
        try:
            return text_template.format(
                resolution=media_info.get('resolution', ''),
                hdr_format=media_info.get('hdr_format', ''),
                audio_codec=media_info.get('audio_codec', ''),
                audio_channels=media_info.get('audio_channels', ''),
                video_codec=media_info.get('video_codec', ''),
                container=media_info.get('container', ''),
                bitrate=media_info.get('bitrate', '')
            )
        except Exception as e:
            self.logger.error(f"Failed to interpolate text: {e}")
            return text_template

    def _load_font(self, font_name: str, size: int) -> ImageFont.FreeTypeFont:
        """
        Load font by name.

        Args:
            font_name: Font name (without extension)
            size: Font size

        Returns:
            PIL Font object
        """
        # Try font locations
        font_paths = [
            self.asset_dir / 'fonts' / f'{font_name}.ttf',
            self.asset_dir / 'fonts' / f'{font_name}.otf',
            Path(__file__).parent / 'fonts' / 'cache' / f'{font_name}.ttf',
            Path(__file__).parent / 'fonts' / 'cache' / f'{font_name}.otf',
            Path(f'/usr/share/fonts/TTF/{font_name}.ttf'),
            Path(f'/usr/share/fonts/truetype/{font_name.lower()}/{font_name}.ttf'),
            Path(f'/usr/share/fonts/truetype/dejavu/{font_name}.ttf'),
        ]

        for font_path in font_paths:
            if font_path.exists():
                try:
                    return ImageFont.truetype(str(font_path), size)
                except Exception as e:
                    self.logger.debug(f"Failed to load font {font_path}: {e}")

        # Fallback to default
        self.logger.warning(f"Font '{font_name}' not found, using default")
        return ImageFont.load_default()

    def _parse_color(self, color_str: str) -> tuple:
        """
        Parse hex color string to RGBA tuple.

        Args:
            color_str: Hex color like '#FFFFFF' or '#FFFFFFAA'

        Returns:
            RGBA tuple
        """
        try:
            color_str = color_str.lstrip('#')

            if len(color_str) == 3:
                # #RGB -> #RRGGBB
                r = int(color_str[0] * 2, 16)
                g = int(color_str[1] * 2, 16)
                b = int(color_str[2] * 2, 16)
                a = 255
            elif len(color_str) == 6:
                # #RRGGBB
                r = int(color_str[0:2], 16)
                g = int(color_str[2:4], 16)
                b = int(color_str[4:6], 16)
                a = 255
            elif len(color_str) == 8:
                # #RRGGBBAA
                r = int(color_str[0:2], 16)
                g = int(color_str[2:4], 16)
                b = int(color_str[4:6], 16)
                a = int(color_str[6:8], 16)
            else:
                raise ValueError(f"Invalid color format: #{color_str}")

            return (r, g, b, a)

        except Exception as e:
            self.logger.error(f"Failed to parse color '{color_str}': {e}")
            return (255, 255, 255, 255)  # Default to white

    def _apply_opacity(self, img: Image.Image, opacity: float) -> Image.Image:
        """
        Apply opacity to image.

        Args:
            img: Image with alpha channel
            opacity: Opacity value (0.0 to 1.0)

        Returns:
            Image with adjusted opacity
        """
        if img.mode != 'RGBA':
            img = img.convert('RGBA')

        # Modify alpha channel
        r, g, b, a = img.split()
        a = a.point(lambda p: int(p * opacity))
        return Image.merge('RGBA', (r, g, b, a))

    # ──────────────────────────────────────────────────────────────────────
    #  DESIGNED BADGE RENDERER
    # ──────────────────────────────────────────────────────────────────────

    def _render_background_panel(self, base_img: Image.Image,
                                  badge: Dict[str, Any]) -> Image.Image:
        """Render a background_panel (Tray) — a rounded rect with optional border, no text."""
        try:
            sx = base_img.width  / 600.0
            sy = base_img.height / 900.0
            s  = sx

            bx   = int(badge.get('x', 9)  * sx)
            by   = int(badge.get('y', 754) * sy)
            raw_w = badge.get('width',  180) or 180
            raw_h = badge.get('height',  58) or 58
            W    = max(4, int(raw_w * s))
            H    = max(4, int(raw_h * sy))
            R    = max(0, int(badge.get('borderRadius', 8) * s))
            pad  = max(0, int(badge.get('bgPadding', 0) * s))
            overall_opacity = float(badge.get('opacity', 1.0))

            px, py = pad, pad
            pw, ph = W - pad * 2, H - pad * 2

            panel_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))

            # Background
            bg_type   = badge.get('bgType', 'solid')
            bg_color  = self._color_with_opacity(badge.get('bgColor',  '#000000'),
                                                  float(badge.get('bgOpacity', 0.8)))
            bg_color2 = self._color_with_opacity(badge.get('bgColor2', '#000000'),
                                                  float(badge.get('bgOpacity', 0.8)))
            if bg_type == 'gradient':
                angle = float(badge.get('bgGradientAngle', 135))
                grad  = self._make_gradient(pw, ph, bg_color, bg_color2, angle)
                mask  = self._rounded_mask(pw, ph, R)
                ov_bg = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                ov_bg.paste(grad, (px, py), mask)
                panel_img = Image.alpha_composite(panel_img, ov_bg)
            else:
                ov_bg = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(ov_bg).rounded_rectangle(
                    [(px, py), (px + pw - 1, py + ph - 1)], radius=R, fill=bg_color)
                panel_img = Image.alpha_composite(panel_img, ov_bg)

            # Border
            if badge.get('borderEnabled', True):
                bw   = max(1, int(badge.get('borderWidth', 1)))
                bc   = self._color_with_opacity(badge.get('borderColor', '#ffffff'),
                                                float(badge.get('borderOpacity', 0.08)))
                half = bw // 2
                b_ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(b_ov).rounded_rectangle(
                    [(px + half, py + half), (px + pw - 1 - half, py + ph - 1 - half)],
                    radius=max(0, R - half), outline=bc, width=bw)
                panel_img = Image.alpha_composite(panel_img, b_ov)

            if overall_opacity < 1.0:
                panel_img = self._apply_opacity(panel_img, overall_opacity)

            if 0 <= bx < base_img.width and 0 <= by < base_img.height:
                overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
                overlay.paste(panel_img, (bx, by))
                base_img = Image.alpha_composite(base_img, overlay)

        except Exception as e:
            self.logger.error(f"Failed to render background_panel: {e}", exc_info=True)

        return base_img

    # ──────────────────────────────────────────────────────────────────────

    def _render_designed_badge(self, base_img: Image.Image,
                               badge: Dict[str, Any],
                               media_info: Optional[Dict[str, Any]] = None) -> Image.Image:
        """
        Render a fully procedural designed_badge onto base_img.

        The badge is built from layers:
          base background (solid or gradient)
          → left segment (optional)
          → divider line (optional)
          → right segment (optional, single or stacked text)
          → border stroke
          → top highlight gradient line
        """
        import math

        media_info = media_info or {}

        # Scale factors: canvas is 600×900; use independent x/y scales so
        # non-2:3 posters place badges in the correct proportional position.
        sx = base_img.width  / 600.0
        sy = base_img.height / 900.0
        # Uniform scale for sizing/fonts — use the smaller of sx/sy so badges
        # never overflow on non-2:3 posters (e.g. square or wide art).
        s = min(sx, sy)

        # Audio codec/combo badge preset → dedicated liquid-glass renderer
        # audio_codec: styled without channel count
        # audio_combo: styled with channel count in corner
        if badge.get('badgePreset') in ('audio_codec', 'audio_combo'):
            badge_result = self._render_audio_codec_badge_styled(badge, media_info, s)
            if badge_result is not None:
                bx = int(badge.get('x', 20) * sx)
                by = int(badge.get('y', 20) * sy)
                if 0 <= bx < base_img.width and 0 <= by < base_img.height:
                    overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
                    overlay.paste(badge_result, (bx, by))
                    base_img = Image.alpha_composite(base_img, overlay)
                return base_img

        # ── Logo-only mode for network / studio ──────────────────────────
        if badge.get('logoOnly') and badge.get('badgePreset') in ('network', 'studio'):
            preset = badge.get('badgePreset')
            name_tmpl = badge.get('leftText', '{{network}}' if preset == 'network' else '{{studio}}')
            name_val  = self._interpolate_badge_text(name_tmpl, media_info)
            if name_val:
                logo_variant = badge.get('logoVariant', 'mono')
                if preset == 'network':
                    sub_dir = 'network/color' if logo_variant == 'color' else 'network/white'
                else:
                    sub_dir = 'studio/bigger' if logo_variant == 'color' else 'studio/standard'
                rel = f'{sub_dir}/{name_val}.png'
                logo_file = _USER_LOGO_DIR / rel
                if not logo_file.exists():
                    logo_file = _SYSTEM_LOGO_DIR / rel
                # Fuzzy fallback: case-insensitive + normalized (strip spaces/punctuation)
                if not logo_file.exists():
                    import re as _re
                    # Known aliases: map Trakt/TVDB names → logo filename stems
                    _NETWORK_ALIASES = {
                        'hbo max': 'Max',
                        'hbo go':  'HBO',
                        'amazon prime video': 'Prime Video',
                        'amazon': 'Prime Video',
                        'prime': 'Prime Video',
                        'abc (us)': 'ABC',
                        'nbc (us)': 'NBC',
                        'cbs (us)': 'CBS',
                        'fox (us)': 'FOX',
                        'apple tv': 'Apple TV+',
                        'disney plus': 'Disney+',
                        'paramount plus': 'Paramount+',
                    }
                    def _norm(v):
                        return _re.sub(r'[\s\-_\.]', '', v).lower()
                    # Check alias map first
                    alias_stem = _NETWORK_ALIASES.get(name_val.lower())
                    if alias_stem:
                        for search_dir in (_USER_LOGO_DIR / sub_dir, _SYSTEM_LOGO_DIR / sub_dir):
                            candidate = search_dir / f'{alias_stem}.png'
                            if candidate.exists():
                                logo_file = candidate
                                self.logger.debug(
                                    f"Alias logo match '{name_val}' → '{alias_stem}.png'")
                                break
                    # Still not found — try normalized + word-containment scan
                    if not logo_file.exists():
                        name_norm = _norm(name_val)
                        name_words = set(_re.split(r'\s+', name_val.lower()))
                        for search_dir in (_USER_LOGO_DIR / sub_dir, _SYSTEM_LOGO_DIR / sub_dir):
                            if not search_dir.is_dir():
                                continue
                            best = None
                            best_score = 0
                            for f in search_dir.iterdir():
                                if f.suffix.lower() != '.png':
                                    continue
                                stem = f.stem
                                # 1. Case-insensitive exact
                                if stem.lower() == name_val.lower():
                                    best = f; break
                                # 2. Normalized (strip spaces/punct) — handles "KBS 2" → "KBS2"
                                if _norm(stem) == name_norm:
                                    best = f; break
                                # 3. Containment: all logo-stem words appear in network name
                                stem_words = set(_re.split(r'\s+', stem.lower()))
                                if stem_words and stem_words <= name_words:
                                    score = len(' '.join(stem_words))  # prefer longer stems
                                    if score > best_score:
                                        best_score = score; best = f
                            if best:
                                logo_file = best
                                if logo_file.exists():
                                    self.logger.debug(
                                        f"Fuzzy logo match '{name_val}' → '{logo_file.name}'")
                                    break
                if not logo_file.exists():
                    self.logger.debug(
                        f"Logo not found for {preset}='{name_val}' (tried {rel})")
                if logo_file.exists():
                    try:
                        bx = int(badge.get('x', 20) * sx)
                        by = int(badge.get('y', 20) * sy)
                        R_logo = max(1, int(badge.get('borderRadius', 8) * s))
                        bg_pad_logo = max(0, int(badge.get('bgPadding', 0) * s))
                        raw_h = badge.get('height', 58)
                        H_logo = max(1, int(raw_h * s))
                        logo_img = Image.open(logo_file).convert('RGBA')
                        logo_h = max(2, H_logo - bg_pad_logo * 2)
                        logo_w = round(logo_img.width * (logo_h / logo_img.height)) if logo_img.height else logo_h
                        # Respect fixed width if set — constrain logo to fit within it
                        raw_w = badge.get('width', 0) or 0
                        fixed_W = max(1, int(raw_w * s)) if raw_w > 0 else 0
                        if fixed_W > 0:
                            max_logo_w = max(2, fixed_W - bg_pad_logo * 2)
                            if logo_w > max_logo_w:
                                logo_w = max_logo_w
                                logo_h = round(logo_img.height * (logo_w / logo_img.width)) if logo_img.width else logo_h
                                logo_h = max(2, logo_h)
                        logo_img = logo_img.resize((logo_w, logo_h), Image.Resampling.LANCZOS)
                        W_logo = fixed_W if fixed_W > 0 else logo_w + bg_pad_logo * 2
                        badge_img = Image.new('RGBA', (W_logo, H_logo), (0, 0, 0, 0))
                        bg_col = self._color_with_opacity(
                            badge.get('bgColor', '#000000'), float(badge.get('bgOpacity', 0.8)))
                        ov_bg = Image.new('RGBA', (W_logo, H_logo), (0, 0, 0, 0))
                        ImageDraw.Draw(ov_bg).rounded_rectangle(
                            [(0, 0), (W_logo - 1, H_logo - 1)], radius=R_logo, fill=bg_col)
                        badge_img = Image.alpha_composite(badge_img, ov_bg)
                        # Center logo within badge
                        logo_x = max(0, (W_logo - logo_w) // 2)
                        logo_y = max(0, (H_logo - logo_h) // 2)
                        badge_img.paste(logo_img, (logo_x, logo_y), logo_img)
                        # Border
                        if badge.get('borderEnabled', True):
                            bw = max(1, int(badge.get('borderWidth', 1)))
                            bc = self._color_with_opacity(
                                badge.get('borderColor', '#ffffff'),
                                float(badge.get('borderOpacity', 0.08)))
                            b_ov = Image.new('RGBA', (W_logo, H_logo), (0, 0, 0, 0))
                            half = bw // 2
                            ImageDraw.Draw(b_ov).rounded_rectangle(
                                [(half, half), (W_logo - 1 - half, H_logo - 1 - half)],
                                radius=max(0, R_logo - half), outline=bc, width=bw)
                            badge_img = Image.alpha_composite(badge_img, b_ov)
                        overall_opacity_logo = float(badge.get('opacity', 1.0))
                        if overall_opacity_logo < 1.0:
                            badge_img = self._apply_opacity(badge_img, overall_opacity_logo)
                        clip_mask = self._rounded_mask(W_logo, H_logo, R_logo)
                        clipped = Image.new('RGBA', (W_logo, H_logo), (0, 0, 0, 0))
                        clipped.paste(badge_img, (0, 0), clip_mask)
                        badge_img = clipped
                        if 0 <= bx < base_img.width and 0 <= by < base_img.height:
                            overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
                            overlay.paste(badge_img, (bx, by))
                            base_img = Image.alpha_composite(base_img, overlay)
                    except Exception as e:
                        self.logger.warning(f"Logo-only render failed for '{name_val}': {e}")
            return base_img

        x  = int(badge.get('x', 20) * sx)
        y  = int(badge.get('y', 20) * sy)
        R  = max(1, int(badge.get('borderRadius', 8) * s))
        overall_opacity = float(badge.get('opacity', 1.0))
        r_ph = max(2, int(badge.get('rightPaddingH', 8) * s))
        l_ph = max(2, int(badge.get('leftPaddingH', 10) * s))
        bg_pad_d   = max(0, int(badge.get('bgPadding', 0)))
        bg_pad_ds  = max(0, int(bg_pad_d * s))

        # ── 1. Pre-load fonts ─────────────────────────────────────────────
        lf = self._load_google_font(
            badge.get('leftFont', 'Bebas Neue'),
            max(6, int(badge.get('leftFontSize', 18) * s)),
            badge.get('leftFontWeight', 'normal') == 'bold' or badge.get('leftBold', False),
            badge.get('leftFontStyle', 'normal') == 'italic')
        f1 = self._load_google_font(
            badge.get('rightFont1', 'Barlow Condensed'),
            max(6, int(badge.get('rightFontSize1', 12) * s)),
            badge.get('rightFontWeight1', 'bold') == 'bold' or badge.get('rightBold1', True),
            badge.get('rightFontStyle1', 'normal') == 'italic')
        f2 = self._load_google_font(
            badge.get('rightFont2', 'Barlow Condensed'),
            max(6, int(badge.get('rightFontSize2', 10) * s)),
            badge.get('rightFontWeight2', 'bold') == 'bold' or badge.get('rightBold2', True),
            badge.get('rightFontStyle2', 'normal') == 'italic')

        # ── 2. Pre-interpolate all texts ──────────────────────────────────
        _rfmt    = badge.get('ratingFormat', 'auto')
        raw_left = self._interpolate_badge_text(badge.get('leftText', ''), media_info, _rfmt)
        if badge.get('friendlyResolution'):
            import re as _re
            raw_left = _re.sub(r'(?i)\b2160p\b', '4K', raw_left)
            raw_left = _re.sub(r'(?i)\b1440p\b', '2K', raw_left)
        t1_raw   = self._interpolate_badge_text(badge.get('rightText1', ''), media_info, _rfmt)
        t2_raw   = self._interpolate_badge_text(badge.get('rightText2', ''), media_info, _rfmt)

        # ── Skip badge if text resolves to nothing meaningful ─────────────
        import re as _vre
        _var_re    = _vre.compile(r'\{\{\w+\}\}')
        _tmpl_left = badge.get('leftText')  or ''
        _tmpl_t1   = badge.get('rightText1') or ''
        _tmpl_t2   = badge.get('rightText2') or ''

        # If left text is a dynamic variable and resolved to empty (or a single
        # stray character), the badge has no primary value — skip it entirely.
        # e.g. leftText="{{format}}" with no format available for this item.
        if _var_re.search(_tmpl_left) and len(raw_left.strip()) < 2:
            return base_img

        # Fallback: skip if every text segment is completely empty.
        if not raw_left and not t1_raw and not t2_raw:
            return base_img

        # ── Status color override ──────────────────────────────────────────
        if badge.get('useStatusColors') and badge.get('statusColorMap'):
            _scm   = badge['statusColorMap']
            _sctxt = raw_left or t1_raw
            _sccol = _scm.get(_sctxt)
            if _sccol:
                badge = dict(badge)  # shallow copy — don't mutate stored data
                # Apply to whole badge background (works even when right segment is hidden)
                badge['bgType']        = 'solid'
                badge['bgColor']       = _sccol
                badge['bgColor2']      = _sccol
                # Also apply to right segment for split-segment layouts
                badge['rightBgType']   = 'solid'
                badge['rightBgColor']  = _sccol
                badge['rightBgColor2'] = _sccol

        # ── 3. Determine effective layout ─────────────────────────────────
        left_enabled  = badge.get('leftEnabled', True)
        left_w        = max(4, int(badge.get('leftWidth', 60) * s))
        right_enabled = badge.get('rightEnabled', True)
        div_enabled   = badge.get('dividerEnabled', True)
        auto_layout   = badge.get('autoLayout', False)
        layout        = badge.get('rightLayout', 'single')

        if auto_layout:
            if t1_raw and t2_raw:
                layout = 'stacked'
            elif t1_raw:
                layout = 'single'
                t2_raw = ''
            elif t2_raw:
                layout = 'single'
                t1_raw = t2_raw  # promote t2 → t1 render slot
                t2_raw = ''
            else:
                fallback_tmpl = badge.get('rightTextFallback', '')
                fallback_val  = self._interpolate_badge_text(fallback_tmpl, media_info, _rfmt) if fallback_tmpl else ''
                if fallback_val:
                    layout = 'single'
                    t1_raw = fallback_val
                else:
                    right_enabled = False
                    div_enabled   = False

        div_active = div_enabled and left_enabled and right_enabled

        # ── Vertical stack path ───────────────────────────────────────────
        if badge.get('verticalStack'):
            base_img = self._render_designed_badge_vertical(
                badge, base_img, media_info, sx, sy, s,
                raw_left, t1_raw, t2_raw,
                left_enabled, right_enabled, div_active,
                layout, lf, f1, f2)
            return base_img

        # ── 4. Auto-size W / H ────────────────────────────────────────────
        raw_w = badge.get('width',  0)
        raw_h = badge.get('height', 0)
        auto_w = not raw_w or str(raw_w).lower() == 'auto'
        auto_h = not raw_h or str(raw_h).lower() == 'auto'

        _td = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        if auto_h:
            lfs  = int(badge.get('leftFontSize',   18) * s)
            rfs1 = int(badge.get('rightFontSize1', 12) * s)
            rfs2 = int(badge.get('rightFontSize2', 10) * s)
            v_pad = max(8, int(16 * s))
            if layout == 'stacked' and right_enabled:
                gap_px   = max(2, int(badge.get('rightStackGap', 4) * s))
                right_h  = rfs1 + gap_px + rfs2
                max_cont = max(lfs if left_enabled else 0, right_h)
            else:
                max_cont = max(lfs if left_enabled else 0, rfs1 if right_enabled else 0)
            H = max(16, max_cont + v_pad + 2 * bg_pad_ds)
        else:
            H = max(1, int(raw_h * s))

        # Fluid font: fit BOTH left AND right fonts to badge height.
        # Fixed-W badges: right content measured first, left gets remaining space — no clipping.
        # Auto-W badges: both sides expand to content at height-fitted font sizes.
        if badge.get('resFluid') and left_enabled and raw_left:
            _slot_h  = H - bg_pad_ds * 2 - 8
            _max_lfs = max(6, int(badge.get('leftFontSize',   18) * s))
            _max_r1  = max(6, int(badge.get('rightFontSize1', 12) * s))
            _max_r2  = max(6, int(badge.get('rightFontSize2', 10) * s))

            def _fluid_font_h(text, font_name, bold, max_fs, max_w=99999, frac=0.72):
                _target_h = int(_slot_h * frac)
                _lo2, _hi2, _best2 = 6, max_fs, 6
                while _lo2 <= _hi2:
                    _mid2 = (_lo2 + _hi2) // 2
                    _tf2  = self._load_google_font(font_name, _mid2, bold)
                    _bb2  = _td.textbbox((0, 0), text, font=_tf2)
                    if ((_bb2[3] - _bb2[1]) <= _target_h and
                            (_bb2[2] - _bb2[0]) <= max_w):
                        _best2 = _mid2; _lo2 = _mid2 + 1
                    else:
                        _hi2 = _mid2 - 1
                return _best2

            _l_bold  = badge.get('leftFontWeight',   'normal') == 'bold' or badge.get('leftBold',   False)
            _r1_bold = badge.get('rightFontWeight1', 'bold')   == 'bold' or badge.get('rightBold1', True)
            _r2_bold = badge.get('rightFontWeight2', 'bold')   == 'bold' or badge.get('rightBold2', True)
            # Step 1: fit right fonts to height
            if right_enabled and t1_raw:
                if layout == 'stacked' and t2_raw:
                    _best_r1 = _fluid_font_h(t1_raw,
                                              badge.get('rightFont1', 'Barlow Condensed'),
                                              _r1_bold, _max_r1, frac=0.45)
                    _best_r2 = _fluid_font_h(t2_raw,
                                              badge.get('rightFont2', 'Barlow Condensed'),
                                              _r2_bold, _max_r2, frac=0.45)
                else:
                    _best_r1 = _fluid_font_h(t1_raw,
                                              badge.get('rightFont1', 'Barlow Condensed'),
                                              _r1_bold, _max_r1)
                    _best_r2 = _max_r2
                f1 = self._load_google_font(badge.get('rightFont1', 'Barlow Condensed'),
                                            _best_r1, _r1_bold)
                f2 = self._load_google_font(badge.get('rightFont2', 'Barlow Condensed'),
                                            _best_r2, _r2_bold)

            # Step 2: determine max left width
            _max_left_w = 99999
            if not auto_w and right_enabled and t1_raw:
                _bb_r1 = _td.textbbox((0, 0), t1_raw, font=f1)
                _rw_fluid = _bb_r1[2] + r_ph * 2
                if layout == 'stacked' and t2_raw:
                    _bb_r2 = _td.textbbox((0, 0), t2_raw, font=f2)
                    _rw_fluid = max(_rw_fluid, _bb_r2[2] + r_ph * 2)
                _fixed_W = max(1, int(raw_w * s))
                _max_left_w = max(10, _fixed_W - _rw_fluid - (1 if div_active else 0))

            # Step 3: fit left font to height AND available left width
            _best_lfs = _fluid_font_h(raw_left,
                                       badge.get('leftFont', 'Bebas Neue'),
                                       _l_bold,
                                       _max_lfs,
                                       max_w=max(1, _max_left_w - l_ph * 2))
            lf = self._load_google_font(badge.get('leftFont', 'Bebas Neue'),
                                        _best_lfs, _l_bold)

        # Always expand left_w to fit its text — prevents clipping when W is fixed
        if left_enabled and raw_left:
            bb_l  = _td.textbbox((0, 0), raw_left, font=lf)
            left_w = max(left_w, bb_l[2] + l_ph * 2)

        if auto_w:
            rw_px = 0
            if right_enabled and t1_raw:
                bb1 = _td.textbbox((0, 0), t1_raw, font=f1)
                # Use bb1[2] (not bb1[2]-bb1[0]) so positive glyph x-offsets don't
                # cause the text to overflow the auto-computed badge width.
                rw_px = bb1[2] + r_ph * 2
                if layout == 'stacked' and t2_raw:
                    bb2 = _td.textbbox((0, 0), t2_raw, font=f2)
                    rw_px = max(rw_px, bb2[2] + r_ph * 2)
            W = max(20, left_w + (1 if div_active else 0) + rw_px)
        else:
            W = max(1, int(raw_w * s))

        # When there is no right segment the left occupies the full badge width —
        # update left_w so the text centres within W rather than within the original
        # (narrower) leftWidth setting.
        if not div_active and left_enabled:
            left_w = W

        # Clamp position so badge never overflows the poster edge.
        # Without this, font-width differences between Canvas (browser) and PIL
        # can cause auto-sized badges near the edge to clip differently in Plex.
        x = max(0, min(x, max(0, base_img.width  - W)))
        y = max(0, min(y, max(0, base_img.height - H)))

        badge_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))

        # ── 5. Base background ────────────────────────────────────────────
        bg_type    = badge.get('bgType', 'solid')
        bg_color   = self._color_with_opacity(badge.get('bgColor',  '#ffffff'), badge.get('bgOpacity',  0.03))
        bg_color2  = self._color_with_opacity(badge.get('bgColor2', '#ffffff'), badge.get('bgOpacity',  0.03))
        bg_angle   = float(badge.get('bgGradientAngle', 135))

        if bg_type == 'gradient':
            grad = self._make_gradient(W, H, bg_color, bg_color2, bg_angle)
            mask = self._rounded_mask(W, H, R)
            badge_img.paste(grad, (0, 0), mask)
        else:
            ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(ov).rounded_rectangle([(0, 0), (W - 1, H - 1)], radius=R, fill=bg_color)
            badge_img = Image.alpha_composite(badge_img, ov)

        # ── 6. Left segment ───────────────────────────────────────────────
        if left_enabled:
            lbg_op = float(badge.get('leftBgOpacity', 0.0))
            if lbg_op > 0:
                lbg = self._color_with_opacity(badge.get('leftBgColor', '#000000'), lbg_op)
                ov2 = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(ov2).rectangle([(0, 0), (left_w - 1, H - 1)], fill=lbg)
                badge_img = Image.alpha_composite(badge_img, ov2)
            if raw_left:
                lc   = self._color_with_opacity(badge.get('leftColor', '#ffffff'),
                                                float(badge.get('leftOpacity', 0.9)))
                draw = ImageDraw.Draw(badge_img)
                bb   = draw.textbbox((0, 0), raw_left, font=lf)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                draw.text(((left_w - tw) // 2, bg_pad_ds + (H - 2 * bg_pad_ds - th) // 2 - bb[1]),
                          raw_left, font=lf, fill=lc)

        # ── 7. Divider ────────────────────────────────────────────────────
        div_x = left_w if left_enabled else 0
        if div_active:
            dc = self._color_with_opacity(badge.get('dividerColor', '#ffffff'),
                                          float(badge.get('dividerOpacity', 0.07)))
            ov3 = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(ov3).rectangle([(div_x, 0), (div_x, H - 1)], fill=dc)
            badge_img = Image.alpha_composite(badge_img, ov3)
            div_x += 1

        # ── 8. Right segment ──────────────────────────────────────────────
        if right_enabled:
            rx     = div_x
            rw     = max(1, W - rx)
            r_bg_t = badge.get('rightBgType', 'gradient')
            r_bop  = float(badge.get('rightBgOpacity', 0.15))
            rc1    = self._color_with_opacity(badge.get('rightBgColor',  '#7838ff'), r_bop)
            rc2    = self._color_with_opacity(badge.get('rightBgColor2', '#ff6e14'), r_bop)
            r_ang  = float(badge.get('rightBgGradientAngle', 135))

            if r_bg_t == 'gradient':
                rg_img = self._make_gradient(rw, H, rc1, rc2, r_ang)
                r_ov   = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                r_ov.paste(rg_img, (rx, 0))
                badge_img = Image.alpha_composite(badge_img, r_ov)
            else:
                r_ov2 = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(r_ov2).rectangle([(rx, 0), (W - 1, H - 1)], fill=rc1)
                badge_img = Image.alpha_composite(badge_img, r_ov2)

            c1_t = self._color_with_opacity(badge.get('rightColor1', '#bc94ff'),
                                            float(badge.get('rightOpacity1', 0.92)))
            draw = ImageDraw.Draw(badge_img)

            if layout == 'stacked':
                c2_t = self._color_with_opacity(badge.get('rightColor2', '#ffa848'),
                                                float(badge.get('rightOpacity2', 0.92)))
                gap_px = max(2, int(badge.get('rightStackGap', 4) * s))
                # Center the visual ink block within H so padding is even top/bottom.
                # Em-size-based centering causes asymmetric padding for all-caps text
                # (no descenders) because ink height << em height, and the error
                # scales with s (e.g. 8 px off at 1500-wide poster).
                if t1_raw and t2_raw:
                    bb1 = draw.textbbox((0, 0), t1_raw, font=f1)
                    bb2 = draw.textbbox((0, 0), t2_raw, font=f2)
                    ink_h1 = bb1[3] - bb1[1]
                    ink_h2 = bb2[3] - bb2[1]
                    y_ink_top = bg_pad_ds + (H - 2 * bg_pad_ds - (ink_h1 + gap_px + ink_h2)) // 2
                    draw.text((rx + r_ph, y_ink_top - bb1[1]), t1_raw, font=f1, fill=c1_t)
                    draw.text((rx + r_ph, y_ink_top + ink_h1 + gap_px - bb2[1]), t2_raw, font=f2, fill=c2_t)
                elif t1_raw:
                    bb1 = draw.textbbox((0, 0), t1_raw, font=f1)
                    y_c = bg_pad_ds + (H - 2 * bg_pad_ds - (bb1[3] - bb1[1])) // 2
                    draw.text((rx + r_ph, y_c - bb1[1]), t1_raw, font=f1, fill=c1_t)
                elif t2_raw:
                    bb2 = draw.textbbox((0, 0), t2_raw, font=f2)
                    y_c = bg_pad_ds + (H - 2 * bg_pad_ds - (bb2[3] - bb2[1])) // 2
                    draw.text((rx + r_ph, y_c - bb2[1]), t2_raw, font=f2, fill=c2_t)
            else:
                if t1_raw:
                    bb1 = draw.textbbox((0, 0), t1_raw, font=f1)
                    ty  = bg_pad_ds + (H - 2 * bg_pad_ds - (bb1[3] - bb1[1])) // 2 - bb1[1]
                    draw.text((rx + r_ph, ty), t1_raw, font=f1, fill=c1_t)

        # ── Border ────────────────────────────────────────────────────────
        if badge.get('borderEnabled', True):
            bw  = max(1, int(badge.get('borderWidth', 1)))
            bc  = self._color_with_opacity(badge.get('borderColor', '#ffffff'),
                                           float(badge.get('borderOpacity', 0.08)))
            b_ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            half = bw // 2
            ImageDraw.Draw(b_ov).rounded_rectangle(
                [(half, half), (W - 1 - half, H - 1 - half)],
                radius=max(0, R - half), outline=bc, width=bw)
            badge_img = Image.alpha_composite(badge_img, b_ov)

        # ── Top highlight ─────────────────────────────────────────────────
        if badge.get('highlightEnabled', True):
            hl_op  = float(badge.get('highlightOpacity', 0.09))
            hl_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            hl_pix = hl_img.load()
            hx1    = int(W * 0.12)
            hx2    = int(W * 0.88)
            span   = max(1, hx2 - hx1)
            for px in range(hx1, min(hx2, W)):
                t     = (px - hx1) / span
                alpha = int(255 * hl_op * (1 - abs(2 * t - 1)))
                hl_pix[px, 0] = (255, 255, 255, alpha)
            badge_img = Image.alpha_composite(badge_img, hl_img)

        # ── Overall opacity & composite ───────────────────────────────────
        if overall_opacity < 1.0:
            badge_img = self._apply_opacity(badge_img, overall_opacity)

        # Final clip: enforce rounded corners (prevents right-segment bg bleeding outside)
        clip_mask = self._rounded_mask(W, H, R)
        clipped = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        clipped.paste(badge_img, (0, 0), clip_mask)
        badge_img = clipped

        if 0 <= x < base_img.width and 0 <= y < base_img.height:
            overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
            overlay.paste(badge_img, (x, y))
            base_img = Image.alpha_composite(base_img, overlay)

        return base_img

    def _render_designed_badge_vertical(
            self,
            badge: Dict[str, Any],
            base_img: Image.Image,
            media_info: Dict[str, Any],
            sx: float, sy: float, s: float,
            raw_left: str, t1_raw: str, t2_raw: str,
            left_enabled: bool, right_enabled: bool, div_active: bool,
            layout: str,
            lf, f1, f2) -> Image.Image:
        """Render a designed badge in vertical-stack mode (top = left, bottom = right)."""
        x = int(badge.get('x', 20) * sx)
        y = int(badge.get('y', 20) * sy)
        R = max(1, int(badge.get('borderRadius', 8) * s))
        overall_opacity = float(badge.get('opacity', 1.0))
        l_ph = max(2, int(badge.get('leftPaddingH', 10) * s))
        r_ph = max(2, int(badge.get('rightPaddingH',  8) * s))
        bg_pad_ds = max(0, int(badge.get('bgPadding', 0) * s))
        gap_px = max(2, int(badge.get('rightStackGap', 4) * s))
        v_pad  = max(4, int(10 * s))

        lfs  = int(badge.get('leftFontSize',   18) * s)
        rfs1 = int(badge.get('rightFontSize1', 12) * s)
        rfs2 = int(badge.get('rightFontSize2', 10) * s)

        _td = ImageDraw.Draw(Image.new('RGBA', (1, 1)))

        # Width: max of left / right text widths
        raw_w = badge.get('width', 0)
        auto_w = not raw_w or str(raw_w).lower() in ('0', 'auto')
        if auto_w:
            l_mw = 0
            if left_enabled and raw_left:
                bb = _td.textbbox((0, 0), raw_left, font=lf)
                l_mw = bb[2] + l_ph * 2
            r_mw = 0
            if right_enabled and t1_raw:
                bb1 = _td.textbbox((0, 0), t1_raw, font=f1)
                r_mw = bb1[2] + r_ph * 2
                if layout == 'stacked' and t2_raw:
                    bb2 = _td.textbbox((0, 0), t2_raw, font=f2)
                    r_mw = max(r_mw, bb2[2] + r_ph * 2)
            W = max(20, l_mw, r_mw)
        else:
            W = max(1, int(raw_w * s))

        # Heights per segment
        raw_h = badge.get('height', 0)
        auto_h = not raw_h or str(raw_h).lower() in ('0', 'auto')
        if auto_h:
            top_h = (lfs  + v_pad * 2 + bg_pad_ds * 2) if left_enabled  else 0
            right_ink = (rfs1 + gap_px + rfs2) if (layout == 'stacked' and t2_raw) else rfs1
            bot_h = (right_ink + v_pad * 2 + bg_pad_ds * 2) if right_enabled else 0
        else:
            total_h = max(1, int(raw_h * s))
            div_h   = 1 if div_active else 0
            top_h   = (round(total_h * 0.45) if (left_enabled and right_enabled)
                       else (total_h - div_h if left_enabled else 0))
            bot_h   = total_h - top_h - div_h

        H = (top_h if left_enabled else 0) + (1 if div_active else 0) + (bot_h if right_enabled else 0)
        H = max(4, H)

        badge_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))

        # Base background
        bg_type  = badge.get('bgType', 'solid')
        bg_color = self._color_with_opacity(badge.get('bgColor',  '#ffffff'), badge.get('bgOpacity',  0.03))
        bg_color2= self._color_with_opacity(badge.get('bgColor2', '#ffffff'), badge.get('bgOpacity',  0.03))
        bg_angle = float(badge.get('bgGradientAngle', 135))
        if bg_type == 'gradient':
            grad = self._make_gradient(W, H, bg_color, bg_color2, bg_angle)
            mask = self._rounded_mask(W, H, R)
            badge_img.paste(grad, (0, 0), mask)
        else:
            ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(ov).rounded_rectangle([(0, 0), (W - 1, H - 1)], radius=R, fill=bg_color)
            badge_img = Image.alpha_composite(badge_img, ov)

        draw = ImageDraw.Draw(badge_img)

        # Top segment (left text)
        if left_enabled:
            lbg_op = float(badge.get('leftBgOpacity', 0.0))
            if lbg_op > 0:
                lbg = self._color_with_opacity(badge.get('leftBgColor', '#000000'), lbg_op)
                ov2 = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(ov2).rectangle([(0, 0), (W - 1, top_h - 1)], fill=lbg)
                badge_img = Image.alpha_composite(badge_img, ov2)
                draw = ImageDraw.Draw(badge_img)
            if raw_left:
                lc  = self._color_with_opacity(badge.get('leftColor', '#ffffff'),
                                               float(badge.get('leftOpacity', 0.9)))
                bb  = draw.textbbox((0, 0), raw_left, font=lf)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                tx = (W - tw) // 2
                ty = bg_pad_ds + (top_h - 2 * bg_pad_ds - th) // 2 - bb[1]
                draw.text((tx, ty), raw_left, font=lf, fill=lc)

        # Horizontal divider
        div_y = top_h if left_enabled else 0
        if div_active:
            dc = self._color_with_opacity(badge.get('dividerColor', '#ffffff'),
                                          float(badge.get('dividerOpacity', 0.07)))
            ov3 = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(ov3).rectangle([(0, div_y), (W - 1, div_y)], fill=dc)
            badge_img = Image.alpha_composite(badge_img, ov3)
            draw = ImageDraw.Draw(badge_img)
            div_y += 1

        # Bottom segment (right text)
        if right_enabled:
            ry   = div_y
            r_bg_t = badge.get('rightBgType', 'gradient')
            r_bop  = float(badge.get('rightBgOpacity', 0.15))
            rc1  = self._color_with_opacity(badge.get('rightBgColor',  '#7838ff'), r_bop)
            rc2  = self._color_with_opacity(badge.get('rightBgColor2', '#ff6e14'), r_bop)
            r_ang = float(badge.get('rightBgGradientAngle', 135))
            if r_bg_t == 'gradient':
                rg_img = self._make_gradient(W, bot_h, rc1, rc2, r_ang)
                r_ov   = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                r_ov.paste(rg_img, (0, ry))
                badge_img = Image.alpha_composite(badge_img, r_ov)
            else:
                r_ov2 = Image.new('RGBA', (W, H), (0, 0, 0, 0))
                ImageDraw.Draw(r_ov2).rectangle([(0, ry), (W - 1, ry + bot_h - 1)], fill=rc1)
                badge_img = Image.alpha_composite(badge_img, r_ov2)
            draw = ImageDraw.Draw(badge_img)

            c1_t = self._color_with_opacity(badge.get('rightColor1', '#bc94ff'),
                                            float(badge.get('rightOpacity1', 0.92)))
            if layout == 'stacked' and t1_raw and t2_raw:
                c2_t = self._color_with_opacity(badge.get('rightColor2', '#ffa848'),
                                                float(badge.get('rightOpacity2', 0.92)))
                bb1 = draw.textbbox((0, 0), t1_raw, font=f1)
                bb2 = draw.textbbox((0, 0), t2_raw, font=f2)
                ink_h1 = bb1[3] - bb1[1]
                ink_h2 = bb2[3] - bb2[1]
                y_ink_top = ry + bg_pad_ds + (bot_h - 2 * bg_pad_ds - (ink_h1 + gap_px + ink_h2)) // 2
                tx1 = (W - (bb1[2] - bb1[0])) // 2
                tx2 = (W - (bb2[2] - bb2[0])) // 2
                draw.text((tx1, y_ink_top - bb1[1]), t1_raw, font=f1, fill=c1_t)
                draw.text((tx2, y_ink_top + ink_h1 + gap_px - bb2[1]), t2_raw, font=f2, fill=c2_t)
            elif t1_raw:
                bb1 = draw.textbbox((0, 0), t1_raw, font=f1)
                ink_h1 = bb1[3] - bb1[1]
                y_c = ry + bg_pad_ds + (bot_h - 2 * bg_pad_ds - ink_h1) // 2
                tx1 = (W - (bb1[2] - bb1[0])) // 2
                draw.text((tx1, y_c - bb1[1]), t1_raw, font=f1, fill=c1_t)

        # Border
        if badge.get('borderEnabled', True):
            bw_b = max(1, int(badge.get('borderWidth', 1)))
            bc   = self._color_with_opacity(badge.get('borderColor', '#ffffff'),
                                            float(badge.get('borderOpacity', 0.08)))
            b_ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            half = bw_b // 2
            ImageDraw.Draw(b_ov).rounded_rectangle(
                [(half, half), (W - 1 - half, H - 1 - half)],
                radius=max(0, R - half), outline=bc, width=bw_b)
            badge_img = Image.alpha_composite(badge_img, b_ov)

        # Top highlight
        if badge.get('highlightEnabled', True):
            hl_op  = float(badge.get('highlightOpacity', 0.09))
            hl_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            hl_pix = hl_img.load()
            hx1 = int(W * 0.12)
            hx2 = int(W * 0.88)
            span = max(1, hx2 - hx1)
            for px in range(hx1, min(hx2, W)):
                t  = (px - hx1) / span
                alpha = int(255 * hl_op * (1 - abs(2 * t - 1)))
                hl_pix[px, 0] = (255, 255, 255, alpha)
            badge_img = Image.alpha_composite(badge_img, hl_img)

        # Opacity & composite
        if overall_opacity < 1.0:
            badge_img = self._apply_opacity(badge_img, overall_opacity)

        clip_mask = self._rounded_mask(W, H, R)
        clipped = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        clipped.paste(badge_img, (0, 0), clip_mask)
        badge_img = clipped

        x = max(0, min(x, max(0, base_img.width  - W)))
        y = max(0, min(y, max(0, base_img.height - H)))
        if 0 <= x < base_img.width and 0 <= y < base_img.height:
            overlay = Image.new('RGBA', base_img.size, (0, 0, 0, 0))
            overlay.paste(badge_img, (x, y))
            base_img = Image.alpha_composite(base_img, overlay)
        return base_img

    def _draw_audio_codec_icon_pil(self, img: Image.Image,
                                    codec_type: str, cx: int, cy: int, size: int) -> None:
        """Draw a small codec icon onto img at (cx, cy), scaled to badge height `size`.
        Mirrors _drawAudioCodecIcon in layout_builder.js exactly."""
        s = size / 104.0  # scale factor relative to SVG viewBox height
        draw = ImageDraw.Draw(img)

        if codec_type == 'aac':
            # WiFi-style arcs + dot
            ax, ay = cx, cy + round((58 - 52) * s)
            sw = max(1, round(2.5 * s))
            for radius, alpha in [(16, 71), (11, 173), (6, 230)]:
                r = round(radius * s)
                # PIL arc: bounding box, start=180°, end=0° (upper semicircle)
                draw.arc([(ax - r, ay - r), (ax + r, ay + r)],
                         start=180, end=0, fill=(255, 255, 255, alpha), width=sw)
            # Dot
            dr = max(1, round(3 * s))
            dot_y = ay + round(4 * s)
            draw.ellipse([(cx - dr, dot_y - dr), (cx + dr, dot_y + dr)],
                         fill=(255, 255, 255, 184))

        elif codec_type == 'flac':
            # Equalizer bars
            bars = [(26, 8), (32.5, 16), (39, 12), (45.5, 24), (52, 14), (58.5, 18), (65, 7)]
            bar_w = max(1, round(4 * s))
            for bx_svg, bh_svg in bars:
                bx = cx + round((bx_svg - 45.5) * s)
                bh = max(1, round(bh_svg * s))
                by = cy - bh // 2
                draw.rounded_rectangle([(bx, by), (bx + bar_w - 1, by + bh - 1)],
                                       radius=max(1, round(2 * s)),
                                       fill=(52, 215, 115, 235))

        elif codec_type == 'pcm':
            # Two triangles (up + down)
            upTop = cy + round((36 - 52) * s)
            upBot = cy + round((48 - 52) * s)
            upL   = cx + round((26 - 35) * s)
            upR   = cx + round((44 - 35) * s)
            draw.polygon([(cx, upTop), (upL, upBot), (upR, upBot)],
                         fill=(255, 255, 255, 166))
            dnBot = cy + round((68 - 52) * s)
            dnTop = cy + round((56 - 52) * s)
            draw.polygon([(cx, dnBot), (upL, dnTop), (upR, dnTop)],
                         fill=(255, 255, 255, 77))

        elif codec_type == 'mp3':
            # Music note: ellipse + stem
            sw = max(1, round(2.5 * s))
            mx = cx + round((33 - 37) * s)
            my = cy + round((61 - 52) * s)
            rx, ry_e = round(8 * s), round(6 * s)
            draw.ellipse([(mx - rx, my - ry_e), (mx + rx, my + ry_e)],
                         outline=(255, 255, 255, 140), width=sw)
            stemX = cx + round((41 - 37) * s)
            stemTop = cy + round((40 - 52) * s)
            draw.line([(stemX, my), (stemX, stemTop)],
                      fill=(255, 255, 255, 140), width=sw)

        elif codec_type == 'opus':
            # Target: outer ring + inner dot
            sw = max(1, round(3.5 * s))
            r_outer = round(14 * s)
            draw.ellipse([(cx - r_outer, cy - r_outer), (cx + r_outer, cy + r_outer)],
                         outline=(255, 255, 255, 107), width=sw)
            r_inner = max(1, round(4 * s))
            draw.ellipse([(cx - r_inner, cy - r_inner), (cx + r_inner, cy + r_inner)],
                         fill=(255, 255, 255, 97))

    def _fit_font_size_pil(self, text: str, font_loader, max_w: float, max_h: float,
                           lo: int = 6, hi: int = 120) -> int:
        """Binary-search the largest integer font size where text fits within max_w × max_h.
        font_loader(size) -> PIL font.  Returns at least lo."""
        _td = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
        best = lo
        while lo <= hi:
            mid = (lo + hi) // 2
            f = font_loader(mid)
            bb = _td.textbbox((0, 0), text, font=f)
            w = bb[2] - bb[0]
            h = bb[3] - bb[1]
            if w <= max_w and h <= max_h:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _render_audio_codec_badge_styled(
            self,
            badge: Dict[str, Any],
            media_info: Dict[str, Any],
            s: float) -> Optional[Image.Image]:
        """
        Render a liquid-glass styled audio codec badge.

        Mirrors renderAudioCodecBadgeOnCanvas in layout_builder.js exactly:
        - Two-segment horizontal OR vertical-stack layout
        - Per-codec brand/variant rendering (DOLBY sub-label, DTS-HD "-HD" suffix, etc.)
        - audioFluid: binary-search font sizing matching JS per-codec height fractions
        - audioPad, audioLeftPct properties respected
        - FLAC, PCM, AAC, MP3, OPUS codec support (single-segment)
        - Channel count drawn as small corner text
        """
        codec_raw = (media_info.get('audio_codec') or '').strip()
        show_channels = badge.get('badgePreset') == 'audio_combo'
        channels = (media_info.get('audio_channels') or '').strip() if show_channels else ''
        if not codec_raw:
            return None

        c = codec_raw.lower()

        # ── Colour schemes: (right_bg_RGBA, right_text_RGBA) ──────────────
        CYAN   = ((0,   155, 255,  28), (0,   220, 255, 235))
        ORANGE = ((255, 130,  20,  36), (255, 138,  28, 242))
        GREEN  = ((45,  200,  90,  23), (52,  215, 115, 235))
        BLUE   = ((50,  130, 255,  23), (55,  145, 255, 235))
        PLAIN  = ((255, 255, 255,   8), (255, 255, 255, 230))

        # ── Codec detection (matches JS order exactly) ────────────────────
        codec_type = 'generic'
        brand = variant = ''
        scheme = PLAIN

        if 'truehd' in c and 'atmos' in c:
            codec_type = 'truehd-atmos'; brand = 'TRUEHD'; variant = 'ATMOS'; scheme = CYAN
        elif 'truehd' in c:
            codec_type = 'truehd'; brand = 'TRUEHD'; variant = ''; scheme = PLAIN
        elif (('eac3' in c or 'dd+' in c) and 'atmos' in c):
            codec_type = 'dd-plus'; brand = 'DD+'; variant = 'ATMOS'; scheme = CYAN
        elif 'eac3' in c or 'dd+' in c:
            codec_type = 'dolby-label'; brand = 'DOLBY'; variant = 'DIGITAL+'; scheme = PLAIN
        elif 'atmos' in c:
            codec_type = 'dolby-label'; brand = 'DOLBY'; variant = 'ATMOS'; scheme = CYAN
        elif 'ac3' in c or ('dolby' in c and 'digital' in c) or c == 'dd':
            codec_type = 'dolby-label'; brand = 'DOLBY'; variant = 'DIGITAL'; scheme = PLAIN
        elif 'dts-x' in c or 'dts:x' in c or 'dtsx' in c:
            codec_type = 'dts-brand'; brand = 'dts'; variant = 'X'; scheme = ORANGE
        elif (('dts' in c and 'hd' in c and 'ma' in c) or 'dts-hd ma' in c
              or 'master audio' in c):
            codec_type = 'dts-hd-ma'; brand = 'dts'; variant = 'MA'; scheme = GREEN
        elif (('dts' in c and 'hra' in c) or 'dts-hd hra' in c
              or 'highres' in c or 'high-res' in c):
            codec_type = 'dts-hd-hra'; brand = 'dts'; variant = 'HRA'; scheme = BLUE
        elif 'dts-hd' in c or 'dts hd' in c:
            codec_type = 'dts-hd'; brand = 'dts'; variant = ''; scheme = PLAIN
        elif 'dts-es' in c or 'dts es' in c:
            codec_type = 'dts-brand'; brand = 'dts'; variant = 'ES'; scheme = PLAIN
        elif 'dts' in c:
            codec_type = 'dts-brand'; brand = 'dts'; variant = ''; scheme = PLAIN
        elif 'flac' in c:
            codec_type = 'flac'; brand = 'FLAC'; variant = ''; scheme = PLAIN
        elif 'pcm' in c:
            codec_type = 'pcm'; brand = 'PCM'; variant = ''; scheme = PLAIN
        elif 'aac' in c:
            codec_type = 'aac'; brand = 'AAC'; variant = ''; scheme = PLAIN
        elif 'mp3' in c:
            codec_type = 'mp3'; brand = 'mp3'; variant = ''; scheme = PLAIN
        elif 'opus' in c:
            codec_type = 'opus'; brand = 'OPUS'; variant = ''; scheme = PLAIN
        else:
            return None  # Not recognised → caller uses generic designed badge

        right_bg_rgba, right_text_rgba = scheme

        # ── Dimensions ────────────────────────────────────────────────────
        R               = max(1, int(badge.get('borderRadius', 8) * s))
        overall_opacity = float(badge.get('opacity', 1.0))
        fluid_mode      = badge.get('audioFluid', True)
        raw_pad         = badge.get('audioPad', 8) if fluid_mode else 10
        pad             = max(2, int(raw_pad * s))
        left_pct        = (badge.get('audioLeftPct', 45)) / 100.0

        raw_h = badge.get('height') or 0
        H = (max(1, int(raw_h * s))
             if (raw_h and str(raw_h).lower() not in ('null', 'none', 'auto', ''))
             else max(20, int(36 * s)))
        slot_h = H - pad * 2

        ch_fs_raw     = float(badge.get('chFontSize', 9))
        ch_fs         = max(4, int(ch_fs_raw * s))
        ch_font_nm    = badge.get('chFont', 'Barlow Condensed')
        ch_font_bold  = badge.get('chFontWeight', 'normal') == 'bold'
        ch_font_italic = badge.get('chFontStyle', 'normal') == 'italic'
        ch_color_hex = badge.get('chColor', '#ffffff')
        ch_opacity   = float(badge.get('chOpacity', 0.28))
        ch_position  = badge.get('chPosition', 'top-right')

        _no_stack_variants = {'X', 'ES'}
        is_vertical = bool(badge.get('verticalStack') and variant
                           and variant not in _no_stack_variants)
        is_dts_hd_type = codec_type in ('dts-hd-ma', 'dts-hd-hra')

        # ── Font loaders (matching JS brandFont / variantFont) ─────────────
        _dts_types = {'dts-brand', 'dts-hd-ma', 'dts-hd-hra', 'dts-hd'}
        _is_dts = codec_type in _dts_types
        _is_mp3 = codec_type == 'mp3'
        _audio_family  = badge.get('audioFont', 'Barlow Condensed')
        _audio_bold    = badge.get('audioFontWeight', 'bold') == 'bold'
        _audio_italic  = badge.get('audioFontStyle', 'normal') == 'italic'
        def _brand_font(sz):
            # DTS/mp3 always use bold; others respect user weight setting
            return self._load_google_font(_audio_family, max(6, sz),
                                          bold=_audio_bold or _is_dts or _is_mp3,
                                          italic=_audio_italic or _is_dts or _is_mp3)
        def _variant_font(sz):
            return self._load_google_font(_audio_family, max(6, sz),
                                          bold=_audio_bold, italic=_audio_italic)
        def _sub_label_font(sz):
            return self._load_google_font(_audio_family, max(4, sz), bold=True)
        def _ch_font(sz):
            return self._load_google_font(ch_font_nm, max(4, sz), bold=ch_font_bold, italic=ch_font_italic)

        _td = ImageDraw.Draw(Image.new('RGBA', (1, 1)))

        # ── Fluid font sizing (mirrors JS _fitFontSize logic) ─────────────
        raw_w = badge.get('width') or 0
        badge_w_fixed = max(20, int(raw_w * s)) if (raw_w and str(raw_w).lower()
                                                     not in ('null', 'none', 'auto', '')) else 0

        if fluid_mode:
            INF = 99999
            if is_dts_hd_type:
                left_slot_h  = H / 2 - pad if (is_vertical and is_dts_hd_type) else slot_h
                right_slot_h = left_slot_h
                _var_sample = ('MASTER AUDIO' if codec_type == 'dts-hd-ma' else 'HIGH-RES AUDIO') \
                    if (is_vertical and is_dts_hd_type) else \
                    ('MASTER' if codec_type == 'dts-hd-ma' else 'HIGH-RES')
                _hd_var_frac = 0.72 if (is_vertical and is_dts_hd_type) else 0.45
                _hd_var_max_w = (max(20, badge_w_fixed * (1 - left_pct) - pad * 2)
                                 if badge_w_fixed else INF)
                brand_fs   = self._fit_font_size_pil('dts', _brand_font, INF,
                                                      left_slot_h * 0.72, hi=120)
                variant_fs = self._fit_font_size_pil(_var_sample, _variant_font,
                                                      _hd_var_max_w,
                                                      right_slot_h * _hd_var_frac, hi=120)
            elif codec_type == 'dolby-label':
                brand_fs   = self._fit_font_size_pil('DOLBY', _brand_font, INF,
                                                      slot_h * 0.30, hi=120)
                _dolby_r_max_w = (max(20, badge_w_fixed * (1 - left_pct) - pad * 2)
                                  if badge_w_fixed else INF)
                variant_fs = self._fit_font_size_pil(variant or 'DIGITAL', _variant_font,
                                                      _dolby_r_max_w, slot_h * 0.72, hi=120)
            elif codec_type in ('truehd-atmos', 'truehd'):
                v_slot_h = H / 2 - pad if is_vertical else slot_h
                brand_fs   = self._fit_font_size_pil('TRUEHD', _brand_font, INF,
                                                      v_slot_h * 0.72, hi=120)
                variant_fs = self._fit_font_size_pil('ATMOS',  _variant_font, INF,
                                                      v_slot_h * 0.72, hi=120)
            elif codec_type == 'dts-brand':
                v_slot_h = H / 2 - pad if is_vertical else slot_h
                _has_variant = bool(variant)
                _dts_brand_max_w = (max(20, (badge_w_fixed * left_pct if _has_variant
                                             else badge_w_fixed) - pad * 2)
                                    if badge_w_fixed else INF)
                _dts_var_max_w   = (max(20, badge_w_fixed * (1 - left_pct) - pad * 2)
                                    if (badge_w_fixed and _has_variant) else INF)
                brand_fs   = self._fit_font_size_pil('dts', _brand_font, _dts_brand_max_w,
                                                      v_slot_h * 0.85, hi=120)
                variant_fs = self._fit_font_size_pil(variant or 'X', _variant_font,
                                                      _dts_var_max_w, v_slot_h * 0.72, hi=120)
            elif codec_type == 'dts-hd':
                # Binary search so "dts -HD" group fits within available width
                _avail_w = max(20, badge_w_fixed - pad * 2) if badge_w_fixed else INF
                if _avail_w == INF:
                    brand_fs = self._fit_font_size_pil('dts', _brand_font, INF,
                                                        slot_h * 0.85, hi=120)
                else:
                    lo2, hi2, best2 = 6, 120, 6
                    while lo2 <= hi2:
                        mid2 = (lo2 + hi2) // 2
                        _bf = _brand_font(mid2)
                        _sf = _sub_label_font(max(4, round(mid2 * 0.45)))
                        bb_dts = _td.textbbox((0, 0), 'dts',  font=_bf)
                        bb_hd  = _td.textbbox((0, 0), '-HD', font=_sf)
                        dw = bb_dts[2] - bb_dts[0]
                        hw = bb_hd[2]  - bb_hd[0]
                        bb_dts_h = bb_dts[3] - bb_dts[1]
                        if dw + 2 + hw <= _avail_w and bb_dts_h <= slot_h * 0.85:
                            best2 = mid2; lo2 = mid2 + 1
                        else:
                            hi2 = mid2 - 1
                    brand_fs = best2
                variant_fs = brand_fs
            elif codec_type == 'dd-plus':
                v_slot_h = H / 2 - pad if is_vertical else slot_h
                brand_fs   = self._fit_font_size_pil('DD+',   _brand_font, INF,
                                                      v_slot_h * 0.72, hi=120)
                variant_fs = self._fit_font_size_pil('ATMOS', _variant_font, INF,
                                                      v_slot_h * 0.72, hi=120)
            else:
                # Single segment: FLAC, AAC, PCM, MP3, OPUS
                _single_max_w = max(20, badge_w_fixed - pad * 2) if badge_w_fixed else INF
                brand_fs   = self._fit_font_size_pil(brand or 'FLAC', _brand_font,
                                                      _single_max_w, slot_h * 0.72, hi=120)
                variant_fs = brand_fs
        else:
            brand_fs   = max(6, int(badge.get('leftFontSize',   14) * s))
            variant_fs = max(6, int(badge.get('rightFontSize1', 15) * s))

        # ── Load fonts at computed sizes ───────────────────────────────────
        lf      = _brand_font(brand_fs)
        rf      = _variant_font(variant_fs)
        small_hd_size = max(4, round(brand_fs * 0.45))
        dolby_size    = max(4, round(brand_fs * 0.5))
        sf      = _sub_label_font(small_hd_size)
        sf_dolby = _sub_label_font(dolby_size)
        chf     = _ch_font(ch_fs)

        # ── Measure text widths ────────────────────────────────────────────
        def _tw(text, font):
            bb = _td.textbbox((0, 0), text, font=font)
            return bb[2] - bb[0]
        def _th(text, font):
            bb = _td.textbbox((0, 0), text, font=font)
            return bb[3] - bb[1], bb[1]  # height, top-offset

        bw_px = 0; hd_label_w = 0; hd_variant_w = 0; vw_px = 0
        if codec_type in ('dts-hd', 'dts-hd-ma', 'dts-hd-hra'):
            bw_px      = _tw('dts', lf)
            hd_label_w = _tw('-HD', sf)
            if codec_type != 'dts-hd':
                v1 = 'MASTER'   if codec_type == 'dts-hd-ma' else 'HIGH-RES'
                v2 = 'AUDIO'
                vw_px = max(_tw(v1, rf), _tw(v2, rf))
                hd_variant_w = vw_px
        elif codec_type == 'dolby-label':
            bw_px = _tw('DOLBY', sf_dolby)
            vw_px = _tw(variant, rf) if variant else 0
        else:
            bw_px = _tw(brand, lf)
            vw_px = _tw(variant, rf) if variant else 0

        bb_c = _td.textbbox((0, 0), channels, font=chf) if channels else None
        ch_reserve = (bb_c[2] - bb_c[0] + max(3, int(6 * s))) if bb_c else 0

        # Icon area for single-segment codecs (AAC, FLAC, PCM, MP3, OPUS)
        # JS: iconAreaW = Math.round(H * (42 / 104)) + 4
        _icon_types = {'aac', 'flac', 'pcm', 'mp3', 'opus'}
        has_icon = codec_type in _icon_types
        icon_area_w = (round(H * (42 / 104)) + 4) if has_icon else 0

        # ── Width calculation (mirrors JS exactly) ─────────────────────────
        if is_vertical and is_dts_hd_type:
            _var_label = 'MASTER AUDIO' if codec_type == 'dts-hd-ma' else 'HIGH-RES AUDIO'
            _var_label_w = _tw(_var_label, rf)
            top_row_w = bw_px + 2 + hd_label_w
            auto_w = max(20, max(top_row_w, _var_label_w) + pad * 2)
            W = max(auto_w, badge_w_fixed) if badge_w_fixed else auto_w
            left_w = right_w = W
        elif is_vertical:
            auto_w = max(20, max(bw_px, vw_px) + pad * 2)
            W = max(auto_w, badge_w_fixed) if badge_w_fixed else auto_w
            left_w = right_w = W
        elif codec_type == 'dts-hd':
            auto_w = max(20, bw_px + hd_label_w + pad * 2 + 4 + ch_reserve)
            W = max(auto_w, badge_w_fixed) if badge_w_fixed else auto_w
            left_w = W; right_w = 0
        elif codec_type in ('dts-hd-ma', 'dts-hd-hra'):
            left_w  = bw_px + hd_label_w + pad * 2 + 4
            right_w = vw_px + pad * 2 + ch_reserve
            auto_w  = max(20, left_w + 1 + right_w)
            W = max(auto_w, badge_w_fixed) if badge_w_fixed else auto_w
        elif codec_type == 'dolby-label':
            left_w  = bw_px + pad * 2
            right_w = (vw_px + pad * 2 + ch_reserve) if variant else ch_reserve
            auto_w  = max(20, left_w + (1 + right_w if variant else 0))
            W = max(auto_w, badge_w_fixed) if badge_w_fixed else auto_w
        else:
            left_w  = bw_px + pad * 2 + icon_area_w
            right_w = (vw_px + pad * 2 + ch_reserve) if variant else ch_reserve
            auto_w  = max(20, left_w + (1 + right_w if variant else 0))
            W = max(auto_w, badge_w_fixed) if badge_w_fixed else auto_w

        # Apply leftPct split when badge has fixed width and is a two-segment horizontal badge
        _is_two_seg_h = (not is_vertical and variant and codec_type != 'dts-hd'
                         and codec_type in ('dts-hd-ma', 'dts-hd-hra', 'dolby-label',
                                            'truehd-atmos', 'dd-plus', 'dts-brand'))
        if fluid_mode and _is_two_seg_h and badge_w_fixed:
            left_w = max(10, round(W * left_pct))

        # ── Build badge image ──────────────────────────────────────────────
        badge_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(badge_img)

        # Base dark background
        bg_op  = float(badge.get('bgOpacity', 0.73))
        bg_col = (0, 0, 0, max(0, min(255, int(bg_op * 255))))
        ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(ov).rounded_rectangle(
            [(0, 0), (W - 1, H - 1)], radius=R, fill=bg_col)
        badge_img = Image.alpha_composite(badge_img, ov)

        # FLAC: green tint overlay
        if codec_type == 'flac':
            flac_tint = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(flac_tint).rounded_rectangle(
                [(0, 0), (W - 1, H - 1)], radius=R,
                fill=(45, 200, 90, int(0.06 * 255)))
            badge_img = Image.alpha_composite(badge_img, flac_tint)

        draw = ImageDraw.Draw(badge_img)

        # ── Helper: draw divider + right tint ─────────────────────────────
        def _draw_divider_and_right_tint(lw):
            d_ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(d_ov).rectangle([(lw, 0), (lw, H - 1)],
                                           fill=(255, 255, 255, 18))
            t_ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(t_ov).rectangle([(lw + 1, 0), (W - 1, H - 1)],
                                           fill=right_bg_rgba)
            return d_ov, t_ov

        # ── Helper: vertically center text in a region ─────────────────────
        def _vcenter_text(text, font, cx, region_y, region_h):
            bb = _td.textbbox((0, 0), text, font=font)
            th_ = bb[3] - bb[1]
            ty_ = region_y + (region_h - th_) // 2 - bb[1]
            return cx, ty_

        # ── Per-codec rendering ────────────────────────────────────────────
        if is_vertical and is_dts_hd_type:
            # DTS-HD MA/HRA vertical: top="dts -HD", bottom=single-line "MASTER AUDIO"/"HIGH-RES AUDIO"
            top_h = round(H / 2)
            bot_h = H - top_h
            var_label = 'MASTER AUDIO' if codec_type == 'dts-hd-ma' else 'HIGH-RES AUDIO'

            # Divider + tint on bottom half
            div_v = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(div_v).rectangle(
                [(0, top_h), (W - 1, top_h)], fill=(255, 255, 255, 18))
            tint_v = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(tint_v).rectangle(
                [(0, top_h + 1), (W - 1, H - 1)], fill=right_bg_rgba)
            badge_img = Image.alpha_composite(badge_img, div_v)
            badge_img = Image.alpha_composite(badge_img, tint_v)
            draw = ImageDraw.Draw(badge_img)

            # Top: "dts" + "-HD" centred as a group
            group_w = bw_px + 2 + hd_label_w
            group_x = (W - group_w) // 2
            _, dts_ty = _vcenter_text('dts', lf, 0, 0, top_h)
            draw.text((group_x, dts_ty), 'dts', font=lf, fill=(255, 255, 255, 230))
            # "-HD" at 65% of topH (slightly below midline like JS y + topH * 0.65)
            hd_y = round(top_h * 0.65)
            bb_hd = _td.textbbox((0, 0), '-HD', font=sf)
            hd_draw_y = hd_y - bb_hd[1] - (bb_hd[3] - bb_hd[1]) // 2
            draw.text((group_x + bw_px + 2, hd_draw_y), '-HD', font=sf,
                      fill=(255, 255, 255, 97))  # 0.38 * 255 ≈ 97

            # Bottom: single-line label centered
            bb_vl = _td.textbbox((0, 0), var_label, font=rf)
            vlw = bb_vl[2] - bb_vl[0]; vlh = bb_vl[3] - bb_vl[1]
            vl_x = (W - vlw) // 2
            vl_y = top_h + 1 + (bot_h - vlh) // 2 - bb_vl[1]
            draw.text((vl_x, vl_y), var_label, font=rf, fill=right_text_rgba)

        elif is_vertical:
            # Vertical stack: brand top half, variant bottom half
            top_h = round(H / 2)
            bot_h = H - top_h

            # Divider + tint
            div_v = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(div_v).rectangle(
                [(0, top_h), (W - 1, top_h)], fill=(255, 255, 255, 18))
            tint_v = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(tint_v).rectangle(
                [(0, top_h + 1), (W - 1, H - 1)], fill=right_bg_rgba)
            badge_img = Image.alpha_composite(badge_img, div_v)
            badge_img = Image.alpha_composite(badge_img, tint_v)
            draw = ImageDraw.Draw(badge_img)

            # Brand (top half)
            bb_b = _td.textbbox((0, 0), brand, font=lf)
            bw2 = bb_b[2] - bb_b[0]; bh2 = bb_b[3] - bb_b[1]
            bx = (W - bw2) // 2
            by = (top_h - bh2) // 2 - bb_b[1]
            draw.text((bx, by), brand, font=lf, fill=(255, 255, 255, 230))

            # Variant (bottom half)
            bb_v2 = _td.textbbox((0, 0), variant, font=rf)
            vw2 = bb_v2[2] - bb_v2[0]; vh2 = bb_v2[3] - bb_v2[1]
            vx2 = (W - vw2) // 2
            vy2 = top_h + 1 + (bot_h - vh2) // 2 - bb_v2[1]
            draw.text((vx2, vy2), variant, font=rf, fill=right_text_rgba)

        elif codec_type == 'dts-hd':
            # Single pill: "dts" italic + "-HD" small dimmed, centred as a group
            group_w = bw_px + 2 + hd_label_w
            group_x = (W - group_w) // 2
            bb_dts = _td.textbbox((0, 0), 'dts', font=lf)
            dts_h = bb_dts[3] - bb_dts[1]
            dts_y = (H - dts_h) // 2 - bb_dts[1]
            draw.text((group_x, dts_y), 'dts', font=lf, fill=(255, 255, 255, 230))
            # "-HD" at 62% of H (JS: y + H * 0.62)
            hd_target_y = round(H * 0.62)
            bb_hd = _td.textbbox((0, 0), '-HD', font=sf)
            hd_h  = bb_hd[3] - bb_hd[1]
            hd_y  = hd_target_y - hd_h // 2 - bb_hd[1]
            draw.text((group_x + bw_px + 2, hd_y), '-HD', font=sf,
                      fill=(255, 255, 255, 97))

        elif codec_type in ('dts-hd-ma', 'dts-hd-hra'):
            # Horizontal: left="dts -HD" right-anchored, right=stacked MASTER/AUDIO or HIGH-RES/AUDIO
            d_ov, t_ov = _draw_divider_and_right_tint(left_w)
            badge_img = Image.alpha_composite(badge_img, d_ov)
            badge_img = Image.alpha_composite(badge_img, t_ov)
            draw = ImageDraw.Draw(badge_img)

            v1 = 'MASTER'   if codec_type == 'dts-hd-ma' else 'HIGH-RES'
            v2 = 'AUDIO'

            # Left: "dts -HD" right-anchored 4px from divider
            group_w = bw_px + 2 + hd_label_w
            group_x = left_w - 4 - group_w
            bb_dts = _td.textbbox((0, 0), 'dts', font=lf)
            dts_h  = bb_dts[3] - bb_dts[1]
            dts_y  = (H - dts_h) // 2 - bb_dts[1]
            draw.text((group_x, dts_y), 'dts', font=lf, fill=(255, 255, 255, 230))
            hd_target_y = round(H * 0.62)
            bb_hd = _td.textbbox((0, 0), '-HD', font=sf)
            hd_h  = bb_hd[3] - bb_hd[1]
            hd_y  = hd_target_y - hd_h // 2 - bb_hd[1]
            draw.text((group_x + bw_px + 2, hd_y), '-HD', font=sf,
                      fill=(255, 255, 255, 97))

            # Right: stacked v1/v2 centred in right segment
            actual_right_w = W - left_w - 1
            r_center_x = left_w + 1 + 4 + (actual_right_w - 4) // 2
            bb_v1 = _td.textbbox((0, 0), v1, font=rf)
            bb_v2 = _td.textbbox((0, 0), v2, font=rf)
            h1 = bb_v1[3] - bb_v1[1]; h2 = bb_v2[3] - bb_v2[1]
            gap = 2
            total_h = h1 + gap + h2
            y_top = (H - total_h) // 2
            # v1 at 65% opacity of right_text, v2 at full
            v1_col = (right_text_rgba[0], right_text_rgba[1], right_text_rgba[2],
                      max(0, min(255, int(right_text_rgba[3] * 0.65))))
            # draw v1
            v1_x = r_center_x - (bb_v1[2] - bb_v1[0]) // 2
            v1_y = y_top - bb_v1[1]
            draw.text((v1_x, v1_y), v1, font=rf, fill=v1_col)
            # draw v2
            v2_x = r_center_x - (bb_v2[2] - bb_v2[0]) // 2
            v2_y = y_top + h1 + gap - bb_v2[1]
            draw.text((v2_x, v2_y), v2, font=rf, fill=right_text_rgba)

        elif codec_type == 'dolby-label':
            # Left: small "DOLBY" sub-label right-anchored 4px from divider
            # Right: large variant
            if variant:
                d_ov, t_ov = _draw_divider_and_right_tint(left_w)
                badge_img = Image.alpha_composite(badge_img, d_ov)
                badge_img = Image.alpha_composite(badge_img, t_ov)
                draw = ImageDraw.Draw(badge_img)

            # "DOLBY" right-anchored at left_w - 4
            bb_dl = _td.textbbox((0, 0), 'DOLBY', font=sf_dolby)
            dl_w = bb_dl[2] - bb_dl[0]; dl_h = bb_dl[3] - bb_dl[1]
            dl_x = left_w - 4 - dl_w
            dl_y = (H - dl_h) // 2 - bb_dl[1]
            draw.text((dl_x, dl_y), 'DOLBY', font=sf_dolby, fill=(255, 255, 255, 115))

            if variant:
                bb_var = _td.textbbox((0, 0), variant, font=rf)
                var_w = bb_var[2] - bb_var[0]; var_h = bb_var[3] - bb_var[1]
                vx_d = left_w + 1 + 5
                vy_d = (H - var_h) // 2 - bb_var[1]
                draw.text((vx_d, vy_d), variant, font=rf, fill=right_text_rgba)

        elif codec_type == 'truehd-atmos':
            # Two segments: TRUEHD right-anchored left, ATMOS left-anchored right
            d_ov, t_ov = _draw_divider_and_right_tint(left_w)
            badge_img = Image.alpha_composite(badge_img, d_ov)
            badge_img = Image.alpha_composite(badge_img, t_ov)
            draw = ImageDraw.Draw(badge_img)

            bb_b2 = _td.textbbox((0, 0), brand, font=lf)
            b_w2 = bb_b2[2] - bb_b2[0]; b_h2 = bb_b2[3] - bb_b2[1]
            bx2 = left_w - 4 - b_w2
            by2 = (H - b_h2) // 2 - bb_b2[1]
            draw.text((bx2, by2), brand, font=lf, fill=(255, 255, 255, 230))

            bb_var = _td.textbbox((0, 0), variant, font=rf)
            var_h = bb_var[3] - bb_var[1]
            vx_ta = left_w + 1 + 5
            vy_ta = (H - var_h) // 2 - bb_var[1]
            draw.text((vx_ta, vy_ta), variant, font=rf, fill=right_text_rgba)

        elif codec_type == 'truehd':
            # Single segment, centred
            bb_b2 = _td.textbbox((0, 0), brand, font=lf)
            b_w2 = bb_b2[2] - bb_b2[0]; b_h2 = bb_b2[3] - bb_b2[1]
            bx2 = (W - b_w2) // 2
            by2 = (H - b_h2) // 2 - bb_b2[1]
            draw.text((bx2, by2), brand, font=lf, fill=(255, 255, 255, 230))

        elif codec_type == 'dd-plus':
            # DD+ right-anchored left, ATMOS left-anchored right
            d_ov, t_ov = _draw_divider_and_right_tint(left_w)
            badge_img = Image.alpha_composite(badge_img, d_ov)
            badge_img = Image.alpha_composite(badge_img, t_ov)
            draw = ImageDraw.Draw(badge_img)

            bb_b2 = _td.textbbox((0, 0), brand, font=lf)
            b_w2 = bb_b2[2] - bb_b2[0]; b_h2 = bb_b2[3] - bb_b2[1]
            bx2 = left_w - 4 - b_w2
            by2 = (H - b_h2) // 2 - bb_b2[1]
            draw.text((bx2, by2), brand, font=lf, fill=(255, 255, 255, 230))

            if variant:
                bb_var = _td.textbbox((0, 0), variant, font=rf)
                var_h = bb_var[3] - bb_var[1]
                vx_dp = left_w + 1 + 5
                vy_dp = (H - var_h) // 2 - bb_var[1]
                draw.text((vx_dp, vy_dp), variant, font=rf, fill=right_text_rgba)

        elif codec_type == 'dts-brand':
            # DTS / DTS-X / DTS-ES: italic brand left-anchored, coloured variant right
            bb_b2 = _td.textbbox((0, 0), brand, font=lf)
            b_w2 = bb_b2[2] - bb_b2[0]; b_h2 = bb_b2[3] - bb_b2[1]
            if variant:
                bx2 = left_w - 4 - b_w2
            else:
                bx2 = (W - b_w2) // 2
            by2 = (H - b_h2) // 2 - bb_b2[1]
            draw.text((bx2, by2), brand, font=lf, fill=(255, 255, 255, 230))

            if variant:
                d_ov, t_ov = _draw_divider_and_right_tint(left_w)
                badge_img = Image.alpha_composite(badge_img, d_ov)
                badge_img = Image.alpha_composite(badge_img, t_ov)
                draw = ImageDraw.Draw(badge_img)
                bb_var = _td.textbbox((0, 0), variant, font=rf)
                var_h = bb_var[3] - bb_var[1]
                vx_dt = left_w + 1 + 5
                vy_dt = (H - var_h) // 2 - bb_var[1]
                draw.text((vx_dt, vy_dt), variant, font=rf, fill=right_text_rgba)

        else:
            # Single-segment: FLAC, PCM, AAC, MP3, OPUS + generic fallback
            # Draw icon (if applicable) in the left icon_area_w slot, then text centred in full W
            if has_icon and icon_area_w > 0:
                icon_cx = pad + icon_area_w // 2
                icon_cy = H // 2
                self._draw_audio_codec_icon_pil(badge_img, codec_type, icon_cx, icon_cy, H)
                draw = ImageDraw.Draw(badge_img)  # re-acquire after icon compositing

            bb_b2 = _td.textbbox((0, 0), brand, font=lf)
            b_w2 = bb_b2[2] - bb_b2[0]; b_h2 = bb_b2[3] - bb_b2[1]
            bx2 = (W - b_w2) // 2
            by2 = (H - b_h2) // 2 - bb_b2[1]
            if codec_type == 'flac':
                _brand_fill = (62, 222, 112, 235)
            else:
                _brand_fill = (255, 255, 255, 230)
            draw.text((bx2, by2), brand, font=lf, fill=_brand_fill)

        # ── Channel count corner ───────────────────────────────────────────
        if channels and bb_c:
            draw = ImageDraw.Draw(badge_img)
            ch_w_px = bb_c[2] - bb_c[0]
            ch_h_px = bb_c[3] - bb_c[1]
            margin_x = max(3, int(5 * s))
            margin_y = max(0, int(3 * s))
            is_right  = 'right'  in ch_position
            is_bottom = 'bottom' in ch_position
            ch_x = (W - ch_w_px - margin_x) if is_right else margin_x
            ch_y = (H - ch_h_px - margin_y) if is_bottom else margin_y
            try:
                r_ch = int(ch_color_hex[1:3], 16)
                g_ch = int(ch_color_hex[3:5], 16)
                b_ch = int(ch_color_hex[5:7], 16)
            except (ValueError, IndexError):
                r_ch, g_ch, b_ch = 255, 255, 255
            a_ch = max(0, min(255, int(ch_opacity * 255)))
            draw.text((ch_x, ch_y), channels, font=chf,
                      fill=(r_ch, g_ch, b_ch, a_ch))

        # ── Border ────────────────────────────────────────────────────────
        if badge.get('borderEnabled', True):
            bw_px_border = max(1, int(badge.get('borderWidth', 1)))
            bc = self._color_with_opacity(
                badge.get('borderColor', '#ffffff'),
                float(badge.get('borderOpacity', 0.08)))
            b_ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            half = bw_px_border // 2
            ImageDraw.Draw(b_ov).rounded_rectangle(
                [(half, half), (W - 1 - half, H - 1 - half)],
                radius=max(0, R - half), outline=bc, width=bw_px_border)
            badge_img = Image.alpha_composite(badge_img, b_ov)

        # ── Top highlight ──────────────────────────────────────────────────
        if badge.get('highlightEnabled', True):
            hl_op  = float(badge.get('highlightOpacity', 0.09))
            hl_img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            hl_pix = hl_img.load()
            hx1 = int(W * 0.12)
            hx2 = int(W * 0.88)
            span = max(1, hx2 - hx1)
            for px in range(hx1, min(hx2, W)):
                t_hl  = (px - hx1) / span
                alpha = int(255 * hl_op * (1 - abs(2 * t_hl - 1)))
                hl_pix[px, 0] = (255, 255, 255, alpha)
            badge_img = Image.alpha_composite(badge_img, hl_img)

        # ── Final clip to rounded shape ────────────────────────────────────
        clip_mask = self._rounded_mask(W, H, R)
        clipped   = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        clipped.paste(badge_img, (0, 0), clip_mask)

        if overall_opacity < 1.0:
            clipped = self._apply_opacity(clipped, overall_opacity)

        return clipped

    # ── Helpers for designed badges ───────────────────────────────────────

    def _color_with_opacity(self, hex_color: str, opacity: float) -> tuple:
        """Return RGBA tuple from #RRGGBB hex + separate opacity float (0-1)."""
        try:
            h = hex_color.lstrip('#')
            r = int(h[0:2], 16)
            g = int(h[2:4], 16)
            b = int(h[4:6], 16)
        except Exception:
            r, g, b = 255, 255, 255
        return (r, g, b, max(0, min(255, int(opacity * 255))))

    def _make_gradient(self, width: int, height: int,
                       c1: tuple, c2: tuple, angle: float) -> Image.Image:
        """Create a w×h RGBA gradient image from c1 to c2 at the given CSS angle.

        Matches the JS _gradientPts / createLinearGradient formula: the centre of
        the image maps to t=0.5 and the gradient spans the full diagonal so corners
        aligned with the gradient direction reach t≈0 and t≈1 respectively.
        """
        import math
        img  = Image.new('RGBA', (width, height))
        pix  = img.load()
        rad  = math.radians(angle - 90)
        dx   = math.cos(rad)
        dy   = math.sin(rad)
        diag = math.sqrt(width * width + height * height) or 1
        for py in range(height):
            for px in range(width):
                nx = px / (width  - 1) if width  > 1 else 0.5
                ny = py / (height - 1) if height > 1 else 0.5
                t  = max(0.0, min(1.0,
                         (width * (nx - 0.5) * dx + height * (ny - 0.5) * dy) / diag + 0.5))
                pix[px, py] = (
                    int(c1[0] + (c2[0] - c1[0]) * t),
                    int(c1[1] + (c2[1] - c1[1]) * t),
                    int(c1[2] + (c2[2] - c1[2]) * t),
                    int(c1[3] + (c2[3] - c1[3]) * t),
                )
        return img

    def _rounded_mask(self, width: int, height: int, radius: int) -> Image.Image:
        """Create an 'L'-mode alpha mask with rounded corners."""
        mask = Image.new('L', (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [(0, 0), (width - 1, height - 1)], radius=radius, fill=255)
        return mask

    def _load_google_font(self, family: str, size: int, bold: bool = False, italic: bool = False):
        """Load a font via font_manager (Google Fonts + local fallbacks)."""
        try:
            from overlays.font_manager import get_pil_font
            return get_pil_font(family, size, bold, italic)
        except Exception as e:
            self.logger.debug(f"font_manager unavailable: {e}")
            return self._load_font('DejaVuSans-Bold' if bold else 'DejaVuSans', size)

    def save_rendered_image(self, image: Image.Image, output_path: Union[str, Path],
                           format: str = 'JPEG', quality: int = 95) -> None:
        """
        Save rendered image to file.

        Args:
            image: PIL Image object
            output_path: Output file path
            format: Image format (JPEG, PNG, etc.)
            quality: JPEG quality (1-100)
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Convert RGBA to RGB for JPEG
        if format.upper() == 'JPEG' and image.mode == 'RGBA':
            rgb_image = Image.new('RGB', image.size, (255, 255, 255))
            rgb_image.paste(image, mask=image.split()[3])  # Use alpha channel as mask
            image = rgb_image

        image.save(output_path, format=format, quality=quality)
        self.logger.info(f"Saved rendered overlay to {output_path}")

    def image_to_bytes(self, image: Image.Image, format: str = 'JPEG',
                       quality: int = 95) -> bytes:
        """
        Convert PIL Image to bytes for uploading to Plex.

        Args:
            image: PIL Image object
            format: Image format
            quality: JPEG quality

        Returns:
            Image bytes
        """
        buffer = BytesIO()

        # Convert RGBA to RGB for JPEG
        if format.upper() == 'JPEG' and image.mode == 'RGBA':
            rgb_image = Image.new('RGB', image.size, (255, 255, 255))
            rgb_image.paste(image, mask=image.split()[3])
            image = rgb_image

        image.save(buffer, format=format, quality=quality)
        return buffer.getvalue()
