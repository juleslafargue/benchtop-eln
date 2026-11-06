import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useApp } from '../App'
import { unitLabel, api, day, fmt, type Lot, type Material } from '../api'
import { Mol, NumInput, useDebounced } from '../components/bits'
import Modal from '../components/Modal'
import MaterialPicker from '../components/MaterialPicker'
import Scanner from '../components/Scanner'
import { SafetyBadge, SignalWord } from '../components/Safety'
import { expiryDay, lotProblem, type ScanAnswer, type ScannedLot } from '../scan'
import { LotForm, LotHistory } from './MaterialDetail'

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
        <label className="field">Amount<NumInput className="input" label="Amount" value={amount} onCommit={setAmount} live={0} /></label>
        <label className="field">Unit<select className="select" value={unit} onChange={e => setUnit(e.target.value)}>{['mg', 'g', 'kg', 'uL', 'mL', 'L'].map(u => <option key={u} value={u}>{unitLabel(u)}</option>)}</select></label>
      </div>
      <label className="field" style={{ marginTop: 12 }}>Note<input className="input" value={note} onChange={e => setNote(e.target.value)} /></label>
      {err && <p className="error">{err}</p>}
    </Modal>
  )
}

const STATUSES = ['available', 'empty', 'quarantined', 'disposed']

/** One scanned bottle: what it is, how much is left and where, its hazards, and what can be done with it. */
function LotCard({ lot, canWrite, onAdjust, onChanged }: {
  lot: ScannedLot; canWrite: boolean; onAdjust: () => void; onChanged: (what: string) => void
}) {
  const [moving, setMoving] = useState(false)
  const [where, setWhere] = useState(lot.location)
  const [err, setErr] = useState('')
  const problem = lotProblem(lot)
  const patch = async (body: Partial<Lot>, said: string) => {
    setErr('')
    try { await api.patch(`/api/lots/${lot.id}`, body); setMoving(false); onChanged(said) } catch (e: any) { setErr(e.message) }
  }
  return (
    <div className="lot-card">
      <div className="mat-thumb">{lot.material.smiles ? <Mol smiles={lot.material.smiles} w={256} h={192} alt={`Structure of ${lot.material.name}`} /> : <span className="faint">No structure</span>}</div>
      <div style={{ minWidth: 0 }}>
        <h3><Link to={`/materials/${lot.material.id}`}>{lot.material.name}</Link></h3>
        <div className="faint" style={{ marginBottom: 8 }}>{lot.material.mid}{lot.material.formula ? ` · ${lot.material.formula}` : ''}</div>
        <dl className="kv">
          <dt>Barcode</dt><dd><b style={{ fontWeight: 600 }}>{lot.barcode}</b></dd>
          <dt>On hand</dt><dd>{fmt(lot.amount, 3)} {unitLabel(lot.unit)}</dd>
          <dt>Location</dt><dd>{lot.location || <span className="faint">not given</span>}</dd>
          <dt>Status</dt><dd style={{ textTransform: 'capitalize' }}>{lot.status}</dd>
          {lot.supplier && <><dt>Supplier</dt><dd>{lot.supplier}{lot.catalog_number ? ` · ${lot.catalog_number}` : ''}</dd></>}
          {lot.expires_at && <><dt>Expires</dt><dd>{expiryDay(lot.expires_at)}</dd></>}
          {(lot.safety?.pictograms?.length || lot.safety?.signal) ? <><dt>Hazards</dt><dd className="row" style={{ gap: 6 }}>
            <SafetyBadge s={lot.safety} /><SignalWord signal={lot.safety?.signal || ''} /></dd></> : null}
        </dl>
      </div>
      {problem && <p className="risk-warn danger lot-problem bottle-bad">{problem}</p>}
      {moving && <form className="lot-edit" onSubmit={e => { e.preventDefault(); patch({ location: where.trim() }, `Moved to ${where.trim() || 'no location'}.`) }}>
        <label className="field">New location<input className="input" autoFocus value={where} onChange={e => setWhere(e.target.value)} /></label>
        <button className="btn primary" type="submit">Save location</button>
        <button className="btn" type="button" onClick={() => { setMoving(false); setWhere(lot.location) }}>Cancel</button>
      </form>}
      {err && <p className="error" style={{ gridColumn: '1 / -1', margin: 0 }}>{err}</p>}
      {canWrite && !moving && <div className="lot-acts">
        <button className="btn" onClick={onAdjust}>Record use or correction</button>
        <button className="btn" onClick={() => setMoving(true)}>Change location</button>
        <label className="row" style={{ gap: 6 }}><span className="faint">Status</span>
          <select className="select" value={lot.status} onChange={e => patch({ status: e.target.value }, `Marked ${e.target.value}.`)}>
            {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}</select></label>
      </div>}
    </div>
  )
}

/** What a scan found: one bottle's card, a choice between bottles that share the barcode, or nothing (with a way to
 *  add it as a new lot). Built for a stock check: after each change, Scan next. */
function ScanResult({ answer, canWrite, onClose, onScanNext, onAddLot, onReload }: {
  answer: ScanAnswer; canWrite: boolean; onClose: () => void; onScanNext: () => void; onAddLot: () => void; onReload: () => Promise<void>
}) {
  const [picked, setPicked] = useState<number | null>(answer.lots.length === 1 ? answer.lots[0].id : null)
  const [adjust, setAdjust] = useState<Lot | null>(null)
  const [done, setDone] = useState('')
  const next = useRef<HTMLButtonElement>(null)
  const lot = answer.lots.find(l => l.id === picked)
  const changed = async (what: string) => { await onReload(); setDone(what); next.current?.focus() }
  return (
    <Modal title={lot ? 'Scanned bottle' : answer.lots.length ? 'Which bottle?' : 'Nothing found'} onClose={onClose} footer={<>
      {lot && answer.lots.length > 1 && <button className="btn ghost" onClick={() => { setPicked(null); setDone('') }}>Other bottles</button>}
      <span className="grow" />
      <button className="btn" onClick={onClose}>Close</button>
      <button ref={next} className="btn primary" autoFocus onClick={onScanNext}>Scan next</button></>}>
      <div role="status" className="stack" style={{ gap: 12 }}>
        {done && <p className="sp-note"><span>Saved. {done}</span></p>}
        {lot ? <LotCard key={`${lot.id}-${lot.status}-${lot.location}`} lot={lot} canWrite={canWrite} onAdjust={() => setAdjust(lot)} onChanged={changed} />
          : answer.lots.length ? <>
            <p style={{ margin: 0 }}><b>{answer.lots.length} bottles</b> have the barcode <b>{answer.code}</b> (a supplier's code can be the same on every bottle of a batch). Which one is it?</p>
            <ul className="list">{answer.lots.map(l => <li key={l.id}>
              <button className="btn ghost lot-pick" onClick={() => setPicked(l.id)}>
                <b>{l.material.name}</b><span className="num">{fmt(l.amount, 3)} {unitLabel(l.unit)}</span>
                <span className="faint">{[l.location || 'no location', l.status, l.supplier, `received ${day(l.received_at)}`].filter(Boolean).join(' · ')}</span>
              </button></li>)}</ul>
          </> : <>
            <p style={{ margin: 0 }}>No lot has the barcode <b>{answer.code}</b>.</p>
            {canWrite && <div><button className="btn" onClick={onAddLot}>Add as a new lot…</button></div>}
          </>}
      </div>
      {adjust && <AdjustDialog lot={adjust} onClose={() => setAdjust(null)} onDone={() => { setAdjust(null); changed('Amount recorded.') }} />}
    </Modal>
  )
}

export default function Inventory() {
  const { canWrite, toast } = useApp()
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('available')
  const [lots, setLots] = useState<Lot[]>([])
  const [adjust, setAdjust] = useState<Lot | null>(null)
  const [history, setHistory] = useState<Lot | null>(null)
  const [scanning, setScanning] = useState(false)
  const [scan, setScan] = useState<ScanAnswer | null>(null)
  const [adding, setAdding] = useState<{ code: string; material: Material | null } | null>(null)
  const dq = useDebounced(q, 200)
  const load = () => api.get<Lot[]>(`/api/lots?q=${encodeURIComponent(dq)}${status ? `&status=${status}` : ''}`).then(setLots)
  useEffect(() => { load() }, [dq, status]) // eslint-disable-line react-hooks/exhaustive-deps

  const setLotStatus = async (l: Lot, s: string) => { await api.patch(`/api/lots/${l.id}`, { status: s }); load() }

  const lookUp = async (code: string) => {
    setScanning(false)
    try {
      const a = await api.get<ScanAnswer>(`/api/scan?code=${encodeURIComponent(code)}`)
      if (!a.lots.length && a.record) {  // a label Benchtop knows: open that record
        const r = a.record
        toast(r.kind === 'sample' ? `${r.code} is a sample of ${r.experiment}` : `${r.code}: ${r.title}`)
        navigate(r.kind === 'material' ? `/materials/${r.id}` : `/experiments/${r.id}`)
        return
      }
      setScan(a)
    } catch (e: any) { toast(e.message) }
  }
  const reloadScan = async () => {
    if (!scan) return
    const a = await api.get<ScanAnswer>(`/api/scan?code=${encodeURIComponent(scan.code)}`)
    setScan(a)
    load()
  }
  const scanNext = () => { setScan(null); setScanning(true) }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div><h1 className="page-title">Inventory</h1><p className="lede">Every container on hand. Amounts go down automatically when you deduct a reaction's reagents.</p></div>
      <div className="row">
        <input className="input grow" placeholder="Barcode, material name or CAS" aria-label="Search the inventory" value={q} onChange={e => setQ(e.target.value)} />
        <select className="select" value={status} onChange={e => setStatus(e.target.value)} aria-label="Status">
          <option value="">Any status</option><option value="available">Available</option><option value="empty">Empty</option>
          <option value="quarantined">Quarantined</option><option value="disposed">Disposed</option>
        </select>
        <button className="btn primary" onClick={() => setScanning(true)}>Scan</button>
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
      {scanning && <Scanner title="Scan a bottle" intro="A container label (such as VIM's), a supplier's barcode, or a Benchtop code like EXP-00025 or a sample code."
        onClose={() => setScanning(false)} onCode={lookUp} />}
      {scan && <ScanResult key={scan.code} answer={scan} canWrite={canWrite} onClose={() => setScan(null)} onScanNext={scanNext} onReload={reloadScan}
        onAddLot={() => { setAdding({ code: scan.code, material: null }); setScan(null) }} />}
      {adding && !adding.material && <MaterialPicker title={`Which material is ${adding.code}?`} withRole={false} onClose={() => setAdding(null)}
        onPick={m => setAdding({ ...adding, material: m })} />}
      {adding?.material && <LotForm material={adding.material} barcode={adding.code} onClose={() => setAdding(null)}
        onSaved={lot => { setAdding(null); load(); toast(`Added ${lot.barcode} to ${adding.material!.name}`, { label: 'Scan next', run: () => setScanning(true) }) }} />}
    </div>
  )
}
