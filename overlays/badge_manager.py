"""
Badge Manager

Manages the badge library: types, variations, and asset PNG files.

Badge types define what metadata field drives badge selection.
Variations are the individual PNG assets for each possible metadata value.
Composite variations combine two metadata fields into one badge PNG.
"""

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_db_connection():
    from database.core import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    return conn


BADGE_ASSET_DIR_DEFAULT = '/user/config/badge_assets'

# ---------------------------------------------------------------------------
# Pre-seeded badge type definitions
# ---------------------------------------------------------------------------

BADGE_TYPE_SEEDS = [
    {
        'slug': 'audio_codec',
        'display_name': 'Audio Codec',
        'category': 'audio',
        'metadata_fields': ['ms_audio_codec'],
        'is_composite': False,
        'sort_order': 1,
        'variations': [
            ('dolby_digital',       'Dolby Digital'),
            ('dolby_digital_plus',  'Dolby Digital+'),
            ('atmos',               'Dolby Atmos'),
            ('atmos_standalone',    'Atmos Standalone'),
            ('dd_plus_atmos',       'DD+ Atmos'),
            ('truehd',              'TrueHD'),
            ('truehd_atmos',        'TrueHD Atmos'),
            ('dts',                 'DTS'),
            ('dts_es',              'DTS-ES'),
            ('dts_x',               'DTS:X'),
            ('dts_hd',              'DTS-HD'),
            ('dts_hd_ma',           'DTS-HD MA'),
            ('dts_hd_hra',          'DTS-HD HRA'),
            ('flac',                'FLAC'),
            ('pcm',                 'PCM'),
            ('aac',                 'AAC'),
            ('mp3',                 'MP3'),
            ('opus',                'Opus'),
        ],
    },
    {
        'slug': 'audio_channels',
        'display_name': 'Audio Channels',
        'category': 'audio',
        'metadata_fields': ['ms_audio_channels'],
        'is_composite': False,
        'sort_order': 2,
        'variations': [
            ('1.0',   'Mono (1.0)'),
            ('2.0',   'Stereo (2.0)'),
            ('5.1',   '5.1 Surround'),
            ('7.1',   '7.1 Surround'),
            ('other', 'Other'),
        ],
    },
    {
        'slug': 'audio_combo',
        'display_name': 'Audio Codec + Channels',
        'category': 'audio',
        'metadata_fields': ['ms_audio_codec', 'ms_audio_channels'],
        'is_composite': True,
        'sort_order': 3,
        'variations': [
            # TrueHD Atmos
            ('truehd_atmos|7.1',        'TrueHD Atmos 7.1'),
            ('truehd_atmos|5.1',        'TrueHD Atmos 5.1'),
            ('truehd_atmos|2.0',        'TrueHD Atmos 2.0'),
            ('truehd_atmos|1.0',        'TrueHD Atmos 1.0'),
            # TrueHD
            ('truehd|7.1',              'TrueHD 7.1'),
            ('truehd|5.1',              'TrueHD 5.1'),
            ('truehd|2.0',              'TrueHD 2.0'),
            ('truehd|1.0',              'TrueHD 1.0'),
            # DTS:X
            ('dts_x|7.1',               'DTS:X 7.1'),
            ('dts_x|5.1',               'DTS:X 5.1'),
            ('dts_x|2.0',               'DTS:X 2.0'),
            ('dts_x|1.0',               'DTS:X 1.0'),
            # DTS-HD MA
            ('dts_hd_ma|7.1',           'DTS-HD MA 7.1'),
            ('dts_hd_ma|5.1',           'DTS-HD MA 5.1'),
            ('dts_hd_ma|2.0',           'DTS-HD MA 2.0'),
            ('dts_hd_ma|1.0',           'DTS-HD MA 1.0'),
            # DTS-HD HRA
            ('dts_hd_hra|7.1',          'DTS-HD HRA 7.1'),
            ('dts_hd_hra|5.1',          'DTS-HD HRA 5.1'),
            ('dts_hd_hra|2.0',          'DTS-HD HRA 2.0'),
            ('dts_hd_hra|1.0',          'DTS-HD HRA 1.0'),
            # DTS-HD
            ('dts_hd|7.1',              'DTS-HD 7.1'),
            ('dts_hd|5.1',              'DTS-HD 5.1'),
            ('dts_hd|2.0',              'DTS-HD 2.0'),
            ('dts_hd|1.0',              'DTS-HD 1.0'),
            # DTS-ES
            ('dts_es|7.1',              'DTS-ES 7.1'),
            ('dts_es|5.1',              'DTS-ES 5.1'),
            ('dts_es|2.0',              'DTS-ES 2.0'),
            ('dts_es|1.0',              'DTS-ES 1.0'),
            # DTS
            ('dts|7.1',                 'DTS 7.1'),
            ('dts|5.1',                 'DTS 5.1'),
            ('dts|2.0',                 'DTS 2.0'),
            ('dts|1.0',                 'DTS 1.0'),
            # Dolby Atmos
            ('atmos|7.1',               'Atmos 7.1'),
            ('atmos|5.1',               'Atmos 5.1'),
            ('atmos|2.0',               'Atmos 2.0'),
            ('atmos|1.0',               'Atmos 1.0'),
            # Atmos Standalone
            ('atmos_standalone|7.1',    'Atmos Standalone 7.1'),
            ('atmos_standalone|5.1',    'Atmos Standalone 5.1'),
            ('atmos_standalone|2.0',    'Atmos Standalone 2.0'),
            ('atmos_standalone|1.0',    'Atmos Standalone 1.0'),
            # DD+ Atmos
            ('dd_plus_atmos|7.1',       'DD+ Atmos 7.1'),
            ('dd_plus_atmos|5.1',       'DD+ Atmos 5.1'),
            ('dd_plus_atmos|2.0',       'DD+ Atmos 2.0'),
            ('dd_plus_atmos|1.0',       'DD+ Atmos 1.0'),
            # Dolby Digital+
            ('dolby_digital_plus|7.1',  'Dolby Digital+ 7.1'),
            ('dolby_digital_plus|5.1',  'Dolby Digital+ 5.1'),
            ('dolby_digital_plus|2.0',  'Dolby Digital+ 2.0'),
            ('dolby_digital_plus|1.0',  'Dolby Digital+ 1.0'),
            # Dolby Digital
            ('dolby_digital|7.1',       'Dolby Digital 7.1'),
            ('dolby_digital|5.1',       'Dolby Digital 5.1'),
            ('dolby_digital|2.0',       'Dolby Digital 2.0'),
            ('dolby_digital|1.0',       'Dolby Digital 1.0'),
            # AAC
            ('aac|7.1',                 'AAC 7.1'),
            ('aac|5.1',                 'AAC 5.1'),
            ('aac|2.0',                 'AAC 2.0'),
            ('aac|1.0',                 'AAC 1.0'),
            # FLAC
            ('flac|7.1',                'FLAC 7.1'),
            ('flac|5.1',                'FLAC 5.1'),
            ('flac|2.0',                'FLAC 2.0'),
            ('flac|1.0',                'FLAC 1.0'),
            # PCM
            ('pcm|7.1',                 'PCM 7.1'),
            ('pcm|5.1',                 'PCM 5.1'),
            ('pcm|2.0',                 'PCM 2.0'),
            ('pcm|1.0',                 'PCM 1.0'),
            # MP3
            ('mp3|7.1',                 'MP3 7.1'),
            ('mp3|5.1',                 'MP3 5.1'),
            ('mp3|2.0',                 'MP3 2.0'),
            ('mp3|1.0',                 'MP3 1.0'),
            # Opus
            ('opus|7.1',                'Opus 7.1'),
            ('opus|5.1',                'Opus 5.1'),
            ('opus|2.0',                'Opus 2.0'),
            ('opus|1.0',                'Opus 1.0'),
        ],
    },
    {
        'slug': 'resolution',
        'display_name': 'Video Resolution',
        'category': 'video',
        'metadata_fields': ['ms_resolution'],
        'is_composite': False,
        'sort_order': 4,
        'variations': [
            ('360p',  '360p'),
            ('480p',  '480p'),
            ('576p',  '576p'),
            ('720p',  '720p'),
            ('1080i', '1080i'),
            ('1080p', '1080p'),
            ('2k',    '2K'),
            ('4k',    '4K'),
        ],
    },
    {
        'slug': 'hdr',
        'display_name': 'HDR Format',
        'category': 'video',
        'metadata_fields': ['ms_hdr', 'ms_dolby_vision'],
        'is_composite': False,
        'sort_order': 5,
        'variations': [
            ('hdr',                   'HDR'),
            ('hdr10plus',             'HDR10+'),
            ('dolby_vision',          'Dolby Vision'),
            ('dolby_vision_hdr',      'DV + HDR'),
            ('dolby_vision_hdr10plus','DV + HDR10+'),
        ],
    },
    {
        'slug': 'resolution_hdr',
        'display_name': 'Resolution + HDR (Combined)',
        'category': 'video',
        'metadata_fields': ['ms_resolution', 'ms_hdr', 'ms_dolby_vision'],
        'is_composite': True,
        'sort_order': 6,
        'variations': [
            ('480p|plain',          '480p'),
            ('480p|hdr',            '480p + HDR'),
            ('480p|hdr10plus',      '480p + HDR10+'),
            ('480p|dv',             '480p + DV'),
            ('480p|dv_hdr',         '480p + DV + HDR'),
            ('480p|dv_hdr10plus',   '480p + DV + HDR10+'),
            ('576p|plain',          '576p'),
            ('576p|hdr',            '576p + HDR'),
            ('576p|hdr10plus',      '576p + HDR10+'),
            ('576p|dv',             '576p + DV'),
            ('576p|dv_hdr',         '576p + DV + HDR'),
            ('576p|dv_hdr10plus',   '576p + DV + HDR10+'),
            ('720p|plain',          '720p'),
            ('720p|hdr',            '720p + HDR'),
            ('720p|hdr10plus',      '720p + HDR10+'),
            ('720p|dv',             '720p + DV'),
            ('720p|dv_hdr',         '720p + DV + HDR'),
            ('720p|dv_hdr10plus',   '720p + DV + HDR10+'),
            ('1080i|plain',         '1080i'),
            ('1080i|hdr',           '1080i + HDR'),
            ('1080p|plain',         '1080p'),
            ('1080p|hdr',           '1080p + HDR'),
            ('1080p|hdr10plus',     '1080p + HDR10+'),
            ('1080p|dv',            '1080p + DV'),
            ('1080p|dv_hdr',        '1080p + DV + HDR'),
            ('1080p|dv_hdr10plus',  '1080p + DV + HDR10+'),
            ('2k|plain',            '2K'),
            ('2k|hdr',              '2K + HDR'),
            ('2k|dv',               '2K + DV'),
            ('4k|plain',            '4K'),
            ('4k|hdr',              '4K + HDR'),
            ('4k|hdr10plus',        '4K + HDR10+'),
            ('4k|dv',               '4K + DV'),
            ('4k|dv_hdr',           '4K + DV + HDR'),
            ('4k|dv_hdr10plus',     '4K + DV + HDR10+'),
        ],
    },
]

# ---------------------------------------------------------------------------
# Normalization maps: raw Plex values → variation keys
# ---------------------------------------------------------------------------

AUDIO_CODEC_MAP = {
    'ac3':                    'dolby_digital',
    'dolby digital':          'dolby_digital',
    'dd':                     'dolby_digital',
    'eac3':                   'dolby_digital_plus',
    'dolby digital plus':     'dolby_digital_plus',
    'dolby digital+':         'dolby_digital_plus',
    'dd+':                    'dolby_digital_plus',
    'ec3 atmos':              'dd_plus_atmos',
    'eac3 atmos':             'dd_plus_atmos',
    'atmos':                  'atmos',
    'dolby atmos':            'atmos',
    'truehd':                 'truehd',
    'truehd atmos':           'truehd_atmos',
    'mlp fba':                'truehd_atmos',
    'dts':                    'dts',
    'dts-es':                 'dts_es',
    'dts es':                 'dts_es',
    'dts-x':                  'dts_x',
    'dtsx':                   'dts_x',
    'dts:x':                  'dts_x',
    'dts-hd ma':              'dts_hd_ma',
    'dts-hd master audio':    'dts_hd_ma',
    'dts-hd hra':             'dts_hd_hra',
    'dts-hd high-res audio':  'dts_hd_hra',
    'dts-hd':                 'dts_hd',
    'flac':                   'flac',
    'pcm':                    'pcm',
    'lpcm':                   'pcm',
    'aac':                    'aac',
    'mp3':                    'mp3',
    'mpeg audio':             'mp3',
    'opus':                   'opus',
}

RESOLUTION_MAP = {
    '360':    '360p',  '360p':  '360p',
    '480':    '480p',  '480p':  '480p',  'sd': '480p',
    '576':    '576p',  '576p':  '576p',
    '720':    '720p',  '720p':  '720p',  'hd': '720p',
    '1080i':  '1080i',
    '1080':   '1080p', '1080p': '1080p', 'fhd': '1080p',
    '1440':   '2k',    '1440p': '2k',
    '2k':     '2k',
    '2160':   '4k',    '2160p': '4k',   '4k': '4k',  'uhd': '4k',
}


# ---------------------------------------------------------------------------
# BadgeManager
# ---------------------------------------------------------------------------

class BadgeManager:
    """Manages badge types, variations, and asset PNG files.

    Two-tier asset lookup:
      1. User uploads  → self.user_asset_dir  (/user/config/badge_assets/)
      2. System default → self.system_asset_dir (overlays/assets/  — shipped with app)
    """

    # System defaults live next to this file: overlays/assets/{slug}/{key}.png
    SYSTEM_ASSET_DIR = Path(__file__).parent / 'assets'

    def __init__(self, db_path: str = None, asset_dir: str = None):
        # db_path is unused — kept for API compatibility. DB access uses get_db_connection().
        # user_asset_dir: where uploads land (runtime, outside app)
        self.user_asset_dir = Path(asset_dir or BADGE_ASSET_DIR_DEFAULT)
        self.user_asset_dir.mkdir(parents=True, exist_ok=True)
        # backwards-compat alias used by save_variation_asset
        self.asset_dir = self.user_asset_dir
        # system_asset_dir: shipped with the app in overlays/assets/
        self.system_asset_dir = self.SYSTEM_ASSET_DIR

    def _get_conn(self) -> sqlite3.Connection:
        return _get_db_connection()

    # ── Seeding ───────────────────────────────────────────────────────────

    def seed_badge_types(self) -> None:
        """Pre-populate badge_types and badge_variations with defaults if not yet seeded."""
        conn = self._get_conn()
        try:
            for seed in BADGE_TYPE_SEEDS:
                conn.execute('''
                    INSERT OR IGNORE INTO badge_types
                        (slug, display_name, category, metadata_fields, is_composite, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    seed['slug'],
                    seed['display_name'],
                    seed['category'],
                    json.dumps(seed['metadata_fields']),
                    1 if seed['is_composite'] else 0,
                    seed['sort_order'],
                ))
                type_row = conn.execute(
                    'SELECT id FROM badge_types WHERE slug = ?', (seed['slug'],)
                ).fetchone()
                type_id = type_row['id']
                for var_key, var_name in seed['variations']:
                    conn.execute('''
                        INSERT OR IGNORE INTO badge_variations
                            (badge_type_id, variation_key, display_name, is_default)
                        VALUES (?, ?, ?, 1)
                    ''', (type_id, var_key, var_name))
            conn.commit()
            logger.info("Badge types seeded successfully")
        except Exception as e:
            logger.error(f"Failed to seed badge types: {e}", exc_info=True)
            conn.rollback()
        finally:
            conn.close()

    # ── Badge Types ───────────────────────────────────────────────────────

    def get_badge_types(self, category: str = None) -> List[Dict]:
        """List all badge types, optionally filtered by category."""
        conn = self._get_conn()
        try:
            if category:
                rows = conn.execute(
                    'SELECT * FROM badge_types WHERE category = ? ORDER BY sort_order, display_name',
                    (category,)
                ).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM badge_types ORDER BY sort_order, display_name'
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d['metadata_fields'] = json.loads(d.get('metadata_fields') or '[]')
                result.append(d)
            return result
        finally:
            conn.close()

    def get_badge_type(self, slug: str) -> Optional[Dict]:
        """Get a single badge type by slug."""
        conn = self._get_conn()
        try:
            row = conn.execute('SELECT * FROM badge_types WHERE slug = ?', (slug,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['metadata_fields'] = json.loads(d.get('metadata_fields') or '[]')
            return d
        finally:
            conn.close()

    # ── Badge Variations ──────────────────────────────────────────────────

    def get_variations(self, badge_type_slug: str) -> List[Dict]:
        """Get all variations for a badge type, with has_asset and has_system_asset flags."""
        badge_type = self.get_badge_type(badge_type_slug)
        if not badge_type:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                'SELECT * FROM badge_variations WHERE badge_type_id = ? ORDER BY variation_key',
                (badge_type['id'],)
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d['has_asset'] = bool(d.get('asset_path') and Path(d['asset_path']).exists())
                safe_key = re.sub(r'[^\w.\-]', '_', d['variation_key'])
                d['has_system_asset'] = (self.system_asset_dir / badge_type_slug / f"{safe_key}.png").exists()
                result.append(d)
            return result
        finally:
            conn.close()

    def get_variation_by_id(self, variation_id: int) -> Optional[Dict]:
        """Get a variation by its DB id."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                '''SELECT bv.*, bt.slug
                   FROM badge_variations bv
                   JOIN badge_types bt ON bt.id = bv.badge_type_id
                   WHERE bv.id = ?''',
                (variation_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d['has_asset'] = bool(d.get('asset_path') and Path(d['asset_path']).exists())
            safe_key = re.sub(r'[^\w.\-]', '_', d['variation_key'])
            d['has_system_asset'] = (self.system_asset_dir / d['slug'] / f"{safe_key}.png").exists()
            return d
        finally:
            conn.close()

    def get_variation_by_key(self, badge_type_slug: str, variation_key: str) -> Optional[Dict]:
        """Get a specific variation by type slug and variation key."""
        badge_type = self.get_badge_type(badge_type_slug)
        if not badge_type:
            return None
        conn = self._get_conn()
        try:
            row = conn.execute(
                'SELECT * FROM badge_variations WHERE badge_type_id = ? AND variation_key = ?',
                (badge_type['id'], variation_key)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d['has_asset'] = bool(d.get('asset_path') and Path(d['asset_path']).exists())
            safe_key = re.sub(r'[^\w.\-]', '_', d['variation_key'])
            d['has_system_asset'] = (self.system_asset_dir / badge_type_slug / f"{safe_key}.png").exists()
            return d
        finally:
            conn.close()

    def add_variation(self, badge_type_id: int, variation_key: str, display_name: str) -> Optional[int]:
        """Add a new custom variation slot (no asset yet). Returns new id or None on conflict."""
        conn = self._get_conn()
        try:
            cursor = conn.execute('''
                INSERT INTO badge_variations (badge_type_id, variation_key, display_name, is_default)
                VALUES (?, ?, ?, 0)
            ''', (badge_type_id, variation_key, display_name))
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.warning(f"Variation key already exists: {variation_key}")
            return None
        finally:
            conn.close()

    def save_variation_asset(self, variation_id: int, file_bytes: bytes, filename: str) -> bool:
        """Save uploaded PNG/image for a variation. Returns True on success."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                '''SELECT bv.*, bt.slug
                   FROM badge_variations bv
                   JOIN badge_types bt ON bt.id = bv.badge_type_id
                   WHERE bv.id = ?''',
                (variation_id,)
            ).fetchone()
            if not row:
                return False
            type_dir = self.asset_dir / row['slug']
            type_dir.mkdir(parents=True, exist_ok=True)
            safe_key = re.sub(r'[^\w.\-]', '_', row['variation_key'])
            ext = Path(filename).suffix.lower() if Path(filename).suffix else '.png'
            asset_path = type_dir / f"{safe_key}{ext}"
            asset_path.write_bytes(file_bytes)
            conn.execute(
                'UPDATE badge_variations SET asset_path = ? WHERE id = ?',
                (str(asset_path), variation_id)
            )
            conn.commit()
            logger.info(f"Saved badge asset: {asset_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save variation asset: {e}", exc_info=True)
            conn.rollback()
            return False
        finally:
            conn.close()

    def delete_variation_asset(self, variation_id: int) -> bool:
        """Remove the asset file for a variation (keeps the variation slot)."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                'SELECT asset_path FROM badge_variations WHERE id = ?', (variation_id,)
            ).fetchone()
            if row and row['asset_path']:
                p = Path(row['asset_path'])
                if p.exists():
                    p.unlink()
            conn.execute('UPDATE badge_variations SET asset_path = NULL WHERE id = ?', (variation_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to delete variation asset: {e}", exc_info=True)
            return False
        finally:
            conn.close()

    def delete_variation(self, variation_id: int) -> bool:
        """Delete a custom variation entirely (default variations cannot be deleted)."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                'SELECT * FROM badge_variations WHERE id = ?', (variation_id,)
            ).fetchone()
            if not row:
                return False
            if row['is_default']:
                logger.warning("Cannot delete a default variation slot")
                return False
            if row['asset_path']:
                p = Path(row['asset_path'])
                if p.exists():
                    p.unlink()
            conn.execute('DELETE FROM badge_variations WHERE id = ?', (variation_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to delete variation: {e}", exc_info=True)
            return False
        finally:
            conn.close()

    # ── Normalization: raw Plex values → variation keys ───────────────────

    def normalize_audio_codec_key(self, raw_codec: str) -> str:
        """Map a raw ms_audio_codec string to a variation key."""
        if not raw_codec:
            return ''
        normalized = raw_codec.strip().lower()
        return AUDIO_CODEC_MAP.get(normalized, re.sub(r'[\s\-:]+', '_', normalized))

    def normalize_resolution_key(self, raw_res: str) -> str:
        """Map a raw ms_resolution string to a variation key."""
        if not raw_res:
            return ''
        return RESOLUTION_MAP.get(raw_res.strip().lower(), raw_res.strip().lower())

    def normalize_hdr_key(self, ms_hdr, ms_dolby_vision, ms_hdr_format: str = '') -> str:
        """Derive HDR variation key from ms_hdr, ms_dolby_vision and ms_hdr_format."""
        has_dv  = bool(ms_dolby_vision)
        has_hdr = bool(ms_hdr)
        if has_dv and has_hdr:
            return 'dolby_vision_hdr'
        if has_dv:
            return 'dolby_vision'
        if has_hdr:
            # Use the stored HDR subtype to return a more specific key when available
            hdr_fmt = (ms_hdr_format or '').strip()
            if hdr_fmt == 'HDR10+':
                return 'hdr10plus'
            if hdr_fmt == 'HDR10':
                return 'hdr10'
            if hdr_fmt == 'HLG':
                return 'hlg'
            return 'hdr'
        return ''

    def normalize_channel_key(self, raw_channels) -> str:
        """Map channel count (int or string) to a variation key."""
        if raw_channels is None:
            return ''
        s = str(raw_channels).strip()
        try:
            n = int(float(s))
            if n == 1:  return '1.0'
            if n == 2:  return '2.0'
            if n == 6:  return '5.1'
            if n == 8:  return '7.1'
        except ValueError:
            pass
        if s in ('1.0', '2.0', '5.1', '7.1'):
            return s
        return 'other'

    # ── Asset resolution for rendering ────────────────────────────────────

    def get_variation_asset_for_media(self, badge_type_slug: str, media_item: Dict) -> Optional[str]:
        """
        Given a badge type slug and a media item dict (from DB), return the
        asset_path of the matching variation PNG, or None if no asset exists.

        Keys used from media_item:
          ms_audio_codec, ms_audio_channels,
          ms_resolution, ms_hdr, ms_dolby_vision
        """
        badge_type = self.get_badge_type(badge_type_slug)
        if not badge_type:
            return None

        variation_key = None

        if badge_type_slug == 'audio_codec':
            variation_key = self.normalize_audio_codec_key(
                media_item.get('ms_audio_codec', '')
            )
        elif badge_type_slug == 'audio_channels':
            variation_key = self.normalize_channel_key(
                media_item.get('ms_audio_channels')
            )
        elif badge_type_slug == 'audio_combo':
            codec_key = self.normalize_audio_codec_key(
                media_item.get('ms_audio_codec', '')
            )
            chan_key = self.normalize_channel_key(
                media_item.get('ms_audio_channels')
            )
            if codec_key and chan_key:
                variation_key = f"{codec_key}|{chan_key}"
        elif badge_type_slug == 'resolution':
            variation_key = self.normalize_resolution_key(
                media_item.get('ms_resolution', '')
            )
        elif badge_type_slug == 'hdr':
            variation_key = self.normalize_hdr_key(
                media_item.get('ms_hdr', 0),
                media_item.get('ms_dolby_vision', 0),
                media_item.get('ms_hdr_format', ''),
            )
        elif badge_type_slug == 'resolution_hdr':
            res_key = self.normalize_resolution_key(
                media_item.get('ms_resolution', '')
            )
            has_dv  = bool(media_item.get('ms_dolby_vision', 0))
            has_hdr = bool(media_item.get('ms_hdr', 0))
            # Build HDR suffix using abbreviated names matching the PNG filenames:
            # 4k_dv.png, 4k_dv_hdr.png, 4k_hdr.png, 4k_plain.png, etc.
            if has_dv and has_hdr:
                hdr_suffix = 'dv_hdr'
            elif has_dv:
                hdr_suffix = 'dv'
            elif has_hdr:
                hdr_suffix = 'hdr'
            else:
                hdr_suffix = 'plain'
            if res_key:
                variation_key = f"{res_key}_{hdr_suffix}"

        if not variation_key:
            return None

        safe_key = re.sub(r'[^\w.\-]', '_', variation_key)

        # Tier 1: user upload — check directly in user_asset_dir (no DB path lookup)
        for ext in ('.png', '.jpg', '.jpeg', '.webp'):
            user_path = self.user_asset_dir / badge_type_slug / f"{safe_key}{ext}"
            if user_path.exists():
                return str(user_path)

        # Tier 2: system default shipped with the app
        system_path = self.system_asset_dir / badge_type_slug / f"{safe_key}.png"
        if system_path.exists():
            return str(system_path)

        return None

    def get_display_asset_for_variation(self, variation_id: int) -> Optional[str]:
        """
        Return the best available asset path for a variation, for UI display.
        Checks user upload first, then system default.
        """
        variation = self.get_variation_by_id(variation_id)
        if not variation:
            return None
        # Tier 1: user upload
        if variation.get('has_asset'):
            return variation['asset_path']
        # Tier 2: system default
        safe_key = re.sub(r'[^\w.\-]', '_', variation['variation_key'])
        system_path = self.system_asset_dir / variation['slug'] / f"{safe_key}.png"
        if system_path.exists():
            return str(system_path)
        return None

    # ── Stats ─────────────────────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Return badge library statistics."""
        conn = self._get_conn()
        try:
            total_types      = conn.execute('SELECT COUNT(*) FROM badge_types').fetchone()[0]
            total_variations = conn.execute('SELECT COUNT(*) FROM badge_variations').fetchone()[0]
            filled           = conn.execute(
                'SELECT COUNT(*) FROM badge_variations WHERE asset_path IS NOT NULL'
            ).fetchone()[0]
            # Count system default PNGs on disk
            system_assets = sum(
                len(list(d.glob('*.png')))
                for d in self.system_asset_dir.iterdir()
                if d.is_dir()
            ) if self.system_asset_dir.exists() else 0
            return {
                'total_types':       total_types,
                'total_variations':  total_variations,
                'filled_variations': filled,
                'empty_variations':  total_variations - filled,
                'system_assets':     system_assets,
            }
        finally:
            conn.close()
