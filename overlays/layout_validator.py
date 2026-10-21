"""
Layout Validator

Validates overlay layout JSON structures.
"""

import json
import logging
import re
from typing import Dict, Any, List, Tuple, Optional


class LayoutValidator:
    """
    Validates overlay layout definitions.

    Ensures layouts are well-formed and contain valid elements,
    positions, conditions, and references to assets.
    """

    VALID_ELEMENT_TYPES = {
        'raster', 'text',
        # Badge types used by the layout builder and renderer
        'text_badge', 'designed_badge', 'smart_badge', 'background_panel',
    }
    VALID_POSITIONS = {'top_left', 'top_right', 'bottom_left', 'bottom_right', 'center', 'custom'}
    VALID_MEDIA_TYPES = {'movie', 'tv', 'season', 'both'}

    def __init__(self, asset_dir: str = "/user/config/overlay_assets"):
        """
        Initialize validator.

        Args:
            asset_dir: Path to overlay assets directory
        """
        self.asset_dir = asset_dir
        self.logger = logging.getLogger(__name__)

    def validate_layout(self, layout_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Validate complete layout structure.

        Supports two formats:
        - v2 (badge-centric): ``{ version: 2, badges: [...] }``
        - legacy:             ``{ name: ..., media_type: ..., elements: [...] }``

        Args:
            layout_data: Layout JSON dictionary

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []

        # Accept either a dict or a JSON string
        if isinstance(layout_data, str):
            try:
                layout_data = json.loads(layout_data)
            except (json.JSONDecodeError, ValueError) as e:
                return False, [f"Invalid JSON: {e}"]

        # ── v2 badge format ──────────────────────────────────
        if layout_data.get('version') == 2 or 'badges' in layout_data:
            return self._validate_badge_layout(layout_data)

        # ── legacy elements format ───────────────────────────
        # name and media_type were embedded in the JSON by the old builder
        required_fields = ['name', 'media_type', 'elements']
        for field in required_fields:
            if field not in layout_data:
                errors.append(f"Missing required field: {field}")

        if errors:
            return False, errors

        name = layout_data.get('name', '')
        if not name or not isinstance(name, str) or len(name) < 3:
            errors.append("Layout name must be a string with at least 3 characters")

        media_type = layout_data.get('media_type', '')
        if media_type not in self.VALID_MEDIA_TYPES:
            errors.append(f"Invalid media_type: {media_type}. Must be one of: {self.VALID_MEDIA_TYPES}")

        elements = layout_data.get('elements', [])
        if not isinstance(elements, list):
            errors.append("'elements' must be an array")
        elif len(elements) == 0:
            errors.append("Layout must have at least one element")
        else:
            for idx, element in enumerate(elements):
                errors.extend(self._validate_element(element, idx))

        return len(errors) == 0, errors

    def _validate_badge_layout(self, layout_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate v2 badge-centric layout format."""
        errors = []

        badges = layout_data.get('badges')
        if not isinstance(badges, list):
            errors.append("v2 layout must have a 'badges' array")
            return False, errors

        if len(badges) == 0:
            errors.append("Layout must have at least one badge")
            return False, errors

        for idx, badge in enumerate(badges):
            prefix = f"Badge {idx}"
            if not isinstance(badge, dict):
                errors.append(f"{prefix}: must be an object")
                continue

            for coord in ('x', 'y'):
                if coord not in badge:
                    errors.append(f"{prefix}: missing '{coord}' coordinate")
                elif not isinstance(badge[coord], (int, float)):
                    errors.append(f"{prefix}: '{coord}' must be a number")

            for section in ('background', 'icon', 'text'):
                if section in badge and not isinstance(badge[section], dict):
                    errors.append(f"{prefix}: '{section}' must be an object")

        return len(errors) == 0, errors

    def _validate_element(self, element: Dict[str, Any], index: int) -> List[str]:
        """
        Validate a single layout element.

        Args:
            element: Element dictionary
            index: Element index in array

        Returns:
            List of validation errors
        """
        errors = []
        prefix = f"Element {index}"

        # Validate type
        element_type = element.get('type')
        if not element_type:
            errors.append(f"{prefix}: Missing 'type' field")
            return errors  # Can't validate further without type

        if element_type not in self.VALID_ELEMENT_TYPES:
            errors.append(f"{prefix}: Invalid type '{element_type}'. Must be one of: {self.VALID_ELEMENT_TYPES}")
            return errors

        # Validate based on element type
        if element_type == 'raster':
            errors.extend(self._validate_raster_element(element, prefix))
        elif element_type == 'text':
            errors.extend(self._validate_text_element(element, prefix))

        # Validate common fields
        errors.extend(self._validate_position(element, prefix))
        errors.extend(self._validate_condition(element, prefix))
        errors.extend(self._validate_opacity(element, prefix))

        return errors

    def _validate_raster_element(self, element: Dict[str, Any], prefix: str) -> List[str]:
        """Validate raster (image) element."""
        errors = []

        # Require imagePath
        image_path = element.get('imagePath')
        if not image_path:
            errors.append(f"{prefix}: Raster element missing 'imagePath' field")
        elif not isinstance(image_path, str):
            errors.append(f"{prefix}: 'imagePath' must be a string")
        elif not image_path.startswith('/'):
            errors.append(f"{prefix}: 'imagePath' must start with '/' (relative to assets directory)")

        # Validate optional width/height
        for dimension in ['width', 'height']:
            if dimension in element:
                value = element[dimension]
                if not isinstance(value, (int, float)) or value <= 0:
                    errors.append(f"{prefix}: '{dimension}' must be a positive number")

        return errors

    def _validate_text_element(self, element: Dict[str, Any], prefix: str) -> List[str]:
        """Validate text element."""
        errors = []

        # Require text field
        text = element.get('text')
        if not text:
            errors.append(f"{prefix}: Text element missing 'text' field")
        elif not isinstance(text, str):
            errors.append(f"{prefix}: 'text' must be a string")

        # Validate font (optional)
        if 'font' in element:
            font = element['font']
            if not isinstance(font, str):
                errors.append(f"{prefix}: 'font' must be a string")

        # Validate size (optional)
        if 'size' in element:
            size = element['size']
            if not isinstance(size, (int, float)) or size <= 0:
                errors.append(f"{prefix}: 'size' must be a positive number")

        # Validate color (optional)
        if 'color' in element:
            color = element['color']
            if not self._is_valid_color(color):
                errors.append(f"{prefix}: 'color' must be a valid hex color (e.g., '#FFFFFF')")

        # Validate background color (optional)
        if 'background' in element:
            background = element['background']
            if not self._is_valid_color(background):
                errors.append(f"{prefix}: 'background' must be a valid hex color with optional alpha (e.g., '#000000AA')")

        return errors

    def _validate_position(self, element: Dict[str, Any], prefix: str) -> List[str]:
        """Validate element positioning."""
        errors = []

        position = element.get('position', 'top_left')
        if position not in self.VALID_POSITIONS:
            errors.append(f"{prefix}: Invalid position '{position}'. Must be one of: {self.VALID_POSITIONS}")

        # If custom position, require x and y
        if position == 'custom':
            if 'x' not in element:
                errors.append(f"{prefix}: Custom position requires 'x' coordinate")
            if 'y' not in element:
                errors.append(f"{prefix}: Custom position requires 'y' coordinate")

        # Validate x/y if present
        for coord in ['x', 'y']:
            if coord in element:
                value = element[coord]
                if not isinstance(value, (int, float)):
                    errors.append(f"{prefix}: '{coord}' must be a number")

        return errors

    def _validate_condition(self, element: Dict[str, Any], prefix: str) -> List[str]:
        """Validate conditional rendering expression."""
        errors = []

        condition = element.get('condition')
        if condition:
            if not isinstance(condition, str):
                errors.append(f"{prefix}: 'condition' must be a string")
            else:
                # Basic syntax check for conditions
                # Allow simple expressions like: resolution == '2160p' AND hdr == true
                if not self._is_valid_condition_syntax(condition):
                    errors.append(f"{prefix}: Invalid condition syntax: {condition}")

        return errors

    def _validate_opacity(self, element: Dict[str, Any], prefix: str) -> List[str]:
        """Validate opacity value."""
        errors = []

        opacity = element.get('opacity')
        if opacity is not None:
            if not isinstance(opacity, (int, float)):
                errors.append(f"{prefix}: 'opacity' must be a number")
            elif opacity < 0 or opacity > 1:
                errors.append(f"{prefix}: 'opacity' must be between 0 and 1")

        return errors

    def _is_valid_color(self, color: str) -> bool:
        """Check if color is a valid hex color string."""
        if not isinstance(color, str):
            return False

        # Match #RGB, #RRGGBB, #RRGGBBAA
        pattern = r'^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$'
        return bool(re.match(pattern, color))

    def _is_valid_condition_syntax(self, condition: str) -> bool:
        """
        Basic validation of condition syntax.

        Allows:
        - Variable names (alphanumeric + underscore)
        - Operators: ==, !=, <, >, <=, >=, AND, OR, NOT, IS, IN
        - String literals in quotes
        - Boolean literals: true, false
        - Numbers
        """
        # Remove string literals temporarily
        temp = re.sub(r'"[^"]*"', '""', condition)
        temp = re.sub(r"'[^']*'", "''", temp)

        # Check for dangerous patterns
        dangerous = ['import', 'exec', 'eval', '__', 'lambda', 'def ', 'class ']
        for pattern in dangerous:
            if pattern in temp.lower():
                return False

        # Check for allowed tokens
        allowed_pattern = r'^[\w\s\.\'"()]+$|^[\w\s\.\'"()\=\!\<\>\&\|]+$'
        if not re.match(allowed_pattern, temp):
            return False

        return True

    def validate_layout_references(self, layout_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Validate that layout references (assets, fonts) actually exist.

        Args:
            layout_data: Layout JSON dictionary

        Returns:
            Tuple of (is_valid, list_of_warnings)
        """
        from pathlib import Path

        warnings = []
        asset_base = Path(self.asset_dir)

        elements = layout_data.get('elements', [])
        for idx, element in enumerate(elements):
            element_type = element.get('type')

            # Check raster image paths
            if element_type == 'raster':
                image_path = element.get('imagePath', '')
                if image_path:
                    # Convert relative path to full path
                    full_path = asset_base / image_path.lstrip('/')
                    if not full_path.exists():
                        warnings.append(f"Element {idx}: Referenced image not found: {image_path}")

            # Check font files
            elif element_type == 'text':
                font = element.get('font', '')
                if font:
                    # Try common font locations
                    font_locations = [
                        asset_base / 'fonts' / f"{font}.ttf",
                        asset_base / 'fonts' / f"{font}.otf",
                        Path(f"/usr/share/fonts/truetype/{font.lower()}/{font}.ttf"),
                        Path(f"/usr/share/fonts/truetype/dejavu/{font}.ttf"),
                    ]

                    if not any(p.exists() for p in font_locations):
                        warnings.append(f"Element {idx}: Font file not found: {font}")

        is_valid = len(warnings) == 0
        return is_valid, warnings
