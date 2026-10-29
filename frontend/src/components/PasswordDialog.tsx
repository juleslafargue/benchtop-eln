import { useState } from 'react'
import { useApp } from '../App'
import { api } from '../api'
import Modal from './Modal'
import PasswordField from './PasswordField'

export default function PasswordDialog({ onClose }: { onClose: () => void }) {
  const { toast, user, setUser, logout } = useApp()
  const first = user.has_password === false
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  const [err, setErr] = useState('')
  const [valid, setValid] = useState(false)
  const forced = !!user.must_change_password
  const save = async () => {
    try {
      const r = await api.post<{ other_sessions_ended: number }>('/api/auth/password', { current_password: cur, new_password: next })
      toast(first ? 'Signing password set' : r.other_sessions_ended ? `Password changed; signed out ${r.other_sessions_ended} other device${r.other_sessions_ended === 1 ? '' : 's'}` : 'Password changed')
      setUser({ ...user, has_password: true, must_change_password: false }); onClose()
    }
    catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title={forced ? 'Choose your own password' : first ? 'Set a signing password' : 'Change password'} onClose={forced ? () => {} : onClose} footer={<>
      {!forced ? <button className="btn" onClick={onClose}>Cancel</button> : <button className="btn ghost" onClick={logout}>Sign out</button>}
      <button className="btn primary" disabled={(!first && !forced && !cur) || !valid} onClick={save}>{first ? 'Set password' : forced ? 'Save and continue' : 'Change password'}</button></>}>
      <div className="stack">
        {forced && <p style={{ margin: 0 }}>Your account was set up (or its password reset) by an administrator, who therefore knows the password. Choose one only you know to continue.</p>}
        {first && <p style={{ margin: 0 }}>You sign in with single sign-on, but signing a record means re-entering a password (the e-signature rules ask for it). Choose one you'll remember; it's only used for signing.</p>}
        {!first && !forced && <label className="field">Current password<input className="input" type="password" autoComplete="current-password" autoFocus value={cur} onChange={e => setCur(e.target.value)} /></label>}
        <PasswordField value={next} onChange={setNext} onValid={setValid} username={user.username} fullName={user.full_name} email={user.email} autoFocus={first || forced} repeat />
        {err && <p className="error">{err}</p>}
      </div>
    </Modal>
  )
}
