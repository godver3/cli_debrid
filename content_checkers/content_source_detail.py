import logging
from typing import Dict, Any, Optional

def append_content_source_detail(item: Dict[str, Any], source_type: Optional[str] = None) -> Dict[str, Any]:
    """
    Takes an item dictionary and appends appropriate content_source_detail based on the content_source.
    For now, returns None for all sources until we implement specific detail logic.
    
    Args:
        item (Dict[str, Any]): The item dictionary containing at minimum a content_source field
        source_type (Optional[str]): The type of source (e.g., 'Overseerr', 'Trakt Watchlist', etc.)
                                   If not provided, will attempt to extract from content_source
        
    Returns:
        Dict[str, Any]: The same item dictionary with content_source_detail added
    """
    try:
        content_source = item.get('content_source')
        if not content_source:
            logging.warning("No content_source found in item, cannot append detail")
            item['content_source_detail'] = None
            return item

        # If source_type not provided, try to extract from content_source
        if not source_type and content_source:
            source_type = content_source.split('_')[0]

        # Get the detail based on source type
        detail = None
        if source_type == 'My Plex Watchlist':
            detail = item.get('content_source_detail', 'Unknown User')
        elif source_type == 'Other Plex Watchlist':
            detail = item.get('content_source_detail', 'Unknown User')
        elif source_type == 'Overseerr':
            detail = item.get('content_source_detail')
        elif source_type == 'Trakt':
            detail = item.get('content_source_detail')
        elif source_type == 'MDBList':
            detail = item.get('content_source_detail')
        elif source_type == 'Magnet_Assigner':
            detail = item.get('content_source_detail')
        
        item['content_source_detail'] = detail
        return item
    except Exception as e:
        logging.error(f"Error appending content source detail: {str(e)}")
        item['content_source_detail'] = None
        return item 