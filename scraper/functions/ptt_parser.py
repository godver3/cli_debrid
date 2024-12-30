"""
Shared PTT parsing functionality for consistent parsing across the application.
"""
import logging
from typing import Dict, Any
from functools import lru_cache
from PTT import parse_title

@lru_cache(maxsize=1024)
def parse_with_ptt(title: str) -> Dict[str, Any]:
    """
    Parse a title using PTT with caching.
    Returns a standardized format that can be used across the application.
    """
    try:
        result = parse_title(title)
        logging.debug(f"PTT parsed '{title}' into: {result}")
        
        # Convert to our standard format
        processed = {
            'title': result.get('title', title),
            'original_title': title,
            'type': 'movie' if not result.get('seasons') and not result.get('episodes') else 'episode',
            'year': result.get('year'),
            'resolution': result.get('resolution', 'Unknown'),
            'source': result.get('source'),
            'audio': result.get('audio'),
            'codec': result.get('codec'),
            'group': result.get('group'),
            'seasons': result.get('seasons', []),
            'episodes': result.get('episodes', [])
        }
        
        # Handle single season/episode for compatibility
        if len(processed['seasons']) == 1:
            processed['season'] = processed['seasons'][0]
        if len(processed['episodes']) == 1:
            processed['episode'] = processed['episodes'][0]
            
        return processed
    except Exception as e:
        logging.error(f"Error parsing title with PTT: {str(e)}")
        return {
            'title': title,
            'original_title': title,
            'parsing_error': True
        }
