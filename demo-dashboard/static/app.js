/* Lightwell SDLC dashboard — front end.
   Everything that touches the cluster goes through the local API; this file only
   renders and polls. */

const $ = (sel) => document.querySelector(sel);

const state = {
  packages: [],
  runs: [],
  activeRun: null,
  poll: null,
};

// --------------------------------------------------------------- helpers

async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON error page */
  }
  if (!res.ok) throw new Error(body?.error || `${res.status} ${res.statusText}`);
  return body;
}

function toast(message, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 12000 : 6000);
}

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]
  );

// --------------------------------------------------------------- header

async function loadConfig() {
  const cfg = await api('/api/config');
  $('#tenant').innerHTML =
    `<span class="chip">tenant <strong>${esc(cfg.guid)}</strong></span>`;
  $('#consoles').innerHTML = Object.entries(cfg.consoles)
    .map(([name, url]) => `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(name)}</a>`)
    .join('');
}

// --------------------------------------------------------------- packages

const TAG = {
  patch: ['patch', 'Drop-in'],
  'cross-version': ['cross', 'Cross-version'],
  downgrade: ['down', 'Downgrade'],
  superseded: ['down', 'Superseded'],
};

function versionRow(pkg, v) {
  const [tagClass, tagText] = TAG[v.direction] || ['consumed', v.direction];
  const classes = ['ver'];
  if (v.consumed) classes.push('consumed');
  if (v.usable) classes.push('usable');
  if (v.direction === 'downgrade' || v.direction === 'superseded') classes.push('downgrade');

  const tag = v.consumed
    ? '<span class="tag consumed">Consumed</span>'
    : `<span class="tag ${tagClass}">${tagText}</span>`;

  const note = v.consumed
    ? 'Already cached on this tenant — re-fetching emits no event.'
    : v.note;

  const button = v.usable
    ? `<button class="btn primary remediate"
         data-group="${esc(pkg.group)}"
         data-artifact="${esc(pkg.artifact)}"
         data-version="${esc(v.version)}"
         data-direction="${esc(v.direction)}"
         data-note="${esc(v.note)}">Remediate</button>`
    : '';

  return `<div class="${classes.join(' ')}">
    <span class="ver-num">${esc(v.version)}</span>
    ${tag}
    <span class="ver-note">${esc(note)}</span>
    ${button}
  </div>`;
}

function renderPackages() {
  const host = $('#packages');
  if (!state.packages.length) {
    host.innerHTML =
      '<p class="placeholder">No declared dependency has a remediated build published upstream.</p>';
    return;
  }
  host.innerHTML = state.packages
    .map(
      (p) => `<article class="pkg ${p.any_usable ? 'actionable' : 'spent'}">
        <div class="pkg-head">
          <span class="pkg-name">${esc(p.artifact)}</span>
          <span class="pkg-group">${esc(p.group)}</span>
          <span class="declared">pom declares <b>${esc(p.declared)}</b></span>
        </div>
        <div class="versions">${p.versions.map((v) => versionRow(p, v)).join('')}</div>
      </article>`
    )
    .join('');
}

async function loadPackages() {
  $('#reload').classList.add('busy');
  try {
    const data = await api('/api/packages');
    state.packages = data.packages;
    renderPackages();
  } catch (e) {
    $('#packages').innerHTML = `<p class="placeholder">${esc(e.message)}</p>`;
    toast(e.message, 'bad');
  } finally {
    $('#reload').classList.remove('busy');
  }
}

// --------------------------------------------------------------- trigger

let pending = null;

document.addEventListener('click', (ev) => {
  const btn = ev.target.closest('.remediate');
  if (!btn) return;
  pending = { ...btn.dataset, button: btn };
  $('#confirm-body').innerHTML =
    `<code>${esc(pending.artifact)}</code> will be bumped to
     <code>${esc(pending.version)}</code>.`;
  $('#confirm-note').textContent = pending.note;
  $('#confirm').showModal();
});

$('#confirm').addEventListener('close', async (ev) => {
  const dialog = ev.target;
  if (dialog.returnValue !== 'go' || !pending) return;
  const { group, artifact, version, button } = pending;
  pending = null;

  button.classList.add('busy');
  button.disabled = true;
  try {
    const { run } = await api('/api/remediate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group, artifact, version }),
    });
    toast(`Triggered ${artifact} ${version}`, 'good');
    await loadRuns();
    selectRun(run.id);
    loadPackages();
  } catch (e) {
    toast(e.message, 'bad');
    button.classList.remove('busy');
    button.disabled = false;
  }
});

// --------------------------------------------------------------- timeline

// Stages that have resolved show a symbol; the rest keep their step number so
// the timeline still reads as an ordered list.
const MARK = { done: '✓', error: '!', stalled: '?', running: '·', pending: '·' };
const RESOLVED = ['done', 'error', 'stalled'];

function renderSteps(run, steps) {
  $('#run-sub').innerHTML =
    `<code>${esc(run.artifact)} ${esc(run.version)}</code> &mdash; started ${esc(
      new Date(run.started * 1000).toLocaleTimeString()
    )}`;

  $('#timeline').classList.remove('empty');
  $('#timeline').innerHTML = steps
    .map((s, i) => {
      const console_ = s.console
        ? `<a class="console" href="${esc(s.console)}" target="_blank" rel="noopener">open console &rarr;</a>`
        : '';
      const detail = s.detail ? `<p class="detail">${esc(s.detail)}</p>` : '';
      const links = s.links.length
        ? `<div class="links">${s.links
            .map(
              (l) => `<a class="link ${esc(l.state || '')}" href="${esc(l.url)}"
                        target="_blank" rel="noopener">
                        <span class="label">${esc(l.label)}</span>
                        ${l.badge ? `<span class="badge">${esc(l.badge)}</span>` : ''}
                        <span class="arrow">&rarr;</span>
                      </a>`
            )
            .join('')}</div>`
        : '';
      return `<div class="stage ${esc(s.state)}">
        <div class="marker">${RESOLVED.includes(s.state) ? MARK[s.state] : i + 1}</div>
        <div>
          <h3>${esc(s.title)} ${console_}</h3>
          <p class="what">${esc(s.what)}</p>
          ${detail}
          ${links}
        </div>
      </div>`;
    })
    .join('');
}

async function refreshRun() {
  if (!state.activeRun) return;
  try {
    const { run, steps } = await api(`/api/run/${state.activeRun}`);
    renderSteps(run, steps);
  } catch (e) {
    toast(e.message, 'bad');
  }
}

function selectRun(id) {
  state.activeRun = id;
  $('#run-picker').value = id;
  clearInterval(state.poll);
  refreshRun();
  // Cheap enough to poll: six API calls, all read-only.
  state.poll = setInterval(refreshRun, 6000);
}

async function loadRuns() {
  const { runs } = await api('/api/runs');
  state.runs = runs;
  const picker = $('#run-picker');
  picker.hidden = runs.length === 0;
  picker.innerHTML = runs
    .map(
      (r) =>
        `<option value="${esc(r.id)}">${esc(r.artifact)} ${esc(r.version)} — ${esc(
          new Date(r.started * 1000).toLocaleTimeString()
        )}</option>`
    )
    .join('');
  if (runs.length && !state.activeRun) selectRun(runs[0].id);
}

$('#run-picker').addEventListener('change', (e) => selectRun(e.target.value));
$('#reload').addEventListener('click', loadPackages);

// --------------------------------------------------------------- boot

(async () => {
  try {
    await loadConfig();
  } catch (e) {
    toast(`Cannot reach the cluster: ${e.message}`, 'bad');
  }
  loadPackages();
  loadRuns().catch(() => {});
})();
