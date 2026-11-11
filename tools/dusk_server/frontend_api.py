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
#
# How the game searches (host logs, 2026-11-08): RequestMatchingTicket, then
# RequestWaitForMatchingTicketResult about every 2 s, 4 times (~6 s); with no MATCHED it drops that
# ticket and makes a new one ~10 s later. So a search is tracked per PLAYER across tickets, a match is
# formed among players still searching, and each member gets MATCHED on its next poll or ticket.
import re, secrets, threading, time

ONLINE = os.environ.get('DUSK_ONLINE') == '1'
BOTS_EVERYWHERE = os.environ.get('DUSK_BOTS') == '1'      # practice: every room is a bots room
P2P_FORMAT = os.environ.get('DUSK_P2P_FORMAT', 'ip4')   # ip4 | ip4port | pid | empty (what p2p_host_info holds)
P2P_PORT = int(os.environ.get('DUSK_P2P_PORT', '0'))
MIN_PLAYERS = max(1, int(os.environ.get('DUSK_MATCH_MIN', '2')))   # the game allows up to 8; 2 to play together
MAX_PLAYERS = int(os.environ.get('DUSK_MATCH_MAX', '8'))
WAIT_SECONDS = float(os.environ.get('DUSK_MATCH_WAIT', '20'))  # wait this long for everyone in the room
SEARCH_TTL = float(os.environ.get('DUSK_SEARCH_TTL', '20'))     # a search nobody touched this long ended
PRESENT_SECONDS = float(os.environ.get('DUSK_PRESENT', '90'))   # signed in and active recently
MATCH_KEEP = float(os.environ.get('DUSK_MATCH_KEEP', '90'))     # how long an undelivered match is kept
GAME_RULE = os.environ.get('DUSK_GAME_RULE', 'REGULAR')
RULE_TYPE = os.environ.get('DUSK_RULE_TYPE', 'DEFAULT')
TEAM_RULE = os.environ.get('DUSK_TEAM_RULE', 'SOLO')
LOG = None                       # set by frontdoor: LOG(event, **details)

_lock = threading.RLock()
_room_of_ip = {}                 # client address -> room (registered by the launcher over HTTP)
_bots_rooms = set()              # rooms whose host asked for bots
_players = {}                    # player id -> {'ip', 'room', 'seen', 'search', 'match'}
_matches = {}                    # match id -> {'room', 'members', 'spec', 'bots', 'created', 'key', ...}
_next = {'pid': 2000, 'ticket': 1, 'match': 1}


def _log(event, **details):
    if LOG:
        LOG(event, **details)


def register_room(ip, room, bots=False):
    with _lock:
        _room_of_ip[ip] = room
        if bots:
            _bots_rooms.add(room)
        for player in _players.values():          # players already signed in from this address
            if player['ip'] == ip:
                player['room'] = room
    _log('online_register', ip=ip, room=room[:8], bots=bool(bots))


def _is_bots(room):
    return BOTS_EVERYWHERE or room in _bots_rooms


def stats():
    with _lock:
        now = time.time()
        searching = {}
        for p in _players.values():
            if _searching(p, now):
                searching[p['room'][:8]] = searching.get(p['room'][:8], 0) + 1
        return {'players': len(_players), 'registered': len(_room_of_ip), 'waiting': searching}


def _new_player(ctx):
    ctx = ctx or {}
    ip = ctx.get('ip', '?')
    _next['pid'] += 1
    pid = _next['pid']
    _players[pid] = {'ip': ip, 'room': _room_of_ip.get(ip) or 'ip:' + ip, 'seen': time.time(),
                     'search': None, 'match': None}
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


def _searching(player, now):
    search = player['search']
    return search is not None and player['match'] is None and now - search['last'] <= SEARCH_TTL


def _room_state(room, now):
    present = [pid for pid, p in _players.items() if p['room'] == room and now - p['seen'] <= PRESENT_SECONDS]
    searching = sorted((pid for pid in present if _searching(_players[pid], now)),
                       key=lambda pid: _players[pid]['search']['since'])
    return present, searching


def _try_match(room, now):
    present, searching = _room_state(room, now)
    bots = _is_bots(room)
    need = 1 if bots else MIN_PLAYERS
    if len(searching) < need:
        return
    oldest = _players[searching[0]]['search']['since']
    everyone = len(searching) >= len(present)
    if len(searching) < MAX_PLAYERS and not everyone and now - oldest < WAIT_SECONDS:
        return
    group = searching[:MAX_PLAYERS]
    # The PC that hosts the room (its game reaches the server over loopback) hosts the match too.
    group.sort(key=lambda pid: 0 if _players[pid]['ip'].startswith('127.') else 1)
    match_id = _next['match']
    _next['match'] += 1
    spec = _players[group[0]]['search'].get('spec') or {}
    _matches[match_id] = {'room': room, 'members': [(pid, _players[pid]['ip']) for pid in group],
                          'spec': spec, 'bots': bots, 'created': now, 'delivered': set(),
                          'specs': {pid: _players[pid]['search'].get('spec') or {} for pid in group},
                          'key': base64.b64encode(secrets.token_bytes(32)).decode(), 'registered': None}
    for pid in group:
        _players[pid]['match'] = match_id
        _players[pid]['search'] = None
    _log('online_match', match_id=match_id, room=room[:8], players=group, host=group[0], bots=bots,
         present=len(present), addresses=[_players[pid]['ip'] for pid in group])


def _current_match(player, now):
    match = _matches.get(player['match'])
    if match is None or now - match['created'] > MATCH_KEEP:
        player['match'] = None
        return None
    return match


def _host_info(match, ctx):
    host_pid, host_ip = match['members'][0]
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
    now = time.time()
    with _lock:
        match = _current_match(player, now)
        if match is not None and pid in match['delivered']:
            player['match'] = None                 # a new search after a delivered match
        tid = _next['ticket']
        _next['ticket'] += 1
        if player['match'] is None:
            if player['search'] is None or now - player['search']['last'] > SEARCH_TTL:
                player['search'] = {'since': now, 'last': now}
            player['search'].update(last=now, ticket=tid,
                                    spec=value.get('normal_matching_spec') if isinstance(value, dict) else None,
                                    request=value if isinstance(value, dict) else {})
            _try_match(player['room'], now)
    _log('online_ticket', ticket=tid, pid=pid, room=player['room'][:8], match=player['match'])
    return {'matching_request_id': tid, 'now_rank_point': 0}


def wait_for_matching(value, ctx):
    pid, player = _player(ctx)
    now = time.time()
    with _lock:
        if player['match'] is None and player['search'] is not None:
            player['search']['last'] = now
            _try_match(player['room'], now)
        match = _current_match(player, now)
        if match is None:
            _, searching = _room_state(player['room'], now)
            return {'result_type': 'WAITING_MATCHING',
                    'body_waiting_matching': {'matchable_player_num': len(searching)}}
        match['delivered'].add(pid)
        match_id = player['match']
        result = {'result_type': 'MATCHED',
                  'body_matched': {'matching_ticket_type': 'NORMAL_MATCH', 'p2p_host_info': _host_info(match, ctx),
                                   'match_id': match_id, 'normal_matching_result': {},
                                   'event_matching_result': {'event_match_type': 0}}}
    _log('online_matched_reply', pid=pid, match_id=match_id, p2p_host_info=result['body_matched']['p2p_host_info'])
    return result


def cancel_matching(value, ctx):
    pid, player = _player(ctx)
    now = time.time()
    with _lock:
        if player['search'] is not None:
            _log('online_cancel', pid=pid)
        player['search'] = None
        _, searching = _room_state(player['room'], now)
        return {'matchable_player_num': len(searching)}


def matching_player_num(value, ctx):
    _, player = _player(ctx)
    with _lock:
        _, searching = _room_state(player['room'], time.time())
        return {'matching_player_num': len(searching)}


def _signature(pid):
    doc = base64.b64encode(json.dumps({'player_id': pid}).encode()).decode()
    return {'player_id_document': doc, 'signature_base64': base64.b64encode(b'\0' * 64).decode()}


def register_room_reply(value, ctx):
    """The match host registers the room the others join (guess: room id = match id)."""
    pid, player = _player(ctx)
    with _lock:
        match = _current_match(player, time.time())
        if match is None:
            return {}
        match['registered'] = pid
        return {'room_id': player['match'], 'public_key_base64': match['key']}


def wait_for_assigning_room(value, ctx):
    pid, player = _player(ctx)
    with _lock:
        match = _current_match(player, time.time())
        if match is None:
            return {'has_result': False}
        spec = match['spec'] or {}
        infos = [{'player_id': member, 'display_user_id': 'LOCAL-%d' % member,
                  'main_role_id': match['specs'].get(member, {}).get('role_id', -1), 'received_role_id': 0,
                  'received_role_player_id_1': 0, 'received_role_player_id_2': 0, 'duo_team': ''}
                 for member, _ in match['members']]
        return {'has_result': True,
                'body': {'game_mode': spec.get('game_mode') or 'REGULAR_MATCH',
                         'assigning_room': {
                             'match_id': player['match'],
                             'room_spec': {'game_mode': spec.get('game_mode') or 'REGULAR_MATCH',
                                           'game_rule': GAME_RULE, 'rule_type': RULE_TYPE, 'team_rule': TEAM_RULE,
                                           'regulation_version': 0, 'executable_version': 26192},
                             'is_allowed_bot': match['bots'],
                             'player_infos': infos}}}


def delete_room(value, ctx):
    pid, player = _player(ctx)
    return {'player_id_with_signature': _signature(pid)}


ONLINE_HANDLERS = {'ResponseMatchingTicketParams': matching_ticket,
                   'ResponseWaitForMatchingTicketResultParams': wait_for_matching,
                   'ResponseCancelMatchingTicketParams': cancel_matching,
                   'ResponseGetMatchingPlayerNumParams': matching_player_num,
                   'ResponseRegisterRoomParams': register_room_reply,
                   'ResponseWaitForAssigningRoomParams': wait_for_assigning_room,
                   'ResponseDeleteRoomParams': delete_room}
# Replies logged in full: everything after a match is still being learned from the game.
TRACE = ('Matching', 'Room', 'Bot', 'Match', 'Polling', 'CheckAlive', 'Visit')


def reply(request_type, value, ctx=None):
    """Return (response_json_bytes, extra_headers) or None if unsupported."""
    if not request_type.startswith('Request'): return None
    name = 'Response' + request_type[len('Request'):]
    if ONLINE and not any(word in name for word in TRACE) and name not in SCHEMAS:
        _log('online_unknown_request', type=request_type)
    if name not in SCHEMAS: return None
    if ONLINE:
        if name == 'ResponseCreateSessionParams':
            with _lock:
                pid = _new_player(ctx)
            payload = build(name, value)
            payload.update(player_id=pid, display_user_id='LOCAL-%d' % pid, user_id='local-%d' % pid,
                           player_id_with_signature=_signature(pid))
            return (json.dumps({'type': name, 'value': payload}).encode(),
                    [('Set-Cookie', 'session-id=local%d; Path=/' % pid)])
        payload = build(name, value)
        if name in ONLINE_HANDLERS:
            payload.update(ONLINE_HANDLERS[name](value, ctx))
        else:
            _player(ctx)   # any request keeps the player "present" in its room
        if any(word in name for word in TRACE) and name != 'ResponseWaitForMatchingTicketResultParams':
            _log('online_reply', type=name, value=json.dumps(payload)[:2000])
        return json.dumps({'type': name, 'value': payload}).encode(), []
    body = json.dumps({'type': name, 'value': build(name, value)}).encode()
    headers = [('Set-Cookie', 'session-id=local%d; Path=/' % PLAYER_ID)] if name == 'ResponseCreateSessionParams' else []
    return body, headers
