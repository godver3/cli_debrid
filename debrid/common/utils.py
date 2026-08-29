import logging
import os
import re
from typing import List, Optional, Union, Dict, Tuple

# Common video file extensions
VIDEO_EXTENSIONS = [
    'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'webm', 'mpg', 'mpeg', 'm2ts', 'ts'
]

def is_video_file(filename: str) -> bool:
    """Check if a file is a video file based on its extension"""
    result = any(filename.lower().endswith(f'.{ext}') for ext in VIDEO_EXTENSIONS)
    #logging.info(f"is_video_file check for {filename}: {result}")
    return result

_JUNK_WORDS = frozenset({"sample", "trailer"})
_EPISODE_RE = re.compile(r"[Ss]\d{1,2}[Ee]\d{1,2}(?![0-9])")
_BRACKET_JUNK_RE = re.compile(r"[\[\(]\s*(?:sample|trailer)\s*[\]\)]", re.I)
_QUALITY_SEGMENTS = frozenset({
    "1080p", "720p", "576p", "480p", "360p", "2160p", "4k", "8k",
    "web", "dl", "webdl", "web-dl", "webrip", "web-rip", "hdrip", "bdrip", "brrip",
    "bluray", "blu-ray", "hdtv", "pdtv", "dsrip", "dvdrip", "dvd",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx",
    "aac", "aac2", "aac2.0", "dts", "dts-hd", "ac3", "eac3", "flac", "mp3",
    "proper", "repack", "rerip", "readnfo", "nfo",
    "amzn", "atvp", "nf", "hulu", "dsnp", "pcok", "stvr",
})


def _split_release_segments(stem: str) -> List[str]:
    return [segment for segment in re.split(r"[.\-_ \t]+", stem) if segment]


def _is_quality_segment(segment: str) -> bool:
    lowered = segment.lower()
    if lowered in _QUALITY_SEGMENTS:
        return True
    if re.fullmatch(r"\d{3,4}[pi]", lowered):
        return True
    if re.fullmatch(r"aac\d(?:\.\d)?", lowered):
        return True
    if re.fullmatch(r"h\.?264|h\.?265", lowered):
        return True
    return False


def _is_title_like_segment(segment: str) -> bool:
    if not segment or _is_quality_segment(segment):
        return False
    if _EPISODE_RE.search(segment):
        return False
    if re.fullmatch(r"\d{4}", segment):
        return False
    if segment.lower() in _JUNK_WORDS:
        return False
    return bool(re.search(r"[a-zA-Z]{2,}", segment))


def _junk_word_segment_is_metadata(segments: List[str], index: int) -> bool:
    """True when sample/trailer is a release tag, not part of a show or episode title."""
    word = segments[index].lower()
    if word not in _JUNK_WORDS:
        return False

    previous = segments[index - 1] if index > 0 else None
    nxt = segments[index + 1] if index + 1 < len(segments) else None

    if nxt is None:
        return True
    if _is_quality_segment(nxt):
        return True
    if previous and _EPISODE_RE.search(previous):
        return not _is_title_like_segment(nxt)
    if _is_title_like_segment(nxt):
        return False
    if previous and _is_title_like_segment(previous):
        return False
    return True


def is_unwanted_file(filename: str) -> bool:
    """Check if a file is an unwanted sample/trailer extra.

    Uses standalone segment matching instead of bare substrings so episode
    titles like 'Punch Drunk Trailer Trashed' are not false positives.
    """
    name = os.path.basename(filename.replace("\\", "/"))
    if _BRACKET_JUNK_RE.search(name):
        return True

    stem = name.rsplit(".", 1)[0] if "." in name else name
    segments = _split_release_segments(stem)
    for index, segment in enumerate(segments):
        if segment.lower() in _JUNK_WORDS and _junk_word_segment_is_metadata(segments, index):
            return True
    return False

def filter_unwanted_video_files(video_files: List[Tuple[str, int]], size_threshold_ratio: float = 0.05) -> List[Tuple[str, int]]:
    """
    Filter a list of (filename, size) tuples to drop samples/trailers.

    First drops files whose name matches is_unwanted_file (standalone sample/trailer tags).
    Then, if multiple files remain, drops any file smaller than size_threshold_ratio
    of the largest remaining file - this catches unnamed extras/trailers without
    affecting legitimate short episodes or bonus content.

    Never returns an empty list if the input was non-empty (falls back to the
    unfiltered input so a false-positive match can't hide every candidate).
    """
    filtered = [(name, size) for name, size in video_files if not is_unwanted_file(name)]
    if not filtered:
        filtered = list(video_files)

    if len(filtered) > 1:
        max_size = max(size for _, size in filtered)
        if max_size > 0:
            filtered = [(name, size) for name, size in filtered if size >= max_size * size_threshold_ratio]

    return filtered


def pick_best_video_file(
    video_files: List[Tuple[str, int]],
    season: Optional[int] = None,
    episode: Optional[int] = None,
    size_threshold_ratio: float = 0.05,
) -> Optional[Tuple[str, int]]:
    """Return the largest suitable video after sample/trailer and relative-size filtering.

    When season/episode are provided, only filenames matching that episode are
    considered; otherwise the largest remaining file wins.
    """
    filtered = filter_unwanted_video_files(video_files, size_threshold_ratio=size_threshold_ratio)
    if not filtered:
        return None

    if season is not None and episode is not None:
        ep_pat = re.compile(rf'[Ss]{int(season):02d}[Ee]{int(episode):02d}(?![0-9])', re.IGNORECASE)
        matches = [(name, size) for name, size in filtered if ep_pat.search(name)]
        if matches:
            return max(matches, key=lambda x: x[1])

    return max(filtered, key=lambda x: x[1])


def extract_hash_from_magnet(magnet_link: str) -> str:
    """Extract hash from magnet link or download and extract from HTTP link."""
    try:
        # If it's an HTTP link, download and extract hash
        if magnet_link.startswith('http'):
            from debrid.common import download_and_extract_hash
            return download_and_extract_hash(magnet_link)
            
        # For magnet links, extract hash directly
        if not magnet_link.startswith('magnet:'):
            raise ValueError("Invalid magnet link format")
            
        # Extract hash from magnet link
        hash_match = re.search(r'btih:([a-fA-F0-9]{40})', magnet_link)
        if not hash_match:
            raise ValueError("Could not find valid hash in magnet link")
            
        return hash_match.group(1).lower()
    except Exception as e:
        logging.error(f"Error extracting hash: {str(e)}")
        raise ValueError("Invalid magnet link format")

def is_valid_hash(hash_string: str) -> bool:
    """Check if a string is a valid hash"""
    return bool(re.match(r'^[a-fA-F0-9]{40}$', hash_string))

def process_hashes(hashes: Union[str, List[str]], batch_size: int = 100) -> List[str]:
    """Process and validate a list of hashes"""
    if isinstance(hashes, str):
        hashes = [hashes]
    
    # Remove duplicates and invalid hashes
    return list(set(h.lower() for h in hashes if is_valid_hash(h)))

def format_torrent_status(active_torrents: List[Dict], download_stats: Tuple[int, int]) -> str:
    """
    Format torrent status information into a human-readable string.
    Shows both active downloads and recently completed downloads.
    
    Args:
        active_torrents: List of dictionaries containing torrent information
        download_stats: Tuple of (active_count, max_downloads)
    
    Returns:
        Formatted string containing torrent status information
    """
    active_count, max_downloads = download_stats
    status_lines = [f"Active Downloads: {active_count}/{max_downloads}"]
    
    # Split torrents into active and completed
    downloading_torrents = []
    completed_torrents = []
    
    for torrent in active_torrents:
        if torrent.get('progress', 0) == 100 and torrent.get('status', '').lower() == 'downloaded':
            completed_torrents.append(torrent)
        else:
            downloading_torrents.append(torrent)
    
    # Show active downloads
    if not downloading_torrents:
        status_lines.append("\nNo active downloads")
    else:
        status_lines.append("\nActive Downloads:")
        for torrent in downloading_torrents:
            filename = torrent.get('filename', 'Unknown')
            progress = torrent.get('progress', 0)
            status = torrent.get('status', 'unknown')
            status_lines.append(f"- {filename}")
            status_lines.append(f"  Progress: {progress}%, Status: {status}")
    
    # Show completed downloads
    if completed_torrents:
        status_lines.append("\nRecently Completed:")
        for torrent in completed_torrents:
            filename = torrent.get('filename', 'Unknown')
            status_lines.append(f"- {filename}")
    
    return "\n".join(status_lines)
