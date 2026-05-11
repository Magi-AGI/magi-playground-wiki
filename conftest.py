"""Pytest configuration: register the `live` marker for sandbox-touching tests."""

from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "live: tests that spawn a real Docker container (requires daemon + hyperon-runtime:0.2.10 image)",
    )


def pytest_collection_modifyitems(config, items) -> None:
    if config.getoption("-m") and "live" in config.getoption("-m"):
        return
    import pytest

    skip_live = pytest.mark.skip(reason="skipping live tests; pass `-m live` to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
