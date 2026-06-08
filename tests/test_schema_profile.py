# -*- coding: utf-8 -*-
"""tests/test_schema_profile.py — Smoke test pre Fázu A.1 (Pydantic ProfileConfig).

Pre každý existujúci profil v `out/profiles/`:
  1. Načítaj cez load_profile() (legacy dict)
  2. Validuj cez ProfileConfig.model_validate (Pydantic)
  3. model_dump() → späť do dict
  4. Re-validate dump → musia dať rovnaký výsledok (round-trip)

Plus testy:
  • toggle gates (uses_vdt, uses_tou, is_real)
  • field validators (mult96 length, name chars, soc_max >= soc_min)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import profiles
from core.schemas import ProfileConfig


def test_load_all_existing_profiles():
    """Každý profile v out/profiles/ musí prejsť Pydantic validáciou.

    Skip ak v current layout nie sú žiadne profily (napr. sandbox pred migration).
    """
    names = profiles.list_profiles()
    if len(names) == 0:
        print("  (skip — žiadne profily v current layout)")
        return
    errors = []
    for name in names:
        try:
            cfg = profiles.load_profile_validated(name)
            assert cfg is not None, f"{name}: load_profile_validated vrátil None"
            assert isinstance(cfg, ProfileConfig), f"{name}: not ProfileConfig"
            assert cfg.name == name or cfg.name == profiles._safe_name(name)
        except Exception as e:
            errors.append((name, str(e)))
    if errors:
        for n, e in errors:
            print(f"  ✗ {n}: {e}")
        assert False, f"{len(errors)} profilov zlyhalo na validácii"


def test_round_trip_dump_load():
    """model_dump() → model_validate musí dať rovnaký výsledok."""
    names = profiles.list_profiles()
    for name in names:
        cfg = profiles.load_profile_validated(name)
        if cfg is None:
            continue
        dump = cfg.model_dump()
        cfg2 = ProfileConfig.model_validate(dump)
        assert cfg.name == cfg2.name
        assert cfg.mode == cfg2.mode
        assert cfg.plan.kwp == cfg2.plan.kwp
        assert cfg.plan.batt_kw == cfg2.plan.batt_kw
        assert cfg.plan.joint_lp.enabled == cfg2.plan.joint_lp.enabled
        assert cfg.plan.joint_lp.use_vdt == cfg2.plan.joint_lp.use_vdt


def test_toggle_gates():
    """uses_vdt / uses_tou / is_real predikáty musia zodpovedať profile flags."""
    names = profiles.list_profiles()
    for name in names:
        cfg = profiles.load_profile_validated(name)
        if cfg is None:
            continue
        # uses_vdt: musí byť konzistentný s joint_lp.use_vdt + joint_lp.enabled
        if cfg.plan.joint_lp.enabled and cfg.plan.joint_lp.use_vdt:
            assert cfg.uses_vdt() or cfg.plan.joint_mpc_enabled is False
        # is_real: konzistentný s mode field
        assert cfg.is_real() == (cfg.mode == "real")


def test_validator_soc_range():
    """soc_max < soc_min musí raise ValueError."""
    from pydantic import ValidationError
    try:
        ProfileConfig.model_validate({
            "name": "Test_Invalid",
            "plan": {"soc_min": 80.0, "soc_max": 50.0},
        })
        assert False, "soc_max < soc_min by mal raise ValidationError"
    except ValidationError:
        pass


def test_validator_mult96_length():
    """mult96 musí mať 96 hodnôt alebo byť prázdny."""
    from pydantic import ValidationError
    # Validný: 96 hodnôt
    ProfileConfig.model_validate({
        "name": "Test_Valid_96",
        "mult96": [1.0] * 96,
    })
    # Validný: prázdny
    ProfileConfig.model_validate({"name": "Test_Valid_Empty", "mult96": []})
    # Invalid: 50 hodnôt
    try:
        ProfileConfig.model_validate({
            "name": "Test_Invalid_50",
            "mult96": [1.0] * 50,
        })
        assert False, "mult96 dĺžky 50 by mal raise"
    except ValidationError:
        pass


def test_validator_name_chars():
    """name môže obsahovať iba A-Za-z0-9_-."""
    from pydantic import ValidationError
    ProfileConfig.model_validate({"name": "Valid_Name-123"})
    try:
        ProfileConfig.model_validate({"name": "Invalid Name s medzerou"})
        assert False, "name s medzerou by mal raise"
    except ValidationError:
        pass


def test_extra_fields_allowed():
    """Forward-compat: pridanie neznámeho fieldu nesmie zlomiť validation."""
    cfg = ProfileConfig.model_validate({
        "name": "Test_Extra",
        "unknown_future_field": "any value",
        "plan": {"unknown_plan_field": 123},
    })
    assert cfg.name == "Test_Extra"


if __name__ == "__main__":
    # CLI mode — spustí všetky testy a vypíše výsledok
    print("━━━ Fáza A.1 smoke test ━━━\n")
    tests = [t for t in dir() if t.startswith("test_")]
    passed = failed = 0
    for tname in tests:
        try:
            globals()[tname]()
            print(f"  ✓ {tname}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {tname}: {e}")
            failed += 1
    print(f"\n━━━ {passed} OK, {failed} FAIL ━━━")
    sys.exit(0 if failed == 0 else 1)
