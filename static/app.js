'use strict';

const Dashboard = (() => {
  // ── State ──────────────────────────────────────────────────────────────────
  let ws = null;
  let reconnectDelay = 1000;
  let state = {};
  let chart = null;
  let chartLabels = [];
  let chartData = [];
  let chartAvg = [];
  let chartColors = [];
  let stintStartTime = null;
  let stintTimerInterval = null;
  let clockInterval = null;
  let dragSrc = null;

  // Regulation times in UTC ms (for client-side countdowns)
  const MAINT_1_UTC = Date.UTC(2026, 4, 23, 20, 0, 0);  // Month is 0-indexed: May=4
  const MAINT_2_UTC = Date.UTC(2026, 4, 24,  5, 0, 0);
  const VISOR_START_UTC = Date.UTC(2026, 4, 23, 20, 0, 0);
  const VISOR_END_UTC   = Date.UTC(2026, 4, 24,  4, 30, 0);
  const RACE_START_UTC  = Date.UTC(2026, 4, 23, 12, 0, 0);
  const RACE_END_UTC    = Date.UTC(2026, 4, 24, 12, 0, 0);

  const FLAG_COLORS = {
    GREEN:  '#22c55e',
    YELLOW: '#eab308',
    SC:     '#f97316',
    RED:    '#ef4444',
  };

  // ── WebSocket ──────────────────────────────────────────────────────────────
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen  = () => { reconnectDelay = 1000; };
    ws.onclose = () => setTimeout(connect, reconnectDelay = Math.min(reconnectDelay * 1.5, 10000));
    ws.onerror = () => ws.close();
    ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case 'full_state':   applyFullState(msg.data); break;
      case 'lap_update':   appendLap(msg.data); break;
      case 'fuel_update':  updateFuelPanel(msg.data); break;
      case 'stint_update': applyFullState(msg.data.data || msg.data); break;
      case 'rotation_update': updateRotationList(msg.data.rotation); break;
      case 'race_started': document.getElementById('start-btn').textContent = 'Race Running'; break;
      case 'alerts':       renderAlerts(msg.data); break;
      case 'standings_update': updateStandings(msg.data); break;
      case 'current_driver_update': renderCurrentDriverPanel(msg.data); break;
    }
  }

  function applyFullState(data) {
    if (!data) return;
    state = data;

    // Header
    el('hdr-lap').textContent = data.current_lap ?? '—';
    const rem = data.time_remaining_s;
    el('hdr-remaining').textContent = rem != null ? fmtDuration(rem) : '—';

    // Current driver
    const curr = data.current_driver || {};
    renderCurrentDriverPanel(curr);

    // Next driver
    const next = data.next_driver || {};
    el('next-name').textContent = capitalize(next.name || '—');
    el('next-ballast').textContent = next.ballast_kg != null ? `${next.ballast_kg.toFixed(1)} kg` : '—';
    el('next-pedal').textContent = next.pedal_pos || '—';
    el('next-weight').textContent = next.weight_kg != null ? `${next.weight_kg} kg` : '—';

    const delta = next.ballast_delta_kg;
    const deltaEl = el('next-ballast-delta');
    if (delta != null) {
      const sign = delta > 0 ? '+' : delta < 0 ? '' : '±';
      const cls = delta > 0 ? 'add' : delta < 0 ? 'remove' : 'none';
      deltaEl.innerHTML = `<span class="ballast-badge ${cls}">${sign}${delta.toFixed(1)} kg</span>`;
    }

    // Swap predictions
    const fuel = data.fuel || {};
    el('swap-laps').textContent = fuel.laps_until_pit != null ? Math.round(fuel.laps_until_pit) : '—';
    el('swap-time').textContent = fuel.time_to_pit_s != null ? fmtDuration(fuel.time_to_pit_s) : '—';

    // Fuel
    updateFuelPanel(fuel);

    // Lap model
    const lm = data.lap_model || {};
    el('fuel-avg-lap').textContent = lm.avg_lap_formatted || '—';
    const stats = el('lap-stats-summary');
    if (lm.total_laps != null) {
      stats.textContent = `${lm.total_laps} laps${lm.fastest_formatted ? ' · Fastest: ' + lm.fastest_formatted : ''}`;
    }

    // Laps table
    if (data.laps) rebuildLapsTable(data.laps, lm.fastest_ms);

    // Chart
    if (data.laps) rebuildChart(data.laps);

    // Rotation
    if (data.rotation) updateRotationList(data.rotation, curr.name, next.name);

    // Drivers (populate selects)
    if (data.drivers) populateDriverSelects(data.drivers);

    // Alerts
    if (data.alerts) renderAlerts(data.alerts);

    // Scraper
    if (data.scraper) updateScraperStatus(data.scraper);

    // Standings (position + gaps)
    updateStandings(data.standings || {});

    // If Fuel Model modal is open, refresh its data on any full-state push
    if (el('modal-fuelmodel-overlay').classList.contains('open')) {
      loadFuelBreakdown();
    }
  }

  // ── Current Driver Panel ───────────────────────────────────────────────────
  function renderCurrentDriverPanel(curr) {
    if (!curr) curr = {};
    el('curr-name').textContent = capitalize(curr.name || '—');
    el('curr-ballast').textContent = curr.ballast_kg != null ? `${curr.ballast_kg.toFixed(1)} kg` : '—';
    el('curr-pedal').textContent = curr.pedal_pos || '—';
    el('curr-weight').textContent = curr.weight_kg != null ? `${curr.weight_kg} kg` : '—';

    if (curr.stint_elapsed_s != null) {
      stintStartTime = Date.now() - curr.stint_elapsed_s * 1000;
      startStintTimer(curr.stint_elapsed_s);
    }

    if (curr.stint_started_at && curr.stint_start_lap != null) {
      const t = new Date(curr.stint_started_at).toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London',
      });
      el('curr-stint-started').textContent = `Lap ${curr.stint_start_lap} · ${t} BST`;
    } else {
      el('curr-stint-started').textContent = '—';
    }
    el('curr-laps-in-kart').textContent = curr.laps_in_kart != null ? curr.laps_in_kart : '—';
    el('curr-avg-lap').textContent = curr.avg_lap_formatted || '—';
    el('curr-total-time').textContent = curr.total_time_in_kart_s != null ? fmtDuration(curr.total_time_in_kart_s) : '—';
    el('curr-total-laps').textContent = curr.total_laps_raced != null ? curr.total_laps_raced : '—';

    // Keep state.current_driver fresh so dependent flows (lap modal, stint timer) stay consistent
    if (state) state.current_driver = curr;
  }

  // ── Standings ──────────────────────────────────────────────────────────────
  function updateStandings(s) {
    const posEl = el('hdr-position');
    const aheadEl = el('hdr-gap-ahead');
    const behindEl = el('hdr-gap-behind');

    const pos = s && s.position;
    posEl.textContent = pos != null ? `P${pos}` : '—';
    posEl.className = 'hdr-pos' + (
      pos == null ? '' :
      pos <= 3 ? ' podium' :
      pos <= 5 ? ' top5' :
      pos > 10 ? ' drop' : ''
    );

    aheadEl.textContent = s && s.gap_ahead ? s.gap_ahead : '—';
    behindEl.textContent = s && s.gap_behind ? s.gap_behind : '—';
    aheadEl.className = gapColorClass(s && s.gap_ahead);
    behindEl.className = gapColorClass(s && s.gap_behind);
  }

  function gapColorClass(gap) {
    if (!gap || gap === '—') return '';
    if (/L$/i.test(gap)) return 'gap-lap';
    const v = Math.abs(parseFloat(gap.replace('+', '')));
    if (isNaN(v)) return '';
    if (v < 1.0) return 'gap-close';
    if (v < 3.0) return 'gap-mid';
    return 'gap-safe';
  }

  function openStandingsModal() {
    const s = (state && state.standings) || {};
    el('stand-pos').value = s.position || '';
    el('stand-ahead').value = s.gap_ahead || '';
    el('stand-behind').value = s.gap_behind || '';
    el('modal-standings-overlay').classList.add('open');
  }

  async function submitStandings() {
    const body = {
      position: el('stand-pos').value || null,
      gap_ahead: el('stand-ahead').value || null,
      gap_behind: el('stand-behind').value || null,
    };
    await apiPost('/api/standings', body);
    closeModal('modal-standings-overlay');
  }

  // ── Fuel Panel ─────────────────────────────────────────────────────────────
  function updateFuelPanel(fuel) {
    if (!fuel) return;
    const pct = fuel.percent ?? 0;
    const gauge = el('fuel-gauge-fill');
    gauge.style.height = `${Math.max(0, pct)}%`;
    gauge.className = 'fuel-gauge-fill' + (pct < 20 ? ' danger' : pct < 40 ? ' warn' : '');

    const lapsEl = el('fuel-laps');
    const laps = fuel.laps_until_pit != null ? Math.round(fuel.laps_until_pit) : null;
    lapsEl.textContent = laps != null ? laps : '—';
    lapsEl.className = 'fuel-big' + (laps != null && laps < 3 ? ' danger' : laps != null && laps < 6 ? ' warn' : '');

    el('fuel-level').textContent = fuel.level_L != null ? `${fuel.level_L.toFixed(2)} L (${pct.toFixed(0)}%)` : '—';
    el('fuel-time').textContent = fuel.time_to_pit_s != null ? fmtDuration(fuel.time_to_pit_s) : '—';
    el('fuel-consumption').textContent = fuel.avg_consumption_Lpl != null ? `${fuel.avg_consumption_Lpl.toFixed(3)} L/lap` : '—';
    el('fuel-stops').textContent = fuel.stops_remaining != null ? fuel.stops_remaining : '—';
  }

  // ── Laps Table ─────────────────────────────────────────────────────────────
  function rebuildLapsTable(laps, fastestMs) {
    const tbody = el('laps-tbody');
    tbody.innerHTML = '';
    const recent = laps.slice(-20);
    for (const lap of recent.slice().reverse()) {
      const isFastest = fastestMs && lap.lap_time_ms === fastestMs;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${lap.lap_number}</td>
        <td class="font-bold" style="text-transform:capitalize">${lap.driver_name}</td>
        <td class="font-mono ${isFastest ? 'lap-fastest' : ''}">${lap.lap_time_formatted}</td>
        <td><span class="flag-badge flag-${lap.flag_condition}">${lap.flag_condition}</span></td>
        <td>${lap.is_rain ? '<span class="rain-dot">🌧</span>' : ''}</td>
        <td class="text-muted">${lap.source === 'speedhive' ? 'SH' : 'M'}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  function appendLap(lap) {
    const tbody = el('laps-tbody');
    const isFastest = state.lap_model && state.lap_model.fastest_ms === lap.lap_time_ms;
    const tr = document.createElement('tr');
    tr.style.background = 'rgba(34,197,94,0.08)';
    tr.innerHTML = `
      <td>${lap.lap_number}</td>
      <td class="font-bold" style="text-transform:capitalize">${lap.driver_name}</td>
      <td class="font-mono ${isFastest ? 'lap-fastest' : ''}">${lap.lap_time_formatted}</td>
      <td><span class="flag-badge flag-${lap.flag_condition}">${lap.flag_condition}</span></td>
      <td>${lap.is_rain ? '<span class="rain-dot">🌧</span>' : ''}</td>
      <td class="text-muted">${lap.source === 'speedhive' ? 'SH' : 'M'}</td>
    `;
    tbody.insertBefore(tr, tbody.firstChild);
    if (tbody.children.length > 20) tbody.removeChild(tbody.lastChild);
    setTimeout(() => tr.style.background = '', 1500);

    // Update chart
    addChartPoint(lap);
  }

  // ── Chart ──────────────────────────────────────────────────────────────────
  function initChart() {
    const ctx = el('lapTimeChart').getContext('2d');
    chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: chartLabels,
        datasets: [
          {
            label: 'Lap Time (s)',
            data: chartData,
            borderColor: '#22c55e',
            backgroundColor: 'rgba(34,197,94,0.08)',
            pointBackgroundColor: chartColors,
            pointRadius: 4,
            tension: 0.2,
            fill: false,
          },
          {
            label: 'Rolling Avg',
            data: chartAvg,
            borderColor: '#94a3b8',
            borderDash: [4, 3],
            pointRadius: 0,
            tension: 0.4,
            fill: false,
          },
        ],
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { labels: { color: '#f1f5f9', font: { size: 11 } } },
          tooltip: {
            callbacks: {
              label: (ctx) => {
                const ms = ctx.raw * 1000;
                return fmtLapTime(ms);
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#94a3b8', font: { size: 10 }, maxTicksLimit: 20 },
            grid: { color: '#1e293b' },
          },
          y: {
            ticks: {
              color: '#94a3b8',
              callback: (v) => fmtLapTime(v * 1000),
            },
            grid: { color: '#1e293b' },
          },
        },
      },
    });
  }

  function rebuildChart(laps) {
    if (!chart) return;
    const recent = laps.slice(-40);
    chartLabels.length = 0;
    chartData.length = 0;
    chartAvg.length = 0;
    chartColors.length = 0;

    let ema = null;
    for (const lap of recent) {
      if (lap.flag_condition === 'RED') continue;
      const s = lap.lap_time_ms / 1000;
      chartLabels.push(lap.lap_number);
      chartData.push(s);
      chartColors.push(FLAG_COLORS[lap.flag_condition] || '#22c55e');
      ema = ema == null ? s : 0.3 * s + 0.7 * ema;
      chartAvg.push(Math.round(ema * 1000) / 1000);
    }
    chart.update();
  }

  function addChartPoint(lap) {
    if (!chart || lap.flag_condition === 'RED') return;
    const s = lap.lap_time_ms / 1000;
    const prev = chartData[chartData.length - 1];
    const ema = prev == null ? s : 0.3 * s + 0.7 * prev;

    if (chartLabels.length >= 40) {
      chartLabels.shift(); chartData.shift(); chartColors.shift(); chartAvg.shift();
    }
    chartLabels.push(lap.lap_number);
    chartData.push(s);
    chartColors.push(FLAG_COLORS[lap.flag_condition] || '#22c55e');
    chartAvg.push(Math.round(ema * 1000) / 1000);
    chart.update();
  }

  // ── Rotation List ──────────────────────────────────────────────────────────
  function updateRotationList(rotation, currentName, nextName) {
    if (!rotation) return;
    const curr = currentName || (state.current_driver && state.current_driver.name);
    const next = nextName || (state.next_driver && state.next_driver.name);
    const list = el('rotation-list');
    list.innerHTML = '';

    rotation.forEach((name, i) => {
      const drivers = state.drivers || [];
      const dInfo = drivers.find(d => d.name === name) || {};
      const ballast = Math.max(0, 85 - (dInfo.weight_kg || 85));

      const li = document.createElement('li');
      li.className = 'rotation-item' +
        (name === curr ? ' current-driver' : '') +
        (name === next && name !== curr ? ' next-driver' : '');
      li.draggable = true;
      li.dataset.name = name;
      li.innerHTML = `
        <span class="drag-handle">⠿</span>
        <span class="rotation-pos">${i + 1}</span>
        <span class="rotation-name">${capitalize(name)}</span>
        <span class="rotation-weight text-muted">${dInfo.weight_kg ? dInfo.weight_kg + 'kg' : ''}</span>
        <span class="rotation-ballast">${ballast > 0 ? '+' + ballast.toFixed(1) + 'kg' : '✓'}</span>
        ${name === curr ? '<span class="text-green font-bold">IN</span>' : ''}
        ${name === next && name !== curr ? '<span class="text-blue font-bold">NEXT</span>' : ''}
      `;

      li.addEventListener('dragstart', (e) => {
        dragSrc = li;
        li.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
      });
      li.addEventListener('dragend', () => li.classList.remove('dragging'));
      li.addEventListener('dragover', (e) => { e.preventDefault(); li.classList.add('drag-over'); });
      li.addEventListener('dragleave', () => li.classList.remove('drag-over'));
      li.addEventListener('drop', (e) => {
        e.preventDefault();
        li.classList.remove('drag-over');
        if (dragSrc && dragSrc !== li) {
          const items = [...list.querySelectorAll('.rotation-item')];
          const srcIdx = items.indexOf(dragSrc);
          const dstIdx = items.indexOf(li);
          if (srcIdx < dstIdx) list.insertBefore(dragSrc, li.nextSibling);
          else list.insertBefore(dragSrc, li);
          saveRotation();
        }
      });
      list.appendChild(li);
    });
  }

  async function saveRotation() {
    const items = el('rotation-list').querySelectorAll('.rotation-item');
    const order = [...items].map(i => i.dataset.name);
    await apiPut('/api/drivers/rotation', { order });
  }

  // ── Alerts ─────────────────────────────────────────────────────────────────
  function renderAlerts(alerts) {
    const list = el('alerts-list');
    list.innerHTML = '';

    if (!alerts || alerts.length === 0) {
      list.innerHTML = '<div class="text-muted" style="font-size:13px">No active alerts</div>';
    }

    // Top banner for critical visor/maintenance
    const criticalVisor = alerts.find(a => a.id === 'visor_on');
    const criticalMaint = alerts.find(a => a.severity === 'CRITICAL' && a.id.includes('maint') && a.id.includes('now'));
    const banner = el('alert-banner');

    if (criticalVisor) {
      banner.textContent = '🔴 CLEAR VISOR MANDATORY — BLACK FLAG RISK (21:00–05:30 BST)';
      banner.className = 'visor-critical';
    } else if (criticalMaint) {
      banner.textContent = `🟠 ${criticalMaint.message.toUpperCase()}`;
      banner.className = 'maintenance-critical';
    } else {
      banner.textContent = '';
      banner.className = '';
    }

    alerts.forEach(a => {
      const div = document.createElement('div');
      div.className = `alert-item ${a.severity}`;
      div.innerHTML = `
        <span>${a.message}</span>
        ${a.dismissible ? `<button class="alert-dismiss" onclick="Dashboard.dismissAlert('${a.id}')">✕</button>` : ''}
      `;
      list.appendChild(div);
    });
  }

  function dismissAlert(id) {
    apiPost(`/api/alerts/dismiss/${id}`, {});
  }

  function markMaintenanceDone(stopId) {
    apiPost(`/api/alerts/maintenance-done/${stopId}`, {});
  }

  // ── Maintenance Countdowns ─────────────────────────────────────────────────
  function updateMaintenanceCountdowns() {
    const now = Date.now();
    updateMaintStop('maint-1', MAINT_1_UTC, now);
    updateMaintStop('maint-2', MAINT_2_UTC, now);
  }

  function updateMaintStop(id, targetMs, now) {
    const panel = el(id);
    const countdown = el(`${id}-countdown`);
    const diff = targetMs - now;

    if (diff <= 0 && now < targetMs + 10 * 60 * 1000) {
      // Within 10-minute window
      panel.className = 'maint-stop critical';
      countdown.textContent = 'PIT NOW';
    } else if (diff <= 0) {
      // Done
      panel.className = 'maint-stop done';
      countdown.innerHTML = '<span class="maint-done-badge">✓</span>';
    } else if (diff < 30 * 60 * 1000) {
      panel.className = 'maint-stop warning';
      countdown.textContent = fmtDuration(diff / 1000);
    } else {
      panel.className = 'maint-stop';
      countdown.textContent = fmtDuration(diff / 1000);
    }
  }

  // ── Stint Timer ─────────────────────────────────────────────────────────────
  function startStintTimer(initialS) {
    clearInterval(stintTimerInterval);
    stintTimerInterval = setInterval(() => {
      const elapsed = stintStartTime ? Math.floor((Date.now() - stintStartTime) / 1000) : initialS;
      const timerEl = el('curr-timer');
      timerEl.textContent = fmtDuration(elapsed);
      if (elapsed > 150 * 60) timerEl.className = 'stint-timer danger';
      else if (elapsed > 90 * 60) timerEl.className = 'stint-timer warning';
      else timerEl.className = 'stint-timer';
    }, 1000);
  }

  // ── Clock ──────────────────────────────────────────────────────────────────
  function startClock() {
    clockInterval = setInterval(() => {
      const now = new Date();
      el('clock').textContent = now.toLocaleTimeString('en-GB', { timeZone: 'Europe/London' });
      updateMaintenanceCountdowns();
      checkVisorWarning(now.getTime());
    }, 1000);
  }

  function checkVisorWarning(nowMs) {
    // Client-side visor check (belt-and-braces alongside server alerts)
    if (nowMs >= VISOR_START_UTC && nowMs < VISOR_END_UTC) {
      const banner = el('alert-banner');
      if (!banner.className.includes('visor-critical')) {
        banner.textContent = '🔴 CLEAR VISOR MANDATORY — BLACK FLAG RISK (21:00–05:30 BST)';
        banner.className = 'visor-critical';
      }
    }
  }

  // ── Scraper Status ─────────────────────────────────────────────────────────
  function updateScraperStatus(scraper) {
    const dot = el('scraper-dot');
    const label = el('scraper-label');
    if (scraper.running) {
      dot.className = 'scraper-dot running';
      label.textContent = 'Scraper live';
    } else if (scraper.error) {
      dot.className = 'scraper-dot error';
      label.textContent = 'Scraper error';
    } else {
      dot.className = 'scraper-dot';
      label.textContent = 'Scraper off';
    }
    const disp = el('scraper-status-display');
    if (disp) {
      disp.textContent = scraper.running
        ? `Running · Last poll: ${scraper.last_poll || 'never'}`
        : scraper.error ? `Error: ${scraper.error}` : 'Stopped';
    }
  }

  // ── Driver Selects ─────────────────────────────────────────────────────────
  function populateDriverSelects(drivers) {
    const currName = state.current_driver && state.current_driver.name;
    const lapModalOpen = el('modal-lap-overlay').classList.contains('open');
    ['lap-driver', 'swap-driver', 'prac-driver'].forEach(id => {
      const sel = el(id);
      if (!sel) return;
      const previous = sel.value;
      sel.innerHTML = drivers.map(d =>
        `<option value="${d.name}">${capitalize(d.name)}</option>`
      ).join('');
      // lap-driver always tracks the current driver — unless user is mid-edit in the open modal
      if (id === 'lap-driver' && currName && !lapModalOpen) {
        sel.value = currName;
      } else if (previous) {
        sel.value = previous;
      }
    });
  }

  // ── Modals ─────────────────────────────────────────────────────────────────
  function openLapModal() {
    const curr = state.current_driver && state.current_driver.name;
    if (curr) el('lap-driver').value = curr;
    el('modal-lap-overlay').classList.add('open');
    el('lap-time').focus();
  }

  function openSwapModal() {
    const next = state.next_driver;
    if (next) el('swap-driver').value = next.name || '';

    // Pre-fill swap-lap with the next lap (operator can override for catch-up swaps)
    const currLap = state.current_lap || 0;
    el('swap-lap').value = currLap + 1;

    // BEFORE refuel → model's current estimate of remaining fuel
    const level = state.fuel && state.fuel.level_L;
    el('swap-fuel-before').value = level != null ? level.toFixed(2) : '';

    // AFTER refuel → full tank by default
    const tank = state.fuel && state.fuel.capacity_L;
    el('swap-fuel-after').value = tank != null ? tank : '';

    updateSwapPreview();
    el('modal-swap-overlay').classList.add('open');
  }

  function updateSwapPreview() {
    const curr = state.current_driver || {};
    const next = state.next_driver || {};
    const fuel = state.fuel || {};
    const before = parseFloat(el('swap-fuel-before').value);
    const after = parseFloat(el('swap-fuel-after').value);

    // Stint-consumption preview: (stint start fuel) − (fuel before refuel) over laps_in_kart
    const stintStart = curr.stint_start_fuel_L != null
      ? curr.stint_start_fuel_L
      : fuel.capacity_L;
    const lapsInKart = curr.laps_in_kart || 0;
    let fuelLine = '';
    if (!isNaN(before) && lapsInKart > 0 && stintStart != null) {
      const burnt = Math.max(0, stintStart - before);
      const lpl = burnt / lapsInKart;
      fuelLine = `<br>Stint burn: <strong>${burnt.toFixed(2)} L</strong> over ${lapsInKart} laps = <strong>${lpl.toFixed(3)} L/lap</strong> → EMA`;
    }
    let refillLine = '';
    if (!isNaN(before) && !isNaN(after)) {
      const added = Math.max(0, after - before);
      refillLine = `<br>Refuel: +${added.toFixed(2)} L (new tank ${after.toFixed(2)} L)`;
    }

    const delta = next.ballast_delta_kg || 0;
    const sign = delta > 0 ? 'ADD' : delta < 0 ? 'REMOVE' : 'NO CHANGE';
    const driverLine = (curr.name && next.name)
      ? `<strong>${capitalize(curr.name)}</strong> → <strong>${capitalize(next.name)}</strong><br>
         Ballast: ${sign} ${Math.abs(delta).toFixed(1)} kg<br>
         Pedal: ${curr.pedal_pos} → ${next.pedal_pos}`
      : '';

    el('swap-preview').innerHTML = driverLine + fuelLine + refillLine;
  }

  function openFuelModal()     { el('modal-fuel-overlay').classList.add('open'); }
  function openPracticeModal() { el('modal-practice-overlay').classList.add('open'); }
  function openScraperPanel()  { el('scraper-panel-overlay').classList.add('open'); }

  function closeModal(id) { el(id).classList.remove('open'); }

  function openSettings() {
    buildDriverSettings();
    const tank = state.fuel && state.fuel.capacity_L;
    if (tank) el('set-tank').value = tank;
    if (state.fuel) el('set-fuel-level').value = state.fuel.level_L || '';
    el('settings-panel').classList.add('open');
  }
  function closeSettings() { el('settings-panel').classList.remove('open'); }

  function buildDriverSettings() {
    const list = el('driver-settings-list');
    list.innerHTML = '';
    const drivers = state.drivers || [];
    drivers.forEach(d => {
      const row = document.createElement('div');
      row.className = 'driver-settings-row';
      row.innerHTML = `
        <span class="driver-settings-name">${capitalize(d.name)}</span>
        <span class="detail-label" style="width:60px">Weight</span>
        <input type="number" step="0.1" min="50" max="130" value="${d.weight_kg}" data-driver="${d.name}" data-field="weight_kg" style="width:70px">
        <span class="detail-label" style="width:50px;margin-left:8px">Pedal</span>
        <input type="text" value="${d.pedal_pos}" data-driver="${d.name}" data-field="pedal_pos" style="width:50px">
      `;
      list.appendChild(row);
    });
  }

  async function saveSettings() {
    const rows = el('driver-settings-list').querySelectorAll('input');
    const updates = {};
    rows.forEach(inp => {
      const driver = inp.dataset.driver;
      const field = inp.dataset.field;
      if (!updates[driver]) updates[driver] = {};
      updates[driver][field] = field === 'weight_kg' ? parseFloat(inp.value) : inp.value;
    });
    for (const [name, data] of Object.entries(updates)) {
      await apiPut(`/api/drivers/${name}`, data);
    }
    const tank = parseFloat(el('set-tank').value);
    const margin = parseInt(el('set-margin').value) || 2;
    const fuelLevel = parseFloat(el('set-fuel-level').value);
    if (tank) await apiPut('/api/fuel/settings', { tank_capacity_L: tank, safety_margin_laps: margin });
    if (fuelLevel) await apiPost('/api/fuel/fill', { litres_added: 0, fuel_level_L: fuelLevel });
    closeSettings();
  }

  // ── Form submissions ───────────────────────────────────────────────────────
  async function submitLap() {
    const driver = el('lap-driver').value;
    const lap_time = el('lap-time').value;
    const flag = el('lap-flag').value;
    const is_rain = el('lap-rain').checked;
    if (!lap_time) return;
    const res = await apiPost('/api/laps', { driver, lap_time, flag, is_rain });
    if (res.ok !== false) {
      el('lap-time').value = '';
      closeModal('modal-lap-overlay');
    }
  }

  async function submitSwap() {
    const next_driver = el('swap-driver').value;
    const fuel_before = el('swap-fuel-before').value;
    const fuel_after = el('swap-fuel-after').value;
    const swap_lap = el('swap-lap').value;
    const body = { next_driver };
    if (fuel_before !== '') body.fuel_before_L = parseFloat(fuel_before);
    if (fuel_after  !== '') body.fuel_after_L  = parseFloat(fuel_after);
    if (swap_lap)           body.swap_lap     = parseInt(swap_lap);
    const result = await apiPost('/api/stints/end', body);
    if (result && result.detail) {
      alert(`Swap failed: ${result.detail}`);
      return;
    }
    if (result && result.reassigned_laps > 0) {
      console.log(`Reassigned ${result.reassigned_laps} laps to ${result.driver}`);
    }
    el('swap-fuel-before').value = '';
    el('swap-fuel-after').value = '';
    el('swap-lap').value = '';
    closeModal('modal-swap-overlay');
  }

  async function submitFuelFill() {
    const litres = parseFloat(el('fill-litres').value) || 0;
    const total = el('fill-total').value;
    const body = { litres_added: litres };
    if (total) body.fuel_level_L = parseFloat(total);
    await apiPost('/api/fuel/fill', body);
    el('fill-litres').value = '';
    el('fill-total').value = '';
    closeModal('modal-fuel-overlay');
  }

  async function submitPractice() {
    const body = {
      driver_name: el('prac-driver').value,
      fuel_start_L: parseFloat(el('prac-fuel-start').value) || 0,
      fuel_end_L: parseFloat(el('prac-fuel-end').value) || 0,
      laps_completed: parseInt(el('prac-laps').value) || 0,
      flag_condition: el('prac-flag').value,
      notes: el('prac-notes').value,
    };
    const avgLap = el('prac-avg-lap').value;
    if (avgLap) {
      // Convert m:ss.xxx to ms client-side
      const parts = avgLap.split(':');
      const secParts = (parts[1] || parts[0]).split('.');
      const mins = parts.length > 1 ? parseInt(parts[0]) : 0;
      const secs = parseInt(secParts[0]);
      const ms = secParts[1] ? parseInt(secParts[1].padEnd(3,'0').slice(0,3)) : 0;
      body.avg_lap_time_ms = (mins * 60 + secs) * 1000 + ms;
    }
    await apiPost('/api/practice', body);
    closeModal('modal-practice-overlay');
  }

  async function startRace() {
    await apiPost('/api/race/start', {});
    el('start-btn').textContent = 'Race Running';
    el('start-btn').disabled = true;
  }

  async function resetRace() {
    const typed = prompt(
      'This deletes ALL laps, stints, fuel fills, and standings for the current race.\n' +
      'Practice data, driver setup, and tank settings are kept.\n\n' +
      'Type RESET to confirm:'
    );
    if (typed !== 'RESET') return;
    const r = await apiPost('/api/race/reset', {});
    if (r && r.detail) { alert('Reset failed: ' + r.detail); return; }
    if (r && r.ok) {
      alert(`Race reset: removed ${r.laps_deleted} laps, ${r.stints_deleted} stints, ${r.fuel_fills_deleted} fuel fills.`);
      el('start-btn').textContent = 'Start Race';
      el('start-btn').disabled = false;
      closeSettings();
    }
  }

  async function startScraper() {
    const session_url = el('scraper-url').value.trim() || undefined;
    const team_number = el('scraper-team').value.trim() || undefined;
    if (!session_url && !team_number) { alert('Enter a session URL or team number'); return; }
    await apiPost('/api/scraper/start', { session_url, team_number });
    closeModal('scraper-panel-overlay');
  }

  async function stopScraper() {
    await apiPost('/api/scraper/stop', {});
  }

  // ── API helpers ─────────────────────────────────────────────────────────────
  async function apiPost(path, body) {
    try {
      const r = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return await r.json();
    } catch (e) { console.error(path, e); return { error: e.message }; }
  }

  async function apiPut(path, body) {
    try {
      const r = await fetch(path, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      return await r.json();
    } catch (e) { console.error(path, e); return { error: e.message }; }
  }

  async function apiDelete(path) {
    try {
      const r = await fetch(path, { method: 'DELETE' });
      return await r.json();
    } catch (e) { console.error(path, e); return { error: e.message }; }
  }

  // ── Fuel Model modal ───────────────────────────────────────────────────────
  let _fuelBreakdown = null;
  let _editingPracticeId = null;
  let _editingStintId = null;

  async function openFuelModelModal() {
    el('modal-fuelmodel-overlay').classList.add('open');
    await loadFuelBreakdown();
  }

  async function loadFuelBreakdown() {
    try {
      const r = await fetch('/api/fuel/breakdown');
      _fuelBreakdown = await r.json();
      renderFuelModel();
    } catch (e) {
      el('fuelmodel-body').textContent = 'Failed to load: ' + e.message;
    }
  }

  function renderFuelModel() {
    if (!_fuelBreakdown) return;
    const { inputs, ema, calculation: c } = _fuelBreakdown;
    const practiceRows = inputs.practice.map(p => practiceRowHtml(p)).join('') || `<div class="text-muted" style="padding:6px">No practice sessions recorded.</div>`;
    const stintRows = inputs.stints.map(s => stintRowHtml(s)).join('') || `<div class="text-muted" style="padding:6px">No completed stints yet.</div>`;

    const initLabel = ema.initialised ? '' : ` <span class="text-muted">(uninitialised — using default ${ema.default_Lpl_if_uninitialised})</span>`;

    el('fuelmodel-body').innerHTML = `
      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-muted);margin-bottom:4px">
        Inputs — contributing to L/lap EMA (α = ${ema.alpha})
      </div>
      <div style="margin-bottom:10px"><strong>Practice sessions</strong> (${inputs.practice.length})</div>
      <div style="background:#0f172a;border:1px solid var(--card-border);border-radius:6px;padding:6px;margin-bottom:14px">${practiceRows}</div>

      <div style="margin-bottom:10px"><strong>Stints</strong> (${inputs.stints.length} completed)</div>
      <div style="background:#0f172a;border:1px solid var(--card-border);border-radius:6px;padding:6px;margin-bottom:14px">${stintRows}</div>

      <div style="padding:8px;background:#0b1120;border-radius:6px;margin-bottom:14px">
        <strong>Current EMA value:</strong> ${ema.current_Lpl.toFixed(4)} L/lap${initLabel}
      </div>

      <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-muted);margin-bottom:8px">
        Calculation — live, same values as Fuel panel
      </div>
      <div style="background:#0f172a;border:1px solid var(--card-border);border-radius:6px;padding:10px;font-variant-numeric:tabular-nums">
        ${calcRow('Current fuel level',        `${c.current_level_L.toFixed(2)} L`)}
        ${calcRow('÷ Avg consumption',         `${c.consumption_Lpl.toFixed(4)} L/lap`, 'from EMA above')}
        ${calcRow('× Flag multiplier',         `×${c.flag_multiplier.toFixed(2)}`,       `current flag: ${c.current_flag}`)}
        ${calcRow('= Effective L/lap',         `${c.effective_Lpl.toFixed(4)} L/lap`)}
        <hr style="border:none;border-top:1px dashed var(--card-border);margin:6px 0">
        ${calcRow('= Laps to empty',           c.laps_to_empty.toFixed(2))}
        ${calcRow('− Safety margin',           `−${c.safety_margin_laps}`)}
        ${calcRow('= Laps until pit',          `<strong style="color:var(--green)">${c.laps_until_pit.toFixed(2)}</strong>`, 'shown in Fuel panel')}
        <hr style="border:none;border-top:1px dashed var(--card-border);margin:6px 0">
        ${calcRow('× Avg lap time',            `${c.avg_lap_s.toFixed(2)} s`)}
        ${calcRow('= Time to pit',             `<strong>${fmtDuration(c.time_to_pit_s)}</strong>`)}
        <hr style="border:none;border-top:1px dashed var(--card-border);margin:6px 0">
        ${calcRow('Stops remaining',           c.stops_remaining, `${fmtDuration(c.race_time_remaining_s)} of race left`)}
      </div>
    `;
  }

  function calcRow(label, value, note) {
    return `<div style="display:flex;justify-content:space-between;padding:2px 0">
      <span>${label}${note ? ` <span class="text-muted" style="font-size:11px">— ${note}</span>` : ''}</span>
      <span>${value}</span>
    </div>`;
  }

  function practiceRowHtml(p) {
    if (_editingPracticeId === p.id) return practiceEditRow(p);
    const raw = p.raw_Lpl != null ? p.raw_Lpl.toFixed(3) : '—';
    const norm = p.normalised_Lpl != null ? p.normalised_Lpl.toFixed(3) : '—';
    return `
      <div style="padding:6px 4px;border-bottom:1px solid var(--card-border)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span><strong style="text-transform:capitalize">${p.driver_name}</strong>
            · ${(+p.fuel_start_L).toFixed(2)}→${(+p.fuel_end_L).toFixed(2)} L
            · ${p.laps_completed} laps
            · <span class="flag-badge flag-${p.flag_condition}">${p.flag_condition}</span></span>
          <span>
            <button class="btn" style="padding:2px 6px;font-size:11px" onclick="Dashboard.editPractice(${p.id})">✎</button>
            <button class="btn danger" style="padding:2px 6px;font-size:11px" onclick="Dashboard.deletePractice(${p.id})">✕</button>
          </span>
        </div>
        <div class="text-muted" style="font-size:11px">raw ${raw} L/lap → normalised ${norm} L/lap${p.notes ? ' · ' + p.notes : ''}</div>
      </div>`;
  }

  function practiceEditRow(p) {
    return `
      <div style="padding:6px 4px;border-bottom:1px solid var(--card-border);background:#1e293b">
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr auto;gap:4px;align-items:end">
          <label class="text-muted" style="font-size:11px">Fuel start
            <input id="ep-fs-${p.id}" type="number" step="0.1" value="${p.fuel_start_L}" style="width:100%"></label>
          <label class="text-muted" style="font-size:11px">Fuel end
            <input id="ep-fe-${p.id}" type="number" step="0.1" value="${p.fuel_end_L}" style="width:100%"></label>
          <label class="text-muted" style="font-size:11px">Laps
            <input id="ep-l-${p.id}"  type="number" min="1"    value="${p.laps_completed}" style="width:100%"></label>
          <label class="text-muted" style="font-size:11px">Flag
            <select id="ep-f-${p.id}" style="width:100%">
              ${['GREEN','YELLOW','SC','RED'].map(f => `<option ${f===p.flag_condition?'selected':''}>${f}</option>`).join('')}
            </select></label>
          <span>
            <button class="btn primary" style="padding:2px 8px;font-size:11px" onclick="Dashboard.savePractice(${p.id})">Save</button>
            <button class="btn" style="padding:2px 6px;font-size:11px" onclick="Dashboard.cancelPracticeEdit()">×</button>
          </span>
        </div>
      </div>`;
  }

  function stintRowHtml(s) {
    if (_editingStintId === s.id) return stintEditRow(s);
    const raw = s.raw_Lpl != null ? s.raw_Lpl.toFixed(3) : '—';
    return `
      <div style="padding:6px 4px;border-bottom:1px solid var(--card-border)">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span><strong style="text-transform:capitalize">${s.driver_name}</strong>
            · laps ${s.start_lap}–${s.end_lap}
            · ${s.fuel_start_L != null ? (+s.fuel_start_L).toFixed(2) : '—'}→${s.fuel_end_L != null ? (+s.fuel_end_L).toFixed(2) : '—'} L</span>
          <span>
            <button class="btn" style="padding:2px 6px;font-size:11px" onclick="Dashboard.editStint(${s.id})">✎</button>
            <button class="btn danger" style="padding:2px 6px;font-size:11px" onclick="Dashboard.deleteStint(${s.id})">✕</button>
          </span>
        </div>
        <div class="text-muted" style="font-size:11px">raw ${raw} L/lap (assumed GREEN)</div>
      </div>`;
  }

  function stintEditRow(s) {
    return `
      <div style="padding:6px 4px;border-bottom:1px solid var(--card-border);background:#1e293b">
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr auto;gap:4px;align-items:end">
          <label class="text-muted" style="font-size:11px">Fuel start
            <input id="es-fs-${s.id}" type="number" step="0.1" value="${s.fuel_start_L ?? ''}" style="width:100%"></label>
          <label class="text-muted" style="font-size:11px">Fuel end
            <input id="es-fe-${s.id}" type="number" step="0.1" value="${s.fuel_end_L ?? ''}"   style="width:100%"></label>
          <label class="text-muted" style="font-size:11px">Start lap
            <input id="es-sl-${s.id}" type="number" min="0"    value="${s.start_lap ?? ''}"    style="width:100%"></label>
          <label class="text-muted" style="font-size:11px">End lap
            <input id="es-el-${s.id}" type="number" min="0"    value="${s.end_lap ?? ''}"      style="width:100%"></label>
          <span>
            <button class="btn primary" style="padding:2px 8px;font-size:11px" onclick="Dashboard.saveStint(${s.id})">Save</button>
            <button class="btn" style="padding:2px 6px;font-size:11px" onclick="Dashboard.cancelStintEdit()">×</button>
          </span>
        </div>
      </div>`;
  }

  function editPractice(id) { _editingPracticeId = id; renderFuelModel(); }
  function cancelPracticeEdit() { _editingPracticeId = null; renderFuelModel(); }

  async function savePractice(id) {
    const body = {
      fuel_start_L: parseFloat(el(`ep-fs-${id}`).value),
      fuel_end_L:   parseFloat(el(`ep-fe-${id}`).value),
      laps_completed: parseInt(el(`ep-l-${id}`).value),
      flag_condition: el(`ep-f-${id}`).value,
    };
    const r = await apiPut(`/api/practice/${id}`, body);
    if (r && r.detail) { alert('Save failed: ' + r.detail); return; }
    _editingPracticeId = null;
    await loadFuelBreakdown();
  }

  async function deletePractice(id) {
    if (!confirm('Delete this practice session? Fuel EMA will recompute.')) return;
    const r = await apiDelete(`/api/practice/${id}`);
    if (r && r.detail) { alert('Delete failed: ' + r.detail); return; }
    await loadFuelBreakdown();
  }

  function editStint(id) { _editingStintId = id; renderFuelModel(); }
  function cancelStintEdit() { _editingStintId = null; renderFuelModel(); }

  async function saveStint(id) {
    const body = {
      fuel_start_L: parseFloat(el(`es-fs-${id}`).value),
      fuel_end_L:   parseFloat(el(`es-fe-${id}`).value),
      start_lap:    parseInt(el(`es-sl-${id}`).value),
      end_lap:      parseInt(el(`es-el-${id}`).value),
    };
    const r = await apiPut(`/api/stints/${id}`, body);
    if (r && r.detail) { alert('Save failed: ' + r.detail); return; }
    _editingStintId = null;
    await loadFuelBreakdown();
  }

  async function deleteStint(id) {
    if (!confirm('Delete this stint AND all laps tagged to it? This cascades — laps will be permanently removed.')) return;
    const r = await apiDelete(`/api/stints/${id}`);
    if (r && r.detail) { alert('Delete failed: ' + r.detail); return; }
    await loadFuelBreakdown();
  }

  // ── Fuel tooltip on the laps-to-pit number ─────────────────────────────────
  function showFuelTooltip(anchor) {
    const f = state.fuel || {};
    const lm = state.lap_model || {};
    if (f.laps_until_pit == null) return;
    const tip = el('fuel-tooltip');
    tip.innerHTML = `
      <div style="margin-bottom:4px;font-weight:600">Laps until pit</div>
      <div style="font-variant-numeric:tabular-nums">
        ${f.level_L != null ? f.level_L.toFixed(2) : '—'} L  ÷  ${f.avg_consumption_Lpl != null ? f.avg_consumption_Lpl.toFixed(3) : '—'} L/lap<br>
        = ${(f.laps_to_empty ?? 0).toFixed(2)} laps to empty<br>
        − ${state.safety_margin_laps ?? 2} safety = <strong>${(f.laps_until_pit ?? 0).toFixed(2)}</strong><br>
        × ${(lm.avg_lap_s ?? 0).toFixed(2)} s/lap = ${fmtDuration(f.time_to_pit_s ?? 0)}
      </div>
      <div class="text-muted" style="font-size:11px;margin-top:4px">Click ⚙ in Fuel header for full breakdown</div>
    `;
    const rect = anchor.getBoundingClientRect();
    tip.style.left = `${Math.min(window.innerWidth - 300, rect.left)}px`;
    tip.style.top  = `${rect.bottom + 6}px`;
    tip.style.display = 'block';
  }

  function hideFuelTooltip() { el('fuel-tooltip').style.display = 'none'; }

  // ── Formatting ──────────────────────────────────────────────────────────────
  function fmtDuration(s) {
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
  }

  function fmtLapTime(ms) {
    ms = Math.round(ms);
    const totalS = Math.floor(ms / 1000);
    const m = Math.floor(totalS / 60);
    const s = totalS % 60;
    const rem = ms % 1000;
    return `${m}:${String(s).padStart(2,'0')}.${String(rem).padStart(3,'0')}`;
  }

  function capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
  function el(id) { return document.getElementById(id); }

  // ── Init ───────────────────────────────────────────────────────────────────
  function init() {
    initChart();
    startClock();
    connect();
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  return {
    openLapModal, openSwapModal, openFuelModal, openPracticeModal,
    openScraperPanel, openSettings, closeModal, closeSettings,
    openStandingsModal, submitStandings,
    submitLap, submitSwap, submitFuelFill, submitPractice,
    updateSwapPreview,
    startRace, resetRace, startScraper, stopScraper,
    dismissAlert, markMaintenanceDone,
    saveSettings,
    openFuelModelModal,
    editPractice, cancelPracticeEdit, savePractice, deletePractice,
    editStint, cancelStintEdit, saveStint, deleteStint,
    showFuelTooltip, hideFuelTooltip,
    init,
  };
})();

document.addEventListener('DOMContentLoaded', () => Dashboard.init());
