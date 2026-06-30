"""Fixture: error definitions for harvest_errors testing."""
import sys


class FooError(Exception):
    """Foo failed."""


def do_something(value):
    if value < 0:
        sys.exit(2)
    return value
