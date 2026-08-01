"""Shared pytest configuration."""

import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

def pytest_configure():
    """Force deterministic address parsing before test collection."""
    import address

    address.LIBPOSTAL_AVAILABLE = False
    address._PATTERNS_CACHE = address._load_patterns(
        str(Path(__file__).parent / "config" / "address_patterns.json")
    )
