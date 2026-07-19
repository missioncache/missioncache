"""Tests for the statusline's MissionCache update indicator.

Spec source: the update-discovery contract - the statusline imports the
in-package update_check module (guarded for the legacy bare-script
invocation), and renders an upgrade cell on the Vitals line only when
update_available is true.
"""

import missioncache_dashboard.statusline as mod


def test_update_check_import_resolves_in_package_context():
    """The guarded import must actually bind when run as a package (the
    missioncache-statusline entry point) - a silent fallback to None would
    disable the feature everywhere."""
    assert mod._update_check is not None


def test_upgrade_cell_carries_link_and_label():
    """The assembled cell (mirroring main()'s mc_update block) links the
    label to the changelog."""
    link = mod._osc8_link(
        "https://missioncache.dev/changelog/", "MissionCache update available"
    )
    cell = f"{mod.COLORS['upgrade']}⬆ {link}{mod.RESET}"
    assert "MissionCache update available" in cell
    assert "https://missioncache.dev/changelog/" in cell
