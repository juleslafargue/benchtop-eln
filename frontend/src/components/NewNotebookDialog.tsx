import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApp } from '../App'
import { api } from '../api'
import Modal from './Modal'
import { AudiencePicker, defaultAudience, type AudienceValue } from './Audience'

/** With `onCreated`, the new notebook is handed back (the New experiment dialog picks it) instead of being opened. */
export default function NewNotebookDialog({ onClose, onCreated }: { onClose: () => void; onCreated?: (id: number) => void }) {
  const { refreshNotebooks, toast, groups } = useApp()
  const nav = useNavigate()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [err, setErr] = useState('')
  const [aud, setAud] = useState<AudienceValue>(() => defaultAudience(groups))
  const [creating, setCreating] = useState(false)  // one notebook per click (0.30.1: a double click made two)
  const busy = useRef(false)

  const create = async () => {
    if (busy.current || !name.trim()) return
    busy.current = true; setCreating(true); setErr('')
    let nb: { id: number }
    try {
      nb = await api.post<{ id: number }>('/api/notebooks', { name, description, ...aud })
    } catch (e: any) { setErr(e.message); busy.current = false; setCreating(false); return }
    // the list it's picked from: wait for it (the New experiment dialog chooses the new notebook from it)
    await refreshNotebooks().catch(() => {})
    toast('Notebook created')
    if (onCreated) { onCreated(nb.id); return }
    onClose()
    nav(`/notebooks/${nb.id}`)
  }

  return (
    <Modal title="New notebook" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name.trim() || creating} onClick={create}>{creating ? 'Creating…' : 'Create notebook'}</button>
    </>}>
      <div className="stack">
        <label className="field">Name<input className="input" autoFocus value={name} onChange={e => setName(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); create() } }} placeholder="e.g. Amide couplings" /></label>
        <label className="field">Description<textarea className="input" rows={2} value={description} onChange={e => setDescription(e.target.value)} /></label>
        <div className="field">Who can open it<AudiencePicker value={aud} onChange={setAud} /></div>
        {err && <p className="error">{err}</p>}
      </div>
    </Modal>
  )
}
