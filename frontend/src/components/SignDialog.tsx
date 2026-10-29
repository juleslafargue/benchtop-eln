import { useEffect, useState } from 'react'
import { api, displayName, type Experiment, type WitnessCandidate } from '../api'
import Modal from './Modal'
import { reminderText } from '../signCheck'

export type SignAction = 'sign' | 'witness' | 'reject' | 'withdraw' | 'void'

const COPY: Record<SignAction, { title: string; button: string; meaning: string; needsReason?: boolean; danger?: boolean }> = {
  sign: { title: 'Sign as author', button: 'Sign and lock', meaning: 'I am the author of this record and its content is accurate and complete. Signing locks it for witnessing.' },
  witness: { title: 'Witness this record', button: 'Witness', meaning: 'I have read and understood this record. Witnessing makes it final.' },
  reject: { title: 'Return to author', button: 'Return to author', meaning: 'The record goes back to the author as open so it can be corrected. Your reason is kept in the record.', needsReason: true, danger: true },
  withdraw: { title: 'Withdraw signature', button: 'Withdraw', meaning: 'Unlocks the record so you can edit it again. Your earlier signature stays in the history.' },
  void: { title: 'Void experiment', button: 'Void', meaning: 'Marks the record as withdrawn. Nothing is deleted; the record and your reason remain visible.', needsReason: true, danger: true },
}

export default function SignDialog({ exp, action, onDone, onClose }: {
  exp: Experiment; action: SignAction; onDone: (e: Experiment) => void; onClose: () => void
}) {
  const c = COPY[action]
  const [password, setPassword] = useState('')
  const [comment, setComment] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // signing as author: who to ask to witness (remembered for next time)
  const [people, setPeople] = useState<WitnessCandidate[] | null>(null)
  const [emailOn, setEmailOn] = useState(false)
  const [witnessId, setWitnessId] = useState<number | ''>('')
  useEffect(() => {
    if (action !== 'sign') return
    api.get<{ candidates: WitnessCandidate[]; email: boolean }>(`/api/experiments/${exp.id}/witness-candidates`).then(r => {
      setPeople(r.candidates); setEmailOn(r.email)
      let last: number | null = null
      try { last = Number(localStorage.getItem('benchtop.last-witness')) || null } catch { /* ok */ }
      if (last && r.candidates.some(c => c.id === last)) setWitnessId(last)
    }).catch(() => setPeople([]))
  }, [action, exp.id])
  const chosen = people?.find(p => p.id === witnessId)
  // a reminder only: it doesn't stop signing, and nothing of it is sent (the server alone decides what a signature covers).
  // Read afresh: the page's copy doesn't follow what was typed into the sections since it opened.
  const [empty, setEmpty] = useState('')
  useEffect(() => {
    if (action !== 'sign') return
    let live = true
    setEmpty(reminderText(exp.entries))
    api.get<Experiment>(`/api/experiments/${exp.id}`).then(x => { if (live) setEmpty(reminderText(x.entries)) }).catch(() => {})
    return () => { live = false }
  }, [action, exp.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const go = async () => {
    setBusy(true); setErr('')
    try {
      const body: Record<string, unknown> = { password, comment }
      if (action === 'sign' && witnessId) { body.witness_id = witnessId; try { localStorage.setItem('benchtop.last-witness', String(witnessId)) } catch { /* ok */ } }
      onDone(await api.post<Experiment>(`/api/experiments/${exp.id}/${action}`, body))
    }
    catch (e: any) { setErr(e.message); setBusy(false) }
  }

  return (
    <Modal title={c.title} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className={`btn ${c.danger ? 'danger' : 'primary'}`} disabled={busy || !password || (c.needsReason && !comment.trim())} onClick={go}>{c.button}</button>
    </>}>
      <form className="stack" onSubmit={e => { e.preventDefault(); go() }}>
        <p style={{ margin: 0 }}><b>{exp.eid}</b> {exp.title}</p>
        <p className="notice" style={{ margin: 0 }}>{c.meaning}</p>
        {action === 'sign' && empty && <p className="sign-reminder" role="note"><b>Still empty:</b> {empty}. <span className="faint">You can sign anyway; once signed,
          later results go in a follow-up.</span></p>}
        {action === 'sign' && people && <label className="field">Ask someone to witness it
          <select className="select" value={witnessId} onChange={e => setWitnessId(e.target.value ? Number(e.target.value) : '')}>
            <option value="">No one in particular (anyone with access can witness)</option>
            {people.map(p => <option key={p.id} value={p.id}>{displayName(p)}{emailOn && !p.has_email ? ' (no email address)' : ''}</option>)}
          </select>
          <span className="faint" style={{ fontSize: 12.5 }}>{chosen ? `${displayName(chosen)} gets a notification${emailOn && chosen.has_email ? ' and an email' : ''}, and reminders while it waits.`
            : people.length ? 'They get a notification and reminders, and you hear back when it’s witnessed or returned.' : 'Nobody else can open this record yet: share it, or put it in a shared notebook, so someone can witness it.'}</span>
        </label>}
        <label className="field">{c.needsReason ? 'Reason (required)' : 'Comment (optional)'}
          <textarea className="input" rows={2} value={comment} onChange={e => setComment(e.target.value)} /></label>
        <label className="field">Re-enter your password to apply your electronic signature
          <input className="input" type="password" autoComplete="current-password" autoFocus value={password} onChange={e => setPassword(e.target.value)} /></label>
        {err && <p className="error" role="alert">{err}</p>}
        <button hidden />
      </form>
    </Modal>
  )
}
