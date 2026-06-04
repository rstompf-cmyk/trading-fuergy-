# -*- coding: utf-8 -*-
"""
selfcheck.py – overí, či máš všetky súbory a najnovšie úpravy zapracované.
Spusti:  python selfcheck.py
Vypíše ✓/✗ pre každú položku; ak je niečo ✗, ten súbor treba znovu stiahnuť.
"""
import os
import sys
import inspect

ok_all = True


def chk(label, cond, hint=""):
    global ok_all
    mark = "✓" if cond else "✗"
    if not cond:
        ok_all = False
    print(f"  {mark} {label}" + ("" if cond else f"   → {hint}"))
    return cond


def has(path, *needles):
    """Súbor existuje a obsahuje všetky reťazce (marker najnovších úprav)."""
    if not os.path.exists(path):
        return False
    s = open(path, encoding="utf-8").read()
    return all(n in s for n in needles)


print("="*70)
print("SELF-CHECK FTV aplikácie")
print("="*70)

print("\n[1] Súbory prítomné:")
need = ["app.py", "rt_controller.py", "case_config.py", "combined_backtest.py",
        "backfill.py", "data_sources.py", "price_model.py", "optimizer.py",
        "report.py", "fetch_imbalance_history.py"]
for f in need:
    chk(f, os.path.exists(f), "chýba – stiahni ho")

print("\n[2] Najnovšie úpravy zapracované:")
chk("rt_controller: apply_case + decide_reason (prípady + pravidlá)",
    has("rt_controller.py", "def apply_case", "def decide_reason"), "stiahni rt_controller.py")
chk("rt_controller: flip-event", has("rt_controller.py", "FLIP_EVENT", "flip_up"), "stiahni rt_controller.py")
chk("rt_controller: haircut + latencia + počet cyklov",
    has("rt_controller.py", "RT_HAIRCUT", "RT_LATENCY", "return_cycles"), "stiahni rt_controller.py")
chk("case_config: flip + haircut + latencia polia",
    has("case_config.py", "flip_event", "rt_haircut", "rt_latency_min"), "stiahni case_config.py")
chk("combined_backtest: sweep + out-of-sample + stĺpec cykly",
    has("combined_backtest.py", "SWEEP", "OUT-OF-SAMPLE", "rt_cycles"), "stiahni combined_backtest.py")
chk("app.py: graf ceny odchýlky (ZCO) + DT + flip farby",
    has("app.py", 'id="c5"', "flip↑", "act_net_roll"), "stiahni app.py")
chk("app.py: okamžitá odchýlka navrch v sys grafe",
    has("app.py", "okamžitá odchýlka"), "stiahni app.py")
chk("app.py: stránka /data (doplnenie dát)",
    has("app.py", "Doplniť chýbajúce", "def data_post"), "stiahni app.py")
chk("backfill: doplnenie chýbajúcich dát", has("backfill.py", "def backfill_all"), "stiahni backfill.py")

print("\n[3] Moduly importujú a sedia spolu:")
try:
    import rt_controller as rtc, case_config as cc
    import combined_backtest, backfill                       # noqa
    chk("import všetkých modulov", True)
    chk("run_day vracia cykly (return_cycles)",
        "return_cycles" in inspect.signature(rtc.run_day).parameters, "stiahni rt_controller.py")
    cc.ensure_default()
    cases = cc.list_cases()
    chk("prípady existujú: " + ", ".join(cases), len(cases) >= 1)
    for n in cases:
        x = cc.load_case(n); rtc.apply_case(x)
        print(f"      {n:10}: max_cycles={x.max_cycles:g}, haircut={rtc.RT_HAIRCUT:g}, "
              f"latencia={rtc.RT_LATENCY}, flip={rtc.FLIP_EVENT}")
    if "realistic" in cases:
        r = cc.load_case("realistic")
        chk("realistic má 3 cykly + haircut 1.0 + latencia 1",
            r.max_cycles == 3 and r.rt_haircut == 1.0 and r.rt_latency_min == 1,
            "uprav out/cases/realistic.json (max_cycles 3, rt_haircut 1.0, rt_latency_min 1)")
except Exception as e:
    chk(f"import modulov ({e})", False, "niektorý .py je starý/chýba")

print("\n[4] Dáta (na backtest a /rt):")
for f in ["out/imbalance_history.csv", "out/imbalance_minute.csv", "out/price_train_2026.csv"]:
    chk(f, os.path.exists(f), "spusti  python backfill.py  na doplnenie")

print("\n" + "="*70)
print("VÝSLEDOK:", "✅ Všetko OK – môžeš spustiť  python app.py" if ok_all
      else "⚠️  Niečo treba doplniť (pozri ✗ vyššie)")
print("="*70)
sys.exit(0 if ok_all else 1)
