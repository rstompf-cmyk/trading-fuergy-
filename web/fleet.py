# -*- coding: utf-8 -*-
"""web/fleet.py — VPP fleet admin/monitor (read-only + enable/disable/tick/add).

Aditívny router (zakladá web/ balík pre rozbíjanie app.py). Číta fleet tabuľky
(battery + instance_status) a umožňuje:
  • zoznam batérií + ich posledný status (health / SOC / setpoint / čas),
  • enable/disable batérie ako inštancie,
  • jednorazový SIM tick flotily (build_fleet_executors → tick_fleet) na overenie,
  • registráciu batérie (UPSERT podľa name).

DEFENZÍVNE: ak fleet tabuľky ešte neexistujú (migrácia neaplikovaná), zobrazí
hlášku namiesto pádu. Konvencia setpointu: + = vybíja, − = nabíja.
"""
from __future__ import annotations
import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _load_rows():
    """Vráti (batteries, status_by_id) alebo vyhodí výnimku ak tabuľky chýbajú."""
    import fleet
    bats = fleet.list_batteries()
    status = {s["battery_id"]: s for s in fleet.fleet_status()}
    return bats, status


def _page(body: str, msg: str = "", err: bool = False) -> str:
    banner = ""
    if msg:
        color = "#b00020" if err else "#0a7d34"
        banner = f'<div style="margin:8px 0;padding:8px 12px;border-radius:6px;background:{color}12;color:{color};border:1px solid {color}55">{_esc(msg)}</div>'
    return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet — Trading Fuergy</title>
<link rel="stylesheet" href="/static/css/app.css">
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 16px; }}
  h1 {{ font-size: 20px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ padding: 6px 10px; border-bottom: 1px solid #ddd; text-align: left; }}
  th {{ background: #f4f4f6; }}
  .health-ok {{ color: #0a7d34; font-weight: 600; }}
  .health-degraded {{ color: #c47f00; font-weight: 600; }}
  .health-down, .health-unknown {{ color: #b00020; font-weight: 600; }}
  .muted {{ color: #888; }}
  button {{ cursor: pointer; padding: 4px 10px; border-radius: 6px; border: 1px solid #bbb; background: #fff; }}
  button.primary {{ background: #1456ff; color: #fff; border-color: #1456ff; }}
  form.inline {{ display: inline; margin: 0; }}
  .toolbar {{ margin: 12px 0; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
  fieldset {{ border: 1px solid #ddd; border-radius: 8px; margin-top: 16px; }}
  input, select {{ padding: 4px 6px; border: 1px solid #bbb; border-radius: 5px; }}
</style></head><body>
<h1>⚡ Fleet — VPP monitor <span class="muted" style="font-size:13px">(read-only + SIM)</span></h1>
{banner}
{body}
</body></html>"""


def _render() -> HTMLResponse:
    try:
        bats, status = _load_rows()
    except Exception as e:
        return HTMLResponse(_page(
            '<p>Fleet tabuľky pravdepodobne ešte neexistujú v tejto DB.</p>'
            '<p class="muted">Spusti migráciu: <code>alembic upgrade head</code> '
            '(vytvorí dormantné tabuľky battery/block/account/...).</p>',
            msg=f"DB chyba: {e}", err=True))

    if not bats:
        rows_html = '<tr><td colspan="10" class="muted">Žiadne batérie. Pridaj nižšie alebo cez tools/fleet_register.py.</td></tr>'
    else:
        rows = []
        for b in bats:
            st = status.get(b["id"], {})
            health = st.get("health", "—")
            hcls = f"health-{health}" if health in ("ok", "degraded", "down", "unknown") else "muted"
            soc = st.get("soc_pct")
            soc_s = f"{soc:.1f} %" if isinstance(soc, (int, float)) else "—"
            sp = st.get("last_setpoint_kw")
            sp_s = f"{sp:+.0f} kW" if isinstance(sp, (int, float)) else "—"
            toggle = ("disable", "Vypnúť") if b["enabled"] else ("enable", "Zapnúť")
            rows.append(
                f'<tr><td>{b["id"]}</td><td>{_esc(b["name"])}</td><td>{_esc(b["country"])}</td>'
                f'<td>{_esc(b["mode"])}</td><td>{b["batt_kw"]:.0f}</td><td>{b["batt_kwh"]:.0f}</td>'
                f'<td>{"✓" if b["enabled"] else "—"}</td>'
                f'<td class="{hcls}">{_esc(health)}</td><td>{soc_s}</td><td>{sp_s}</td>'
                f'<td class="muted">{_esc(st.get("ts","—"))}</td>'
                f'<td><form class="inline" method="post" action="/fleet/{toggle[0]}/{b["id"]}">'
                f'<button>{toggle[1]}</button></form></td></tr>')
        rows_html = "".join(rows)

    table = f"""<div class="toolbar">
  <form class="inline" method="post" action="/fleet/tick"><button class="primary">▶ SIM tick flotily</button></form>
  <a href="/fleet"><button>↻ Obnoviť</button></a>
  <span class="muted">setpoint: + vybíja / − nabíja</span>
</div>
<table><thead><tr>
  <th>id</th><th>názov</th><th>kraj</th><th>mód</th><th>kW</th><th>kWh</th><th>enabled</th>
  <th>health</th><th>SOC</th><th>setpoint</th><th>čas statusu</th><th></th>
</tr></thead><tbody>{rows_html}</tbody></table>

<fieldset><legend>Registrovať / upraviť batériu</legend>
<form method="post" action="/fleet/add">
  <p>názov <input name="name" required>
     kraj <select name="country"><option>sk</option><option>cz</option></select>
     mód <select name="mode"><option>simulation</option><option>real</option></select></p>
  <p>kW <input name="batt_kw" type="number" step="1" value="1000" style="width:90px">
     kWh <input name="batt_kwh" type="number" step="1" value="2000" style="width:90px">
     eff <input name="eff" type="number" step="0.01" value="0.95" style="width:70px">
     realio_host <input name="realio_host" placeholder="(len real mód)">
     <label><input type="checkbox" name="enabled" value="1"> enabled</label></p>
  <button class="primary">Uložiť batériu</button>
</form></fieldset>"""
    return HTMLResponse(_page(table))


@router.get("/fleet", response_class=HTMLResponse)
def fleet_get():
    return _render()


@router.post("/fleet/enable/{battery_id}")
def fleet_enable(battery_id: int):
    import fleet
    fleet.set_enabled(battery_id, True)
    return RedirectResponse("/fleet", status_code=303)


@router.post("/fleet/disable/{battery_id}")
def fleet_disable(battery_id: int):
    import fleet
    fleet.set_enabled(battery_id, False)
    return RedirectResponse("/fleet", status_code=303)


@router.post("/fleet/tick")
def fleet_tick():
    """Jednorazový SIM control tick flotily (na overenie). Real batérie bez
    wiringu → fail-safe degraded (fleet nespadne)."""
    try:
        from control.runner import build_fleet_executors, tick_fleet
        execs = build_fleet_executors()
        res = tick_fleet(execs, dt_h=1.0 / 60.0)
        ok = sum(1 for r in res.values() if r.get("health") == "ok")
        deg = sum(1 for r in res.values() if r.get("health") == "degraded")
    except Exception as e:
        return HTMLResponse(_page("", msg=f"tick zlyhal: {e}", err=True))
    return RedirectResponse(f"/fleet?ticked={ok}ok_{deg}deg", status_code=303)


@router.post("/fleet/add")
async def fleet_add(req: Request):
    """Registrácia/úprava batérie z formulára (req.form() = urlencoded, bez
    python-multipart závislosti — rovnaký štýl ako ostatné POST routes appky)."""
    import fleet
    form = await req.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/fleet", status_code=303)

    def _f(key, default=0.0):
        try:
            return float(form.get(key) or default)
        except (ValueError, TypeError):
            return default

    try:
        fleet.register_battery(
            name, (form.get("country") or "sk"), mode=(form.get("mode") or "simulation"),
            batt_kw=_f("batt_kw"), batt_kwh=_f("batt_kwh"), eff=_f("eff", 0.95),
            enabled=bool(form.get("enabled")),
            realio_host=((form.get("realio_host") or "").strip() or None))
    except Exception as e:
        return HTMLResponse(_page("", msg=f"registrácia zlyhala: {e}", err=True))
    return RedirectResponse("/fleet", status_code=303)
