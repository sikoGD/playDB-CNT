"""Local-only stand-in replies for the game's /frontend/ JSON API (diagnostic).

Envelope (client main+0x217de0): {"type": <name>, "value": {...}}; reply type names
come from the table at main+0x4bc541c, parsers from the table at main+0x5a08a88.
"""
import base64, json, os
from pathlib import Path

PLAYER_ID = int(os.environ.get('DUSK_PLAYER_ID', '1001'))

def create_session(req):
    doc = base64.b64encode(json.dumps({'player_id': PLAYER_ID}).encode()).decode()
    return {
        'player_id': PLAYER_ID,
        'player_id_with_signature': {'player_id_document': doc,
                                     'signature_base64': base64.b64encode(b'\0' * 64).decode()},
        'display_user_id': 'LOCAL-%d' % PLAYER_ID,
        'user_id': 'local-%d' % PLAYER_ID,
        'client_ip': '127.0.0.1',
        'redirect_addr': '',
        'required_relogin_sec': 86400,
        'environment_name': 'local',
        'show_environment_name_in_lobby': False,
    }

SCHEMAS = json.loads((Path(__file__).with_name('response_schemas.json')).read_text())

def default(spec):
    if isinstance(spec, dict):
        if len(spec) == 1 and next(iter(spec)).startswith('object@'): spec = next(iter(spec.values()))
        return {k: default(v) for k, v in spec.items()}
    if isinstance(spec, list): return []
    return {'int': 0, 'bool': False, 'string': ''}.get(spec, 0)

STATE = {'region': None}

def create_session_tracking(req):
    if isinstance(req, dict) and isinstance(req.get('matching_region_id'), int):
        STATE['region'] = req['matching_region_id']
    return create_session(req)

def ping_urls(req):
    regions = sorted({r for r in (STATE['region'], 0, 1, 2, 3) if r is not None})
    return {'ping_servers': [{'matching_region_id': r,
                              'ping_server_url': os.environ.get('DUSK_PING_URL', 'https://phx-ping-nt.fromsoftware-game.net')}
                             for r in regions]}

OVERRIDES = {'ResponseCreateSessionParams': create_session_tracking,
             'ResponseGetPingServerUrlsParams': ping_urls}

def build(name, value):
    fields = SCHEMAS.get(name, {}).get('fields') or {}
    out = default(fields)
    if name in OVERRIDES: out.update(OVERRIDES[name](value))
    return out

# --- online play: players who registered the same room password are matched together ------------
# Only active when the launcher starts the servers in online mode (DUSK_ONLINE=1). The room is a
# hash made by the launcher (the password itself never reaches this server).
import re, threading, time

ONLINE = os.environ.get('DUSK_ONLINE') == '1'
P2P_FORMAT = os.environ.get('DUSK_P2P_FORMAT', 'ip4')   # ip4 | ip4port | pid | empty (what p2p_host_info holds)
P2P_PORT = int(os.environ.get('DUSK_P2P_PORT', '0'))
MIN_PLAYERS = int(os.environ.get('DUSK_MATCH_MIN', '2'))
MAX_PLAYERS = int(os.environ.get('DUSK_MATCH_MAX', '8'))
WAIT_SECONDS = float(os.environ.get('DUSK_MATCH_WAIT', '5'))   # after the minimum is reached, wait for more
TICKET_TTL = float(os.environ.get('DUSK_TICKET_TTL', '30'))   # a search nobody polls for this long is abandoned
LOG = None                       # set by frontdoor: LOG(event, **details)

_lock = threading.RLock()
_room_of_ip = {}                 # client address -> room (registered by the launcher over HTTP)
_players = {}                    # player id -> {'ip', 'room', 'seen'}
_tickets = {}                    # matching request id -> ticket dict
_next = {'pid': 2000, 'ticket': 1, 'match': 1}


def _log(event, **details):
    if LOG:
        LOG(event, **details)


def register_room(ip, room):
    with _lock:
        _room_of_ip[ip] = room
        for player in _players.values():          # players already signed in from this address
            if player['ip'] == ip:
                player['room'] = room
    _log('online_register', ip=ip, room=room[:8])


def stats():
    with _lock:
        waiting = {}
        for t in _tickets.values():
            if t['state'] == 'waiting':
                waiting[t['room'][:8]] = waiting.get(t['room'][:8], 0) + 1
        return {'players': len(_players), 'registered': len(_room_of_ip), 'waiting': waiting}


def _new_player(ctx):
    ctx = ctx or {}
    ip = ctx.get('ip', '?')
    _next['pid'] += 1
    pid = _next['pid']
    _players[pid] = {'ip': ip, 'room': _room_of_ip.get(ip) or 'ip:' + ip, 'seen': time.time()}
    _log('online_player', pid=pid, ip=ip, room=_players[pid]['room'][:8])
    return pid


def _player(ctx):
    found = re.search(r'session-id=local(\d+)', (ctx or {}).get('cookie') or '')
    pid = int(found.group(1)) if found else None
    with _lock:
        if pid not in _players:
            pid = _new_player(ctx)
        _players[pid]['seen'] = time.time()
        return pid, _players[pid]


def _waiting(room, now=None):
    now = now or time.time()
    for t in _tickets.values():   # a player who quit the game mid-search must not be matched
        if t['state'] == 'waiting' and now - t['polled'] > TICKET_TTL:
            t['state'] = 'expired'
            _log('online_ticket_expired', ticket=t['id'], pid=t['pid'])
    return sorted((t for t in _tickets.values() if t['state'] == 'waiting' and t['room'] == room),
                  key=lambda t: t['created'])


def _try_match(room, now):
    group = _waiting(room)
    if len(group) < MIN_PLAYERS:
        return
    if len(group) < MAX_PLAYERS and now - group[0]['created'] < WAIT_SECONDS:
        return
    group = group[:MAX_PLAYERS]
    match_id = _next['match']
    _next['match'] += 1
    members = [(t['pid'], _players[t['pid']]['ip']) for t in group]
    for t in group:
        t.update(state='matched', match_id=match_id, members=members, matched=now)
    _log('online_match', match_id=match_id, room=room[:8], players=[m[0] for m in members],
         host=members[0][0], addresses=[m[1] for m in members])


def _host_info(ticket, ctx):
    host_pid, host_ip = ticket['members'][0]
    if host_ip.startswith('127.') or host_ip == '::1':
        host_ip = (ctx or {}).get('local') or host_ip     # the host reached its own server over loopback
    if host_ip.startswith('127.') or host_ip == '::1':
        host_ip = os.environ.get('DUSK_HOST_IP') or host_ip   # ...and is asking itself: its network address
    if P2P_FORMAT == 'pid':
        return [host_pid]
    if P2P_FORMAT == 'empty':
        return []
    try:
        octets = [int(x) for x in host_ip.split('.')]
    except ValueError:
        octets = []
    return octets + ([P2P_PORT] if P2P_FORMAT == 'ip4port' else [])


def matching_ticket(value, ctx):
    pid, player = _player(ctx)
    with _lock:
        for t in _tickets.values():          # one search at a time per player
            if t['pid'] == pid and t['state'] == 'waiting':
                t['state'] = 'cancelled'
        tid = _next['ticket']
        _next['ticket'] += 1
        now = time.time()
        _tickets[tid] = {'id': tid, 'pid': pid, 'room': player['room'], 'created': now, 'polled': now,
                         'state': 'waiting'}
    _log('online_ticket', ticket=tid, pid=pid, room=player['room'][:8])
    return {'matching_request_id': tid, 'now_rank_point': 0}


def wait_for_matching(value, ctx):
    pid, player = _player(ctx)
    tid = value.get('matching_request_id') if isinstance(value, dict) else None
    now = time.time()
    with _lock:
        ticket = _tickets.get(tid)
        if ticket is None:   # a ticket we never issued: use this player's latest recent one
            ticket = max((t for t in _tickets.values() if t['pid'] == pid and (t['state'] == 'waiting' or (
                t['state'] == 'matched' and now - t['matched'] < 60))), key=lambda t: t['id'], default=None)
        if ticket is not None and ticket['state'] in ('waiting', 'expired') and ticket['pid'] == pid:
            ticket.update(state='waiting', polled=now)   # still searching (revives a slow poller)
        if ticket is not None and ticket['state'] == 'waiting':
            _try_match(ticket['room'], now)
        if ticket is None or ticket['state'] != 'matched':
            return {'result_type': 'WAITING_MATCHING',
                    'body_waiting_matching': {'matchable_player_num': len(_waiting(player['room']))}}
        result = {'result_type': 'MATCHED',
                  'body_matched': {'matching_ticket_type': 'NORMAL_MATCH', 'p2p_host_info': _host_info(ticket, ctx),
                                   'match_id': ticket['match_id'], 'normal_matching_result': {},
                                   'event_matching_result': {'event_match_type': 0}}}
    _log('online_matched_reply', pid=pid, ticket=ticket['id'], match_id=ticket['match_id'],
         p2p_host_info=result['body_matched']['p2p_host_info'])
    return result


def cancel_matching(value, ctx):
    pid, player = _player(ctx)
    with _lock:
        for t in _tickets.values():
            if t['pid'] == pid and t['state'] == 'waiting':
                t['state'] = 'cancelled'
        return {'matchable_player_num': len(_waiting(player['room']))}


def matching_player_num(value, ctx):
    _, player = _player(ctx)
    with _lock:
        return {'matching_player_num': len(_waiting(player['room']))}


ONLINE_HANDLERS = {'ResponseMatchingTicketParams': matching_ticket,
                   'ResponseWaitForMatchingTicketResultParams': wait_for_matching,
                   'ResponseCancelMatchingTicketParams': cancel_matching,
                   'ResponseGetMatchingPlayerNumParams': matching_player_num}


def reply(request_type, value, ctx=None):
    """Return (response_json_bytes, extra_headers) or None if unsupported."""
    if not request_type.startswith('Request'): return None
    name = 'Response' + request_type[len('Request'):]
    if name not in SCHEMAS: return None
    if ONLINE:
        if name == 'ResponseCreateSessionParams':
            with _lock:
                pid = _new_player(ctx)
            doc = base64.b64encode(json.dumps({'player_id': pid}).encode()).decode()
            payload = build(name, value)
            payload.update(player_id=pid, display_user_id='LOCAL-%d' % pid, user_id='local-%d' % pid,
                           player_id_with_signature={'player_id_document': doc,
                                                     'signature_base64': base64.b64encode(b'\0' * 64).decode()})
            return (json.dumps({'type': name, 'value': payload}).encode(),
                    [('Set-Cookie', 'session-id=local%d; Path=/' % pid)])
        if name in ONLINE_HANDLERS:
            payload = build(name, value)
            payload.update(ONLINE_HANDLERS[name](value, ctx))
            return json.dumps({'type': name, 'value': payload}).encode(), []
    body = json.dumps({'type': name, 'value': build(name, value)}).encode()
    headers = [('Set-Cookie', 'session-id=local%d; Path=/' % PLAYER_ID)] if name == 'ResponseCreateSessionParams' else []
    return body, headers
