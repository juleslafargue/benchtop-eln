import { useCallback, useEffect, useState } from 'react'
import { api, when } from '../api'
import type { EntryEditorProps } from './EntryCard'
import { ago, Pictogram, SignalWord, type SafetySummary } from './Safety'

interface Chem { name: string; role: string; smiles: string; material_id: number | null; safety: SafetySummary | null }
interface Assessment {
  chemicals: Chem[]; warnings: { level: 'danger' | 'warning' | 'info'; text: string; chemicals: string[] }[]
  controls: string[]; pictograms: string[]; pubchem_enabled: boolean; pending: number; refresh_days: number
}
interface SnapChem { name: string; role: string; signal: string; codes: string[]; hazards: string[]; pictograms: string[]; source: string; fetched_at: string | null }
interface Snapshot { taken_at: string; chemicals: SnapChem[]; warnings: Assessment['warnings'] }

const LEVEL = { danger: 'Danger', warning: 'Caution', info: 'Note' }

function snapOf(a: Assessment): Snapshot {
  return {
    taken_at: new Date().toISOString(),
    chemicals: a.chemicals.map(c => {
      const d = c.safety?.data
      return {
        name: c.name, role: c.role, signal: d?.signal || '', codes: (d?.hazards || []).map(h => h.code),
        hazards: (d?.hazards || []).map(h => `${h.code} ${h.text}`), pictograms: (d?.pictograms || []).map(p => p.code),
        source: d?.source?.name || (c.safety?.status === 'not_found' ? 'Not in PubChem' : c.safety?.status === 'unclassified' ? 'Not classified' : ''),
        fetched_at: c.safety?.fetched_at || null,
      }
    }),
    warnings: a.warnings,
  }
}

/** Has the reaction or the hazard data moved on since the snapshot was taken? */
function drift(s: Snapshot | null, a: Assessment | null): string | null {
  if (!s || !a) return null
  const now = snapOf(a)
  const names = (x: Snapshot) => x.chemicals.map(c => c.name.toLowerCase()).sort().join('|')
  if (names(s) !== names(now)) return 'The reaction has changed since these hazards were recorded.'
  for (const c of now.chemicals) {
    const old = s.chemicals.find(o => o.name.toLowerCase() === c.name.toLowerCase())
    if (old && c.codes.length && old.codes.slice().sort().join() !== c.codes.slice().sort().join()) return `The classification of ${c.name} has changed since these hazards were recorded.`
  }
  return null
}

export default function SafetyEntry({ content, onChange, readOnly, experimentId }: EntryEditorProps) {
  const [a, setA] = useState<Assessment | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const snap: Snapshot | null = content.snapshot || null
  const set = (k: string, v: any) => onChange({ ...content, [k]: v })

  const load = useCallback(async (refresh = false) => {
    setBusy(true); setErr('')
    try { setA(await api.get<Assessment>(`/api/experiments/${experimentId}/safety${refresh ? '?refresh=true' : ''}`)) }
    catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }, [experimentId])
  useEffect(() => { load() }, [load])
  useEffect(() => { // the reaction table was saved: re-assess
    const f = (e: Event) => { if ((e as CustomEvent).detail?.experimentId === experimentId) load() }
    window.addEventListener('benchtop:reaction-saved', f)
    return () => window.removeEventListener('benchtop:reaction-saved', f)
  }, [experimentId, load])

  const changed = drift(snap, a)
  const shown: { name: string; role: string; signal: string; pictograms: string[]; codes: string[]; titles: string[]; source: string; age: string; status?: string }[] =
    readOnly && snap ? snap.chemicals.map(c => ({ name: c.name, role: c.role, signal: c.signal, pictograms: c.pictograms, codes: c.codes, titles: c.hazards, source: c.source, age: c.fetched_at ? when(c.fetched_at) : '' }))
      : (a?.chemicals || []).map(c => {
        const d = c.safety?.data
        return { name: c.name, role: c.role, signal: d?.signal || '', pictograms: (d?.pictograms || []).map(p => p.code), codes: (d?.hazards || []).map(h => h.code),
          titles: (d?.hazards || []).map(h => `${h.code} ${h.text}`), source: d?.source?.name || '', age: c.safety?.fetched_at ? ago(c.safety.age_days) : '', status: c.safety?.status }
      })
  const warnings = readOnly && snap ? snap.warnings : a?.warnings || []
  const pictos = readOnly && snap ? [...new Set(snap.chemicals.flatMap(c => c.pictograms))].sort() : a?.pictograms || []

  return (
    <div className="risk">
      {!readOnly && a && !a.pubchem_enabled && <p className="notice" style={{ margin: 0 }}>PubChem lookups are switched off on this server, so only hazard data
        fetched earlier is shown. For a chemical without it, check the supplier's SDS and note what matters under Controls and PPE.</p>}
      {err && <p className="error">{err}</p>}
      {changed && <div className="sp-note warn" role="status"><span><b>{changed}</b> {readOnly ? 'This signed record keeps what was known when it was signed.' : 'Review the warnings and record them again.'}</span>
        {!readOnly && a && <button className="btn sm primary" onClick={() => set('snapshot', snapOf(a))}>Record current hazards</button>}</div>}

      {shown.length === 0 ? <p className="faint">{busy ? 'Checking the reaction…' : 'Add reagents to the reaction table and their hazards will appear here.'}</p> : <>
        {pictos.length > 0 && <div className="row" style={{ gap: 6 }}>{pictos.map(p => <Pictogram key={p} code={p} size={44} />)}</div>}
        {warnings.length > 0 && <div className="stack" style={{ gap: 6 }}>
          {warnings.map((w, i) => <div key={i} className={`risk-warn ${w.level}`}><span className="lvl">{LEVEL[w.level]}</span>
            <span>{w.text} <span className="faint">({w.chemicals.join(', ')})</span></span></div>)}
        </div>}
        <div className="grid-wrap"><table className="plain risk-table">
          <thead><tr><th>Chemical</th><th>Hazards</th><th>Hazard statements</th><th>Source</th></tr></thead>
          <tbody>{shown.map((c, i) => <tr key={i}>
            <td><b style={{ fontWeight: 600 }}>{c.name}</b><br /><span className="faint">{c.role}</span></td>
            <td><span className="picto-row">{c.pictograms.map(p => <Pictogram key={p} code={p} size={26} />)}<SignalWord signal={c.signal} /></span></td>
            <td className="codes">{c.status === 'pending' ? 'Fetching…' : c.status === 'unavailable' ? 'Not available here: check the SDS' : c.status === 'not_found' ? 'Not in PubChem: check the SDS' : c.status === 'unclassified' ? 'No GHS classification listed'
              : c.status === 'error' ? 'Couldn\'t fetch: check the SDS' : c.codes.map((code, j) => <span key={code} title={c.titles[j]}>{j ? ', ' : ''}{code}</span>)}</td>
            <td className="faint" style={{ fontSize: 12.5 }}>{c.source}{c.age ? ` · ${c.age}` : ''}</td>
          </tr>)}</tbody>
        </table></div>
      </>}

      {!readOnly && a && a.controls.length > 0 && <div className="stack" style={{ gap: 4 }}>
        <span className="fld-cap">Suggested from the precautionary statements</span>
        <ul className="hlist">{a.controls.map(c => <li key={c}>{c}</li>)}</ul>
        <div><button className="btn sm" onClick={() => set('controls', [content.controls, ...a.controls.filter(c => !(content.controls || '').includes(c))].filter(Boolean).join('\n'))}>Add these to controls</button></div>
      </div>}

      <div className="form-grid">
        <label className="field">Controls and PPE<textarea className="input" rows={4} disabled={readOnly} value={content.controls || ''} onChange={e => set('controls', e.target.value)}
          placeholder="Fume hood, nitrile gloves, inert atmosphere, blast shield…" /></label>
        <label className="field">Waste disposal<textarea className="input" rows={4} disabled={readOnly} value={content.waste || ''} onChange={e => set('waste', e.target.value)}
          placeholder="Halogenated / non-halogenated, aqueous, heavy metals, quench before disposal…" /></label>
      </div>
      <label className="field">Emergency and spills<textarea className="input" rows={2} disabled={readOnly} value={content.emergency || ''} onChange={e => set('emergency', e.target.value)}
        placeholder="Spill kit, quench procedure, first aid, who to call…" /></label>

      <div className="spread" style={{ gap: 8 }}>
        <span className="faint" style={{ fontSize: 12.5 }}>
          {snap ? `Hazards recorded in this notebook ${when(snap.taken_at)}.` : 'Hazards are not yet recorded in the notebook.'} Data from PubChem (EU CLP, ECHA and other public sources), refreshed every {a?.refresh_days ?? 30} days.
          The supplier's SDS takes precedence.</span>
        {!readOnly && <span className="row" style={{ gap: 6 }}>
          {a?.pubchem_enabled !== false && <button className="btn sm" disabled={busy} onClick={() => load(true)}>{busy ? 'Checking…' : 'Re-check PubChem'}</button>}
          {a && <button className="btn sm primary" disabled={!a.chemicals.length || a.pending > 0} onClick={() => set('snapshot', snapOf(a))}>{snap ? 'Record again' : 'Record hazards in notebook'}</button>}
        </span>}
      </div>
    </div>
  )
}
