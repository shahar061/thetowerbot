import pytest

import upgrades


@pytest.mark.parametrize("label", ["Unlock Interest / Wave", "Unlock Interest Per Wave"])
def test_recovery_prerequisite_uses_known_unlock_instead_of_discovered_row(label: str) -> None:
    row = upgrades.resolve(label, "UTILITY")
    assert row is not None
    assert row.id == "unlock_interest"
    assert row.unlock
    assert row.unlocks == ("interest_per_wave",)


def test_observed_singular_recovery_package_unlock_uses_known_identity() -> None:
    row = upgrades.resolve("Unlock Recovery Package", "UTILITY")
    assert row is not None
    assert row.id == "unlock_recovery_packages"
    assert row.unlock
    assert row.unlocks == ("recovery_amount", "max_recovery", "package_chance")
