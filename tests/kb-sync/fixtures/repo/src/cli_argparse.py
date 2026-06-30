"""Fixture: argparse CLI with a main parser and two subparsers (run, check).

Used by test_harvest.py to verify harvest_argparse extracts 2 items with the
expected command names and flags. Not intended to be executed.
"""
import argparse


def main():
    parser = argparse.ArgumentParser(
        description="Fixture CLI tool for kb-sync harvest tests."
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the pipeline")
    run_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    run_parser.add_argument("--verbose", action="store_true", help="Verbose output")

    check_parser = subparsers.add_parser("check", help="Check configuration")
    check_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    check_parser.add_argument("--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()


if __name__ == "__main__":
    main()
