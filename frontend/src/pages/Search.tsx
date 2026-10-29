import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName, fmt, when, type ExperimentSummary } from '../api'
import { StatusDot } from '../components/ExperimentList'
import { Mol, StructureEditor } from '../components/bits'

type Mode = 'text' | 'substructure' | 'similarity'
type Tab = 'all' | 'experiments' | 'materials' | 'files'
interface ExpHit extends ExperimentSummary { score: number; match: string; snippet?: string; smiles?: string }
interface MatHit { id: number; mid: string; name: string; smiles: string; formula: string; mw: number | null; cas: string; kind: string; score?: number }
interface FileHit { id: number; filename: string; size: number; experiment_id: number; eid: string; title: string; created_at: string }
interface Result { experiments: ExpHit[]; materials: MatHit[]; files: FileHit[]; terms: string[] }

const RECENT_KEY = 'benchtop.recentSearches'
const loadRecent = (): { q: string; mode: Mode }[] => { try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]').slice(0, 8) } catch { return [] } }
const saveRecent = (q: string, mode: Mode) => {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify([{ q, mode }, ...loadRecent().filter(r => !(r.q === q && r.mode === mode))].slice(0, 8))) } catch { /* private mode */ }
}
// SMILES-ish: no spaces and some bond/ring/bracket syntax, e.g. c1ccccc1Br or CC(=O)O
const looksLikeSmiles = (q: string) => /^\S{3,}$/.test(q) && /[=#()[\]@\\/]|^[cnosCNOS]\d|\d[cnos]/.test(q) && !/^[A-Z]{2,}-\d/.test(q)

/** Wrap every occurrence of the search words in <mark>. */
function Hl({ text, terms }: { text: string; terms: string[] }) {
  if (!terms.length || !text) return <>{text}</>
  const esc = (t: string) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  // numbers highlight only as whole numbers, matching how the server searched
  const re = new RegExp(`(${terms.map(t => (/^\d+(?:[.,]\d+)?$/.test(t) ? `(?<![\\d.,-])${esc(t)}(?![\\d]|[.,]\\d|-\\d)` : esc(t))).join('|')})`, 'gi')
  return <>{text.split(re).map((part, i) => (i % 2 ? <mark key={i}>{part}</mark> : <Fragment key={i}>{part}</Fragment>))}</>
}

export default function Search() {
  const { notebooks } = useApp()
  const nav = useNavigate()
  const [params, setParams] = useSearchParams()
  const [q, setQ] = useState(params.get('q') || '')
  const [mode, setMode] = useState<Mode>((params.get('mode') as Mode) || 'text')
  const [threshold, setThreshold] = useState(Number(params.get('t')) || 0.5)
  const [status, setStatus] = useState(params.get('status') || '')
  const [nb, setNb] = useState(params.get('nb') || '')
  const [mine, setMine] = useState(params.get('mine') === '1')
  const [tab, setTab] = useState<Tab>('all')
  const [res, setRes] = useState<Result | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [drawing, setDrawing] = useState(false)
  const [recent, setRecent] = useState(loadRecent)
  const reqId = useRef(0)
  const input = useRef<HTMLInputElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const structural = mode !== 'text'

  // search as you type (short pause first), keeping the URL in step so Back and links restore it
  useEffect(() => {
    const query = q.trim()
    setParams(p => {
      const n = new URLSearchParams()
      if (query) n.set('q', query)
      if (mode !== 'text') n.set('mode', mode)
      if (mode === 'similarity' && threshold !== 0.5) n.set('t', String(threshold))
      if (status) n.set('status', status)
      if (nb) n.set('nb', nb)
      if (mine) n.set('mine', '1')
      return n.toString() === p.toString() ? p : n
    }, { replace: true })
    if (!query || (structural && query.length < 2)) { setRes(null); setErr(''); setBusy(false); return }
    const my = ++reqId.current
    setBusy(true)
    const t = setTimeout(async () => {
      const qs = new URLSearchParams({ q: query, mode, threshold: String(threshold) })
      if (status) qs.set('status', status)
      if (nb) qs.set('notebook_id', nb)
      if (mine) qs.set('mine', 'true')
      try {
        const r = await api.get<Result>(`/api/search?${qs}`)
        if (my !== reqId.current) return
        setRes(r); setErr('')
        if (r.experiments.length + r.materials.length + r.files.length) { saveRecent(query, mode); setRecent(loadRecent()) }
      } catch (e: any) { if (my === reqId.current) { setErr(e.message); setRes(null) } }
      finally { if (my === reqId.current) setBusy(false) }
    }, structural ? 450 : 220)
    return () => clearTimeout(t)
  }, [q, mode, threshold, status, nb, mine]) // eslint-disable-line react-hooks/exhaustive-deps

  const counts = res ? { experiments: res.experiments.length, materials: res.materials.length, files: res.files.length } : null
  const total = counts ? counts.experiments + counts.materials + counts.files : 0
  const show = (t: Tab) => tab === 'all' || tab === t
  const terms = res?.terms || []
  const filtersOn = !!(status || nb || mine)

  // ↑/↓ move between results, Enter opens; ↓ from the box jumps into the list
  const onListKey = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
    const items = Array.from(list.current?.querySelectorAll<HTMLAnchorElement>('a[data-result]') || [])
    const i = items.indexOf(document.activeElement as HTMLAnchorElement)
    e.preventDefault()
    if (e.key === 'ArrowDown') items[Math.min(items.length - 1, i + 1)]?.focus()
    else if (i <= 0) input.current?.focus()
    else items[i - 1]?.focus()
  }
  const examples = useMemo(() => [
    { q: 'suzuki', mode: 'text' as Mode, label: 'suzuki' }, { q: '"flash column"', mode: 'text' as Mode, label: '"flash column" (exact phrase)' },
    { q: 'boronic 80', mode: 'text' as Mode, label: 'boronic 80 (both words)' }, { q: 'c1ccccc1Br', mode: 'substructure' as Mode, label: 'aryl bromides (substructure)' },
  ], [])

  return (
    <div className="stack" style={{ gap: 18 }} onKeyDown={onListKey}>
      <div><h1 className="page-title">Search</h1>
        <p className="lede">Search everything: titles, tags, experiment IDs, section text, tables, files, materials and lot barcodes. Or search by structure.</p></div>

      <div className="panel panel-pad stack search-box">
        <div className="seg search-modes" role="radiogroup" aria-label="Search by">
          {(['text', 'substructure', 'similarity'] as Mode[]).map(m => (
            <button type="button" key={m} role="radio" aria-checked={mode === m} onClick={() => { setMode(m); input.current?.focus() }}>
              {{ text: 'Words', substructure: 'Substructure', similarity: 'Similar structures' }[m]}</button>
          ))}
        </div>
        <div className="search-input">
          <span className="search-icon" aria-hidden>⌕</span>
          <input ref={input} className="input" value={q} onChange={e => setQ(e.target.value)} autoFocus spellCheck={!structural}
            placeholder={structural ? 'SMILES or SMARTS, or draw a structure' : 'Try a reagent, a technique, an experiment ID, a word from your notes…'}
            aria-label="Search" onKeyDown={e => {
              if (e.key === 'Escape') setQ('')
              if (e.key === 'Enter') { const first = list.current?.querySelector<HTMLAnchorElement>('a[data-result]'); if (first) nav(first.getAttribute('href')!) }
            }} />
          {busy && <span className="search-busy" aria-live="polite">Searching…</span>}
          {q && <button type="button" className="btn ghost sm" onClick={() => { setQ(''); input.current?.focus() }} aria-label="Clear search">×</button>}
          {structural && <button type="button" className="btn sm" onClick={() => setDrawing(true)}>Draw</button>}
        </div>
        {mode === 'text' && looksLikeSmiles(q.trim()) && <p className="search-hint">That looks like a structure.
          <button type="button" className="chip" onClick={() => setMode('substructure')}>Search by substructure</button>
          <button type="button" className="chip" onClick={() => setMode('similarity')}>Find similar structures</button></p>}
        {structural && q.trim() && <div className="row" style={{ gap: 14, alignItems: 'center' }}>
          <span className="mat-thumb" style={{ width: 150, height: 92 }}><Mol smiles={q.trim()} w={300} h={184} /></span>
          {mode === 'similarity' && <label className="row faint" style={{ gap: 8 }}>At least
            <input type="range" min={0.2} max={1} step={0.05} value={threshold} onChange={e => setThreshold(Number(e.target.value))} aria-label="Minimum similarity" />
            <b style={{ color: 'var(--ink)' }}>{Math.round(threshold * 100)}%</b> similar (Tanimoto)</label>}
          {mode === 'substructure' && <span className="faint" style={{ fontSize: 13 }}>Matches are highlighted in blue in each result.</span>}
        </div>}
        <div className="search-filters">
          <select className="select" value={status} onChange={e => setStatus(e.target.value)} aria-label="Status">
            <option value="">Any status</option><option value="open">Open</option><option value="signed">Signed</option><option value="witnessed">Witnessed</option><option value="voided">Voided</option><option value="locked">Locked (imported)</option>
          </select>
          <select className="select" value={nb} onChange={e => setNb(e.target.value)} aria-label="Notebook">
            <option value="">All notebooks</option>{notebooks.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
          </select>
          <label className="row" style={{ gap: 6 }}><input type="checkbox" checked={mine} onChange={e => setMine(e.target.checked)} /> Only my experiments</label>
          {filtersOn && <button type="button" className="btn ghost sm" onClick={() => { setStatus(''); setNb(''); setMine(false) }}>Clear filters</button>}
        </div>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      </div>

      {!q.trim() && <div className="stack" style={{ gap: 10 }}>
        {recent.length > 0 && <div className="chips"><span className="faint" style={{ marginRight: 4 }}>Recent:</span>
          {recent.map(r => <button key={r.mode + r.q} className="chip" onClick={() => { setMode(r.mode); setQ(r.q) }}>{r.q}{r.mode !== 'text' ? ` · ${r.mode}` : ''}</button>)}
          <button className="btn ghost sm" onClick={() => { try { localStorage.removeItem(RECENT_KEY) } catch { /* ignore */ } setRecent([]) }}>Clear</button></div>}
        <div className="chips"><span className="faint" style={{ marginRight: 4 }}>Try:</span>
          {examples.map(x => <button key={x.label} className="chip" onClick={() => { setMode(x.mode); setQ(x.q) }}>{x.label}</button>)}</div>
        <p className="faint" style={{ margin: 0, fontSize: 13 }}>Results appear as you type. Every word must match, in any order; put an exact phrase in "quotes". ↓ moves into the results, Enter opens one.</p>
      </div>}

      {res && counts && <>
        <div className="spread" style={{ gap: 8 }}>
          <div className="seg" role="tablist" aria-label="Result type">
            {(['all', 'experiments', 'materials', 'files'] as Tab[]).filter(t => t === 'all' || !structural || t !== 'files').map(t => (
              <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
                {{ all: 'All', experiments: 'Experiments', materials: 'Materials', files: 'Files' }[t]}{t !== 'all' ? ` ${counts[t]}` : ` ${total}`}</button>))}
          </div>
          <span className="faint" style={{ fontSize: 13 }}>{total === 0 ? '' : `${total} result${total === 1 ? '' : 's'}${filtersOn ? ' (filtered)' : ''}`}</span>
        </div>

        {total === 0 && <div className="panel panel-pad empty-state">
          <p style={{ margin: 0 }}><b>Nothing matches “{q.trim()}”.</b></p>
          <p className="faint" style={{ margin: '4px 0 0' }}>{mode === 'text'
            ? `Every word has to appear somewhere in the experiment. Try fewer words${filtersOn ? ', clear the filters' : ''}, or check the spelling.`
            : mode === 'similarity' ? 'Lower the similarity threshold, or try a substructure search.' : 'Try a smaller fragment, or a similarity search.'}</p>
        </div>}

        <div ref={list} className="stack" style={{ gap: 16 }}>
          {show('experiments') && counts.experiments > 0 && <section className="stack" style={{ gap: 6 }}>
            {tab === 'all' && <h2 className="search-h2">Experiments <span className="faint">{counts.experiments}</span></h2>}
            <ul className="search-results panel">
              {res.experiments.map(h => <li key={h.id}><Link data-result to={`/experiments/${h.id}`} className="search-hit">
                {structural && h.smiles && <span className="mat-thumb hit-thumb"><Mol smiles={h.smiles} w={220} h={136} highlight={mode === 'substructure' ? q.trim() : ''} /></span>}
                <span className="hit-main">
                  <span className="hit-title"><span className="eid">{h.eid}</span> <Hl text={h.title} terms={terms} /></span>
                  <span className="hit-meta">{h.notebook_name || 'No notebook'} · {displayName(h.owner)} · edited {when(h.updated_at)}
                    {h.tags.length ? <> · {h.tags.map(t => <span key={t} className="hit-tag"><Hl text={t} terms={terms} /></span>)}</> : null}</span>
                  {h.match && !['Title', 'ID'].includes(h.match) && <span className="hit-snippet"><span className="hit-where">{h.match}</span>
                    {h.snippet ? <Hl text={h.snippet} terms={terms} /> : null}</span>}
                </span>
                <span className="hit-side">
                  <StatusDot status={h.status} />
                  {mode === 'similarity' && <span className="sim" title="Tanimoto similarity of the closest structure in the reaction table">
                    <span className="sim-bar"><span style={{ width: `${Math.round(h.score * 100)}%` }} /></span>{Math.round(h.score * 100)}%</span>}
                </span>
              </Link></li>)}
            </ul>
          </section>}

          {show('materials') && counts.materials > 0 && <section className="stack" style={{ gap: 6 }}>
            {tab === 'all' && <h2 className="search-h2">Materials <span className="faint">{counts.materials}</span></h2>}
            <ul className="search-results panel">
              {res.materials.map(m => <li key={m.id}><Link data-result to={`/materials/${m.id}`} className="search-hit">
                <span className="mat-thumb hit-thumb">{m.smiles && <Mol smiles={m.smiles} w={220} h={136} highlight={mode === 'substructure' ? q.trim() : ''} />}</span>
                <span className="hit-main">
                  <span className="hit-title"><Hl text={m.name} terms={terms} /></span>
                  <span className="hit-meta">{m.mid} · {m.kind}{m.cas ? <> · CAS <Hl text={m.cas} terms={terms} /></> : null}{m.formula ? ` · ${m.formula}` : ''}{m.mw ? ` · ${fmt(m.mw, 2)} g/mol` : ''}</span>
                </span>
                <span className="hit-side">{mode === 'similarity' && m.score !== undefined && <span className="sim"><span className="sim-bar"><span style={{ width: `${Math.round(m.score * 100)}%` }} /></span>{Math.round(m.score * 100)}%</span>}</span>
              </Link></li>)}
            </ul>
          </section>}

          {show('files') && counts.files > 0 && <section className="stack" style={{ gap: 6 }}>
            {tab === 'all' && <h2 className="search-h2">Files <span className="faint">{counts.files}</span></h2>}
            <ul className="search-results panel">
              {res.files.map(f => <li key={f.id}><Link data-result to={`/experiments/${f.experiment_id}`} className="search-hit">
                <span className="file-icon" aria-hidden>{f.filename.split('.').pop()?.slice(0, 4).toUpperCase()}</span>
                <span className="hit-main"><span className="hit-title"><Hl text={f.filename} terms={terms} /></span>
                  <span className="hit-meta">{f.eid} · {f.title} · {(f.size / 1024).toFixed(0)} kB · {when(f.created_at)}</span></span>
              </Link></li>)}
            </ul>
          </section>}
        </div>
      </>}
      {drawing && <StructureEditor mode="molecule" initial={structural ? q : ''} onClose={() => setDrawing(false)} onDone={r => { setDrawing(false); setQ(r.smiles) }} />}
    </div>
  )
}
