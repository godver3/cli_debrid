"""
Zurg WebDAV utility for fast file existence checks.

On macOS, FUSE mounts are extremely slow for directory operations.
This module provides WebDAV-based file checking that bypasses FUSE,
reducing file existence checks from ~20 seconds to ~50ms.
"""

import logging
import urllib.request
import urllib.error
from typing import Optional
from utilities.settings import get_setting

# Module-level singleton instance
_webdav_instance: Optional['ZurgWebDAV'] = None


class ZurgWebDAV:
    """
    WebDAV client for Zurg file operations.

    Provides fast file existence checking by making HTTP requests
    directly to Zurg's WebDAV server instead of through FUSE.
    """

    def __init__(self, webdav_url: str, mount_path: str):
        """
        Initialize WebDAV client.

        Args:
            webdav_url: Base URL of Zurg WebDAV server (e.g., "http://localhost:9999/dav")
            mount_path: Local FUSE mount path (e.g., "/mnt/zurg")
        """
        self.webdav_url = webdav_url.rstrip('/')
        self.mount_path = mount_path.rstrip('/')
        self.timeout = 10
        self._enabled = True
        logging.info(f"[ZurgWebDAV] Initialized with URL: {self.webdav_url}, mount path: {self.mount_path}")

    def _path_to_url(self, local_path: str) -> Optional[str]:
        """
        Convert a local FUSE path to a WebDAV URL.

        Args:
            local_path: Local filesystem path (e.g., "/mnt/zurg/__all__/Movie.mkv")

        Returns:
            WebDAV URL or None if path is not under the mount point
        """
        if not local_path.startswith(self.mount_path):
            return None

        relative_path = local_path[len(self.mount_path):]
        # URL-encode the path components
        encoded_path = urllib.request.pathname2url(relative_path)
        return f"{self.webdav_url}{encoded_path}"

    def is_zurg_path(self, path: str) -> bool:
        """Check if a path is under the Zurg mount point."""
        return path.startswith(self.mount_path)

    def exists(self, path: str) -> bool:
        """
        Check if a file or directory exists via WebDAV HEAD request.

        This is ~400x faster than os.path.exists() on macOS FUSE mounts.

        Args:
            path: Local filesystem path to check

        Returns:
            True if file exists, False otherwise
        """
        if not self._enabled:
            import os
            return os.path.exists(path)

        url = self._path_to_url(path)
        if not url:
            # Not a zurg path, fall back to filesystem
            import os
            return os.path.exists(path)

        try:
            req = urllib.request.Request(url, method='HEAD')
            response = urllib.request.urlopen(req, timeout=self.timeout)
            return response.status in (200, 207)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            logging.debug(f"[ZurgWebDAV] HTTP error checking {path}: {e.code}")
            return False
        except Exception as e:
            logging.debug(f"[ZurgWebDAV] Error checking {path}: {e}")
            # On error, fall back to filesystem check
            import os
            return os.path.exists(path)

    def is_accessible(self) -> bool:
        """
        Quick connectivity check to verify Zurg WebDAV is responsive.

        Returns:
            True if WebDAV server is accessible, False otherwise
        """
        try:
            req = urllib.request.Request(f"{self.webdav_url}/", method='PROPFIND')
            req.add_header('Depth', '0')
            response = urllib.request.urlopen(req, timeout=self.timeout)
            return response.status in (200, 207)
        except Exception as e:
            logging.warning(f"[ZurgWebDAV] Accessibility check failed: {e}")
            return False

    def list_dir(self, path: str) -> Optional[list]:
        """
        List directory contents via WebDAV PROPFIND.

        Note: This returns a list of names, not full paths.

        Args:
            path: Local filesystem path to list

        Returns:
            List of filenames or None on error
        """
        url = self._path_to_url(path)
        if not url:
            return None

        try:
            req = urllib.request.Request(url, method='PROPFIND')
            req.add_header('Depth', '1')
            response = urllib.request.urlopen(req, timeout=self.timeout)

            # Parse the XML response to extract hrefs
            import xml.etree.ElementTree as ET
            content = response.read()
            root = ET.fromstring(content)

            # Extract file/folder names from response
            names = []
            ns = {'d': 'DAV:'}
            for response_elem in root.findall('.//d:response', ns):
                href = response_elem.find('d:href', ns)
                if href is not None and href.text:
                    # Extract just the filename from the href
                    name = href.text.rstrip('/').split('/')[-1]
                    if name and name != path.split('/')[-1]:  # Skip self
                        names.append(urllib.request.url2pathname(name))

            return names
        except Exception as e:
            logging.debug(f"[ZurgWebDAV] Error listing {path}: {e}")
            return None

    def has_content(self) -> bool:
        """
        Check if the Zurg mount has any content.

        This is a fast alternative to os.scandir() for emptiness checks.

        Returns:
            True if mount has content, False if empty or inaccessible
        """
        try:
            req = urllib.request.Request(f"{self.webdav_url}/", method='PROPFIND')
            req.add_header('Depth', '1')
            response = urllib.request.urlopen(req, timeout=self.timeout)
            content = response.read()

            # If we get more than just the root in the response, there's content
            # A simple heuristic: if response is > 500 bytes, there's likely content
            return len(content) > 500
        except Exception as e:
            logging.warning(f"[ZurgWebDAV] Content check failed: {e}")
            return False


def get_webdav_client() -> Optional[ZurgWebDAV]:
    """
    Get the global ZurgWebDAV client instance.

    Returns the singleton instance, creating it if necessary based on settings.
    Returns None if WebDAV is not configured or disabled.
    """
    global _webdav_instance

    if _webdav_instance is not None:
        return _webdav_instance

    # Check if WebDAV is configured and enabled
    webdav_url = get_setting('Zurg', 'webdav_url')
    use_webdav = get_setting('Zurg', 'use_webdav_for_checks', default=False)
    mount_path = get_setting('File Management', 'original_files_path', default='/mnt/zurg/__all__')

    if not webdav_url or not use_webdav:
        logging.debug("[ZurgWebDAV] WebDAV not configured or disabled")
        return None

    # Extract mount base from original_files_path (e.g., /mnt/zurg/__all__ -> /mnt/zurg)
    # Assume mount path is everything up to __all__ or similar
    mount_base = mount_path
    for marker in ['/__all__', '/__downloads__', '/__unplayable__']:
        if marker in mount_path:
            mount_base = mount_path.split(marker)[0]
            break

    _webdav_instance = ZurgWebDAV(webdav_url, mount_base)
    return _webdav_instance


def reset_webdav_client():
    """Reset the global WebDAV client instance (useful after settings change)."""
    global _webdav_instance
    _webdav_instance = None


def webdav_exists(path: str) -> bool:
    """
    Convenience function to check file existence.

    Uses WebDAV if configured, otherwise falls back to os.path.exists().

    Args:
        path: Path to check

    Returns:
        True if file exists
    """
    client = get_webdav_client()
    if client and client.is_zurg_path(path):
        return client.exists(path)

    import os
    return os.path.exists(path)
