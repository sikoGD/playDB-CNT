"""Web pages for the private NPLN server, served by npln_stub.py (default 127.0.0.1:8090):

- /        status: players online, who is searching, tickets, NAT checks, connection-quality
           reports, refreshed every 2 s (/api/stats as JSON). No addresses or user ids.
- /admin   administration behind a password: server settings (who may sign up, maintenance, room
           size, matchmaking, rate limit, refused games); accounts (approve, ban, release a code
           for a reclaim, note, delete) and friendships (make, remove, move to a new code);
           players online (kick, ban by account or IP), rooms (close), tickets (cancel), player
           reports, the latest telemetry and an audit log. Accounts, settings and bans live in
           npln_accounts.py's database.

Admin sign-in (AdminAuth): the password is set with `npln_stub.py --set-admin-password` and only
its salted scrypt hash is stored (admin-password.json, mode 0600). A good password starts a
session: a random cookie (HttpOnly, SameSite=Strict, Secure behind HTTPS) that lasts 8 hours.
Five wrong passwords from one address lock it out for 15 minutes. Every admin request also needs
the X-Reservoir-Admin header and, when the browser sends one, a same-site Origin, so other sites
can't act through a signed-in browser. On a public server the pages sit behind Caddy (HTTPS);
--trust-proxy then takes the client address and scheme from its X-Forwarded-* headers.

Everything players send (names, presence) is shown with textContent, never as HTML.
"""
import hashlib, hmac, ipaddress, json, os, re, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from npln_accounts import GAMES

ACTIONS = {
    'kick': re.compile(r'u-[0-9a-f]{16}'), 'ban_user': re.compile(r'u-[0-9a-f]{16}'),
    'unban_user': re.compile(r'u-[0-9a-f]{16}'),
    'ban_ip': re.compile(r'\d{1,3}(\.\d{1,3}){3}'), 'unban_ip': re.compile(r'\d{1,3}(\.\d{1,3}){3}'),
    'cancel_ticket': re.compile(r'mt-\d{4,8}'), 'maintenance': re.compile(r'on|off'),
    'add_test_player': re.compile(r'caller'), 'close_room': re.compile(r'gs-\d{4,8}'),
}
ACCOUNT_OPS = {'approve', 'ban', 'unban', 'release', 'note', 'delete', 'befriend', 'unfriend', 'transfer'}

SCRYPT = {'n': 2 ** 15, 'r': 8, 'p': 1}
SESSION_SECONDS = 8 * 3600
LOCK_AFTER, LOCK_SECONDS = 5, 15 * 60
COOKIE = 'raiders_admin'


def hash_password(password, salt=None, n=SCRYPT['n'], r=SCRYPT['r'], p=SCRYPT['p']):
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=2 ** 26, dklen=32)
    return {'algo': 'scrypt', 'n': n, 'r': r, 'p': p, 'salt': salt.hex(), 'hash': digest.hex()}


def write_password_file(path, password):
    """Store a new admin password's hash (never the password) readable only by its owner."""
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(hash_password(password), f)
    os.replace(tmp, path)


class AdminAuth:
    """Password check, sign-in throttling and sessions for the admin pages."""

    def __init__(self, password_file):
        self.password_file = Path(password_file)
        self.lock = threading.Lock()
        self.sessions = {}   # cookie value -> expiry
        self.failures = {}   # client address -> [failure times]

    def configured(self):
        return self.password_file.exists()

    def check_password(self, password):
        try:
            rec = json.loads(self.password_file.read_text())
            given = hash_password(password, bytes.fromhex(rec['salt']), rec['n'], rec['r'], rec['p'])
            return hmac.compare_digest(given['hash'], rec['hash'])
        except (OSError, ValueError, KeyError):
            return False

    def locked_for(self, client):
        """Seconds this address must still wait, 0 if it may try."""
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(client, []) if now - t < LOCK_SECONDS]
            self.failures[client] = recent
            return int(LOCK_SECONDS - (now - recent[0])) if len(recent) >= LOCK_AFTER else 0

    def failed(self, client):
        with self.lock:
            self.failures.setdefault(client, []).append(time.time())

    def start_session(self, client):
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.failures.pop(client, None)
            self.sessions[token] = time.time() + SESSION_SECONDS
        return token

    def valid(self, token):
        now = time.time()
        with self.lock:
            for t in [t for t, exp in self.sessions.items() if exp < now]:
                del self.sessions[t]
            return bool(token) and any(hmac.compare_digest(token, t) for t in self.sessions)

    def end_session(self, token):
        with self.lock:
            self.sessions.pop(token, None)


STYLE = r'''
:root { --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --muted:#667085; --line:#e4e7ec;
        --accent:#e2552d; --ok:#1a7f4b; --warn:#b54708; --chip:#eef2f6; --btn:#eef2f6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#12151b; --card:#1b2029; --ink:#e7eaf0; --muted:#98a2b3; --line:#2a313d;
          --accent:#ff7a50; --ok:#4ccf8a; --warn:#f5a524; --chip:#252c38; --btn:#252c38; } }
* { box-sizing: border-box; }
html { background:var(--bg); }
body { margin:0 auto; max-width:1200px; background:var(--bg); color:var(--ink);
       font:15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", Arial, sans-serif; }
header { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:20px 24px 8px; }
h1 { margin:0; font-size:22px; }
.sub { color:var(--muted); font-size:13px; }
.spacer { flex:1; }
main { padding:8px 24px 32px; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px;
         margin:8px 0 20px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
.tile .n { font-size:28px; font-weight:650; font-variant-numeric:tabular-nums; }
.tile .l { color:var(--muted); font-size:13px; }
section { background:var(--card); border:1px solid var(--line); border-radius:12px;
          margin-bottom:16px; overflow:hidden; }
section h2 { margin:0; padding:12px 14px; font-size:15px; border-bottom:1px solid var(--line); }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; padding:12px 14px; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:13.5px; }
th, td { text-align:left; padding:8px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; white-space:nowrap; }
tr:last-child td { border-bottom:0; }
.empty { padding:14px; color:var(--muted); }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px;
       background:var(--muted); }
.dot.on { background:var(--ok); } .dot.search { background:var(--accent); }
.chip { display:inline-block; background:var(--chip); border-radius:6px; padding:1px 6px;
        margin:1px 2px; font-size:12px; white-space:nowrap; }
.bad { color:var(--warn); } .good { color:var(--ok); }
button { font:inherit; font-size:13px; padding:5px 12px; border-radius:8px; cursor:pointer;
         border:1px solid var(--line); background:var(--btn); color:var(--ink); margin:2px 4px 2px 0; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button.danger { border-color:var(--warn); color:var(--warn); }
input { font:inherit; padding:7px 10px; border-radius:8px; border:1px solid var(--line);
        background:var(--card); color:var(--ink); width:min(320px, 100%); }
.login { max-width:420px; margin:48px auto; }
.login p { margin:0; padding:0 14px 14px; }
code { font-size:12px; }
a { color:var(--accent); }
[hidden] { display:none !important; }
@media (max-width:600px) { header, main { padding-left:16px; padding-right:16px; } }
'''

COMMON_JS = r'''
const ver = (v, b) => [v, b && 'build ' + b.slice(0, 8)].filter(Boolean).join(' · ') || '—';
const el = (tag, text, cls) => { const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text); if (cls) e.className = cls; return e; };
const ago = s => s < 60 ? Math.round(s) + ' s' : s < 3600 ? Math.round(s / 60) + ' min' : (s / 3600).toFixed(1) + ' h';
function table(id, cols, rows, empty) {
  const box = document.getElementById(id); box.replaceChildren();
  if (!rows.length) { box.append(el('div', empty, 'empty')); return; }
  const t = el('table'), h = el('tr');
  cols.forEach(c => h.append(el('th', c[0]))); t.append(h);
  rows.forEach(r => { const tr = el('tr');
    cols.forEach(c => { const td = el('td'); const v = c[1](r);
      if (v instanceof Node) td.append(v); else td.textContent = v ?? ''; tr.append(td); });
    t.append(tr); });
  box.append(t);
}
function chips(obj) { const s = el('span');
  Object.entries(obj || {}).forEach(([k, v]) => s.append(el('span', k + ' ' + v, 'chip'))); return s; }
'''

PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reservoir</title>
<style>''' + STYLE + r'''</style>
</head>
<body>
<header><h1>Reservoir</h1><span class="sub" id="status">connecting…</span>
  <span class="spacer"></span><a class="sub" href="admin">Admin</a></header>
<main>
  <section id="join" hidden><h2>How to join</h2>
    <p>In Droplet: Configure &gt; System &gt; Network. Turn online play on, enter the server
      <code id="address"></code>, and pick this server's certificate,
      <a href="ca.der" download>ca.der</a> (also as <a href="ca.pem" download>ca.pem</a>).</p></section>
  <div class="tiles" id="tiles"></div>
  <section><h2>Games</h2><div class="scroll" id="games"></div></section>
  <section><h2>Players</h2><div class="scroll" id="players"></div></section>
  <section><h2>Matchmaking</h2><div class="scroll" id="tickets"></div></section>
  <section><h2>Connection quality reports</h2><div class="scroll" id="reports"></div></section>
  <section><h2>Calls</h2><div class="scroll" id="calls"></div></section>
</main>
<script>''' + COMMON_JS + r'''
function state(p) { const s = el('span');
  s.append(el('span', '', 'dot ' + (p.searching ? 'search' : p.online ? 'on' : '')));
  s.append(document.createTextNode(p.searching ? 'searching' : p.online ? 'online' : 'offline'));
  return s; }
async function tick() {
  try {
    const d = await (await fetch('api/stats', {cache: 'no-store'})).json();
    document.getElementById('status').textContent =
      'up ' + ago(d.uptime) + ' · updated ' + new Date().toLocaleTimeString();
    if (d.game_port) { document.getElementById('join').hidden = !d.ca;
      document.getElementById('address').textContent = location.hostname + ':' + d.game_port; }
    const tiles = document.getElementById('tiles'); tiles.replaceChildren();
    [['online', 'players online'], ['searching', 'searching'], ['tickets', 'tickets'],
     ['sessions', 'game sessions'], ['reports', 'quality reports'], ['calls', 'calls']].forEach(([k, l]) => {
      const t = el('div', null, 'tile'); t.append(el('div', d.counts[k], 'n'), el('div', l, 'l')); tiles.append(t); });
    table('games', [['Game', g => g.name], ['Online', g => g.online], ['Searching', g => g.searching],
      ['Rooms', g => g.rooms], ['Matches', g => g.sessions], ['Open', g => g.enabled ? 'yes' : 'closed']],
      d.games || [], 'No games yet.');
    table('players', [['', state], ['Name', p => p.name || '—'], ['Game', p => p.game || '—'],
      ['Version', p => ver(p.version, p.build)], ['Level', p => p.level ?? '—'],
      ['Status', p => chips(p.presence_short)], ['Signed in', p => ago(p.since) + ' ago']],
      d.players, 'No players yet.');
    table('tickets', [['Player', t => t.player], ['Game', t => t.game || '—'],
      ['Version', t => ver(t.game_version, t.build)], ['Mode', t => t.config],
      ['State', t => t.state], ['Waiting', t => ago(t.age)]], d.tickets, 'No tickets.');
    table('reports', [['When', r => ago(r.age) + ' ago'], ['Type', r => r.type],
      ['Result', r => r.error ? el('span', r.error, 'bad') : r.body_size != null ?
        (r.body_size + ' B body, ' + r.fields + ' fields') : (r.size + ' B, not decoded')]],
      d.reports, 'None yet. Pia sends one when a session is created, joined or left.');
    table('calls', [['Method', c => c[0]], ['Count', c => c[1]]], d.calls, 'No calls yet.');
  } catch (e) {
    document.getElementById('status').textContent = 'server not reachable (' + e + ')';
  }
}
tick(); setInterval(tick, 2000);
</script>
</body>
</html>
'''

ADMIN_PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reservoir admin</title>
<style>''' + STYLE + r'''
select { font:inherit; padding:6px 8px; border-radius:8px; border:1px solid var(--line);
         background:var(--card); color:var(--ink); }
input[type=number] { width:110px; } input[type=checkbox] { width:auto; transform:scale(1.2); }
.setting { display:grid; grid-template-columns:minmax(200px,1fr) auto auto; gap:8px 12px;
           align-items:center; padding:10px 14px; border-bottom:1px solid var(--line); }
.setting:last-child { border-bottom:0; }
.setting .l { font-size:13.5px; } .setting .k { color:var(--muted); font-size:12px; }
.note { margin:0; padding:0 14px 12px; }
.code { white-space:nowrap; font-variant-numeric:tabular-nums; }
@media (max-width:600px) { .setting { grid-template-columns:1fr; } }
</style>
</head>
<body>
<header><h1>Server admin</h1><span class="sub" id="status"></span><span class="spacer"></span>
  <a class="sub" href="/">Status page</a>
  <button id="logout" hidden>Sign out</button></header>
<main>
  <section class="login" id="login"><h2>Sign in</h2>
    <form class="row" id="loginform" autocomplete="on">
      <input id="password" type="password" placeholder="Admin password" autocomplete="current-password" required>
      <button class="primary" type="submit">Sign in</button></form>
    <p class="sub" id="loginmsg"></p></section>
  <div id="panels" hidden>
    <section><h2>Server</h2><div class="row" id="server"></div></section>
    <section><h2>Games</h2><div class="scroll" id="games"></div>
      <p class="sub note">Each game keeps its own players, rooms and matchmaking queues; friends
        are shared. A closed game refuses new sign-ins (players already in it stay).</p></section>
    <section><h2>Settings</h2><div id="settings"></div></section>
    <section><h2>Accounts</h2>
      <div class="row"><input id="acctfilter" placeholder="Filter: name, friend code or IP address" autocomplete="off">
        <span class="sub" id="acctcount"></span></div>
      <div class="scroll" id="accounts"></div>
      <div class="row"><input id="codea" placeholder="Friend code A" autocomplete="off">
        <input id="codeb" placeholder="Friend code B" autocomplete="off">
        <button id="befriend">Make friends</button><button id="unfriend">Remove friendship</button>
        <button id="transfer" class="danger">Move A's friends to B</button></div>
      <p class="sub note">A player who lost their emulator data has a new friend code. Either
        <b>Release code</b> on their old account, so their emulator can take the old code back
        (Configure &gt; System &gt; Network, "Use another friend code"), or keep the new code and
        move the old code's friends to it.</p></section>
    <section><h2>Players online</h2><div class="scroll" id="players"></div></section>
    <section><h2>Rooms</h2><div class="scroll" id="rooms"></div></section>
    <section><h2>Matchmaking tickets</h2><div class="scroll" id="tickets"></div></section>
    <section><h2>Bans</h2><div class="scroll" id="bans"></div></section>
    <section><h2>Player reports</h2><div class="scroll" id="preports"></div></section>
    <section><h2>Latest connection-quality reports</h2><div class="scroll" id="reports"></div></section>
    <section><h2>Audit log</h2><div class="scroll" id="audit"></div></section>
  </div>
</main>
<script>''' + COMMON_JS + r'''
const H = {'X-Reservoir-Admin': '1'};
const fmt = c => c ? 'SW-' + c.slice(0, 4) + '-' + c.slice(4, 8) + '-' + c.slice(8) : '—';
const show = signedIn => { document.getElementById('login').hidden = signedIn;
  document.getElementById('panels').hidden = !signedIn; document.getElementById('logout').hidden = !signedIn; };
document.getElementById('loginform').onsubmit = async ev => {
  ev.preventDefault();
  const msg = document.getElementById('loginmsg'); msg.textContent = 'checking…';
  const pw = document.getElementById('password');
  const r = await fetch('/api/admin/login', {method: 'POST', credentials: 'same-origin',
    headers: Object.assign({'Content-Type': 'application/json'}, H), body: JSON.stringify({password: pw.value})});
  pw.value = '';
  const d = await r.json().catch(() => ({}));
  if (r.ok) { msg.textContent = ''; settingsShown = false; tick(); }
  else msg.textContent = d.error || ('sign-in failed (' + r.status + ')');
};
document.getElementById('logout').onclick = async () => {
  await fetch('/api/admin/logout', {method: 'POST', credentials: 'same-origin', headers: H}); show(false);
  document.getElementById('status').textContent = 'signed out'; };
async function post(url, body, question) {
  if (question && !confirm(question)) return false;
  const r = await fetch(url, {method: 'POST', credentials: 'same-origin',
    headers: Object.assign({'Content-Type': 'application/json'}, H), body: JSON.stringify(body)});
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.result !== 'ok') { alert('Failed: ' + (d.error || d.result || r.status)); return false; }
  return true;
}
async function act(action, target, question) {
  if (await post('/api/admin/action', {action, target}, question) && action === 'maintenance') settingsShown = false;
  tick(); }
async function game(tenant, change, question) {
  if (await post('/api/admin/game', Object.assign({tenant}, change), question)) settingsShown = false;
  tick(); }
async function acct(op, code, extra, question) {
  await post('/api/admin/account', Object.assign({op, code}, extra || {}), question); tick(); }
function button(text, cls, onclick) { const b = el('button', text, cls); b.onclick = onclick; return b; }
function strings(x, out = []) {   // printable strings inside a decoded protobuf (player reports)
  if (typeof x === 'string') out.push(x); else if (Array.isArray(x)) x.forEach(y => strings(y, out));
  else if (x && typeof x === 'object') Object.values(x).forEach(y => strings(y, out)); return out; }
let settingsShown = false, accounts = [], ipBans = [];
function renderSettings(list, games) {
  const box = document.getElementById('settings'); box.replaceChildren();
  list.forEach(s => {
    const row = el('div', null, 'setting'), left = el('div');
    const known = s.key === 'blocked_games' ? ' · ' + Object.entries(games).map(([t, n]) => n + ': ' + t).join(', ') : '';
    left.append(el('div', s.label, 'l'), el('div', s.key + known, 'k'));
    let input;
    if (s.kind === 'bool') { input = el('input'); input.type = 'checkbox'; input.checked = !!s.value; }
    else if (s.kind === 'choice') { input = el('select');
      s.choices.forEach(c => { const o = el('option', c); o.value = c; input.append(o); }); input.value = s.value; }
    else if (s.kind === 'number') { input = el('input'); input.type = 'number';
      input.min = s.range[0]; input.max = s.range[1]; input.value = s.value; }
    else { input = el('input'); input.value = s.value; input.placeholder = 'none'; }
    row.append(left, input, button('Save', '', async () => {
      if (await post('/api/admin/settings', {key: s.key, value: s.kind === 'bool' ? input.checked : input.value})) {
        settingsShown = false; tick(); } }));
    box.append(row);
  });
}
function renderAccounts() {
  const q = document.getElementById('acctfilter').value.trim().toLowerCase().replace(/[\s-]/g, '').replace(/^sw/, '');
  const rows = accounts.filter(a => !q || a.code.includes(q) || (a.name || '').toLowerCase().includes(q) ||
    a.ips.some(e => e.ip.includes(q)));
  document.getElementById('acctcount').textContent = rows.length + ' of ' + accounts.length + ' accounts';
  table('accounts', [
    ['', a => el('span', '', 'dot ' + (a.online ? 'on' : ''))],
    ['Name', a => a.name || '—'], ['Friend code', a => el('span', fmt(a.code), 'code')],
    ['Last seen', a => a.online ? 'online' : a.seen ? ago(Date.now() / 1000 - a.seen) + ' ago' : 'never'],
    ['Friends', a => { const s = el('span'); s.append(el('b', a.friends.length + ' '));
      a.friends.forEach(f => s.append(el('span', fmt(f), 'chip'))); return s; }],
    ['Requests', a => { const s = el('span');
      a.requests.forEach(f => s.append(el('span', 'from ' + fmt(f), 'chip')));
      a.pending.forEach(f => s.append(el('span', 'to ' + fmt(f), 'chip'))); return s; }],
    ['Signed in from', a => { const s = el('span');
      a.ips.slice(0, 5).forEach(e => s.append(el('span', e.ip + ' · ' + e.logins + '× · ' +
        ago(Date.now() / 1000 - e.last) + ' ago', 'chip'), button('Ban IP', '', () => act('ban_ip', e.ip,
          'Ban every login from ' + e.ip + '?' + (e.ip.startsWith('127.') ? ' This is the server itself: every local player is banned.' : '')))));
      if (a.ips.length > 5) s.append(el('span', '+' + (a.ips.length - 5) + ' more', 'chip'));
      a.same_ip.forEach(c => s.append(el('span', 'same address: ' + fmt(c), 'chip')));
      a.same_emulator.forEach(c => s.append(el('span', 'same emulator: ' + fmt(c), 'chip')));
      return s; }],
    ['Status', a => { const s = el('span');
      if (!a.approved) s.append(el('span', 'waiting for approval', 'chip bad'));
      if (a.banned) s.append(el('span', 'banned: ' + a.banned, 'chip bad'));
      if (a.claimable) s.append(el('span', 'released: the next sign-in claims it', 'chip'));
      if (a.note) s.append(el('span', a.note, 'chip'));
      return s; }],
    ['', a => { const s = el('span'), who = a.name || fmt(a.code);
      if (!a.approved) s.append(button('Approve', 'primary', () => acct('approve', a.code)));
      s.append(a.banned ? button('Unban', '', () => acct('unban', a.code))
        : button('Ban', 'danger', () => { const why = prompt('Ban ' + who + '? Reason (for the audit log):', '');
            if (why !== null) acct('ban', a.code, {text: why || 'banned'}); }));
      if (!a.claimable) s.append(button('Release code', '', () => acct('release', a.code, null,
        'Release ' + fmt(a.code) + '? The next emulator that signs in with this code takes it over, with ' +
        'its friends. Only for an owner who lost their emulator data.')));
      s.append(button('Note', '', () => { const t = prompt('Note for ' + who + ':', a.note || '');
        if (t !== null) acct('note', a.code, {text: t}); }));
      s.append(button('Delete', 'danger', () => acct('delete', a.code, null, 'Delete ' + who + ' (' +
        fmt(a.code) + ') and all their friendships? Their emulator can sign in again as a new player.')));
      return s; }]], rows, accounts.length ? 'No account matches.' : 'No accounts yet.');
}
const pair = () => [document.getElementById('codea').value, document.getElementById('codeb').value];
document.getElementById('befriend').onclick = () => { const [a, b] = pair(); acct('befriend', a, {other: b}); };
document.getElementById('unfriend').onclick = () => { const [a, b] = pair(); acct('unfriend', a, {other: b}); };
document.getElementById('transfer').onclick = () => { const [a, b] = pair(); acct('transfer', a, {other: b},
  'Move every friendship of ' + a + ' to ' + b + '? ' + a + ' keeps its account, without friends.'); };
document.getElementById('acctfilter').oninput = renderAccounts;
async function tick() {
  const status = document.getElementById('status');
  let r, d;
  try { r = await fetch('/api/admin/state', {credentials: 'same-origin', headers: H, cache: 'no-store'}); }
  catch (e) { status.textContent = 'server not reachable (' + e + ')'; return; }
  if (r.status === 401 || r.status === 403) {
    const info = await r.json().catch(() => ({}));
    show(false); status.textContent = '';
    document.getElementById('loginmsg').textContent = info.setup || '';
    return;
  }
  d = await r.json(); show(true);
  status.textContent = 'signed in · up ' + ago(d.uptime) + ' · updated ' + new Date().toLocaleTimeString();
  const ra = await fetch('/api/admin/accounts', {credentials: 'same-origin', headers: H, cache: 'no-store'});
  const da = ra.ok ? await ra.json() : null;
  if (da) { accounts = da.accounts; ipBans = da.ip_bans; renderAccounts();
    if (!settingsShown) { renderSettings(da.settings, da.games); settingsShown = true; } }
  const server = document.getElementById('server'); server.replaceChildren();
  server.append(el('span', d.maintenance ? 'Closed for maintenance: new logins are refused.' : 'Open: new logins allowed.',
                   d.maintenance ? 'bad' : 'good'));
  server.append(d.maintenance ? button('Open again', '', () => act('maintenance', 'off'))
                              : button('Close for maintenance', 'danger', () => act('maintenance', 'on',
                                  'Refuse new logins? Players already online stay.')));
  server.append(button('Add test player', '', () => act('add_test_player', 'caller')));
  const waiting = accounts.filter(a => !a.approved).length;
  if (waiting) server.append(el('span', waiting + ' friend code' + (waiting > 1 ? 's' : '') + ' waiting for approval (Accounts)', 'bad'));
  table('games', [['Game', g => { const s = el('span'); s.append(el('b', g.name + ' '), el('code', g.tenant)); return s; }],
    ['Online', g => g.online], ['Searching', g => g.searching], ['Rooms', g => g.rooms], ['Matches', g => g.sessions],
    ['Room size', g => { const s = el('span'), input = el('input'); input.type = 'number'; input.min = 2; input.max = 32;
      input.value = g.room_size; input.style.width = '80px';
      s.append(input, button('Save', '', () => game(g.tenant, {room_size: input.value}))); return s; }],
    ['', g => g.enabled ? button('Close', 'danger', () => game(g.tenant, {enabled: false},
        'Close ' + g.name + ' on this server? New sign-ins for it are refused; players already in stay.'))
      : button('Open', 'primary', () => game(g.tenant, {enabled: true}))]], d.games || [], 'No games yet.');
  table('players', [['Name', p => p.name || '—'], ['Game', p => p.game || '—'], ['Version', p => ver(p.version, p.build)],
    ['Friend code', p => el('span', p.friend_code || '—', 'code')], ['User', p => p.uid],
    ['IP', p => p.ip], ['State', p => p.kicked ? 'kicked' : p.banned ? 'banned' : p.searching ? 'searching' : p.online ? 'online' : 'offline'],
    ['Presence', p => chips(p.presence)], ['Since', p => ago(p.since)],
    ['', p => { const s = el('span');
      s.append(button('Kick', '', () => act('kick', p.uid, 'Kick ' + (p.name || p.uid) + '? Their game disconnects; they can sign in again.')));
      s.append(button('Ban', 'danger', () => act('ban_user', p.uid, 'Ban ' + (p.name || p.uid) +
        (p.friend_code ? ' (friend code ' + p.friend_code + ')?' : ' (this login only)?'))));
      s.append(button('Ban IP', 'danger', () => act('ban_ip', p.ip, 'Ban every login from ' + p.ip + '?' +
        (p.ip.startsWith('127.') ? ' This is the server itself: every local player is banned.' : ''))));
      return s; }]], d.players, 'No players yet.');
  table('rooms', [['Room', r => r.id], ['Game', r => r.game || '—'], ['Version', r => ver(r.version, r.build)],
    ['Kind', r => r.room ? 'room' : 'matchmade (' + r.config + ')'], ['Host', r => r.host || '—'],
    ['Players', r => { const s = el('span'); r.members.forEach(m => s.append(el('span', m, 'chip'))); return s; }],
    ['Passcode', r => r.passcode ? 'yes' : 'no'], ['Open for', r => ago(r.age)],
    ['', r => r.closed ? el('span', 'closed', 'bad') : button('Close', '', () => act('close_room', r.id,
      'Close room ' + r.id + '? Nobody new can join; the players in it stay.'))]], d.rooms || [], 'No rooms.');
  table('tickets', [['Ticket', t => t.id], ['Player', t => t.player], ['Game', t => t.game || '—'],
    ['Version', t => ver(t.game_version, t.build)], ['Mode', t => t.config],
    ['State', t => t.state], ['Waiting', t => ago(t.age)], ['Attributes', t => chips(t.attributes)],
    ['', t => t.state === 'searching' ? button('Cancel', '', () => act('cancel_ticket', t.id)) : '']],
    d.tickets, 'No tickets.');
  const bans = [...accounts.filter(a => a.banned).map(a => ({kind: 'account', v: (a.name ? a.name + ' · ' : '') + fmt(a.code),
                   why: a.banned, undo: () => acct('unban', a.code)})),
                ...d.banned_uids.map(v => ({kind: 'login', v, undo: () => act('unban_user', v)})),
                ...d.kicked.filter(v => !d.banned_uids.includes(v)).map(v => ({kind: 'kicked', v, undo: () => act('unban_user', v)})),
                ...ipBans.map(b => ({kind: 'IP', v: b.ip + accounts.filter(a => a.ips.some(e => e.ip === b.ip))
                     .map(a => ' · ' + fmt(a.code)).join(''), why: b.reason, undo: () => act('unban_ip', b.ip)}))];
  table('bans', [['Kind', b => b.kind], ['Who', b => b.v], ['Reason', b => b.why || ''],
    ['', b => button(b.kind === 'kicked' ? 'Clear' : 'Unban', '', b.undo)]], bans, 'Nobody is banned.');
  table('preports', [['When', r => ago(r.age) + ' ago'], ['From', r => r.uid], ['Kind', r => r.method],
    ['Contents', r => strings(r.request).join(' · ')]], d.player_reports,
    'No reports. They appear here when a player reports someone in the game.');
  table('reports', [['When', r => ago((Date.now() / 1000) - r.t) + ' ago'], ['From', r => r.src],
    ['Result', r => r.error ? el('span', r.error, 'bad') : r.body_size != null ?
      (r.body_size + ' B, ' + (r.fields || []).length + ' fields') : (r.size + ' B, not decoded')],
    ['Fields', r => el('code', (r.fields || []).slice(0, 24).join(' '))], ['Text', r => (r.text || []).join(' · ')]],
    d.report_details, 'None yet.');
  table('audit', [['When', a => new Date(a.t * 1000).toLocaleString()], ['Action', a => a.action],
    ['Target', a => a.target], ['Result', a => a.result]], d.audit, 'No actions yet.');
}
tick(); setInterval(() => { if (!document.getElementById('panels').hidden) tick(); }, 2000);
</script>
</body>
</html>
'''

def serve(state, host='127.0.0.1', port=8090, auth=None, log=None, trust_proxy=False, accounts=None,
          pki=None):
    """Start the pages on host:port in a thread. auth is an AdminAuth (no admin without one);
    trust_proxy, the networks of a reverse proxy in front ("127.0.0.0/8,172.30.0.0/24"; True is
    127.0.0.0/8) whose X-Forwarded-* headers count; accounts is npln_accounts.Accounts; pki is the
    folder whose ca.der and ca.pem (the server's certificate authority, public) players download."""
    ca_files = {f'/{name}': Path(pki) / name for name in ('ca.der', 'ca.pem')} if pki else {}
    if trust_proxy is True:
        trust_proxy = '127.0.0.0/8'
    proxies = [ipaddress.ip_network(n.strip(), strict=False)
               for n in (trust_proxy or '').split(',') if n.strip()]

    class Page(BaseHTTPRequestHandler):
        def proxied(self):
            try:
                address = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            return any(address in net for net in proxies)

        def client(self):
            if self.proxied():
                forwarded = (self.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
                if forwarded:
                    return forwarded
            return self.client_address[0]

        def https(self):
            return self.proxied() and self.headers.get('X-Forwarded-Proto') == 'https'

        def send(self, code, body, kind, cookie=None):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy',
                             "default-src 'self'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            if cookie is not None:
                self.send_header('Set-Cookie', cookie)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def json(self, code, obj, cookie=None):
            self.send(code, json.dumps(obj).encode(), 'application/json', cookie)

        def cookie(self, value, max_age):
            secure = '; Secure' if self.https() else ''
            return f'{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}'

        def session_token(self):
            for part in (self.headers.get('Cookie') or '').split(';'):
                name, _, value = part.strip().partition('=')
                if name == COOKIE:
                    return value
            return ''

        def same_site(self):
            """Admin requests come from our own page: the custom header can't be sent cross-site
            without a CORS preflight (which this server never grants), and a browser's Origin,
            when present, must be this host."""
            if self.headers.get('X-Reservoir-Admin') != '1':
                return False
            origin = self.headers.get('Origin')
            if origin:
                host = self.headers.get('X-Forwarded-Host') if self.proxied() else None
                host = host or self.headers.get('Host') or ''
                return origin.split('://', 1)[-1] == host
            return True

        def admin_ok(self):
            if auth is None or not auth.configured():
                self.json(403, {'error': 'admin disabled',
                                'setup': 'No admin password is set. On the server, run '
                                         'npln_stub.py --set-admin-password, then sign in.'})
                return False
            if not self.same_site():
                self.json(403, {'error': 'cross-site request refused'})
                return False
            if not auth.valid(self.session_token()):
                self.json(401, {'error': 'sign in first'})
                return False
            return True

        def do_GET(self):
            path = self.path.split('?')[0].rstrip('/') or '/'
            if path in ('/', '/index.html'):
                self.send(200, PAGE.encode(), 'text/html; charset=utf-8')
            elif path == '/admin':
                self.send(200, ADMIN_PAGE.encode(), 'text/html; charset=utf-8')
            elif path == '/api/stats':
                self.json(200, dict(state.snapshot(), ca=any(f.is_file() for f in ca_files.values())))
            elif path in ca_files and ca_files[path].is_file():
                kind = 'application/x-x509-ca-cert' if path.endswith('.der') else 'application/x-pem-file'
                self.send(200, ca_files[path].read_bytes(), kind)
            elif path == '/api/admin/state':
                if self.admin_ok():
                    self.json(200, state.snapshot(private=True))
            elif path == '/api/admin/accounts':
                if self.admin_ok():
                    self.json(200, {'accounts': accounts.rows(state.online_codes()),
                                    'ip_bans': accounts.ip_bans(), 'settings': accounts.settings_view(),
                                    'games': GAMES})
            else:
                self.send_error(404)

        def read_json(self):
            size = int(self.headers.get('Content-Length') or 0)
            if size > 4096:
                return None
            try:
                req = json.loads(self.rfile.read(size) or b'{}')
                return req if isinstance(req, dict) else None
            except ValueError:
                return None

        def login(self):
            if auth is None or not auth.configured():
                self.json(403, {'error': 'No admin password is set. On the server, run '
                                         'npln_stub.py --set-admin-password.'})
                return
            if not self.same_site():
                self.json(403, {'error': 'cross-site request refused'})
                return
            client = self.client()
            wait = auth.locked_for(client)
            if wait:
                self.json(429, {'error': f'too many wrong passwords; try again in {wait // 60 + 1} min'})
                return
            req = self.read_json() or {}
            if auth.check_password(str(req.get('password', ''))):
                token = auth.start_session(client)
                state.audit('sign-in', client, 'ok')
                self.json(200, {'result': 'ok'}, self.cookie(token, 8 * 3600))
            else:
                auth.failed(client)
                state.audit('sign-in', client, 'wrong password')
                time.sleep(1)  # slows guessing further
                self.json(401, {'error': 'wrong password'})

        def do_POST(self):
            path = self.path.split('?')[0]
            if path == '/api/admin/login':
                self.login()
                return
            if path == '/api/admin/logout':
                if auth is not None and self.same_site():
                    auth.end_session(self.session_token())
                self.json(200, {'result': 'ok'}, self.cookie('', 0))
                return
            if path not in ('/api/admin/action', '/api/admin/account', '/api/admin/settings', '/api/admin/game'):
                self.send_error(404)
                return
            if not self.admin_ok():
                return
            req = self.read_json()
            if req is None:
                self.json(400, {'error': 'bad request'})
                return
            if path == '/api/admin/account':
                op = str(req.get('op', ''))
                if op not in ACCOUNT_OPS:
                    self.json(400, {'error': 'unknown action'})
                    return
                code, other = str(req.get('code', '')), req.get('other')
                result = accounts.admin(op, code, None if other is None else str(other),
                                        None if req.get('text') is None else str(req['text']))
                target = code + (f' {other}' if other else '')
                state.audit(f'account {op}', target, result)
                if log:
                    log(ev='admin', action=f'account {op}', target=target, result=result, client=self.client())
                self.json(200, {'result': result})
                return
            if path == '/api/admin/game':
                tenant = str(req.get('tenant', ''))
                enabled = req.get('enabled')
                why = accounts.set_game(tenant, None if enabled is None else bool(enabled), req.get('room_size'))
                result = why or 'ok'
                state.audit('game', f'{tenant} {json.dumps({k: req[k] for k in ("enabled", "room_size") if k in req})}'[:120], result)
                if log:
                    log(ev='admin', action='game', target=tenant, result=result, client=self.client())
                self.json(200, {'result': result})
                return
            if path == '/api/admin/settings':
                key = str(req.get('key', ''))
                why = accounts.set(key, req.get('value'))
                result = why or 'ok'
                state.audit('setting', f'{key}={req.get("value")!r}'[:120], result)
                if log:
                    log(ev='admin', action='setting', target=key, result=result, client=self.client())
                self.json(200, {'result': result})
                return
            action, target = str(req.get('action', '')), str(req.get('target', ''))
            pattern = ACTIONS.get(action)
            if pattern is None or not pattern.fullmatch(target):
                self.json(400, {'error': 'unknown action or bad target'})
                return
            result = state.act(action, target)
            state.audit(action, target, result)
            if log:
                log(ev='admin', action=action, target=target, result=result, client=self.client())
            self.json(200, {'result': result})

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer((host, port), Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if log:
        log(ev='dashboard', url=f'http://{host}:{port}/', admin=f'http://{host}:{port}/admin')
    return httpd
