import { useState } from 'react'
import { api, fmt } from '../api'
import { Mol } from './bits'

export interface Hazard { code: string; text: string; pct: number | null; category?: string }
export interface GhsData {
  cid: number | null; title: string
  source: { name: string; ref_name?: string; url?: string; reports?: number | null }
  signal: string; pictograms: { code: string; name: string }[]; hazards: Hazard[]; precautions: string[]
  other_hazards: { code: string; text: string; sources: string[]; max_pct: number | null }[]
  not_classified: boolean; sources: string[]; flags: { key: string; label: string; codes: string[] }[]; url: string
}
export interface SafetySummary {
  key: string; status: 'ok' | 'unclassified' | 'not_found' | 'error' | 'pending' | 'unavailable'; error: string; cid: number | null; title: string
  data: GhsData | null; fetched_at: string | null; age_days: number | null; stale: boolean; refresh_days: number
  changed_at: string | null; change: { added: string[]; removed: string[]; signal_before: string; signal_after: string } | null
  change_unacknowledged: boolean
}
export interface SafetyBrief { status: string; signal?: string; pictograms?: string[]; flags?: string[]; change_unacknowledged?: boolean }
export interface LookupResult {
  cid: number; name: string; smiles: string; formula: string; mw: number | null; inchikey: string; cas: string; density: number | null
  url: string; safety: SafetySummary | null; registered: { id: number; mid: string; name: string } | null
}

const PICTO_NAMES: Record<string, string> = {
  GHS01: 'Explosive', GHS02: 'Flammable', GHS03: 'Oxidiser', GHS04: 'Gas under pressure', GHS05: 'Corrosive',
  GHS06: 'Acute toxicity', GHS07: 'Harmful / irritant', GHS08: 'Health hazard', GHS09: 'Environment',
}

/** The official GHS pictogram from PubChem; offline it falls back to a labelled red diamond. */
export function Pictogram({ code, size = 40 }: { code: string; size?: number }) {
  const [broken, setBroken] = useState(false)
  const name = PICTO_NAMES[code] || code
  if (broken) return (
    <svg width={size} height={size} viewBox="0 0 40 40" role="img" aria-label={`${code} ${name}`} className="ghs-fallback">
      <title>{`${code} · ${name}`}</title>
      <rect x="8" y="8" width="24" height="24" transform="rotate(45 20 20)" fill="#fff" stroke="#d0021b" strokeWidth="3" />
      <text x="20" y="23.5" textAnchor="middle" fontSize="8.5" fontWeight="700" fill="#111">{code.slice(3)}</text>
    </svg>
  )
  return <img src={`https://pubchem.ncbi.nlm.nih.gov/images/ghs/${code}.svg`} width={size} height={size} alt={`${code} ${name}`} title={`${code} · ${name}`}
    className="ghs" onError={() => setBroken(true)} referrerPolicy="no-referrer" />
}

export function SignalWord({ signal }: { signal: string }) {
  if (!signal) return null
  return <span className={`signal ${signal.toLowerCase() === 'danger' ? 'danger' : 'warning'}`}>{signal}</span>
}

export function SafetyBadge({ s }: { s: SafetyBrief | null | undefined }) {
  if (!s || !s.pictograms?.length) return s?.status === 'unclassified' ? <span className="faint">Not classified</span> : null
  return (
    <span className="safety-badge" title={(s.flags || []).join(' · ')}>
      {s.pictograms.map(p => <Pictogram key={p} code={p} size={22} />)}
      {s.change_unacknowledged && <span className="w-flag warn" title="The classification changed at the last check">Changed</span>}
    </span>
  )
}

export function ago(days: number | null): string {
  if (days === null) return 'never'
  if (days < 1) return 'today'
  if (days < 2) return 'yesterday'
  if (days < 60) return `${Math.round(days)} days ago`
  return `${Math.round(days / 30)} months ago`
}

export function Provenance({ s }: { s: SafetySummary }) {
  const d = s.data
  return (
    <p className="provenance">
      {d?.source?.name ? <>Source: {d.source.url ? <a href={d.source.url} target="_blank" rel="noreferrer">{d.source.name}</a> : d.source.name}, via </> : null}
      {d?.url ? <a href={d.url} target="_blank" rel="noreferrer">PubChem</a> : 'PubChem'} · retrieved {ago(s.age_days)}
      {s.stale && s.fetched_at ? <span className="w-flag warn" style={{ marginLeft: 6 }}>due for refresh</span> : null}
      {s.error ? <span className="faint"> · last check failed: {s.error}</span> : null}
    </p>
  )
}

/** Full GHS block: pictograms, signal word, statements, extras from other sources, flags and provenance. */
export function GhsDetails({ s, compact = false }: { s: SafetySummary; compact?: boolean }) {
  // lists may be missing in data cached by older versions
  const d = s.data && { ...s.data, pictograms: s.data.pictograms || [], hazards: s.data.hazards || [], precautions: s.data.precautions || [],
    other_hazards: s.data.other_hazards || [], flags: s.data.flags || [], sources: s.data.sources || [] }
  if (s.status === 'pending') return <p className="faint">Fetching safety data…</p>
  if (s.status === 'unavailable') return <p className="faint">Hazard data isn't available here: this Benchtop doesn't look chemicals up on
    PubChem. Check the supplier's SDS{compact ? '' : ", and note anything important under Hazard notes (Edit)"}.</p>
  if (s.status === 'not_found') return <p className="faint">PubChem has no entry for this chemical. Check the supplier's SDS.</p>
  if (!d) return <p className="error">{s.error || 'No safety data yet.'}</p>
  if (d.not_classified) return <><p>No GHS hazard classification is listed in PubChem for this compound. That isn't proof it's safe: check the supplier's SDS.</p><Provenance s={s} /></>
  return (
    <div className="ghs-block">
      <div className="row" style={{ gap: 6, alignItems: 'center' }}>
        {d.pictograms.map(p => <Pictogram key={p.code} code={p.code} size={compact ? 32 : 44} />)}
        <SignalWord signal={d.signal} />
      </div>
      {d.flags.length > 0 && <div className="chips">{d.flags.map(f => <span key={f.key} className="flag-chip" title={f.codes.join(', ')}>{f.label}</span>)}</div>}
      <ul className="hlist">
        {d.hazards.map(h => <li key={h.code}><b>{h.code}</b> {h.text}{h.pct !== null && h.pct < 100 ? <span className="faint"> ({fmt(h.pct, 1)}% of notifiers)</span> : null}</li>)}
      </ul>
      {d.other_hazards.length > 0 && <details className="others">
        <summary>{d.other_hazards.length} more reported by other sources</summary>
        <ul className="hlist">{d.other_hazards.map(o => <li key={o.code} className={o.max_pct !== null && o.max_pct < 10 ? 'minor' : ''}>
          <b>{o.code}</b> {o.text} <span className="faint">({o.sources.join(', ')}{o.max_pct !== null ? `, up to ${fmt(o.max_pct, 1)}% of notifiers` : ''})</span></li>)}</ul>
      </details>}
      {!compact && d.precautions.length > 0 && <p className="pcodes"><span className="faint">Precautionary statements:</span> {d.precautions.map(p =>
        <a key={p} href={`https://pubchem.ncbi.nlm.nih.gov/ghs/#${p.split('+')[0]}`} target="_blank" rel="noreferrer">{p}</a>)}</p>}
      <Provenance s={s} />
    </div>
  )
}

/** Name / CAS / SMILES search against PubChem. */
export function LookupBox({ onResult, initial = '', autoFocus = false, label = 'Look up on PubChem' }: {
  onResult: (r: LookupResult) => void; initial?: string; autoFocus?: boolean; label?: string
}) {
  const [q, setQ] = useState(initial)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const go = async () => {
    if (!q.trim()) return
    setBusy(true); setErr('')
    try { onResult(await api.get<LookupResult>(`/api/lookup?q=${encodeURIComponent(q.trim())}`)) }
    catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="lookup">
      <form className="row" style={{ flexWrap: 'nowrap', gap: 6 }} onSubmit={e => { e.preventDefault(); go() }}>
        <input className="input grow" autoFocus={autoFocus} placeholder="Name, CAS number or SMILES, e.g. sodium borohydride or 16940-66-2"
          value={q} onChange={e => setQ(e.target.value)} aria-label="Compound name, CAS or SMILES" />
        <button className="btn" disabled={busy || !q.trim()}>{busy ? 'Searching…' : label}</button>
      </form>
      {err && <p className="error" style={{ margin: '6px 0 0' }}>{err}</p>}
    </div>
  )
}

export function LookupCard({ r, children }: { r: LookupResult; children?: React.ReactNode }) {
  return (
    <div className="lookup-card">
      <div className="mat-thumb" style={{ width: 120, height: 80 }}>{r.smiles && <Mol smiles={r.smiles} w={240} h={160} />}</div>
      <div className="stack" style={{ gap: 4, minWidth: 0 }}>
        <div><b>{r.name}</b> <a className="faint" href={r.url} target="_blank" rel="noreferrer">PubChem CID {r.cid}</a></div>
        <div className="faint">{[r.formula, r.mw ? `${fmt(r.mw, 2)} g/mol` : '', r.cas ? `CAS ${r.cas}` : '', r.density ? `${r.density} g/mL` : ''].filter(Boolean).join(' · ')}</div>
        {r.safety?.data && <div className="row" style={{ gap: 4 }}>{r.safety.data.pictograms.map(p => <Pictogram key={p.code} code={p.code} size={26} />)}
          <SignalWord signal={r.safety.data.signal} /></div>}
        {r.registered && <div className="faint">Already registered as {r.registered.mid} ({r.registered.name})</div>}
        {children}
      </div>
    </div>
  )
}
