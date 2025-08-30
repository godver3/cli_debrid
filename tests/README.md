# Filter Results Unit Tests

This directory contains comprehensive unit tests for the `filter_results.py` function, specifically focusing on anime and XEM mappings with various episode formats.

## Test Coverage

The tests cover the following scenarios:

### Anime Episode Formats
- **Absolute Format**: Episode numbers like `125` (absolute episode number)
- **Regular Format**: Standard season/episode format like `S02E05`
- **Combined Format**: Mixed format like `S15E520` where 520 is the absolute episode number

### XEM Mapping Scenarios
- Absolute episode fallback when S/E doesn't match
- Original episode fallback for XEM mapped episodes
- Absolute episode calculation for proper XEM mapping
- Multiple format handling in mixed result sets

### Anime-Specific Features
- Season pack detection and filtering
- Heuristic pack detection for titles without explicit indicators
- Similarity threshold enforcement for anime content
- Sanity checks to prevent false matches
- Leniency rules for titles without season info

## Running the Tests

### Prerequisites
Make sure you have the required dependencies installed:
```bash
pip install fuzzywuzzy python-Levenshtein
```

### Run All Tests
```bash
cd tests
python run_tests.py
```

### Run Specific Test Class
```bash
python run_tests.py --test-class TestFilterResultsAnimeXEM
```

### Run Specific Test Method
```bash
python run_tests.py --test-class TestFilterResultsAnimeXEM --test-method test_anime_absolute_episode_format
```

### List All Available Tests
```bash
python run_tests.py --list-tests
```

### Using Python's unittest directly
```bash
python -m unittest test_filter_results.TestFilterResultsAnimeXEM
```

## Test Structure

### Test Data Setup
Each test uses a common setup with:
- Mock scraped results with various episode formats
- Realistic anime titles and metadata
- Proper PTT parsing simulation
- Mock DirectAPI for external service calls

### Mock Results
The tests create mock results that simulate real scraped torrent data:
- Different episode numbering schemes
- Various title formats
- Realistic file sizes and metadata
- Proper season/episode information

### Test Scenarios

#### 1. Absolute Episode Format
Tests anime releases that use absolute episode numbers (e.g., "Test.Anime.125.1080p.WEB-DL")

#### 2. Regular Season/Episode Format
Tests standard S/E format (e.g., "Test.Anime.S02E05.1080p.WEB-DL")

#### 3. Combined Format
Tests mixed formats where S/E contains absolute episode numbers (e.g., "Test.Anime.S15E520.1080p.WEB-DL")

#### 4. XEM Mapping Fallbacks
Tests various fallback mechanisms when XEM mapping is involved:
- Absolute episode fallback
- Original episode fallback
- Proper absolute episode calculation

#### 5. Pack Detection
Tests season pack detection and filtering:
- Multi-mode acceptance of packs
- Single-mode rejection of packs
- Heuristic pack detection

#### 6. Anime-Specific Features
Tests anime-specific filtering logic:
- Similarity thresholds
- Sanity checks
- Season info leniency rules

## Expected Behavior

### Accepted Results
- Exact S/E matches
- Absolute episode matches with proper XEM mapping
- Season packs in multi-mode
- Heuristic packs with appropriate indicators

### Rejected Results
- Season packs in single mode
- Mismatched episode numbers
- Low similarity scores
- Failed sanity checks
- Inappropriate season leniency for S2+

## Debugging Tests

If tests fail, check:
1. **Import paths**: Ensure the project root is in the Python path
2. **Dependencies**: Verify all required packages are installed
3. **Mock data**: Check that mock results match expected format
4. **Filter logic**: Review the actual filter_results function for recent changes

## Adding New Tests

To add new test cases:

1. Add a new test method to `TestFilterResultsAnimeXEM`
2. Use the `create_mock_result` helper method
3. Set up appropriate test data and expected outcomes
4. Run the specific test to verify it works

Example:
```python
def test_new_anime_format(self):
    """Test new anime episode format."""
    results = [
        self.create_mock_result(
            "Test.Anime.NewFormat.1080p.WEB-DL",
            parsed_info={
                # ... test data
            }
        )
    ]
    
    filtered_results, pre_size_filtered = filter_results(
        # ... parameters
    )
    
    self.assertEqual(len(filtered_results), 1, "Should accept new format")
```

## Notes

- Tests use mock objects to avoid external dependencies
- Logging is configured to ERROR level to reduce noise
- Tests are designed to be independent and repeatable
- Each test focuses on a specific aspect of the filtering logic
