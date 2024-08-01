import unittest
from typing import List, Dict, Any
import json
import os

# Import your scraper functions here
from utilities.manual_scrape import run_manual_scrape, scrape_sync, search_overseerr, get_details

class ScraperTestCase(unittest.TestCase):
    def setUp(self):
        # Set up test cases
        self.test_cases = [
            "The Matrix 1999",
            "Stranger Things S01E01",
            "Inception 2010",
            "Breaking Bad S05E14",
            "Pulp Fiction 1994",
            # Add more test cases as needed
        ]

    def display_results(self, results: List[Dict[str, Any]], limit: int = 10):
        print("\nTop {} results:".format(limit))
        for idx, result in enumerate(results[:limit], 1):
            title = result.get('title', 'N/A')
            year = result.get('year', 'N/A')
            media_type = result.get('mediaType', 'N/A')
            print(f"{idx}. {title} ({year}) - Type: {media_type}")

    def test_search_results(self):
        for search_term in self.test_cases:
            with self.subTest(search_term=search_term):
                print(f"\nTesting search term: {search_term}")
                
                # Get search results
                search_results = search_overseerr(search_term)
                
                if search_results:
                    self.display_results(search_results)
                    
                    # Allow user to select a result
                    selection = input("Enter the number of the result you want to select (or 'skip'): ")
                    if selection.lower() != 'skip':
                        selected_index = int(selection) - 1
                        selected_result = search_results[selected_index]
                        
                        # Get details for the selected result
                        details = get_details(selected_result)
                        
                        if details:
                            print("\nSelected result details:")
                            print(json.dumps(details, indent=2))
                            
                            # Manual rating of the search result
                            rating = input("Rate the accuracy of this search result (1-5, or 'skip'): ")
                            if rating.lower() != 'skip':
                                self.assertTrue(1 <= int(rating) <= 5, "Rating should be between 1 and 5")
                            
                            notes = input("Any notes about this search result? (Press Enter to skip): ")
                            if notes:
                                print(f"Notes: {notes}")
                        else:
                            print("Could not fetch details for the selected result.")
                else:
                    self.fail(f"No results found for search term: {search_term}")

    def test_scrape_sync(self):
        for search_term in self.test_cases:
            with self.subTest(search_term=search_term):
                print(f"\nTesting scrape for: {search_term}")
                
                # Get search results
                search_results = search_overseerr(search_term)
                
                if search_results:
                    self.display_results(search_results)
                    
                    # Allow user to select a result
                    selection = input("Enter the number of the result you want to scrape (or 'skip'): ")
                    if selection.lower() != 'skip':
                        selected_index = int(selection) - 1
                        selected_result = search_results[selected_index]
                        
                        # Get details for the selected result
                        details = get_details(selected_result)
                        
                        if details:
                            # Use the details to run scrape_sync
                            scrape_result = scrape_sync(
                                details.get('externalIds', {}).get('imdbId', ''),
                                str(details.get('id', '')),
                                details.get('title') or details.get('name', ''),
                                (details.get('releaseDate') or details.get('firstAirDate', ''))[:4],
                                'movie' if selected_result['mediaType'] == 'movie' else 'episode',
                                details.get('season'),
                                details.get('episode'),
                                'false'
                            )

                            # Display the scrape results
                            self.display_results(scrape_result)

                            # Manual rating of the scrape results
                            rating = input("Rate the quality of these scrape results (1-5, or 'skip'): ")
                            if rating.lower() != 'skip':
                                self.assertTrue(1 <= int(rating) <= 5, "Rating should be between 1 and 5")
                            
                            notes = input("Any notes about these scrape results? (Press Enter to skip): ")
                            if notes:
                                print(f"Notes: {notes}")
                        else:
                            print("Could not fetch details for the selected result.")
                else:
                    self.fail(f"No search results found for term: {search_term}")

    def tearDown(self):
        # Clean up any resources if needed
        pass

if __name__ == '__main__':
    unittest.main()
