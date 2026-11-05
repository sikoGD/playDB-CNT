"""Loopback diagnostic TLS router; no production endpoints are contacted."""
import argparse, datetime, json, os, select, socket, ssl, struct, threading, time
from pathlib import Path
import frontend_api
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

lock = threading.Lock()
def log(event, **details):
    with lock:
        with open(args.log, 'a', encoding='utf8') as out:
            out.write(json.dumps(dict(event=event, time=time.time(), **details))+'\n')

def certificate(ca_dir, out):
    out.mkdir(parents=True, exist_ok=True)
    keyfile, certfile = out/'fs.key', out/'fs-chain.pem'
    if keyfile.exists() and certfile.exists(): return keyfile, certfile
    ca_key = serialization.load_pem_private_key((ca_dir/'ca.key').read_bytes(), None)
    ca_pem = (ca_dir/'ca.pem').read_bytes()
    ca = x509.load_pem_x509_certificate(ca_pem)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'fromsoftware-game.net')]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('*.fromsoftware-game.net'),x509.DNSName('fromsoftware-game.net')]),False)
            .sign(ca_key,hashes.SHA256()))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM)+ca_pem)
    return keyfile, certfile

def client_hello(data):
    try:
        if len(data)<5 or data[0]!=22: return None, False
        end=5+int.from_bytes(data[3:5],'big')
        if len(data)<end: return None,False
        i=5+4+2+32
        i+=1+data[i]; i+=2+int.from_bytes(data[i:i+2],'big'); i+=1+data[i]
        ext_end=i+2+int.from_bytes(data[i:i+2],'big'); i+=2
        while i+4<=min(ext_end,end):
            kind,size=struct.unpack_from('>HH',data,i); body=data[i+4:i+4+size]; i+=4+size
            if kind==0 and len(body)>=5:
                n=int.from_bytes(body[3:5],'big'); return body[5:5+n].decode('ascii'),True
        return None,True
    except (IndexError,ValueError,struct.error,UnicodeError): return None,True

def proxy(conn):
    with socket.create_connection(('127.0.0.1',args.upstream),timeout=5) as upstream:
        conn.settimeout(None); upstream.settimeout(None)
        while True:
            ready,_,_=select.select([conn,upstream],[],[],30)
            if not ready: continue
            for sock in ready:
                data=sock.recv(65536)
                if not data: return
                (upstream if sock is conn else conn).sendall(data)

def fs_request(conn,context,host):
    try:
        with context.wrap_socket(conn,server_side=True) as tls:
            log('tls_ok',host=host,alpn=tls.selected_alpn_protocol(),version=tls.version())
            tls.settimeout(15); buf=b''
            while len(buf)<1048576:
                chunk=tls.recv(65536)
                if not chunk: break
                buf+=chunk
                while b'\r\n\r\n' in buf:
                    head,_,body=buf.partition(b'\r\n\r\n'); lines=head.decode('latin1').split('\r\n')
                    headers=dict(line.split(':',1) for line in lines[1:] if ':' in line)
                    size=int(next((v.strip() for k,v in headers.items() if k.lower()=='content-length'),'0'))
                    if len(body)<size: break
                    buf=body[size:]
                    # Avoid logging tokens, headers or message bodies.
                    def shape(value):
                        if isinstance(value, dict): return {str(k): shape(v) for k,v in value.items()}
                        if isinstance(value, list): return [shape(v) for v in value[:3]]
                        return type(value).__name__
                    try: body_shape=shape(json.loads(body[:size]))
                    except (ValueError,UnicodeError): body_shape={'prefix_hex':body[:min(size,8)].hex()}
                    content_type=next((v.strip() for k,v in headers.items() if k.lower()=='content-type'),None)
                    log('http_request',host=host,request=lines[0].split('?')[0],body_length=size,
                        content_type=content_type,body_shape=body_shape)
                    target=lines[0].split(' ')[1].split('?')[0] if ' ' in lines[0] else ''
                    redirect=os.environ.get('DUSK_REDIRECT_VALUE')
                    try: parsed=json.loads(body[:size])
                    except (ValueError,UnicodeError): parsed=None
                    rtype=parsed.get('type') if isinstance(parsed,dict) and isinstance(parsed.get('type'),str) else None
                    if rtype:
                        # Values are logged only for request types that carry no credentials.
                        val=parsed.get('value') if rtype!='RequestCreateSessionParams' else None
                        log('frontend_type',type=rtype,target=target,value=json.dumps(val)[:400] if val is not None else None)
                    api=frontend_api.reply(rtype,parsed.get('value')) if rtype and os.environ.get('DUSK_FRONTEND')=='1' else None
                    if not api and os.environ.get('DUSK_FRONTEND')=='1' and host.startswith('phx-ping'):
                        api=(b'{}',[])
                    if api and target!='/redirect':
                        payload,extra=api
                        head='HTTP/1.1 200 OK|Content-Type: application/json|Content-Length: %d|'%len(payload)
                        head+=''.join('%s: %s|'%h for h in extra)+'Connection: close||'
                        tls.sendall(head.replace('|',chr(13)+chr(10)).encode()+payload)
                        log('http_response',status=200,body='frontend',type=rtype)
                    elif redirect and target=='/redirect':
                        # Reply schema from the client handler at main+0xbbee48: response.redirect (string; "nochange" keeps base URL).
                        payload=json.dumps({'response':{'redirect':redirect}}).encode()
                        tls.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'%len(payload)+payload)
                        log('http_response',status=200,body='redirect',value=redirect)
                    elif os.environ.get('DUSK_REDIRECT_EMPTY_JSON') == '1':
                        tls.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                        log('http_response',status=200,body='empty_object')
                    else:
                        tls.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                        log('http_response',status=404)
                    return
    except (OSError,ssl.SSLError,ValueError) as exc: log('tls_or_http_error',host=host,error=str(exc))

def handle(conn,context):
    try:
        conn.settimeout(5); start=time.monotonic(); sni=None
        while time.monotonic()-start<5:
            data=conn.recv(65536,socket.MSG_PEEK)
            if not data: return
            sni,done=client_hello(data)
            if done: break
            time.sleep(.02)
        log('route',host=sni)
        if not sni and not done:
            data=conn.recv(65536,socket.MSG_PEEK)
            log('non_tls_connection',length=len(data),prefix_hex=data[:8].hex())
            return
        if sni and (sni=='fromsoftware-game.net' or sni.endswith('.fromsoftware-game.net')):
            fs_request(conn,context,sni)
        else: proxy(conn)
    except (OSError,ssl.SSLError) as exc: log('connection_error',error=str(exc))
    finally: conn.close()

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--port',type=int,default=18443); ap.add_argument('--upstream',type=int,default=18444)
    ap.add_argument('--ca-dir',type=Path,required=True); ap.add_argument('--cert-dir',type=Path,required=True); ap.add_argument('--log',required=True)
    args=ap.parse_args(); key,chain=certificate(args.ca_dir,args.cert_dir)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(chain,key); context.set_alpn_protocols(['http/1.1'])
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); server.bind(('127.0.0.1',args.port)); server.listen(32)
        log('listening',port=args.port,upstream=args.upstream)
        while True:
            conn,_=server.accept(); threading.Thread(target=handle,args=(conn,context),daemon=True).start()
