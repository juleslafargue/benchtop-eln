import { useEffect, useState } from 'react'
import { api, fmt, type Material, type StoichRow } from '../api'
import { Mol, useDebounced } from './bits'
import Modal from './Modal'
import { LookupBox, LookupCard, type LookupResult } from './Safety'

interface Usual { id: number; mid: string; name: string; formula: string; mw: number | null; smiles: string; kind: string; uses: number; role: StoichRow['role']; last_lot_id: number | null; last_used: string }

export default function MaterialPicker({ onPick, onLookup, onClose, title = 'Add from registry', withRole = true }: {
  onPick: (m: Material, role: StoichRow['role']) => void; onLookup?: (r: LookupResult, role: StoichRow['role']) => void; onClose: () => void
  title?: string; withRole?: boolean  // withRole: the "as reagent" choice, for a reaction table
}) {
  const [found, setFound] = useState<LookupResult | null>(null)
  const [regErr, setRegErr] = useState('')
  const [q, setQ] = useState('')
  const [role, setRole] = useState<StoichRow['role']>('reagent')
  const [items, setItems] = useState<Material[]>([])
  const dq = useDebounced(q, 200)
  useEffect(() => { api.get<Material[]>(`/api/materials?q=${encodeURIComponent(dq)}&limit=40`).then(setItems) }, [dq])
  const [usual, setUsual] = useState<Usual[]>([])
  useEffect(() => { api.get<Usual[]>('/api/frequent-materials').then(setUsual).catch(() => {}) }, [])

  const pick = async (m: Material) => onPick(await api.get<Material>(`/api/materials/${m.id}`), role)

  return (
    <Modal title={title} onClose={onClose} footer={<button className="btn" onClick={onClose}>Close</button>}>
      <div className="row" style={{ marginBottom: 10 }}>
        <input className="input grow" autoFocus placeholder="Name, CAS or formula" aria-label="Find a material" value={q} onChange={e => setQ(e.target.value)} />
        {withRole && <select className="select" value={role} onChange={e => setRole(e.target.value as StoichRow['role'])} aria-label="Add as">
          <option value="reactant">as reactant</option><option value="reagent">as reagent</option>
          <option value="catalyst">as catalyst</option><option value="solvent">as solvent</option>
        </select>}
      </div>
      {!q && usual.length > 0 && <div className="usual">
        <div className="fld-cap">Your usual</div>
        <div className="usual-chips">{usual.map(u => (
          <button key={u.id} className="usual-chip" title={`${u.mid} · used in ${u.uses} reaction${u.uses === 1 ? '' : 's'}, usually as ${u.role} · adds as ${u.role}${u.last_lot_id ? ', with the lot you used last' : ''}`}
            onClick={async () => onPick({ ...(await api.get<Material>(`/api/materials/${u.id}`)), preferred_lot_id: u.last_lot_id } as Material, u.role)}>
            {u.name}{withRole && <span className="faint"> · {u.role}</span>}</button>))}</div>
      </div>}
      <ul className="list" style={{ maxHeight: 360, overflowY: 'auto' }}>
        {items.map(m => (
          <li key={m.id}>
            <button className="list-row btn ghost" style={{ gridTemplateColumns: '80px 1fr auto', width: '100%', display: 'grid', textAlign: 'left', borderRadius: 0 }} onClick={() => pick(m)}>
              <span className="mat-thumb" style={{ width: 80, height: 48 }}>{m.smiles && <Mol smiles={m.smiles} w={160} h={96} />}</span>
              <span><b style={{ fontWeight: 600 }}>{m.name}</b><span className="faint" style={{ display: 'block' }}>{m.mid} · {m.cas || 'no CAS'} · {m.formula}</span></span>
              <span className="faint">{fmt(m.mw, 2)} g/mol<br />{m.available_lots} lot{m.available_lots === 1 ? '' : 's'}</span>
            </button>
          </li>
        ))}
        {!items.length && <li className="empty">Nothing in your registry matches{q ? ` “${q}”` : ''}.</li>}
      </ul>
      {onLookup && <div className="stack" style={{ gap: 8, marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--rule)' }}>
        <span className="fld-cap">Not registered? Find it on PubChem</span>
        <LookupBox key={dq} initial={q} onResult={setFound} label="Search PubChem" />
        {found && <LookupCard r={found}>
          <div className="row" style={{ gap: 6 }}>
            {found.registered
              ? <button className="btn sm primary" onClick={async () => onPick(await api.get<Material>(`/api/materials/${found.registered!.id}`), role)}>Add {found.registered.mid}</button>
              : <>
                <button className="btn sm primary" onClick={async () => {
                  setRegErr('')
                  try {
                    const m = await api.post<Material>('/api/materials', { name: found.name, smiles: found.smiles, cas: found.cas, kind: role === 'solvent' ? 'solvent' : 'reagent',
                      density: role === 'solvent' ? found.density : null, pubchem_cid: found.cid })
                    onPick(m, role)
                  } catch (e: any) { setRegErr(e.message) }
                }}>Register and add</button>
                <button className="btn sm" onClick={() => onLookup(found, role)}>Add without registering</button>
              </>}
          </div>
          {regErr && <p className="error" style={{ margin: 0 }}>{regErr}</p>}
        </LookupCard>}
      </div>}
    </Modal>
  )
}
