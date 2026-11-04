#!/usr/bin/env python3
"""Run the bots service test suite (standard library only, no pytest).

  python3 scripts/test-bots.py            # unit tests against the fake world
  python3 scripts/test-bots.py -v         # verbose
  Only top-level tests/test_*.py files are part of this service suite.
"""
import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT_PACKAGE = ROOT / 'containers' / 'bots'
TESTS_DIR = ROOT / 'tests'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()

    # The service package and the shared test helpers share the tests/ directory
    # import style, so both paths must exist before discovery imports anything.
    sys.path.insert(0, str(TESTS_DIR))
    sys.path.insert(0, str(BOT_PACKAGE))

    loader = unittest.TestLoader()
    loader.sortTestMethodsUsing = None  # deterministic order for logs
    # Other experiments may have their own test packages under tests/. They
    # are not part of the bot service or its container image.
    suite = unittest.TestSuite(loader.loadTestsFromName(path.stem)
                               for path in sorted(TESTS_DIR.glob('test_*.py')))
    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else (0 if args.quiet else 1))
    result = runner.run(suite)
    print(f'bots tests: {result.testsRun} run, '
          f'{len(result.failures)} failures, {len(result.errors)} errors')
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
