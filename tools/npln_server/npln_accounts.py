"""Accounts, friendships, bans and server settings for the private NPLN server, kept in SQLite
(<data>/accounts.db, mode 0600). npln_stub.py signs players in through it and the admin panel
(npln_dashboard.py) manages it.

- An account is a friend code, written SW-1234-5678-9012 like a Switch's. The first emulator
  that signs in with a code ties it to its secret (only a SHA-256 of it is stored); another
  secret is refused. Players choose their code in the emulator: one the server doesn't know yet
  becomes theirs. The emulator keeps its secret when its player changes codes, so the server
  knows all of one emulator's codes: friendships follow it to the code it signs in with, and a
  ban on any of them refuses them all. The admin can release a code so that the next sign-in
  with it claims it again: a player who lost their emulator data enters their old code and
  keeps their friends.
- Each code's IP addresses (first and last sign-in, how many) are kept for the admin panel, for
  ip_history_days.
- Friendships: each player adds codes (the "friends" list of the emulator's sign-in token). Two
  players are friends once each has added the other; an add that isn't returned is a friend
  request. Sign-ins only ever add: a fresh install with an empty list removes nothing. Removing
  takes an explicit "removed" list from the emulator, or the admin.
- Settings: who may sign in with a new code (open, approval, closed), maintenance, room size,
  matchmaking knobs, login rate limit and the games the server refuses. The admin panel edits
  them; they survive restarts, as do bans.
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

CODE = re.compile(r'\d{12}')
TENANT = re.compile(r't-[0-9a-f]{8}-[a-z0-9]{2,8}')
IPV4 = re.compile(r'\d{1,3}(\.\d{1,3}){3}')

GAMES = {'t-54a666f5-lp1': 'Splatoon Raiders', 't-e96531c6-lp1': 'Mario Kart World'}
# Room sizes the games themselves allow: Raiders' Online Play desk takes 4, Mario Kart World's
# rooms (Create Room, joining friends, room codes) 24, 2 per console. room_sizes overrides them.
ROOM_SIZES = {'t-54a666f5-lp1': 4, 't-e96531c6-lp1': 24}

# key: (default, allowed values: a tuple of choices, (min, max) for numbers, bool or str; label)
SETTINGS = {
    'registration': ('open', ('open', 'approval', 'closed'),
                     'New friend codes: open (anyone), approval (the admin approves each) or '
                     'closed (only codes the server knows)'),
    'maintenance': (False, bool, 'Closed for maintenance: refuse new logins (players online stay)'),
    'room_max_players': (8, (2, 32), 'Most players in a room, for games without a size of their own '
                                     '(Games sets each game\'s)'),
    'room_sizes': ({}, dict, 'Room size per game (tenant id: players)'),
    'rescue_helpers': (1, (1, 3), 'Raiders: helpers matched to each player calling for help'),
    'loose_match': (False, bool, 'Raiders: match any caller with any helper (skip the stage check)'),
    'solo_match_after': (15, (0, 3600), 'Other games: give a lone matchmaking ticket a lobby of '
                                        'its own after this many seconds, which later players join '
                                        '(backfill) while its host allows it (0: never)'),
    'knockout_solo_after': (90, (0, 3600), 'Mario Kart World Knockout Tour: its lobbies never take '
                                           'later players, so a lone ticket waits this many seconds '
                                           'for others before it gets a lobby of its own'),
    'login_limit': (20, (1, 1000), 'Logins a minute from one address'),
    'max_friends': (200, (1, 5000), 'Most friend codes one player can add'),
    'blocked_games': ('', str, 'Games (tenant ids, comma-separated) this server refuses'),
    'ip_history_days': (90, (1, 3650), 'Days the server remembers which IP addresses each friend '
                                       'code signed in from (shown under Accounts)'),
}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS accounts (
    code TEXT PRIMARY KEY,
    secret TEXT,               -- SHA-256 of the emulator's secret; NULL: the next sign-in claims it
    name TEXT,                 -- the player's name in the game, as last seen
    created REAL NOT NULL,
    seen REAL,
    approved INTEGER NOT NULL DEFAULT 1,
    banned TEXT,               -- the reason, or NULL
    note TEXT,
    released REAL              -- when the admin released the code for a reclaim
);
CREATE TABLE IF NOT EXISTS friend_adds (
    code TEXT NOT NULL,        -- who added
    friend TEXT NOT NULL,      -- whom
    created REAL NOT NULL,
    PRIMARY KEY (code, friend)
);
CREATE INDEX IF NOT EXISTS friend_adds_friend ON friend_adds (friend);
CREATE INDEX IF NOT EXISTS accounts_secret ON accounts (secret);
CREATE TABLE IF NOT EXISTS code_ips (
    code TEXT NOT NULL,
    ip TEXT NOT NULL,
    first REAL NOT NULL,
    last REAL NOT NULL,
    logins INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (code, ip)
);
CREATE INDEX IF NOT EXISTS code_ips_ip ON code_ips (ip);
CREATE TABLE IF NOT EXISTS ip_bans (ip TEXT PRIMARY KEY, reason TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def normalize_code(text):
    """'SW-1234-5678-9012' (SW, dashes and spaces optional) -> '123456789012', or None."""
    code = re.sub(r'^SW', '', re.sub(r'[\s-]', '', str(text or '')).upper())
    return code if CODE.fullmatch(code) and int(code) else None


def format_code(code):
    """'123456789012' -> 'SW-1234-5678-9012', as a Switch shows friend codes."""
    return f'SW-{code[:4]}-{code[4:8]}-{code[8:]}' if code else None


def check_setting(key, value):
    """(the value as stored, None) or (None, why it's refused)."""
    if key not in SETTINGS:
        return None, f'unknown setting {key}'
    default, allowed, _ = SETTINGS[key]
    if allowed is bool:
        if isinstance(value, bool):
            return value, None
        if str(value).lower() in ('true', 'on', '1', 'false', 'off', '0'):
            return str(value).lower() in ('true', 'on', '1'), None
        return None, f'{key} is on or off'
    if allowed is dict:
        if not isinstance(value, dict):
            return None, f'{key} maps tenant ids to sizes'
        sizes = {}
        for tenant, size in value.items():
            if not TENANT.fullmatch(str(tenant)):
                return None, f'{key}: tenant ids look like t-54a666f5-lp1'
            try:
                size = int(size)
            except (TypeError, ValueError):
                return None, f'{key}: sizes are numbers'
            if not 2 <= size <= 32:
                return None, f'{key}: sizes are from 2 to 32'
            sizes[str(tenant)] = size
        return sizes, None
    if allowed is str:
        games = [g.strip() for g in str(value).split(',') if g.strip()]
        if any(not TENANT.fullmatch(g) for g in games):
            return None, f'{key}: tenant ids look like t-54a666f5-lp1'
        return ','.join(games), None
    if isinstance(allowed, tuple) and allowed and isinstance(allowed[0], str):
        return (str(value), None) if str(value) in allowed else (None, f'{key} is one of {", ".join(allowed)}')
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None, f'{key} is a number'
    lo, hi = allowed
    if not lo <= number <= hi:
        return None, f'{key} is from {lo} to {hi}'
    return number, None


class Accounts:
    def __init__(self, path, legacy_friends=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        new = not self.path.exists()
        if new:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        self.version = 0  # bumped by every change to accounts or friendships
        if legacy_friends and not self._one('SELECT COUNT(*) FROM accounts'):
            self._import_friends_json(Path(legacy_friends))
        self.banned_codes = {c for (c,) in self.db.execute(
            'SELECT code FROM accounts WHERE banned IS NOT NULL')}
        self.banned_ips = {ip for (ip,) in self.db.execute('SELECT ip FROM ip_bans')}
        self.settings = {k: v[0] for k, v in SETTINGS.items()}
        for key, value in self.db.execute('SELECT key, value FROM settings'):
            if key in SETTINGS:
                self.settings[key] = json.loads(value)
        self.pruned = 0  # when code_ips last dropped addresses older than ip_history_days

    def _one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return row[0] if row else None

    def _import_friends_json(self, path):
        """The first server kept players in friends.json: code -> {secret, friends, name, seen}."""
        try:
            players = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        with self.lock:
            self.db.execute('BEGIN')
            for code, p in players.items():
                if not CODE.fullmatch(code):
                    continue
                seen = p.get('seen') or time.time()
                self.db.execute('INSERT OR IGNORE INTO accounts (code, secret, name, created, seen) '
                                'VALUES (?, ?, ?, ?, ?)', (code, p.get('secret'), p.get('name'), seen, seen))
                for friend in p.get('friends', []):
                    if CODE.fullmatch(friend) and friend != code:
                        self.db.execute('INSERT OR IGNORE INTO friend_adds VALUES (?, ?, ?)',
                                        (code, friend, seen))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('imported_friends_json', ?)",
                            (json.dumps(time.time()),))
            self.db.execute('COMMIT')

    # --- signing in ---------------------------------------------------------------------------

    def login(self, code, secret, added=(), removed=(), tenant=None, ip=None):
        """None if this emulator may sign in as `code`, else why not (shown to the player as an
        error code; the reason goes to the server log). `ip` goes in the code's IP history."""
        blocked = self.settings['blocked_games'].split(',') if self.settings['blocked_games'] else []
        if tenant and tenant in blocked:
            return f'this server does not host {GAMES.get(tenant, tenant)}'
        mine = digest(secret)
        now = time.time()
        with self.lock:
            row = self.db.execute('SELECT secret, approved, banned, seen FROM accounts WHERE code = ?',
                                  (code,)).fetchone()
            if row is not None and row[0] is not None and not hmac.compare_digest(row[0], mine):
                return f'friend code {format_code(code)} belongs to another emulator'
            # This emulator's other codes: its player changed codes
            others = self.db.execute('SELECT code, seen, banned FROM accounts WHERE secret = ? AND code != ?',
                                     (mine, code)).fetchall()
            banned = next((c for c, _, why in others if why is not None), None)
            if (row is not None and row[2] is not None) or banned:
                if ip:
                    self._record_ip(code if row is not None else banned, ip, now)
                return 'banned'
            if row is None:
                registration = self.settings['registration']
                if registration == 'closed':
                    return 'this server is not taking new players'
                # seen stays empty until a sign-in goes through (one waiting for approval doesn't)
                self.db.execute('INSERT INTO accounts (code, secret, created, approved) VALUES (?, ?, ?, ?)',
                                (code, mine, now, 0 if registration == 'approval' else 1))
                self.version += 1
                row = (mine, 0 if registration == 'approval' else 1, None, None)
            elif row[0] is None:
                # Released by the admin: this sign-in claims the code again
                self.db.execute('UPDATE accounts SET secret = ?, released = NULL WHERE code = ?',
                                (mine, code))
                self.version += 1
            if ip:
                self._record_ip(code, ip, now)
            if not row[1]:
                return 'waiting for the server admin to approve this friend code'
            # Friendships follow the emulator: from its codes used since this one last signed in
            moved = [c for c, seen, _ in others if seen is not None and (row[3] is None or seen > row[3])]
            changed = False
            if moved:
                self.db.execute('BEGIN')
                for old in moved:
                    changed |= self._move_friends(old, code)
                self.db.execute('COMMIT')
            self.db.execute('UPDATE accounts SET seen = ? WHERE code = ?', (now, code))
            changed |= self._add_friends(code, added, now)
            for friend in removed:
                if CODE.fullmatch(friend) and friend not in added:
                    changed |= self.db.execute('DELETE FROM friend_adds WHERE code = ? AND friend = ?',
                                               (code, friend)).rowcount > 0
            if changed:
                self.version += 1
        return None

    def _record_ip(self, code, ip, now):
        self.db.execute('INSERT INTO code_ips VALUES (?, ?, ?, ?, 1) ON CONFLICT (code, ip) DO UPDATE '
                        'SET last = excluded.last, logins = logins + 1', (code, str(ip)[:64], now, now))
        if now - self.pruned > 3600:
            self.pruned = now
            self.db.execute('DELETE FROM code_ips WHERE last < ?',
                            (now - self.settings['ip_history_days'] * 86400,))

    def _move_friends(self, old, new):
        """Every friendship of `old`, both directions, moves to `new`. True if `old` had any."""
        self.db.execute('INSERT OR IGNORE INTO friend_adds SELECT ?, friend, created FROM friend_adds '
                        'WHERE code = ? AND friend != ?', (new, old, new))
        self.db.execute('INSERT OR IGNORE INTO friend_adds SELECT code, ?, created FROM friend_adds '
                        'WHERE friend = ? AND code != ?', (new, old, new))
        return self.db.execute('DELETE FROM friend_adds WHERE code = ? OR friend = ?',
                               (old, old)).rowcount > 0

    def login_without_code(self, tenant=None):
        """An emulator from before friend codes: no account, a new user each time."""
        blocked = self.settings['blocked_games'].split(',') if self.settings['blocked_games'] else []
        if tenant and tenant in blocked:
            return f'this server does not host {GAMES.get(tenant, tenant)}'
        if self.settings['registration'] != 'open':
            return 'this server needs a friend code: update the emulator'
        return None

    def _add_friends(self, code, codes, now):
        room = self.settings['max_friends'] - self._one(
            'SELECT COUNT(*) FROM friend_adds WHERE code = ?', (code,))
        changed = False
        for friend in codes:
            if room <= 0:
                break
            if CODE.fullmatch(friend) and int(friend) and friend != code:
                if self.db.execute('INSERT OR IGNORE INTO friend_adds VALUES (?, ?, ?)',
                                   (code, friend, now)).rowcount:
                    changed, room = True, room - 1
        return changed

    def exists(self, code):
        with self.lock:
            return self._one('SELECT 1 FROM accounts WHERE code = ?', (code,)) is not None

    def set_name(self, code, name):
        name = str(name)[:40]
        with self.lock:
            if self.db.execute('UPDATE accounts SET name = ? WHERE code = ? AND name IS NOT ?',
                               (name, code, name)).rowcount:
                self.version += 1

    # --- friends --------------------------------------------------------------------------------

    def friends_of(self, code):
        with self.lock:
            return [f for (f,) in self.db.execute(
                'SELECT a.friend FROM friend_adds a JOIN friend_adds b '
                'ON b.code = a.friend AND b.friend = a.code WHERE a.code = ? ORDER BY a.created',
                (code,))]

    def requests_for(self, code):
        """Codes that added this one and aren't added back."""
        with self.lock:
            return [c for (c,) in self.db.execute(
                'SELECT a.code FROM friend_adds a WHERE a.friend = ? AND NOT EXISTS '
                '(SELECT 1 FROM friend_adds b WHERE b.code = ? AND b.friend = a.code) ORDER BY a.created',
                (code, code))]

    def pending_from(self, code):
        """Codes this one added that haven't added it back."""
        with self.lock:
            return [c for (c,) in self.db.execute(
                'SELECT a.friend FROM friend_adds a WHERE a.code = ? AND NOT EXISTS '
                '(SELECT 1 FROM friend_adds b WHERE b.code = a.friend AND b.friend = ?) ORDER BY a.created',
                (code, code))]

    # --- moderation (admin panel) ----------------------------------------------------------------

    def is_banned(self, code):
        return code in self.banned_codes

    def ip_banned(self, ip):
        return ip in self.banned_ips

    def admin(self, op, code, other=None, text=None):
        """An admin action on an account: 'ok' or why it failed."""
        code = normalize_code(code)
        if code is None:
            return 'not a friend code'
        now = time.time()
        with self.lock:
            known = self._one('SELECT 1 FROM accounts WHERE code = ?', (code,)) is not None
            if op in ('befriend', 'unfriend', 'transfer'):
                other = normalize_code(other)
                if other is None or other == code:
                    return 'needs a second, different friend code'
            if op == 'befriend':
                for a, b in ((code, other), (other, code)):
                    self.db.execute('INSERT OR IGNORE INTO friend_adds VALUES (?, ?, ?)', (a, b, now))
            elif op == 'unfriend':
                self.db.execute('DELETE FROM friend_adds WHERE (code = ? AND friend = ?) OR '
                                '(code = ? AND friend = ?)', (code, other, other, code))
            elif not known:
                return 'no such account'
            elif op == 'approve':
                self.db.execute('UPDATE accounts SET approved = 1 WHERE code = ?', (code,))
            elif op == 'ban':
                self.db.execute('UPDATE accounts SET banned = ? WHERE code = ?',
                                ((text or 'banned')[:200], code))
                self.banned_codes.add(code)
            elif op == 'unban':
                self.db.execute('UPDATE accounts SET banned = NULL WHERE code = ?', (code,))
                self.banned_codes.discard(code)
            elif op == 'release':
                self.db.execute('UPDATE accounts SET secret = NULL, released = ? WHERE code = ?',
                                (now, code))
            elif op == 'note':
                self.db.execute('UPDATE accounts SET note = ? WHERE code = ?', ((text or '')[:200] or None, code))
            elif op == 'delete':
                self.db.execute('DELETE FROM friend_adds WHERE code = ? OR friend = ?', (code, code))
                self.db.execute('DELETE FROM code_ips WHERE code = ?', (code,))
                self.db.execute('DELETE FROM accounts WHERE code = ?', (code,))
                self.banned_codes.discard(code)
            elif op == 'transfer':
                # A player lost their emulator data and kept the new code: their friendships
                # (both directions) move from the old code to the new one
                self.db.execute('BEGIN')
                self._move_friends(code, other)
                self.db.execute('UPDATE accounts SET note = ? WHERE code = ?',
                                (f'friends moved to {format_code(other)}', code))
                self.db.execute('COMMIT')
            else:
                return 'unknown action'
            self.version += 1
        return 'ok'

    def ban_ip(self, ip, reason=None):
        if not IPV4.fullmatch(ip):
            return 'not an IPv4 address'
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO ip_bans VALUES (?, ?, ?)',
                            (ip, (reason or 'banned')[:200], time.time()))
            self.banned_ips.add(ip)
        return 'ok'

    def unban_ip(self, ip):
        with self.lock:
            self.db.execute('DELETE FROM ip_bans WHERE ip = ?', (ip,))
            self.banned_ips.discard(ip)
        return 'ok'

    # --- settings ---------------------------------------------------------------------------------

    def get(self, key):
        return self.settings[key]

    def room_size(self, tenant):
        return self.settings['room_sizes'].get(tenant) or ROOM_SIZES.get(tenant) or \
            self.settings['room_max_players']

    def game_enabled(self, tenant):
        return tenant not in self.settings['blocked_games'].split(',')

    def set_game(self, tenant, enabled=None, room_size=None):
        """The admin panel's Games section: None if stored, else why not."""
        if not TENANT.fullmatch(str(tenant)):
            return 'tenant ids look like t-54a666f5-lp1'
        if enabled is not None:
            blocked = [g for g in self.settings['blocked_games'].split(',') if g and g != tenant]
            why = self.set('blocked_games', ','.join(blocked + ([] if enabled else [tenant])))
            if why:
                return why
        if room_size is not None:
            if room_size != '':
                try:
                    room_size = int(room_size)
                except (TypeError, ValueError):
                    return 'the room size is a number'
            sizes = dict(self.settings['room_sizes'])
            if room_size == '' or room_size == ROOM_SIZES.get(tenant):
                sizes.pop(tenant, None)
            else:
                sizes[tenant] = room_size
            return self.set('room_sizes', sizes)
        return None

    def set(self, key, value):
        """None if stored, else why not."""
        stored, why = check_setting(key, value)
        if why:
            return why
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, json.dumps(stored)))
            self.settings[key] = stored
        return None

    # --- views --------------------------------------------------------------------------------------

    def rows(self, online=frozenset()):
        """Every account for the admin panel, newest activity first, with the IP addresses it
        signed in from, the other codes seen at those addresses and its emulator's other codes."""
        with self.lock:
            adds = {}
            for code, friend in self.db.execute('SELECT code, friend FROM friend_adds'):
                adds.setdefault(code, set()).add(friend)
            ips, at = {}, {}
            for code, ip, first, last, logins in self.db.execute(
                    'SELECT code, ip, first, last, logins FROM code_ips ORDER BY last DESC'):
                ips.setdefault(code, []).append({'ip': ip, 'first': first, 'last': last, 'logins': logins})
                at.setdefault(ip, set()).add(code)
            accounts = self.db.execute('SELECT code, secret, name, created, seen, approved, banned, note, '
                                       'released FROM accounts ORDER BY COALESCE(seen, created) DESC').fetchall()
            emulators = {}
            for code, secret, *_ in accounts:
                if secret:
                    emulators.setdefault(secret, set()).add(code)
            out = []
            for code, secret, name, created, seen, approved, banned, note, released in accounts:
                mine = adds.get(code, set())
                friends = sorted(f for f in mine if code in adds.get(f, ()))
                requests = sorted(c for c, theirs in adds.items() if code in theirs and c not in mine)
                mine_ips = ips.get(code, [])
                out.append({'code': code, 'name': name, 'created': created, 'seen': seen,
                            'approved': bool(approved), 'banned': banned, 'note': note,
                            'claimable': secret is None, 'released': released,
                            'online': code in online, 'friends': friends, 'requests': requests,
                            'pending': sorted(f for f in mine if code not in adds.get(f, ())),
                            'ips': mine_ips,
                            'same_ip': sorted(set().union(*(at[e['ip']] for e in mine_ips)) - {code}),
                            'same_emulator': sorted(emulators.get(secret, set()) - {code})})
            return out

    def ip_bans(self):
        with self.lock:
            return [{'ip': ip, 'reason': reason, 'created': created}
                    for ip, reason, created in self.db.execute(
                        'SELECT ip, reason, created FROM ip_bans ORDER BY created DESC')]

    def settings_view(self):
        return [{'key': k, 'value': self.settings[k], 'default': d,
                 'choices': list(allowed) if isinstance(allowed, tuple) and allowed and isinstance(allowed[0], str) else None,
                 'range': list(allowed) if isinstance(allowed, tuple) and allowed and not isinstance(allowed[0], str) else None,
                 'kind': 'bool' if allowed is bool else 'text' if allowed is str else
                         'choice' if isinstance(allowed[0], str) else 'number',
                 'label': label}
                for k, (d, allowed, label) in SETTINGS.items() if allowed is not dict]
