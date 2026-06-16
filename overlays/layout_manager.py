"""
Layout Manager

Manages overlay layout CRUD operations and storage.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from .layout_validator import LayoutValidator


def _get_db_connection():
    from database.core import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    return conn


class LayoutManager:
    """
    Manages overlay layouts in database and filesystem.

    Layouts are stored in:
    1. Database (overlay_layouts table) - for quick queries
    2. Filesystem (/user/config/overlay_layouts/) - for backup/portability
    """

    def __init__(self, db_path=None, layout_dir: str = "/user/config/overlay_layouts"):
        """
        Initialize layout manager.

        Args:
            db_path: Unused — kept for API compatibility. DB access uses get_db_connection().
            layout_dir: Path to layout storage directory
        """
        self.layout_dir = Path(layout_dir)
        self.layout_dir.mkdir(parents=True, exist_ok=True)
        self.validator = LayoutValidator()
        self.logger = logging.getLogger(__name__)

    def create_layout(self, name: str, description: str, media_type: str,
                       layout_json: Dict[str, Any], is_default: bool = True,
                       is_system: bool = False) -> Dict[str, Any]:
        """
        Create a new layout.

        Args:
            name: Layout name (must be unique)
            description: Layout description
            media_type: 'movie', 'tv', or 'both'
            layout_json: Layout JSON structure
            is_default: Whether layout is active

        Returns:
            Layout dictionary with ID

        Raises:
            ValueError: If layout validation fails or name already exists
        """
        # Validate layout structure
        is_valid, errors = self.validator.validate_layout(layout_json)
        if not is_valid:
            error_msg = "Layout validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # Check for asset reference warnings
        refs_valid, warnings = self.validator.validate_layout_references(layout_json)
        if warnings:
            self.logger.warning("Layout reference warnings:\n" + "\n".join(f"  - {w}" for w in warnings))

        # Check if name already exists
        existing = self.get_layout_by_name(name)
        if existing:
            raise ValueError(f"Layout with name '{name}' already exists")

        # Serialize to JSON string for DB storage; keep the dict for filesystem save
        layout_json_str = json.dumps(layout_json, indent=2)

        # Insert into database
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO overlay_layouts
                (name, description, media_type, layout_json, is_default, is_system)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (name, description, media_type, layout_json_str, 1 if is_default else 0, 1 if is_system else 0))

            layout_id = cursor.lastrowid
            conn.commit()
            conn.close()

            # Save dict to filesystem (json.dump inside will serialize it correctly)
            self._save_layout_to_file(name, layout_json)

            self.logger.info(f"Created layout: {name} (ID: {layout_id})")

            return self.get_layout(layout_id)

        except sqlite3.IntegrityError as e:
            self.logger.error(f"Database integrity error creating layout: {e}")
            raise ValueError(f"Layout name '{name}' already exists")
        except Exception as e:
            self.logger.error(f"Failed to create layout: {e}")
            raise

    def get_layout(self, layout_id: int) -> Optional[Dict[str, Any]]:
        """
        Get layout by ID.

        Args:
            layout_id: Layout ID

        Returns:
            Layout dictionary or None if not found
        """
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, name, description, media_type, layout_json,
                       is_default, is_system, created_at, updated_at
                FROM overlay_layouts
                WHERE id = ?
            ''', (layout_id,))

            row = cursor.fetchone()
            conn.close()

            if row:
                layout = dict(row)
                # Parse JSON layout_json
                layout['layout_json'] = json.loads(layout['layout_json'])
                return layout

            return None

        except Exception as e:
            self.logger.error(f"Failed to get layout {layout_id}: {e}")
            return None

    def get_layout_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get layout by name.

        Args:
            name: Layout name

        Returns:
            Layout dictionary or None if not found
        """
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, name, description, media_type, layout_json,
                       is_default, is_system, created_at, updated_at
                FROM overlay_layouts
                WHERE name = ?
            ''', (name,))

            row = cursor.fetchone()
            conn.close()

            if row:
                layout = dict(row)
                layout['layout_json'] = json.loads(layout['layout_json'])
                return layout

            return None

        except Exception as e:
            self.logger.error(f"Failed to get layout by name '{name}': {e}")
            return None

    def list_layouts(self, media_type: Optional[str] = None,
                      active_only: bool = False) -> List[Dict[str, Any]]:
        """
        List all layouts.

        Args:
            media_type: Filter by media type ('movie', 'tv', 'both')
            active_only: Only return active layouts

        Returns:
            List of layout dictionaries
        """
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            # Build query with filters
            query = '''
                SELECT id, name, description, media_type, layout_json,
                       is_default, is_system, created_at, updated_at
                FROM overlay_layouts
                WHERE 1=1
            '''
            params = []

            if media_type:
                query += ' AND (media_type = ? OR media_type = ?)'
                params.extend([media_type, 'both'])

            if active_only:
                query += ' AND is_default = 1'

            query += ' ORDER BY name'

            cursor.execute(query, params)
            rows = cursor.fetchall()
            conn.close()

            layouts = []
            for row in rows:
                layout = dict(row)
                layout['layout_json'] = json.loads(layout['layout_json'])
                layouts.append(layout)

            return layouts

        except Exception as e:
            self.logger.error(f"Failed to list layouts: {e}")
            return []

    def update_layout(self, layout_id: int, name: Optional[str] = None,
                       description: Optional[str] = None,
                       media_type: Optional[str] = None,
                       layout_json: Optional[Dict[str, Any]] = None,
                       is_default: Optional[bool] = None) -> bool:
        """
        Update layout fields.

        Args:
            layout_id: Layout ID
            name: New name (optional)
            description: New description (optional)
            media_type: New media type ('movie', 'tv', 'season', 'both') (optional)
            layout_json: New layout structure (optional)
            is_default: New active status (optional)

        Returns:
            True if successful, False otherwise
        """
        # Get existing layout
        existing = self.get_layout(layout_id)
        if not existing:
            self.logger.error(f"Layout {layout_id} not found")
            return False

        # Validate new layout_json if provided
        if layout_json:
            is_valid, errors = self.validator.validate_layout(layout_json)
            if not is_valid:
                error_msg = "Layout validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
                self.logger.error(error_msg)
                raise ValueError(error_msg)

        # Check for name conflicts
        if name and name != existing['name']:
            conflict = self.get_layout_by_name(name)
            if conflict and conflict['id'] != layout_id:
                raise ValueError(f"Layout with name '{name}' already exists")

        # Build update query
        updates = []
        params = []

        if name is not None:
            updates.append('name = ?')
            params.append(name)

        if description is not None:
            updates.append('description = ?')
            params.append(description)

        if media_type is not None:
            valid_types = {'movie', 'tv', 'season', 'both'}
            if media_type not in valid_types:
                raise ValueError(f"Invalid media_type '{media_type}'. Must be one of: {', '.join(sorted(valid_types))}")
            updates.append('media_type = ?')
            params.append(media_type)

        if layout_json is not None:
            updates.append('layout_json = ?')
            params.append(json.dumps(layout_json, indent=2))

        if is_default is not None:
            updates.append('is_default = ?')
            params.append(1 if is_default else 0)

        if not updates:
            return True  # Nothing to update

        updates.append('updated_at = CURRENT_TIMESTAMP')

        # Perform update
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            query = f"UPDATE overlay_layouts SET {', '.join(updates)} WHERE id = ?"
            params.append(layout_id)

            cursor.execute(query, params)
            conn.commit()
            conn.close()

            # Update filesystem file using values already in scope — no second DB read needed
            final_name = name if name is not None else existing['name']
            final_data = layout_json if layout_json is not None else existing['layout_json']
            self._save_layout_to_file(final_name, final_data)

            self.logger.info(f"Updated layout {layout_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to update layout {layout_id}: {e}")
            return False

    def delete_layout(self, layout_id: int) -> bool:
        """
        Delete a layout.

        Args:
            layout_id: Layout ID

        Returns:
            True if successful, False otherwise
        """
        # Get layout to get name for file deletion
        layout = self.get_layout(layout_id)
        if not layout:
            self.logger.error(f"Layout {layout_id} not found")
            return False

        try:
            conn = _get_db_connection()
            cursor = conn.cursor()

            cursor.execute('DELETE FROM overlay_layouts WHERE id = ?', (layout_id,))
            conn.commit()
            conn.close()

            # Delete file
            self._delete_layout_file(layout['name'])

            self.logger.info(f"Deleted layout {layout_id}: {layout['name']}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to delete layout {layout_id}: {e}")
            return False

    def _save_layout_to_file(self, name: str, layout_json: Dict[str, Any]):
        """Save layout to JSON file."""
        try:
            # Sanitize filename
            filename = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in name)
            filename = f"{filename}.json"

            file_path = self.layout_dir / filename
            with open(file_path, 'w') as f:
                json.dump(layout_json, f, indent=2)

            self.logger.debug(f"Saved layout to {file_path}")

        except Exception as e:
            self.logger.error(f"Failed to save layout file: {e}")

    def _delete_layout_file(self, name: str):
        """Delete layout JSON file."""
        try:
            filename = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in name)
            filename = f"{filename}.json"

            file_path = self.layout_dir / filename
            if file_path.exists():
                file_path.unlink()
                self.logger.debug(f"Deleted layout file {file_path}")

        except Exception as e:
            self.logger.error(f"Failed to delete layout file: {e}")

    def import_layout_from_file(self, file_path: str, is_default: bool = True) -> Dict[str, Any]:
        """
        Import layout from JSON file.

        Args:
            file_path: Path to JSON layout file
            is_default: Whether layout should be active

        Returns:
            Created layout dictionary

        Raises:
            ValueError: If file is invalid or layout validation fails
        """
        try:
            with open(file_path, 'r') as f:
                layout_json = json.load(f)

            # Extract metadata from layout
            name = layout_json.get('name', Path(file_path).stem)
            description = layout_json.get('description', f'Imported from {Path(file_path).name}')
            media_type = layout_json.get('media_type', 'both')

            # Create layout
            return self.create_layout(name, description, media_type, layout_json, is_default)

        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON file: {e}")
        except Exception as e:
            raise ValueError(f"Failed to import layout: {e}")

    def load_default_layouts(self, skip_existing: bool = True) -> Dict[str, Any]:
        """
        Import bundled default layouts from overlays/default_layouts/.

        Args:
            skip_existing: If True, skip any layout whose name already exists in the DB.
                           If False, raises ValueError on name collision (same as create_layout).

        Returns:
            Dict with keys: loaded (int), skipped (int), errors (list of str)
        """
        defaults_dir = Path(__file__).parent / 'default_layouts'
        if not defaults_dir.exists():
            self.logger.warning(f"Default layouts directory not found: {defaults_dir}")
            return {'loaded': 0, 'skipped': 0, 'errors': []}

        loaded = 0
        skipped = 0
        errors = []

        for json_file in sorted(defaults_dir.glob('*.json')):
            try:
                with open(json_file, 'r') as f:
                    data = json.load(f)

                name = data.get('name', json_file.stem)
                description = data.get('description', '')
                media_type = data.get('media_type', 'both')
                layout_json = data.get('layout_json', data)

                if skip_existing and self.get_layout_by_name(name):
                    self.logger.debug(f"Default layout '{name}' already exists, skipping.")
                    skipped += 1
                    continue

                self.create_layout(
                    name=name,
                    description=description,
                    media_type=media_type,
                    layout_json=layout_json,
                    is_default=True,
                    is_system=True,
                )
                self.logger.info(f"Loaded default layout: {name}")
                loaded += 1

            except Exception as e:
                self.logger.error(f"Failed to load default layout {json_file.name}: {e}")
                errors.append(f"{json_file.name}: {e}")

        return {'loaded': loaded, 'skipped': skipped, 'errors': errors}

    def restore_from_filesystem(self, skip_existing: bool = True) -> Dict[str, Any]:
        """
        Restore user layouts from filesystem backup at self.layout_dir.
        Called on startup when the DB was wiped to recover user-created layouts.

        The filesystem files only contain layout_json (no name/media_type metadata),
        so we infer metadata from the filename.
        """
        loaded = 0
        skipped = 0
        errors = []

        _media_type_hints = {
            'movie': 'movie', 'movies': 'movie',
            'show': 'tv', 'shows': 'tv', 'tv': 'tv',
            'season': 'tv', 'seasons': 'tv',
            'episode': 'tv', 'episodes': 'tv',
        }

        for json_file in sorted(self.layout_dir.glob('*.json')):
            try:
                with open(json_file, 'r') as f:
                    layout_json = json.load(f)

                # Derive name from filename stem
                name = json_file.stem

                # Skip if already in DB
                if skip_existing and self.get_layout_by_name(name):
                    self.logger.debug(f"Layout '{name}' already in DB, skipping restore.")
                    skipped += 1
                    continue

                # Infer media_type from name
                name_lower = name.lower()
                media_type = 'both'
                for keyword, mtype in _media_type_hints.items():
                    if keyword in name_lower:
                        media_type = mtype
                        break

                # layout_json may be the full dict or wrapped under 'layout_json' key
                if 'layout_json' in layout_json:
                    actual_layout = layout_json['layout_json']
                    description = layout_json.get('description', f'Restored from {json_file.name}')
                    media_type = layout_json.get('media_type', media_type)
                else:
                    actual_layout = layout_json
                    description = f'Restored from {json_file.name}'

                self.create_layout(
                    name=name,
                    description=description,
                    media_type=media_type,
                    layout_json=actual_layout,
                    is_default=True,
                    is_system=False,
                )
                self.logger.info(f"Restored layout '{name}' from filesystem backup.")
                loaded += 1

            except Exception as e:
                self.logger.error(f"Failed to restore layout {json_file.name}: {e}")
                errors.append(f"{json_file.name}: {e}")

        return {'loaded': loaded, 'skipped': skipped, 'errors': errors}

    def export_layout_to_file(self, layout_id: int, output_path: str) -> bool:
        """
        Export layout to JSON file.

        Args:
            layout_id: Layout ID
            output_path: Output file path

        Returns:
            True if successful, False otherwise
        """
        layout = self.get_layout(layout_id)
        if not layout:
            self.logger.error(f"Layout {layout_id} not found")
            return False

        try:
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            with open(output, 'w') as f:
                json.dump(layout['layout_json'], f, indent=2)

            self.logger.info(f"Exported layout {layout_id} to {output_path}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to export layout {layout_id}: {e}")
            return False
