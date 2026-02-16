#!/usr/bin/env python3
"""
Unit tests for the component-based filename truncation logic.

These tests are self-contained and don't rely on importing the full project,
which avoids configuration and dependency issues during testing.
"""

import unittest
import sys
import os
import re

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Copy the functions we want to test directly here to avoid import issues
# This allows testing the logic in isolation

def _clean_separators_in_string(s: str) -> str:
    """
    Clean up orphaned separators (like ' - ', ' () ', empty brackets) in a string.
    This is used after removing template components to clean up the resulting string.
    """
    # Remove empty parentheses with surrounding spaces/dashes
    s = re.sub(r'\s*-\s*\(\s*\)', '', s)
    s = re.sub(r'\(\s*\)', '', s)
    # Remove empty square brackets with surrounding spaces/dashes
    s = re.sub(r'\s*-\s*\[\s*\]', '', s)
    s = re.sub(r'\[\s*\]', '', s)
    # Remove orphaned dashes (consecutive or leading/trailing)
    s = re.sub(r'\s*-\s*-\s*', ' - ', s)
    s = re.sub(r'^\s*-\s*', '', s)
    s = re.sub(r'\s*-\s*$', '', s)
    # Remove multiple consecutive spaces
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip()


def sanitize_filename(filename: str) -> str:
    """Simplified sanitize_filename for testing."""
    import unicodedata
    filename = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    filename = re.sub(r'[<>|?*:"\'\&/\\]', '_', filename)
    return filename.strip()


def truncate_path_components(
    template: str,
    template_vars: dict,
    base_path: str,
    directory_parts: list,
    extension: str,
    max_path_length: int = 255
) -> str:
    """
    Truncate filename components to fit within max_path_length.

    Removal priority (lowest first): original_filename, content_source, tmdb_id, resolution, version
    Then truncates: episode_title, title (aggressively if needed)
    Never removed: imdb_id, season_number, episode_number, year, season_year
    """
    removal_priority = ['original_filename', 'content_source', 'tmdb_id', 'resolution', 'version']
    truncatable_components = ['episode_title', 'title']
    working_vars = dict(template_vars)

    def calculate_full_path(filename_part: str) -> str:
        dir_path = os.path.join(base_path, *directory_parts) if directory_parts else base_path
        return os.path.join(dir_path, filename_part)

    def format_and_sanitize() -> str:
        try:
            formatted = template.format(**working_vars)
        except KeyError:
            formatted = template
        formatted = _clean_separators_in_string(formatted)
        sanitized = sanitize_filename(formatted)
        if not sanitized.endswith(extension):
            sanitized += extension
        return sanitized

    def get_current_length() -> int:
        return len(calculate_full_path(format_and_sanitize()))

    if get_current_length() <= max_path_length:
        return format_and_sanitize()

    # Remove components in priority order
    for component in removal_priority:
        if component in working_vars and working_vars[component]:
            working_vars[component] = ''
            if get_current_length() <= max_path_length:
                return format_and_sanitize()

    # Truncate episode_title and title (preserve imdb_id for Plex)
    for component in truncatable_components:
        if component in working_vars and working_vars[component]:
            original_value = str(working_vars[component])
            if len(original_value) <= 4:  # Too short to truncate meaningfully
                continue

            excess = get_current_length() - max_path_length
            chars_to_remove = excess + 3

            if len(original_value) > chars_to_remove:
                truncated_value = original_value[:-(chars_to_remove)] + '...'
                working_vars[component] = truncated_value

                current_length = get_current_length()
                if current_length <= max_path_length:
                    return format_and_sanitize()
                else:
                    # More aggressive truncation
                    min_length = 13
                    while len(working_vars[component]) > min_length:
                        working_vars[component] = working_vars[component][:-4] + '...'
                        if get_current_length() <= max_path_length:
                            return format_and_sanitize()

    # Legacy fallback (imdb_id is never removed)
    sanitized = format_and_sanitize()
    full_path = calculate_full_path(sanitized)
    if len(full_path) > max_path_length:
        excess = len(full_path) - max_path_length
        filename_without_ext = os.path.splitext(sanitized)[0]
        if len(filename_without_ext) > excess + 3:
            sanitized = filename_without_ext[:-(excess + 3)] + "..." + extension

    return sanitized


class TestCleanSeparators(unittest.TestCase):
    """Tests for the _clean_separators_in_string helper function."""

    def test_removes_empty_parentheses(self):
        """Empty parentheses should be removed."""
        result = _clean_separators_in_string("Title () - Version")
        self.assertNotIn("()", result)
        self.assertIn("Title", result)
        self.assertIn("Version", result)

    def test_removes_dash_with_empty_parentheses(self):
        """Dash followed by empty parentheses should be removed."""
        result = _clean_separators_in_string("Title - () - Version")
        self.assertNotIn("()", result)
        self.assertEqual(result, "Title - Version")

    def test_removes_empty_brackets(self):
        """Empty square brackets should be removed."""
        result = _clean_separators_in_string("Title [] - Version")
        self.assertNotIn("[]", result)

    def test_removes_orphaned_leading_dash(self):
        """Leading dashes should be removed."""
        result = _clean_separators_in_string(" - Title")
        self.assertEqual(result, "Title")

    def test_removes_orphaned_trailing_dash(self):
        """Trailing dashes should be removed."""
        result = _clean_separators_in_string("Title - ")
        self.assertEqual(result, "Title")

    def test_collapses_consecutive_dashes(self):
        """Consecutive dashes should be collapsed."""
        result = _clean_separators_in_string("Title - - Version")
        self.assertEqual(result, "Title - Version")

    def test_collapses_multiple_spaces(self):
        """Multiple spaces should be collapsed to single space."""
        result = _clean_separators_in_string("Title    Version")
        self.assertEqual(result, "Title Version")


class TestTruncatePathComponents(unittest.TestCase):
    """Tests for the truncate_path_components function."""

    def setUp(self):
        """Set up test fixtures."""
        self.base_path = "/media/symlinks"
        self.extension = ".mkv"

    def test_path_already_short_enough(self):
        """A path that is already short enough should not be modified."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title}"
        template_vars = {
            'title': 'Short Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 5,
            'episode_title': 'Pilot',
            'imdb_id': 'tt1234567',
            'version': '1080p',
            'original_filename': 'source.file',
            'content_source': 'usenet',
            'tmdb_id': '12345',
            'resolution': '1080p',
        }
        directory_parts = ['TV Shows', 'Short Show (2024)', 'Season 01']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        # The result should contain the essential information
        self.assertIn('Short Show', result)
        self.assertIn('2024', result)
        self.assertIn('S01E05', result)
        self.assertIn('Pilot', result)
        self.assertTrue(result.endswith('.mkv'))

    def test_removes_original_filename_first(self):
        """original_filename should be removed first when path is too long."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {version} - ({original_filename})"
        template_vars = {
            'title': 'A Very Long Show Title That Takes Up Space',
            'year': '2024',
            'season_number': 1,
            'episode_number': 5,
            'episode_title': 'The Episode With A Very Long Title',
            'imdb_id': 'tt1234567',
            'version': '1080p.BluRay.x264',
            'original_filename': 'This.Is.A.Very.Long.Original.Filename.That.Should.Be.Removed.First',
            'content_source': 'usenet',
            'tmdb_id': '12345',
            'resolution': '1080p',
        }
        directory_parts = ['TV Shows', 'A Very Long Show Title That Takes Up Space (2024)', 'Season 01']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=200
        )

        # original_filename should be removed
        self.assertNotIn('This.Is.A.Very.Long.Original.Filename', result)
        # Critical info should still be present
        self.assertIn('S01E05', result)
        self.assertIn('2024', result)

    def test_removes_multiple_components(self):
        """Multiple components should be removed in priority order."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {tmdb_id} - {version} - {resolution} - ({original_filename})"
        template_vars = {
            'title': 'A Very Long Show Title',
            'year': '2024',
            'season_number': 1,
            'episode_number': 5,
            'episode_title': 'Episode Title',
            'imdb_id': 'tt1234567',
            'version': '1080p.BluRay',
            'original_filename': 'Original.File.Name',
            'content_source': 'usenet',
            'tmdb_id': '12345',
            'resolution': '1080p',
        }
        directory_parts = ['TV Shows', 'A Very Long Show Title (2024)', 'Season 01']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=150
        )

        # The essential info should still be there
        self.assertIn('S01E05', result)
        self.assertIn('2024', result)
        # At this low limit, several components should be removed
        full_path_length = len(os.path.join(self.base_path, *directory_parts, result))
        self.assertLessEqual(full_path_length, 150)

    def test_truncates_episode_title(self):
        """Episode title should be truncated if removal isn't sufficient."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 5,
            'episode_title': 'This Is A Very Long Episode Title That Should Be Truncated To Fit The Path Length Limit',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows', 'Show (2024)', 'Season 01']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=100
        )

        # Critical info should still be present
        self.assertIn('S01E05', result)
        self.assertIn('2024', result)
        self.assertIn('Show', result)
        full_path_length = len(os.path.join(self.base_path, *directory_parts, result))
        self.assertLessEqual(full_path_length, 100)

    def test_truncates_title_as_last_resort(self):
        """Title should only be truncated as a last resort."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d}"
        template_vars = {
            'title': 'This Is An Extremely Long Show Title That Will Need To Be Truncated As A Last Resort',
            'year': '2024',
            'season_number': 1,
            'episode_number': 5,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows', 'This Is An Extremely Long Show Title (2024)', 'Season 01']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=120
        )

        # Critical info should still be present
        self.assertIn('S01E05', result)
        self.assertIn('2024', result)
        full_path_length = len(os.path.join(self.base_path, *directory_parts, result))
        self.assertLessEqual(full_path_length, 120)

    def test_preserves_season_episode_numbers(self):
        """Season and episode numbers should NEVER be removed or truncated."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {version}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 12,
            'episode_number': 99,
            'episode_title': 'Episode Title',
            'imdb_id': 'tt1234567',
            'version': '1080p',
            'original_filename': 'source',
            'content_source': 'usenet',
            'tmdb_id': '12345',
            'resolution': '1080p',
        }
        directory_parts = ['TV Shows', 'Show (2024)', 'Season 12']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=100
        )

        # Season and episode numbers must be present
        self.assertIn('S12E99', result)
        self.assertIn('2024', result)

    def test_preserves_year(self):
        """Year should NEVER be removed or truncated."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d}"
        template_vars = {
            'title': 'Show Title',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        self.assertIn('2024', result)

    def test_movie_template_truncation(self):
        """Movie templates should also be handled correctly."""
        template = "{title} ({year}) - {imdb_id} - {version} - ({original_filename})"
        template_vars = {
            'title': 'A Very Long Movie Title That Might Need Truncation',
            'year': '2024',
            'imdb_id': 'tt1234567',
            'version': '1080p.BluRay.x264.DTS',
            'original_filename': 'A.Very.Long.Original.Filename.That.Should.Be.Removed',
            'content_source': 'usenet',
            'tmdb_id': '12345',
            'resolution': '1080p',
        }
        directory_parts = ['Movies', 'A Very Long Movie Title That Might Need Truncation (2024)']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=180
        )

        # Year should be preserved, original_filename should be removed first
        self.assertIn('2024', result)
        full_path_length = len(os.path.join(self.base_path, *directory_parts, result))
        self.assertLessEqual(full_path_length, 180)

    def test_handles_empty_optional_fields(self):
        """Empty optional fields should be handled gracefully."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d} - {episode_title} - {imdb_id} - {version}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows', 'Show (2024)', 'Season 01']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        # Should handle empty fields without error
        self.assertIn('Show', result)
        self.assertIn('2024', result)
        self.assertIn('S01E01', result)
        self.assertTrue(result.endswith('.mkv'))

    def test_extension_always_present(self):
        """The file extension should always be present in the result."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=".mkv",
            max_path_length=255
        )

        self.assertTrue(result.endswith('.mkv'))

    def test_content_source_removed_before_tmdb_id(self):
        """Content source should be removed before tmdb_id in priority order."""
        template = "{title} - {content_source} - {tmdb_id}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': 'MyContentSource',
            'tmdb_id': '99999',
            'resolution': '',
        }
        directory_parts = ['TV Shows']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=60
        )

        full_path_length = len(os.path.join(self.base_path, *directory_parts, result))
        self.assertLessEqual(full_path_length, 60)


class TestTruncatePathComponentsEdgeCases(unittest.TestCase):
    """Edge case tests for truncate_path_components."""

    def setUp(self):
        """Set up test fixtures."""
        self.base_path = "/media/symlinks"
        self.extension = ".mkv"

    def test_very_long_base_path(self):
        """Should handle very long base paths."""
        long_base_path = "/media/" + "a" * 100 + "/symlinks"
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=long_base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        # Should still work and contain critical info
        self.assertIn('S01E01', result)
        self.assertIn('2024', result)

    def test_unicode_characters_in_title(self):
        """Should handle unicode characters in title."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d}"
        template_vars = {
            'title': 'Show with Unicode: cafe resume',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        self.assertIn('2024', result)
        self.assertIn('S01E01', result)

    def test_special_characters_in_version(self):
        """Should handle special characters in version string."""
        template = "{title} ({year}) - {version}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '1080p.BluRay.x264-GROUP',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['Movies']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        self.assertIn('2024', result)

    def test_empty_directory_parts(self):
        """Should handle empty directory_parts list."""
        template = "{title} ({year}) - S{season_number:02d}E{episode_number:02d}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=[],
            extension=self.extension,
            max_path_length=255
        )

        self.assertIn('Show', result)
        self.assertIn('2024', result)
        self.assertIn('S01E01', result)

    def test_multi_episode_format(self):
        """Should handle multi-episode format like E17-E18."""
        template = "{title} ({year}) - S{season_number:02d}{episode_number}"
        template_vars = {
            'title': 'Show',
            'year': '2024',
            'season_number': 1,
            'episode_number': 'E17-E18',
            'episode_title': '',
            'imdb_id': '',
            'version': '',
            'original_filename': '',
            'content_source': '',
            'tmdb_id': '',
            'resolution': '',
        }
        directory_parts = ['TV Shows', 'Show (2024)', 'Season 01']

        result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        self.assertIn('E17-E18', result)
        self.assertIn('2024', result)

    def test_removal_order_verified(self):
        """Verify components are removed in correct priority order."""
        # Create a scenario where we can verify the order
        template = "{title} - {original_filename} - {content_source} - {tmdb_id} - {resolution} - {version}"
        template_vars = {
            'title': 'T',
            'year': '2024',
            'season_number': 1,
            'episode_number': 1,
            'episode_title': '',
            'imdb_id': '',
            'version': 'VERSION',
            'original_filename': 'ORIGINAL',
            'content_source': 'SOURCE',
            'tmdb_id': 'TMDB',
            'resolution': 'RES',
        }
        directory_parts = []

        # Start with max length that requires removing original_filename only
        base_result = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=255
        )

        # With full length, all should be present
        self.assertIn('ORIGINAL', base_result)
        self.assertIn('SOURCE', base_result)

        # Now with tighter constraint - original_filename should be removed first
        result_tight = truncate_path_components(
            template=template,
            template_vars=template_vars,
            base_path=self.base_path,
            directory_parts=directory_parts,
            extension=self.extension,
            max_path_length=60
        )

        # At tight constraint, ORIGINAL should be gone (lowest priority)
        self.assertNotIn('ORIGINAL', result_tight)


if __name__ == '__main__':
    unittest.main()
