"""Bot Match: the game's hidden full-map missions (you + 7 CPU players) as extra tutorials:
Bot Match (Solo), row 8000, everyone for themselves, and Bot Match (Duo), row 8020, played in
alliances of two.

Everything is built from the player's own game files at launch:
- regulation_nt.bin (AES-256-CBC, DCX/ZSTD, a BND4 of params): SinglePlayMissionParam rows 8000 and
  8020 are moved into the Tutorials tab and their unlock condition is cleared. The launcher writes the result
  into the BCAT cache the game reads its regulation from.
- msg/<language>/menu_nt.msgbnd.dcx (DCX/Kraken, a BND4 of FMG text): the missions' names and
  descriptions are added and the archive is written back as DCX/zlib, as a LayeredFS mod in the
  emulator's load folder. Kraken is decoded by the bundled ooz.exe (GPL-3.0, source in tools/ooz).
"""
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

import zstandard
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TITLE_ID = '0400D1D02399A000'
MOD_NAME = 'Bot Match (playDB-CNT)'
STAMP = 'botmatch.json'
STAMP_VERSION = 2

REGULATION_KEY = bytes.fromhex('10B8C2BDAAB0FA1D5EA1CD750CFDBC1368A76603119D56647F4B25069443C64B')
MISSION_PARAM = 'SinglePlayMissionParam.param'
MISSION_ROW_SIZE = 0xfc
TUTORIAL_TAB = 1000          # SinglePlayMissionTabParam row of Battle Remembrance > Tutorials
NAME_FMG, INFO_FMG = 211, 212  # SinglePlayMission_Name, SinglePlayMission_Info


class Mission:
    def __init__(self, row, text_id, name, info):
        self.row, self.text_id, self.name, self.info = row, text_id, name, info

    def info_for(self, language):
        return self.info.get(language) or self.info.get(language[:3]) or self.info['en']


# Both are the full map (m10_00) with 8 players: you + PCBot_1..7. In the Tutorials list, Solo comes
# first (the rows' own sort values, 80 and 85).
SOLO = Mission(8000, 10101, 'Bot Match (Solo)', {
    'en': 'A complete Dusk Battle on the full map: you against 7 CPU players, everyone for themselves. '
          'No other players needed.\n\nPlay all 4 phases of a real match, up to the final battle.',
    'porbr': 'Uma Batalha Crepuscular completa no mapa inteiro: você contra 7 jogadores da CPU, cada um '
             'por si. Não precisa de outros jogadores.\n\nJogue as quatro fases de uma partida real, até '
             'a batalha final.',
    'spa': 'Una batalla crepuscular completa en el mapa entero: tú contra 7 jugadores controlados por '
           'la CPU, cada uno por su cuenta. No hacen falta otros jugadores.\n\nDisputa las cuatro fases '
           'de una partida real, hasta el combate final.',
    'fra': "Une bataille crépusculaire complète sur toute la carte : vous contre 7 joueurs contrôlés "
           "par l'ordinateur, chacun pour soi. Aucun autre joueur n'est nécessaire.\n\nParticipez aux "
           "quatre phases d'une vraie partie, jusqu'à l'ultime bataille.",
    'deude': 'Eine vollständige Dämmerungsschlacht auf der ganzen Karte: du gegen 7 Computer-Spieler, '
             'jeder für sich. Andere Spieler sind nicht nötig.\n\nSpiele alle vier Phasen einer echten '
             'Partie bis zur Entscheidungsschlacht.',
    'itait': "Una battaglia crepuscolare completa sull'intera mappa: tu contro 7 giocatori controllati "
             "dalla CPU, ognuno per sé. Non servono altri giocatori.\n\nGioca tutte le 4 fasi di una vera "
             "partita, fino alla battaglia finale.",
})
DUO = Mission(8020, 10121, 'Bot Match (Duo)', {
    'en': 'A complete Dusk Battle on the full map with 7 CPU players, fought in alliances of two. No '
          'other players needed.\n\nPlay all 4 phases of a real match, up to the final battle.',
    'porbr': 'Uma Batalha Crepuscular completa no mapa inteiro com 7 jogadores da CPU, em alianças de '
             'dois. Não precisa de outros jogadores.\n\nJogue as quatro fases de uma partida real, até a '
             'batalha final.',
    'spa': 'Una batalla crepuscular completa en el mapa entero con 7 jugadores controlados por la CPU, '
           'en alianzas de dos. No hacen falta otros jugadores.\n\nDisputa las cuatro fases de una '
           'partida real, hasta el combate final.',
    'fra': "Une bataille crépusculaire complète sur toute la carte avec 7 joueurs contrôlés par "
           "l'ordinateur, en alliances de deux. Aucun autre joueur n'est nécessaire.\n\nParticipez aux "
           "quatre phases d'une vraie partie, jusqu'à l'ultime bataille.",
    'deude': 'Eine vollständige Dämmerungsschlacht auf der ganzen Karte mit 7 Computer-Spielern, in '
             'Zweierbündnissen. Andere Spieler sind nicht nötig.\n\nSpiele alle vier Phasen einer '
             'echten Partie bis zur Entscheidungsschlacht.',
    'itait': "Una battaglia crepuscolare completa sull'intera mappa con 7 giocatori controllati dalla "
             "CPU, in alleanze di due. Non servono altri giocatori.\n\nGioca tutte le 4 fasi di una vera "
             "partita, fino alla battaglia finale.",
})
MISSIONS = (SOLO, DUO)


# --- Regulation -----------------------------------------------------------------------------------

def _aes(iv):
    return Cipher(algorithms.AES(REGULATION_KEY), modes.CBC(iv))


def game_zstd(data):
    """zstd as the game's decoder needs it: every block holds at most 64 KiB (a block is flushed
    after each 64 KiB of input), a 128 KiB window, no content size and no checksum. Frames from a
    plain zstd call crash the game while it loads the regulation."""
    params = zstandard.ZstdCompressionParameters.from_level(
        19, window_log=17, write_content_size=0, write_checksum=0)
    compressor = zstandard.ZstdCompressor(compression_params=params).compressobj()
    out = bytearray()
    for pos in range(0, len(data), 0x10000):
        out += compressor.compress(data[pos:pos + 0x10000])
        out += compressor.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)
    out += compressor.flush()
    return bytes(out)


def bnd_entries(bnd):
    """[(header offset, id, name, data offset, size)] of a BND4 (format 0x74 entries)."""
    if bnd[:4] != b'BND4':
        raise ValueError('not a BND4 archive')
    out = []
    for i in range(struct.unpack_from('<I', bnd, 0xc)[0]):
        at = 0x40 + i * 0x24
        _, _, size, _, offset, fid, name_at = struct.unpack_from('<BxxxiqqIiI', bnd, at)
        end = name_at
        while bnd[end:end + 2] != b'\0\0':
            end += 2
        out.append((at, fid, bnd[name_at:end].decode('utf-16le').split('\\')[-1], offset, size))
    return out


def patch_regulation(blob):
    """The regulation with both Bot Matches in the Tutorials tab."""
    iv, body = blob[:16], blob[16:]
    if len(body) % 16:
        raise ValueError('regulation is not a whole number of AES blocks')
    decryptor = _aes(iv).decryptor()
    plain = decryptor.update(body) + decryptor.finalize()
    if plain[:4] != b'DCX\0' or plain[0x28:0x2c] != b'ZSTD':
        raise ValueError('unexpected regulation format')
    usize, csize = struct.unpack_from('>II', plain, 0x1c)
    bnd = bytearray(zstandard.ZstdDecompressor().decompress(plain[0x4c:0x4c + csize], max_output_size=usize))
    param = next((e for e in bnd_entries(bnd) if e[2] == MISSION_PARAM), None)
    if param is None:
        raise ValueError(f'{MISSION_PARAM} not found')
    base = param[3]
    rows = {}
    for i in range(struct.unpack_from('<H', bnd, base + 0xa)[0]):
        rid, offset = struct.unpack_from('<ixxxxQ', bnd, base + 0x40 + i * 24)
        rows[rid] = base + offset
    ordered = sorted(rows.values())
    if len(ordered) < 2 or ordered[1] - ordered[0] != MISSION_ROW_SIZE:
        raise ValueError(f'{MISSION_PARAM} has an unexpected layout')
    for mission in MISSIONS:
        row = rows.get(mission.row)
        if row is None or struct.unpack_from('<I', bnd, row + 4)[0] != mission.text_id or bnd[row + 0x17] != 8:
            raise ValueError(f'{MISSION_PARAM} row {mission.row} is not the 8-player full map')
        struct.pack_into('<I', bnd, row + 8, TUTORIAL_TAB)   # tab: Tutorials
        struct.pack_into('<i', bnd, row + 0x5c, -1)          # no unlock condition
    data = game_zstd(bytes(bnd))
    header = bytearray(plain[:0x4c])
    struct.pack_into('>II', header, 0x1c, len(bnd), len(data))
    packed = bytes(header) + data
    packed += bytes(-len(packed) % 16)
    encryptor = _aes(iv).encryptor()
    return iv + encryptor.update(packed) + encryptor.finalize()


# --- Menu text ------------------------------------------------------------------------------------

def undcx(blob, ooz):
    usize, csize = struct.unpack_from('>II', blob, 0x1c)
    algo, data = blob[0x28:0x2c], blob[0x4c:0x4c + csize]
    if algo == b'DFLT':
        return zlib.decompress(data)
    if algo != b'KRAK':
        raise ValueError(f'unknown DCX compression {algo!r}')
    with tempfile.TemporaryDirectory() as tmp:
        source, target = Path(tmp) / 'in', Path(tmp) / 'out'
        source.write_bytes(data)
        done = subprocess.run([str(ooz), str(source), str(usize), str(target)], capture_output=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if done.returncode != 0 or not target.exists():
            raise ValueError('could not decompress the menu text (Kraken)')
        return target.read_bytes()


def dcx_dflt(data):
    """DCX with zlib, laid out like the game's own gameparam_nt.parambnd.dcx."""
    packed = zlib.compress(data, 9)
    head = bytearray(0x4c)
    head[0:4] = b'DCX\0'
    struct.pack_into('>IIIII', head, 4, 0x10000, 0x18, 0x24, 0x44, 0x4c)
    head[0x18:0x1c] = b'DCS\0'
    struct.pack_into('>II', head, 0x1c, len(data), len(packed))
    head[0x24:0x28] = b'DCP\0'
    head[0x28:0x2c] = b'DFLT'
    struct.pack_into('>I', head, 0x2c, 0x20)
    head[0x30] = 9
    head[0x40:0x44] = bytes([0, 1, 1, 0])
    head[0x44:0x48] = b'DCA\0'
    struct.pack_into('>I', head, 0x48, 8)
    return bytes(head) + packed


def fmg_set(fmg, texts):
    """The FMG (version 2) with {id: text} filled in; each id must already have a slot."""
    if fmg[2] != 2:
        raise ValueError('unexpected FMG version')
    groups, count = struct.unpack_from('<ii', fmg, 0xc)
    table = struct.unpack_from('<q', fmg, 0x18)[0]
    strings = []
    for i in range(count):
        offset = struct.unpack_from('<q', fmg, table + i * 8)[0]
        if not offset:
            strings.append(None)
            continue
        end = offset
        while fmg[end:end + 2] != b'\0\0':
            end += 2
        strings.append(fmg[offset:end].decode('utf-16le'))
    found = set()
    for g in range(groups):
        index, first, last = struct.unpack_from('<iii', fmg, 0x28 + g * 0x10)
        for k in range(last - first + 1):
            if first + k in texts:
                strings[index + k] = texts[first + k]
                found.add(first + k)
    if found != set(texts):
        raise ValueError(f'no text slot for {sorted(set(texts) - found)}')
    out = bytearray(fmg[:table + count * 8])
    for i, text in enumerate(strings):
        if text is None:
            struct.pack_into('<q', out, table + i * 8, 0)
        else:
            struct.pack_into('<q', out, table + i * 8, len(out))
            out += text.encode('utf-16le') + b'\0\0'
    struct.pack_into('<i', out, 4, len(out))
    return bytes(out)


class MissingTables(ValueError):
    """This language's archive has no mission text tables."""


def patch_menu(blob, ooz, language):
    bnd = bytearray(undcx(blob, ooz))
    entries = {e[1]: e for e in bnd_entries(bnd)}
    if NAME_FMG not in entries or INFO_FMG not in entries:
        raise MissingTables(language)
    texts = {NAME_FMG: {m.text_id: m.name for m in MISSIONS},
             INFO_FMG: {m.text_id: m.info_for(language) for m in MISSIONS}}
    for fid, text in texts.items():
        at, _, _, offset, size = entries[fid]
        data = fmg_set(bytes(bnd[offset:offset + size]), text)
        bnd += bytes(-len(bnd) % 16)
        struct.pack_into('<qqI', bnd, at + 8, len(data), len(data), len(bnd))
        bnd += data
    return dcx_dflt(bytes(bnd))


# --- Game files and install -----------------------------------------------------------------------

def nup_tree(path):
    """{romfs path: (size, offset, stored size, codec, sha256)} of a .nup's base game."""
    with open(path, 'rb') as f:
        head = f.read(0x80)
        if head[:4] != b'NUP\x01':
            return {}
        toc_offset, toc_size = struct.unpack_from('<QQ', head, 8)
        f.seek(toc_offset)
        toc = f.read(toc_size)
    magic, packages, dirs, files, pool, _ = struct.unpack_from('<IIQQQQ', toc, 0)
    if magic != 0x01434F54:
        return {}
    dir_at = 0x30 + packages * 0x40
    file_at = dir_at + dirs * 0x18
    children_at = file_at + files * 0x50

    def name(at):
        name_at, length = struct.unpack_from('<II', toc, at)
        return toc[pool + name_at:pool + name_at + length].decode('utf-8', 'replace')

    found = {}
    for p in range(packages):
        romfs = struct.unpack_from('<I', toc, 0x30 + p * 0x40 + 0x10)[0]
        if toc[0x30 + p * 0x40 + 0x18] != 0 or romfs == 0xFFFFFFFF:   # base program only
            continue
        stack = [(romfs, '')]
        while stack:
            d, prefix = stack.pop()
            _, _, first, count = struct.unpack_from('<IIII', toc, dir_at + d * 0x18)
            for c in range(count):
                child = struct.unpack_from('<I', toc, children_at + (first + c) * 4)[0]
                if child < dirs:
                    stack.append((child, prefix + name(dir_at + child * 0x18) + '/'))
                else:
                    at = file_at + (child - dirs) * 0x50
                    size, offset, stored = struct.unpack_from('<QQQ', toc, at + 8)
                    found[prefix + name(at)] = (size, offset, stored, toc[at + 0x40], toc[at + 0x20:at + 0x40].hex())
        break
    return found


class GameFiles:
    """romfs files of the game, from an unpacked folder or a .nup package."""

    def __init__(self, game_main):
        self.main = Path(game_main)
        self.nup = self.main.suffix.lower() == '.nup'
        self.tree = nup_tree(self.main) if self.nup else None
        self.romfs = None if self.nup else self.main.parents[1] / 'romfs'

    def read(self, rel):
        if not self.nup:
            path = self.romfs / rel
            return path.read_bytes() if path.is_file() else None
        record = self.tree.get(rel)
        if not record:
            return None
        size, offset, stored, codec, digest = record
        with open(self.main, 'rb') as f:
            f.seek(offset)
            data = f.read(stored)
        if codec == 1:
            data = zstandard.ZstdDecompressor().decompress(data, max_output_size=size)
        elif codec != 0:
            return None
        return data if hashlib.sha256(data).hexdigest() == digest else None

    def languages(self):
        if self.nup:
            names = {p.split('/')[1] for p in self.tree if p.startswith('msg/') and p.endswith('/menu_nt.msgbnd.dcx')}
        else:
            names = {p.parent.name for p in (self.romfs / 'msg').glob('*/menu_nt.msgbnd.dcx')}
        return sorted(names)


def install_text_mod(game, load_dir, ooz, log):
    """Write the Bot Match menu text for every language into <load>/<title>/<mod>/romfs/msg."""
    mod = Path(load_dir) / TITLE_ID / MOD_NAME
    sources = {lang: game.read(f'msg/{lang}/menu_nt.msgbnd.dcx') for lang in game.languages()}
    sources = {lang: blob for lang, blob in sources.items() if blob}
    if not sources:
        raise ValueError('the game\'s menu text was not found')
    digest = hashlib.sha256()
    for lang in sorted(sources):
        digest.update(lang.encode() + hashlib.sha256(sources[lang]).digest())
    wanted = {'version': STAMP_VERSION, 'source': digest.hexdigest(),
              'missions': [[m.row, m.name, m.info] for m in MISSIONS]}
    stamp = mod / STAMP
    try:
        if json.loads(stamp.read_text(encoding='utf-8')) == wanted:
            return False
    except (OSError, ValueError):
        pass
    log('Adding the Bot Match names and descriptions to the game text (first start only)...')
    staging = mod.with_name(MOD_NAME + '.new')
    shutil.rmtree(staging, ignore_errors=True)
    written = 0
    for lang, blob in sources.items():
        try:
            data = patch_menu(blob, ooz, lang)
        except MissingTables:  # a stub language without the mission text (araae, thath)
            continue
        target = staging / 'romfs/msg' / lang / 'menu_nt.msgbnd.dcx'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written += 1
    if not written:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError('no language has the mission text tables')
    (staging / STAMP).write_text(json.dumps(wanted), encoding='utf-8')
    shutil.rmtree(mod, ignore_errors=True)
    staging.rename(mod)
    return True


def remove_text_mod(load_dir):
    shutil.rmtree(Path(load_dir) / TITLE_ID / MOD_NAME, ignore_errors=True)


# --- Certificate archive for emulators without the online patch ---------------------------------
# The game builds the trust list for its own TLS from the system's certificate archive (system data
# 0100000000000800, ssl_TrustedCerts.bdf). Duskbloods emulator builds add the launcher's test CA to
# that list themselves; for other emulators the launcher writes an archive holding that CA into its
# test NAND: a plaintext NCA (the emulator accepts unencrypted ones) plus a .cnmt in yuzu_meta.

CERT_STORE_TITLE = 0x0100000000000800
# Every certificate id the system knows: the game asks for the ones it wants, and each answers with
# the launcher's CA.
CERT_IDS = [1, 2, 3] + list(range(1000, 1060)) + [65536]
_CERT_BLOCK = 0x1000


def cert_store_bdf(ca_der):
    """ssl_TrustedCerts.bdf: 'sslT', count, entries {id, status=trusted, size, offset}, DER data."""
    entries, data = b'', b''
    for cert_id in CERT_IDS:
        entries += struct.pack('<iiII', cert_id, 1, len(ca_der), len(CERT_IDS) * 16 + len(data))
        data += ca_der
    return b'sslT' + struct.pack('<I', len(CERT_IDS)) + entries + data


def _romfs_hash(parent, name):
    value = parent ^ 123456789
    for byte in name:
        value = (((value >> 5) | (value << 27)) & 0xFFFFFFFF) ^ byte
    return value


def romfs_one_file(name, content):
    """A RomFS image whose root holds one file."""
    empty = 0xFFFFFFFF
    name = name.encode()
    data_offset = 0x200
    data = content + bytes(-len(content) % 4)
    dir_meta = struct.pack('<IIIIII', 0, empty, empty, 0, empty, 0)  # root: first file at 0
    file_meta = struct.pack('<IIQQII', 0, empty, 0, len(content), empty, len(name)) + name
    file_meta += bytes(-len(file_meta) % 4)
    dir_hash = [empty] * 3
    dir_hash[_romfs_hash(0, b'') % 3] = 0
    file_hash = [empty] * 3
    file_hash[_romfs_hash(0, name) % 3] = 0
    tables = []
    at = data_offset + len(data)
    out = {}
    for key, blob in (('dir_hash', struct.pack('<3I', *dir_hash)), ('dir_meta', dir_meta),
                      ('file_hash', struct.pack('<3I', *file_hash)), ('file_meta', file_meta)):
        out[key] = (at, len(blob))
        tables.append(blob)
        at += len(blob)
    header = struct.pack('<Q', 0x50)
    for key in ('dir_hash', 'dir_meta', 'file_hash', 'file_meta'):
        header += struct.pack('<QQ', *out[key])
    header += struct.pack('<Q', data_offset)
    return header + bytes(data_offset - len(header)) + data + b''.join(tables)


def plain_data_nca(title_id, romfs):
    """An unencrypted NCA3 (content type Data) with one RomFS section and SHA-256 hash layers."""
    blocks = [romfs[i:i + _CERT_BLOCK] for i in range(0, len(romfs), _CERT_BLOCK)]
    hash_table = b''.join(hashlib.sha256(block).digest() for block in blocks)
    if len(hash_table) > _CERT_BLOCK:
        raise ValueError('certificate archive too large')
    data_at = 0x200  # the hash table fits in one 0x200 sector
    section = hash_table + bytes(data_at - len(hash_table)) + romfs
    section += bytes(-len(section) % 0x200)

    fs_header = bytearray(0x200)
    struct.pack_into('<HBBB', fs_header, 0, 2, 0, 2, 1)  # version, RomFs, Sha256 hash, no encryption
    fs_header[0x08:0x28] = hashlib.sha256(hash_table).digest()  # master hash
    struct.pack_into('<ii', fs_header, 0x28, _CERT_BLOCK, 2)
    struct.pack_into('<QQQQ', fs_header, 0x30, 0, len(hash_table), data_at, len(romfs))

    section_start = 0xC00  # after the header and the four FS headers
    header = bytearray(0x400)
    header[0x200:0x204] = b'NCA3'
    header[0x204] = 0  # distribution: download
    header[0x205] = 4  # content type: Data
    struct.pack_into('<QQII', header, 0x208, section_start + len(section), title_id, 0, 0x000D0000)
    struct.pack_into('<II', header, 0x240, section_start // 0x200, (section_start + len(section)) // 0x200)
    header[0x280:0x2A0] = hashlib.sha256(bytes(fs_header)).digest()
    return bytes(header) + bytes(fs_header) + bytes(0x200 * 3) + section


def cnmt_for(title_id, nca_id, nca):
    """A SystemData content meta listing one Data NCA."""
    head = struct.pack('<QIBBHHHBxxBI4x', title_id, 0, 0x02, 0, 0x10, 1, 0, 0, 0, 0)
    record = hashlib.sha256(nca).digest() + nca_id + len(nca).to_bytes(6, 'little') + bytes([2, 0])
    return head + bytes(0x10) + record


def install_cert_store(nand, ca_der):
    """Write the certificate archive holding `ca_der` into `nand` (the launcher's test NAND)."""
    registered = Path(nand) / 'system/Contents/registered'
    nca = plain_data_nca(CERT_STORE_TITLE, romfs_one_file('ssl_TrustedCerts.bdf', cert_store_bdf(ca_der)))
    nca_id = hashlib.sha256(nca).digest()[:16]
    meta_dir = registered / 'yuzu_meta'
    meta_dir.mkdir(parents=True, exist_ok=True)
    stamp = meta_dir / 'playdb-certs.json'
    try:
        old = json.loads(stamp.read_text())
    except (OSError, ValueError):
        old = {}
    if old.get('id') == nca_id.hex() and (registered / f'{nca_id.hex()}.nca').exists():
        return
    if old.get('id'):  # a previous archive (another CA)
        for path in (registered / f"{old['id']}.nca", meta_dir / f"{old['id']}.cnmt"):
            path.unlink(missing_ok=True)
    (registered / f'{nca_id.hex()}.nca').write_bytes(nca)
    (meta_dir / f'{nca_id.hex()}.cnmt').write_bytes(cnmt_for(CERT_STORE_TITLE, nca_id, nca))
    stamp.write_text(json.dumps({'id': nca_id.hex()}))


def remove_cert_store(nand):
    meta_dir = Path(nand) / 'system/Contents/registered/yuzu_meta'
    try:
        old = json.loads((meta_dir / 'playdb-certs.json').read_text())
    except (OSError, ValueError):
        return
    for path in (meta_dir.parent / f"{old.get('id')}.nca", meta_dir / f"{old.get('id')}.cnmt",
                 meta_dir / 'playdb-certs.json'):
        path.unlink(missing_ok=True)
