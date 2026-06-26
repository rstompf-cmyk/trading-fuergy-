# -*- coding: utf-8 -*-
"""Test sviatkového kalendára SK/CZ + is_holiday feature v cenových modeloch."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.holidays_skcz import is_holiday, holidays_for_year, _easter_sunday
from datetime import date


def test_easter_known_years():
    # overené dátumy Veľkonočnej nedele (gregoriánsky kalendár)
    assert _easter_sunday(2026) == date(2026, 4, 5)
    assert _easter_sunday(2025) == date(2025, 4, 20)
    assert _easter_sunday(2024) == date(2024, 3, 31)


def test_movable_holidays():
    # Veľký piatok = Easter−2, Veľkonočný pondelok = Easter+1
    assert is_holiday("2026-04-03", "sk")   # Veľký piatok
    assert is_holiday("2026-04-06", "cz")   # Veľkonočný pondelok
    assert is_holiday("2026-04-03", "cz")


def test_fixed_common():
    for mk in ("sk", "cz"):
        assert is_holiday("2026-01-01", mk)   # Nový rok
        assert is_holiday("2026-05-01", mk)   # Sviatok práce
        assert is_holiday("2026-05-08", mk)   # Deň víťazstva
        assert is_holiday("2026-12-24", mk)
        assert is_holiday("2026-12-25", mk)
        assert is_holiday("2026-12-26", mk)


def test_market_specific():
    assert is_holiday("2026-08-29", "sk") and not is_holiday("2026-08-29", "cz")   # SNP len SK
    assert is_holiday("2026-09-01", "sk") and not is_holiday("2026-09-01", "cz")   # Ústava SK
    assert is_holiday("2026-09-28", "cz") and not is_holiday("2026-09-28", "sk")   # sv. Václav CZ
    assert is_holiday("2026-10-28", "cz") and not is_holiday("2026-10-28", "sk")   # vznik ČSR CZ


def test_workday_not_holiday():
    assert not is_holiday("2026-06-24", "sk")
    assert not is_holiday("2026-06-24", "cz")


def test_counts():
    assert len(holidays_for_year(2026, "sk")) == 15
    assert len(holidays_for_year(2026, "cz")) == 13


def test_hourly_model_has_is_holiday():
    from price_model import FEATURES, LEGACY_FEATURES
    assert "is_holiday" in FEATURES
    assert "is_holiday" not in LEGACY_FEATURES
    assert len(FEATURES) == len(LEGACY_FEATURES) + 1


def test_15m_model_has_is_holiday():
    from price_model_15m import FEAT, LEGACY_FEAT
    assert "is_holiday" in FEAT
    assert "is_holiday" not in LEGACY_FEAT


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
