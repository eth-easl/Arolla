#!/usr/bin/env python3
"""
Run all unit tests for ms_simulator.

This script runs all test files in the tests/ directory.
"""

import sys
import subprocess
from pathlib import Path


def run_test_file(test_file):
    """Run a single test file and return success status"""
    print(f"\n{'='*70}")
    print(f"Running {test_file.name}...")
    print('='*70)
    
    result = subprocess.run(
        [sys.executable, str(test_file)],
        capture_output=False
    )
    
    return result.returncode == 0


def main():
    # Find all test files recursively
    tests_dir = Path(__file__).parent
    test_files = sorted(tests_dir.rglob("test_*.py"))  # Recursive glob
    
    if not test_files:
        print("No test files found!")
        return 1
    
    print(f"Found {len(test_files)} test files")
    
    # Run all tests
    results = {}
    for test_file in test_files:
        success = run_test_file(test_file)
        results[test_file.name] = success
    
    # Print summary
    print(f"\n{'='*70}")
    print("TEST SUMMARY")
    print('='*70)
    
    passed = sum(1 for success in results.values() if success)
    failed = len(results) - passed
    
    for test_name, success in results.items():
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    print(f"\n{passed}/{len(results)} test files passed")
    
    if failed > 0:
        print(f"\n❌ {failed} test file(s) failed")
        return 1
    else:
        print("\n✅ All tests passed!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
