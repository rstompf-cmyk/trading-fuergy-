"""VDT-ZERO-PRICE writer guard: VDT paper trade sa zapíše LEN s reálnou cenou.

Koreň bugu (VW_simulacia_3, 13:30): DAM nabíjanie bez VDT ceny sa zalogovalo ako
VDT buy @ price 0.00 → engine ho aplikoval navyše k DAM = double-count SOC =
porušenie SOC. Fix: _vdt_price_is_real odmietne None/0.0/NaN/inf (záporná OK).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vdt_live_advisor import _vdt_price_is_real


def test_zero_and_invalid_rejected():
    assert _vdt_price_is_real(0.0) is False        # placeholder = neplatná
    assert _vdt_price_is_real(0) is False
    assert _vdt_price_is_real(None) is False
    assert _vdt_price_is_real("") is False
    assert _vdt_price_is_real(float("nan")) is False
    assert _vdt_price_is_real(float("inf")) is False
    assert _vdt_price_is_real(float("-inf")) is False


def test_real_prices_accepted():
    assert _vdt_price_is_real(100.0) is True
    assert _vdt_price_is_real(0.01) is True
    assert _vdt_price_is_real(364.68) is True       # reálny VDT predaj
    assert _vdt_price_is_real(-30.0) is True         # záporná cena PLATNÁ (prebytok)
    assert _vdt_price_is_real("245.11") is True      # string z CSV


if __name__ == "__main__":
    test_zero_and_invalid_rejected()
    test_real_prices_accepted()
    print("✓ VDT-ZERO-PRICE writer guard OK")
