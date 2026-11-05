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

def reply(request_type, value):
    """Return (response_json_bytes, extra_headers) or None if unsupported."""
    if not request_type.startswith('Request'): return None
    name = 'Response' + request_type[len('Request'):]
    if name not in SCHEMAS: return None
    body = json.dumps({'type': name, 'value': build(name, value)}).encode()
    headers = [('Set-Cookie', 'session-id=local%d; Path=/' % PLAYER_ID)] if name == 'ResponseCreateSessionParams' else []
    return body, headers
