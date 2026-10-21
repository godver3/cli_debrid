"""
Media Info Extractor

Extracts media information from Plex metadata for overlay generation.
Based on patterns from original plexmeta.py and plexmeta2.py scripts.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional


class MediaInfoExtractor:
    """
    Extracts media technical information from Plex metadata.

    Parses Plex API responses to extract:
    - Resolution (4K, 1080p, 720p, etc.)
    - HDR format (HDR10, Dolby Vision, HDR10+, etc.)
    - Audio codec (TrueHD Atmos, DTS:X, etc.)
    - Video codec (HEVC, AVC, etc.)
    - Container format (MKV, MP4, etc.)
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def extract_from_plex_metadata(self, metadata: Dict[str, Any],
                                   plex_client=None,
                                   rating_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Extract media info from Plex metadata dictionary.

        Args:
            metadata: Plex metadata dictionary (from JSON or XML)
            plex_client: Optional PlexClient instance for additional queries
            rating_key: Optional rating key for fetching detailed stream info

        Returns:
            Dictionary with extracted media information:
            {
                'resolution': '2160p',
                'hdr': True,
                'dolby_vision': False,
                'hdr_format': 'HDR10',
                'audio_codec': 'TrueHD Atmos',
                'audio_channels': '7.1',
                'video_codec': 'HEVC',
                'container': 'mkv',
                'bitrate': 50000
            }
        """
        info = {
            'resolution': None,
            'hdr': False,
            'dolby_vision': False,
            'hdr_format': None,
            'audio_codec': None,
            'audio_channels': None,
            'video_codec': None,
            'container': None,
            'bitrate': None,
            'network': None,
            'studio': None,
            'content_rating': None,
        }

        try:
            # Extract resolution
            info['resolution'] = self._extract_resolution(metadata)

            # Extract video codec and HDR info
            video_info = self._extract_video_info(metadata)
            info.update(video_info)

            # Extract audio info
            audio_info = self._extract_audio_info(metadata)
            info.update(audio_info)

            # Extract container and bitrate
            media_elem = metadata.get('Media', [])
            if media_elem and len(media_elem) > 0:
                media = media_elem[0]
                info['container'] = media.get('container', '').lower()
                info['bitrate'] = media.get('bitrate')

            # Extract network, studio, content rating from top-level metadata fields.
            # Plex returns these directly on the metadata object.
            # 'network' appears on TV show items; 'studio' on movies; 'contentRating' on both.
            network = metadata.get('network') or metadata.get('Network') or ''
            info['network'] = network.strip() or None

            studio = metadata.get('studio') or metadata.get('Studio') or ''
            info['studio'] = studio.strip() or None

            content_rating = metadata.get('contentRating') or metadata.get('content_rating') or ''
            info['content_rating'] = content_rating.strip() or None

            self.logger.info(f"Extracted media info: {info['resolution']} {info.get('hdr_format', '')} "
                           f"{info.get('video_codec', '')} {info.get('audio_codec', '')} "
                           f"network={info.get('network')} studio={info.get('studio')} "
                           f"rating={info.get('content_rating')}")

        except Exception as e:
            self.logger.error(f"Failed to extract media info: {e}", exc_info=True)

        return info

    def _extract_resolution(self, metadata: Dict[str, Any]) -> Optional[str]:
        """
        Extract video resolution from metadata.

        Follows original plexmeta.py pattern (lines 463-482):
        - Get videoResolution field
        - Convert numeric values to "XXXXp" format
        - Check filename for interlaced (1080i)

        Args:
            metadata: Plex metadata dictionary

        Returns:
            Resolution string (e.g., "2160p", "1080p", "720p") or None
        """
        try:
            # Try to get from Media element
            media_list = metadata.get('Media', [])
            if media_list and len(media_list) > 0:
                video_resolution = media_list[0].get('videoResolution')

                if video_resolution:
                    # Convert numeric resolution to "XXXXp" format
                    # e.g., "2160" -> "2160p"
                    resolution = re.sub(r'^(\d{3,4})$', r'\1p', str(video_resolution))

                    # Check for interlaced (1080i)
                    title = metadata.get('title', '')
                    file_path = metadata.get('file', '')
                    if '1080i' in title.lower() or '1080i' in file_path.lower():
                        resolution = '1080i'

                    return resolution

            # Fallback: try direct videoResolution field
            video_resolution = metadata.get('videoResolution')
            if video_resolution:
                return re.sub(r'^(\d{3,4})$', r'\1p', str(video_resolution))

        except Exception as e:
            self.logger.error(f"Failed to extract resolution: {e}")

        return None

    def _extract_video_info(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract video codec and HDR information.

        Follows original plexmeta.py pattern (lines 497-518):
        - Get extendedDisplayTitle from Stream element
        - Parse for codec, HDR format, Dolby Vision

        Args:
            metadata: Plex metadata dictionary

        Returns:
            Dictionary with video_codec, hdr, dolby_vision, hdr_format
        """
        info = {
            'video_codec': None,
            'hdr': False,
            'dolby_vision': False,
            'hdr_format': None
        }

        try:
            # Get Media/Part/Stream elements
            media_list = metadata.get('Media', [])
            if not media_list:
                return info

            media = media_list[0]
            parts = media.get('Part', [])
            if not parts:
                return info

            part = parts[0]
            streams = part.get('Stream', [])

            # Find video stream
            video_stream = None
            for stream in streams:
                if stream.get('streamType') == 1:  # Video stream
                    video_stream = stream
                    break

            if not video_stream:
                return info

            # Get extended display title (contains codec and HDR info)
            extended_title = video_stream.get('extendedDisplayTitle', '')
            codec = video_stream.get('codec', '').upper()
            codec_id = video_stream.get('codecID', '')

            # Extract video codec
            if 'HEVC' in extended_title.upper() or codec == 'HEVC' or 'hev1' in codec_id.lower():
                info['video_codec'] = 'HEVC'
            elif 'AVC' in extended_title.upper() or codec == 'H264' or codec == 'AVC':
                info['video_codec'] = 'AVC'
            elif 'VP9' in extended_title.upper() or codec == 'VP9':
                info['video_codec'] = 'VP9'
            elif 'AV1' in extended_title.upper() or codec == 'AV1':
                info['video_codec'] = 'AV1'
            else:
                info['video_codec'] = codec

            _etl = extended_title.lower()

            # Check for Dolby Vision (independent — DV can coexist with HDR10/HDR10+)
            if 'dolby vision' in _etl or 'dovi' in _etl:
                info['dolby_vision'] = True
                info['hdr'] = True

            # Check for HDR standard (separate from DV — e.g. "Dolby Vision / HDR10+")
            if 'hdr10+' in _etl:
                info['hdr'] = True
                info['hdr_format'] = 'HDR10+'
            elif 'hdr10' in _etl:
                info['hdr'] = True
                info['hdr_format'] = 'HDR10'
            elif 'smpte st 2086' in _etl or 'st 2086' in _etl:
                info['hdr'] = True
                info['hdr_format'] = 'HDR10'
            elif 'hdr' in _etl:
                info['hdr'] = True
                if not info['hdr_format']:
                    info['hdr_format'] = 'HDR'

        except Exception as e:
            self.logger.error(f"Failed to extract video info: {e}")

        return info

    def _extract_audio_info(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract audio codec and channel information.

        Args:
            metadata: Plex metadata dictionary

        Returns:
            Dictionary with audio_codec and audio_channels
        """
        info = {
            'audio_codec': None,
            'audio_channels': None,
            'audio_track': None,
            'subtitle_track': None,
        }

        try:
            # Get Media/Part/Stream elements
            media_list = metadata.get('Media', [])
            if not media_list:
                return info

            media = media_list[0]
            parts = media.get('Part', [])
            if not parts:
                return info

            part = parts[0]
            streams = part.get('Stream', [])

            # Extract audio language tags (all audio tracks)
            audio_langs = []
            for stream in streams:
                if stream.get('streamType') == 2:
                    lang = stream.get('languageTag') or stream.get('language') or ''
                    if lang and lang not in audio_langs:
                        audio_langs.append(lang.lower())
            if audio_langs:
                info['audio_track'] = ','.join(audio_langs)

            # Extract subtitle language tags (all subtitle tracks)
            sub_langs = []
            for stream in streams:
                if stream.get('streamType') == 3:
                    lang = stream.get('languageTag') or stream.get('language') or ''
                    if lang and lang not in sub_langs:
                        sub_langs.append(lang.lower())
            if sub_langs:
                info['subtitle_track'] = ','.join(sub_langs)

            # Find audio stream (prefer first audio track)
            audio_stream = None
            for stream in streams:
                if stream.get('streamType') == 2:  # Audio stream
                    audio_stream = stream
                    break

            if not audio_stream:
                return info

            # Get codec and profile
            codec = audio_stream.get('codec', '').upper()
            profile = audio_stream.get('profile', '').lower()
            display_title = audio_stream.get('displayTitle', '').lower()
            extended_title = audio_stream.get('extendedDisplayTitle', '').lower()

            # Get file path for filename-based Atmos fallback
            file_path = ''
            try:
                parts_list = metadata.get('Media', [{}])[0].get('Part', [{}])
                file_path = (parts_list[0].get('file', '') if parts_list else '').lower()
            except Exception:
                pass

            # Extract audio codec with Atmos/DTS:X detection
            if 'truehd' in codec.lower() or 'truehd' in profile:
                if 'atmos' in profile or 'atmos' in display_title or 'atmos' in extended_title:
                    info['audio_codec'] = 'TrueHD Atmos'
                else:
                    info['audio_codec'] = 'TrueHD'

            elif 'dts' in codec.lower() or codec.lower() == 'dca':
                # DCA is Plex's raw codec name for all DTS variants — use display/extended titles to differentiate
                if 'dts:x' in extended_title or 'dtsx' in extended_title or 'dts-x' in extended_title or \
                   'dts:x' in display_title or 'dtsx' in display_title:
                    info['audio_codec'] = 'DTS:X'
                elif 'dts-hd ma' in extended_title or 'dts-hd master' in extended_title or \
                     'dts-hd ma' in display_title or profile in ('ma', 'dts-hd ma') or \
                     'dts-hd master audio' in profile:
                    info['audio_codec'] = 'DTS-HD MA'
                elif 'dts-hd hra' in extended_title or 'dts-hd hra' in display_title or \
                     'dts-hd' in extended_title or 'dts-hd' in display_title or \
                     profile in ('dts-hd hra', 'dts-hd'):
                    info['audio_codec'] = 'DTS-HD HRA'
                else:
                    info['audio_codec'] = 'DTS'

            elif 'eac3' in codec.lower() or 'eac-3' in codec.lower():
                if 'atmos' in profile or 'atmos' in display_title or 'atmos' in extended_title or \
                   'atmos' in file_path:
                    info['audio_codec'] = 'DD+ Atmos'
                else:
                    info['audio_codec'] = 'DD+'

            elif 'ac3' in codec.lower() or 'ac-3' in codec.lower():
                if 'atmos' in display_title or 'atmos' in extended_title or 'atmos' in file_path:
                    info['audio_codec'] = 'DD Atmos'
                else:
                    info['audio_codec'] = 'DD'

            elif 'aac' in codec.lower():
                info['audio_codec'] = 'AAC'

            elif 'flac' in codec.lower():
                info['audio_codec'] = 'FLAC'

            elif 'opus' in codec.lower():
                info['audio_codec'] = 'Opus'

            elif 'vorbis' in codec.lower():
                info['audio_codec'] = 'Vorbis'

            else:
                info['audio_codec'] = codec

            # Extract channel count
            channels = audio_stream.get('channels')
            if channels:
                # Convert channel count to standard notation
                if channels >= 8:
                    info['audio_channels'] = '7.1'
                elif channels >= 6:
                    info['audio_channels'] = '5.1'
                elif channels >= 3:
                    info['audio_channels'] = '2.1'
                elif channels == 2:
                    info['audio_channels'] = '2.0'
                elif channels == 1:
                    info['audio_channels'] = '1.0'
                else:
                    info['audio_channels'] = str(channels)

        except Exception as e:
            self.logger.error(f"Failed to extract audio info: {e}")

        return info

    def extract_from_jellyfin_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract media info from a Jellyfin/Emby item metadata dict.

        Jellyfin returns MediaSources[0].MediaStreams with Type='Video'/'Audio' entries
        containing codec, resolution, HDR range, channel info etc.

        Returns the same output dict shape as extract_from_plex_metadata() so callers
        are fully interchangeable.
        """
        info = {
            'resolution': None,
            'hdr': False,
            'dolby_vision': False,
            'hdr_format': None,
            'audio_codec': None,
            'audio_channels': None,
            'audio_track': None,
            'subtitle_track': None,
            'video_codec': None,
            'container': None,
            'bitrate': None,
            'network': None,
            'studio': None,
            'content_rating': None,
            'tmdb_rating': None,
        }

        try:
            sources = metadata.get('MediaSources') or []
            if not sources:
                return info

            source = sources[0]
            streams = source.get('MediaStreams') or []
            info['container'] = (source.get('Container') or '').lower() or None
            info['bitrate'] = source.get('Bitrate')

            video_stream = None
            audio_stream = None
            for s in streams:
                stype = (s.get('Type') or '').lower()
                if stype == 'video' and video_stream is None:
                    video_stream = s
                elif stype == 'audio' and audio_stream is None:
                    audio_stream = s
                if video_stream and audio_stream:
                    break

            # ── Resolution ────────────────────────────────────────────────
            if video_stream:
                width = video_stream.get('Width') or 0
                height = video_stream.get('Height') or 0
                if width >= 3800 or height >= 2100:
                    info['resolution'] = '2160p'
                elif width >= 2500 or height >= 1400:
                    info['resolution'] = '1440p'
                elif width >= 1800 or height >= 1060:
                    info['resolution'] = '1080p'
                elif width >= 1200 or height >= 700:
                    info['resolution'] = '720p'
                elif width > 0:
                    info['resolution'] = f"{height}p" if height else None

            # ── HDR / Dolby Vision ─────────────────────────────────────────
            if video_stream:
                vr = (video_stream.get('VideoRange') or '').upper()
                vrt = (video_stream.get('VideoRangeType') or '').upper()

                # Dolby Vision
                if 'DOLBY' in vrt or 'DOVI' in vrt or vrt == 'DOLBYVISION':
                    info['dolby_vision'] = True
                    info['hdr'] = True

                # HDR type (independent of DV — can be "Dolby Vision / HDR10+")
                if 'HDR10PLUS' in vrt or 'HDR10+' in vrt:
                    info['hdr'] = True
                    info['hdr_format'] = 'HDR10+'
                elif 'HDR10' in vrt:
                    info['hdr'] = True
                    info['hdr_format'] = 'HDR10'
                elif 'HLG' in vrt:
                    info['hdr'] = True
                    info['hdr_format'] = 'HLG'
                elif vr == 'HDR' or 'HDR' in vrt:
                    info['hdr'] = True
                    if not info['hdr_format']:
                        info['hdr_format'] = 'HDR'

            # ── Video codec ────────────────────────────────────────────────
            if video_stream:
                codec = (video_stream.get('Codec') or '').lower()
                if codec in ('hevc', 'h265'):
                    info['video_codec'] = 'HEVC'
                elif codec in ('h264', 'avc'):
                    info['video_codec'] = 'AVC'
                elif codec == 'av1':
                    info['video_codec'] = 'AV1'
                elif codec == 'vp9':
                    info['video_codec'] = 'VP9'
                elif codec:
                    info['video_codec'] = codec.upper()

            # ── Audio codec ────────────────────────────────────────────────
            if audio_stream:
                codec = (audio_stream.get('Codec') or '').lower()
                display = (audio_stream.get('DisplayTitle') or '').lower()
                profile = (audio_stream.get('Profile') or '').lower()
                channels = audio_stream.get('Channels') or 0

                # Get file path for filename-based Atmos fallback
                jf_file_path = (source.get('Path') or '').lower()

                if 'truehd' in codec:
                    if 'atmos' in display or 'atmos' in profile:
                        info['audio_codec'] = 'TrueHD Atmos'
                    else:
                        info['audio_codec'] = 'TrueHD'
                elif 'dts' in codec or codec == 'dca':
                    # DCA is the raw codec name for all DTS variants — use display title to differentiate
                    if 'dts:x' in display or 'dtsx' in display:
                        info['audio_codec'] = 'DTS:X'
                    elif 'dts-hd ma' in display or 'master' in profile or 'ma' in profile:
                        info['audio_codec'] = 'DTS-HD MA'
                    elif 'dts-hd' in display:
                        info['audio_codec'] = 'DTS-HD HRA'
                    else:
                        info['audio_codec'] = 'DTS'
                elif codec in ('eac3', 'eac-3', 'e-ac-3'):
                    if 'atmos' in display or 'atmos' in profile or 'atmos' in jf_file_path:
                        info['audio_codec'] = 'DD+ Atmos'
                    else:
                        info['audio_codec'] = 'DD+'
                elif codec in ('ac3', 'ac-3'):
                    if 'atmos' in display or 'atmos' in profile or 'atmos' in jf_file_path:
                        info['audio_codec'] = 'DD Atmos'
                    else:
                        info['audio_codec'] = 'DD'
                elif codec == 'aac':
                    info['audio_codec'] = 'AAC'
                elif codec == 'flac':
                    info['audio_codec'] = 'FLAC'
                elif codec == 'opus':
                    info['audio_codec'] = 'Opus'
                elif codec:
                    info['audio_codec'] = codec.upper()

                # Channel layout
                ch_layout = (audio_stream.get('ChannelLayout') or '').lower()
                if ch_layout in ('7.1', '7.1(side)'):
                    info['audio_channels'] = '7.1'
                elif ch_layout in ('5.1', '5.1(side)'):
                    info['audio_channels'] = '5.1'
                elif ch_layout == 'stereo':
                    info['audio_channels'] = '2.0'
                elif ch_layout == 'mono':
                    info['audio_channels'] = '1.0'
                elif channels >= 8:
                    info['audio_channels'] = '7.1'
                elif channels >= 6:
                    info['audio_channels'] = '5.1'
                elif channels == 2:
                    info['audio_channels'] = '2.0'
                elif channels == 1:
                    info['audio_channels'] = '1.0'

            # ── Audio / subtitle language tracks ──────────────────────────
            audio_langs = []
            sub_langs = []
            for s in streams:
                stype = (s.get('Type') or '').lower()
                lang = s.get('Language') or ''
                if stype == 'audio' and lang and lang not in audio_langs:
                    audio_langs.append(lang.lower())
                elif stype == 'subtitle' and lang and lang not in sub_langs:
                    sub_langs.append(lang.lower())
            if audio_langs:
                info['audio_track'] = ','.join(audio_langs)
            if sub_langs:
                info['subtitle_track'] = ','.join(sub_langs)

            # ── Series / studio / content rating ──────────────────────────
            info['network'] = (metadata.get('Studios') or [{}])[0].get('Name') if metadata.get('Studios') else None
            info['studio'] = (metadata.get('Studios') or [{}])[0].get('Name') if metadata.get('Studios') else None
            cr = metadata.get('OfficialRating') or metadata.get('CustomRating') or ''
            info['content_rating'] = cr.strip() or None

            # ── TMDb community rating ──────────────────────────────────────
            # Jellyfin stores the TMDb community score in CommunityRating (0–10 scale).
            # Used as a fallback when MDBList is not configured.
            _cr = metadata.get('CommunityRating')
            if _cr is not None:
                try:
                    info['tmdb_rating'] = round(float(_cr), 1)
                except (ValueError, TypeError):
                    pass

            self.logger.info(
                f"Jellyfin media info: {info['resolution']} "
                f"{'DV/' if info['dolby_vision'] else ''}"
                f"{info.get('hdr_format') or ('HDR' if info['hdr'] else 'SDR')} "
                f"{info.get('video_codec')} {info.get('audio_codec')}"
            )

        except Exception as e:
            self.logger.error(f"Failed to extract Jellyfin media info: {e}", exc_info=True)

        return info

    def is_complete(self, info: Dict[str, Any]) -> bool:
        """
        Check if media info extraction was successful.

        Follows original plexmeta.py validation pattern:
        - videoResolution must exist
        - extendedDisplayTitle must exist (video stream info)

        Args:
            info: Extracted media info dictionary

        Returns:
            True if all required fields are present
        """
        # Resolution is the most critical field
        if not info.get('resolution'):
            return False

        # Video codec should also be present
        if not info.get('video_codec'):
            return False

        return True
