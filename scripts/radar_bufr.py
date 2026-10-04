"""DÃ©codeur BUFR minimal pour les produits radar polaires MÃ©tÃ©o-France (PAG / PAM).

Lit les tables B/D maÃ®tresses (version 11) et locales (centre 85, version 12) fournies par
l'utilitaire OPERA de MÃ©tÃ©o-France, dÃ©veloppe les sÃ©quences (opÃ©rateurs 2 01, 2 02, rÃ©plications
1 XX YYY) et retourne la liste ordonnÃ©e (descripteur, nom, valeur). Les grandes matrices
d'octets sont renvoyÃ©es sous forme de tableaux numpy.
"""
from __future__ import annotations

import gzip
import os
from pathlib import Path

import numpy as np

TABLES = Path(os.environ.get("BUFR_TABLES", Path(__file__).resolve().parent.parent / "config" / "bufr"))


def _split(line: str) -> list[str]:
    return [p.strip() for p in line.rstrip("\r\n").split(";")]


def load_tables(master: int = 11, local: int = 12):
    elems: dict[tuple[int, int], tuple[str, str, int, int, int]] = {}
    seqs: dict[tuple[int, int], list[tuple[int, int, int]]] = {}

    def read_b(path: Path) -> None:
        if not path.exists():
            return
        for line in path.read_text(encoding="latin1").splitlines():
            p = _split(line)
            if len(p) < 8 or p[0] != "0":
                continue
            try:
                x, y = int(p[1]), int(p[2])
                elems[(x, y)] = (p[3], p[4], int(p[5]), int(p[6]), int(p[7]))
            except ValueError:
                continue

    def read_d(path: Path) -> None:
        if not path.exists():
            return
        cur: tuple[int, int] | None = None
        for line in path.read_text(encoding="latin1").splitlines():
            p = _split(line)
            if len(p) < 6:
                continue
            if p[0] == "3" and p[1] and p[2]:
                cur = (int(p[1]), int(p[2]))
                seqs[cur] = []
            if cur is None or not p[3] or not p[4] or not p[5]:
                continue
            try:
                seqs[cur].append((int(p[3]), int(p[4]), int(p[5])))
            except ValueError:
                continue

    read_b(TABLES / f"bufrtabb_{master}.csv")
    read_b(TABLES / f"localtabb_85_{local}.csv")
    read_d(TABLES / f"bufrtabd_{master}.csv")
    read_d(TABLES / f"localtabd_85_{local}.csv")
    return elems, seqs


class Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        end = self.pos + n
        first, last = self.pos >> 3, (end + 7) >> 3
        chunk = int.from_bytes(self.data[first:last], "big")
        shift = (last << 3) - end
        self.pos = end
        return (chunk >> shift) & ((1 << n) - 1)

    def read_bytes(self, count: int) -> np.ndarray:
        assert self.pos % 8 == 0
        start = self.pos >> 3
        self.pos += count * 8
        return np.frombuffer(self.data, dtype=np.uint8, count=count, offset=start)


def split_messages(raw: bytes):
    pos = 0
    while True:
        i = raw.find(b"BUFR", pos)
        if i < 0:
            return
        length = int.from_bytes(raw[i + 4 : i + 7], "big")
        yield raw[i : i + length]
        pos = i + length


_CACHE: dict[tuple[int, int], tuple] = {}


def tables_for(master: int, local: int):
    key = (master, local)
    if key not in _CACHE:
        _CACHE[key] = load_tables(master, local)
    return _CACHE[key]


class _Stop(Exception):
    pass


def parse_message(msg: bytes, tables=None, stop_label: str | None = None):
    s1_len0 = int.from_bytes(msg[8:11], "big")
    tables = tables or tables_for(msg[8 + 10], msg[8 + 11]) if msg[7] < 4 else tables_for(msg[8 + 14], msg[8 + 15])
    elems, seqs = tables
    edition = msg[7]
    s1_len = int.from_bytes(msg[8:11], "big")
    s1 = msg[8 : 8 + s1_len]
    p = 8 + s1_len
    if edition >= 4:
        flag = s1[9]
    else:
        flag = s1[7]
    if flag & 0x80:
        p += int.from_bytes(msg[p : p + 3], "big")
    s3_len = int.from_bytes(msg[p : p + 3], "big")
    s3 = msg[p : p + s3_len]
    descs = []
    for q in range(7, s3_len - 1, 2):
        v = int.from_bytes(s3[q : q + 2], "big")
        descs.append((v >> 14, (v >> 8) & 63, v & 255))
    p += s3_len
    s4_len = int.from_bytes(msg[p : p + 3], "big")
    bits = Bits(msg[p + 4 : p + s4_len])
    out: list[tuple[str, str, object]] = []

    def run(seq: list[tuple[int, int, int]], width_add: int, scale_add: int):
        i = 0
        while i < len(seq):
            f, x, y = seq[i]
            i += 1
            if f == 0:
                name, unit, scale, ref, width = elems[(x, y)]
                label = f"0{x:02d}{y:03d}"
                if unit.upper().startswith("CCITT"):
                    n = width // 8
                    s = bits.read_bytes(n).tobytes().decode("latin1").strip("\x00 ")
                    out.append((label, name, s))
                    continue
                w = width + (width_add if unit not in ("Code table", "Flag table") else 0)
                sc = scale + scale_add
                raw = bits.read(w)
                if raw == (1 << w) - 1 and w > 1:
                    out.append((label, name, None))
                else:
                    out.append((label, name, (raw + ref) / (10 ** sc) if sc else raw + ref))
                if stop_label and label == stop_label:
                    raise _Stop
            elif f == 1:
                count = y
                if y == 0:
                    ff, fx, fy = seq[i]
                    i += 1
                    fname, _, _, _, fw = elems[(fx, fy)]
                    count = bits.read(fw)
                    out.append((f"0{fx:02d}{fy:03d}", fname, count))
                block = seq[i : i + x]
                i += x
                # matrice d'octets : un seul élément numérique de 8 ou 16 bits (après opérateur 2 01) répété
                if len(block) == 1 and block[0][0] == 0 and (block[0][1], block[0][2]) in elems:
                    bname, bunit, bscale, bref, bwidth = elems[(block[0][1], block[0][2])]
                    bw = bwidth + (width_add if bunit not in ("Code table", "Flag table") else 0)
                    if bw == 8 and bunit.upper() != "CCITT IA5":
                        arr = bits.read_bytes(count)
                        out.append((f"0{block[0][1]:02d}{block[0][2]:03d}", "matrix8", arr))
                        continue
                for _ in range(count):
                    run(block, width_add, scale_add)
            elif f == 2:
                if x == 1:
                    width_add = 0 if y == 0 else y - 128
                elif x == 2:
                    scale_add = 0 if y == 0 else y - 128
                # autres opÃ©rateurs ignorÃ©s
            elif f == 3:
                run(seqs[(x, y)], width_add, scale_add)

    try:
        run(descs, 0, 0)
    except _Stop:
        pass
    return out


def read_file(path: str | Path, tables=None):
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return [parse_message(m, tables) for m in split_messages(raw)]


def tail_matrix(msg: bytes, count: int, dtype: str = "u1") -> np.ndarray:
    """Matrice de `count` valeurs stockée en fin de section 4 (tables non nécessaires)."""
    edition = msg[7]
    s1_len = int.from_bytes(msg[8:11], "big")
    flag = msg[8 + (9 if edition >= 4 else 7)]
    p = 8 + s1_len
    if flag & 0x80:
        p += int.from_bytes(msg[p : p + 3], "big")
    p += int.from_bytes(msg[p : p + 3], "big")
    n4 = int.from_bytes(msg[p : p + 3], "big")
    body = msg[p + 4 : p + n4]
    size = np.dtype(dtype).itemsize * count
    return np.frombuffer(body[len(body) - size :], dtype=dtype)


def elevation(msg: bytes) -> float | None:
    for label, _name, value in parse_message(msg, stop_label="007021"):
        if label == "007021":
            return float(value) if value is not None else None
    return None
