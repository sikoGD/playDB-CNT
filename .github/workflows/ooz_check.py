"""CI: decode ooz's dickens.kraken sample (4-byte size + Kraken stream) and check the result."""
import hashlib, shlex, struct, subprocess, sys, tempfile
from pathlib import Path

EXPECTED = 'b24c37886142e11d0ee687db6ab06f936207aa7f2ea1fd1d9a36763c7a507e6a'
command, sample = shlex.split(sys.argv[1]), Path(sys.argv[2]).read_bytes()
size = struct.unpack_from('<I', sample, 0)[0]
with tempfile.TemporaryDirectory() as tmp:
    raw, out = Path(tmp) / 'in', Path(tmp) / 'out'
    raw.write_bytes(sample[4:])
    subprocess.run(command + [str(raw), str(size), str(out)], check=True)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
print(sys.argv[1], digest)
sys.exit(0 if digest == EXPECTED else 1)
