# Audit rolí a permissions — FTV+batéria appka

> Fáza 0 migrácie. Stav: 2026-06-04.
>
> Cieľ: navrhnúť permission maticu pre 4 role na základe užívateľských požiadaviek.

## Role (podľa Radoslavovho zadania)

| Rola | Popis (Radoslav) |
|------|------------------|
| **Admin** | Full access — všetko |
| **Obchodník** | Simulácie a veci okolo bez možnosti ovplyvniť reálne fungovanie |
| **Trading** | Generuje plány, pracuje so simuláciami aj reálnymi riešeniami **ktoré má povolené** (per-profile assignment) |
| **Zákazník** | Vidí len vybrané simulácie a reálne aplikácie, žiadne nastavenia, **iba výsledky a bilancie** |

## Princípy

1. **Per-profile prístup pre Trading + Zákazník** — admin priradí konkrétny profil (alebo viac) konkrétnemu užívateľovi. Bez priradenia → užívateľ nevidí profil vôbec.
2. **Admin a Obchodník vidia VŠETKY profily** — Obchodník však nemôže meniť `mode=real` profily ani robiť žiadne HW zápisy.
3. **Sensitivity gating:** real-mode HW write (Bender, OKTE order) je zaheslované unlock tokenom navyše rozhodované na úrovni profile-access (Trading musí mať pre konkrétny profil flag `can_write_hw=True`).
4. **Read-only sa nedá obísť cez API** — JSON endpointy (napr. `/realio/api/latest`) majú rovnaké permission checks ako HTML stránky.
5. **Žiadna self-registration** — Admin vytvára účty cez `/admin/users`.

---

## Permission matica — endpoint × rola

Legenda:
- ✅ povolené (full)
- 🔵 povolené read-only (vidí stránku, formulár, dáta, ale nemôže submit)
- 🟡 povolené **iba pre priradené profily** (filter v middleware)
- ❌ zakázané (HTTP 403 alebo skryté z navigation)

### 1. Plán D-1 + Denný trh

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/` | ✅ | ✅ | ✅ | 🔵 (formulár skrytý, redirect na `/livesim`) |
| POST `/plan` | ✅ | ❌ | 🟡 | ❌ |
| GET `/dentrh` | ✅ | ✅ | 🟡 | ❌ |
| POST `/dentrh` | ✅ | ❌ | 🟡 | ❌ |
| GET `/plan_batch` | ✅ | ✅ | 🟡 | ❌ |
| POST `/plan_batch` | ✅ | ❌ | 🟡 | ❌ |
| GET `/plans` | ✅ | ✅ | 🟡 | 🟡 (read-only) |
| GET `/plan_view` | ✅ | ✅ | 🟡 | 🟡 |
| POST `/plans/delete` | ✅ | ❌ | 🟡 | ❌ |
| GET `/download` | ✅ | ✅ | 🟡 | ❌ |

**Pozor — Obchodník:** môže pozerať /plan formulár ale **nemôže ho submitnúť** (genertvať plán). Reálny rozdiel oproti Tradingu je len v POST routes.

### 2. Profily

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/profiles` | ✅ | ✅ (read-only) | 🟡 (iba priradené) | ❌ |
| GET `/profiles/edit?name=…` | ✅ | 🔵 (read-only view) | 🟡 (priradené, ale **bez** distribution/HW polia) | ❌ |
| POST `/profiles/save` | ✅ | ❌ | 🟡 (iba simulácia profily) | ❌ |
| POST `/profiles/apply` | ✅ | ✅ (set active) | 🟡 | ❌ |
| POST `/profiles/delete` | ✅ | ❌ | ❌ | ❌ |
| POST `/profiles/snapshot` | ✅ | ✅ | 🟡 | ❌ |
| POST `/market/set` | ✅ | ✅ | ✅ | ❌ |

### 3. FTV scenario

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/ftv_scenario` | ✅ | ✅ | 🟡 | ❌ |
| POST `/ftv_scenario` | ✅ | ❌ | 🟡 | ❌ |

### 4. Livesim

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/livesim` | ✅ | ✅ | 🟡 | 🟡 (stripped UI — bez formulárov, len grafy a karty) |
| GET `/livesim/chC_export` | ✅ | ✅ | 🟡 | 🟡 |
| GET `/livesim_pdf` | ✅ | ✅ | 🟡 | 🟡 |

**Zákazník UI v livesime:** skrytý formulár, skrytý "FTV scenár editor" tlačidlo, skrytý "Export 15-min na Bender" tlačidlo, skrytý case picker. Vidí: cards (ZISK, FTV výroba, Prínos), grafy (chMW, chDT, chPlan, chFlow, chRiadenie, chF, chC), tabuľka denných zhrnutí.

### 5. Load import

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/load_import` | ✅ | ❌ | 🟡 | ❌ |
| POST `/load_import` (upload) | ✅ | ❌ | 🟡 | ❌ |
| POST `/load_import/rescale` | ✅ | ❌ | 🟡 | ❌ |
| POST `/load_import/clear` | ✅ | ❌ | ❌ | ❌ |

### 6. VDT (OKTE intraday)

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/vdt`, `/vdt/live_advisor`, `/vdt/d1`, `/vdt/backtest`, `/vdt/simulator`, `/vdt/board`, atď. | ✅ | ✅ | 🟡 | ❌ |
| POST diagnostické (`/vdt/inspect_cert`, `/vdt/discover`, `/vdt/probe`) | ✅ | ❌ | 🟡 | ❌ |

### 7. Realio — **kritické**

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/realio?tab=vizualizacia` | ✅ | ✅ (read-only viz) | 🟡 | 🟡 (iba priradené, read-only) |
| GET `/realio?tab=nastavenie` | ✅ | ❌ | 🟡 (pri priradených) | ❌ |
| GET `/realio?tab=riadenie` | ✅ | ❌ | 🟡 (priradené + has_hw_write flag) | ❌ |
| POST `/realio/save` (config) | ✅ | ❌ | 🟡 | ❌ |
| POST `/realio/test_read` | ✅ | ❌ | 🟡 | ❌ |
| POST `/realio/relogin` | ✅ | ❌ | 🟡 | ❌ |
| POST `/realio/cleanup_csv`, `/fix_future_timestamps` | ✅ | ❌ | ❌ | ❌ |
| POST `/realio/backfill_range`, `/backfill` | ✅ | ❌ | 🟡 | ❌ |
| **POST `/realio/batt_plan_export/submit`** | ✅ | ❌ | 🟡 (priradené + has_hw_write + unlock token) | ❌ |
| **POST `/realio/write`** | ✅ | ❌ | 🟡 (priradené + has_hw_write + unlock) | ❌ |
| **POST `/realio/fve_write`** | ✅ | ❌ | 🟡 (priradené + has_hw_write + unlock) | ❌ |
| POST diag (`/scan_js`, `/probe_ws`, `/scan_msg_types`, `/ws_listen`) | ✅ | ❌ | ❌ | ❌ |
| GET `/realio/api/latest`, `/api/soc` | ✅ | ✅ | 🟡 | 🟡 |

**Sensitivity ladder pre HW write:**
1. User musí byť Trading rola
2. Aktívny profile.mode = "real"
3. user_profile_access(user_id, profile_id).can_write_hw = True
4. `auto_control_unlock.json` obsahuje token `I_UNDERSTAND_THIS_WRITES_TO_BENDER`
5. Form pre HW write má confirm-checkbox + recent password re-entry (re-auth pre HIGH risk akcie)

### 8. Auto control

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/auto_control` | ✅ | ✅ (read-only) | 🟡 | ❌ |
| POST `/auto_control/kill_switch` | ✅ | ❌ | 🟡 | ❌ |
| POST `/auto_control/toggle_profile` | ✅ | ❌ | 🟡 | ❌ |

### 9. Simulácia / kalibrácia

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| `/simulacia` GET+POST | ✅ | ✅ | 🟡 | ❌ |
| `/kalibracia` GET+POST | ✅ | ✅ | 🟡 | ❌ |

### 10. RT poradca

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/rt` | ✅ | ✅ | 🟡 | 🟡 (read-only, žiadne form inputs) |

### 11. Utilita

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/data` | ✅ | ❌ | ❌ | ❌ |
| POST `/data` (upload) | ✅ | ❌ | ❌ | ❌ |
| GET `/download` (ZIP) | ✅ | ✅ | 🟡 | ❌ |

### 12. Admin (nové endpointy)

| Endpoint | Admin | Obchodník | Trading | Zákazník |
|----------|-------|-----------|---------|----------|
| GET `/admin/users` | ✅ | ❌ | ❌ | ❌ |
| POST `/admin/users/create` | ✅ | ❌ | ❌ | ❌ |
| POST `/admin/users/edit` | ✅ | ❌ | ❌ | ❌ |
| POST `/admin/users/delete` | ✅ | ❌ | ❌ | ❌ |
| POST `/admin/users/access` (priradenie profilov) | ✅ | ❌ | ❌ | ❌ |
| GET `/admin/audit_log` | ✅ | ❌ | ❌ | ❌ |
| GET `/login`, `/logout` | (verejné) | (verejné) | (verejné) | (verejné) |

### 13. Login + session (nové)

| Endpoint | Účel |
|----------|------|
| GET `/login` | Form (username, password) |
| POST `/login` | Validate → session cookie |
| GET `/logout` | Clear session, redirect na `/login` |
| GET `/profile` | Zobrazenie vlastných údajov + zmena hesla |
| POST `/profile/password` | Zmena hesla |

---

## DB schéma pre auth (Fáza 1 + 2)

```sql
CREATE TABLE user (
  id            INTEGER PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  email         TEXT,
  password_hash TEXT NOT NULL,         -- bcrypt
  role          TEXT NOT NULL,         -- 'admin'|'obchodnik'|'trading'|'zakaznik'
  is_active     INTEGER DEFAULT 1,
  created_at    TEXT NOT NULL,
  last_login    TEXT,
  created_by    INTEGER REFERENCES user(id)
);

CREATE TABLE user_profile_access (
  user_id         INTEGER REFERENCES user(id) ON DELETE CASCADE,
  profile_id      INTEGER REFERENCES profile(id) ON DELETE CASCADE,
  can_read        INTEGER DEFAULT 1,
  can_write       INTEGER DEFAULT 0,       -- pre Trading: môže meniť profil
  can_write_hw    INTEGER DEFAULT 0,       -- pre Trading: môže robiť HW write na realio
  granted_at      TEXT,
  granted_by      INTEGER REFERENCES user(id),
  PRIMARY KEY (user_id, profile_id)
);

CREATE TABLE session (
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER REFERENCES user(id) ON DELETE CASCADE,
  token       TEXT UNIQUE NOT NULL,       -- random 32-byte hex
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL,              -- napr. 7 dní
  last_seen   TEXT,
  ip_address  TEXT,
  user_agent  TEXT
);

CREATE TABLE audit_log (
  id         INTEGER PRIMARY KEY,
  ts         TEXT NOT NULL,
  user_id    INTEGER REFERENCES user(id),
  action     TEXT NOT NULL,         -- 'login', 'profile_save', 'hw_write', 'plan_generate', ...
  resource   TEXT,                  -- profile_name, plan_date, atď.
  details    TEXT,                  -- JSON s deltami
  ip_address TEXT
);
```

---

## Implementačné poznámky pre Fázu 2

### Middleware

```python
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # 1. Public routes (login, static)
    if request.url.path.startswith(("/login", "/static")):
        return await call_next(request)
    # 2. Načítaj session cookie → user
    user = get_user_from_session(request)
    if not user:
        return RedirectResponse("/login")
    # 3. Pripoj user do request.state
    request.state.user = user
    # 4. Filter active profile (per-profile access)
    request.state.allowed_profiles = list_allowed_profiles(user)
    return await call_next(request)
```

### Permission decorator

```python
def require_role(*roles):
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            user = current_user()
            if user.role not in roles:
                return HTMLResponse("Forbidden", status_code=403)
            return await fn(*args, **kwargs)
        return wrapper
    return decorator

# použitie
@app.post("/plan")
@require_role("admin", "trading")
def plan(...): ...
```

### Per-profile access check

```python
def require_profile_access(profile_name, write=False, hw_write=False):
    user = current_user()
    if user.role == "admin":
        return True
    access = db.query(UserProfileAccess).filter_by(
        user_id=user.id, profile_id=Profile.by_name(profile_name).id
    ).first()
    if not access or not access.can_read:
        raise HTTPException(403)
    if write and not access.can_write:
        raise HTTPException(403)
    if hw_write and not access.can_write_hw:
        raise HTTPException(403)
```

### Audit log

Každý write endpoint zapíše záznam do `audit_log`:
- `action`: napr. `"plan_save"`, `"profile_update"`, `"hw_write_batt_setpoint"`
- `resource`: `profile.name + date` atď.
- `details`: JSON delta (čo sa zmenilo)

Admin má `/admin/audit_log` na prehľadávanie.

---

## UI rozdiely podľa rolí

### Header navigation (base.html)

| Položka navi | Admin | Obchodník | Trading | Zákazník |
|--------------|-------|-----------|---------|----------|
| /plan | ✓ | ✓ (read-only) | ✓ | ❌ |
| /dentrh | ✓ | ✓ | ✓ | ❌ |
| /livesim | ✓ | ✓ | ✓ | ✓ |
| /plans | ✓ | ✓ | ✓ | ✓ (filter) |
| /profiles | ✓ | ✓ | ✓ (filter) | ❌ |
| /vdt | ✓ | ✓ | ✓ | ❌ |
| /realio | ✓ | ❌ | ✓ (filter) | ❌ |
| /auto_control | ✓ | ✓ | ✓ | ❌ |
| /rt | ✓ | ✓ | ✓ | ✓ |
| /admin/users | ✓ | ❌ | ❌ | ❌ |

### Profil chip v hlavičke

Každá stránka v hlavičke ukazuje:
- meno prihláseného užívateľa + rola (chip farbou rola)
- aktívny profil (chip farba podľa profile.mode: simulácia=zelená, real=červená)
- "logout" link

### Read-only stripped UI pre Zákazníka

- Formuláre: úplne skryté (žiadny `<form>` element)
- Submit tlačidlá: skryté
- Bulk-edit ovládače: skryté
- Export do Excel/PDF: ✓ (môžu si stiahnuť svoje výsledky)
- "Edit profile" link: skrytý
- "Aktivovať iný profil": skrytý (admin im fixuje profile)

---

## Default seed users (pre dev)

Po inštalácii vytvoriť cez migration:

```python
# alembic migration init_users.py
admin = User(username="admin", password=bcrypt("admin"), role="admin")
db.add(admin)
db.commit()
print("Default admin: admin / admin — CHANGE PASSWORD IMMEDIATELY")
```

V produkcii: pri prvom prihlasování force-change hesla.

---

## Bezpečnostné poznámky

1. **bcrypt heslá** — žiadny plaintext, žiadny MD5
2. **Session tokens** — random 32 bytov, HTTPS-only cookie (`Secure`, `HttpOnly`, `SameSite=Strict`)
3. **Rate limiting** — login endpoint max 5 failed/min per IP
4. **CSRF tokens** — pre všetky POST endpointy (FastAPI-csrf middleware)
5. **HW write** — okrem rola + per-profile musí mať aj **re-auth cooldown** (re-zadanie hesla ak posledná auth > 15 min)
6. **Audit log** — všetky write actions, výmena hesla, login/logout, role changes
7. **Backup** — admin export DB pravidelne (cron); pred prepnutím profilu na real-mode automatic backup
