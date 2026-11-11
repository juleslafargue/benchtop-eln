import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApp } from '../App'
import { api } from '../api'
import Modal from './Modal'

/** Make a follow-up of a signed record: a new draft, linked to it, for results or work that came after signing.
 *  From the Inbox it also takes the waiting file; from a record's page it starts empty. */
export default function FollowUpDialog({ eid, title, notebookId, url, onClose, onDone }: {
  eid: string; title: string; notebookId: number | null
  url: string  // /api/inbox/{id}/follow-up or /api/experiments/{id}/follow-up
  onClose: () => void; onDone?: () => void
}) {
  const { notebooks, toast } = useApp()
  const nav = useNavigate()
  const open = notebooks.filter(n => !n.archived)
  const sameOk = open.some(n => n.id === notebookId)
  const [nb, setNb] = useState<number | ''>(sameOk ? notebookId! : open[0]?.id ?? '')
  const [name, setName] = useState(`Follow-up to ${eid}: ${title}`.slice(0, 300))
  const [copy, setCopy] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const go = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.post<{ id: number; eid: string }>(url, { notebook_id: nb === '' ? null : nb, copy_shares: copy, title: name })
      toast(`${r.eid} created, linked to ${eid}`)
      onDone?.()
      nav(`/experiments/${r.id}`)
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <Modal title={`Follow-up to ${eid}`} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={busy || nb === '' || !name.trim()} onClick={go}>{busy ? 'Creating…' : 'Create follow-up'}</button>
    </>}>
      <div className="stack" style={{ gap: 12 }}>
        <p className="faint" style={{ margin: 0 }}>{eid} is signed, so nothing more can be added to it. A follow-up is a new draft, linked
          to {eid} both ways, for what came afterwards: late results (HRMS, a spectrum) or more work with the same material, such as an
          assay. {eid} itself doesn't change.</p>
        <label className="field">Title<input className="input" value={name} maxLength={300} onChange={e => setName(e.target.value)} /></label>
        <label className="field">Notebook
          <select className="select" value={nb} onChange={e => setNb(e.target.value === '' ? '' : Number(e.target.value))}>
            {open.length === 0 && <option value="">You have no notebooks yet</option>}
            {open.map(n => <option key={n.id} value={n.id}>{n.name}{n.id === notebookId ? ` (same as ${eid})` : ''}</option>)}
          </select></label>
        {!sameOk && notebookId != null && <p className="faint" style={{ margin: 0, fontSize: 13 }}>{eid}'s notebook isn't one you can add to, so choose one of yours.</p>}
        <label className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
          <input type="checkbox" checked={copy} onChange={e => setCopy(e.target.checked)} style={{ marginTop: 3 }} />
          <span>Share it with the same people as {eid}<br /><span className="faint" style={{ fontSize: 13 }}>Its author and everyone it's shared with, so they see the new results too.</span></span>
        </label>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      </div>
    </Modal>
  )
}
