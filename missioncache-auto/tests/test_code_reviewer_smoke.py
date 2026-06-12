"""Import-smoke tests for ``missioncache_auto.code_reviewer``.

The module is wired in only when ``--enable-review`` / ``--spec-review-only``
/ ``--tdd`` is passed, so it is invisible to the rest of the test suite
and any botched rename of an imported symbol would silently survive.

These tests force import-time resolution of its dependencies (importing
``code_reviewer`` triggers ``from missioncache_auto.claude_runner import
ClaudeRunner`` and ``from missioncache_auto.models import Visibility``) and pin
the public surface so the rename sweep cannot quietly delete or rename
one of the three review entrypoints.
"""

import missioncache_auto.code_reviewer as code_reviewer


class TestCodeReviewerImport:
    def test_module_imports_cleanly(self):
        """Just importing the module must not raise.

        This is the actual coverage gap: the file was never imported by
        any other test, so an import-time NameError from a missed rename
        would never surface in CI.
        """
        assert code_reviewer is not None

    def test_public_review_entrypoints_exist(self):
        """The three public review functions are the contract surface.

        ``parallel.py`` / ``sequential.py`` call them by name when the
        review flags are set; renaming any of them silently breaks the
        review pipeline. Pin the names.
        """
        assert callable(code_reviewer.run_tdd_review)
        assert callable(code_reviewer.run_spec_review)
        assert callable(code_reviewer.run_quality_review)

    def test_claude_runner_dependency_resolved(self):
        """The ``from missioncache_auto.claude_runner import ClaudeRunner`` line
        at the top of code_reviewer.py executes at import time. If
        ``ClaudeRunner`` is ever renamed without updating this import,
        the smoke test above already fails - this test just makes the
        actual dependency explicit."""
        from missioncache_auto.claude_runner import ClaudeRunner

        assert ClaudeRunner is not None

    def test_visibility_dependency_resolved(self):
        """Same idea for ``Visibility`` - it is imported at module top
        and passed into every ``ClaudeRunner(visibility=Visibility.NONE)``
        call inside the review functions."""
        from missioncache_auto.models import Visibility

        assert Visibility.NONE in tuple(Visibility)
