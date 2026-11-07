import { useEffect, useState } from 'react'
import { useApp } from '../App'
import { api, when } from '../api'

/** Whether this Benchtop makes archival PDFs (0.34): the PDF/A package installs only where a ready-made one exists. */
export function PdfaStatusPanel() {
  const [on, setOn] = useState<boolean | null>(null)
  useEffect(() => { api.get<{ available: boolean }>('/api/archive/pdfa').then(x => setOn(x.available)).catch(() => setOn(null)) }, [])
  if (on === null) return null
  return (
    <section className="stack" style={{ gap: 6 }}>
      <h2 style={{ fontSize: 17 }}>Archival PDFs (PDF/A)</h2>
      <p className="faint pdfa-state" style={{ margin: 0 }}>{on
        ? 'On: every PDF (Download PDF, Zenodo, Archive PDF) is PDF/A-3b, with the record\'s data and timestamp proofs inside it.'
        : 'Off on this computer: the PDF/A package isn\'t installed (there is no ready-made one for this computer). PDFs are ordinary PDFs with the same pages, and nothing is attached to them.'}</p>
    </section>
  )
}

interface EmailSettings {
  host: string; port: number; security: 'starttls' | 'ssl' | 'none'; username: string; password: string; password_set: boolean
  from_address: string; from_name: string; base_url: string; fixed: string[]; configured: boolean; link_base: string; waiting?: number
  log: { to: string; subject: string; status: string; error: string; at: string; next_try?: string | null }[]
}

/** Admin → Email: the server Benchtop sends witness requests, reminders and outcomes through. */
export function EmailSettingsPanel() {
  const { toast, user } = useApp()
  const [s, setS] = useState<EmailSettings | null>(null)
  const [f, setF] = useState<Partial<EmailSettings>>({})
  const [testTo, setTestTo] = useState(user.email || '')
  const [busy, setBusy] = useState('')
  const [err, setErr] = useState('')
  const load = () => api.get<EmailSettings>('/api/admin/email').then(x => { setS(x); setF({ ...x, password: '' }) }).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  if (!s) return null
  const fixed = (k: string) => s.fixed.includes(k)
  const set = (k: keyof EmailSettings) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setF({ ...f, [k]: k === 'port' ? Number(e.target.value) : e.target.value })
  const save = async () => {
    setBusy('save'); setErr('')
    try {
      const body: Record<string, unknown> = { host: f.host, port: f.port, security: f.security, username: f.username, from_address: f.from_address, from_name: f.from_name, base_url: f.base_url }
      if (f.password) body.password = f.password
      await api.put('/api/admin/email', body); toast('Email settings saved'); load()
    } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  const test = async () => {
    setBusy('test'); setErr('')
    try { await api.post('/api/admin/email/test', { to: testTo }); toast(`Test email sent to ${testTo}`) } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  const preset = (host: string, port: number, security: EmailSettings['security']) => setF({ ...f, host, port, security })
  return (
    <section className="stack" style={{ gap: 10 }}>
      <div className="spread" style={{ alignItems: 'center' }}><h2 style={{ fontSize: 17 }}>Email</h2>
        <span className={`pill ${s.configured && !s.waiting ? 'ok' : s.waiting ? 'warn' : ''}`}>
          {!s.configured ? 'Not set up' : s.waiting ? `${s.waiting} waiting to retry` : 'Sending'}</span></div>
      <p className="faint" style={{ margin: 0 }}>Benchtop emails people when they're asked to witness a record, reminds them while it waits, and tells authors when
        their record is witnessed or returned. Without an email server, all of this still appears as notifications in Benchtop.</p>
      <div className="panel admin-card">
        <div className="card-row"><span className="card-label">Fill in for</span>
          <button className="btn sm" onClick={() => preset('smtp.gmail.com', 587, 'starttls')}>Gmail</button>
          <button className="btn sm" onClick={() => preset('smtp.office365.com', 587, 'starttls')}>Outlook / Microsoft 365</button>
          <button className="btn sm" onClick={() => preset('smtp.mail.me.com', 587, 'starttls')}>iCloud</button></div>
        <div className="form-grid">
          <label className="field">Email server<input className="input" value={f.host || ''} onChange={set('host')} disabled={fixed('host')} placeholder="smtp.example.org" /></label>
          <label className="field">Port<input className="input" type="number" value={f.port || 587} onChange={set('port')} disabled={fixed('port')} /></label>
          <label className="field">Security<select className="select" value={f.security} onChange={set('security')} disabled={fixed('security')}>
            <option value="starttls">STARTTLS (port 587)</option><option value="ssl">SSL/TLS (port 465)</option><option value="none">None (local relay only)</option></select></label>
          <label className="field">Username<input className="input" value={f.username || ''} onChange={set('username')} disabled={fixed('username')} autoComplete="off" /></label>
          <label className="field">Password<input className="input" type="password" value={f.password || ''} onChange={set('password')} disabled={fixed('password')} autoComplete="new-password"
            placeholder={s.password_set ? 'Saved (leave blank to keep)' : ''} /></label>
          <label className="field">From address<input className="input" value={f.from_address || ''} onChange={set('from_address')} disabled={fixed('from_address')} placeholder="you@example.org" /></label>
          <label className="field">From name<input className="input" value={f.from_name || ''} onChange={set('from_name')} placeholder="Benchtop" /></label>
          <label className="field">Address in links<input className="input" value={f.base_url || ''} onChange={set('base_url')} disabled={fixed('base_url')} placeholder={s.link_base} /></label>
        </div>
        <p className="faint" style={{ margin: 0, fontSize: 12.5 }}>For Gmail, Outlook and iCloud, use an <i>app password</i> from your account's security settings (your normal password won't work).
          “Address in links” is what people open Benchtop at; links in emails currently go to <code>{s.link_base}</code>. The password is stored encrypted.</p>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
        <div className="card-foot">
          <button className="btn primary" disabled={!!busy} onClick={save}>{busy === 'save' ? 'Saving…' : 'Save'}</button>
          <div className="foot-group">
            <input className="input" type="email" value={testTo} onChange={e => setTestTo(e.target.value)} placeholder="Send a test to…" aria-label="Test email address" />
            <button className="btn" disabled={!!busy || !s.configured || !testTo.includes('@')} onClick={test}
              title={s.configured ? '' : 'Save the email settings first'}>{busy === 'test' ? 'Sending…' : 'Send test email'}</button>
          </div>
        </div>
      </div>
      {s.log.length > 0 && <details><summary className="faint">Recent emails ({s.log.length})</summary>
        <table className="plain mail-log" style={{ marginTop: 6 }}><tbody>{s.log.map((e, i) => <tr key={i}>
          <td className={`st ${e.status}`}>{e.status}</td><td>{e.to}</td><td>{e.subject}{e.error && <div className={e.status === 'failed' ? 'error' : 'faint'} style={{ fontSize: 12 }}>{e.error}{e.next_try ? ` · next try ${when(e.next_try)}` : ''}</div>}</td><td className="faint when">{when(e.at)}</td>
        </tr>)}</tbody></table></details>}
    </section>
  )
}

/** Admin → Witnessing: how long before reminders and "overdue". */
export function WitnessSettingsPanel() {
  const { toast } = useApp()
  const [s, setS] = useState<{ remind_after_days: number; overdue_after_days: number } | null>(null)
  useEffect(() => { api.get<typeof s>('/api/admin/witness-settings').then(setS).catch(() => {}) }, [])
  if (!s) return null
  const save = async (changes: Partial<NonNullable<typeof s>>) => {
    try { setS(await api.put('/api/admin/witness-settings', changes)); toast('Saved') } catch (e: any) { toast(e.message) }
  }
  return (
    <section className="stack" style={{ gap: 10 }}>
      <h2 style={{ fontSize: 17 }}>Witnessing</h2>
      <p className="faint" style={{ margin: 0 }}>When an author asks someone to witness, that person gets a reminder until it's done,
        and the request is marked overdue for both of them after a while. Changes save when you leave the box.</p>
      <div className="panel admin-card">
        <div className="card-row" style={{ columnGap: 28, rowGap: 10 }}>
          <label className="inline-field">Remind every
            <input className="input inline-num" type="number" min={1} max={90} defaultValue={s.remind_after_days} onBlur={e => save({ remind_after_days: Number(e.target.value) })} /> days</label>
          <label className="inline-field">Overdue after
            <input className="input inline-num" type="number" min={1} max={90} defaultValue={s.overdue_after_days} onBlur={e => save({ overdue_after_days: Number(e.target.value) })} /> days</label>
        </div>
      </div>
    </section>
  )
}

interface TsStatus {
  enabled: boolean; tsa_url: string; fixed: string[]; available: boolean; services: { name: string; url: string }[]
  counts: { ok: number; pending: number; failed: number }; last_error: string
}

/** Admin → Trusted timestamps (RFC 3161). */
export function TimestampSettingsPanel() {
  const { toast } = useApp()
  const [s, setS] = useState<TsStatus | null>(null)
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState('')
  const [result, setResult] = useState<{ ok: boolean; error?: string; tsa?: string; gen_time?: string } | null>(null)
  const load = () => api.get<TsStatus>('/api/admin/timestamps').then(x => { setS(x); setUrl(x.tsa_url) })
  useEffect(() => { load() }, [])
  if (!s) return null
  const save = async (changes: Partial<TsStatus>) => {
    try { const x = await api.put<TsStatus>('/api/admin/timestamps', changes); setS(x); setUrl(x.tsa_url); toast('Saved') } catch (e: any) { toast(e.message) }
  }
  const test = async () => {
    setBusy('test'); setResult(null)
    try { setResult(await api.post('/api/admin/timestamps/test', { tsa_url: url })) } catch (e: any) { setResult({ ok: false, error: e.message }) } finally { setBusy('') }
  }
  const retry = async () => { setBusy('retry'); try { const x = await api.post<TsStatus & { retried: number }>('/api/admin/timestamps/retry'); setS(x); toast(`Retrying ${x.retried}`) } finally { setBusy('') } }
  const fixed = (k: string) => s.fixed.includes(k)
  return (
    <section className="stack" style={{ gap: 10 }}>
      <div className="spread" style={{ alignItems: 'center' }}><h2 style={{ fontSize: 17 }}>Trusted timestamps</h2>
        <label className="switch-row"><input type="checkbox" checked={s.enabled} disabled={fixed('enabled') || !s.available} onChange={e => save({ enabled: e.target.checked })} /> On</label></div>
      <p className="faint" style={{ margin: 0 }}>Every signature is also certified by an independent timestamp service (RFC 3161, as used for patents and in
        industry ELNs): proof, checkable without Benchtop, that it existed at that time. Only a fingerprint of the signature is sent, never the record.
        Offline, signing still works and the timestamp is fetched later.</p>
      {!s.available && <p className="notice" style={{ margin: 0 }}>Stop Benchtop and start it again so it can install what trusted timestamps need.</p>}
      <div className="panel admin-card">
        <div className="card-row"><span className="card-label">Service</span>{s.services.map(t =>
          <button key={t.url} className={`btn sm${url === t.url ? ' primary' : ''}`} disabled={fixed('tsa_url')} onClick={() => setUrl(t.url)}>{t.name}</button>)}</div>
        <div className="card-row url-row">
          <input className="input grow" value={url} onChange={e => setUrl(e.target.value)} disabled={fixed('tsa_url')} aria-label="Timestamp service address" />
          <button className="btn" disabled={!!busy} onClick={test}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
          <button className="btn primary" disabled={url === s.tsa_url || fixed('tsa_url')} onClick={() => save({ tsa_url: url })}>Use this service</button>
        </div>
        {result && <p className={result.ok ? '' : 'error'} style={{ margin: 0 }}>{result.ok ? `Works: ${result.tsa} certified ${result.gen_time ? when(result.gen_time) : 'just now'}.` : result.error}</p>}
        <div className="card-foot"><span>{s.counts.ok} signature{s.counts.ok === 1 ? '' : 's'} timestamped
          {s.counts.pending ? ` · ${s.counts.pending} waiting` : ''}{s.counts.failed ? ` · ${s.counts.failed} not obtained` : ''}
          {(s.counts.pending > 0 || s.counts.failed > 0) && <> · <button className="linkish" disabled={!!busy} onClick={retry}>Retry now</button></>}</span>
          {s.last_error && (s.counts.pending > 0 || s.counts.failed > 0) && <span className="faint" style={{ flexBasis: '100%', fontSize: 12.5 }}>Last problem: {s.last_error}</span>}
        </div>
      </div>
    </section>
  )
}
