"""core/granularity — 60↔15 min helpery (15-min canonical, hodinový = priemer)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.granularity import (upsample_price_h_to_15, upsample_series_h_to_15,
                              upsample_mask_h_to_15, resample_15_to_h_mean,
                              resample_15_to_h_sum, resample_15_to_h_last)


def test_price_upsample_copies_not_divides():
    ph = [100.0] * 24
    p15 = upsample_price_h_to_15(ph)
    assert len(p15) == 96
    assert all(abs(x - 100.0) < 1e-9 for x in p15)   # cena sa KOPÍRUJE, nie delí


def test_energy_upsample_divides():
    eh = [4.0] * 24
    e15 = upsample_series_h_to_15(eh, divide=True)
    assert len(e15) == 96 and all(abs(x - 1.0) < 1e-9 for x in e15)


def test_mask_upsample():
    m = upsample_mask_h_to_15([1, 0] + [1] * 22)
    assert len(m) == 96 and m[0:4] == [1, 1, 1, 1] and m[4:8] == [0, 0, 0, 0]


def test_resample_mean_roundtrip():
    ph = list(range(24))
    p15 = upsample_price_h_to_15(ph)
    back = resample_15_to_h_mean(p15)
    assert all(abs(a - b) < 1e-9 for a, b in zip(ph, back))   # hodinový = priemer 15-min


def test_resample_sum_and_last():
    a15 = [1.0] * 96
    assert resample_15_to_h_sum(a15)[0] == 4.0
    soc = list(range(96))
    assert resample_15_to_h_last(soc)[0] == 3.0   # posledný slot hodiny


if __name__ == "__main__":
    for f in [test_price_upsample_copies_not_divides, test_energy_upsample_divides,
              test_mask_upsample, test_resample_mean_roundtrip, test_resample_sum_and_last]:
        f()
    print("✓ granularity testy OK")
