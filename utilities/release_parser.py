"""
Release Parser Utility
Extracts quality tags and metadata from video release titles using GuessIt library.
Used across statistics, library, and torrent results displays.
"""

import logging
from typing import Dict, List, Optional
import re

logger = logging.getLogger(__name__)

# Try to import guessit, but provide fallback regex patterns if not available
try:
    from guessit import guessit
    HAS_GUESSIT = True
except Exception:
    HAS_GUESSIT = False
    logger.warning("GuessIt library not available. Using basic regex fallback for quality tag extraction.")


class ReleaseParser:
    """Parse video release titles to extract quality information and tags"""
    
    # Fallback regex patterns if GuessIt is not available
    RESOLUTION_PATTERNS = {
        '2160p': r'(?:2160p|4K|UHD)',
        '1080p': r'1080p',
        '720p': r'720p',
        '480p': r'480p',
        'SD': r'(?:480p|360p|SD)'
    }
    
    SOURCE_PATTERNS = {
        'REMUX': r'REMUX',
        'BluRay': r'(?:BluRay|Blu-Ray|BDMV|BDRip)',
        'WEB-DL': r'WEB-?DL',
        'WEBRip': r'WEBRip',
        'HDTV': r'HDTV',
        'DVD': r'DVDRip',
        'CAM': r'(?:CAM|TS|TC)',
    }
    
    CODEC_PATTERNS = {
        'x265': r'(?:x265|h\.?265|HEVC)',
        'x264': r'(?:x264|h\.?264|AVC)',
        'AV1': r'AV1',
        'VP9': r'VP9',
        'XviD': r'XviD',
    }
    
    AUDIO_PATTERNS = {
        'Atmos': r'Atmos',
        'TrueHD': r'TrueHD',
        'DTS-HD MA': r'DTS-?HD\.?MA',
        'DTS-X': r'DTS-?X',
        'DTS': r'DTS',
        'DD+': r'(?:DD\+|E-?AC3|DDP)',
        'DD': r'(?:DD|AC3)',
        'AAC': r'AAC',
    }
    
    HDR_PATTERNS = {
        'DV': r'(?:DV|DoVi|Dolby\.?Vision)',
        'HDR10+': r'HDR10\+',
        'HDR10': r'HDR10',
        'HDR': r'(?:HDR)',
    }
    
    @staticmethod
    def parse_with_guessit(title: str) -> Dict[str, any]:
        """
        Parse release title using GuessIt library (preferred method).
        
        Args:
            title: The release title to parse
            
        Returns:
            Dictionary with parsed metadata
        """
        if not HAS_GUESSIT:
            return {}
            
        try:
            # GuessIt returns a dict-like object with all parsed information
            guess = guessit(title)
            
            # Convert to standard dict and extract what we need
            result = {
                'title': guess.get('title', ''),
                'year': guess.get('year'),
                'resolution': guess.get('screen_size'),
                'source': guess.get('source'),
                'codec': guess.get('video_codec'),
                'audio': guess.get('audio_codec'),
                'release_group': guess.get('release_group'),
                'season': guess.get('season'),
                'episode': guess.get('episode'),
                'type': guess.get('type'),  # 'movie', 'episode', etc.
            }
            
            # Handle HDR
            if guess.get('color_depth'):
                result['hdr'] = guess.get('color_depth')
            elif 'HDR' in title.upper():
                result['hdr'] = 'HDR'
                
            return result
            
        except Exception as e:
            logger.error(f"GuessIt parsing failed for '{title}': {e}")
            return {}
    
    @staticmethod
    def parse_with_regex(title: str) -> Dict[str, Optional[str]]:
        """
        Parse release title using regex patterns (fallback method).
        
        Args:
            title: The release title to parse
            
        Returns:
            Dictionary with extracted quality tags
        """
        result = {
            'resolution': None,
            'source': None,
            'codec': None,
            'audio': None,
            'hdr': None,
        }
        
        # Extract resolution
        for res, pattern in ReleaseParser.RESOLUTION_PATTERNS.items():
            if re.search(pattern, title, re.IGNORECASE):
                result['resolution'] = res
                break
        
        # Extract source
        for source, pattern in ReleaseParser.SOURCE_PATTERNS.items():
            if re.search(pattern, title, re.IGNORECASE):
                result['source'] = source
                break
        
        # Extract codec
        for codec, pattern in ReleaseParser.CODEC_PATTERNS.items():
            if re.search(pattern, title, re.IGNORECASE):
                result['codec'] = codec
                break
        
        # Extract audio
        for audio, pattern in ReleaseParser.AUDIO_PATTERNS.items():
            if re.search(pattern, title, re.IGNORECASE):
                result['audio'] = audio
                break
        
        # Extract HDR
        for hdr, pattern in ReleaseParser.HDR_PATTERNS.items():
            if re.search(pattern, title, re.IGNORECASE):
                result['hdr'] = hdr
                break
        
        return result
    
    @classmethod
    def extract_quality_tags(cls, title: str) -> List[Dict[str, str]]:
        """
        Extract quality tags from release title as a list of tag dictionaries.
        This is the main method to use for displaying badges/tags in UI.
        
        Args:
            title: The release title to parse
            
        Returns:
            List of dictionaries with 'type' and 'value' for each tag.
            Example: [{'type': 'resolution', 'value': '1080p'}, 
                     {'type': 'source', 'value': 'BluRay'}, ...]
        """
        # Try GuessIt first (more accurate)
        if HAS_GUESSIT:
            parsed = cls.parse_with_guessit(title)
        else:
            parsed = cls.parse_with_regex(title)
        
        tags = []
        
        # Build tags list in priority order
        priority_order = ['resolution', 'source', 'codec', 'audio', 'hdr']
        
        for tag_type in priority_order:
            value = parsed.get(tag_type)
            if value:
                tags.append({
                    'type': tag_type,
                    'value': str(value).upper() if tag_type in ['resolution', 'hdr'] else str(value)
                })
        
        return tags
    
    @classmethod
    def get_quality_summary(cls, title: str) -> str:
        """
        Get a compact quality summary string for display.
        
        Args:
            title: The release title to parse
            
        Returns:
            Compact string like "1080p WEB-DL x265 HDR"
        """
        tags = cls.extract_quality_tags(title)
        return ' '.join([tag['value'] for tag in tags])
    
    @classmethod
    def extract_release_group(cls, title: str) -> Optional[str]:
        """
        Extract release group from title.
        
        Args:
            title: The release title
            
        Returns:
            Release group name or None
        """
        if HAS_GUESSIT:
            parsed = cls.parse_with_guessit(title)
            return parsed.get('release_group')
        
        # Fallback regex: release group is usually after the last dash
        match = re.search(r'-([A-Za-z0-9]+)$', title)
        return match.group(1) if match else None


def extract_quality_tags(title: str) -> List[Dict[str, str]]:
    """
    Convenience function to extract quality tags from a release title.
    
    Args:
        title: The release title to parse
        
    Returns:
        List of tag dictionaries
        
    Example:
        >>> tags = extract_quality_tags("Movie.2024.2160p.WEB-DL.x265.HDR-GROUP")
        >>> print(tags)
        [{'type': 'resolution', 'value': '2160p'}, 
         {'type': 'source', 'value': 'WEB-DL'}, 
         {'type': 'codec', 'value': 'x265'},
         {'type': 'hdr', 'value': 'HDR'}]
    """
    return ReleaseParser.extract_quality_tags(title)


def get_quality_summary(title: str) -> str:
    """
    Convenience function to get quality summary string.
    
    Args:
        title: The release title
        
    Returns:
        Compact quality string
        
    Example:
        >>> summary = get_quality_summary("Movie.2024.2160p.WEB-DL.x265.HDR-GROUP")
        >>> print(summary)
        "2160p WEB-DL x265 HDR"
    """
    return ReleaseParser.get_quality_summary(title)
