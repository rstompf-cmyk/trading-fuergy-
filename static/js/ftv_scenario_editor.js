/* FTV scenár editor — interaktívne SVG s 24 ručkami + Gauss smoothing.
   Inicializovaný z templates/pages/ftv_scenario.html cez:
       window.initFtvScenarioEditor({ hours: [...24 čísel...] });
*/
(function () {
  const W = 720, H = 320, PAD_L = 42, PAD_R = 14, PAD_T = 14, PAD_B = 44;
  const X0 = PAD_L, X1 = W - PAD_R, Y0 = PAD_T, Y1 = H - PAD_B;
  const N = 24;
  const dx = (X1 - X0) / N;

  let hours, initHours, Y_MAX;

  function yScale(v) { return Y1 - (v / Y_MAX) * (Y1 - Y0); }
  function vFromY(y) { return Math.max(0, (Y1 - y) / (Y1 - Y0) * Y_MAX); }

  function applyGauss(idx, newVal) {
    const sigma = 1.0, radius = 3;
    const old = hours[idx], delta = newVal - old;
    hours[idx] = newVal;
    for (let off = -radius; off <= radius; off++) {
      if (off === 0) continue;
      const j = idx + off;
      if (j < 0 || j >= N) continue;
      const w = Math.exp(-(off * off) / (2 * sigma * sigma));
      hours[j] = Math.max(0, hours[j] + w * delta * 0.5);
    }
  }

  function rescaleY() {
    const m = Math.max(...hours.map(v => Math.abs(v)), 1);
    Y_MAX = Math.max(100, Math.ceil(m * 1.2 / 10) * 10);
  }

  function render() {
    const svg = document.getElementById('ftv-svg');
    if (!svg) return;
    svg.innerHTML = '';
    // mriežka — horizontálne
    for (let i = 0; i <= 5; i++) {
      const yv = Y_MAX * (1 - i / 5);
      const y = yScale(yv);
      svg.innerHTML += `<line x1="${X0}" y1="${y}" x2="${X1}" y2="${y}" stroke="#e8e8e8"/>`;
      svg.innerHTML += `<text x="${X0 - 6}" y="${y + 4}" text-anchor="end" font-size="11" fill="#666">${Math.round(yv)}</text>`;
    }
    // vertikálne (každé 3 hodiny)
    for (let i = 0; i <= N; i += 3) {
      const x = X0 + i * dx;
      svg.innerHTML += `<line x1="${x}" y1="${Y0}" x2="${x}" y2="${Y1}" stroke="#eee"/>`;
      svg.innerHTML += `<text x="${x}" y="${Y1 + 16}" text-anchor="middle" font-size="11" fill="#666">${i}:00</text>`;
    }
    // bars + krivka
    let polyPts = [];
    for (let i = 0; i < N; i++) {
      const x = X0 + i * dx;
      const y = yScale(hours[i]);
      const w = dx - 1;
      svg.innerHTML += `<rect class="bar" x="${x}" y="${y}" width="${w}" height="${Y1 - y}"/>`;
      polyPts.push(`${x + dx / 2},${y}`);
    }
    svg.innerHTML += `<polyline points="${polyPts.join(' ')}" fill="none" stroke="#7a5c00" stroke-width="1.4"/>`;
    // ručky
    for (let i = 0; i < N; i++) {
      const x = X0 + i * dx + dx / 2;
      const y = yScale(hours[i]);
      svg.innerHTML += `<circle class="handle" data-idx="${i}" cx="${x}" cy="${y}" r="7"><title>h${i}: ${hours[i].toFixed(1)} kW</title></circle>`;
    }
    // popisek osi Y
    svg.innerHTML += `<text x="${X0 - 22}" y="${(Y0 + Y1) / 2}" text-anchor="middle" font-size="11" fill="#1F4E78" transform="rotate(-90 ${X0 - 22} ${(Y0 + Y1) / 2})">FTV [kW]</text>`;
    // totals
    const daySum = hours.reduce((a, b) => a + b, 0);
    const peak = Math.max(...hours);
    document.getElementById('t-day').textContent = daySum.toFixed(1);
    document.getElementById('t-peak').textContent = peak.toFixed(1);
    document.getElementById('hourly_json').value = JSON.stringify(hours);
    attachHandlers();
  }

  function attachHandlers() {
    document.querySelectorAll('.handle').forEach(el => {
      el.addEventListener('pointerdown', dragStart);
    });
  }

  let dragging = null;
  function dragStart(e) {
    dragging = parseInt(e.target.dataset.idx);
    e.target.setPointerCapture(e.pointerId);
    e.preventDefault();
    document.addEventListener('pointermove', dragMove);
    document.addEventListener('pointerup', dragEnd, { once: true });
  }
  function dragMove(e) {
    if (dragging === null) return;
    const svg = document.getElementById('ftv-svg');
    const r = svg.getBoundingClientRect();
    const yLocal = (e.clientY - r.top) / r.height * H;
    const newV = vFromY(yLocal);
    applyGauss(dragging, newV);
    rescaleY();
    render();
  }
  function dragEnd() {
    document.removeEventListener('pointermove', dragMove);
    dragging = null;
  }

  window.resetFtvHours = function () {
    hours = initHours.slice();
    rescaleY();
    render();
  };

  window.initFtvScenarioEditor = function (opts) {
    hours = (opts.hours || []).slice();
    initHours = hours.slice();
    const hint = Math.max(...hours.map(v => Math.abs(v)), 100);
    Y_MAX = Math.ceil(hint * 1.2 / 10) * 10;
    render();
  };
})();
