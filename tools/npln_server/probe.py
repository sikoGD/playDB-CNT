import json
from concurrent import futures
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import grpc
import make_pki
import npln_accounts
import npln_stub as server_code

root=Path(__file__).resolve().parent
pki=root/'pki'
if not (pki/'ca.key').exists():
    make_pki.make(pki, name='Isolated local test')
server_code.STATE=server_code.State(root/'audit.jsonl')
server_code.ACCOUNTS=npln_accounts.Accounts(root/'accounts.db')
logs=[]
def safe_log(**event):
    logs.append(event.get('ev','unknown'))
server=grpc.server(futures.ThreadPoolExecutor(max_workers=2), handlers=[server_code.Handler(safe_log)])
key=(pki/'server.key').read_bytes()
chain=(pki/'server-chain.pem').read_bytes()
port=server.add_secure_port('127.0.0.1:0',grpc.ssl_server_credentials([(key,chain)]))
if not port: raise RuntimeError('loopback bind failed')
server.start()
channel=grpc.secure_channel(f'127.0.0.1:{port}', grpc.ssl_channel_credentials((pki/'ca.pem').read_bytes()), options=(('grpc.ssl_target_name_override','t-2812e67e-lp1.lp1.t.npln.srv.nintendo.net'),))
try:
    call=channel.unary_unary('/nn.npln.auth.v1.Auth/IssuePrearrangedUserToken')
    reply=call(server_code.field(1,'tenants/local-test'),timeout=5)
    fields=server_code.decode(reply)
    ok=isinstance(fields,list) and {row[0] for row in fields}>={1,2}
    wrong = grpc.secure_channel(f'127.0.0.1:{port}', grpc.ssl_channel_credentials((pki/'ca.pem').read_bytes()), options=(('grpc.ssl_target_name_override','wrong.invalid'),))
    try:
        try:
            wrong.unary_unary('/nn.npln.auth.v1.Auth/IssuePrearrangedUserToken')(b'',timeout=3)
            rejected_wrong_name=False
        except grpc.RpcError:
            rejected_wrong_name=True
    finally:
        wrong.close()
    if not rejected_wrong_name: raise RuntimeError('TLS accepted wrong hostname')
    result={'wrong_tls_hostname_rejected':rejected_wrong_name,'host':'127.0.0.1','tls_hostname_verified':True,'auth_request':'synthetic anonymous with no account credential', 'rpc_succeeded':True,'response_has_user_and_session_fields':ok,'event_names':sorted(set(logs)),'game_connected':False}
    (root/'local_probe_result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
    if not ok: raise RuntimeError('missing response fields')
finally:
    channel.close()
    server.stop(0).wait(5)


