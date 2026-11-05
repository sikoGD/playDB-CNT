"""Reservoir: a private server for games on Nintendo's NPLN online platform (Splatoon Raiders,
Mario Kart World), played through the Droplet emulator. A gRPC (TLS, HTTP/2) server that accepts
every NPLN method, logs the call (metadata and a raw decode of the request protobuf) and answers
from REPLIES, or with an empty message. It also answers Pia's NAT check (UDP 10025/10125),
receives Pia's connection-quality reports (UDP 34343) and serves a status page with an admin
panel (http://127.0.0.1:8090/, /admin).

    python -I npln_stub.py [--port 8443] [--data FOLDER] [--pki FOLDER] [--log FILE]
        [--web-port 8090] [--web-host 127.0.0.1] [--trust-proxy [NETWORKS]]
        [--test-players N] [--match-delay SECONDS]
    python -I npln_stub.py --set-admin-password     (asks twice; stores only a scrypt hash)

Matchmaking pairs a Call for Help ticket with Go Help tickets (rescue_match_config attributes)
and answers with PLACING, then SUCCEEDED and a game session. --test-players keeps server-side
players calling for help, so one game can be matched; the raid itself (Pia, peer to peer) still
needs a second real game.

Players point Droplet at this server's address and pick its CA certificate (ca.der, made by
make_pki.py). Everything listens on 127.0.0.1 only, unless --host names addresses for the
game-facing services (e.g. --host 127.0.0.1,10.0.0.202 so a second PC on
the home network can play; the status page and admin panel stay on --web-host).
"""
import argparse, base64, copy, json, math, os, queue, re, secrets, socket, struct, sys, threading, time
from collections import Counter, defaultdict, deque
from concurrent import futures
from pathlib import Path

import grpc

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import npln_accounts, npln_dashboard  # noqa: E402  (siblings; -I drops the script directory)
try:  # decodes the reports with keys from the player's own game: a private tool, not published
    import pia_monitoring  # noqa: E402
except ImportError:
    pia_monitoring = None

ONLINE = TOOLS.parent
PKI = ONLINE / 'pki'

# RPC kinds from the game's generated stubs (RpcMethod type 2 = server streaming, 3 = bidi).
STREAMING = {
    'SubscribeFriendUsers': 'server', 'SubscribePresences': 'server', 'KeepAlive': 'bidi',
    'SubscribeMaintenanceSchedules': 'server', 'TrackMatchmakingTicket': 'server',
    'TrackGameSessionCreationTicket': 'server', 'KeepUserSession': 'bidi',
}
LOGIN = 'IssuePrearrangedUserToken'

# method name -> function(request bytes, call info dict) -> response bytes (protobuf)
REPLIES = {}


def reply(name):
    def register(fn):
        REPLIES[name] = fn
        return fn
    return register


# --- protobuf wire helpers ---------------------------------------------------------------------

def varint(n):
    out = bytearray()
    n &= (1 << 64) - 1
    while True:
        b = n & 0x7f
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(num, value):
    """Encode one field: int -> varint, bytes/str -> length-delimited."""
    if isinstance(value, bool) or isinstance(value, int):
        return varint(num << 3) + varint(int(value))
    if isinstance(value, str):
        value = value.encode()
    return varint(num << 3 | 2) + varint(len(value)) + value


def read_varint(buf, i):
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7f) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def decode(buf, depth=0):
    """Best-effort raw decode: [[num, wiretype, value], ...]; None if buf isn't a message."""
    out, i = [], 0
    try:
        while i < len(buf):
            key, i = read_varint(buf, i)
            num, wt = key >> 3, key & 7
            if num == 0:
                return None
            if wt == 0:
                v, i = read_varint(buf, i)
            elif wt == 1:
                v = int.from_bytes(buf[i:i + 8], 'little'); i += 8
            elif wt == 5:
                v = int.from_bytes(buf[i:i + 4], 'little'); i += 4
            elif wt == 2:
                n, i = read_varint(buf, i)
                raw = buf[i:i + n]; i += n
                if len(raw) != n:
                    return None
                sub = decode(raw, depth + 1) if raw and depth < 40 else None  # documents nest deep
                try:
                    text = raw.decode('utf-8')
                    printable = all(c.isprintable() for c in text)
                except UnicodeDecodeError:
                    printable = False
                # Printable text that starts like a name is a string even when it also parses
                # as a message ("ussid" does: tag 0x75 and a fixed32).
                if printable and (sub is None or len(raw) < 3 or text[0].isalnum() or text[0] in '_`'):
                    v = text
                elif sub is not None:
                    v = {'msg': sub, 'hex': raw.hex()}  # raw kept: bytes fields can parse as messages
                else:
                    v = {'hex': raw.hex()}
            else:
                return None
            out.append([num, wt, v])
        return out if i == len(buf) else None
    except IndexError:
        return None


def first_string(req, num):
    for n, wt, v in decode(req) or []:
        if n == num and isinstance(v, str):
            return v
    return ''


def first_bytes(req, num):
    """Raw bytes of the first length-delimited field num (a nested message), or b''."""
    i = 0
    while i < len(req):
        key, i = read_varint(req, i)
        n, wt = key >> 3, key & 7
        if wt == 0:
            _, i = read_varint(req, i)
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        elif wt == 2:
            size, i = read_varint(req, i)
            if n == num:
                return req[i:i + size]
            i += size
        else:
            break
    return b''


def timestamp(seconds=None):
    """google.protobuf.Timestamp / Duration: 1 seconds (int64), 2 nanos (int32)."""
    return field(1, int(time.time() if seconds is None else seconds))


def map_value(decoded):
    """nn.npln.common.MapValue (#25, a oneof): 1 int32, 2 bool, 3 int64, 4 fixed32, 5 fixed64,
    6 Timestamp, 7/11 string, 8 bytes, 9 list (#26: repeated MapValue), 10 map (#28)."""
    for n, wt, v in decoded or []:
        if n in (1, 3) and isinstance(v, int):
            return v - (1 << 64) if v >= 1 << 63 else v
        if n == 2 and isinstance(v, int):
            return bool(v)
        if n == 4 and isinstance(v, int):
            return Fixed32(v)
        if n == 5 and isinstance(v, int):
            return Fixed64(v)
        if n in (7, 11):
            return v if isinstance(v, str) else ''
        if n == 8:  # bytes: keep them bytes, or a reader of bytes gets nothing (Pia's "pl")
            if isinstance(v, dict) and 'hex' in v:
                return bytes.fromhex(v['hex'])
            return v.encode() if isinstance(v, str) else bytes()
        if n == 6 and isinstance(v, dict):
            return next((x[2] for x in v.get('msg', []) if x[0] == 1), 0)
        if n == 9:
            items = v.get('msg', []) if isinstance(v, dict) else []
            return [map_value(x[2]['msg']) if isinstance(x[2], dict) and 'msg' in x[2] else x[2]
                    for x in items if x[0] == 1]
        if n == 10:
            return attributes(v.get('msg', []) if isinstance(v, dict) else [])
    return None


class Fixed32(int):
    """A MapValue fixed32 (4), kept as its 4 bytes so it goes back to the games unchanged."""


class Fixed64(int):
    """A MapValue fixed64 (5): Mario Kart World's doubles (its getter reads type
    5 only), kept as their 8 bytes so they go back to the games unchanged."""

    @classmethod
    def double(cls, x):
        return cls(struct.unpack('<Q', struct.pack('<d', float(x)))[0])


def encode_value(v):
    """The reverse of map_value: a Python value -> MapValue (#25) bytes. The game's getters
    want int64 (3) for numbers and 7 for strings."""
    if isinstance(v, bool):
        return field(2, v)
    if isinstance(v, Fixed32):
        return varint(4 << 3 | 5) + struct.pack('<I', v)
    if isinstance(v, Fixed64):
        return varint(5 << 3 | 1) + struct.pack('<Q', v)
    if isinstance(v, int):
        return field(3, v)
    if isinstance(v, str):
        return field(7, v)
    if isinstance(v, (bytes, bytearray)):
        return field(8, bytes(v))
    if isinstance(v, list):
        return field(9, b''.join(field(1, encode_value(x)) for x in v))
    if isinstance(v, dict):
        return field(10, encode_map(v))
    return b''


def encode_map(d):
    """dict -> map<string, MapValue> entries (the body of #28)."""
    return b''.join(field(1, field(1, k) + field(2, encode_value(v))) for k, v in d.items())


def attributes(entries):
    """A map<string, MapValue>: repeated {1 key, 2 MapValue} entries -> dict."""
    out = {}
    for n, wt, v in entries or []:
        if isinstance(v, dict) and 'msg' in v:
            key = next((x[2] for x in v['msg'] if x[0] == 1 and isinstance(x[2], str)), None)
            val = next((x[2] for x in v['msg'] if x[0] == 2), None)
            if key is not None:
                out[key] = map_value(val['msg'] if isinstance(val, dict) and 'msg' in val else [])
    return out


def version_of(attrs):
    """The game version a ticket or room member carries: Mario Kart World's batching_key (or its
    rom_id; both "cv13" in update 917504), Splatoon Raiders' match_hash (1 in update 65536, the
    only version seen so far). None when it carries neither."""
    for key, label in (('batching_key', '{}'), ('rom_id', '{}'), ('match_hash', 'match {}')):
        value = (attrs or {}).get(key)
        if value not in (None, ''):
            return label.format(value)
    return None


def compatible(a, b):
    """Two players' game versions or builds can meet unless both are known and differ."""
    return not (a and b and a != b)


def member_version(member_fields):
    """version_of the attributes (field 2) of a decoded TicketMember."""
    return next((version_of(attributes(a)) for a in submsgs(member_fields, 2)), None)


# --- matchmaking enums (values from the game's protobuf name tables) ----------------------------

SEARCHING, PLACING, SUCCEEDED, TIMED_OUT, FAILED, CANCELLED, REQUIRING_ACCEPTANCE, DECLINED = \
    1, 2, 3, 4, 5, 6, 7, 8
TERMINAL = {SUCCEEDED, TIMED_OUT, FAILED, CANCELLED, DECLINED}
TICKET_STATE = {SEARCHING: 'searching', PLACING: 'placing', SUCCEEDED: 'matched',
                TIMED_OUT: 'timed out', FAILED: 'failed', CANCELLED: 'cancelled',
                REQUIRING_ACCEPTANCE: 'needs acceptance', DECLINED: 'declined'}
SESSION_CREATING, SESSION_ACTIVE, SESSION_TERMINATING, SESSION_TERMINATED = 1, 2, 3, 4


# --- server state (shown on the web page) ------------------------------------------------------

# Splatoon Raiders' own matchmaking configs (State.match handles them); other games' configs
# go to the general matcher (State._match_other_games).
RAIDERS_CONFIGS = {'rescue_match_config', 'spiral_search_config'}
NO_BACKFILL_CONFIGS = {'Survival24', 'ClutchSurvival24'}  # Mario Kart World Knockout Tour
RAIDERS_TENANT = 't-54a666f5-lp1'


def is_raiders(s):
    """Whether a game session is Splatoon Raiders' (its tenant, or one of its configs)."""
    return s.get('tenant') == RAIDERS_TENANT or s.get('config') in RAIDERS_CONFIGS


def config_size(config):
    """Players a session of this config holds: the number its name ends with (Mario Kart World's
    Normal24), else 12."""
    digits = re.search(r'(\d+)$', config)
    return max(2, min(int(digits.group(1)), 99)) if digits else 12


class State:
    ONLINE_SECONDS = 120  # logged in but no stream open yet: online this long after the last call
    LEFT_SECONDS = 15     # every stream closed (game quit or lost its connection): gone after this
    MATCH_DELAY = 6       # a Go Help ticket searches this long before the matcher looks at it

    def __init__(self, audit_path):
        self.lock = threading.RLock()
        self.started = time.time()
        self.game_port = None  # shown on the status page's How to join
        self.tenant = ''           # the tenant (game) of the last login, for the status page
        self.user_tenant = {}      # uid -> the tenant (game) it last signed in to
        self.users = {}            # uid -> dict
        self.tokens = {}           # bearer token -> uid
        self.tickets = {}          # ticket id -> dict
        self.calls = Counter()
        self.nat = {}              # "ip:port" -> dict
        self.reports = deque(maxlen=200)
        self.player_reports = deque(maxlen=200)
        # Bans and maintenance live in ACCOUNTS and survive restarts. These are players without a
        # friend code (older emulators), banned for this run only, and kicks.
        self.banned_uids, self.kicked = set(), set()
        self.audit_log = deque(maxlen=200)
        self.audit_path = audit_path
        self.next_user = 1
        self.sessions = {}         # game session id -> dict
        self.joined = {}           # uid -> the game session it joined or created last
        self.login_times = {}      # address -> recent login times (rate limit)
        self.test_players = 0      # keep this many test players calling for help
        self.overrides = {}        # settings given on the command line, for this run only

    def setting(self, key):
        return self.overrides.get(key, ACCOUNTS.get(key))

    # The matcher's knobs: server settings in the admin panel
    rescue_helpers = property(lambda self: self.setting('rescue_helpers'))
    loose_match = property(lambda self: self.setting('loose_match'))
    solo_match_after = property(lambda self: self.setting('solo_match_after'))
    knockout_solo_after = property(lambda self: self.setting('knockout_solo_after'))

    def tenant_of(self, uid):
        with self.lock:
            return self.user_tenant.get(uid) or self.tenant or 'current'

    # players
    def new_user(self, peer_ip, code=None):
        """A login: a player with a friend code keeps the same user id (code_uid) every time;
        without one each login is a new user."""
        with self.lock:
            if code:
                uid = code_uid(code)
            else:
                uid = f'u-{self.next_user:016x}'
                self.next_user += 1
            now = time.time()
            old = self.users.get(uid, {})
            self.users[uid] = {'uid': uid, 'ip': peer_ip, 'peer': peer_ip, 'since': now,
                               'last': now, 'left': None, 'streams': old.get('streams', Counter()),
                               'presence': old.get('presence', {}), 'code': code}
            return uid

    def touch(self, uid, peer, method):
        with self.lock:
            self.calls[method] += 1
            u = self.users.get(uid)
            if u is None and uid.startswith('u-') and ACCOUNTS is not None:
                # After a stub restart the games keep their (still valid) tokens and never log in
                # again, so their friend lists came back empty and presence went nowhere. A friend
                # code's user id carries the code: register the player again.
                try:
                    code = f'{int(uid[2:], 16):012d}'
                except ValueError:
                    code = None
                if code and ACCOUNTS.exists(code):
                    self.new_user(peer, code)
                    u = self.users.get(uid)
            if u:
                u['last'] = time.time()
                u['peer'] = peer

    def stream(self, uid, method, delta):
        with self.lock:
            u = self.users.get(uid)
            if u:
                u['streams'][method] += delta
                if u['streams'][method] <= 0:
                    del u['streams'][method]
                u['left'] = None if u['streams'] else time.time()

    def _online(self, u, now):
        if u['streams'] or u.get('bot'):
            return True
        if u['left']:
            return now - max(u['left'], u['last']) < self.LEFT_SECONDS
        return now - u['last'] < self.ONLINE_SECONDS

    def _expire_tickets(self, now):
        """A ticket whose owner has gone is abandoned (an unclean exit never cancels it)."""
        for t in self.tickets.values():
            u = self.users.get(t['uid'])
            if t['status'] == SEARCHING and (u is None or not self._online(u, now)):
                self._set_status(t, CANCELLED)
                t['state'] = 'abandoned'

    def presence(self, uid, attrs, mask=()):
        """A presence update: every attribute, or with a field mask only the keys it names (a
        named key the update leaves out is cleared). The game sends the full set once, then
        only what changed, so replacing the set lost the player's name and level."""
        with self.lock:
            if uid not in self.users:
                return
            if mask:
                p = dict(self.users[uid].get('presence') or {})
                for key in mask:
                    if key in attrs:
                        p[key] = attrs[key]
                    else:
                        p.pop(key, None)
                attrs = p
            self.users[uid]['presence'] = attrs

    def login_allowed(self, ip, window=60):
        """At most login_limit logins a minute from one address (a game logs in once per session)."""
        limit = self.setting('login_limit')
        now = time.time()
        with self.lock:
            recent = [t for t in self.login_times.get(ip, []) if now - t < window]
            allowed = len(recent) < limit
            if allowed:
                recent.append(now)
            self.login_times[ip] = recent
            return allowed

    def denied(self, uid, ip, method=''):
        """Why this caller may not continue (None if it may)."""
        with self.lock:
            code = self.users.get(uid, {}).get('code')
            if ACCOUNTS.ip_banned(ip) or uid in self.banned_uids or ACCOUNTS.is_banned(code):
                return 'banned'
            if uid in self.kicked:
                return 'kicked'
            if method == LOGIN and ACCOUNTS.get('maintenance'):
                return 'maintenance'
            return None

    # matchmaking
    def ticket(self, tid, uid, config, attrs, bot=False, tenant=None):
        with self.lock:
            self.tickets[tid] = {'id': tid, 'uid': uid, 'config': config.rsplit('/', 1)[-1],
                                 'attributes': attrs, 'created': time.time(), 'state': 'searching',
                                 'status': SEARCHING, 'version': 0, 'session': None, 'bot': bot,
                                 'tenant': tenant or self.tenant_of(uid)}

    def note_version(self, uid, version):
        """Remember the game version a player last showed (a ticket, a room they made or joined)."""
        if version:
            with self.lock:
                if uid in self.users:
                    self.users[uid]['version'] = version

    def user_version(self, uid):
        with self.lock:
            return self.users.get(uid, {}).get('version')

    def note_build(self, uid, build):
        """The game build a player signed in with (identity_build)."""
        if build:
            with self.lock:
                if uid in self.users:
                    self.users[uid]['build'] = build

    def build_fits(self, s, uid):
        """Whether uid's game build matches every member of session s whose build is known."""
        with self.lock:
            mine = self.users.get(uid, {}).get('build')
            return all(compatible(self.users.get(m, {}).get('build'), mine) for m in s['members'] if m != uid)

    def ticket_state(self, tid, value, status=None):
        with self.lock:
            t = self.tickets.get(tid)
            if t:
                t['state'] = value
                if status is not None and t['status'] not in TERMINAL:
                    t['status'] = status
                    t['version'] += 1

    def ticket_version(self, tid):
        with self.lock:
            t = self.tickets.get(tid)
            return t['version'] if t else -1

    def _set_status(self, t, status, session=None):
        t['status'], t['state'] = status, TICKET_STATE[status]
        if session:
            t['session'] = session
        t['version'] += 1

    def add_test_player(self):
        """A server-side player who calls for help (no emulator behind it). It takes whatever
        stage the helper can play, so any Go Help ticket can match it."""
        with self.lock:
            uid = f'u-{0xb0700000 + self.next_user:016x}'
            self.next_user += 1
            n = sum(1 for u in self.users.values() if u.get('bot')) + 1
            now = time.time()
            self.users[uid] = {'uid': uid, 'ip': 'test', 'peer': 'test player', 'since': now,
                               'last': now, 'left': None, 'streams': Counter(), 'bot': True,
                               'presence': {'PlayerName': f'TestBot{n}', 'Level': 10,
                                            'GameStatus': 1}}
            tid = f'mt-{len(TICKETS) + 1:04d}'
            attrs = {'uid': uid, 'player_level': 10, 'is_rescued_player': 1, 'difficulty': 0,
                     'select_stage_level': 1, 'select_dungeon_type': 1, 'select_stage_id_1': 0,
                     'match_hash': 1}
            name = f'tenants/{self.tenant or "current"}/matchmakingTickets/{tid}'
            TICKETS[tid] = (name, b'')  # the body is rebuilt from the attributes when sent
            self.ticket(tid, uid, 'rescue_match_config', attrs, bot=True)
            return uid

    def _fits(self, caller, helper):
        a, b = caller['attributes'], helper['attributes']
        if a.get('match_hash') != b.get('match_hash'):
            return False
        if not compatible(self.users.get(caller['uid'], {}).get('build'),
                          self.users.get(helper['uid'], {}).get('build')):
            return False
        if a.get('uid') in (b.get('block_list') or []) or b.get('uid') in (a.get('block_list') or []):
            return False
        if caller['bot'] or self.loose_match:
            return True
        kind = a.get('select_dungeon_type') or 0
        return bool(kind) and a.get(f'select_stage_id_{kind}') in (b.get(f'playable_stage_id_{kind}') or [])

    def match(self, now):
        """One matchmaking pass: keep the test players topped up, move PLACING tickets to
        SUCCEEDED with their game session, and pair a caller with Go Help tickets that have
        searched for MATCH_DELAY seconds. Returns log events."""
        events = []
        with self.lock:
            waiting = sum(1 for t in self.tickets.values() if t['bot'] and t['status'] == SEARCHING)
            for _ in range(self.test_players - waiting):
                events.append({'ev': 'test-player', 'uid': self.add_test_player()})
            for t in self.tickets.values():
                if t['status'] == PLACING and now - t['placed'] >= 1:
                    self._set_status(t, SUCCEEDED)
            searching = [t for t in self.tickets.values() if t['status'] == SEARCHING]
            for caller in [t for t in searching if t['attributes'].get('is_rescued_player') == 1]:
                helpers = [t for t in searching if t['attributes'].get('is_rescued_player') == 0
                           and t['uid'] != caller['uid'] and now - t['created'] >= self.MATCH_DELAY
                           and self._fits(caller, t)]
                if not helpers:
                    continue
                helpers = self._agreeing(caller, helpers)[:int(self.rescue_helpers)]
                if caller['bot']:  # call for help on the first stage the helper can play
                    stages = [s for s in helpers[0]['attributes'].get('playable_stage_id_1') or [] if s]
                    caller['attributes']['select_stage_id_1'] = stages[0] if stages else 0
                sid = f'gs-{len(self.sessions) + 1:04d}'
                tenant = caller['tenant']
                session = {'id': sid, 'name': f'tenants/{tenant}/gameSessions/{sid}',
                           'tenant': tenant, 'created': now, 'host': caller['uid'],
                           'members': [caller['uid']] + [h['uid'] for h in helpers],
                           'tickets': [caller['id']] + [h['id'] for h in helpers],
                           'config': caller['config'],
                           'attributes': {k: v for k, v in caller['attributes'].items()
                                          if k.startswith('select_') or k == 'difficulty'},
                           'updated': {}, 'watchers': [], 'vars': {}, 'deferments': {}}
                session['docs'] = session_docs(session)
                self.sessions[sid] = session
                for member in session['members']:
                    self.joined[member] = sid
                for t in [caller] + helpers:
                    t['placed'] = now
                    self._set_status(t, PLACING, sid)
                events.append({'ev': 'match', 'session': sid, 'tickets': session['tickets'],
                               'members': session['members']})
                searching = [t for t in searching if t['status'] == SEARCHING]
            events += self._backfill(now, searching)
            searching = [t for t in searching if t['status'] == SEARCHING]
            events += self._match_other_games(now, searching)
            searching = [t for t in searching if t['status'] == SEARCHING]
            # The Online Play desk's Keyword tab is a ticket for spiral_search_config with
            # search_keyword: it finds the open room whose session_keyword (the
            # passcode its host set) matches, and the player joins that room.
            for t in [t for t in searching if t['config'] == 'spiral_search_config']:
                keyword = str(t['attributes'].get('search_keyword') or '')
                mine = version_of(t['attributes']) or self.users.get(t['uid'], {}).get('version')
                room = next((r for r in self.sessions.values() if r.get('room') and keyword
                             and r['attributes'].get('session_keyword') == keyword
                             and not (mine and r.get('version') and r['version'] != mine)
                             and self.build_fits(r, t['uid'])
                             and len(r['members']) < ACCOUNTS.room_size(r.get('tenant'))
                             and r['docs'].get('__gs/m', {}).get('cp', True)), None)
                if room is None:
                    if now - t['created'] >= KEYWORD_SEARCH_SECONDS:
                        self._set_status(t, TIMED_OUT)
                        events.append({'ev': 'keyword-not-found', 'ticket': t['id'], 'keyword': keyword})
                    continue
                if add_room_member(room, t['uid']):
                    room['tickets'][room['members'].index(t['uid'])] = t['id']
                    t['placed'] = now
                    self._set_status(t, PLACING, room['id'])
                    events.append({'ev': 'keyword-match', 'ticket': t['id'], 'session': room['id']})
        return events

    def _agreeing(self, caller, helpers):
        """The helpers that agree on the game build with the caller and with each other (each one
        fits the caller; when the caller's build is unknown, the first known helper build decides)."""
        build, kept = self.users.get(caller['uid'], {}).get('build'), []
        for h in helpers:
            mine = self.users.get(h['uid'], {}).get('build')
            if compatible(mine, build):
                kept.append(h)
                build = build or mine
        return kept

    def _build_queues(self, tickets):
        """One queue's tickets split by game build, so that a new session never mixes known builds:
        a list per known build, each with the tickets of unknown builds (older emulators) as well."""
        known, unknown = defaultdict(list), []
        for t in tickets:
            build = self.users.get(t['uid'], {}).get('build')
            (known[build] if build else unknown).append(t)
        return [same + unknown for same in known.values()] or [unknown]

    def _backfill(self, now, searching):
        """Mario Kart World and other games: a new ticket joins a session of its queue (same game,
        config and batching_key) while that session's host allows backfill (__gs/m ebf, which
        Mario Kart World keeps writing in its lobby), can take players (cp) and has room
        (max_players, or the host's bfmax), as when a player joins a lobby in progress. Someone
        must still be in it: the last ebf stays behind when everyone leaves, and a later player
        was backfilled into such an empty lobby (battle2)."""
        events = []
        for t in searching:
            if t['config'] in RAIDERS_CONFIGS or t['bot']:
                continue
            key = str(t['attributes'].get('batching_key', ''))
            for s in sorted(self.sessions.values(), key=lambda s: s['created']):
                m = s['docs'].get('__gs/m', {})
                limit = min(s.get('max_players', 0), int(m.get('bfmax') or s.get('max_players', 0)))
                if (s.get('room') or s.get('closed') or not s['members'] or not m.get('ebf')
                        or not any(p.startswith('__pgn/') and '/__pus/' in p for p in s['docs'])
                        or not m.get('cp', True) or s.get('tenant') != t['tenant']
                        or s.get('config') != t['config'] or t['uid'] in s['members']
                        or str(s['attributes'].get('batching_key', '')) != key
                        or len(s['members']) >= limit or not self.build_fits(s, t['uid'])):
                    continue
                if add_room_member(s, t['uid'], limit=limit):
                    s['tickets'][s['members'].index(t['uid'])] = t['id']
                    t['placed'] = now
                    self._set_status(t, PLACING, s['id'])
                    events.append({'ev': 'backfill', 'session': s['id'], 'ticket': t['id'],
                                   'members': list(s['members'])})
                    break
        return events

    def _match_other_games(self, now, searching):
        """Queues of other games, such as Mario Kart World's Normal24: tickets on one config
        whose batching_key (the game's version) and game builds agree become one session once two
        have searched MATCH_DELAY seconds, up to the player count at the end of the config's name.
        With solo_match_after, a lone ticket gets a session of its own after that long; Knockout
        Tour lobbies never let anyone in later (the host writes no `__gs/m`, ko1: the second game
        got a lobby of its own and both raced CPUs), so a lone Knockout ticket waits longer."""
        events, groups = [], defaultdict(list)
        for t in searching:
            if t['config'] not in RAIDERS_CONFIGS and not t['bot']:
                groups[(t['tenant'], t['config'], str(t['attributes'].get('batching_key', '')))].append(t)
        queues = [(tenant, config, same) for (tenant, config, _), tickets in groups.items()
                  for same in self._build_queues(tickets)]
        for tenant, config, tickets in queues:
            tickets = sorted((t for t in tickets if t['status'] == SEARCHING), key=lambda t: t['created'])
            if not tickets:
                continue
            ready = [t for t in tickets if now - t['created'] >= self.MATCH_DELAY]
            wait = self.solo_match_after
            if wait and config in NO_BACKFILL_CONFIGS:
                wait = max(wait, self.knockout_solo_after)
            alone = wait and now - tickets[0]['created'] >= wait
            if len(ready) < 2 and not alone:
                continue
            members = (ready if len(ready) >= 2 else tickets[:1])[:config_size(config)]
            sid = f'gs-{len(self.sessions) + 1:04d}'
            session = {'id': sid, 'name': f'tenants/{tenant}/gameSessions/{sid}', 'tenant': tenant,
                       'created': now, 'host': members[0]['uid'],
                       'members': [t['uid'] for t in members], 'tickets': [t['id'] for t in members],
                       'config': config, 'max_players': config_size(config),
                       'attributes': {k: v for k, v in members[0]['attributes'].items()
                                      if k != 'npln_user_id'},
                       'updated': {}, 'watchers': [], 'vars': {}, 'deferments': {}}
            session['docs'] = session_docs(session)
            self.sessions[sid] = session
            for t in members:
                self.joined[t['uid']] = sid
                t['placed'] = now
                self._set_status(t, PLACING, sid)
            events.append({'ev': 'match', 'session': sid, 'config': config,
                           'tickets': session['tickets'], 'members': session['members']})
        return events

    # udp side
    def nat_probe(self, src, kind):
        with self.lock:
            n = self.nat.setdefault(src, {'src': src, 'types': [], 'last': 0})
            if kind not in n['types']:
                n['types'].append(kind)
            n['last'] = time.time()

    def report(self, src, rep):
        with self.lock:
            rep = dict(rep, src=src, t=time.time())
            self.reports.appendleft(rep)

    def player_report(self, uid, method, req):
        with self.lock:
            self.player_reports.appendleft({'t': time.time(), 'uid': uid,
                                            'method': method, 'request': req})

    # moderation
    def audit(self, action, target, result):
        rec = {'t': time.time(), 'action': action, 'target': target, 'result': result}
        with self.lock:
            self.audit_log.appendleft(rec)
        with open(self.audit_path, 'a') as f:
            f.write(json.dumps(rec) + '\n')

    def act(self, action, target):
        with self.lock:
            if action == 'kick' and target in self.users:
                self.kicked.add(target)
                self._cancel_tickets(lambda u: u == target)
            elif action == 'ban_user' and target in self.users:
                code = self.users[target].get('code')
                if code:
                    ACCOUNTS.admin('ban', code, text='banned from the player list')
                else:
                    self.banned_uids.add(target)
                self._cancel_tickets(lambda u: u == target)
            elif action == 'ban_ip':
                ACCOUNTS.ban_ip(target)
                self._cancel_tickets(lambda u: self.users.get(u, {}).get('ip') == target)
            elif action == 'unban_user':
                self.banned_uids.discard(target)
                self.kicked.discard(target)
                code = self.users.get(target, {}).get('code') or uid_code(target)
                if code and ACCOUNTS.is_banned(code):
                    ACCOUNTS.admin('unban', code)
            elif action == 'unban_ip':
                ACCOUNTS.unban_ip(target)
            elif action == 'cancel_ticket' and target in self.tickets:
                if self.tickets[target]['status'] not in TERMINAL:
                    self._set_status(self.tickets[target], CANCELLED)
                self.tickets[target]['state'] = 'cancelled by admin'
            elif action == 'maintenance':
                ACCOUNTS.set('maintenance', target == 'on')
            elif action == 'close_room' and target in self.sessions:
                # Nobody new can join (the game reads cp, "can participate"); members stay
                s = self.sessions[target]
                s['closed'] = True
                s['docs'].setdefault('__gs/m', {})['cp'] = False
                s['updated']['__gs/m'] = time.time()
                push_change(s, '__gs/m', lambda **kw: None)
            elif action == 'add_test_player':
                self.add_test_player()
            else:
                return 'unknown target'
            return 'ok'

    def _cancel_tickets(self, owned):
        for t in self.tickets.values():
            if t['status'] == SEARCHING and owned(t['uid']):
                self._set_status(t, CANCELLED)
                t['state'] = 'cancelled by admin'

    # views
    def _players(self, now, private):
        out = []
        for u in self.users.values():
            removed = u['uid'] in self.kicked or u['uid'] in self.banned_uids or \
                ACCOUNTS.ip_banned(u['ip']) or ACCOUNTS.is_banned(u.get('code'))
            if removed and not private:
                continue
            online = self._online(u, now)
            searching = online and any(t['uid'] == u['uid'] and t['status'] == SEARCHING
                                       for t in self.tickets.values())
            p = u['presence']
            tenant = self.user_tenant.get(u['uid'])
            row = {
                'uid': u['uid'] if private else '', 'name': p.get('PlayerName'), 'level': p.get('Level'),
                'game': npln_accounts.GAMES.get(tenant, tenant),
                'online': online,
                'searching': searching, 'since': now - u['since'], 'idle': now - u['last'],
                'presence_short': {k: p[k] for k in ('GameStatus', 'GameMode', 'Style',
                                                     'Difficulty') if k in p},
                'friend_code': format_code(u.get('code')) if private else None,
                'version': u.get('version'), 'build': u.get('build'),
            }
            if private:
                row.update(peer=u['peer'], ip=u['ip'], presence=p, streams=dict(u['streams']),
                           kicked=u['uid'] in self.kicked,
                           banned=u['uid'] in self.banned_uids or ACCOUNTS.is_banned(u.get('code')))
            out.append(row)
        out.sort(key=lambda r: (not r['online'], r['idle']))
        return out

    def _rooms(self, now):
        def name(uid):
            return self.users.get(uid, {}).get('presence', {}).get('PlayerName') or uid
        return [{'id': s['id'], 'game': npln_accounts.GAMES.get(s.get('tenant'), s.get('tenant')),
                 'config': s.get('config'), 'room': bool(s.get('room')), 'closed': bool(s.get('closed')),
                 'age': now - s['created'], 'host': name(s['host']) if s.get('host') else None,
                 'members': [name(u) for u in s['members']], 'version': s.get('version'),
                 'build': next((self.users[u]['build'] for u in s['members']
                                if self.users.get(u, {}).get('build')), None),
                 'passcode': bool(s.get('attributes', {}).get('session_keyword'))}
                for s in sorted(self.sessions.values(), key=lambda s: -s['created']) if s['members']][:50]

    def _games(self, now):
        """Per game (tenant): who is online and searching, its open rooms and sessions."""
        games = {t: {'tenant': t, 'name': n, 'online': 0, 'searching': 0, 'rooms': 0, 'sessions': 0}
                 for t, n in npln_accounts.GAMES.items()}
        def entry(tenant):
            return games.setdefault(tenant, {'tenant': tenant, 'name': tenant, 'online': 0,
                                             'searching': 0, 'rooms': 0, 'sessions': 0})
        for u in self.users.values():
            if self._online(u, now) and self.user_tenant.get(u['uid']):
                entry(self.user_tenant[u['uid']])['online'] += 1
        for t in self.tickets.values():
            if t['status'] == SEARCHING and t.get('tenant'):
                entry(t['tenant'])['searching'] += 1
        for s in self.sessions.values():
            if s['members'] and s.get('tenant'):
                entry(s['tenant'])['rooms' if s.get('room') else 'sessions'] += 1
        for g in games.values():
            g.update(enabled=ACCOUNTS.game_enabled(g['tenant']), room_size=ACCOUNTS.room_size(g['tenant']))
        return sorted(games.values(), key=lambda g: (-g['online'], g['name']))

    def online_codes(self):
        with self.lock:
            now = time.time()
            return {u['code'] for u in self.users.values() if u.get('code') and self._online(u, now)}

    def snapshot(self, private=False):
        with self.lock:
            now = time.time()
            self._expire_tickets(now)
            players = self._players(now, private)
            names = {u['uid']: u['presence'].get('PlayerName') or u['uid']
                     for u in self.users.values()}
            tickets = [dict(t, player=names.get(t['uid'], t['uid']), age=now - t['created'],
                            game=npln_accounts.GAMES.get(t.get('tenant'), t.get('tenant')),
                            game_version=version_of(t['attributes']),
                            build=self.users.get(t['uid'], {}).get('build'))
                       for t in sorted(self.tickets.values(), key=lambda t: -t['created'])]
            reports = [{'age': now - r['t'], 'src': r['src'] if private else '',
                        'type': r.get('type'), 'size': r.get('size'),
                        'error': r.get('error'), 'body_size': r.get('body_size'),
                        'fields': len(r.get('fields', [])), 'text': r.get('text', [])}
                       for r in list(self.reports)[:50]]
            snap = {
                'uptime': now - self.started, 'tenant': self.tenant, 'game_port': self.game_port,
                'counts': {'online': sum(p['online'] for p in players),
                           'searching': sum(p['searching'] for p in players),
                           'tickets': len(tickets), 'sessions': len(self.sessions),
                           'reports': len(self.reports), 'calls': sum(self.calls.values())},
                'players': players, 'tickets': tickets[:50], 'reports': reports,
                'games': self._games(now),
                'nat': [dict(n, age=now - n['last'], src=n['src'] if private else '')
                        for n in self.nat.values()],
                'calls': self.calls.most_common(25),
            }
            if private:
                snap.update(maintenance=ACCOUNTS.get('maintenance'), banned_uids=sorted(self.banned_uids),
                            banned_ips=sorted(ACCOUNTS.banned_ips), kicked=sorted(self.kicked),
                            rooms=self._rooms(now),
                            audit=list(self.audit_log),
                            player_reports=[dict(r, age=now - r['t'])
                                            for r in list(self.player_reports)[:50]],
                            report_details=list(self.reports)[:20])
            return snap


STATE = None  # set in main()


# --- canned replies ------------------------------------------------------------------------------

def jwt_for(uid):
    """An unsigned JWT-shaped bearer token; the client only stores and echoes it."""
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b'=').decode()
    return f"{enc({'alg': 'none'})}.{enc({'sub': uid, 'exp': 4102444800})}."


def user(parent, user_id, alias='E'):
    """User (#18): 1 name tenants/{tenant}/users/{user} (the SDK aborts on any other form),
    2 string (alias?), 3/4 Timestamps (create/update?), 5 int64."""
    return (field(1, f'{parent}/users/{user_id}') + field(2, alias) +
            field(3, timestamp()) + field(4, timestamp()))


def user_token(parent, user_id, token):
    """#11, the user token: 1 user name tenants/{tenant}/users/{user} (the game
    aborts on any other form), 2 access token (sent back as "authorization: bearer"), 3 refresh
    token?, 4 Duration (lifetime). The client keeps 2 and 3 with an expiry from 4."""
    return (field(1, f'{parent}/users/{user_id}') + field(2, token) +
            field(3, 'refresh-' + user_id) + field(4, timestamp(3600)))


@reply(LOGIN)
def issue_prearranged_user_token(req, call):
    # request (#5): 1 parent "tenants/<tenant>", 2 int32, 3 credential {1 NSA ID token | 2 ...}
    # response (#6): 1 User (#18), 2 user token (#11). The emulator puts its online identity in
    # the NSA ID token (see parse_identity): a player with a friend code keeps one user id.
    # Without one (an older emulator) every login is a new user.
    parent = first_string(req, 1) or 'tenants/' + call['meta'].get('npln-tenant-id', 'unknown')
    credential = submsgs(decode(req), 3)
    token_text = substr(credential[0], 1) if credential else ''
    identity = parse_identity(token_text)
    tenant = parent.rsplit('/', 1)[-1]
    code = None
    if identity:
        why = ACCOUNTS.login(*identity, tenant=tenant, ip=call['ip'])
        code = identity[0]
    else:
        why = ACCOUNTS.login_without_code(tenant)
    if why:
        call['log'](ev='login-refused', code=format_code(code), why=why)
        raise RpcError(grpc.StatusCode.PERMISSION_DENIED, why)
    uid = STATE.new_user(call['ip'], code)
    STATE.note_build(uid, identity_build(token_text))
    token = jwt_for(uid)
    with STATE.lock:
        STATE.tokens[token] = uid
        STATE.kicked.discard(uid)  # a kick ends the session it hit; signing in again is fine
        STATE.tenant = STATE.user_tenant[uid] = tenant
    call['uid'] = uid
    return field(1, user(parent, uid)) + field(2, user_token(parent, uid, token))


def ticket_id(name):
    return name.rsplit('/', 1)[-1]


TICKETS = {}


@reply('CreateMatchmakingTicket')
def create_matchmaking_ticket(req, call):
    # request (#114?): 1 parent, 2 MatchmakingTicket (#122: 2 config name, 4 members with their
    # attributes); reply #122. The client parses field 1 as
    # tenants/{tenant}/matchmakingTickets/{ticket} and aborts otherwise.
    ticket = first_bytes(req, 2)
    tenant = call['meta'].get('npln-tenant-id', 'unknown')
    name = f'tenants/{tenant}/matchmakingTickets/mt-{len(TICKETS) + 1:04d}'
    TICKETS[ticket_id(name)] = (name, ticket)
    decoded = decode(ticket) or []
    config = next((v for n, wt, v in decoded if n == 2 and isinstance(v, str)), '')
    attrs = {}
    for n, wt, v in decoded:
        if n == 4 and isinstance(v, dict):
            for m, wt2, v2 in v['msg']:
                if m == 2 and isinstance(v2, dict):
                    attrs = attributes(v2['msg'])
    STATE.ticket(ticket_id(name), call['uid'], config, attrs, tenant=tenant)
    STATE.note_version(call['uid'], version_of(attrs))
    return field(1, name) + ticket + field(5, SEARCHING)


def member_msg(uid, attrs):
    """#126 TicketMember: 1 user, 2 attributes (#28)."""
    return field(1, f'tenants/{STATE.tenant_of(uid)}/users/{uid}') + field(2, encode_map(attrs))


GAMESYNC_HOST, GAMESYNC_PORT = 'gamesync.npln.nintendo.net', 443  # redirected to this server


def game_session_msg(s):
    """#124 GameSession: 1 name, 2 state, 3 int,
    4/5 bools, 6 string, 7 int, 8 the Gamesync host and 9 its port (each player connects there
    after a match, with TLS as gamesync.npln.nintendo.net unless the host is
    localhost), 10 created, 11 attributes (the caller's stage choice), 12 repeated messages
    (unknown; must stay empty: 32 random bytes made the game reject the session, 0321-0384)."""
    return (field(1, s['name']) + field(2, SESSION_ACTIVE) + field(8, GAMESYNC_HOST)
            + field(9, GAMESYNC_PORT) + field(10, timestamp(s['created']))
            + field(11, encode_map(s['attributes'])))


def user_session_name(s, uid):
    return f'{s["name"]}/userSessions/us-{uid[2:]}'


def member_entries(s):
    """One #125 per session member: 1 its TicketMember (#126), 2 its user session
    (tenants/{t}/gameSessions/{gs}/userSessions/{us}; the game aborts on
    anything else), 3 a string the Gamesync connection carries with the user session (likely
    its credential; a token here). The join step finds each local player's
    entry by user; without the entries it fails with "the user's connection
    info not found" (0321-3072)."""
    out = []
    for uid, tid in zip(s['members'], s['tickets']):
        t = STATE.tickets.get(tid)
        out.append(field(1, member_msg(uid, t['attributes'] if t else {}))
                   + field(2, user_session_name(s, uid)) + field(3, jwt_for(uid)))
    return out


def ticket_message(tid):
    """The ticket as TrackMatchmakingTicket sends it (#122: 5 state, 7 the game session once
    SUCCEEDED) and whether that state is final."""
    with STATE.lock:
        t = STATE.tickets[tid]
        name, body = TICKETS[tid]
        if t['bot']:
            body = (field(2, f'tenants/{t["tenant"]}/matchmakingConfigs/{t["config"]}')
                    + field(4, member_msg(t['uid'], t['attributes'])))
        msg = field(1, name) + body + field(5, t['status'])
        if t['session'] and t['status'] == SUCCEEDED:
            s = STATE.sessions[t['session']]
            msg += b''.join(field(6, e) for e in member_entries(s)) + field(7, game_session_msg(s))
        return msg, t['status'] in TERMINAL


@reply('TrackMatchmakingTicket')
def track_matchmaking_ticket(req, call):
    # request: 1 ticket name, 2 user. Stream messages are tickets (#122); the handler streams
    # later changes (Handler.service).
    tid = ticket_id(first_string(req, 1))
    if tid in TICKETS and tid in STATE.tickets:
        return ticket_message(tid)[0]
    return field(1, first_string(req, 1)) + field(5, SEARCHING)


@reply('CancelMatchmakingTicket')
def cancel_matchmaking_ticket(req, call):
    STATE.ticket_state(ticket_id(first_string(req, 1)), 'cancelled', CANCELLED)
    return b''


def session_for(req, call):
    """The game session a request names (…/gameSessions/<id>), else the caller's latest."""
    text = req.decode('latin-1')
    with STATE.lock:
        for sid, s in STATE.sessions.items():
            if f'/gameSessions/{sid}' in text:
                return s
        return next((s for s in reversed(list(STATE.sessions.values()))
                     if call['uid'] in s['members']), None)


@reply('JoinGameSession')
def join_game_session(req, call):
    # Request: 1 the game session, 3 the joiner as a TicketMember (Mario Kart World's attributes
    # carry its version). Reply #105: 1 members (#125), 2 the session.
    # Joining a room adds the caller to it (matchmade sessions have their members already), unless
    # the room was made on another version or build of the game (when both are known).
    s = session_for(req, call)
    joiner = next((member_version(m) for m in submsgs(decode(req), 3)), None)
    STATE.note_version(call['uid'], joiner)
    joiner = joiner or STATE.user_version(call['uid'])
    if s and s.get('room') and s.get('version') and joiner and joiner != s['version']:
        call['log'](ev='room-version-refused', session=s['id'], uid=call['uid'], room=s['version'],
                    joiner=joiner)
        raise RpcError(grpc.StatusCode.FAILED_PRECONDITION,
                       f'room {s["id"]} plays game version {s["version"]}, not {joiner}')
    if s and s.get('room') and not STATE.build_fits(s, call['uid']):
        call['log'](ev='room-build-refused', session=s['id'], uid=call['uid'],
                    joiner=STATE.users.get(call['uid'], {}).get('build'))
        raise RpcError(grpc.StatusCode.FAILED_PRECONDITION,
                       f'room {s["id"]} plays another build of the game')
    if s and s.get('room') and not add_room_member(s, call['uid'], call.get('log')):
        raise RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED,
                       f'room {s["id"]} is {"closed" if s.get("closed") else "full"}')
    return b''.join(field(1, e) for e in member_entries(s)) + field(2, game_session_msg(s)) if s else b''


# --- rooms: the Hideout's Online Play desk ------------------------------------------------------
#
# "Create a Room" asks for a game session directly. CreateGameSessionCreationTicket: 1 parent,
# 2 a ticket (M118: 2 config, "spiral_match_config"; 3 the host as a TicketMember; 6 a game
# session template whose 11 holds the room's attributes: player_level, session_keyword (the
# passcode, empty without one), user_name, style, game_mode, difficulty, uid). The client parses
# reply field 1 as tenants/{t}/gameSessionCreationTickets/{id} (an empty reply makes it
# panic), then reads TrackGameSessionCreationTicket until the stream ends: M118 field 4 state (2 created; 3 cancelled, 4 timed out, 5 failed), 5 the
# member entries (#125), 6 the game session.
CREATION_TICKETS = {}  # ticket id -> (ticket name, game session id)
ROOM_CREATED, ROOM_FAILED = 2, 5
KEYWORD_SEARCH_SECONDS = 10  # a keyword search with no matching room times out after this


@reply('CreateGameSessionCreationTicket')
def create_game_session_creation_ticket(req, call):
    decoded = decode(first_bytes(req, 2)) or []
    config = next((v for n, wt, v in decoded if n == 2 and isinstance(v, str)), '')
    attrs = {}
    for template in submsgs(decoded, 6):
        for room_attrs in submsgs(template, 11):
            attrs = attributes(room_attrs)
    # The host's version: from their TicketMember (3), the room's attributes, or (Raiders, whose
    # rooms carry none) the version they last showed
    shown = next((member_version(m) for m in submsgs(decoded, 3)), None) or version_of(attrs)
    STATE.note_version(call['uid'], shown)
    version = shown or STATE.user_version(call['uid'])
    with STATE.lock:
        tenant = call['meta'].get('npln-tenant-id') or STATE.tenant_of(call['uid'])
        sid = f'gs-{len(STATE.sessions) + 1:04d}'
        tid = f'gct-{len(CREATION_TICKETS) + 1:04d}'
        name = f'tenants/{tenant}/gameSessionCreationTickets/{tid}'
        s = {'id': sid, 'name': f'tenants/{tenant}/gameSessions/{sid}', 'tenant': tenant, 'created': time.time(),
             'host': call['uid'], 'members': [call['uid']], 'tickets': [None], 'next_user_session': 1,
             'config': config.rsplit('/', 1)[-1], 'attributes': attrs, 'room': True, 'version': version,
             'updated': {}, 'watchers': [], 'vars': {}, 'deferments': {}}
        s['docs'] = session_docs(s)
        STATE.sessions[sid] = s
        STATE.joined[call['uid']] = sid
        CREATION_TICKETS[tid] = (name, sid)
    return field(1, name) + field(2, config) + field(4, 1)


@reply('TrackGameSessionCreationTicket')
def track_game_session_creation_ticket(req, call):
    # One update with the room, then the stream ends (Handler.service).
    name = first_string(req, 1)
    entry = CREATION_TICKETS.get(ticket_id(name))
    if entry is None:
        return field(1, name) + field(4, ROOM_FAILED)
    name, sid = entry
    with STATE.lock:
        s = STATE.sessions[sid]
        return (field(1, name) + field(2, f'tenants/{s["tenant"]}/matchmakingConfigs/{s["config"]}')
                + field(4, ROOM_CREATED) + b''.join(field(5, e) for e in member_entries(s))
                + field(6, game_session_msg(s)))


@reply('CancelGameSessionCreationTicket')
def cancel_game_session_creation_ticket(req, call):
    return b''


def add_room_member(s, uid, log=None, limit=None):
    """A player joins a room (or, with a limit, a matchmade session by backfill): their user
    session document goes to everyone already in it. False when it is full."""
    with STATE.lock:
        if uid in s['members']:
            STATE.joined[uid] = s['id']
            return True
        if len(s['members']) >= (limit or ACCOUNTS.room_size(s.get('tenant'))) or s.get('closed'):
            return False
        STATE.joined[uid] = s['id']
        s['members'].append(uid)
        s['tickets'].append(None)
        n = s['next_user_session'] = s.get('next_user_session', len(s['members']) - 1) + 1
        path = f'__us/us-{uid[2:]}'
        s['docs'][path] = {'uid': uid, 'st': 2, 'att': {}, 'ltc': '', 'tn': s['tenant'],
                           'ussid': n, 'ucsid': n}
        s['updated'][path] = time.time()
    push_change(s, path, log or (lambda **kw: None), DOC_ADDED)
    return True


def leave_room(s, uid, log):
    """A member's Gamesync stream closed: they are out of the room (the game deletes its
    participation docs itself); the others see their user session go."""
    with STATE.lock:
        if uid not in s['members']:
            return
        i = s['members'].index(uid)
        del s['members'][i], s['tickets'][i]
        path = f'__us/us-{uid[2:]}'
        s['docs'].pop(path, None)
        if STATE.joined.get(uid) == s['id']:
            del STATE.joined[uid]
    log(ev='room-left', session=s['id'], uid=uid, members=list(s['members']))
    push_change(s, path, log, DOC_REMOVED)


@reply('GetGameSession')
def get_game_session(req, call):
    s = session_for(req, call)
    return game_session_msg(s) if s else b''


# --- room IDs: Mario Kart World's "Search by Room ID" -------------------------------------------
#
# A room's ID is a short alias of its game session (GameSessionService Create/Get
# GameSessionShortAlias). The room menu's View Room Info asks for one: Create's request is {1
# "tenants/current", 2 {2 the game session}}. The game reads the reply as {1
# "tenants/{t}/GameSessionShortAliases/{alias}" (capital G; the game aborts on any other
# form), 2 the game session, 3 Timestamp expiry}, and shows the alias as "123
# 456". Search by Room ID takes exactly 6 characters (its keyboard allows download-code
# characters), so the IDs here are 6 digits. Get's request is {1 "tenants/current/
# GameSessionShortAliases/<typed>"}. Both calls log their request.
SHORT_ALIASES = {}  # alias (6 digits) -> game session id
ALIAS_SECONDS = 24 * 3600


def short_alias_msg(tenant, alias, sid):
    return (field(1, f'tenants/{tenant}/GameSessionShortAliases/{alias}')
            + field(2, f'tenants/{tenant}/gameSessions/{sid}')
            + field(3, timestamp(time.time() + ALIAS_SECONDS)))


@reply('CreateGameSessionShortAlias')
def create_game_session_short_alias(req, call):
    s = session_for(req, call)
    call['log'](ev='short-alias-request', kind='create', uid=call['uid'], req=decode(req), req_hex=req.hex())
    if s is None:
        raise RpcError(grpc.StatusCode.NOT_FOUND, 'no game session to name')
    with STATE.lock:
        for a, sid in list(SHORT_ALIASES.items()):  # IDs of rooms that are gone are free again
            if STATE.sessions.get(sid, {'closed': True}).get('closed'):
                del SHORT_ALIASES[a]
        alias = next((a for a, sid in SHORT_ALIASES.items() if sid == s['id']), None)
        while alias is None or (alias in SHORT_ALIASES and SHORT_ALIASES[alias] != s['id']):
            alias = f'{secrets.randbelow(10**6):06d}'
        SHORT_ALIASES[alias] = s['id']
    call['log'](ev='short-alias', session=s['id'], alias=alias)
    return short_alias_msg(s.get('tenant') or STATE.tenant, alias, s['id'])


@reply('GetGameSessionShortAlias')
def get_game_session_short_alias(req, call):
    call['log'](ev='short-alias-request', kind='get', uid=call['uid'], req=decode(req), req_hex=req.hex())
    alias = re.sub(r'\s', '', first_string(req, 1).rsplit('/', 1)[-1])
    with STATE.lock:
        sid = SHORT_ALIASES.get(alias)
        s = STATE.sessions.get(sid) if sid else None
    if s is None or s.get('closed'):
        raise RpcError(grpc.StatusCode.NOT_FOUND, 'no room with that ID')
    return short_alias_msg(s.get('tenant') or STATE.tenant, alias, sid)


# --- friends: friend codes, friend lists and presences -----------------------------------------
#
# The emulator sends its online identity as the NSA ID token (acc LoadIdTokenCache), which the
# game forwards at login: "droplet-online-v1;code=<12 digits>;secret=<hex>;friends=<code>,<code>"
# (builds from before the rename to Droplet say "ounce2dn-online-v1").
# The server binds a code to the first secret that uses it, so nobody else can sign in with
# your code. Two players are friends once each has added the other's code.
#
# SubscribeFriendUsers streams #39 {1 friends, 2 a second list (not used here), 3 Duration}.
# Each friend (#38): 1 its id as a hex string (strtoull, base 16; 0 makes the game drop its
# presences), 2 its users (#54, field 2 "tenants/{t}/users/{u}"; the game
# aborts on any other form). The game keeps {user id, friend id}.
# SubscribePresences streams #50 {1: #48 {1 presences, 2 a string}}. Each presence (#57): 1
# "tenants/{t}/users/{u}/presence", 2 state (1 online with attributes,
# 2 offline, 3 another state), 3 attribute entries {1 key, 2 MapValue}.
# The game shows a friend's room from PlayerName, GameStatus, SessionId, MaxParticipants,
# CurrentParticipants, UsePassword, Level, Style and Difficulty, and joins
# it by SessionId (JoinGameSession).

IDENTITY_PREFIXES = ('droplet-online-v1', 'ounce2dn-online-v1')
PRESENCE_ONLINE, PRESENCE_OFFLINE = 1, 2


format_code = npln_accounts.format_code  # 123456789012 -> "SW-1234-5678-9012"


def code_uid(code):
    """A friend code's fixed NPLN user id."""
    return f'u-{int(code):016x}'


def uid_code(uid):
    """The friend code inside a friend code's user id (code_uid), or None."""
    try:
        code = f'{int(uid[2:], 16):012d}'
    except ValueError:
        return None
    return code if len(code) == 12 else None


def identity_build(token):
    """The game build an identity token names (Droplet sends the main module's build ID in hex: the
    game's exact version), or None (older emulators send none)."""
    parts = token.split(';')
    if not parts or parts[0] not in IDENTITY_PREFIXES:
        return None
    kv = dict(p.split('=', 1) for p in parts[1:] if '=' in p)
    build = kv.get('build', '').lower()
    return build if re.fullmatch(r'[0-9a-f]{8,64}', build) else None


def parse_identity(token):
    """(code, secret, [codes added], [codes removed]) from the emulator's identity token, or None.
    Emulators from before the account database send no "removed"."""
    parts = token.split(';')
    if not parts or parts[0] not in IDENTITY_PREFIXES:
        return None
    kv = dict(p.split('=', 1) for p in parts[1:] if '=' in p)
    code, secret = kv.get('code', ''), kv.get('secret', '')
    def codes(key):
        return [c for c in kv.get(key, '').split(',') if len(c) == 12 and c.isdigit()]
    if len(code) != 12 or not code.isdigit() or int(code) == 0 or len(secret) < 16:
        return None
    return code, secret, codes('friends'), codes('removed')


ACCOUNTS = None  # set in main(): npln_accounts.Accounts


def caller_code(call):
    with STATE.lock:
        return STATE.users.get(call['uid'], {}).get('code')


def user_name(uid, tenant=None):
    return f'tenants/{tenant or STATE.tenant_of(uid)}/users/{uid}'


def friend_users_msg(call):
    """#39: the caller's friends, named in the caller's own tenant (game)."""
    code = caller_code(call)
    if not code:
        return b''
    tenant = STATE.tenant_of(call['uid'])
    return b''.join(field(1, field(1, f'{int(f):x}') + field(2, field(2, user_name(code_uid(f), tenant))))
                    for f in ACCOUNTS.friends_of(code))


def presence_msg(uid, now, tenant=None):
    """A friend's presence as a player in `tenant` sees it: online only while playing the same
    game, since each game reads its own presence attributes."""
    with STATE.lock:
        u = STATE.users.get(uid)
        online = (u is not None and STATE._online(u, now)
                  and (tenant is None or STATE.user_tenant.get(uid) == tenant))
        attrs = dict(u['presence']) if online else {}
    msg = field(1, user_name(uid, tenant) + '/presence') + field(2, PRESENCE_ONLINE if online else PRESENCE_OFFLINE)
    return msg + b''.join(field(3, field(1, k) + field(2, encode_value(v))) for k, v in attrs.items())


def presences_msg(call):
    """#50 {1: #48}: the presences of the caller's friends."""
    code = caller_code(call)
    if not code:
        return b''
    now, tenant = time.time(), STATE.tenant_of(call['uid'])
    body = b''.join(field(1, presence_msg(code_uid(f), now, tenant)) for f in ACCOUNTS.friends_of(code))
    return field(1, body + field(2, 'p'))


@reply('SubscribeFriendUsers')
def subscribe_friend_users(req, call):
    return friend_users_msg(call)


@reply('SubscribePresences')
def subscribe_presences(req, call):
    return presences_msg(call)


# Server streams that follow a changing view: the stream handler re-sends the message whenever
# it changes (see Handler.service).
FOLLOW = {'SubscribeFriendUsers': friend_users_msg, 'SubscribePresences': presences_msg}


class RpcError(Exception):
    """Raised by a reply function to end the call with a gRPC status."""
    def __init__(self, code, details):
        super().__init__(details)
        self.code, self.details = code, details


# --- Gamesync: one document store per game session ---------------------------------------------
#
# After a match each player connects to the session's Gamesync host, gets a token
# (IssueToken) and keeps a user session open (KeepUserSession), a Firestore-like listen stream.
# The game's P2P plugin reads the session from documents under "docs/":
#   __us/<user session>  each member: uid, st, att, ltc, tn, ussid, ucsid;
#                        the game waits for its own before anything else
#   __gs/f               fixed data for Pia: gsid, addr, p, mcn, maxu, rs
#   __gs/m               flags the members write: cp, ip, ebf (pw, prp too)

# #92 field 2: 1 settles the client's add-listener future; 2 "current" (every
# document of the target sent: the client runs the listener's second callback, which the
# P2P plugin needs before it reads mailboxes or counts participants); 4 error. 3 isn't read.
TARGET_ADDED, TARGET_CURRENT, TARGET_REMOVED, TARGET_ERROR = 1, 2, 3, 4
DOC_ADDED, DOC_MODIFIED, DOC_REMOVED = 1, 2, 3        # #91 field 2

GAMESYNC_TOKENS = {}  # Gamesync bearer token -> (game session id, uid)


def session_docs(s):
    """A new game session's documents. Values the game doesn't explain yet are guesses."""
    docs = {
        '__gs/f': {'gsid': s['id'], 'addr': '', 'p': 0, 'mcn': s.get('config', ''),
                   'maxu': s.get('max_players', 4),
                   'rs': os.urandom(16)},
        # Mutable session data (cp, ip, pw, ebf, bfmin, bfmax, prp). cp is
        # "can participate": a joiner whose copy says false raises AlreadySessionClosed
        # (IsClosed = !cp). The host writes cp and ip together when it opens or
        # closes the room, so a new session starts open.
        '__gs/m': {'cp': True, 'ip': True, 'ebf': False},
    }
    for i, uid in enumerate(s['members']):
        docs[f'__us/us-{uid[2:]}'] = {'uid': uid, 'st': 2, 'att': {}, 'ltc': '',
                                      'tn': s.get('tenant') or STATE.tenant,
                                      'ussid': i + 1, 'ucsid': i + 1}
    return docs


def gamesync_session(call):
    """(game session, uid) for the Gamesync token a call carries, or (None, None)."""
    token = call['meta'].get('authorization', '').removeprefix('bearer ').strip()
    with STATE.lock:
        sid, uid = GAMESYNC_TOKENS.get(token, (None, None))
        return STATE.sessions.get(sid), uid


@reply('Gamesync/IssueToken')
@reply('Gamesync/RefreshToken')
def gamesync_token(req, call):
    # Request #61 {1 the player's user session ("userSessions/us-..."), 2 the credential from
    # its #125 entry}. Reply #62/#64: 1 #93 {1 the user session, parsed as
    # "userSessions/{id}" (the game aborts otherwise), 2 and 3 two
    # tokens kept as strings, 4 lifetime}. An empty reply also makes the game abort.
    user_session = first_string(req, 1)
    token = jwt_for(user_session)
    uid = 'u-' + user_session.rsplit('/', 1)[-1].removeprefix('us-')
    with STATE.lock:
        s = STATE.sessions.get(STATE.joined.get(uid, ''))
        if s is None or uid not in s['members']:
            s = next((s for s in reversed(list(STATE.sessions.values())) if uid in s['members']), None)
        if s:
            GAMESYNC_TOKENS[token] = (s['id'], uid)
    return field(1, field(1, user_session) + field(2, token) + field(3, token)
                 + field(4, timestamp(3600)))


def listen_targets(req):
    """What a KeepUserSession request (#68) changes: field 3 adds a target (#70 {1: #74 {1 name,
    2: #89 {1 document paths} | 3: #90 {1 collection}}}), field 4 removes one (#71 {1 name}).
    Returns ([(name, [paths], collection)], [removed names])."""
    added, removed = [], []
    for n, wt, v in decode(req) or []:
        if n == 4 and isinstance(v, dict):
            removed += [x[2] for x in v.get('msg', []) if x[0] == 1 and isinstance(x[2], str)]
        if n != 3 or not isinstance(v, dict):
            continue
        for n2, wt2, target in v.get('msg', []):
            if n2 != 1 or not isinstance(target, dict):
                continue
            name, docs, collection = '', [], None
            for n3, wt3, v3 in target.get('msg', []):
                if n3 == 1 and isinstance(v3, str):
                    name = v3
                elif n3 == 2 and isinstance(v3, dict):
                    docs += [x[2] for x in v3.get('msg', []) if x[0] == 1 and isinstance(x[2], str)]
                elif n3 == 2 and isinstance(v3, str):  # a one-path list decodes as a string
                    docs.append(v3)
                elif n3 == 3:
                    collection = (v3 if isinstance(v3, str) else
                                  next((x[2] for x in v3.get('msg', []) if x[0] == 1), ''))
            added.append((name, docs, collection))
    return added, removed


def document_msg(s, path):
    """#72 Document: 1 "docs/<path>" (the client strips "docs/"), 2 fields (#28),
    3/4 created and updated."""
    return (field(1, 'docs/' + path) + field(2, encode_map(s['docs'][path]))
            + field(3, timestamp(s['created'])) + field(4, timestamp(s['updated'].get(path, s['created']))))


def doc_change(target, s, path, kind=DOC_ADDED):
    """#69 field 2 (#91): 1 the target as a bare number (the client runs strtoul on it and
    ignores a change for target 0), 2 the change (1 added, 2 modified, 3 removed), 3 the
    document (only its name when removed)."""
    if path not in s['docs']:
        return field(2, field(1, target) + field(2, DOC_REMOVED) + field(3, field(1, 'docs/' + path)))
    return field(2, field(1, target) + field(2, kind) + field(3, document_msg(s, path)))


def target_paths(s, docs, collection):
    if collection is not None:
        prefix = collection.removeprefix('docs/').rstrip('/') + '/'
        return [p for p in s['docs'] if p.startswith(prefix)]
    return [p.removeprefix('docs/') for p in docs]


@reply('Gamesync/KeepUserSession')
def keep_user_session(req, call):
    # Each reply #69 is one of: 1 a string (not used by the reader), 2 a document change (#91,
    # see doc_change), 3 a target status #92 {1 target number, 2 status (TARGET_*), 3
    # rpc.Status}. A new target gets "added", its documents, then "current".
    s, uid = gamesync_session(call)
    targets = call.setdefault('targets', {})
    if s is not None and 'push' in call and not any(w is call for w in s['watchers']):
        s['watchers'].append(call)
    replies = []
    added, removed = listen_targets(req)
    for name, docs, collection in added:
        target = name.rsplit('/', 1)[-1]
        paths = target_paths(s, docs, collection) if s else []
        targets[target] = (docs, collection)
        replies.append(field(3, field(1, target) + field(2, TARGET_ADDED)))
        replies += [doc_change(target, s, p) for p in paths]
        if s is not None and not participants_complete(s, collection):
            # Pia picks its host from the participants it knows when this target goes current:
            # hold "current" until every member has joined, or a game that
            # joins first sees only itself and hosts too (pair25).
            call.setdefault('pending_current', {})[target] = collection
        else:
            replies.append(field(3, field(1, target) + field(2, TARGET_CURRENT)))
    for name in removed:
        target = name.rsplit('/', 1)[-1]
        targets.pop(target, None)
        replies.append(field(3, field(1, target) + field(2, TARGET_REMOVED)))
    return replies


def covers(path, docs, collection):
    """Whether a listen target covers a document path, one just deleted included."""
    if collection is not None:
        return path.startswith(collection.removeprefix('docs/').rstrip('/') + '/')
    return path in [p.removeprefix('docs/') for p in docs]


def push_change(s, path, log, kind=DOC_MODIFIED):
    """Send a document's new state (or its removal) to every user session in the game session
    watching it. A player leaving deletes its participation record, and the others only learn
    they left from that removal."""
    for w in list(s['watchers']):
        for target, (docs, collection) in list(w.get('targets', {}).items()):
            if covers(path, docs, collection):
                w['push'](doc_change(target, s, path, kind))
                log(ev='gamesync-push', session=s['id'], to=gamesync_session(w)[1], doc=path)


@reply('Gamesync/GetDocument')
def get_document(req, call):
    # Request #65 {1 "docs/<path>", 2 FieldMask, 3 transaction}; reply the #72 Document.
    s, uid = gamesync_session(call)
    path = first_string(req, 1).removeprefix('docs/')
    if s is None or path not in s['docs']:
        raise RpcError(grpc.StatusCode.NOT_FOUND, f'document {path} not found')
    return document_msg(s, path)


def submsgs(fields, num):
    """The decoded sub-messages in field num."""
    return [v['msg'] for n, wt, v in fields or [] if n == num and isinstance(v, dict) and 'msg' in v]


def substr(fields, num):
    return next((v for n, wt, v in fields or [] if n == num and isinstance(v, str)), '')


MISSING = object()


def field_path(p):
    """A Firestore-style field path, "`prp`.`user_name`" or "a.b", as its list of keys."""
    keys, cur, quoted = [], '', False
    for ch in p:
        if ch == '`':
            quoted = not quoted
        elif ch == '.' and not quoted:
            keys.append(cur)
            cur = ''
        else:
            cur += ch
    return keys + [cur]


def apply_write(s, w, uid=None):
    """Apply one write (#75, a oneof) to the session's documents. Returns (changed path, #79
    write result). The kinds follow Firestore's Write and FieldTransform, which Gamesync
    copies: 1 update #80 {1 #72, 2 field mask ("*" = all), 3 precondition}, 2 set #81 {1 #72},
    3 transform #82 {1 name, 2 #85...}, 4 delete #83 {1 name, 2 precondition}."""
    for n, wt, body in w:
        body = body['msg'] if isinstance(body, dict) and 'msg' in body else []
        if n in (1, 2):
            doc = (submsgs(body, 1) or [[]])[0]
            path = substr(doc, 1).removeprefix('docs/')
            fields = attributes((submsgs(doc, 2) or [[]])[0])
            mask = ([p for m in submsgs(body, 2) for k, wt2, p in m
                     if k == 1 and isinstance(p, str)] if n == 1 else ['*'])
            if not mask or '*' in mask or path not in s['docs']:
                s['docs'][path] = fields
            else:
                # Paths can be nested: the host's room properties come as "`prp`.`user_name`" ...
                for p in mask:
                    keys = field_path(p)
                    src = fields
                    for k in keys:
                        src = src.get(k, MISSING) if isinstance(src, dict) else MISSING
                    dst = s['docs'][path]
                    for k in keys[:-1]:
                        if not isinstance(dst.get(k), dict):
                            dst[k] = {}
                        dst = dst[k]
                    if src is MISSING:
                        dst.pop(keys[-1], None)
                    else:
                        dst[keys[-1]] = src
            # case 1: "written" (the client stamps the entry with the commit time)
            return path, field(1, b'')
        if n == 3:
            path = substr(body, 1).removeprefix('docs/')
            doc = s['docs'].setdefault(path, {})
            for t in submsgs(body, 2):
                key = substr(t, 1).strip('`')
                for m, wt2, val in t:
                    value = map_value(val['msg']) if isinstance(val, dict) and 'msg' in val else val
                    if m in (3, 4, 5) and not isinstance(value, int):
                        continue
                    if m == 2:      # server value (REQUEST_TIME)
                        doc[key] = int(time.time())
                    elif m == 3:    # increment
                        doc[key] = (doc.get(key) or 0) + value
                    elif m == 4:    # maximum
                        doc[key] = max(doc.get(key) or 0, value)
                    elif m == 5:    # minimum
                        doc[key] = min(doc.get(key) or 0, value)
                    elif m == 6:    # load a session counter (e.g. __upcsidn)
                        doc[key] = s['vars'].get(value, 0)
                    elif m == 7:    # store it back
                        if key == 'upcsid' and uid in s['members']:
                            # Pia makes the lowest station id the host; first-come let the helper
                            # host a Call for Help raid, and the caller then backed out (pair21).
                            # Hand them out in member order instead: the caller (member 0) hosts.
                            # Room members come and go, so a room numbers them as they join (the
                            # creator first) and never reuses a number.
                            if s.get('room'):
                                ids = s.setdefault('upcsids', {})
                                doc[key] = ids.setdefault(uid, 20001 + len(ids))
                            else:
                                doc[key] = 20001 + s['members'].index(uid)
                        s['vars'][value] = max(s['vars'].get(value) or 0, doc.get(key) or 0)
            # case 2: the transformed values, in the order of the client's field paths
            # (the client pairs them by index, so one value per field, never more)
            keys = list(dict.fromkeys(substr(t, 1).strip('`') for t in submsgs(body, 2)))
            return path, field(2, b''.join(field(1, encode_value(doc.get(k))) for k in keys))
        if n == 4:
            path = substr(body, 1).removeprefix('docs/')
            s['docs'].pop(path, None)
            return path, field(1, b'')
    return None, b''


def run_writes(s, writes, log, uid):
    """Apply decoded writes (#75s), then push every changed document to its watchers. In other
    games' sessions a write that leaves its document as it was isn't pushed: Mario Kart World
    writes its `__gs/m` flags again whenever that document changes, so echoing those unchanged
    writes made a loop of ~40 writes a second (worldnet5)."""
    changed, results = [], []
    quiet = not is_raiders(s)
    with STATE.lock:
        for w in writes:
            before = copy.deepcopy(s['docs']) if quiet else {}
            path, result = apply_write(s, w, uid)
            results.append(result)
            if path and not (quiet and s['docs'].get(path, MISSING) == before.get(path, MISSING)):
                changed.append(path)
                s['updated'][path] = time.time()
    log(ev='gamesync-write', uid=uid, session=s['id'],
        docs={p: {k: (v.hex() if isinstance(v, bytes) else v) for k, v in s['docs'][p].items()}
              if p in s['docs'] else None for p in changed})
    for path in dict.fromkeys(changed):
        push_change(s, path, log)
    release_current(s, log)
    referee(s, changed, log)
    return results


RACE_GRACE = 120   # seconds after a race's first finisher before it is closed without everyone
RATING_K = 24.0    # Elo step for the ratings a closed race hands out


def referee(s, changed, log):
    """Mario Kart World's race referee. As a race starts every player writes
    `@@RefereeRequest/<ussid>/Request/<request id>` and waits up to 36 s for a
    `@@RefereeResult/<uuid>` document whose `request_id` is that request and whose `status` is 0;
    without one the race ends in a communication error (worldvote1, worldvote2). The document's
    name is the race's UUID in its 36-character form, which the players then
    use for `@@RefereeReport/<uuid>/User/<ussid>`: `result` (the players in the order that game
    saw them finish), `result_order` (their ranks; ties share one) and `retired`.
    Players in `result` are indices, the keys of the request's `ussid_map`
    ({"0": 1, "1": 2}: index -> ussid; battle4 reported [0, 1]). The result's `result` is the
    official order of those indices: each listed player is placed at that rank.
    `status` 1 closes the race: the game waits for it at the end (a battle sat
    at 0:00 until one game gave up after ~29 s, battle4) and moves on once everyone still in it
    is placed. It also reads `user_session_count`, `ratings` and `coefficients` (ussid ->
    double, stored as the players' floats) and `inet_param`; 2 and above abort it."""
    for path in dict.fromkeys(changed):
        parts = path.split('/')
        if len(parts) == 4 and parts[0] == '@@RefereeRequest' and parts[2] == 'Request':
            request = s['docs'].get(path) or {}
            with STATE.lock:
                races = s.setdefault('referee', {})
                if parts[3] in races:
                    continue
                users = list(request.get('users') or [])
                ussids = {int(k): v for k, v in (request.get('ussid_map') or {}).items()
                          if str(k).isdigit() and isinstance(v, int)}
                # Knockout Tour: one request per section, "Surv_<course>_1_0_<section>_<hash>",
                # each with the first rank knocked out after it (option.dropout_rank).
                section = re.fullmatch(r'(Surv_\d+_\d+_\d+)_(\d+)_(\w+)', parts[3])
                h = secrets.token_hex(16)
                race = races[parts[3]] = {
                    'uuid': f'{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}',
                    'users': users, 'mode': request.get('mode'),
                    'ussids': ussids or dict(enumerate(users)),
                    'tour': f'{section[1]}_{section[3]}' if section else None,
                    'section': int(section[2]) if section else 0,
                    'dropout': (request.get('option') or {}).get('dropout_rank', -1),
                    'reports': {}, 'order': [], 'ranks': {}, 'retired': [], 'first_report': None,
                    'closed': False, 'final': []}
                result_path = '@@RefereeResult/' + race['uuid']
                s['docs'][result_path] = {'request_id': parts[3], 'status': 0, 'result': [],
                                          'user_session_count': len(race['users'])}
                s['updated'][result_path] = time.time()
            log(ev='referee-accept', session=s['id'], request=parts[3], uuid=race['uuid'],
                course_id=request.get('course_id'), mode=request.get('mode'), users=race['users'])
            push_change(s, result_path, log)
        elif len(parts) == 4 and parts[0] == '@@RefereeReport' and parts[2] == 'User':
            report = s['docs'].get(path) or {}
            with STATE.lock:
                race = next((r for r in s.get('referee', {}).values() if r['uuid'] == parts[1]),
                            None)
                if race is None or race['closed']:
                    continue
                race['reports'][parts[3]] = report
                race['first_report'] = race['first_report'] or time.time()
                result, ranks = report.get('result') or [], report.get('result_order') or []
                for i, player in enumerate(result):
                    if isinstance(player, int) and player not in race['order']:
                        race['order'].append(player)
                    if isinstance(player, int) and i < len(ranks) and isinstance(ranks[i], int):
                        race['ranks'][player] = ranks[i]
                index = {u: i for i, u in race['ussids'].items()}
                if report.get('retired') and parts[3].isdigit() and int(parts[3]) in index:
                    if index[int(parts[3])] not in race['retired']:
                        race['retired'].append(index[int(parts[3])])
            log(ev='referee-report', session=s['id'], uuid=parts[1], ussid=parts[3], report=report)
            judge(s, race, log)
    for race in list(s.get('referee', {}).values()):
        if not race['closed'] and race['first_report'] and time.time() - race['first_report'] > RACE_GRACE:
            judge(s, race, log, force=True)


def racing(s, ussid):
    """Whether the player with this ussid is still in the game session: their user session
    document is there and so is their participation record (`__pgn/<group>/__pus/us-<id>`), which
    their deferred writes delete when they leave."""
    uid = next((d.get('uid') for p, d in s['docs'].items()
                if p.startswith('__us/') and isinstance(d, dict) and d.get('ussid') == ussid), None)
    if uid is None:
        return False
    return any(p.startswith('__pgn/') and p.endswith('/__pus/us-' + uid[2:]) for p in s['docs'])


def knocked_out(s, race):
    """Knockout Tour: the players an earlier, closed section of the same tour put out (placed at
    or past its dropout_rank, 1-based). They spectate and never reach this section's line, so they
    must not hold it open."""
    if not race.get('tour'):
        return set()
    out = set()
    for r in s.get('referee', {}).values():
        if r.get('tour') == race['tour'] and r['section'] < race['section'] and r['closed'] and r['dropout'] > 0:
            out.update(i for n, i in enumerate(r['final'], 1) if n >= r['dropout'])
    return out


def rating_of(s, ussid):
    """(rating, coefficient) from the player's PlayerStatic document: floats sent as their bits
    in ints (rating_point_raw_value 1153138688 is 1500.0)."""
    doc = s['docs'].get(f'PlayerStatic/{ussid}') or {}

    def as_float(v, default):
        if not isinstance(v, int) or isinstance(v, bool):
            return default
        value = struct.unpack('<f', struct.pack('<I', v & 0xffffffff))[0]
        return value if math.isfinite(value) else default
    return (as_float(doc.get('rating_point_raw_value'), 1500.0),
            as_float(doc.get('rating_point_coefficient'), 0.0))


def judge(s, race, log, force=False):
    """Publish a race's official order so far (player indices), and close it (status 1, new
    ratings by ussid) once every player still in the session has finished or retired, or when
    forced."""
    with STATE.lock:
        result_path = '@@RefereeResult/' + race['uuid']
        doc = s['docs'].get(result_path)
        if doc is None or race['closed']:
            return
        ussids = race['ussids']
        present = [i for i, u in ussids.items() if racing(s, u) and i not in knocked_out(s, race)]
        placed = race['order'] + [i for i in race['retired'] if i not in race['order']]
        new = dict(doc, result=list(race['order']))
        if force or all(i in placed for i in present):
            order = placed + [i for i in ussids if i not in placed]
            rank = {i: race['ranks'].get(i, n) for n, i in enumerate(order)}
            ratings = {i: rating_of(s, ussids[i]) for i in order if i in ussids}
            delta = dict.fromkeys(ratings, 0.0)
            players = list(ratings)
            for n, a in enumerate(players):
                for b in players[n + 1:]:
                    expected = 1 / (1 + 10 ** ((ratings[b][0] - ratings[a][0]) / 400))
                    score = 1.0 if rank[a] < rank[b] else 0.5 if rank[a] == rank[b] else 0.0
                    step = RATING_K * (score - expected) / max(len(players) - 1, 1)
                    delta[a] += step
                    delta[b] -= step
            new.update(status=1, result=order, user_session_count=len(present),
                       ratings={str(ussids[i]): Fixed64.double(ratings[i][0] + delta[i]) for i in ratings},
                       coefficients={str(ussids[i]): Fixed64.double(ratings[i][1]) for i in ratings})
            race['closed'] = True
            race['final'] = order
        if new == doc:
            return
        s['docs'][result_path] = new
        s['updated'][result_path] = time.time()
    log(ev='referee-close' if race['closed'] else 'referee-order', session=s['id'],
        uuid=race['uuid'], result=new['result'], present=present, forced=force,
        ratings={k: round(struct.unpack('<d', struct.pack('<Q', v))[0], 2)
                 for k, v in new.get('ratings', {}).items()})
    push_change(s, result_path, log)


def participants_complete(s, collection):
    """False while a participants query (`__pgn/<group>/__pus`) lacks some member's record."""
    if not collection or not collection.rstrip('/').endswith('/__pus'):
        return True
    prefix = collection.removeprefix('docs/').rstrip('/') + '/us-'
    return all(prefix + uid[2:] in s['docs'] for uid in s['members'])


def release_current(s, log):
    """Send the held-back "current" once every member's participation record exists."""
    for w in list(s['watchers']):
        for target, collection in list(w.get('pending_current', {}).items()):
            if participants_complete(s, collection):
                del w['pending_current'][target]
                w['push'](field(3, field(1, target) + field(2, TARGET_CURRENT)))
                log(ev='gamesync-current', session=s['id'], to=gamesync_session(w)[1], target=target)


@reply('Gamesync/WriteDocuments')
def write_documents(req, call):
    # Request: 1 writes (#75), 2 deferment changes (#86 {1 set: #87 {1 #73 {1
    # "userSessions/current/deferments/<path>", 2 writes}} | 2 delete: #88 {1 name}}), writes
    # the server runs when this user session ends (the game registers "delete my document").
    # Reply #67 {1 one #79 result per write, 2 commit time}; the game reads its allocated
    # upcsid from a transform's result.
    s, uid = gamesync_session(call)
    if s is None:
        raise RpcError(grpc.StatusCode.FAILED_PRECONDITION, 'no game session for this token')
    decoded = decode(req) or []
    results = run_writes(s, submsgs(decoded, 1), call['log'], uid)
    with STATE.lock:
        mine = s['deferments'].setdefault(uid, {})
        for op in submsgs(decoded, 2):
            for d in submsgs(op, 1):
                for item in submsgs(d, 1):
                    mine[substr(item, 1)] = submsgs(item, 2)
            for d in submsgs(op, 2):
                mine.pop(substr(d, 1), None)
    return b''.join(field(1, r) for r in results) + field(2, timestamp())


def end_user_session(call, log):
    """A KeepUserSession stream closed: run that player's deferred writes."""
    s, uid = gamesync_session(call)
    if s is None:
        return
    with STATE.lock:
        deferred = s['deferments'].pop(uid, {})
    for name, writes in deferred.items():
        log(ev='gamesync-deferred', uid=uid, session=s['id'], name=name)
        run_writes(s, writes, log, uid)
    if s.get('room'):
        leave_room(s, uid, log)


STUN_PORT, RELAY_PORT = 3478, 3479
# Pia resolves these names (a failed lookup is its error 0x848a, report code 11).
# With the online redirect on, the emulator resolves nothing but the redirected Nintendo names, not
# even "127.0.0.1", so the ICE set names hosts the redirect sends to this server.
STUN_NAME, RELAY_NAME = 'stun.npln.nintendo.net', 'relay.npln.nintendo.net'
ICE_UDP = 1  # ICE protocol enum: 1 UDP, 2 TCP, 3 TLS


@reply('AllocateIceServerSet')
def allocate_ice_server_set(req, call):
    # Reply #130: 1 name, 2 a STUN-like server #131 {1 host, 2 port,
    # 3 protocol}, 3 relay servers #132 {1 host, 2 port, 3 protocol, 4 username,
    # 5 credential}, 4 lifetime, 5 expiry, 6 a Duration. Pia's network setup failed without
    # servers (pair11); these point at ice_server below, which logs what Pia sends.
    tenant = call['meta'].get('npln-tenant-id') or STATE.tenant_of(call['uid'])
    stun = field(1, STUN_NAME) + field(2, STUN_PORT) + field(3, ICE_UDP)
    relay = (field(1, RELAY_NAME) + field(2, RELAY_PORT) + field(3, ICE_UDP)
             + field(4, call['uid'] or 'relay') + field(5, jwt_for(call['uid'] or 'relay')))
    return (field(1, f'tenants/{tenant}/iceServerSets/local') + field(2, stun) + field(3, relay)
            + field(4, timestamp(600)) + field(5, timestamp(time.time() + 600)))


# PresenceService/KeepAlive replies (M044, a oneof): 1 M058 {1, 2 Durations}, the server's
# keep-alive timing; 2 ends the client's reader. The client's reader takes case 1 as the
# server having the presence: it signals the events a presence change waits on, and each reply
# pushes the stream's idle deadline back by the duration. Unanswered, the client
# cancels the stream a few seconds after a presence change, and the step that made the change
# fails with "A communication error has occurred": leaving a room to join a friend's, a guest
# entering a dungeon (stub-run43). The client answers each case-1 reply with a heartbeat
# (request M042 case 2, M051 {1 user}); answering heartbeats too made a ping-pong of ~2,000
# messages a second (stub-run44). So: a reply for each presence change (case 1, M040), and one
# every few seconds from the stream loop, which the client's heartbeats follow.
KEEPALIVE_SECONDS = 30
KEEPALIVE_BEAT = 5
KEEPALIVE_ACK = field(1, field(1, field(1, KEEPALIVE_SECONDS)) + field(2, field(1, KEEPALIVE_SECONDS)))


@reply('PresenceService/KeepAlive')
def keep_alive(req, call):
    return KEEPALIVE_ACK if submsgs(decode(req), 1) else b''


def presence_mask(req):
    """The attribute keys a presence change's field mask (M040 field 2) names, if any."""
    return [p.removeprefix('attributes.') for m040 in submsgs(decode(req), 1)
            for mask in submsgs(m040, 2) for n, wt, p in mask
            if n == 1 and isinstance(p, str) and p.startswith('attributes.')]


def presence_from_keepalive(req):
    """KeepAlive carries {1: {1: {1: ".../users/current/presence", 3: {1 key, 2 MapValue}...}}};
    find the message named .../presence and read its attributes."""
    def walk(fields):
        if any(n == 1 and isinstance(v, str) and v.endswith('/presence') for n, wt, v in fields):
            return attributes([x for x in fields if x[0] == 3])
        for n, wt, v in fields:
            if isinstance(v, dict) and 'msg' in v:
                found = walk(v['msg'])
                if found:
                    return found
        return {}
    return walk(decode(req) or [])


# --- gRPC handler ------------------------------------------------------------------------------

class Log:
    """JSON lines to a file (stdout without one). quiet keeps events and drops message contents,
    addresses' traffic and auth headers, for a public server. A friend code's secret never gets
    written: it's redacted from the login's decoded request and the login's raw bytes are dropped."""
    SECRET = re.compile(r'(secret=)[0-9a-fA-F]+')
    QUIET_DROP = {'req', 'req_hex', 'resp_hex', 'msg', 'msg_hex', 'raw', 'report', 'docs', 'meta', 'hex'}
    QUIET_SKIP = {'bidi-msg', 'bidi-reply', 'stream-update', 'gamesync-push', 'gamesync-current',
                  'natcheck', 'monitoring', 'ticket-update', 'stream-open', 'stream-closed', 'call'}

    def __init__(self, path, quiet=False):
        self.f = open(path, 'a', buffering=1) if path else None
        self.quiet = quiet
        self.lock = threading.Lock()

    def __call__(self, **rec):
        if self.quiet:
            if rec.get('ev') in self.QUIET_SKIP:
                return
            rec = {k: v for k, v in rec.items() if k not in self.QUIET_DROP}
        elif str(rec.get('method', '')).endswith(LOGIN):
            rec.pop('req_hex', None)
            rec.pop('resp_hex', None)
        rec['t'] = round(time.time(), 3)
        line = json.dumps(rec, ensure_ascii=False,
                          default=lambda o: o.hex() if isinstance(o, (bytes, bytearray)) else str(o))
        line = self.SECRET.sub(r'\1(redacted)', line)
        with self.lock:
            if self.f:
                self.f.write(line + '\n')
            else:
                print(line, flush=True)


def peer_ip(ctx):
    peer = ctx.peer() or ''  # "ipv4:127.0.0.1:43210" or "ipv6:[2001:db8::1]:43210"
    kind, _, rest = peer.partition(':')
    if kind == 'ipv4':
        return rest.rsplit(':', 1)[0]
    if kind == 'ipv6' and rest.startswith('['):
        address = rest[1:].split(']', 1)[0]
        return address[7:] if address.startswith('::ffff:') and '.' in address else address
    return peer


class Handler(grpc.GenericRpcHandler):
    def __init__(self, log):
        self.log = log

    def service(self, details):
        method = details.method
        name = method.rsplit('/', 1)[-1]
        meta = {k: (v if isinstance(v, str) else v.hex()) for k, v in details.invocation_metadata}
        kind = STREAMING.get(name, 'unary')
        self.log(ev='call', method=method, kind=kind, meta=meta)
        log = self.log

        def start(ctx):
            ip = peer_ip(ctx)
            uid = meta.get('uid', '')
            token = meta.get('authorization', '').removeprefix('bearer ').strip()
            with STATE.lock:
                uid = STATE.tokens.get(token, uid)
            call = {'meta': meta, 'ip': ip, 'uid': uid}
            why = STATE.denied(uid, ip, name)
            if why:
                log(ev='denied', method=method, uid=uid, ip=ip, why=why)
                ctx.abort(grpc.StatusCode.UNAVAILABLE if why == 'maintenance'
                          else grpc.StatusCode.PERMISSION_DENIED, why)
            if name == LOGIN and not STATE.login_allowed(ip):
                log(ev='denied', method=method, ip=ip, why='too many logins')
                ctx.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, 'too many logins; wait a minute')
            STATE.touch(uid, ip, name)
            call['log'] = log
            return call

        def check(call, ctx):
            why = STATE.denied(call['uid'], call['ip'])
            if why:
                log(ev='disconnected', method=method, uid=call['uid'], why=why)
                ctx.abort(grpc.StatusCode.PERMISSION_DENIED, why)

        def reply_for(req, call):
            if name in ('CreateReport', 'CreateDocumentReport'):
                STATE.player_report(call['uid'], name, decode(req))
            service = method.split('/')[1].rsplit('.', 1)[-1] if method.count('/') >= 2 else ''
            fn = REPLIES.get(f'{service}/{name}') or REPLIES.get(name)
            return fn(req, call) if fn else b''

        if kind == 'unary':
            def unary(req, ctx):
                call = start(ctx)
                try:
                    resp = reply_for(req, call)
                except RpcError as e:
                    log(ev='unary', method=method, req=decode(req), req_hex=req.hex(),
                        status=e.code.name, details=e.details)
                    ctx.abort(e.code, e.details)
                log(ev='unary', method=method, req=decode(req), req_hex=req.hex(),
                    resp_hex=resp.hex())
                return resp
            return grpc.unary_unary_rpc_method_handler(unary)

        if kind == 'server':
            def server_stream(req, ctx):
                call = start(ctx)
                log(ev='stream-open', method=method, req=decode(req), req_hex=req.hex())
                STATE.stream(call['uid'], name, +1)
                tid = ticket_id(first_string(req, 1)) if name == 'TrackMatchmakingTicket' else None
                try:
                    # One message right away (an empty one is a valid "nothing yet" snapshot for
                    # any type), then keep the stream open until the client leaves or is kicked.
                    # A ticket's stream also carries its changes and ends at a final state.
                    seen = STATE.ticket_version(tid) if tid else None
                    yield reply_for(req, call)
                    if tid and STATE.tickets.get(tid, {}).get('status') in TERMINAL:
                        return
                    if name == 'TrackGameSessionCreationTicket':
                        return  # the client reads until the stream ends
                    follow = FOLLOW.get(name)
                    last = follow(call) if follow else None
                    while ctx.is_active():
                        check(call, ctx)
                        if follow:
                            now_msg = follow(call)
                            if now_msg != last:
                                last = now_msg
                                log(ev='stream-update', method=method, msg=decode(now_msg))
                                yield now_msg
                        if tid and STATE.ticket_version(tid) != seen:
                            seen = STATE.ticket_version(tid)
                            msg, done = ticket_message(tid)
                            log(ev='ticket-update', ticket=tid, msg=decode(msg), msg_hex=msg.hex())
                            yield msg
                            if done:
                                return
                        time.sleep(0.5)
                finally:
                    STATE.stream(call['uid'], name, -1)
                    log(ev='stream-closed', method=method)
            return grpc.unary_stream_rpc_method_handler(server_stream)

        def bidi(reqs, ctx):
            # Requests are read on a thread of their own, so replies (and changes pushed
            # through call['push'], e.g. Gamesync documents another player wrote) can go out
            # at any time.
            call = start(ctx)
            out = queue.Queue()
            call['push'] = out.put
            STATE.stream(call['uid'], name, +1)

            half_closed = threading.Event()

            def read():
                try:
                    for req in reqs:
                        log(ev='bidi-msg', method=method, req=decode(req), req_hex=req.hex())
                        # Only presence changes (field 1) carry attributes; the heartbeats
                        # (field 2) every few seconds would wipe them.
                        if name == 'KeepAlive' and submsgs(decode(req), 1):
                            attrs = presence_from_keepalive(req)
                            STATE.presence(call['uid'], attrs, presence_mask(req))
                            code = caller_code(call)
                            if code and attrs.get('PlayerName'):
                                ACCOUNTS.set_name(code, attrs['PlayerName'])
                        resp = reply_for(req, call)
                        for one in resp if isinstance(resp, list) else [resp]:
                            if one:
                                out.put(one)
                    log(ev='bidi-half-closed', method=method)
                    half_closed.set()
                except Exception as e:  # the call ended, or a reply function failed
                    log(ev='bidi-read-ended', method=method, error=repr(e))
            threading.Thread(target=read, daemon=True).start()
            last_out = time.time()
            try:
                while ctx.is_active():
                    check(call, ctx)
                    try:
                        one = out.get(timeout=0.5)
                    except queue.Empty:
                        if name == 'KeepAlive' and not half_closed.is_set() \
                                and time.time() - last_out >= KEEPALIVE_BEAT:
                            last_out = time.time()
                            yield KEEPALIVE_ACK
                            continue
                        # A player leaving a room removes its Gamesync targets, half-closes and waits
                        # for the stream to end. Left open, the client cancels it after 5.5 s, but
                        # leaving a room to join a friend's gives up sooner: "A communication error
                        # has occurred" and the game logs out (stub-run42).
                        # The presence stream half-closes when the player goes offline; end it too.
                        if half_closed.is_set() and name in ('KeepUserSession', 'KeepAlive'):
                            break
                        continue
                    log(ev='bidi-reply', method=method, msg=decode(one), msg_hex=one.hex())
                    last_out = time.time()
                    yield one
            finally:
                with STATE.lock:
                    for s in STATE.sessions.values():
                        s['watchers'] = [w for w in s['watchers'] if w is not call]
                if name == 'KeepUserSession':
                    end_user_session(call, log)
                STATE.stream(call['uid'], name, -1)
                log(ev='bidi-closed', method=method)
        return grpc.stream_stream_rpc_method_handler(bidi)


# --- UDP: Pia NAT check and monitoring -----------------------------------------------------------

def nat_check_server(log, host, port):
    """Pia's NEX NAT check (nncs1/nncs2-<env>.n.n.srv.nintendo.net:10025, UDP). Requests and
    replies are 16 bytes, four big-endian u32: type (101/102/103), port, IPv4, 0. The reply
    echoes the type with the sender's address as seen here (success needs
    101 and 103, best also 102)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    log(ev='natcheck-listening', host=host, port=port)
    while True:
        data, (ip, sport) = sock.recvfrom(64)
        kind = struct.unpack('>I', data[:4])[0] if len(data) >= 4 else None
        log(ev='natcheck', port=port, src=f'{ip}:{sport}', type=kind, hex=data.hex())
        if len(data) == 16 and kind in (101, 102, 103):
            STATE.nat_probe(f'{ip}:{sport}', kind)
            # Games on this PC reach the server over loopback; --nat-ip reports another address
            # (e.g. the LAN one) so Pia doesn't advertise 127.0.0.1 as its public address.
            seen = NAT_IP if NAT_IP and ip.startswith('127.') else ip
            packed_ip = struct.unpack('>I', socket.inet_aton(seen))[0]
            sock.sendto(struct.pack('>IIII', kind, sport, packed_ip, 0), (ip, sport))


NAT_IP = None  # set by --nat-ip


def is_private(address):
    """Loopback, RFC 1918, link-local or shared-space (100.64.0.0/10, what mesh VPNs such as
    Tailscale hand out) IPv4: the only addresses the server may listen on."""
    import ipaddress
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    if ip.is_unspecified:  # 0.0.0.0 would mean every interface, VPN and public ones too
        return False
    return (ip.is_loopback or ip.is_private and not ip.is_global or ip.is_link_local
            or ip in ipaddress.IPv4Network('100.64.0.0/10'))
STUN_COOKIE = 0x2112A442


def ice_server(log, host, port):
    """The ICE servers AllocateIceServerSet hands out: log every packet, and answer a standard
    STUN binding request (RFC 5389) with the sender's address, in case Pia uses plain STUN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    log(ev='ice-listening', host=host, port=port)
    while True:
        data, (ip, sport) = sock.recvfrom(4096)
        log(ev='ice-udp', port=port, src=f'{ip}:{sport}', size=len(data), hex=data[:256].hex())
        if len(data) >= 20 and data[:2] == b'\x00\x01' and \
                struct.unpack('>I', data[4:8])[0] == STUN_COOKIE:
            packed_ip = struct.unpack('>I', socket.inet_aton(ip))[0]
            attrs = struct.pack('>HHBBHI', 0x0020, 8, 0, 1, sport ^ (STUN_COOKIE >> 16),
                                packed_ip ^ STUN_COOKIE)                    # XOR-MAPPED-ADDRESS
            attrs += struct.pack('>HHBBH4s', 0x0001, 8, 0, 1, sport, socket.inet_aton(ip))
            sock.sendto(struct.pack('>HHI12s', 0x0101, len(attrs), STUN_COOKIE, data[8:20]) + attrs,
                        (ip, sport))


def ice_tcp_logger(log, host, port):
    """Log TCP connections to an ICE port too, in case Pia's relay client uses TCP."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    while True:
        conn, (ip, sport) = srv.accept()
        conn.settimeout(5)
        try:
            data = conn.recv(4096)
        except OSError:
            data = b''
        log(ev='ice-tcp', port=port, src=f'{ip}:{sport}', size=len(data), hex=data[:256].hex())
        conn.close()


def monitoring_server(log, host, port):
    """Pia's monitoring server (g<id>.<env>.p.srv.nintendo.net, UDP 34343): connection-quality
    reports, kept for the page. They are encrypted; without pia_monitoring (not part of the
    published server) only their sizes are kept."""
    keys, widths = [], []
    if pia_monitoring is not None:
        try:
            keys = pia_monitoring.load_keys()
        except OSError as e:
            log(ev='monitoring-keys-missing', error=str(e))
        widths = pia_monitoring.load_widths()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    log(ev='monitoring-listening', host=host, port=port, keys=len(keys), fields=len(widths))
    while True:
        data, (ip, sport) = sock.recvfrom(4096)
        rep = pia_monitoring.decode(data, keys, widths) if keys else {'size': len(data)}
        STATE.report(f'{ip}:{sport}', rep)
        log(ev='monitoring', src=f'{ip}:{sport}', raw=data.hex(), report=rep)


def main():
    global STATE
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8443)
    ap.add_argument('--log', default=None)
    ap.add_argument('--web-port', type=int, default=8090)
    ap.add_argument('--web-host', default='127.0.0.1',
                    help='address for the status page and admin panel (e.g. a LAN or VPN address '
                         'to show friends the status page; the admin API still needs the token)')
    ap.add_argument('--data', default=None,
                    help='folder for accounts.db, admin-password.json and the audit log '
                         '(default: the folder above this script\'s)')
    ap.add_argument('--pki', default=None,
                    help='folder with server.key and server-chain.pem, as make_pki.py writes them '
                         '(default: pki in the data folder\'s default)')
    ap.add_argument('--set-admin-password', action='store_true',
                    help='ask for a new admin password, store its hash (admin-password.json) and exit')
    ap.add_argument('--public', action='store_true',
                    help='allow --host to name public or all (0.0.0.0) addresses, for a server on the '
                         'internet; pair it with --quiet-log')
    ap.add_argument('--quiet-log', action='store_true',
                    help='log events only: no message contents, traffic or auth headers')
    ap.add_argument('--trust-proxy', nargs='?', const='127.0.0.0/8', default=None, metavar='NETWORKS',
                    help='the pages sit behind a reverse proxy (Caddy): take the client address and '
                         'scheme from its X-Forwarded-* headers, only from these comma-separated '
                         'networks (default 127.0.0.0/8; in Docker, the proxy container\'s network)')
    ap.add_argument('--test-players', type=int, default=0,
                    help='keep this many server-side test players calling for help, so a Go Help '
                         'search finds a match without a second game')
    ap.add_argument('--match-delay', type=float, default=State.MATCH_DELAY,
                    help='seconds a Go Help ticket searches before it is matched')
    ap.add_argument('--solo-match-after', type=float, default=0,
                    help='other games (Mario Kart World): give a lone matchmaking ticket a session '
                         'of its own after this many seconds, for testing with one game (0: never)')
    ap.add_argument('--loose-match', action='store_true',
                    help='pair any caller with any helper, skipping the stage check (tests)')
    ap.add_argument('--nat-ip', default=None,
                    help='address the NAT check reports for loopback clients (two games on one PC: '
                         'this PC\'s LAN address; default: the first non-loopback --host)')
    ap.add_argument('--host', default='127.0.0.1',
                    help='comma-separated addresses for the game server, NAT check, monitoring and '
                         'ICE ports (add this PC\'s LAN address to let other PCs on the home network '
                         'play; never a public address)')
    args = ap.parse_args()
    data = Path(args.data) if args.data else ONLINE
    pki = Path(args.pki) if args.pki else PKI
    data.mkdir(parents=True, exist_ok=True)
    if args.set_admin_password:
        import getpass
        first = getpass.getpass('New admin password (12+ characters): ')
        if len(first) < 12:
            raise SystemExit('too short: use at least 12 characters (a few random words work well)')
        if getpass.getpass('Again: ') != first:
            raise SystemExit('the two passwords differ; nothing changed')
        npln_dashboard.write_password_file(data / 'admin-password.json', first)
        print(f'admin password set (hash in {data / "admin-password.json"})')
        return
    hosts = [h.strip() for h in args.host.split(',') if h.strip()]
    for h in hosts:
        if not (args.public or is_private(h)):
            ap.error(f'--host {h}: only loopback, private (RFC 1918), link-local and mesh-VPN '
                     f'addresses; add --public for a server on the internet')
    global NAT_IP
    NAT_IP = args.nat_ip or next((h for h in hosts if not h.startswith('127.') and h != '0.0.0.0'), None)
    log = Log(args.log, args.quiet_log)
    global ACCOUNTS
    STATE = State(data / 'admin-audit.jsonl')
    STATE.game_port = args.port
    ACCOUNTS = npln_accounts.Accounts(data / 'accounts.db', legacy_friends=data / 'friends.json')
    STATE.test_players, STATE.MATCH_DELAY = args.test_players, args.match_delay
    if args.loose_match:
        STATE.overrides['loose_match'] = True
    if args.solo_match_after:
        STATE.overrides['solo_match_after'] = args.solo_match_after

    def matcher():
        while True:
            time.sleep(1)
            for event in STATE.match(time.time()):
                log(**event)
    threading.Thread(target=matcher, daemon=True).start()

    auth = npln_dashboard.AdminAuth(data / 'admin-password.json')
    if not auth.configured():
        log(ev='admin-disabled', why='no admin password; run npln_stub.py --set-admin-password')

    for host in hosts:
        for nat_port in (10025, 10125, 33334):  # 33334: the mapping test
            threading.Thread(target=nat_check_server, args=(log, host, nat_port),
                             daemon=True).start()
        threading.Thread(target=monitoring_server, args=(log, host, 34343), daemon=True).start()
        for ice_port in (STUN_PORT, RELAY_PORT):
            threading.Thread(target=ice_server, args=(log, host, ice_port), daemon=True).start()
            threading.Thread(target=ice_tcp_logger, args=(log, host, ice_port), daemon=True).start()
    npln_dashboard.serve(STATE, args.web_host, args.web_port, auth, log, args.trust_proxy, ACCOUNTS, pki)

    key = (pki / 'server.key').read_bytes()
    chain = (pki / 'server-chain.pem').read_bytes()
    # Tolerate the client's HTTP/2 keepalive pings (the default policy answers frequent pings
    # with GOAWAY, which drops every open stream).
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=64), handlers=[Handler(log)],
                         options=[('grpc.max_receive_message_length', 1 << 20),
                                  ('grpc.keepalive_permit_without_calls', 1),
                                  ('grpc.http2.min_ping_interval_without_data_ms', 1000),
                                  ('grpc.http2.max_ping_strikes', 0),
                                  ('grpc.keepalive_time_ms', 20000),
                                  ('grpc.keepalive_timeout_ms', 10000)])
    for host in hosts:
        if not server.add_secure_port(f'{host}:{args.port}', grpc.ssl_server_credentials([(key, chain)])):
            raise SystemExit(f'cannot listen on {host}:{args.port} (is that address on this PC?)')
    server.start()
    log(ev='listening', port=args.port, hosts=hosts, nat_ip=NAT_IP)
    server.wait_for_termination()


if __name__ == '__main__':
    main()
