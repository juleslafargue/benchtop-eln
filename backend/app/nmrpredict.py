"""NMR chemical-shift prediction from NMRShiftDB2.

Method: each atom's surroundings are described sphere by sphere (the HOSE-code idea of
Bremser 1978, implemented as RDKit environment hashes): sphere 0 is the atom itself, sphere 1
adds its neighbours, and so on up to sphere 4. For every assigned atom in NMRShiftDB2 the observed shift is added to running
statistics for each of its codes. To predict, we start from the 1-sphere estimate and refine it
with each more specific sphere that has data, weighting each by how many examples it rests on
(a sphere seen once nudges the estimate; one seen fifty times decides it). Per-code values are
outlier-trimmed means, so a mis-assigned record in the database can't drag a prediction off.
On molecules held out of the table this blending cut the mean error by about a tenth compared
with "use the most specific sphere" (see `python -m app.nmrdata --benchmark`).

Confirmed assignments from your own lab's signed experiments are layered on top (see `lab_*`).

13C shifts are keyed by the carbon's environment; 1H shifts by the environment of the heavy
atom carrying the hydrogen.

Data: NMRShiftDB2 (https://nmrshiftdb.nmr.uni-koeln.de), nmrshiftdb2 Data License. The table
built from it is a derived database under the same licence (see data/NMRSHIFTDB2_LICENSE.md).
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from rdkit import Chem, RDLogger

from .config import settings

RDLogger.DisableLog("rdApp.*")

MAX_R = 4
NUC = {"13C": 13, "1H": 1}
RANGE = {13: (-20.0, 240.0), 1: (-3.0, 17.0)}
BUNDLED = Path(__file__).parent / "data" / "nmr_shifts.sqlite"
_BOND = {Chem.BondType.SINGLE: "-", Chem.BondType.DOUBLE: "=", Chem.BondType.TRIPLE: "#", Chem.BondType.AROMATIC: ":"}


def user_table() -> Path:
    return settings.data_dir / "nmr_shifts.sqlite"


def table_path() -> Optional[Path]:
    """The table built on this machine from the latest download wins over the one shipped with Benchtop."""
    for p in (user_table(), BUNDLED):
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


# ---------------------------------------------------------------- environment codes

def _h64(data: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big", signed=True)


_BOND = {Chem.BondType.SINGLE: 1, Chem.BondType.DOUBLE: 2, Chem.BondType.TRIPLE: 3, Chem.BondType.AROMATIC: 4}


def environments(mol: Chem.Mol, max_r: int = MAX_R) -> list[list[int]]:
    """For each atom of a hydrogen-suppressed molecule: [env_r0, env_r1, ..., env_rmax].
    Each radius folds in the previous radius of every neighbour (with the bond order), so a code
    captures exact connectivity out to that many bonds. Stable across Python and RDKit versions.
    (On held-out NMRShiftDB2 molecules this beat a looser sphere-list code: 13C median error
    1.24 vs 1.35 ppm.)"""
    n = mol.GetNumAtoms()
    cur = []
    for a in mol.GetAtoms():
        inv = (a.GetAtomicNum(), int(a.GetIsAromatic()), a.GetDegree(), a.GetTotalNumHs(), a.GetFormalCharge() + 8,
               int(a.IsInRing()), int(a.IsInRingSize(3) or a.IsInRingSize(4)))
        cur.append(_h64(bytes(inv)))
    out = [[c] for c in cur]
    nbrs = [[(_BOND.get(b.GetBondType(), 1), b.GetOtherAtomIdx(a.GetIdx())) for b in a.GetBonds()] for a in mol.GetAtoms()]
    for _ in range(max_r):
        nxt = []
        for i in range(n):
            parts = sorted((bo, cur[j]) for bo, j in nbrs[i])
            payload = cur[i].to_bytes(8, "big", signed=True) + b"".join(bytes([bo]) + h.to_bytes(8, "big", signed=True) for bo, h in parts)
            nxt.append(_h64(payload))
        cur = nxt
        for i in range(n):
            out[i].append(cur[i])
    return out


def heavy_envs(mol: Chem.Mol) -> Optional[dict[int, list[int]]]:
    """Environment codes keyed by the atom indices of `mol` (which may contain explicit hydrogens)."""
    for a in mol.GetAtoms():
        a.SetIntProp("_orig", a.GetIdx())
    try:
        heavy = Chem.RemoveHs(mol)
    except Exception:  # noqa: BLE001 - a few records have odd valences
        return None
    envs = environments(heavy)
    return {a.GetIntProp("_orig"): envs[a.GetIdx()] for a in heavy.GetAtoms() if a.HasProp("_orig")}


# ---------------------------------------------------------------- building the table

def read_sd(path: Path) -> Iterable[Chem.Mol]:
    """NMRShiftDB2 molecules with their real hydrogen counts.

    The export comes from CDK with hydrogens stripped and each atom's valence field set, which
    RDKit would honour literally: every carbon would read as a radical with no hydrogens, so CH3,
    CH2, CH and C would all look alike. We drop those fields and let RDKit work out the hydrogens."""
    for mol in Chem.SDMolSupplier(str(path), removeHs=False, sanitize=False, strictParsing=False):
        if mol is None:
            continue
        for a in mol.GetAtoms():
            a.SetNoImplicit(False)
            a.SetNumRadicalElectrons(0)
        try:
            Chem.SanitizeMol(mol)
        except Exception:  # noqa: BLE001 - a few records have impossible valences
            continue
        yield mol


def _parse_peaks(value: str) -> list[tuple[float, int]]:
    out = []
    for item in str(value).split("|"):
        bits = item.strip().split(";")
        if len(bits) < 3:
            continue
        try:
            out.append((float(bits[0]), int(bits[-1])))
        except ValueError:
            continue
    return out


def iter_observations(mol: Chem.Mol) -> Iterable[tuple[int, int, float]]:
    """(nucleus, heavy-atom index, shift) for every assigned peak in one NMRShiftDB2 record.
    Several spectra of one molecule are averaged per atom so a well-studied compound doesn't dominate."""
    acc: dict[tuple[int, int], list[float]] = {}
    n_atoms = mol.GetNumAtoms()
    for key in mol.GetPropNames():
        if key.startswith("Spectrum 13C"):
            nuc = 13
        elif key.startswith("Spectrum 1H"):
            nuc = 1
        else:
            continue
        lo, hi = RANGE[nuc]
        for shift, i in _parse_peaks(mol.GetProp(key)):
            if not (lo <= shift <= hi) or not (0 <= i < n_atoms):
                continue
            a = mol.GetAtomWithIdx(i)
            if nuc == 13 and a.GetAtomicNum() != 6:
                continue
            if nuc == 1:
                if a.GetAtomicNum() == 1:  # assigned to an explicit H: key by the atom it sits on
                    nbrs = a.GetNeighbors()
                    if not nbrs:
                        continue
                    a = nbrs[0]
                if a.GetTotalNumHs(includeNeighbors=True) == 0:
                    continue
            acc.setdefault((nuc, a.GetIdx()), []).append(shift)
    for (nuc, i), vals in acc.items():
        yield nuc, i, sum(vals) / len(vals)


BLEND_K = 0.25  # pseudo-count for blending a sphere with the coarser estimate (tuned on held-out molecules)


def trimmed_mean(v: list[float], nuc: int) -> float:
    """Mean of the values within ~3 robust standard deviations of the median (drops mis-assignments)."""
    if len(v) < 4:
        return sorted(v)[1] if len(v) == 3 else sum(v) / len(v)
    srt = sorted(v)
    med = srt[len(srt) // 2] if len(srt) % 2 else (srt[len(srt) // 2 - 1] + srt[len(srt) // 2]) / 2
    dev = sorted(abs(x - med) for x in v)
    mad = dev[len(dev) // 2]
    lim = max(3 * 1.4826 * mad, 0.5 if nuc == 13 else 0.05)
    kept = [x for x in v if abs(x - med) <= lim] or v
    return sum(kept) / len(kept)


def build(sd_files: list[Path], out: Path, source: str, *, progress=None, holdout: float = 0.0) -> dict[str, Any]:
    """Compile NMRShiftDB2 SD files into a shift table. `holdout` keeps a fraction of molecules out
    (by a stable hash) for an honest accuracy estimate."""
    values: dict[tuple[int, int, int], list[float]] = {}  # (nuc, r, key) -> observed shifts
    held: list[tuple[Chem.Mol, list[tuple[int, int, float]]]] = []
    sha = hashlib.sha256()
    n_mol = n_obs = 0
    nuc_obs = {1: 0, 13: 0}
    for path in sd_files:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
        for mol in read_sd(path):
            obs = list(iter_observations(mol))
            if not obs:
                continue
            n_mol += 1
            if progress and n_mol % 2000 == 0:
                progress(n_mol)
            if holdout and (int(hashlib.md5(Chem.MolToSmiles(mol).encode()).hexdigest(), 16) % 1000) < holdout * 1000:
                held.append((mol, obs))
                continue
            envs = heavy_envs(mol)
            if envs is None:
                continue
            for nuc, i, shift in obs:
                codes = envs.get(i)
                if codes is None:
                    continue
                for r in range(1, MAX_R + 1):
                    values.setdefault((nuc, r, codes[r]), []).append(shift)
                n_obs += 1
                nuc_obs[nuc] += 1
    tmp = out.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE stats (nuc INTEGER, r INTEGER, key INTEGER, n INTEGER, m REAL, sd REAL, mn REAL, mx REAL, PRIMARY KEY (nuc, r, key)) WITHOUT ROWID")
    con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")

    def rows():
        for (nuc, r, k), v in values.items():
            if r == MAX_R and len(v) < 2:
                continue  # single examples at 4 spheres add size for little gain (3-sphere ones are kept: they help)
            n = len(v)
            mean = sum(v) / n
            sd = math.sqrt(max(0.0, sum(x * x for x in v) / n - mean * mean))
            yield nuc, r, k, n, round(trimmed_mean(v, nuc), 3), round(sd, 3), min(v), max(v)
    con.executemany("INSERT INTO stats VALUES (?,?,?,?,?,?,?,?)", rows())
    meta = {"source": source, "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "molecules": str(n_mol - len(held)), "assignments_13C": str(nuc_obs[13]), "assignments_1H": str(nuc_obs[1]),
            "input_sha256": sha.hexdigest(), "method": f"HOSE-style sphere codes, radius 1-{MAX_R}, blended (k={BLEND_K}), trimmed means",
            "format": "2"}
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()
    con.execute("VACUUM")
    con.close()
    tmp.replace(out)
    reset_cache()
    result = {**meta, "rows": len(values)}
    if held:
        result["holdout"] = evaluate(held, out)
    return result


def _atom_class(mol: Chem.Mol, i: int, nuc: int) -> str:
    a = mol.GetAtomWithIdx(i)
    carbonyl = any(b.GetBondType() == Chem.BondType.DOUBLE and b.GetOtherAtom(a).GetAtomicNum() == 8 for b in a.GetBonds())
    het = any(n.GetAtomicNum() not in (1, 6) for n in a.GetNeighbors())
    if nuc == 13:
        if carbonyl:
            return "C=O"
        if a.GetIsAromatic():
            return "aromatic CH" if a.GetTotalNumHs() else "aromatic C (substituted)"
        hyb = a.GetHybridization()
        if hyb == Chem.HybridizationType.SP2:
            return "alkene C"
        if hyb == Chem.HybridizationType.SP:
            return "alkyne / nitrile C"
        return "sp3 C next to O/N/X" if het else f"sp3 C (CH{a.GetTotalNumHs()})" if a.GetTotalNumHs() < 4 else "CH4"
    if a.GetAtomicNum() != 6:
        return "OH / NH"
    if carbonyl:
        return "aldehyde H"
    if a.GetIsAromatic():
        return "aromatic H"
    if a.GetHybridization() == Chem.HybridizationType.SP2:
        return "vinyl H"
    return "H on C next to O/N/X" if het else "aliphatic H"


def evaluate(held, table: Path) -> dict[str, Any]:
    """Errors on molecules the table never saw: overall, by confidence level and by kind of atom."""
    p = Predictor(table)
    err: dict[int, list[float]] = {1: [], 13: []}
    by_conf: dict[tuple[int, str], list[float]] = {}
    by_cls: dict[tuple[int, str], list[float]] = {}
    missing = {1: 0, 13: 0}
    for mol, obs in held:
        envs = heavy_envs(mol) or {}
        for nuc, i, shift in obs:
            pr = p.lookup(nuc, envs[i]) if i in envs else None
            if not pr:
                missing[nuc] += 1
                continue
            d = abs(pr["shift"] - shift)
            err[nuc].append(d)
            by_conf.setdefault((nuc, confidence(pr, nuc)), []).append(d)
            by_cls.setdefault((nuc, _atom_class(mol, i, nuc)), []).append(d)

    def summ(e: list[float], nuc: int) -> dict[str, Any]:
        e = sorted(e)
        tol = 2 if nuc == 13 else 0.3
        return {"mae": round(sum(e) / len(e), 3), "median": round(e[len(e) // 2], 3), "p90": round(e[int(len(e) * 0.9)], 2),
                f"within_{tol}ppm": round(sum(x <= tol for x in e) / len(e), 3), "n": len(e)}
    out = {}
    for nuc, e in err.items():
        if not e:
            continue
        label = "13C" if nuc == 13 else "1H"
        out[label] = {**summ(e, nuc), "coverage": round(len(e) / (len(e) + missing[nuc]), 3), "molecules": len(held),
                      "by_confidence": {c: summ(v, nuc) for (n2, c), v in sorted(by_conf.items()) if n2 == nuc},
                      "by_atom": {c: summ(v, nuc) for (n2, c), v in sorted(by_cls.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])) if n2 == nuc}}
    return out


# ---------------------------------------------------------------- predicting

class Predictor:
    def __init__(self, path: Path):
        self.path = path
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.meta = dict(self.con.execute("SELECT k, v FROM meta").fetchall())
        self.v2 = self.meta.get("format", "1") >= "2"
        self.lock = threading.Lock()

    def _row(self, nuc: int, r: int, key: int) -> Optional[tuple[int, float, float, float, float]]:
        """(n, robust mean, sd, min, max) for one code."""
        if self.v2:
            return self.con.execute("SELECT n, m, sd, mn, mx FROM stats WHERE nuc=? AND r=? AND key=?", (nuc, r, key)).fetchone()
        row = self.con.execute("SELECT n, s, ss, mn, mx FROM stats WHERE nuc=? AND r=? AND key=?", (nuc, r, key)).fetchone()
        if not row:
            return None
        n, s_, ss, mn, mx = row
        mean = s_ / n
        return n, mean, math.sqrt(max(0.0, ss / n - mean * mean)), mn, mx

    def lookup(self, nuc: int, codes: list[int], lab: Optional[list[tuple[int, float]]] = None) -> Optional[dict[str, Any]]:
        """Blend the spheres from coarse to specific; `lab` adds this lab's confirmed shifts on top.
        `radius` and `n` describe the most specific sphere used (they drive the confidence label)."""
        with self.lock:
            est = None
            info: dict[str, Any] = {}
            for r in range(1, MAX_R + 1):
                row = self._row(nuc, r, codes[r])
                if not row:
                    break
                n, m, sd, mn, mx = row
                est = m if est is None else (n * m + BLEND_K * est) / (n + BLEND_K)
                info = {"n": n, "radius": r, "min": mn, "max": mx, "sd": sd if n > 1 or not info else info["sd"]}
        if est is None and not lab:
            return None
        out = {"shift": None, "sd": round(info.get("sd", 0.0), 2), "n": info.get("n", 0), "radius": info.get("radius", 0),
               "min": round(info["min"], 2) if info else None, "max": round(info["max"], 2) if info else None}
        if lab:  # confirmed in this lab: same instrument, solvent habits and compound families, so it counts for a lot
            n_lab = len(lab)
            m_lab = sum(v for _, v in lab) / n_lab
            depth = max(r for r, _ in lab)
            est = m_lab if est is None else (n_lab * m_lab + LAB_K * est) / (n_lab + LAB_K)
            out.update(lab_n=n_lab, lab_radius=depth)
            if out["radius"] < depth:
                out["radius"], out["n"] = depth, max(out["n"], n_lab)
        out["shift"] = round(est, 2)
        return out

    def has(self, nuc: int) -> bool:
        with self.lock:
            return self.con.execute("SELECT 1 FROM stats WHERE nuc=? LIMIT 1", (nuc,)).fetchone() is not None


_pred: Optional[Predictor] = None
_pred_path: Optional[Path] = None


def reset_cache() -> None:
    global _pred, _pred_path
    _pred, _pred_path = None, None


def predictor() -> Optional[Predictor]:
    global _pred, _pred_path
    p = table_path()
    if p is None:
        return None
    if _pred is None or _pred_path != p:
        _pred, _pred_path = Predictor(p), p
    return _pred


class NotReady(Exception):
    """No shift table (or no data for this nucleus) yet."""


def confidence(r: dict[str, Any], nuc: int) -> str:
    if r.get("lab_n"):
        return "high"
    if r["radius"] >= 3 and r["n"] >= 3:
        return "high"
    if r["radius"] >= 2:
        return "medium"
    return "low"


MAX_HEAVY_ATOMS = 200  # far beyond a small molecule; a peptide of about 25 residues
MAX_SMILES = 2000     # read no further than that could need (with stereo marks and ring numbers)


def predict(smiles: str, nucleus: str = "1H", use_lab: bool = True) -> dict[str, Any]:
    """Predicted shifts for each symmetry-distinct group of carbons (13C) or of H-bearing atoms (1H).
    Atom indices refer to the returned SMILES (the main fragment), so the depiction can number them."""
    if nucleus not in NUC:
        raise ValueError("nucleus must be 1H or 13C")
    nuc = NUC[nucleus]
    # a molecule of the size the shift table describes (0.36.4 had no limit: the time grew with the square of the size, a
    # 10,000-character SMILES held a worker for 7 s, and any signed-in person could send one)
    if len(smiles or "") > MAX_SMILES:
        raise ValueError(f"That structure is too large to predict: Benchtop predicts shifts for molecules of up to "
                         f"{MAX_HEAVY_ATOMS} atoms other than hydrogen.")
    mol = Chem.MolFromSmiles(smiles or "")
    if mol is None:
        raise ValueError("Couldn't read that structure")
    frags = Chem.GetMolFrags(mol, asMols=True)
    if not frags:
        raise ValueError("Couldn't read that structure")
    main = max(frags, key=lambda f: f.GetNumHeavyAtoms())
    if main.GetNumHeavyAtoms() > MAX_HEAVY_ATOMS:
        raise ValueError(f"That structure is too large to predict ({main.GetNumHeavyAtoms():,} atoms other than hydrogen): "
                         f"Benchtop predicts shifts for molecules of up to {MAX_HEAVY_ATOMS} atoms other than hydrogen.")
    smi = Chem.MolToSmiles(main)
    mol = Chem.MolFromSmiles(smi)  # indices now match a fresh parse of `smi`
    p = predictor()
    if p is None:
        raise NotReady("Prediction data hasn't been downloaded yet")
    if not p.has(nuc):
        raise NotReady(f"The prediction table has no {nucleus} data yet; it will after the first download of the full NMRShiftDB2 export")
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    envs = environments(mol)
    lab = lab_shifts(nuc, envs) if use_lab else {}
    dia = (diastereotopic_ch2(mol) | geminal_alkene_ch2(mol)) if nuc == 1 else set()
    groups: dict[int, dict[str, Any]] = {}
    for a in mol.GetAtoms():
        i, nh = a.GetIdx(), a.GetTotalNumHs()
        if (nuc == 13 and a.GetAtomicNum() != 6) or (nuc == 1 and nh == 0):
            continue
        g = groups.get(ranks[i])
        if g is None:
            r = p.lookup(nuc, envs[i], lab.get(i))
            g = groups[ranks[i]] = {
                "atoms": [], "nH": 0, "element": a.GetSymbol(), "aromatic": a.GetIsAromatic(),
                "exchangeable": nuc == 1 and a.GetSymbol() in ("O", "N", "S"),
                "shift": r["shift"] if r else None, "confidence": confidence(r, nuc) if r else "none",
                "vicinal_h": sum(n.GetTotalNumHs() for n in a.GetNeighbors() if n.GetSymbol() == "C") if nuc == 1 else None,
                **({"sd": r["sd"], "n": r["n"], "sphere": r["radius"], "lo": r["min"], "hi": r["max"]} if r else {}),
                **({"lab_n": r["lab_n"]} if r and r.get("lab_n") else {}),
            }
        g["atoms"].append(i)
        g["nH"] += nh if nuc == 1 else 0
    out = []
    for g in groups.values():
        # a CH2 near a stereocentre has two different protons: list them separately so each can match its own signal
        if g["nH"] == 2 and len(g["atoms"]) == 1 and g["atoms"][0] in dia:
            for half in ("a", "b"):
                out.append({**g, "nH": 1, "diastereotopic": half})
        else:
            out.append(g)
    out.sort(key=lambda g: -(g["shift"] if g["shift"] is not None else -1e9))
    return {"nucleus": nucleus, "smiles": smi, "groups": out,
            "covered": sum(1 for g in out if g["shift"] is not None), "total": len(out),
            "source": {"source": p.meta.get("source"), "built_at": p.meta.get("built_at"), "molecules": p.meta.get("molecules"),
                       "assigned_atoms": p.meta.get(f"assignments_{nucleus}"), "bundled": p.path == BUNDLED,
                       "lab_groups": sum(1 for g in out if g.get("lab_n"))}}


def diastereotopic_ch2(mol: Chem.Mol) -> set[int]:
    """CH2 carbons whose two protons are diastereotopic: swapping one H for D would make the carbon a
    stereocentre, and there's already a stereocentre close by (within three bonds, or in the same ring).
    Remote stereocentres split CH2 protons too little to see, so they're ignored."""
    centres = [i for i, _ in Chem.FindMolChiralCenters(mol, includeUnassigned=True, useLegacyImplementation=False)]
    if not centres:
        return set()
    dist = Chem.GetDistanceMatrix(mol)
    ri = mol.GetRingInfo()
    out = set()
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    for a in mol.GetAtoms():
        if a.GetAtomicNum() != 6 or a.GetTotalNumHs() != 2 or a.GetDegree() != 2 or a.GetIsAromatic():
            continue
        i = a.GetIdx()
        n1, n2 = (nb.GetIdx() for nb in a.GetNeighbors())
        if ranks[n1] == ranks[n2]:
            continue  # both sides identical: the two H are enantiotopic or homotopic
        near = any(c != i and (dist[i][c] <= 3 or any(i in ring and c in ring for ring in ri.AtomRings())) for c in centres)
        if near:
            out.add(i)
    return out


def geminal_alkene_ch2(mol: Chem.Mol) -> set[int]:
    """=CH2 carbons whose two protons differ: one is cis and one trans to a substituent, as in styrene or methyl
    acrylate (two signals, about 5.2 and 5.7 ppm in styrene). Not when the other end carries two identical groups
    (isobutylene's =CH2 is one signal)."""
    ranks = list(Chem.CanonicalRankAtoms(mol, breakTies=False))
    out = set()
    for a in mol.GetAtoms():
        if a.GetAtomicNum() != 6 or a.GetTotalNumHs() != 2 or a.GetIsAromatic():
            continue
        for b in a.GetBonds():
            if b.GetBondType() != Chem.BondType.DOUBLE:
                continue
            other = b.GetOtherAtom(a)
            subs = [ranks[n.GetIdx()] for n in other.GetNeighbors() if n.GetIdx() != a.GetIdx()] + [-1] * other.GetTotalNumHs()
            if len(subs) == 2 and subs[0] != subs[1]:
                out.add(a.GetIdx())
    return out


# ---------------------------------------------------------------- your lab's confirmed assignments

LAB_K = 0.5  # one confirmed lab shift outweighs a NMRShiftDB2 estimate two to one


def lab_shifts(nuc: int, envs: list[list[int]]) -> dict[int, list[tuple[int, float]]]:
    """Confirmed shifts from signed experiments, per atom: the most specific sphere (4, else 3) that has any."""
    try:
        from sqlmodel import Session, select

        from .db import engine
        from .models import LabShift
        keys = {codes[r] for codes in envs for r in (3, 4)}
        with Session(engine) as s:
            rows = s.exec(select(LabShift.r, LabShift.key, LabShift.shift).where(LabShift.nuc == nuc, LabShift.key.in_(keys))).all()
    except Exception:  # noqa: BLE001 - no database (scripts, tests of the table alone)
        return {}
    by: dict[tuple[int, int], list[float]] = {}
    for r, key, shift in rows:
        by.setdefault((r, key), []).append(shift)
    out = {}
    for i, codes in enumerate(envs):
        for r in (4, 3):
            v = by.get((r, codes[r]))
            if v:
                out[i] = [(r, x) for x in v]
                break
    return out


def lab_record(session, exp) -> int:
    """Store the confirmed assignments of a signed experiment's spectra. Returns how many shifts."""
    from sqlmodel import select

    from .models import Entry, EntryKind, LabShift
    lab_forget(session, exp.id)
    n = 0
    for e in session.exec(select(Entry).where(Entry.experiment_id == exp.id, Entry.deleted == False)).all():  # noqa: E712
        items = []
        if e.kind == EntryKind.characterization:
            items = [it.get("analysis") for it in (e.content or {}).get("items", [])]
        elif e.kind == EntryKind.file:
            items = list(((e.content or {}).get("analyses") or {}).values())
        for an in items:
            asg = (an or {}).get("assignments") if isinstance(an, dict) else None
            if not asg or asg.get("nucleus") not in NUC:
                continue
            mol = Chem.MolFromSmiles(asg.get("smiles") or "")
            if mol is None:
                continue
            nuc = NUC[asg["nucleus"]]
            envs = environments(mol)
            for item in asg.get("items", []):
                for i in item.get("atoms", []):
                    if 0 <= i < len(envs):
                        for r in (3, 4):
                            session.add(LabShift(experiment_id=exp.id, nuc=nuc, r=r, key=envs[i][r], shift=item["shift"],
                                                 solvent=str(asg.get("solvent") or "")[:20]))
                        n += 1
    return n


def lab_forget(session, exp_id: int) -> None:
    from sqlmodel import delete

    from .models import LabShift
    session.exec(delete(LabShift).where(LabShift.experiment_id == exp_id))


def lab_summary() -> dict[str, int]:
    try:
        from sqlalchemy import func
        from sqlmodel import Session, select

        from .db import engine
        from .models import LabShift
        with Session(engine) as s:
            n = s.exec(select(func.count()).select_from(LabShift).where(LabShift.r == 4)).one()
            exps = s.exec(select(func.count(func.distinct(LabShift.experiment_id)))).one()
        return {"shifts": int(n), "experiments": int(exps)}
    except Exception:  # noqa: BLE001
        return {"shifts": 0, "experiments": 0}


def status() -> dict[str, Any]:
    from . import nmrdata
    pr = nmrdata.progress()
    stage = pr.get("stage")
    msg = ""
    if stage == "downloading":
        msg = f"Downloading NMRShiftDB2: {pr.get('done_mb', 0)} MB" + (f" of {pr['total_mb']} MB" if pr.get("total_mb") else "")
    elif stage == "building":
        msg = f"Building the prediction table: {pr.get('molecules', 0):,} structures read"
    elif stage == "checking":
        msg = "Checking NMRShiftDB2 for a newer export"
    p = predictor()
    out = {"available": p is not None, "updating": bool(pr.get("running")), "progress": msg, "lab": lab_summary(),
           "last_error": pr.get("last_error") or "", "refresh_days": 30, "auto_update": settings.nmr_auto_update,
           "last_check": pr.get("last_check"), "last_update": pr.get("last_update")}
    if p is not None:
        out["meta"] = {**p.meta, "table": "downloaded" if p.path != BUNDLED else "bundled with Benchtop"}
        out["has_13C"], out["has_1H"] = p.has(13), p.has(1)
        try:
            built = datetime.fromisoformat(p.meta["built_at"].replace("Z", "+00:00"))
            out["age_days"] = round((datetime.now(timezone.utc) - built).total_seconds() / 86400, 1)
        except (KeyError, ValueError):
            out["age_days"] = None
    return out
