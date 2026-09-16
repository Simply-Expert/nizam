// Shared helpers for the layout prototypes: live data from /api/board plus tiny formatting utils.
const $ = (s, el=document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ORDER = {needs: 0, working: 1, inbox: 2, done: 3};
const SECTION = {needs: 'Needs you', working: 'Working', inbox: 'Your turn', done: 'Done'};
const DOT = {needs: '●', working: '◐', inbox: '○', done: '✓'};
let DATA = null;

async function loadBoard() {
  const r = await fetch('/api/board'); DATA = await r.json();
  DATA.sessions = DATA.sessions.filter(s => s.bucket !== 'done');
  DATA.agents.sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    const ca = counts(byAgent(a.root)), cb = counts(byAgent(b.root));
    return (cb.needs*100+cb.working*10+cb.inbox) - (ca.needs*100+ca.working*10+ca.inbox) || a.display_name.localeCompare(b.display_name);
  });
  return DATA;
}
function byAgent(root) { return DATA.sessions.filter(s => s.agent === root); }
function counts(list) { const c = {needs:0, working:0, inbox:0}; for (const s of list) if (c[s.bucket] !== undefined) c[s.bucket]++; return c; }
function badges(c) {
  return `<span class="cnt">${c.needs?`<b class="n">${c.needs}</b>`:''}${c.working?`<b class="w">${c.working}</b>`:''}${c.inbox?`<b class="i">${c.inbox}</b>`:''}</span>`;
}
function firstLine(s) { return (s || '').split('\n').map(x => x.trim()).filter(Boolean)[0] || ''; }
function lastPara(s) { const p = (s || '').split(/\n\s*\n/).map(x => x.trim()).filter(Boolean); return p[p.length - 1] || ''; }
function sortSessions(list) { return [...list].sort((a, b) => ORDER[a.bucket] - ORDER[b.bucket] || b.last_activity - a.last_activity); }
async function act(id, action) { await fetch(`/api/sessions/${id}/${action}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); }
