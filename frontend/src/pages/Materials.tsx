import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useApp } from '../App'
import { api, fmt, type Material } from '../api'
import { Mol, NumInput, StructureEditor, useDebounced } from '../components/bits'
import Modal from '../components/Modal'
import { LookupBox, LookupCard, SafetyBadge, type LookupResult } from '../components/Safety'

export function MaterialForm({ initial, onSaved, onClose }: { initial?: Material; onSaved: (m: Material) => void; onClose: () => void }) {
  const [f, setF] = useState({
    name: initial?.name || '', kind: initial?.kind || 'reagent', smiles: initial?.smiles || '', cas: initial?.cas || '',
    density: initial?.density ?? null as number | null, synonyms: initial?.synonyms || '', hazards: initial?.hazards || '', notes: initial?.notes || '',
    pubchem_cid: initial?.pubchem_cid ?? null as number | null,
  })
  const [found, setFound] = useState<LookupResult | null>(null)
  const useFound = (r: LookupResult) => {
    setFound(r)
    setF(prev => ({ ...prev, name: prev.name || r.name, cas: r.cas || prev.cas, smiles: r.smiles || prev.smiles,
      density: prev.density ?? r.density, pubchem_cid: r.cid }))
  }
  const [drawing, setDrawing] = useState(false)
  const [err, setErr] = useState('')
  const [props, setProps] = useState<{ formula: string; mw: number } | null>(null)
  const ds = useDebounced(f.smiles, 400)
  useEffect(() => {
    if (!ds) { setProps(null); return }
    api.post<{ formula: string; mw: number }>('/api/chem/properties', { structure: ds }).then(setProps).catch(() => setProps(null))
  }, [ds])

  const save = async () => {
    try {
      const m = initial ? await api.patch<Material>(`/api/materials/${initial.id}`, f) : await api.post<Material>('/api/materials', f)
      onSaved(m)
    } catch (e: any) { setErr(e.message) }
  }
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value })

  return (
    <Modal title={initial ? `Edit ${initial.mid}` : 'Register material'} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!f.name.trim()} onClick={save}>{initial ? 'Save changes' : 'Register'}</button>
    </>}>
      <div className="stack">
        {!initial && <div className="stack" style={{ gap: 8 }}>
          <LookupBox onResult={useFound} autoFocus label="Fill from PubChem" />
          {found && <LookupCard r={found}><span className="faint">Filled in below: check the name, and the density if it's a solution or a solid.</span></LookupCard>}
        </div>}
        <div className="form-grid">
          <label className="field">Name<input className="input" value={f.name} onChange={set('name')} /></label>
          <label className="field">Type<select className="select" value={f.kind} onChange={set('kind')}>
            {['reagent', 'solvent', 'sample', 'product', 'other'].map(k => <option key={k}>{k}</option>)}</select></label>
          <label className="field">CAS number<input className="input" value={f.cas} onChange={set('cas')} placeholder="e.g. 64-17-5" /></label>
          <label className="field">Density (g/mL)<NumInput className="input" label="Density" value={f.density} onCommit={v => setF({ ...f, density: v })} /></label>
        </div>
        <label className="field">Structure (SMILES)
          <div className="row" style={{ flexWrap: 'nowrap' }}>
            <input className="input grow" value={f.smiles} onChange={set('smiles')} />
            <button className="btn" type="button" onClick={() => setDrawing(true)}>Draw</button>
          </div>
        </label>
        {f.smiles && <div className="row"><div className="mat-thumb" style={{ width: 160, height: 100 }}><Mol smiles={f.smiles} w={320} h={200} /></div>
          <span className="muted">{props ? `${props.formula} · ${fmt(props.mw, 3)} g/mol` : 'Unrecognised structure'}</span></div>}
        <label className="field">Synonyms<input className="input" value={f.synonyms} onChange={set('synonyms')} /></label>
        <label className="field">Hazard notes<input className="input" value={f.hazards} onChange={set('hazards')} placeholder="Anything extra from the supplier's SDS, e.g. stabilised with BHT" /></label>
        <label className="field">Notes<textarea className="input" rows={2} value={f.notes} onChange={set('notes')} /></label>
        {err && <p className="error">{err}</p>}
      </div>
      {drawing && <StructureEditor mode="molecule" initial={f.smiles} onClose={() => setDrawing(false)} onDone={r => { setF({ ...f, smiles: r.smiles }); setDrawing(false) }} />}
    </Modal>
  )
}

export default function Materials() {
  const { canWrite } = useApp()
  const nav = useNavigate()
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('')
  const [items, setItems] = useState<Material[]>([])
  const [params, setParams] = useSearchParams()
  const [creating, setCreating] = useState(params.get('new') === '1')
  const dq = useDebounced(q, 200)
  useEffect(() => { api.get<Material[]>(`/api/materials?q=${encodeURIComponent(dq)}${kind ? `&kind=${kind}` : ''}`).then(setItems) }, [dq, kind])

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="spread">
        <div><h1 className="page-title">Materials</h1><p className="lede">Registered reagents, solvents and products. Each structure is registered once; containers are tracked as lots.</p></div>
        {canWrite && <button className="btn primary" onClick={() => setCreating(true)}>Register material</button>}
      </div>
      <div className="row">
        <input className="input grow" placeholder="Search by name, CAS or formula" value={q} onChange={e => setQ(e.target.value)} />
        <select className="select" value={kind} onChange={e => setKind(e.target.value)} aria-label="Type">
          <option value="">All types</option>{['reagent', 'solvent', 'sample', 'product', 'other'].map(k => <option key={k}>{k}</option>)}
        </select>
      </div>
      <div className="panel">
        <ul className="list">
          {items.map(m => (
            <li key={m.id}>
              <Link to={`/materials/${m.id}`} className="list-row mat-row">
                <span className="mat-thumb">{m.smiles && <Mol smiles={m.smiles} w={192} h={116} />}</span>
                <span><b style={{ fontWeight: 600 }}>{m.name}</b><span className="faint" style={{ display: 'block' }}>{m.mid} · {m.kind}</span></span>
                <span className="hide-sm muted">{m.formula}</span>
                <span className="hide-sm muted">{m.cas}</span>
                <span className="hide-sm"><SafetyBadge s={m.safety} /></span>
                <span className="muted">{fmt(m.mw, 2)}</span>
                <span className="faint">{m.available_lots} lot{m.available_lots === 1 ? '' : 's'}</span>
              </Link>
            </li>
          ))}
          {!items.length && <li className="empty">{q.trim() || kind ? 'No materials match.'
            : <>No materials registered yet.{canWrite && ' Click Register material to add your first reagent or solvent.'}</>}</li>}
        </ul>
      </div>
      {creating && <MaterialForm onClose={() => { setCreating(false); setParams({}) }} onSaved={m => nav(`/materials/${m.id}`)} />}
    </div>
  )
}
