import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { day, api, displayName, fmt, type StoichRow, type UserBrief } from '../api'
import { Mol } from './bits'
import Modal from './Modal'
import type { Conditions } from './ConditionsBar'

export interface SimilarReaction {
  experiment_id: number; eid: string; title: string; status: string; owner: UserBrief | null; date: string; entry_id: number
  score: number; kind: 'same' | 'same_product' | 'analogue' | 'same_substrate' | 'related'
  reaction_smiles: string; substrates: string[]; products: string[]; reagents: string[]; solvents: string[]
  temperature_c: number | null; temperature_label: string | null; time_h: number | null; atmosphere: string | null
  yield_pct: number | null; notes: string
  condition_rows: Partial<StoichRow>[]; conditions_raw: Conditions
}

const KIND: Record<SimilarReaction['kind'], string> = {
  same: 'Same reaction', same_product: 'Same product, different route', analogue: 'Same transformation, different substrate',
  same_substrate: 'Similar starting materials', related: 'Related',
}

function conditionsLine(r: SimilarReaction) {
  const t = r.temperature_c != null ? `${fmt(r.temperature_c, 0)} °C` : r.temperature_label
  return [...r.reagents, ...r.solvents.map(s => `in ${s}`), t, r.time_h != null ? `${fmt(r.time_h, 1)} h` : null, r.atmosphere]
    .filter(Boolean).join(' · ')
}

/** "Have we done this before?": past reactions that look like this one, with how they turned out. */
export default function SimilarReactions({ rows, experimentId, readOnly, onUse }: {
  rows: StoichRow[]; experimentId: number; readOnly: boolean
  onUse: (rows: Partial<StoichRow>[], conditions: Conditions) => void
}) {
  // only structures and roles matter; typing amounts shouldn't re-run the search
  const key = useMemo(() => JSON.stringify(rows.filter(r => r.smiles).map(r => [r.role, r.smiles, r.limiting ? 1 : 0, r.name])), [rows])
  const [res, setRes] = useState<{ results: SimilarReaction[]; searched: number } | null>(null)
  const [open, setOpen] = useState(true)
  const [all, setAll] = useState(false)
  const [using, setUsing] = useState<SimilarReaction | null>(null)

  useEffect(() => {
    const structures = JSON.parse(key) as unknown[]
    if (!structures.length) { setRes(null); return }
    let alive = true
    const t = setTimeout(() => {
      api.post<{ results: SimilarReaction[]; searched: number }>('/api/reactions/similar', { rows, experiment_id: experimentId, limit: 6 })
        .then(r => { if (alive) setRes(r) }).catch(() => { if (alive) setRes(null) })
    }, 500)
    return () => { alive = false; clearTimeout(t) }
  }, [key, experimentId]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!res) return null
  const shown = all ? res.results : res.results.slice(0, 3)
  const have = new Set(rows.flatMap(r => [r.smiles, (r.name || '').toLowerCase()]).filter(Boolean))
  const toAdd = (s: SimilarReaction) => s.condition_rows.filter(r => !(r.smiles && have.has(r.smiles)) && !(r.name && have.has(r.name.toLowerCase())))

  return (
    <section className={`similar${open ? ' open' : ''}`} aria-label="Similar reactions">
      <button className="similar-head" onClick={() => setOpen(o => !o)} aria-expanded={open}>
        <span className="similar-title">Have we done this before?</span>
        <span className="faint">{res.results.length
          ? `${res.results.length}${res.results.length >= 6 ? '+' : ''} similar reaction${res.results.length === 1 ? '' : 's'} in this lab`
          : `nothing similar among ${res.searched} reaction${res.searched === 1 ? '' : 's'} you can see`}</span>
        <span className="similar-caret" aria-hidden="true">{open ? '▾' : '▸'}</span>
      </button>
      {open && res.results.length > 0 && <div className="similar-list">
        {shown.map(s => (
          <article key={s.entry_id} className={`similar-card kind-${s.kind}`}>
            <div className="similar-scheme">{s.reaction_smiles ? <Mol rxn smiles={s.reaction_smiles} w={300} h={90} /> : null}</div>
            <div className="similar-body">
              <div className="similar-top">
                <span className="similar-kind">{KIND[s.kind]}</span>
                <span className="similar-score" title="How alike the two reactions are, from structures, the transformation and the reagents">{Math.round(s.score * 100)}% match</span>
              </div>
              <Link to={`/experiments/${s.experiment_id}`} className="similar-link"><b>{s.eid}</b> {s.title}</Link>
              <div className="faint">{s.owner ? displayName(s.owner) : ''} · {day(s.date)} · <span className={`status ${s.status}`}>{s.status}</span></div>
              {conditionsLine(s) && <div className="similar-cond">{conditionsLine(s)}</div>}
              {s.notes && <div className="similar-notes">“{s.notes}”</div>}
            </div>
            <div className="similar-side">
              <div className={`similar-yield${s.yield_pct == null ? ' none' : s.yield_pct < 30 ? ' low' : ''}`}>
                {s.yield_pct != null ? `${fmt(s.yield_pct, 0)}%` : '—'}<span>{s.yield_pct != null ? 'yield' : 'no yield'}</span></div>
              {!readOnly && s.condition_rows.length + Object.keys(s.conditions_raw).length > 0 &&
                <button className="btn sm" onClick={() => setUsing(s)}>Use these conditions</button>}
            </div>
          </article>
        ))}
        {res.results.length > 3 && <button className="btn ghost sm" onClick={() => setAll(a => !a)}>{all ? 'Show fewer' : `Show ${res.results.length - 3} more`}</button>}
      </div>}
      {using && (() => {
        const add = toAdd(using)
        const c = using.conditions_raw
        const condBits = [c.temperature_c != null ? `${c.temperature_c} °C` : c.temperature_label, c.time_h != null ? `${c.time_h} h` : null,
          c.atmosphere, c.target_conc_m != null ? `${c.target_conc_m} M` : null].filter(Boolean)
        return <Modal title={`Use conditions from ${using.eid}`} onClose={() => setUsing(null)} footer={<>
          <button className="btn" onClick={() => setUsing(null)}>Cancel</button>
          <button className="btn primary" onClick={() => { onUse(add, c); setUsing(null) }}>Use conditions</button>
        </>}>
          <p style={{ marginTop: 0 }}>Your starting materials and products stay as they are.</p>
          {add.length > 0 ? <>
            <p className="fld-cap">Rows added</p>
            <ul className="similar-adds">{add.map((r, k) => <li key={k}><b>{r.name || r.formula}</b> <span className="faint">{r.role}{
              r.basis === 'equiv' && r.equiv != null ? ` · ${r.equiv} equiv` : r.basis === 'fill' ? ' · fill to target concentration' : r.volume_ml != null ? ` · ${r.volume_ml} mL` : ''}</span></li>)}</ul>
          </> : <p className="faint">Its reagents, catalysts and solvents are already in your table.</p>}
          {condBits.length > 0 && <p><span className="fld-cap">Conditions set to</span><br />{condBits.join(' · ')}</p>}
          <p className="faint">Amounts are copied as equivalents, so they scale to your limiting reagent.</p>
        </Modal>
      })()}
    </section>
  )
}
