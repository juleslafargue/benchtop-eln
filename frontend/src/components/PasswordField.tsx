import { useEffect, useId, useState } from 'react'
import { api } from '../api'

/** New-password input with live feedback from the same rules the server enforces.
 *  `onValid` tells the parent whether the current value would be accepted. With `repeat`, the password is typed a
 *  second time, and it's only valid once both match (for a password you choose for yourself: a typo would lock you out). */
export default function PasswordField({ label = 'New password', value, onChange, onValid, username = '', fullName = '', email = '', autoFocus, repeat }: {
  label?: string; value: string; onChange: (v: string) => void; onValid?: (ok: boolean) => void
  username?: string; fullName?: string; email?: string; autoFocus?: boolean; repeat?: boolean
}) {
  const [problems, setProblems] = useState<string[] | null>(null)
  const [show, setShow] = useState(false)
  const [again, setAgain] = useState('')
  const id = useId()
  const matches = !repeat || again === value
  useEffect(() => {
    if (!value) { setProblems(null); onValid?.(false); return }
    let alive = true
    const t = setTimeout(() => {
      api.post<{ problems: string[] }>('/api/auth/password-check', { password: value, username, full_name: fullName, email })
        .then(r => { if (alive) { setProblems(r.problems); onValid?.(r.problems.length === 0 && matches) } })
        .catch(() => { if (alive) { setProblems(null); onValid?.(value.length >= 10 && matches) } })
    }, 250)
    return () => { alive = false; clearTimeout(t) }
  }, [value, username, fullName, email, matches]) // eslint-disable-line react-hooks/exhaustive-deps
  const strength = !value ? 0 : problems && problems.length ? 1 : value.length >= 16 ? 3 : 2
  return (
    <div className="field pw-field">
      <label htmlFor={`${id}-pw`}>{label}</label>
      <div className="pw-row">
        <input id={`${id}-pw`} className="input" type={show ? 'text' : 'password'} autoComplete="new-password" autoFocus={autoFocus} value={value}
          onChange={e => onChange(e.target.value)} aria-describedby={`${id}-help`} aria-invalid={!!problems?.length} />
        <button type="button" className="btn ghost sm" onClick={() => setShow(s => !s)} aria-pressed={show}>{show ? 'Hide' : 'Show'}</button>
      </div>
      <div className={`pw-meter s${strength}`} aria-hidden="true"><i /><i /><i /></div>
      <div id={`${id}-help`} className="pw-help" aria-live="polite">
        {!value ? <span className="faint">At least 10 characters. A few unrelated words together is long, strong and easy to remember.</span>
          : problems === null ? <span className="faint">Checking…</span>
            : problems.length ? <span className="warn-text">{problems[0]}.</span>
              : <span className="ok-text">{strength === 3 ? 'Strong password.' : 'Good. Longer is even better.'}</span>}
      </div>
      {repeat && <>
        <label htmlFor={`${id}-again`} style={{ marginTop: 6 }}>Repeat password</label>
        <input id={`${id}-again`} className="input" type={show ? 'text' : 'password'} autoComplete="new-password" value={again}
          onChange={e => setAgain(e.target.value)} aria-describedby={`${id}-match`} aria-invalid={!!again && !matches} />
        <div id={`${id}-match`} className="pw-help" aria-live="polite">
          {again && !matches ? <span className="warn-text">The two passwords are different.</span>
            : again && value ? <span className="ok-text">Both the same.</span> : <span className="faint">Type it again, to be sure of it.</span>}
        </div>
      </>}
    </div>
  )
}
