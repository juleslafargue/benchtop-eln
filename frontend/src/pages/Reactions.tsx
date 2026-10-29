import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, downloadAuthed, fmt, greenOk, PURIFICATION_LABELS, type ReactionSummary } from '../api'
import { StatusDot } from '../components/ExperimentList'
import { Mol, NumInput, useDebounced } from '../components/bits'
import { median } from '../stats'

type SortKey = 'date' | 'yield' | 'temp' | 'scale'

export default function Reactions() {
  const [q, setQ] = useState('')
  const [minYield, setMinYield] = useState<number | null>(null)
  const [minTemp, setMinTemp] = useState<number | null>(null)
  const [maxTemp, setMaxTemp] = useState<number | null>(null)
  const [atmosphere, setAtmosphere] = useState('')
  const [sub, setSub] = useState('')
  const [rows, setRows] = useState<ReactionSummary[] | null>(null)
  const [err, setErr] = useState('')
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'date', dir: -1 })
  const dq = useDebounced(q, 250)
  const dsub = useDebounced(sub, 400)

  const params = useMemo(() => {
    const p = new URLSearchParams()
    if (dq) p.set('q', dq)
    if (minYield !== null) p.set('min_yield', String(minYield))
    if (minTemp !== null) p.set('min_temp', String(minTemp))
    if (maxTemp !== null) p.set('max_temp', String(maxTemp))
    if (atmosphere) p.set('atmosphere', atmosphere)
    if (dsub) p.set('substructure', dsub)
    return p.toString()
  }, [dq, minYield, minTemp, maxTemp, atmosphere, dsub])

  useEffect(() => {
    api.get<ReactionSummary[]>(`/api/reactions?${params}`).then(r => { setRows(r); setErr('') }).catch(e => setErr(e.message))
  }, [params])

  const sorted = useMemo(() => {
    if (!rows) return []
    const val = (r: ReactionSummary) => ({ date: r.date, yield: r.yield_pct, temp: r.temperature_c, scale: r.scale_mmol }[sort.key])
    return [...rows].sort((a, b) => {
      const x = val(a), y = val(b)
      if (x === y) return 0
      if (x === null || x === undefined) return 1
      if (y === null || y === undefined) return -1
      return (x < y ? -1 : 1) * sort.dir
    })
  }, [rows, sort])

  const yields = (rows || []).map(r => r.yield_pct).filter((y): y is number => y !== null)
  const th = (key: SortKey, label: string) => (
    <th aria-sort={sort.key === key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'}>
      <button className="btn ghost sm" style={{ padding: 0, fontWeight: 600 }} onClick={() => setSort(s => ({ key, dir: s.key === key ? (-s.dir as 1 | -1) : -1 }))}>
        {label}{sort.key === key ? (sort.dir === 1 ? ' ↑' : ' ↓') : ''}</button>
    </th>
  )

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="spread">
        <div>
          <h1 className="page-title">Reactions</h1>
          <p className="lede">Every reaction across your notebooks, with its conditions and outcome.
            {yields.length > 0 && <> {rows!.length} shown · median yield {fmt(median(yields), 0)}%.</>}</p>
        </div>
        <button className="btn" onClick={() => downloadAuthed(`/api/reactions?${params}&format=csv`, 'reactions.csv')}>Download CSV</button>
      </div>

      <div className="panel panel-pad filters">
        <label className="field">Search<input className="input" placeholder="Reagent, product, tag, title…" value={q} onChange={e => setQ(e.target.value)} /></label>
        <label className="field">Contains substructure<input className="input" placeholder="SMILES/SMARTS" value={sub} onChange={e => setSub(e.target.value)} style={{ fontFamily: 'var(--mono)', fontSize: 13 }} /></label>
        <label className="field">Yield at least (%)<NumInput className="input" label="Minimum yield" value={minYield} onCommit={setMinYield} /></label>
        <label className="field">Temp from (°C)<NumInput className="input" label="Minimum temperature" value={minTemp} onCommit={setMinTemp} /></label>
        <label className="field">Temp to (°C)<NumInput className="input" label="Maximum temperature" value={maxTemp} onCommit={setMaxTemp} /></label>
        <label className="field">Atmosphere<select className="select" value={atmosphere} onChange={e => setAtmosphere(e.target.value)}>
          {['', 'air', 'N2', 'Ar', 'H2', 'O2', 'CO'].map(a => <option key={a} value={a}>{a || 'Any'}</option>)}</select></label>
      </div>
      {err && <p className="error">{err}</p>}

      <div className="panel grid-wrap" style={{ border: 0 }}>
        <table className="plain rx-table">
          <thead><tr>
            {th('date', 'Experiment')}<th>Scheme</th>{th('scale', 'Scale')}<th>Reagents · solvent</th>{th('temp', 'Conditions')}{th('yield', 'Yield')}<th>Purified · data</th>
          </tr></thead>
          <tbody>
            {sorted.map(r => (
              <tr key={`${r.experiment_id}-${r.entry_title}`}>
                <td style={{ minWidth: 170 }}>
                  <Link to={`/experiments/${r.experiment_id}`} style={{ fontWeight: 600 }}>{r.eid}</Link> <StatusDot status={r.status} />
                  <div style={{ fontSize: 13 }}>{r.title}</div>
                  <div className="faint">{new Date(r.date).toLocaleDateString()}</div>
                </td>
                <td>{r.reaction_smiles ? <Mol className="rx-scheme" rxn smiles={r.reaction_smiles} w={520} h={160} /> : <span className="faint">—</span>}</td>
                <td className="num">{r.scale_mmol !== null ? `${fmt(r.scale_mmol, 2)} mmol` : '—'}<div className="faint">{r.limiting}</div></td>
                <td style={{ fontSize: 13 }}>{r.reagents.join(', ') || '—'}<div className="faint">{r.solvents.join(', ')}{r.concentration_m ? ` · ${fmt(r.concentration_m, 2)} M` : ''}</div></td>
                <td style={{ whiteSpace: 'nowrap', fontSize: 13 }}>
                  {r.temperature_c !== null ? `${fmt(r.temperature_c, 1)} °C` : ''}{r.temperature_label ? ` ${r.temperature_label}` : ''}
                  {r.temperature_c === null && !r.temperature_label && <span className="faint">—</span>}
                  <div className="faint">{[r.time_h !== null ? `${fmt(r.time_h, 2)} h` : null, r.atmosphere].filter(Boolean).join(' · ')}</div>
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  {r.yield_pct !== null
                    ? <><b>{fmt(r.yield_pct, 1)}%</b><span className="yield-bar" style={{ width: Math.max(2, Math.min(100, r.yield_pct)) * 0.6 }} /></>
                    : <span className="faint">—</span>}
                  <div className="faint">{r.product}</div>
                  {(() => {
                    const g = greenOk(r.metrics)
                    const parts = [g.ae != null ? `AE ${fmt(g.ae, 0)}%` : g.aeImpossible ? 'AE: check roles' : '', r.metrics?.pmi != null ? `PMI ${fmt(r.metrics.pmi, 0)}` : ''].filter(Boolean)
                    return parts.length > 0 && <div className="faint" style={{ fontSize: 12 }} title={g.aeImpossible
                      ? 'Atom economy would be over 100 %: a co-reactant may be marked as Reagent. Only Reactants count.'
                      : 'Atom economy (only Reactants count) and process mass intensity, reaction only'}>{parts.join(' · ')}</div>
                  })()}
                </td>
                <td style={{ fontSize: 13 }}>{r.purification ? PURIFICATION_LABELS[r.purification] || r.purification : <span className="faint">—</span>}
                  <div className="faint">{r.characterised.join(', ')}</div></td>
              </tr>
            ))}
            {rows && !sorted.length && <tr><td colSpan={7} className="empty">{params
              ? 'No reactions match these filters.' : 'No reactions yet. Draw one in an experiment’s Reaction section and it appears here.'}</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
