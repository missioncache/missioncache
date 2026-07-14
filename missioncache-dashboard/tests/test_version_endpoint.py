"""Tests for the dashboard version surface.

Calls the endpoint function directly (no TestClient / lifespan boot),
mirroring test_category_endpoint.py.

Note: __version__ comes from the INSTALLED distribution metadata, not from
the source pyproject.toml - under `make test` (PYTHONPATH, no install of the
tree) those can differ. So the runtime assertions here compare against
__version__ itself, never against a literal. That keeps them green in any
environment, but it also means they cannot catch someone re-hardcoding the
literal that happens to match the installed metadata. That specific mutant is
what test_version_is_never_hardcoded exists for: it reads the source, so it
does not depend on what is installed.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

from missioncache_dashboard import __version__, server


def test_version_endpoint_returns_the_package_version():
    assert asyncio.run(server.get_version()) == {"version": __version__}


def test_fastapi_app_version_is_the_package_version():
    """Regression: server.py hardcoded version="2.0.0", which drifted away
    from the real package version and surfaced stale in OpenAPI / /docs."""
    assert server.app.version == __version__


def test_version_is_never_hardcoded():
    """The runtime assertions above compare __version__ to itself, so they
    would still pass if someone wrote `version="1.0.1"` back into server.py
    while that happened to match the installed dist. This one reads the
    source instead, so it kills that mutant regardless of the environment."""
    src = pathlib.Path(server.__file__).read_text()
    literal = re.search(r'version\s*=\s*["\']\d+\.\d+', src)
    assert literal is None, f"version is hardcoded in server.py: {literal.group(0)}"


# The three JIRA anchors are built inside JS template literals and predate the
# rel=noopener convention. They are known debt, not an accepted pattern: listing
# them here keeps the invariant below enforced for every OTHER anchor, and makes
# the remaining work visible instead of silently excluded.
KNOWN_MISSING_REL = (
    "jira-link",
)


def test_every_external_blank_anchor_carries_rel_noopener():
    """Enforce the invariant as a CLASS, not as two hardcoded hrefs.

    Scoping this to only the two new sidebar links would lock two strings and
    give the next external anchor no protection at all.
    """
    body = asyncio.run(server.serve_dashboard()).body.decode()
    offenders = [
        tag
        for tag in re.findall(r'<a\b[^>]*target="_blank"[^>]*>', body)
        if not any(known in tag for known in KNOWN_MISSING_REL)
        and 'rel="noopener' not in tag
    ]
    assert not offenders, f"external anchors missing rel=noopener: {offenders}"


def test_sidebar_external_links_are_present():
    """The Website + Changelog links are the dashboard's first outbound links.
    Guard that they exist at all, so the invariant test above cannot pass
    vacuously by them having been deleted."""
    body = asyncio.run(server.serve_dashboard()).body.decode()
    for href in ("https://missioncache.dev", "https://missioncache.dev/changelog/"):
        anchor = re.search(rf'<a\b[^>]*href="{re.escape(href)}"[^>]*>', body)
        assert anchor, f"sidebar link to {href} is missing"
        assert 'rel="noopener' in anchor.group(0), f"{href} is missing rel=noopener"
