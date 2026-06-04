/* Distribučné tarify — form ↔ JSON sync + presets.
   Vyžaduje globálne `window.DIST_PRESETS` (object z TARIFF_PRESETS). */
(function () {
  function _setVal(id, val) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.type === 'checkbox') { el.checked = !!val; }
    else { el.value = (val === null || val === undefined) ? '' : val; }
  }
  function _getVal(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    return el.type === 'checkbox' ? el.checked : el.value;
  }

  function formToJson() {
    const cfg = {
      enabled: _getVal('dist_enabled'),
      distribution_company: _getVal('dist_company'),
      tariff_group: _getVal('dist_tariff_group'),
      voltage_level: _getVal('dist_voltage_level'),
      tou_mode: _getVal('dist_tou_mode'),
      tou_high_eur_per_mwh: parseFloat(_getVal('dist_tou_high')) || 0,
      tou_low_eur_per_mwh: parseFloat(_getVal('dist_tou_low')) || 0,
      tou_high_hours: (_getVal('dist_tou_high_hours') || '').split(',')
        .map(s => parseInt(s.trim())).filter(n => !isNaN(n) && n >= 0 && n <= 23),
      tou_weekend_low_only: _getVal('dist_tou_weekend'),
      hourly_custom_eur_per_mwh: null,
      tps_eur_per_mwh: parseFloat(_getVal('dist_tps')) || 0,
      ss_eur_per_mwh: parseFloat(_getVal('dist_ss')) || 0,
      oze_eur_per_mwh: parseFloat(_getVal('dist_oze')) || 0,
      peak_charge_eur_per_kw_month: parseFloat(_getVal('dist_peak_charge')) || 0,
      monthly_fix_eur: parseFloat(_getVal('dist_monthly_fix')) || 0,
    };
    if (_getVal('dist_hourly_enable')) {
      const vals = (_getVal('dist_hourly_custom') || '').split(',')
        .map(s => parseFloat(s.trim())).filter(n => !isNaN(n));
      if (vals.length === 24) cfg.hourly_custom_eur_per_mwh = vals;
    }
    const json = JSON.stringify(cfg, null, 2);
    document.getElementById('dist_json_textarea').value = json;
    document.getElementById('dist_json_preview').value = json;
  }

  function jsonToForm(cfg) {
    _setVal('dist_enabled', cfg.enabled);
    _setVal('dist_company', cfg.distribution_company || 'ZSD');
    _setVal('dist_tariff_group', cfg.tariff_group || 'VO2');
    _setVal('dist_voltage_level', cfg.voltage_level || 'VN');
    _setVal('dist_tou_mode', cfg.tou_mode || 'tou');
    _setVal('dist_tou_high', cfg.tou_high_eur_per_mwh);
    _setVal('dist_tou_low', cfg.tou_low_eur_per_mwh);
    _setVal('dist_tou_high_hours', (cfg.tou_high_hours || []).join(','));
    _setVal('dist_tou_weekend', cfg.tou_weekend_low_only !== false);
    const hc = cfg.hourly_custom_eur_per_mwh;
    const hasHc = Array.isArray(hc) && hc.length === 24;
    _setVal('dist_hourly_enable', hasHc);
    _setVal('dist_hourly_custom', hasHc ? hc.join(',') : '');
    document.getElementById('dist_hourly_custom').style.display = hasHc ? 'block' : 'none';
    _setVal('dist_tps', cfg.tps_eur_per_mwh);
    _setVal('dist_ss', cfg.ss_eur_per_mwh);
    _setVal('dist_oze', cfg.oze_eur_per_mwh);
    _setVal('dist_peak_charge', cfg.peak_charge_eur_per_kw_month);
    _setVal('dist_monthly_fix', cfg.monthly_fix_eur);
    formToJson();
  }

  window.loadDistPreset = function () {
    const sel = document.getElementById('dist_preset_select');
    const note = document.getElementById('dist_preset_note');
    const k = sel.value;
    if (!k) { note.textContent = ''; return; }
    const p = (window.DIST_PRESETS || {})[k];
    if (!p) { note.textContent = 'Preset nenájdený: ' + k; return; }
    const cfg = Object.assign({}, p);
    const noteText = cfg._note || '';
    delete cfg._note;
    cfg.enabled = true;
    jsonToForm(cfg);
    note.textContent = noteText
      ? '✓ Načítané: ' + noteText + ' — ULOŽ profil aby sa zachovalo.'
      : '✓ Načítané. ULOŽ profil aby sa zachovalo.';
  };

  // Init
  document.addEventListener('DOMContentLoaded', function () {
    try {
      const raw = document.getElementById('dist_json_textarea').value;
      if (raw && raw.trim()) {
        const cfg = JSON.parse(raw);
        jsonToForm(cfg);
      }
    } catch (e) { console.warn('Dist JSON parse error:', e); }

    const fields = ['dist_enabled', 'dist_company', 'dist_tariff_group', 'dist_voltage_level',
      'dist_tou_mode', 'dist_tou_high', 'dist_tou_low', 'dist_tou_high_hours',
      'dist_tou_weekend', 'dist_hourly_enable', 'dist_hourly_custom',
      'dist_tps', 'dist_ss', 'dist_oze', 'dist_peak_charge', 'dist_monthly_fix'];
    fields.forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('change', formToJson);
      if (el.type !== 'checkbox' && el.tagName !== 'SELECT') {
        el.addEventListener('input', formToJson);
      }
    });

    const hourlyEnable = document.getElementById('dist_hourly_enable');
    if (hourlyEnable) {
      hourlyEnable.addEventListener('change', function () {
        document.getElementById('dist_hourly_custom').style.display = this.checked ? 'block' : 'none';
      });
    }
    const presetSel = document.getElementById('dist_preset_select');
    if (presetSel) {
      presetSel.addEventListener('change', function () {
        const k = this.value;
        const note = document.getElementById('dist_preset_note');
        const p = (window.DIST_PRESETS || {})[k];
        if (k && p && p._note) {
          note.textContent = '💡 ' + p._note + ' — stlač "Načítať do form".';
        } else { note.textContent = ''; }
      });
    }
  });
})();
