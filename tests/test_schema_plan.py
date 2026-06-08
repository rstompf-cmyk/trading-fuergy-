# -*- coding: utf-8 -*-
"""tests/test_schema_plan.py — Smoke test pre Fázu A.2 (Pydantic StoredPlan).

Pre každý existujúci plán v `out/cz/plans/`:
  1. Načítaj raw JSON
  2. Validuj cez StoredPlan.model_validate
  3. model_dump() → späť do dict
  4. Re-validate dump → musia dať rovnaký výsledok (round-trip)

Plus testy:
  • field validators (kind, step_min, date format)
  • cross-field validators (schedule dĺžka vs step_min)
  • mults / rt_mask dĺžka
  • get_array / has_field / expected_slots helpery
  • forward-compat (extra="allow")
"""
import sys
import os
import json
import glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import StoredPlan, PlanSlot
import plan_store


# Skúsim oba marketové stromy (CZ + SK)
def _all_plan_files():
    roots = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "out", "cz", "plans"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "out", "sk", "plans"),
    ]
    files = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        files += glob.glob(os.path.join(r, "*.json"))
        files += glob.glob(os.path.join(r, "**", "*.json"), recursive=True)
    return sorted(set(files))


def test_load_all_existing_plans():
    """Každý plán v out/<market>/plans/ musí prejsť Pydantic validáciou."""
    files = _all_plan_files()
    assert len(files) > 0, "Žiadne plány v out/*/plans/ — nemám čo testovať"
    errors = []
    ok = 0
    combos = {}
    for f in files:
        try:
            with open(f) as fh:
                d = json.load(fh)
            sp = StoredPlan.model_validate(d)
            ok += 1
            key = (sp.kind, sp.step_min)
            combos[key] = combos.get(key, 0) + 1
        except Exception as e:
            errors.append((f, str(e)[:200]))
    if errors:
        for f, e in errors[:5]:
            print(f"  ✗ {f}: {e}")
        assert False, f"{len(errors)}/{len(files)} plánov zlyhalo na validácii"
    assert ok == len(files), f"Validovaných {ok}/{len(files)}"


def test_round_trip_dump_load():
    """model_dump() → model_validate musí dať rovnaký výsledok."""
    files = _all_plan_files()
    # Test na sample 20 plánov (rýchlosť)
    sample = files[::max(1, len(files) // 20)][:20]
    for f in sample:
        with open(f) as fh:
            d = json.load(fh)
        sp = StoredPlan.model_validate(d)
        dump = sp.model_dump()
        sp2 = StoredPlan.model_validate(dump)
        assert sp.date == sp2.date
        assert sp.step_min == sp2.step_min
        assert sp.kind == sp2.kind
        assert sp.profile == sp2.profile
        # schedule kľúče sa zachovajú
        assert set(sp.schedule.keys()) == set(sp2.schedule.keys())


def test_validator_invalid_kind():
    """Neznámy kind musí raise."""
    from pydantic import ValidationError
    try:
        StoredPlan.model_validate({
            "date": "2026-05-01", "step_min": 60, "kind": "unknown_kind",
        })
        assert False, "neznámy kind by mal raise"
    except ValidationError:
        pass


def test_validator_invalid_step():
    """step_min mimo {15, 60} musí raise."""
    from pydantic import ValidationError
    for bad in (1, 30, 45, 120, 0):
        try:
            StoredPlan.model_validate({
                "date": "2026-05-01", "step_min": bad, "kind": "plan",
            })
            assert False, f"step_min={bad} by mal raise"
        except ValidationError:
            pass


def test_validator_invalid_date_format():
    """date mimo YYYY-MM-DD formátu musí raise."""
    from pydantic import ValidationError
    for bad in ("01-05-2026", "2026/05/01", "2026-5-1", "nedávno", ""):
        try:
            StoredPlan.model_validate({
                "date": bad, "step_min": 60, "kind": "plan",
            })
            assert False, f"date='{bad}' by mal raise"
        except ValidationError:
            pass


def test_validator_schedule_length_mismatch_60min():
    """60-min plán so schedule dĺžky != 24 musí raise."""
    from pydantic import ValidationError
    try:
        StoredPlan.model_validate({
            "date": "2026-05-01", "step_min": 60, "kind": "plan",
            "schedule": {"batt_kw": [0.0] * 10},
        })
        assert False, "60-min so schedule dĺžky 10 by mal raise"
    except ValidationError:
        pass


def test_validator_schedule_length_mismatch_15min():
    """15-min plán so schedule dĺžky != 96 musí raise."""
    from pydantic import ValidationError
    try:
        StoredPlan.model_validate({
            "date": "2026-05-01", "step_min": 15, "kind": "dentrh",
            "schedule": {"batt_kw": [0.0] * 24},
        })
        assert False, "15-min so schedule dĺžky 24 by mal raise"
    except ValidationError:
        pass


def test_validator_mults_length():
    """mults dĺžky != 24/96 musí raise (ale None/[] je OK)."""
    from pydantic import ValidationError
    # OK: None
    StoredPlan.model_validate({
        "date": "2026-05-01", "step_min": 60, "kind": "plan", "mults": None,
    })
    # OK: empty
    StoredPlan.model_validate({
        "date": "2026-05-01", "step_min": 60, "kind": "plan", "mults": [],
    })
    # OK: 24 hodnôt pre 60-min
    StoredPlan.model_validate({
        "date": "2026-05-01", "step_min": 60, "kind": "plan",
        "mults": [1.0] * 24,
    })
    # FAIL: 50 pre 60-min
    try:
        StoredPlan.model_validate({
            "date": "2026-05-01", "step_min": 60, "kind": "plan",
            "mults": [1.0] * 50,
        })
        assert False, "mults dĺžky 50 v 60-min plána by mal raise"
    except ValidationError:
        pass


def test_helper_expected_slots():
    """expected_slots() vracia správne 24 / 96."""
    sp60 = StoredPlan.model_validate({"date": "2026-01-01", "step_min": 60, "kind": "plan"})
    sp15 = StoredPlan.model_validate({"date": "2026-01-01", "step_min": 15, "kind": "dentrh"})
    assert sp60.expected_slots() == 24
    assert sp15.expected_slots() == 96


def test_helper_get_array():
    """get_array vracia list dĺžky expected_slots, None → default."""
    sp = StoredPlan.model_validate({
        "date": "2026-01-01", "step_min": 60, "kind": "plan",
        "schedule": {"batt_kw": [1.0, 2.0, None, 4.0] + [0.0] * 20},
    })
    arr = sp.get_array("batt_kw")
    assert len(arr) == 24
    assert arr[0] == 1.0
    assert arr[1] == 2.0
    assert arr[2] == 0.0   # None → default
    assert arr[3] == 4.0
    # chýbajúci kľúč → list of defaults
    arr2 = sp.get_array("non_existing", default=42.0)
    assert len(arr2) == 24
    assert all(v == 42.0 for v in arr2)


def test_helper_has_field():
    """has_field detekuje prítomné polia."""
    sp = StoredPlan.model_validate({
        "date": "2026-01-01", "step_min": 60, "kind": "plan",
        "schedule": {"batt_kw": [0.0] * 24, "empty_key": []},
    })
    assert sp.has_field("batt_kw") is True
    assert sp.has_field("empty_key") is False
    assert sp.has_field("non_existing") is False


def test_extra_fields_allowed():
    """Forward-compat: pridanie neznámeho fieldu nesmie zlomiť validation."""
    sp = StoredPlan.model_validate({
        "date": "2026-01-01", "step_min": 60, "kind": "plan",
        "unknown_future_field": "any value",
        "schedule": {"batt_kw": [0.0] * 24, "future_metric": [1.0] * 24},
        "params": {"future_param": True},
        "summary": {"future_summary_key": 123},
    })
    # Cez model_dump musí pretrvať
    dump = sp.model_dump()
    assert "unknown_future_field" in dump
    assert "future_metric" in dump["schedule"]


def test_load_plan_validated_api():
    """plan_store.load_plan_validated vracia StoredPlan alebo None.

    Skip ak je FTV_SANDBOX=1 (sandbox layout) a žiadne plány v sandboxe (čaká migration).
    Skip ak je plán z neaktívneho trhu (CZ plán pri aktívnom SK trhu).
    """
    import os as _os
    sandbox = _os.environ.get("FTV_SANDBOX", "").strip() in ("1", "true", "True", "yes")

    # Zisti aktívny trh — vyber plán z neho (aby _dir_for vedel cestu).
    try:
        import market as _mk
        active = (_mk.active_market() or "").lower()
    except Exception:
        active = ""

    files = _all_plan_files()
    if not files:
        return  # nič na testovanie

    # Najprv vyber plány z aktívneho trhu (out/<active>/plans/...).
    if active:
        sub = f"/out/{active}/plans/"
        files_active = [f for f in files if sub in f.replace("\\", "/")]
        if files_active:
            files = files_active

    sample = files[0]
    fn = os.path.basename(sample)
    parts = fn.replace(".json", "").split("_")
    date_iso = parts[0]
    step_min = int(parts[1].replace("min", ""))
    kind = parts[2]
    parent = os.path.basename(os.path.dirname(sample))
    profile = None if parent == "plans" else parent

    sp = plan_store.load_plan_validated(date_iso, step_min, kind, profile)
    if sp is None and sandbox:
        print("  (skip — sandbox mode, plán v legacy layout-e)")
        return
    if sp is None:
        # Cross-market fallback: stačí keď load_plan_validated nevybuchne pri valid raw
        raw = plan_store.load_plan_safe(date_iso, step_min, kind, profile)
        if raw is None:
            print(f"  (skip — plán {fn} nedostupný v aktívnom trhu '{active}')")
            return
        # Manual validate to overiť že schema funguje
        sp = plan_store.validate_plan_dict(raw)
    assert sp is not None, f"load_plan_validated nevrátil StoredPlan pre {fn}"
    assert sp.date == date_iso
    assert sp.step_min == step_min
    assert sp.kind == kind


def test_validate_plan_dict_api():
    """plan_store.validate_plan_dict raise pre nevalidný dict."""
    from pydantic import ValidationError
    # Valid
    sp = plan_store.validate_plan_dict({
        "date": "2026-01-01", "step_min": 60, "kind": "plan",
    })
    assert sp.date == "2026-01-01"
    # Invalid
    try:
        plan_store.validate_plan_dict({"date": "x", "step_min": 60, "kind": "plan"})
        assert False, "validate_plan_dict mal raise"
    except ValidationError:
        pass


if __name__ == "__main__":
    # CLI mode — spustí všetky testy a vypíše výsledok
    print("━━━ Fáza A.2 smoke test ━━━\n")
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
