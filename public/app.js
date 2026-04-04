// ── State ────────────────────────────────────────────────────────────────────
let members         = [];
let archivedMembers = [];
let chart           = null;
let openHistoryId   = null;
let openHistoryName = null;
let archiveOpen     = false;

// Member accent colors (consistent order)
const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ec4899'];

// Nicknames come from the DB (m.nickname field)
function nick(m) { return m.nickname || m.name; }

// ── Utilities ────────────────────────────────────────────────────────────────
function minsToHHMM(mins) {
  if (mins === null || mins === undefined) return '0:00';
  const sign = mins < 0 ? '-' : '';
  const abs  = Math.abs(mins);
  const h    = Math.floor(abs / 60);
  const m    = abs % 60;
  return `${sign}${h}:${String(m).padStart(2, '0')}`;
}

function parseHHMM(str) {
  const s = str.trim().replace(',', ':').replace('.', ':');
  const parts = s.split(':');
  if (parts.length !== 2) return null;
  const h = parseInt(parts[0], 10);
  const m = parseInt(parts[1], 10);
  if (isNaN(h) || isNaN(m) || m < 0 || m >= 60 || h < 0) return null;
  return h * 60 + m;
}

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

function formatDate(iso) {
  if (!iso) return '';
  const [y, mo, d] = iso.split('-');
  return `${d}/${mo}/${y}`;
}

// ── Auth ─────────────────────────────────────────────────────────────────────
async function checkAuth() {
  try {
    const res  = await fetch('/api/auth/status');
    const data = await res.json();
    data.authenticated ? showApp() : showLogin();
  } catch {
    showLogin();
  }
}

async function login() {
  const pw  = document.getElementById('password-input').value;
  const err = document.getElementById('login-error');
  err.textContent = '';
  try {
    const res = await fetch('/api/login', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ password: pw }),
    });
    if (res.ok) {
      showApp();
    } else {
      err.textContent = 'Fel lösenord — försök igen.';
    }
  } catch {
    err.textContent = 'Kunde inte kontakta servern.';
  }
}

async function logout() {
  await fetch('/api/logout', { method: 'POST' }).catch(() => {});
  showLogin();
}

function showLogin() {
  document.getElementById('login-screen').classList.remove('hidden');
  document.getElementById('app-screen').classList.add('hidden');
  document.getElementById('password-input').value = '';
}

function showApp() {
  document.getElementById('login-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');
  loadData();
}

// ── Data ─────────────────────────────────────────────────────────────────────
async function loadData() {
  try {
    const [r1, r2] = await Promise.all([
      fetch('/api/members'),
      fetch('/api/members/archived'),
    ]);
    members         = await r1.json();
    archivedMembers = await r2.json();
    renderChart();
    renderMembers();
    renderArchivedMembers();
    if (openHistoryId !== null) loadHistory(openHistoryId, openHistoryName);
  } catch (e) {
    console.error('Failed to load data', e);
  }
}

// ── Chart ────────────────────────────────────────────────────────────────────
function renderChart() {
  const ctx    = document.getElementById('balanceChart').getContext('2d');
  const labels = members.map(m => nick(m));
  const data   = members.map(m => Math.max(0, m.balance_minutes) / 60);
  const colors = members.map((_, i) => COLORS[i % COLORS.length]);

  if (chart) chart.destroy();

  chart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors,
        borderRadius:    10,
        borderSkipped:   false,
      }],
    },
    options: {
      responsive:          true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => {
              const m = members[ctx.dataIndex];
              return `  ${minsToHHMM(m.balance_minutes)} timmar`;
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { font: { weight: '600', size: 13 } },
        },
        y: {
          beginAtZero: true,
          grid: { color: '#f0f4f8' },
          ticks: {
            callback: val => `${val}h`,
            font: { size: 12 },
          },
          title: { display: true, text: 'Timmar', color: '#94a3b8', font: { size: 12 } },
        },
      },
    },
  });
}

// ── Members List ──────────────────────────────────────────────────────────────
function renderMembers() {
  const list = document.getElementById('members-list');
  if (!members.length) {
    list.innerHTML = '<p class="history-empty">Inga medlemmar hittades.</p>';
    return;
  }
  list.innerHTML = members.map((m, i) => {
    const color    = COLORS[i % COLORS.length];
    const neg      = m.balance_minutes < 0;
    const nickname = nick(m);
    const hasNick  = nickname !== m.name;
    return `
      <div class="member-row">
        <div class="member-left" onclick="toggleHistory(${m.id}, '${escHtml(m.name)}')">
          <div class="member-dot" style="background:${color}"></div>
          <span class="member-name" title="${hasNick ? escHtml(m.name) : ''}">${escHtml(nickname)}</span>
          ${hasNick ? `<span class="real-name">${escHtml(m.name)}</span>` : ''}
          <span class="member-name-hint">▸ visa historia</span>
        </div>
        <div class="member-balance ${neg ? 'neg' : ''}">${minsToHHMM(m.balance_minutes)}</div>
        <div class="member-btns">
          <button class="btn-icon btn-earn" title="Lägg till intjänad komp"
            onclick="openModal(${m.id},'earned')">+</button>
          <button class="btn-icon btn-take" title="Lägg till uttag"
            onclick="openModal(${m.id},'taken')">−</button>
          <button class="btn-icon btn-archive" title="Arkivera ${escHtml(nickname)}"
            onclick="archiveMember(${m.id},'${escHtml(nickname)}')">⊗</button>
        </div>
      </div>`;
  }).join('');
}

// ── History ───────────────────────────────────────────────────────────────────
async function toggleHistory(memberId, memberName, isArchived = false) {
  const card = document.getElementById('history-card');
  if (openHistoryId === memberId && !card.classList.contains('hidden')) {
    closeHistory();
  } else {
    openHistoryId   = memberId;
    openHistoryName = memberName;
    card.classList.remove('hidden');
    await loadHistory(memberId, memberName, isArchived);
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

async function loadHistory(memberId, memberName, isArchived = false) {
  const memberObj   = [...members, ...archivedMembers].find(x => x.name === memberName);
  const nickName    = memberObj ? nick(memberObj) : memberName;
  const displayName = nickName !== memberName ? `${nickName} (${memberName})` : memberName;
  const label = isArchived ? `Arkiv — ${displayName}` : `Historia — ${displayName}`;
  document.getElementById('history-title').textContent = label;
  const list = document.getElementById('entries-list');
  list.innerHTML = '<p class="history-empty">Laddar…</p>';

  try {
    const res     = await fetch(`/api/entries?member_id=${memberId}`);
    const entries = await res.json();

    if (!entries.length) {
      list.innerHTML = '<p class="history-empty">Ingen historik ännu.</p>';
      return;
    }

    list.innerHTML = entries.map(e => {
      const pos = e.minutes > 0;
      const delBtn = isArchived
        ? ''
        : `<button class="btn-del" onclick="deleteEntry(${e.id})" title="Ta bort">✕</button>`;
      return `
        <div class="entry-row">
          <div class="entry-date">${formatDate(e.date)}</div>
          <div class="entry-amount ${pos ? 'pos' : 'neg'}">
            ${pos ? '+' : ''}${minsToHHMM(e.minutes)}
          </div>
          <div class="entry-comment" title="${escHtml(e.comment || '')}">
            ${escHtml(e.comment || '—')}
          </div>
          ${delBtn}
        </div>`;
    }).join('');
  } catch {
    list.innerHTML = '<p class="history-empty">Kunde inte ladda historik.</p>';
  }
}

function closeHistory() {
  document.getElementById('history-card').classList.add('hidden');
  openHistoryId   = null;
  openHistoryName = null;
}

async function deleteEntry(id) {
  if (!confirm('Ta bort denna post?')) return;
  try {
    await fetch(`/api/entries/${id}`, { method: 'DELETE' });
    await loadData();
  } catch {
    alert('Kunde inte ta bort posten.');
  }
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal(memberId, type) {
  // Populate member select
  const sel = document.getElementById('entry-member');
  const placeholder = memberId === null
    ? `<option value="" disabled selected>— Välj person —</option>`
    : '';
  sel.innerHTML = placeholder + members.map(m => {
    const n     = nick(m);
    const label = n !== m.name ? `${n} (${m.name})` : m.name;
    return `<option value="${m.id}" ${m.id === memberId ? 'selected' : ''}>${escHtml(label)}</option>`;
  }).join('');

  // Set type
  document.querySelector(`input[name="entry-type"][value="${type}"]`).checked = true;

  // Reset fields
  document.getElementById('entry-date').value    = todayISO();
  document.getElementById('entry-hours').value   = '';
  document.getElementById('entry-comment').value = '';
  document.getElementById('modal-error').textContent = '';

  document.getElementById('modal').classList.remove('hidden');
  setTimeout(() => document.getElementById('entry-hours').focus(), 50);
}

function closeModal() {
  document.getElementById('modal').classList.add('hidden');
}

function modalBackdropClick(e) {
  if (e.target === document.getElementById('modal')) closeModal();
}

async function submitEntry() {
  const memberId = parseInt(document.getElementById('entry-member').value, 10);
  const type     = document.querySelector('input[name="entry-type"]:checked').value;
  const date     = document.getElementById('entry-date').value;
  const hoursStr = document.getElementById('entry-hours').value;
  const comment  = document.getElementById('entry-comment').value.trim();
  const errEl    = document.getElementById('modal-error');

  errEl.textContent = '';

  if (!memberId || isNaN(memberId)) { errEl.textContent = 'Välj en person.'; return; }
  if (!date) { errEl.textContent = 'Välj ett datum.'; return; }

  const totalMins = parseHHMM(hoursStr);
  if (totalMins === null || totalMins === 0) {
    errEl.textContent = 'Ange timmar i formatet TT:MM (ex. 8:30).';
    return;
  }

  const minutes = type === 'taken' ? -totalMins : totalMins;

  try {
    const res = await fetch('/api/entries', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ member_id: memberId, date, minutes, comment }),
    });
    if (!res.ok) throw new Error();
    closeModal();
    await loadData();
  } catch {
    errEl.textContent = 'Något gick fel — försök igen.';
  }
}

// ── Member management ─────────────────────────────────────────────────────────
async function archiveMember(id, displayName) {
  const answer = prompt(
    `Arkivera ${displayName}?\n\nDe flyttas till arkivet och kan inte längre få nya poster.\n\nSkriv "${displayName}" för att bekräfta:`
  );
  if (answer === null) return; // cancelled
  if (answer.trim() !== displayName) {
    alert(`Fel namn. Arkivering avbruten.`);
    return;
  }
  try {
    await fetch(`/api/members/${id}`, {
      method:  'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ is_archived: 1 }),
    });
    if (openHistoryId === id) closeHistory();
    await loadData();
  } catch {
    alert('Kunde inte arkivera personen.');
  }
}

function openNewMemberModal() {
  document.getElementById('new-member-name').value      = '';
  document.getElementById('new-member-nick').value      = '';
  document.getElementById('new-member-error').textContent = '';
  document.getElementById('new-member-modal').classList.remove('hidden');
  setTimeout(() => document.getElementById('new-member-name').focus(), 50);
}

function closeNewMemberModal() {
  document.getElementById('new-member-modal').classList.add('hidden');
}

async function submitNewMember() {
  const name     = document.getElementById('new-member-name').value.trim();
  const nickname = document.getElementById('new-member-nick').value.trim();
  const errEl    = document.getElementById('new-member-error');
  errEl.textContent = '';

  if (!name) { errEl.textContent = 'Ange ett namn.'; return; }

  try {
    const res = await fetch('/api/members', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ name, nickname }),
    });
    if (!res.ok) {
      const data = await res.json();
      errEl.textContent = data.error || 'Något gick fel.';
      return;
    }
    closeNewMemberModal();
    await loadData();
  } catch {
    errEl.textContent = 'Kunde inte lägga till personen.';
  }
}

// ── Archive ───────────────────────────────────────────────────────────────────
function toggleArchive() {
  archiveOpen = !archiveOpen;
  document.getElementById('archive-body').classList.toggle('hidden', !archiveOpen);
  document.getElementById('archive-chevron').textContent = archiveOpen ? '▼' : '▶';
}

function renderArchivedMembers() {
  const list = document.getElementById('archived-members-list');
  if (!archivedMembers.length) {
    list.innerHTML = '<p class="history-empty">Inga arkiverade medlemmar.</p>';
    return;
  }
  list.innerHTML = archivedMembers.map(m => {
    const neg      = m.balance_minutes < 0;
    const nickname = nick(m);
    const hasNick  = nickname !== m.name;
    return `
      <div class="member-row archive-row">
        <div class="member-left" onclick="toggleHistory(${m.id}, '${escHtml(m.name)}', true)">
          <div class="member-dot" style="background:#94a3b8"></div>
          <span class="member-name" style="color:#64748b" title="${hasNick ? escHtml(m.name) : ''}">${escHtml(nickname)}</span>
          ${hasNick ? `<span class="real-name">${escHtml(m.name)}</span>` : ''}
          <span class="member-name-hint">▸ visa historia</span>
        </div>
        <div class="member-balance ${neg ? 'neg' : ''}" style="font-size:1rem;color:${neg?'var(--red)':'#64748b'}">
          ${minsToHHMM(m.balance_minutes)}
        </div>
      </div>`;
  }).join('');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function escHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Event Listeners ───────────────────────────────────────────────────────────
document.getElementById('password-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') login();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});

// ── Boot ──────────────────────────────────────────────────────────────────────
checkAuth();
