import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import { ago, api, fmt, type Status } from '../api'
import { Mol } from '../components/bits'
import CutShort from '../components/CutShort'

const PAGE = 200

interface MadeIn { id: number; eid: string; title: string; status: Status; date: string; yield_pct: number | null }
interface Compound {
  name: string; smiles: string; formula: string; mw: number; logp: number; tpsa: number; hbd: number; hba: number
  rotatable_bonds: number; heavy_atoms: number; aromatic_rings: number; fsp3: number; qed: number | null
  lipinski_violations: string[]; veber_violations: string[]; experiments: MadeIn[]
  best_yield: number | null; material: { id: number; mid: string } | null
}
type Key = 'name' | 'made' | 'mw' | 'logp' | 'tpsa' | 'hbd' | 'hba' | 'rotatable_bonds' | 'fsp3' | 'qed' | 'best_yield'
const COLS: [Key, string, string][] = [
  ['mw', 'MW', 'Molecular weight of the largest fragment'], ['logp', 'cLogP', 'Crippen calculated logP'], ['tpsa', 'TPSA', 'Topological polar surface area (Å²)'],
  ['hbd', 'HBD', 'H-bond donors'], ['hba', 'HBA', 'H-bond acceptors'], ['rotatable_bonds', 'RotB', 'Rotatable bonds'],
  ['fsp3', 'Fsp³', 'Fraction of sp³ carbons'], ['qed', 'QED', 'Quantitative estimate of drug-likeness (0–1, higher is more drug-like)'],
  ['best_yield', 'Best yield', 'Best isolated yield across experiments'],
]

export default function Compounds() {
  const [rows, setRows] = useState<Compound[] | null>(null)
  const [sort, setSort] = useState<{ key: Key; dir: 1 | -1 }>({ key: 'name', dir: 1 })
  const [onlyRo5, setOnlyRo5] = useState(false)
  const [err, setErr] = useState('')
  // 0.36.6: the table draws 200 rows at first (each row's structure is a picture); Show more draws the next 200
  const [upTo, setUpTo] = useState(PAGE)
  useEffect(() => { api.get<Compound[]>('/api/compounds').then(setRows).catch(e => setErr(e.message)) }, [])
  useEffect(() => { setUpTo(PAGE) }, [sort, onlyRo5])
  const shown = useMemo(() => {
    const r = (rows || []).filter(c => !onlyRo5 || (c.lipinski_violations.length <= 1 && !c.veber_violations.length))
    return [...r].sort((a, b) => {
      const val = (c: Compound) => sort.key === 'made' ? c.experiments.length : c[sort.key]
      const x = val(a), y = val(b)
      if (typeof x === 'string' || typeof y === 'string') return String(x).localeCompare(String(y)) * sort.dir
      return ((x ?? -Infinity) as number) > ((y ?? -Infinity) as number) ? sort.dir : -sort.dir
    })
  }, [rows, sort, onlyRo5])
  const th = (key: Key, label: string, title?: string) => (
    <th aria-sort={sort.key === key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'} title={title}>
      <button className="btn ghost sm" style={{ padding: 0, fontWeight: 600 }} onClick={() => setSort(s => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : (key === 'name' ? 1 : -1) }))}>
        {label}{sort.key === key ? (sort.dir === 1 ? ' ↑' : ' ↓') : ''}</button></th>)

  return (
    <div className="stack" style={{ gap: 18 }}>
      <div className="spread">
        <div><h1 className="page-title">Compounds</h1>
          <p className="lede">Every product you've made and every registered sample, with medicinal-chemistry descriptors. Calculated with RDKit on the parent structure (salts removed).</p></div>
        <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={onlyRo5} onChange={e => setOnlyRo5(e.target.checked)} /> Drug-like only (≤1 Lipinski violation, passes Veber)</label>
      </div>
      {err && <p className="error">{err}</p>}
      <div className="panel grid-wrap" style={{ border: 0 }}>
        <table className="plain">
          <thead><tr>{th('name', 'Compound')}<th>Structure</th>{COLS.map(([k, l, t]) => th(k, l, t))}<th>Rules</th>{th('made', 'Made in', 'Experiments that made this compound')}</tr></thead>
          <tbody>
            {shown.slice(0, upTo).map(c => <tr key={c.smiles}>
              <td><b style={{ fontWeight: 600 }}>{c.name || c.formula}</b><div className="faint">{c.formula}{c.material ? <> · <Link to={`/materials/${c.material.id}`}>{c.material.mid}</Link></> : null}</div></td>
              <td><span className="mat-thumb" style={{ width: 110, height: 66, display: 'block' }}><Mol smiles={c.smiles} w={220} h={132} /></span></td>
              <td className="num">{fmt(c.mw, 1)}</td><td className="num">{fmt(c.logp, 2)}</td><td className="num">{fmt(c.tpsa, 1)}</td>
              <td className="num">{c.hbd}</td><td className="num">{c.hba}</td><td className="num">{c.rotatable_bonds}</td>
              <td className="num">{fmt(c.fsp3, 2)}</td><td className="num">{c.qed ?? '—'}</td>
              <td className="num">{c.best_yield !== null ? `${fmt(c.best_yield, 1)}%` : '—'}</td>
              <td style={{ fontSize: 12.5 }}>{!c.lipinski_violations.length && !c.veber_violations.length ? <span className="faint">Passes Lipinski & Veber</span>
                : [...c.lipinski_violations, ...c.veber_violations].map(v => <div key={v}>{v}</div>)}</td>
              <td className="made-in"><MadeInCell exps={c.experiments} name={c.name || c.formula} /></td>
            </tr>)}
            {rows && !shown.length && <tr><td colSpan={13} className="empty">No compounds yet. Products from your reaction tables appear here.</td></tr>}
            {!rows && !err && <tr><td colSpan={13} className="empty">Calculating…</td></tr>}
          </tbody>
        </table>
      </div>
      <CutShort shown={Math.min(upTo, shown.length)} total={shown.length} things="compounds" busy={false} onMore={() => setUpTo(n => n + PAGE)} />
    </div>
  )
}

/** One experiment links straight to it; more than one shows a count that opens a scrollable list,
 *  so a compound made a hundred times keeps a normal-sized row. */
function MadeInCell({ exps, name }: { exps: MadeIn[]; name: string }) {
  const [open, setOpen] = useState(false)
  const btn = useRef<HTMLButtonElement>(null)
  const pop = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null)
  useLayoutEffect(() => {
    if (!open || !btn.current) return
    const r = btn.current.getBoundingClientRect(), w = 340
    const below = window.innerHeight - r.bottom > 300
    setPos({ left: Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8)), top: below ? r.bottom + 6 : Math.max(8, r.top - 6 - Math.min(320, 52 + exps.length * 52)) })
  }, [open, exps.length])
  useEffect(() => {
    if (!open) return
    const close = (e: Event) => { if (!pop.current?.contains(e.target as Node) && !btn.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { setOpen(false); btn.current?.focus() } }
    const away = (e: Event) => { if (!pop.current?.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close); document.addEventListener('keydown', esc); window.addEventListener('scroll', away, true)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc); window.removeEventListener('scroll', away, true) }
  }, [open])
  if (!exps.length) return <span className="faint">—</span>
  if (exps.length === 1) return <Link to={`/experiments/${exps[0].id}`} className="eid-link">{exps[0].eid}</Link>
  return <>
    <button ref={btn} className="made-btn" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(o => !o)}
      title={`Made in ${exps.length} experiments; latest ${exps[0].eid}`}>
      <span className="made-n">{exps.length}</span> experiments <span aria-hidden>▾</span>
    </button>
    {open && createPortal(
      <div ref={pop} className="made-pop" role="dialog" aria-label={`Experiments that made ${name}`} style={pos ? { top: pos.top, left: pos.left } : { visibility: 'hidden' }}>
        <div className="made-head">{name} <span className="faint">· {exps.length} experiments, newest first</span></div>
        <ul>
          {exps.map(e => <li key={e.id}>
            <Link to={`/experiments/${e.id}`}>
              <span className={`status ${e.status}`} aria-label={e.status} />
              <span className="made-main"><b>{e.eid}</b> <span className="made-title">{e.title}</span></span>
              <span className="made-meta">{e.yield_pct !== null ? `${fmt(e.yield_pct, 1)}%` : ''}<span className="faint">{ago(e.date)}</span></span>
            </Link>
          </li>)}
        </ul>
      </div>, document.body)}
  </>
}
