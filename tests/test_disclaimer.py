"""Disclaimer tests: the first-start dialog and the acceptance record."""
from __future__ import annotations

import pytest

from src.version import DISCLAIMER_VERSION


@pytest.fixture()
def bridge(isolated_home):
    from src.gui.bridge import GuiBridge

    return GuiBridge()


def test_the_disclaimer_is_shown_on_a_fresh_install(bridge):
    """A new user must see the disclaimer before anything else."""
    status = bridge.get_disclaimer_status()
    assert status["required"] is True
    assert status["version"] == DISCLAIMER_VERSION
    assert status["accepted_version"] == ""


def test_accepting_records_the_version(bridge):
    result = bridge.accept_disclaimer()
    assert result["success"] is True
    assert result["version"] == DISCLAIMER_VERSION
    status = bridge.get_disclaimer_status()
    assert status["required"] is False
    assert status["accepted_version"] == DISCLAIMER_VERSION


def test_the_acceptance_survives_a_restart(bridge, isolated_home):
    """The dialog must not come back on every start."""
    from src.config import get_settings

    bridge.accept_disclaimer()
    get_settings().save()
    # A fresh Settings instance reads the same file.
    from src.config.settings import Settings

    Settings._instance = None
    reloaded = Settings()
    assert reloaded.ui.disclaimer_accepted_version == DISCLAIMER_VERSION
    Settings._instance = None


def test_a_new_disclaimer_version_is_shown_again(bridge, monkeypatch):
    """Bumping DISCLAIMER_VERSION re-shows the dialog after an update."""
    import src.gui.bridge as bridge_module

    bridge.accept_disclaimer()
    monkeypatch.setattr(bridge_module, "DISCLAIMER_VERSION", "2")
    status = bridge.get_disclaimer_status()
    assert status["required"] is True
    assert status["version"] == "2"


def test_quit_app_is_reachable(bridge):
    assert bridge.quit_app()["success"] is True
