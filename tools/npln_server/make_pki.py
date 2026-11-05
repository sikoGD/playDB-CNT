"""Make the certificates a Reservoir server needs: its own certificate authority (CA) and a server
certificate signed by it for the names the games connect to. Players pick the CA's certificate
(ca.der) in Droplet, so the emulator trusts this server; nothing else trusts it.

    python -I make_pki.py --out <folder> [--name "My server"] [--extra-name host.example.org]

Writes ca.key, ca.pem, ca.der, server.key, server.pem and server-chain.pem (server, then CA).
Refuses to overwrite an existing CA: players would have to fetch the new ca.der again.
"""
import argparse
import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# The names the games ask for (Droplet redirects them to the server and checks the certificate
# against these).
NPLN_NAMES = ['*.lp1.t.npln.srv.nintendo.net', '*.t.npln.srv.nintendo.net', '*.npln.srv.nintendo.net',
              'npln.srv.nintendo.net', '*.npln.nintendo.net', 'npln.nintendo.net', 'localhost']


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)


def make(out, name='Reservoir', extra_names=()):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'ca.key').exists():
        raise SystemExit(f'{out} already has a CA (ca.key); move it away to make a new one')
    now = datetime.datetime.now(datetime.timezone.utc)
    # RSA, as the certificates the games were tested with
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, name),
                         x509.NameAttribute(NameOID.COMMON_NAME, f'{name} root CA')])
    ca = (x509.CertificateBuilder()
          .subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, key_cert_sign=True, crl_sign=True,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))

    sans = [x509.DNSName(n) for n in NPLN_NAMES]
    for extra in extra_names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(extra)))
        except ValueError:
            sans.append(x509.DNSName(extra))
    sans.append(x509.IPAddress(ipaddress.ip_address('127.0.0.1')))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, name),
                                     x509.NameAttribute(NameOID.COMMON_NAME, 'npln.private.test')]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=True, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))

    pem = serialization.Encoding.PEM
    private = dict(encoding=pem, format=serialization.PrivateFormat.PKCS8,
                   encryption_algorithm=serialization.NoEncryption())
    write_private(out / 'ca.key', ca_key.private_bytes(**private))
    write_private(out / 'server.key', key.private_bytes(**private))
    (out / 'ca.pem').write_bytes(ca.public_bytes(pem))
    (out / 'ca.der').write_bytes(ca.public_bytes(serialization.Encoding.DER))
    (out / 'server.pem').write_bytes(cert.public_bytes(pem))
    (out / 'server-chain.pem').write_bytes(cert.public_bytes(pem) + ca.public_bytes(pem))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--out', required=True, help='folder for the certificates')
    ap.add_argument('--name', default='Reservoir', help='the server\'s name, shown in the certificates')
    ap.add_argument('--extra-name', action='append', default=[],
                    help='another host name or IP address for the server certificate (repeatable)')
    args = ap.parse_args()
    out = make(args.out, args.name, args.extra_name)
    print(f'wrote the CA and server certificate to {out}; give players {out / "ca.der"}')


if __name__ == '__main__':
    main()
