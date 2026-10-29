import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApp } from '../App'
import { unitLabel, api, fmt, type Lot } from '../api'
import { NumInput, useDebounced } from '../components/bits'
import Modal from '../components/Modal'
import { LotHistory } from './MaterialDetail'

function AdjustDialog({ lot, onClose, onDone }: { lot: Lot; onClose: () => void; onDone: () => void }) {
  const [amount, setAmount] = useState<number | null>(null)
  const [unit, setUnit] = useState(lot.unit)
  const [direction, setDirection] = useState('out')
  const [note, setNote] = useState('')
  const [err, setErr] = useState('')
  const go = async () => {
    try { await api.post(`/api/lots/${lot.id}/transactions`, { amount, unit, direction, note }); onDone() } catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title={`Adjust ${lot.barcode}`} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!amount} onClick={go}>Record change</button></>}>
      <p className="muted" style={{ marginTop: 0 }}>{lot.material_name}: {fmt(lot.amount, 4)} {unitLabel(lot.unit)} on hand.</p>
      <div className="form-grid">
        <label className="field">Change<select className="select" value={direction} onChange={e => setDirection(e.target.value)}><option value="out">Remove (used)</option><option value="in">Add (correction)</option></select></label>
        <label className="field">Amount<NumInput className="input" label="Amount" value={amount} onCommit={setAmount} /></label>
        <label className="field">Unit<select className="select" value={unit} onChange={e => setUnit(e.target.value)}>{['mg', 'g', 'kg', 'uL', 'mL', 'L'].map(u => <option key={u} value={u}>{unitLabel(u)}</option>)}</select></label>
      </div>
      <label className="field" style={{ marginTop: 12 }}>Note<input className="input" value={note} onChange={e => setNote(e.target.value)} /></label>
      {err && <p className="error">{err}</p>}
    </Modal>
  )
}

export default function Inventory() {
  const { canWrite } = useApp()
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('available')
  const [lots, setLots] = useState<Lot[]>([])
  const [adjust, setAdjust] = useState<Lot | null>(null)
  const [history, setHistory] = useState<Lot | null>(null)
  const dq = useDebounced(q, 200)
  const load = () => api.get<Lot[]>(`/api/lots?q=${encodeURIComponent(dq)}${status ? `&status=${status}` : ''}`).then(setLots)
  useEffect(() => { load() }, [dq, status]) // eslint-disable-line react-hooks/exhaustive-deps

  const setLotStatus = async (l: Lot, s: string) => { await api.patch(`/api/lots/${l.id}`, { status: s }); load() }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div><h1 className="page-title">Inventory</h1><p className="lede">Every container on hand. Amounts go down automatically when you deduct a reaction's reagents.</p></div>
      <div className="row">
        <input className="input grow" placeholder="Barcode, material name or CAS" value={q} onChange={e => setQ(e.target.value)} />
        <select className="select" value={status} onChange={e => setStatus(e.target.value)} aria-label="Status">
          <option value="">Any status</option><option value="available">Available</option><option value="empty">Empty</option>
          <option value="quarantined">Quarantined</option><option value="disposed">Disposed</option>
        </select>
      </div>
      <div className="panel grid-wrap" style={{ border: 0 }}>
        <table className="plain">
          <thead><tr><th>Barcode</th><th>Material</th><th>On hand</th><th>Location</th><th>Supplier</th><th>Status</th><th><span className="vh">Actions</span></th></tr></thead>
          <tbody>
            {lots.map(l => (
              <tr key={l.id}>
                <td><b style={{ fontWeight: 600 }}>{l.barcode}</b></td>
                <td><Link to={`/materials/${l.material_id}`}>{l.material_name}</Link> <span className="faint">{l.mid}</span></td>
                <td className="num">{fmt(l.amount, 3)} {unitLabel(l.unit)}</td><td>{l.location}</td><td>{l.supplier}</td>
                <td>{canWrite ? <select className="select" style={{ padding: '2px 6px' }} value={l.status} onChange={e => setLotStatus(l, e.target.value)} aria-label={`Status of ${l.barcode}`}>
                  {['available', 'empty', 'quarantined', 'disposed'].map(s => <option key={s}>{s}</option>)}</select> : l.status}</td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  {canWrite && <button className="btn sm" onClick={() => setAdjust(l)}>Adjust</button>}
                  <button className="btn ghost sm" onClick={() => setHistory(l)}>Usage</button>
                </td>
              </tr>
            ))}
            {!lots.length && <tr><td colSpan={7} className="empty">No containers match. Receive lots from a material's page.</td></tr>}
          </tbody>
        </table>
      </div>
      {adjust && <AdjustDialog lot={adjust} onClose={() => setAdjust(null)} onDone={() => { setAdjust(null); load() }} />}
      {history && <LotHistory lot={history} onClose={() => setHistory(null)} />}
    </div>
  )
}
