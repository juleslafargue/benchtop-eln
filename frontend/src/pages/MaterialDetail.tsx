import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useApp } from '../App'
import { unitLabel, api, displayName, fmt, when, type Lot, type Material } from '../api'
import { StatusDot } from '../components/ExperimentList'
import { Mol, NumInput } from '../components/bits'
import Modal from '../components/Modal'
import { MaterialForm } from './Materials'
import { GhsDetails, type SafetySummary } from '../components/Safety'

const UNITS = ['mg', 'g', 'kg', 'uL', 'mL', 'L']

function LotForm({ material, onSaved, onClose }: { material: Material; onSaved: () => void; onClose: () => void }) {
  const [f, setF] = useState({ amount: null as number | null, unit: material.kind === 'solvent' ? 'mL' : 'g', location: '', supplier: '', catalog_number: '', purity: null as number | null, barcode: '' })
  const [err, setErr] = useState('')
  const save = async () => {
    try { await api.post(`/api/materials/${material.id}/lots`, f); onSaved() } catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title={`Receive a lot of ${material.name}`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!f.amount} onClick={save}>Add lot</button></>}>
      <div className="form-grid">
        <label className="field">Amount<NumInput className="input" label="Amount" value={f.amount} onCommit={v => setF({ ...f, amount: v })} /></label>
        <label className="field">Unit<select className="select" value={f.unit} onChange={e => setF({ ...f, unit: e.target.value })}>{UNITS.map(u => <option key={u} value={u}>{unitLabel(u)}</option>)}</select></label>
        <label className="field">Location<input className="input" value={f.location} onChange={e => setF({ ...f, location: e.target.value })} /></label>
        <label className="field">Supplier<input className="input" value={f.supplier} onChange={e => setF({ ...f, supplier: e.target.value })} /></label>
        <label className="field">Catalogue no.<input className="input" value={f.catalog_number} onChange={e => setF({ ...f, catalog_number: e.target.value })} /></label>
        <label className="field">Purity %<NumInput className="input" label="Purity" value={f.purity} onCommit={v => setF({ ...f, purity: v })} /></label>
        <label className="field">Barcode<input className="input" placeholder="Generated if empty" value={f.barcode} onChange={e => setF({ ...f, barcode: e.target.value })} /></label>
      </div>
      {err && <p className="error">{err}</p>}
    </Modal>
  )
}

export function LotHistory({ lot, onClose }: { lot: Lot; onClose: () => void }) {
  const [tx, setTx] = useState<any[]>([])
  useEffect(() => { api.get<any[]>(`/api/lots/${lot.id}/transactions`).then(setTx) }, [lot.id])
  return (
    <Modal title={`Usage of ${lot.barcode}`} onClose={onClose} footer={<button className="btn" onClick={onClose}>Close</button>}>
      <table className="plain"><thead><tr><th>When</th><th>Change</th><th>Balance</th><th>By</th><th>Note</th></tr></thead>
        <tbody>{tx.map(t => <tr key={t.id}><td>{when(t.created_at)}</td><td className="num">{t.delta > 0 ? '+' : ''}{fmt(t.delta, 4)} {unitLabel(lot.unit)}</td>
          <td className="num">{fmt(t.balance_after, 4)}</td><td>{displayName(t.user)}</td>
          <td>{t.experiment?.id ? <Link to={`/experiments/${t.experiment.id}`}>{t.experiment.eid}</Link>
            : t.experiment ? <span title="A record you can't open">{t.experiment.eid}</span> : null} {t.note}</td></tr>)}</tbody></table>
    </Modal>
  )
}

export default function MaterialDetail() {
  const { id } = useParams()
  const { canWrite, toast } = useApp()
  const [refreshing, setRefreshing] = useState(false)
  const [m, setM] = useState<Material | null>(null)
  const [editing, setEditing] = useState(false)
  const [addingLot, setAddingLot] = useState(false)
  const [history, setHistory] = useState<Lot | null>(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => api.get<Material>(`/api/materials/${id}`).then(x => { setM(x); setErr('') }).catch(e => setErr(e.message)), [id])
  useEffect(() => { load() }, [load])
  if (err && !m) return <div className="stack"><h1 className="page-title">Material not found</h1><p className="lede">{err}</p><p><Link to="/materials">Back to Materials</Link></p></div>
  if (!m) return null

  return (
    <div className="stack" style={{ gap: 22 }}>
      <div className="spread">
        <div>
          <span className="eid">{m.mid}</span>
          <h1 className="page-title">{m.name}</h1>
          <p className="lede">{m.kind}{m.source_experiment && <> · made in {m.source_experiment.id ? <Link to={`/experiments/${m.source_experiment.id}`}>{m.source_experiment.eid}</Link> : <span title="A record you can't open">{m.source_experiment.eid}</span>}</>}</p>
        </div>
        {canWrite && <button className="btn" onClick={() => setEditing(true)}>Edit</button>}
      </div>
      <div className="panel panel-pad mat-overview">
        <div className="mat-thumb" style={{ width: '100%', height: 200 }}>{m.smiles ? <Mol smiles={m.smiles} w={420} h={280} /> : <span className="faint">No structure</span>}</div>
        <dl className="kv">
          <dt>Formula</dt><dd>{m.formula || '—'}</dd>
          <dt>MW</dt><dd>{m.mw ? `${fmt(m.mw, 3)} g/mol` : '—'}</dd>
          <dt>CAS</dt><dd>{m.cas || '—'}</dd>
          <dt>Density</dt><dd>{m.density ? `${m.density} g/mL` : '—'}</dd>
          <dt>InChIKey</dt><dd className="hash">{m.inchikey || '—'}</dd>
          <dt>SMILES</dt><dd className="hash">{m.smiles || '—'}</dd>
          <dt>Synonyms</dt><dd>{m.synonyms || '—'}</dd>
          {m.hazards && <><dt>Hazard notes</dt><dd>{m.hazards}</dd></>}
          {m.notes && <><dt>Notes</dt><dd>{m.notes}</dd></>}
        </dl>
      </div>

      <section className="stack" style={{ gap: 8 }}>
        <div className="spread"><h2 style={{ fontSize: 17 }}>Safety (GHS)</h2>
          {(m.safety as SafetySummary | null)?.status !== 'unavailable' && <button className="btn sm" disabled={refreshing} onClick={async () => {
            setRefreshing(true)
            try { await api.post(`/api/materials/${m.id}/safety/refresh`, {}); await load(); toast('Safety data refreshed from PubChem') }
            catch (e: any) { toast(e.message) } finally { setRefreshing(false) }
          }}>{refreshing ? 'Checking PubChem…' : 'Refresh now'}</button>}</div>
        {(m.safety as SafetySummary | null)?.change_unacknowledged && (() => {
          const s = m.safety as SafetySummary
          return <div className="sp-note warn" role="status"><span><b>The classification changed</b> at the last check{s.change?.added?.length ? `: now includes ${s.change.added.join(', ')}` : ''}
            {s.change?.removed?.length ? `${s.change?.added?.length ? ';' : ':'} no longer lists ${s.change.removed.join(', ')}` : ''}
            {s.change?.signal_before !== s.change?.signal_after ? ` (signal word ${s.change?.signal_before || 'none'} → ${s.change?.signal_after || 'none'})` : ''}.
            Review your risk assessments that use it.</span>
            <button className="btn sm" onClick={async () => { await api.post(`/api/materials/${m.id}/safety/acknowledge`, {}); load() }}>Mark as reviewed</button></div>
        })()}
        <div className="panel panel-pad">
          {m.safety ? <GhsDetails s={m.safety as SafetySummary} /> : <p className="faint">No safety data yet. Click Refresh now to fetch it from PubChem.</p>}
          <p className="faint" style={{ margin: '8px 0 0', fontSize: 12.5 }}>Public classifications for the pure substance. The supplier's SDS for your actual container (grade, solution, stabiliser) takes precedence.</p>
        </div>
      </section>

      <section className="stack" style={{ gap: 8 }}>
        <div className="spread"><h2 style={{ fontSize: 17 }}>Lots</h2>{canWrite && <button className="btn sm" onClick={() => setAddingLot(true)}>Receive lot</button>}</div>
        <div className="panel grid-wrap" style={{ border: 0 }}>
          <table className="plain">
            <thead><tr><th>Barcode</th><th>Amount</th><th>Location</th><th>Supplier</th><th>Purity</th><th>Status</th><th>Received</th><th><span className="vh">Actions</span></th></tr></thead>
            <tbody>
              {(m.lots || []).map(l => (
                <tr key={l.id}>
                  <td><b style={{ fontWeight: 600 }}>{l.barcode}</b></td><td className="num">{fmt(l.amount, 3)} {unitLabel(l.unit)}</td><td>{l.location}</td>
                  <td>{l.supplier}</td><td>{l.purity ? `${l.purity}%` : ''}</td><td>{l.status}</td><td>{when(l.received_at)}</td>
                  <td><button className="btn ghost sm" onClick={() => setHistory(l)}>Usage</button></td>
                </tr>
              ))}
              {!m.lots?.length && <tr><td colSpan={8} className="faint">No lots on hand.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      <section className="stack" style={{ gap: 8 }}>
        <h2 style={{ fontSize: 17 }}>Used in</h2>
        <div className="panel">
          <ul className="list">
            {(m.used_in || []).map(e => <li key={e.id}><Link className="list-row" style={{ gridTemplateColumns: '92px 1fr auto' }} to={`/experiments/${e.id}`}>
              <span className="eid">{e.eid}</span><span>{e.title}</span><StatusDot status={e.status} /></Link></li>)}
            {!m.used_in?.length && !m.used_in_hidden && <li className="empty">Not linked to any reaction tables yet.</li>}
            {!!m.used_in_hidden && <li className="empty">{m.used_in?.length ? 'Also in' : 'In'} {m.used_in_hidden} record{m.used_in_hidden === 1 ? '' : 's'} you
              can't open (in notebooks not shared with you).</li>}
          </ul>
        </div>
      </section>
      {editing && <MaterialForm initial={m} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); load() }} />}
      {addingLot && <LotForm material={m} onClose={() => setAddingLot(false)} onSaved={() => { setAddingLot(false); load() }} />}
      {history && <LotHistory lot={history} onClose={() => setHistory(null)} />}
    </div>
  )
}
