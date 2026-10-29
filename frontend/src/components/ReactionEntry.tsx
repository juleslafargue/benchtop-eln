import { useEffect, useRef, useState } from "react"
import { Link } from 'react-router-dom'
import { useApp } from '../App'
import { api, fmt, greenOk, type Material, type StoichRow } from '../api'
import { Mol, NumInput, StructureEditor } from './bits'
import MaterialPicker from './MaterialPicker'
import type { LookupResult } from './Safety'
import Modal from './Modal'
import type { EntryEditorProps } from './EntryCard'
import ConditionsBar, { type Conditions } from './ConditionsBar'
import SimilarReactions from './SimilarReactions'

const ROLE_ORDER: StoichRow['role'][] = ['reactant', 'reagent', 'catalyst', 'solvent', 'product']
const ROLE_LABEL: Record<StoichRow['role'], string> = {
  reactant: 'Reactants', reagent: 'Reagents', catalyst: 'Catalysts', solvent: 'Solvents', product: 'Products',
}

const ROLE_ONE: Record<StoichRow['role'], string> = { reactant: 'Reactant', reagent: 'Reagent', catalyst: 'Catalyst', solvent: 'Solvent', product: 'Product' }

/** What to call a row (in labels read aloud, and messages): its name, else its formula, else its place ("Reactant 2").
 *  Never the SMILES, and no name is made up. The server's chem.row_label says the same. */
export function rowLabel(r: StoichRow, rows: StoichRow[]): string {
  if (r.name?.trim()) return r.name.trim()
  if (r.formula?.trim()) return r.formula.trim()
  const same = rows.filter(x => x.role === r.role)
  return `${ROLE_ONE[r.role] || 'Row'} ${same.indexOf(r) + 1 || same.length || 1}`
}

/** Column names, as read aloud with the row's name ("Mass (mg), benzyl azide"). */
const COL_LABEL: Partial<Record<keyof StoichRow, string>> = {
  equiv: 'Equiv', mmol: 'mmol', mass_mg: 'Mass (mg)', volume_ml: 'Volume (mL)', density: 'Density (g/mL)', conc_m: 'Concentration (M)',
  purity: 'Purity (%)', actual_mass_mg: 'Isolated mass (mg)',
}

const blank = (role: StoichRow['role']): StoichRow => ({
  role, name: '', smiles: '', formula: '', mw: null, material_id: null, lot_id: null,
  equiv: role === 'product' ? 1 : null, mmol: null, mass_mg: null, volume_ml: null, density: null, conc_m: null,
  purity: 100, limiting: false, basis: role === 'solvent' ? 'volume' : 'equiv',
  actual_mass_mg: null, theoretical_mass_mg: null, yield_pct: null, notes: '',
})

export default function ReactionEntry({ content, onChange, readOnly, experimentId }: EntryEditorProps) {
  const { toast } = useApp()
  const rows: StoichRow[] = content.rows || []
  const [drawing, setDrawing] = useState<null | { mode: 'reaction' } | { mode: 'molecule'; row: number }>(null)
  const [picking, setPicking] = useState(false)
  const [zoom, setZoom] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [pasting, setPasting] = useState(false)
  const [deducting, setDeducting] = useState<StoichRow[] | null>(null)
  const [pasteText, setPasteText] = useState('')
  const reqId = useRef(0)
  const conditions: Conditions = content.conditions || {}

  /** Push edited rows to the server engine, then store the recomputed table. */
  const recompute = async (next: StoichRow[], extra: Record<string, any> = {}) => {
    onChange({ ...content, ...extra, rows: next }) // optimistic
    const my = ++reqId.current
    const target = (extra.conditions ?? conditions).target_conc_m ?? null
    try {
      const r = await api.post<{ rows: StoichRow[]; totals: any; warnings: string[] }>('/api/chem/stoichiometry', { rows: next, target_conc_m: target })
      if (my === reqId.current) onChange({ ...content, ...extra, rows: r.rows, totals: r.totals, warnings: r.warnings })
    } catch (e: any) { setErr(e.message) }
  }

  const setField = (i: number, patch: Partial<StoichRow>, basis?: StoichRow['basis']) => {
    const next = rows.map((r, j) => (j === i ? { ...r, ...patch, ...(basis ? { basis } : {}) } : r))
    if (patch.limiting) next.forEach((r, j) => { if (j !== i) r.limiting = false })
    recompute(next)
  }

  const addRow = (role: StoichRow['role']) => recompute([...rows, { ...blank(role), limiting: role === 'reactant' && !rows.some(r => r.limiting) }])
  const removeRow = (i: number) => recompute(rows.filter((_, j) => j !== i))

  const fromMaterial = (m: Material, role: StoichRow['role']) => {
    const pref = (m as any).preferred_lot_id
    const lot = m.lots?.find(l => l.id === pref && l.status === 'available') || m.lots?.find(l => l.status === 'available')
    const row: StoichRow = {
      ...blank(m.kind === 'solvent' ? 'solvent' : role), name: m.name, smiles: m.smiles, mw: m.mw, formula: m.formula,
      material_id: m.id, lot_id: lot?.id ?? null, density: m.density, purity: lot?.purity ?? 100,
    }
    if (row.role === 'reactant' && !rows.some(r => r.limiting)) { row.limiting = true; row.basis = 'mass' }
    recompute([...rows, row])
    setPicking(false)
  }

  const fromLookup = (r: LookupResult, role: StoichRow['role']) => {
    const row: StoichRow = { ...blank(role), name: r.name, smiles: r.smiles, mw: r.mw, formula: r.formula,
      density: role === 'solvent' ? r.density : null }  // a solid's density would turn masses into meaningless volumes
    if (row.role === 'reactant' && !rows.some(x => x.limiting)) { row.limiting = true; row.basis = 'mass' }
    recompute([...rows, row])
    setPicking(false)
  }

  const applyReaction = async (rxn: string) => {
    setBusy(true); setErr('')
    try {
      const parsed = await api.post<{ rows: StoichRow[]; reaction_smiles: string }>('/api/chem/reaction/parse', { reaction: rxn })
      // 1) unchanged structures keep everything; 2) an edited structure inherits the amounts the
      //    chemist typed for the row it replaced (same role, same order); 3) hand-added rows stay.
      const used = new Set<StoichRow>()
      const newSmiles = new Set(parsed.rows.map(p => p.smiles))
      const carried = parsed.rows.map(p => {
        const same = rows.find(r => r.smiles === p.smiles && !used.has(r))
        if (same) { used.add(same); return { ...p, ...same } }
        const prev = rows.find(r => r.role === p.role && r.smiles && !used.has(r) && !newSmiles.has(r.smiles))
        if (!prev) return { ...p, limiting: false }
        used.add(prev)
        return {
          ...p, basis: prev.basis, equiv: prev.equiv, mmol: prev.basis === 'mmol' ? prev.mmol : null,
          mass_mg: prev.basis === 'mass' ? prev.mass_mg : null, volume_ml: prev.basis === 'volume' ? prev.volume_ml : null,
          conc_m: prev.conc_m, limiting: prev.limiting, actual_mass_mg: prev.actual_mass_mg, purity: prev.purity,
        }
      })
      if (!carried.some(r => r.limiting)) { const first = carried.find(r => r.role === 'reactant'); if (first) first.limiting = true }
      const keep = rows.filter(r => !used.has(r) && (!r.smiles || ['solvent', 'catalyst', 'reagent'].includes(r.role)))
      await recompute([...carried, ...keep], { reaction_smiles: parsed.reaction_smiles })
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }

  const pasteReaction = () => { setPasteText(content.reaction_smiles || ''); setPasting(true) }

  const setConditions = (patch: Partial<Conditions>) => {
    const next = { ...conditions, ...patch }
    if ('target_conc_m' in patch && rows.length) recompute(rows, { conditions: next })
    else onChange({ ...content, conditions: next })
  }

  const deductable = (r: StoichRow) => !!r.lot_id && r.role !== 'product' && !(r as any).deducted && !!(r.mass_mg || r.volume_ml)
  const usage = (r: StoichRow) => {
    const useVol = r.basis === 'volume' || r.basis === 'fill' || (!r.mass_mg && !!r.volume_ml)
    return useVol ? { amount: r.volume_ml as number, unit: 'mL' } : { amount: r.mass_mg as number, unit: 'mg' }
  }
  const deduct = () => {
    const todo = rows.filter(deductable)
    if (!todo.length) { toast('Nothing to deduct: add rows from the registry (so they have a lot) and enter amounts first'); return }
    setDeducting(todo)
  }
  const runDeduct = async (todo: StoichRow[]) => {
    setDeducting(null)
    const next = [...rows]
    for (const r of todo) {
      const u = usage(r)
      try {
        const res = await api.post<{ transaction_id: number }>(`/api/lots/${r.lot_id}/transactions`, {
          ...u, direction: 'out', experiment_id: experimentId, note: `${rowLabel(r, rows)} in reaction`,
        })
        next[rows.indexOf(r)] = { ...r, deducted: { tx_id: res.transaction_id, ...u } } as StoichRow
      } catch (e: any) { toast(`${rowLabel(r, rows)}: ${e.message}`) }
    }
    onChange({ ...content, rows: next })
    toast('Inventory updated')
  }
  const undoDeduct = async (i: number) => {
    const d = (rows[i] as any).deducted
    try {
      if (d && typeof d === 'object') await api.post(`/api/lots/transactions/${d.tx_id}/reverse`)
      onChange({ ...content, rows: rows.map((r, j) => (j === i ? { ...r, deducted: null } : r)) })
      toast('Returned to inventory')
    } catch (e: any) { toast(e.message) }
  }

  const register = async (r: StoichRow) => {
    try {
      const res = await api.post<{ material: Material }>('/api/materials/register-product', {
        experiment_id: experimentId, name: r.name || r.formula || 'Product', smiles: r.smiles,
        amount_mg: r.actual_mass_mg, purity: r.purity,
      })
      const i = rows.indexOf(r)
      onChange({ ...content, rows: rows.map((x, j) => (j === i ? { ...x, material_id: res.material.id } : x)) })
      toast(`Registered as ${res.material.mid}`)
    } catch (e: any) { toast(e.message) }
  }

  const grouped = ROLE_ORDER.map(role => ({ role, items: rows.map((r, i) => ({ r, i })).filter(x => x.r.role === role) }))
    .filter(g => g.items.length)
  const totals = content.totals || {}
  // older records were saved before metrics existed: work them out for display without touching the record
  const [lateMetrics, setLateMetrics] = useState<any>(null)
  useEffect(() => {
    if (totals.metrics || !rows.length || lateMetrics) return
    api.post<{ totals: any }>('/api/chem/stoichiometry', { rows, target_conc_m: conditions.target_conc_m ?? null })
      .then(r => setLateMetrics(r.totals?.metrics || {})).catch(() => {})
  }, [totals.metrics, rows.length]) // eslint-disable-line react-hooks/exhaustive-deps
  const nonProduct = grouped.filter(g => g.role !== 'product')
  const products = grouped.find(g => g.role === 'product')?.items || []

  const nameCell = (r: StoichRow, i: number) => (
    <td className="name-cell">
      <div className="row" style={{ gap: 4, flexWrap: 'nowrap' }}>
        <input className="text" aria-label={r.name ? 'Name' : `Name of ${rowLabel(r, rows)}`} disabled={readOnly} value={r.name}
          placeholder={r.formula || ROLE_ONE[r.role]} title={r.name || undefined}
          onChange={e => onChange({ ...content, rows: rows.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)) })} />
        {r.material_id && <Link to={`/materials/${r.material_id}`} className="faint" title="Open in registry">reg</Link>}
      </div>
    </td>
  )
  const structCell = (r: StoichRow, i: number) => (
    <td className="struct-cell">
      {r.smiles
        ? <button className="btn ghost sm" style={{ padding: 0 }} onClick={() => setZoom(r.smiles)} aria-label={`Enlarge the structure of ${rowLabel(r, rows)}`}>
          <Mol className="mol-thumb" smiles={r.smiles} w={128} h={80} alt={`Structure of ${rowLabel(r, rows)}`} /></button>
        : !readOnly && <button className="btn ghost sm" onClick={() => setDrawing({ mode: 'molecule', row: i })} aria-label={`Draw the structure of ${rowLabel(r, rows)}`}>Draw</button>}
    </td>
  )
  const UNIT_OF: Partial<Record<keyof StoichRow, 'mg' | 'mL' | 'mmol' | 'M'>> = { mass_mg: 'mg', actual_mass_mg: 'mg', volume_ml: 'mL', mmol: 'mmol', conc_m: 'M' }
  const n = (r: StoichRow, i: number, k: keyof StoichRow, basis?: StoichRow['basis'], nd = 4) => (
    <td><NumInput label={`${COL_LABEL[k] || String(k)}, ${rowLabel(r, rows)}`} nd={nd} sig={5} disabled={readOnly} value={r[k] as number | null} unit={UNIT_OF[k]}
      className={basis && r.basis === basis ? 'basis' : ''} onCommit={v => setField(i, { [k]: v } as Partial<StoichRow>, basis)} /></td>
  )

  return (
    <div className="stack" style={{ gap: 10 }}>
      <div className={`rxn-scheme${content.reaction_smiles ? '' : ' empty'}`}>
        {content.reaction_smiles
          ? <Mol rxn smiles={content.reaction_smiles} w={760} h={190} />
          : <span>No reaction scheme yet. Draw one to fill the table from the structures.</span>}
      </div>
      {!readOnly && (
        <div className="row">
          <button className="btn" onClick={() => setDrawing({ mode: 'reaction' })} disabled={busy}>{content.reaction_smiles ? 'Edit reaction' : 'Draw reaction'}</button>
          <button className="btn ghost" onClick={pasteReaction}>Paste reaction SMILES</button>
          <span className="grow" />
          <button className="btn ghost" onClick={() => setPicking(true)}>Add from registry</button>
          <select className="select" value="" aria-label="Add blank row" onChange={e => e.target.value && addRow(e.target.value as StoichRow['role'])}>
            <option value="">Add row…</option>
            {ROLE_ORDER.map(r => <option key={r} value={r}>{ROLE_LABEL[r].slice(0, -1)}</option>)}
          </select>
        </div>
      )}
      {err && <p className="error">{err}</p>}
      <ConditionsBar value={conditions} onChange={setConditions} readOnly={readOnly} />

      {rows.length > 0 && (
        <div className="grid-wrap">
          <table className="grid rxn-grid">
            <thead><tr>
              <th><span className="vh">Structure</span></th><th className="name-h">Name</th><th>MW</th><th title="Limiting reagent">Lim.</th><th>Equiv</th><th>mmol</th>
              <th>Mass (mg)</th><th>Vol (mL)</th><th title="Density">ρ (g/mL)</th><th title="Concentration of a solution">Conc (M)</th><th>Purity %</th><th><span className="vh">Remove</span></th>
            </tr></thead>
            <tbody>
              {nonProduct.map(g => [
                <tr className="group" key={g.role}><td colSpan={12}>{ROLE_LABEL[g.role]}</td></tr>,
                ...g.items.map(({ r, i }) => (
                  <tr key={i}>
                    {structCell(r, i)}{nameCell(r, i)}
                    <td className="calc" title={r.formula}>{fmt(r.mw, 2)}</td>
                    <td style={{ textAlign: 'center' }}>{g.role !== 'solvent' &&
                      <input type="radio" className="lim-dot" name={`lim-${experimentId}`} aria-label={`Limiting reagent: ${rowLabel(r, rows)}`} disabled={readOnly}
                        checked={r.limiting} onChange={() => setField(i, { limiting: true }, r.mass_mg ? 'mass' : 'mmol')} />}</td>
                    {g.role === 'solvent'
                      ? <td colSpan={2}><label className="fill-toggle" title="Calculate this solvent's volume from the target concentration">
                          <input type="checkbox" disabled={readOnly} checked={r.basis === 'fill'}
                            onChange={e => setField(i, {}, e.target.checked ? 'fill' : 'volume')} /> fill to {conditions.target_conc_m ? `${conditions.target_conc_m} M` : 'target M'}
                        </label></td>
                      : <>{n(r, i, 'equiv', 'equiv', 3)}{n(r, i, 'mmol', 'mmol')}</>}
                    {n(r, i, 'mass_mg', 'mass', 3)}
                    {r.basis === 'fill' ? <td className="calc"><b>{fmt(r.volume_ml, 3)}</b></td> : n(r, i, 'volume_ml', 'volume')}
                    {n(r, i, 'density', undefined, 4)}{n(r, i, 'conc_m', undefined, 4)}{n(r, i, 'purity', undefined, 2)}
                    <td>{!readOnly && <button className="btn ghost sm" onClick={() => removeRow(i)} aria-label={`Remove ${rowLabel(r, rows)}`}>×</button>}
                      {(r as any).deducted && <span className="faint used" title="Deducted from inventory">✓ used
                        {!readOnly && <button className="btn ghost sm" style={{ padding: '0 4px' }} onClick={() => undoDeduct(i)}>undo</button>}</span>}</td>
                  </tr>
                )),
              ])}
            </tbody>
          </table>
        </div>
      )}

      {products.length > 0 && (
        <div className="grid-wrap">
          <table className="grid rxn-grid">
            <thead><tr>
              <th><span className="vh">Structure</span></th><th className="name-h">Product</th><th>MW</th><th>Equiv</th><th title="Theoretical amount">Theor. mmol</th><th title="Theoretical mass">Theor. mass (mg)</th>
              <th>Isolated (mg)</th><th>Purity %</th><th>Yield</th><th><span className="vh">Register or remove</span></th>
            </tr></thead>
            <tbody>
              {products.map(({ r, i }) => (
                <tr key={i}>
                  {structCell(r, i)}{nameCell(r, i)}
                  <td className="calc">{fmt(r.mw, 2)}</td>
                  {n(r, i, 'equiv', undefined, 3)}
                  <td className="calc">{fmt(r.mmol, 4)}</td>
                  <td className="calc">{fmt(r.theoretical_mass_mg, 2)}</td>
                  {n(r, i, 'actual_mass_mg', undefined, 2)}{n(r, i, 'purity', undefined, 2)}
                  <td className="calc yield">{r.yield_pct !== null ? `${fmt(r.yield_pct, 1)}%` : ''}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    {!readOnly && r.actual_mass_mg && !r.material_id && <button className="btn sm" onClick={() => register(r)} title="Add the product to the registry, as a lot of what you isolated">Register</button>}
                    {!readOnly && <button className="btn ghost sm" onClick={() => removeRow(i)} aria-label={`Remove ${rowLabel(r, rows)}`}>×</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rows.length > 0 && (
        <div className="spread">
          <div className="totals">
            <span>Limiting: <b>{fmt(totals.limiting_mmol, 4) || '—'}</b> mmol</span>
            <span>Total volume: <b>{fmt(totals.total_volume_ml, 2) || '—'}</b> mL</span>
            {totals.concentration_m && <span>Concentration: <b>{fmt(totals.concentration_m, 3)}</b> M</span>}
          </div>
          {!readOnly && <button className="btn sm" onClick={deduct}>Deduct amounts from inventory</button>}
        </div>
      )}
      {(() => {
        const m = totals.metrics || lateMetrics || {}
        const g = greenOk(m)
        const items: [string, string, string][] = [
          ['Atom economy', g.ae != null ? `${fmt(g.ae, 1)}%` : '', 'Product MW ÷ sum of the MWs of the rows marked Reactant (one of each). Reagents, catalysts and solvents don\'t count. Independent of yield.'],
          ['RME', g.rme != null ? `${fmt(g.rme, 1)}%` : '', 'Reaction mass efficiency: isolated product ÷ mass of the rows marked Reactant.'],
          ['E-factor', m.e_factor != null ? fmt(m.e_factor, 1) : '', 'kg of waste per kg of product: everything in the table except the product.'],
          ['PMI', m.pmi != null ? fmt(m.pmi, 1) : '', 'Process mass intensity: total mass in ÷ mass of product (= E-factor + 1).'],
          ['Solvent', m.solvent_l_per_kg != null ? `${fmt(m.solvent_l_per_kg, 0)} L/kg` : '', 'Reaction solvent volume per kg of product.'],
        ]
        const shown = items.filter(i => i[1])
        const impossible = [g.aeImpossible && 'atom economy', g.rmeImpossible && 'RME'].filter(Boolean).join(' and ')
        if (!shown.length && !impossible) return null
        return <div className="metrics" aria-label="Green chemistry metrics">
          <span className="fld-cap" style={{ alignSelf: 'center' }}>Green metrics</span>
          {shown.map(([k, v, t]) => <span key={k} title={t}>{k} <b>{v}</b></span>)}
          {impossible && <span className="warn-text metrics-roles">{impossible[0].toUpperCase() + impossible.slice(1)} would be over 100 %.
            Check {g.aeImpossible ? '' : 'the isolated mass and '}the roles: a co-reactant may be marked as Reagent (only Reactants count).</span>}
          <span className="faint" title={m.scope}>reaction only{m.missing_mass?.length ? ` · E-factor/PMI need a mass for ${m.missing_mass.map((x: string) => {
            const r = rows.find(r => r.smiles && r.smiles === x)  // saved before 0.29, which named unnamed rows by their SMILES
            return r ? rowLabel(r, rows) : x
          }).join(', ')}` : ''}</span>
        </div>
      })()}
      {(content.warnings || []).map((w: string) => <p className="warn" key={w}>{w}</p>)}
      <SimilarReactions rows={rows} experimentId={experimentId} readOnly={readOnly}
        onUse={(add, cond) => recompute([...rows, ...add.map(r => ({ ...blank(r.role || 'reagent'), ...r } as StoichRow))], { conditions: { ...conditions, ...cond } })} />
      {!readOnly && rows.length > 0 && <p className="faint">The highlighted cell in each row is the value you entered; the rest are calculated from it.</p>}

      {drawing && (
        <StructureEditor
          mode={drawing.mode}
          initial={drawing.mode === 'reaction' ? content.reaction_smiles : undefined}
          onClose={() => setDrawing(null)}
          onDone={async ({ smiles }) => {
            const d = drawing
            setDrawing(null)
            if (d.mode === 'reaction') { if (smiles) applyReaction(smiles) }
            else if (smiles) setField(d.row, { smiles, mw: null })
          }}
        />
      )}
      {deducting && <Modal title="Deduct from inventory" onClose={() => setDeducting(null)} footer={<>
        <button className="btn" onClick={() => setDeducting(null)}>Cancel</button>
        <button className="btn primary" onClick={() => runDeduct(deducting)}>Deduct {deducting.length} amount{deducting.length === 1 ? '' : 's'}</button>
      </>}>
        <p style={{ marginTop: 0 }}>These amounts will be taken from their lots and linked to this experiment. You can undo each one afterwards.</p>
        <table className="plain"><thead><tr><th>Material</th><th>Amount</th></tr></thead>
          <tbody>{deducting.map((r, k) => { const u = usage(r); return <tr key={k}><td>{rowLabel(r, rows)}</td><td className="num">{fmt(u.amount, 3)} {u.unit}</td></tr> })}</tbody></table>
        <p className="faint">Masses are as weighed (already corrected for purity), so they match what leaves the bottle.</p>
      </Modal>}
      {pasting && <Modal title="Paste reaction SMILES" onClose={() => setPasting(false)} footer={<>
        <button className="btn" onClick={() => setPasting(false)}>Cancel</button>
        <button className="btn primary" disabled={!pasteText.trim()} onClick={() => { setPasting(false); applyReaction(pasteText.trim()) }}>Use reaction</button>
      </>}>
        <label className="field">Reactants &gt; agents &gt; products, e.g. <code>CC(=O)O.OCC&gt;[H+]&gt;CC(=O)OCC</code>
          <textarea className="input" rows={3} autoFocus value={pasteText} onChange={e => setPasteText(e.target.value)} style={{ fontFamily: 'var(--mono)', fontSize: 13 }} /></label>
        {pasteText.includes('>') && <div className="rxn-scheme" style={{ marginTop: 10, minHeight: 80 }}><Mol rxn smiles={pasteText.trim()} w={520} h={130} /></div>}
      </Modal>}
      {picking && <MaterialPicker onClose={() => setPicking(false)} onPick={(m, role) => fromMaterial(m, role)} onLookup={fromLookup} />}
      {zoom && <Modal title="Structure" onClose={() => setZoom(null)} footer={<button className="btn" onClick={() => setZoom(null)}>Close</button>}>
        <div style={{ textAlign: 'center' }}><Mol smiles={zoom} w={480} h={320} /></div>
        <p className="hash" style={{ textAlign: 'center' }}>{zoom}</p>
      </Modal>}
    </div>
  )
}
