#!/usr/bin/env python3
"""
Test runner for filter_results unit tests.
Run this script to execute all tests for the filter_results function.
"""

import sys
import os
import unittest

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def run_tests():
    """Run all tests in the tests directory."""
    # Discover and run all tests
    loader = unittest.TestLoader()
    start_dir = os.path.dirname(os.path.abspath(__file__))
    suite = loader.discover(start_dir, pattern='test_*.py')
    
    # Create a test runner
    runner = unittest.TextTestRunner(verbosity=2)
    
    # Run the tests
    result = runner.run(suite)
    
    # Return exit code based on test results
    return 0 if result.wasSuccessful() else 1

def run_specific_test(test_class_name=None, test_method_name=None):
    """Run a specific test class or method."""
    if test_class_name:
        # Import the specific test class
        from test_filter_results import TestFilterResultsAnimeXEM
        
        if test_method_name:
            # Run specific test method
            suite = unittest.TestSuite()
            suite.addTest(TestFilterResultsAnimeXEM(test_method_name))
        else:
            # Run all methods in the test class
            suite = unittest.TestLoader().loadTestsFromTestCase(TestFilterResultsAnimeXEM)
    else:
        # Run all tests
        return run_tests()
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Run filter_results unit tests')
    parser.add_argument('--test-class', help='Run specific test class')
    parser.add_argument('--test-method', help='Run specific test method')
    parser.add_argument('--list-tests', action='store_true', help='List all available tests')
    
    args = parser.parse_args()
    
    if args.list_tests:
        # List all available tests
        from test_filter_results import TestFilterResultsAnimeXEM
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromTestCase(TestFilterResultsAnimeXEM)
        
        print("Available tests:")
        for test in suite:
            print(f"  {test}")
        sys.exit(0)
    
    if args.test_class or args.test_method:
        exit_code = run_specific_test(args.test_class, args.test_method)
    else:
        exit_code = run_tests()
    
    sys.exit(exit_code)
