"""Overí súvislosť VÝRAZNEJ ZMENY AKTIVÁCIE (odchýlka od pár-hodinového priemeru) a ceny odchýlky ZCO.
Cieľ: zistiť, či "aktivácia nad/pod svojím priemerom" predpovedá vysokú/nízku ZCO (a spread ZCO−DT),
a aký rolling-window + prah dáva najlepšiu separáciu → na spúšťanie batérie na plný výkon mimo hraníc.

Spustenie:  python analyze_activation_zco.py
Vstup:      out/imbalance_minute.csv  (minútové aFRR_plus/minus, mFRR..., sys_MW, zco_eur, isot_eur, ts15)
"""
import numpy as np
import pandas as pd

WINDOWS_MIN = [60, 120, 180, 240]     # testované rolling okná (1–4 h)


def load():
    df = pd.read_csv("out/imbalance_minute.csv", parse_dates=["time", "ts15"])
    for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5",
              "sys_MW", "zco_eur", "isot_eur"]:
        if c not in df.columns:
            df[c] = np.nan
    df = df.sort_values("time").reset_index(drop=True)
    df["date"] = df.time.dt.date
    return df


def corr(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    return np.corrcoef(a[m], b[m])[0, 1] if m.sum() > 30 else float("nan")


def main():
    df = load()
    n_days = df.date.nunique()
    print("=" * 74)
    print(f"ANALÝZA: zmena aktivácie (odchýlka od priemeru) vs cena odchýlky ZCO — {n_days} dní")
    print("=" * 74)

    # surové (okamžité) veličiny vs ZCO a spread
    up = df.aFRR_plus.fillna(0) + df.mFRR_plus.fillna(0) + df.mFRR5.fillna(0)
    dn = df.aFRR_minus.fillna(0) + df.mFRR_minus.fillna(0)
    net = (up - dn).values
    zco = df.zco_eur.values
    dt = df.isot_eur.values
    spread = zco - dt
    print("Surové (bez priemerovania):")
    print(f"   net aktivácia (up−dn) ~ ZCO    : {corr(net, zco):+.3f}")
    print(f"   net aktivácia (up−dn) ~ spread : {corr(net, spread):+.3f}")
    print(f"   aFRR+ ~ ZCO {corr(df.aFRR_plus.values, zco):+.3f} | "
          f"aFRR− ~ ZCO {corr(df.aFRR_minus.values, zco):+.3f}")
    print("-" * 74)

    print("ODCHÝLKA OD KĹZAVÉHO PRIEMERU (kauzálne, shift 1 min) — pre rôzne okná:")
    print(f"{'okno':>6} {'up_dev~ZCO':>11} {'dn_dev~ZCO':>11} {'net_dev~ZCO':>12} {'net_dev~spread':>15}")
    best = (None, -1)
    for w in WINDOWS_MIN:
        roll_up = df.aFRR_plus.shift(1).rolling(w, min_periods=20).mean()
        roll_dn = df.aFRR_minus.shift(1).rolling(w, min_periods=20).mean()
        up_dev = (df.aFRR_plus - roll_up).values
        dn_dev = (df.aFRR_minus - roll_dn).values
        net_dev = up_dev - dn_dev
        c_up = corr(up_dev, zco); c_dn = corr(dn_dev, zco)
        c_net = corr(net_dev, zco); c_spr = corr(net_dev, spread)
        print(f"{w:>5}m {c_up:>+11.3f} {c_dn:>+11.3f} {c_net:>+12.3f} {c_spr:>+15.3f}")
        score = abs(c_net) if np.isfinite(c_net) else -1
        if score > best[1]:
            best = (w, score)
    bw = best[0]
    print("-" * 74)
    print(f"Najsilnejšia súvislosť pri okne ≈ {bw} min — používam ho na podmienenú analýzu.\n")

    # podmienená analýza pri najlepšom okne: čo robí ZCO, keď je aktivácia NAD/POD priemerom
    roll_up = df.aFRR_plus.shift(1).rolling(bw, min_periods=20).mean()
    roll_dn = df.aFRR_minus.shift(1).rolling(bw, min_periods=20).mean()
    up_dev = (df.aFRR_plus - roll_up)
    dn_dev = (df.aFRR_minus - roll_dn)
    g = pd.DataFrame({"zco": zco, "dt": dt, "spread": spread,
                      "up_dev": up_dev.values, "dn_dev": dn_dev.values}).dropna()
    base_z = g.zco.mean(); base_s = g.spread.mean()
    print(f"Základ (všetky periódy): ZCO priemer {base_z:.0f} € | spread {base_s:+.0f} €")
    print("-" * 74)

    # prahy ako násobok smerodajnej odchýlky odchýlky aktivácie
    su, sd = g.up_dev.std(), g.dn_dev.std()
    for k in [0.0, 0.5, 1.0, 1.5]:
        up_sig = g[g.up_dev > k * su]
        dn_sig = g[g.dn_dev > k * sd]
        print(f"prah {k:.1f}σ:")
        print(f"   aFRR+ nad priemer (+{k:.1f}σ, n={len(up_sig):5d}): ZCO {up_sig.zco.mean():4.0f} € "
              f"(Δ{up_sig.zco.mean()-base_z:+.0f}) | spread {up_sig.spread.mean():+4.0f} € "
              f"→ {'VYBI dáva zmysel' if up_sig.zco.mean() > base_z else 'slabé'}")
        print(f"   aFRR− nad priemer (+{k:.1f}σ, n={len(dn_sig):5d}): ZCO {dn_sig.zco.mean():4.0f} € "
              f"(Δ{dn_sig.zco.mean()-base_z:+.0f}) | ZCO≤0 v {(dn_sig.zco<=0).mean()*100:3.0f}% "
              f"→ {'NABI dáva zmysel' if dn_sig.zco.mean() < base_z else 'slabé'}")
    print("=" * 74)
    print("Interpretácia: ak 'aFRR+ nad priemer' dvíha ZCO a 'aFRR− nad priemer' tlačí ZCO dole/k 0,")
    print("potom má zmysel spúšťať plný výkon (VYBI/NABI) pri prekročení priemeru o zvolený prah.")


if __name__ == "__main__":
    main()
