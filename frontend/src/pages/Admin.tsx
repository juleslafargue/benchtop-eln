import { useEffect, useState } from 'react'
import { NavLink, Navigate, useLocation, useParams } from 'react-router-dom'
import { useApp } from '../App'
import type { NmrStatus } from '../components/Prediction'
import { api, displayName, when, type AuditEvent, type Role, type User } from '../api'
import Modal from '../components/Modal'
import { Plugins, Webhooks } from '../components/Integrations'
import { ZenodoSettingsPanel } from '../components/Publish'
import { EmailSettingsPanel, TimestampSettingsPanel, WitnessSettingsPanel } from '../components/AdminSigning'
import InstrumentsPanel from '../components/AdminInstruments'
import PasswordField from '../components/PasswordField'
import { auditSentence, type AuditNames } from '../auditText'

function EditUser({ u, onClose, onSaved }: { u: User; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({ username: u.username, full_name: u.full_name, email: u.email })
  const [err, setErr] = useState('')
  const save = async () => {
    const changes = Object.fromEntries(Object.entries(f).filter(([k, v]) => v !== (u as any)[k]))
    if (!Object.keys(changes).length) { onClose(); return }
    try { await api.patch(`/api/users/${u.id}`, changes); onSaved() } catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title={`Edit ${u.username}`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" onClick={save}>Save changes</button></>}>
      <div className="form-grid">
        <label className="field">Username<input className="input" value={f.username} onChange={e => setF({ ...f, username: e.target.value.toLowerCase() })} /></label>
        <label className="field">Full name<input className="input" value={f.full_name} onChange={e => setF({ ...f, full_name: e.target.value })} /></label>
        <label className="field">Email<input className="input" type="email" value={f.email} onChange={e => setF({ ...f, email: e.target.value })} /></label>
      </div>
      <p className="faint">Signed records keep the name the person used when signing.</p>
      {err && <p className="error">{err}</p>}
    </Modal>
  )
}

function UserForm({ onClose, onSaved }: { onClose: () => void; onSaved: (added: Added) => void }) {
  const { groups, refreshGroups } = useApp()
  const [f, setF] = useState({ username: '', full_name: '', email: '', password: '', role: 'scientist' as Role, must_change: true })
  const [groupId, setGroupId] = useState<number | ''>(groups.length === 1 ? groups[0].id : '')
  const [err, setErr] = useState('')
  const [valid, setValid] = useState(false)
  const [busy, setBusy] = useState(false)
  const save = async () => {
    setBusy(true); setErr('')
    let made: User
    try { made = await api.post<User>('/api/users', f) } catch (e: any) { setErr(e.message); setBusy(false); return }
    // the account exists now: a group that couldn't be set is reported, not a reason to lose the account
    const g = groups.find(x => x.id === groupId)
    let problem = ''
    if (g) {
      try { await api.put(`/api/groups/${g.id}/members/${made.id}`, { role: 'member' }); refreshGroups() }
      catch (e: any) { problem = `Couldn't add them to ${g.name} (${e.message}). Add them on the Groups page.` }
    }
    onSaved({ u: made, group: problem ? '' : g?.name || '', mustChange: f.must_change, problem })
  }
  return (
    <Modal title="Add user" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!f.username || !valid || busy} onClick={save}>{busy ? 'Adding…' : 'Add user'}</button></>}>
      <div className="form-grid">
        <label className="field">Username<input className="input" autoFocus value={f.username} onChange={e => setF({ ...f, username: e.target.value })} /></label>
        <label className="field">Full name<input className="input" value={f.full_name} onChange={e => setF({ ...f, full_name: e.target.value })} /></label>
        <label className="field">Email<input className="input" type="email" value={f.email} onChange={e => setF({ ...f, email: e.target.value })} /></label>
        <label className="field">Role<select className="select" value={f.role} onChange={e => setF({ ...f, role: e.target.value as Role })}>
          <option value="scientist">Scientist</option><option value="viewer">Viewer (read-only)</option><option value="admin">Admin</option></select></label>
        <label className="field">Group<select className="select" value={groupId} onChange={e => setGroupId(e.target.value ? Number(e.target.value) : '')}>
          <option value="">No group</option>
          {groups.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select>
          <span className="faint" style={{ fontSize: 12.5 }}>{groups.length ? "They'll see the notebooks kept for this group." : 'No groups yet: make one on the Groups page.'}</span></label>
      </div>
      <div style={{ marginTop: 12 }}><PasswordField label="First password (give it to them privately)" value={f.password} onChange={v => setF({ ...f, password: v })} onValid={setValid}
        username={f.username} fullName={f.full_name} email={f.email} /></div>
      <label className="check-row" style={{ marginTop: 8 }}><input type="checkbox" checked={f.must_change} onChange={e => setF({ ...f, must_change: e.target.checked })} />
        Ask them to choose their own password when they first sign in (recommended: you know this one)</label>
      {err && <p className="error">{err}</p>}
    </Modal>
  )
}

interface Added { u: User; group: string; mustChange: boolean; problem: string }

/** After adding someone: what to send them. The password isn't shown again: it goes to them privately. */
function UserAdded({ u, group, mustChange, problem, onClose }: Added & { onClose: () => void }) {
  const { toast } = useApp()
  const address = window.location.origin
  const message = `Your Benchtop account is ready.\nAddress: ${address}\nUsername: ${u.username}\n`
    + (mustChange ? "I'll give you the first password separately; you'll choose your own when you first sign in." : "I'll give you the password separately.")
  return (
    <Modal title={`${displayName(u)} can sign in now`} onClose={onClose} footer={<>
      <button className="btn" onClick={() => navigator.clipboard?.writeText(message).then(() => toast('Message copied')).catch(() => toast("Couldn't copy: select the text instead"))}>Copy a message for them</button>
      <button className="btn primary" onClick={onClose}>Done</button></>}>
      <p style={{ marginTop: 0 }}>Send them:</p>
      <dl className="kv">
        <dt>Address</dt><dd className="mono">{address}</dd>
        <dt>Username</dt><dd className="mono">{u.username}</dd>
        <dt>Password</dt><dd>the one you chose: tell them privately, not in the same message</dd>
      </dl>
      <p>{mustChange ? 'When they first sign in, Benchtop asks them to choose their own password before anything else.'
        : 'They keep the password you chose; they can change it in Account settings.'}
        {' '}{group ? `They're in ${group}, so they see its notebooks.` : "They're not in a group: add them on the Groups page so they see the group's notebooks."}</p>
      {problem && <p className="error">{problem}</p>}
    </Modal>
  )
}

function ResetPassword({ u, onClose, onDone }: { u: User; onClose: () => void; onDone: () => void }) {
  const [pw, setPw] = useState('')
  const [valid, setValid] = useState(false)
  const [err, setErr] = useState('')
  const save = async () => { try { await api.patch(`/api/users/${u.id}`, { password: pw }); onDone() } catch (e: any) { setErr(e.message) } }
  return (
    <Modal title={`Reset ${u.username}'s password`} onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!valid} onClick={save}>Reset password</button></>}>
      <p style={{ marginTop: 0 }}>Set a temporary password and give it to them privately. They'll be signed out everywhere now and asked to choose their own password the next time they sign in.</p>
      <PasswordField label="Temporary password" value={pw} onChange={setPw} onValid={setValid} username={u.username} fullName={u.full_name} email={u.email} autoFocus />
      {err && <p className="error">{err}</p>}
    </Modal>
  )
}

interface CheckItem { name: string; ok: boolean; detail: string }
interface BackupTest { ok: boolean; at: string; name: string; where: string; checks: CheckItem[] }
interface BackupInfo {
  directory: string; keep: number; interval_hours: number; backups: { name: string; size: number; created_at: string }[]
  copy_dir: string | null; copy_dir_from_env: boolean; copies: number; warnings: string[]; files?: { files: number; bytes: number }
  last_copy: { ok: boolean; at: string; name: string; folder: string; error?: string } | null
  last_check: BackupTest | null; suggestions: { label: string; path: string }[]
  data_check?: DataCheck
  disk?: DiskSpace
}
interface DiskSpace {
  places: { path: string; holds: string[]; label: string; free: number; total: number; warn_below: number }[]
  database: number; attached: number; backups: number; warn_gb: number; reserve: number; warnings: string[]
}
interface DataCheck {
  enabled: boolean; available: boolean; interval_hours: number; full_check_gb: number; note: string | null; warnings: string[]
  last: { ok: boolean; at: string; checks: CheckItem[]; every_file?: boolean; error?: string } | null
}

/** "today 03:12", "yesterday 03:12", or the date */
function checkedWhen(iso: string) {
  const d = new Date(iso), now = new Date()
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  const days = Math.round((new Date(now.toDateString()).getTime() - new Date(d.toDateString()).getTime()) / 86400000)
  return days === 0 ? `today ${time}` : days === 1 ? `yesterday ${time}` : when(iso)
}

function DataCheckBox({ dc, busy, onCheck, onPutBack }: { dc: DataCheck; busy: '' | 'check' | 'putback'; onCheck: () => void; onPutBack: () => void }) {
  const last = dc.last
  const files = last?.checks.some(c => c.name === 'Attached files' && !c.ok)
  return (
    <div className="panel backup-box">
      <h3>Daily data check</h3>
      <p>Once a day Benchtop checks the data it is using: the database, the audit trail, every signed record and trusted timestamp,
        and that every attached file is there and unchanged. Files are re-read byte for byte every day while they total up to {dc.full_check_gb} GB; above that, a seventh of them each day, so each file once a week.</p>
      {!dc.available ? <p className="faint">{dc.note}</p>
        : !dc.enabled ? <p className="faint">Turned off on this server (BENCHTOP_CHECK_INTERVAL_HOURS=0). <code>./benchtop check</code> still checks when you ask.</p>
        : !last ? <p className="faint">Not checked yet: the first check runs within the hour after Benchtop starts.</p> : null}
      {dc.available && dc.enabled && last && <>
        <p className={last.ok ? '' : 'error'}><b>Last checked {checkedWhen(last.at)}: {last.ok ? 'all good' : last.error ? "couldn't run" : 'problems found'}</b></p>
        {dc.warnings.map(w => <p key={w}>{w}</p>)}
        {last.checks.length > 0 && <ul className="check-list">{last.checks.map(c => <li key={c.name} className={c.ok ? 'ok' : 'bad'}><span aria-hidden>{c.ok ? '✓' : '✗'}</span> <b>{c.name}</b> <span className="faint">{c.detail}</span></li>)}</ul>}
      </>}
      {dc.available && dc.enabled && <div className="row" style={{ gap: 6 }}>
        {files && <button className="btn primary sm" disabled={!!busy} onClick={onPutBack} title="Copies the good files back from the backups; nothing else changes">{busy === 'putback' ? 'Putting back…' : 'Put back from backups'}</button>}
        <button className="btn sm" disabled={!!busy} onClick={onCheck}>{busy === 'check' ? 'Checking…' : 'Check now'}</button></div>}
    </div>
  )
}

function DiskSpaceBox({ d }: { d: DiskSpace }) {
  return (
    <div className="panel backup-box">
      <h3>Disk space</h3>
      {d.places.map(p => {
        const low = d.warn_gb > 0 && p.free < p.warn_below
        const used = p.total > 0 ? Math.min(100, Math.max(0, 100 - (p.free / p.total) * 100)) : 0
        return <div key={p.path} className="stack" style={{ gap: 4 }}>
          <p><b>{p.label}</b>: <span className={low ? 'error' : ''}>{fmtBytes(p.free)} free</span> <span className="faint">of {fmtBytes(p.total)}</span></p>
          <div className={`disk-bar${low ? ' low' : ''}`} role="img" aria-label={`${Math.round(used)}% full`}><span style={{ width: `${used}%` }} /></div>
        </div>
      })}
      {d.warnings.map(w => <p key={w}>{w}</p>)}
      <p className="faint">Benchtop's own share: database {fmtBytes(d.database)}, attached files {fmtBytes(d.attached)}, backups {d.backups ? fmtBytes(d.backups) : 'none yet'}.</p>
      <p className="faint">{d.warn_gb > 0
        ? <>You're warned here and on the dashboard below {d.warn_gb} GB free. Uploads and instrument files that would leave less than {fmtBytes(d.reserve)} are refused, to keep room for saving.</>
        : <>Warnings are turned off on this server (BENCHTOP_DISK_WARN_GB=0).</>}</p>
    </div>
  )
}

function NmrData() {
  const [st, setSt] = useState<NmrStatus | null>(null)
  const [err, setErr] = useState('')
  const load = () => api.get<NmrStatus>('/api/nmr/status').then(setSt).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  useEffect(() => { if (!st?.updating) return; const t = setInterval(load, 2500); return () => clearInterval(t) }, [st?.updating])
  const start = async () => { setErr(''); try { setSt(await api.post<NmrStatus>('/api/nmr/update', {})) } catch (e: any) { setErr(e.message) } }
  const m = st?.meta
  return (
    <section className="stack" style={{ gap: 8 }}>
      <div className="spread"><h2 style={{ fontSize: 17 }}>NMR prediction data</h2>
        <button className="btn sm" disabled={!st || st.updating} onClick={start}>{st?.updating ? 'Updating…' : st?.available ? 'Update now' : 'Download'}</button></div>
      <p className="faint" style={{ margin: 0 }}>Predicted ¹H and ¹³C shifts come from NMRShiftDB2, an open database of assigned spectra (nmrshiftdb2 data licence: attribution, share-alike).
        Benchtop ships with a table built from a 2023 extract; it checks every {st?.refresh_days ?? 30} days while running and rebuilds when NMRShiftDB2 publishes a newer export (or now, with Update now or <code>./benchtop update-nmr-data</code>). Downloading and building takes a few minutes.</p>
      {st?.updating && <p style={{ margin: 0 }}><b>Working:</b> {st.progress}</p>}
      {st?.last_error && <p className="error" style={{ margin: 0 }}>Last update failed: {st.last_error}</p>}
      {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      {m ? <dl className="kv"><dt>Source</dt><dd>{m.source}</dd><dt>Table</dt><dd>{m.table}</dd>
        <dt>Built</dt><dd>{when(m.built_at)}{st?.age_days != null ? ` · ${Math.round(st.age_days)} days ago` : ''}</dd>
        <dt>Structures</dt><dd>{Number(m.molecules).toLocaleString()}</dd>
        <dt>Assigned shifts</dt><dd>{Number(m.assignments_13C).toLocaleString()} ¹³C · {Number(m.assignments_1H).toLocaleString()} ¹H</dd>
        <dt>Your lab's data</dt><dd>{st?.lab?.shifts ? `${st.lab.shifts.toLocaleString()} confirmed shifts from ${st.lab.experiments} signed experiment${st.lab.experiments === 1 ? '' : 's'}, used on top of NMRShiftDB2` : 'none yet: confirm the assignments on a spectrum, then sign the experiment'}</dd>
        <dt>Method</dt><dd>{m.method}</dd>
        <dt>Checks for updates</dt><dd>{(st as any)?.auto_update ? `monthly${(st as any)?.last_check ? `, last ${when((st as any).last_check)}` : ''}` : 'off (BENCHTOP_NMR_AUTO_UPDATE=off)'}</dd></dl>
        : st && !st.updating && <p className="faint" style={{ margin: 0 }}>No prediction table found.</p>}
    </section>
  )
}

function fmtBytes(n: number) {
  return n >= 100e9 ? `${Math.round(n / 1e9)} GB` : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`
}

function Backups() {
  const { toast } = useApp()
  const [info, setInfo] = useState<BackupInfo | null>(null)
  const [busy, setBusy] = useState<'' | 'backup' | 'test' | 'folder' | 'check' | 'putback'>('')
  const [err, setErr] = useState('')
  const [folder, setFolder] = useState<string | null>(null)  // editing the second-copy folder
  const load = () => api.get<BackupInfo>('/api/admin/backups').then(setInfo).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  const run = async (kind: typeof busy, fn: () => Promise<void>) => {
    setBusy(kind); setErr('')
    try { await fn() } catch (e: any) { setErr(e.message) } finally { setBusy(''); load() }
  }
  const backupNow = () => run('backup', async () => {
    const b = await api.post<any>('/api/admin/backups')
    toast(b.copy ? (b.copy.ok ? 'Backup written and copied' : `Backup written, but the copy failed: ${b.copy.error}`) : 'Backup written')
  })
  const test = () => run('test', async () => {
    const r = await api.post<BackupTest>('/api/admin/backups/test')
    toast(r.ok ? `${r.name} restores cleanly` : 'The backup test found problems; see below')
  })
  const saveFolder = (path: string) => run('folder', async () => {
    const r = await api.put<any>('/api/admin/backups/copy-dir', { path })
    setFolder(null)
    toast(!path ? 'Second copies turned off' : r.first_copy?.ok ? 'Folder set; the newest backup is already copied there' : r.first_copy ? `Folder set, but copying failed: ${r.first_copy.error}` : 'Folder set')
  })
  const checkNow = () => run('check', async () => {
    const r = await api.post<DataCheck>('/api/admin/check')
    toast(r.last?.ok ? 'Checked: all good' : 'The check found problems; see below')
  })
  const putBack = () => run('putback', async () => {
    const r = await api.post<DataCheck & { put_back: string[]; no_good_copy: string[] }>('/api/admin/check/put-back-files')
    const n = r.put_back.length
    toast(r.no_good_copy.length ? `${n} put back; no good copy in the backups of ${r.no_good_copy.join(', ')}` : `${n} file${n === 1 ? '' : 's'} put back${r.last?.ok ? ': all good now' : ''}`)
  })
  const lc = info?.last_check
  return (
    <section className="stack" style={{ gap: 10 }}>
      <div className="spread"><h2 style={{ fontSize: 17 }}>Backups</h2>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn sm" disabled={!!busy || !info?.backups.length} onClick={test}>{busy === 'test' ? 'Testing…' : 'Test the latest backup'}</button>
          <button className="btn sm" disabled={!!busy} onClick={backupNow}>{busy === 'backup' ? 'Backing up…' : 'Back up now'}</button></div></div>
      {info && <p className="faint" style={{ margin: 0 }}>
        {info.interval_hours > 0 ? `Automatic every ${info.interval_hours} h` : 'Automatic backups are off'}, keeping the newest {info.keep}.
        Saved in <code>{info.directory}</code>. Attached files are kept once for all backups, in the <code>benchtop-files</code> folder
        beside them{info.files && info.files.files > 0 ? ` (${info.files.files.toLocaleString()} files, ${fmtBytes(info.files.bytes)})` : ''}:
        keep that folder with the backups if you move or copy them. To restore, stop Benchtop and run <code>./benchtop restore &lt;file&gt;</code>.</p>}
      {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      {info && info.warnings.length > 0 && <div className="notice" role="status">{info.warnings.map(w => <div key={w}>{w}</div>)}</div>}

      {info && <div className="backup-boxes">
        <div className="panel backup-box">
          <h3>Second copy</h3>
          {folder === null ? <>
            <p>{info.copy_dir ? <>Every backup is also copied to <code>{info.copy_dir}</code> ({info.copies} there now).</>
              : 'Backups are only on this computer. If it is lost or its drive fails, so are they. Choose a second place: iCloud Drive, or an external drive.'}</p>
            {info.last_copy && <p className={info.last_copy.ok ? 'faint' : 'error'}>{info.last_copy.ok ? `Last copied ${when(info.last_copy.at)}.` : `Last copy failed (${when(info.last_copy.at)}): ${info.last_copy.error}`}</p>}
            {info.copy_dir_from_env ? <p className="faint">Set by BENCHTOP_BACKUP_COPY_DIR in this server's settings.</p>
              : <div className="row" style={{ gap: 6 }}>
                <button className="btn sm" onClick={() => setFolder(info.copy_dir || info.suggestions[0]?.path || '')}>{info.copy_dir ? 'Change folder' : 'Choose a folder'}</button>
                {info.copy_dir && <button className="btn ghost sm" disabled={!!busy} onClick={() => saveFolder('')}>Stop copying</button>}</div>}
          </> : <div className="stack" style={{ gap: 8 }}>
            {info.suggestions.length > 0 && <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {info.suggestions.map(s => <button key={s.path} className={`btn sm${folder === s.path ? ' primary' : ''}`} onClick={() => setFolder(s.path)}>{s.label}</button>)}</div>}
            <label className="field">Folder<input className="input" value={folder} onChange={e => setFolder(e.target.value)} placeholder="/Volumes/MyDrive/Benchtop backups" /></label>
            <p className="faint" style={{ margin: 0 }}>Benchtop creates the last folder in the path if needed. An external drive only has to be plugged in when a backup runs; missed copies are reported here.</p>
            <div className="row" style={{ gap: 6 }}><button className="btn primary sm" disabled={!folder.trim() || !!busy} onClick={() => saveFolder(folder)}>{busy === 'folder' ? 'Checking…' : 'Use this folder'}</button>
              <button className="btn ghost sm" onClick={() => setFolder(null)}>Cancel</button></div>
          </div>}
        </div>
        <div className="panel backup-box">
          <h3>Backup test</h3>
          <p>Once a week Benchtop unpacks the newest backup{info.copy_dir ? ' from the second copy' : ''} into a temporary folder and checks it as if it had been restored: the database, the audit trail, every signed record and every file.</p>
          {lc ? <>
            <p className={lc.ok ? '' : 'error'}><b>{lc.ok ? 'Passed' : 'Problems found'}</b> <span className="faint">· {lc.name} ({lc.where}) · {when(lc.at)}</span></p>
            {!lc.ok && <p className="faint">Your live data isn't changed by this test. Run <code>./benchtop check</code> to see whether the live data has the same problem; for a signed record that no longer matches, open it and use Verify integrity. Older backups are listed below.</p>}
            <ul className="check-list">{lc.checks.map(c => <li key={c.name} className={c.ok ? 'ok' : 'bad'}><span aria-hidden>{c.ok ? '✓' : '✗'}</span> <b>{c.name}</b> <span className="faint">{c.detail}</span></li>)}</ul>
          </> : <p className="faint">Not tested yet.</p>}
        </div>
        {info.disk && <DiskSpaceBox d={info.disk} />}
        {info.data_check && <DataCheckBox dc={info.data_check} busy={busy === 'check' || busy === 'putback' ? busy : ''} onCheck={checkNow} onPutBack={putBack} />}
      </div>}

      <div className="panel grid-wrap" style={{ border: 0 }}>
        <table className="plain"><thead><tr><th>File</th><th>Made</th><th>Size</th></tr></thead>
          <tbody>{(info?.backups || []).map(b => (
            <tr key={b.name}><td>{b.name}</td><td>{when(b.created_at)}</td><td className="num">{(b.size / 1e6).toFixed(1)} MB</td></tr>))}
            {info && !info.backups.length && <tr><td colSpan={3} className="faint">No backups yet.</td></tr>}</tbody></table>
      </div>
    </section>
  )
}

/** Admin's tabs (0.26): each has its own address (/admin/backups…), so links from the dashboard and emails open it. */
export const ADMIN_TABS = [
  { key: 'users', label: 'Users' },
  { key: 'backups', label: 'Backups' },
  { key: 'instruments', label: 'Instruments' },
  { key: 'email', label: 'Email' },
  { key: 'signing', label: 'Signing' },
  { key: 'integrations', label: 'Integrations' },
  { key: 'audit', label: 'Audit trail' },
] as const
type TabKey = typeof ADMIN_TABS[number]['key']

/** Which tabs have a problem to look at: backup, data-check or disk-space warnings; an instrument folder with an error. */
function useProblems() {
  const [bad, setBad] = useState<Partial<Record<TabKey, string>>>({})
  const { pathname } = useLocation()
  useEffect(() => {
    let live = true
    api.get<{ backups: string[]; instruments: string[] }>('/api/admin/problems').then(r => {
      if (live) setBad(Object.fromEntries(Object.entries(r).filter(([, v]) => v.length).map(([k, v]) => [k, v.join(' ')])))
    }).catch(() => {})
    return () => { live = false }
  }, [pathname])  // looked at again on each tab change, so a fixed problem's dot goes
  return bad
}

export default function Admin() {
  const { tab = 'users' } = useParams()
  const problems = useProblems()
  const known = ADMIN_TABS.some(t => t.key === tab)
  if (!known) return <Navigate to="/admin" replace />
  return (
    <div className="stack" style={{ gap: 18 }}>
      <div><h1 className="page-title">Admin</h1>
        <p className="lede">People and their accounts, backups and checks of the data, instrument folders, email, signing, integrations, and the audit trail.</p></div>
      <nav className="tabs admin-tabs" aria-label="Admin">
        {ADMIN_TABS.map(t => (
          <NavLink key={t.key} to={t.key === 'users' ? '/admin' : `/admin/${t.key}`} end aria-current={tab === t.key ? 'page' : undefined}
            className={tab === t.key ? 'active' : ''}>
            {t.label}{problems[t.key] && <span className="tab-dot" title={problems[t.key]} aria-label=" (needs attention)" />}
          </NavLink>))}
      </nav>
      {tab === 'users' && <Users />}
      {tab === 'backups' && <Backups />}
      {tab === 'instruments' && <InstrumentsPanel />}
      {tab === 'email' && <EmailSettingsPanel />}
      {tab === 'signing' && <><WitnessSettingsPanel /><TimestampSettingsPanel /></>}
      {tab === 'integrations' && <><Plugins /><Webhooks /><ZenodoSettingsPanel /><NmrData /></>}
      {tab === 'audit' && <AuditTrail />}
    </div>
  )
}

function Users() {
  const { user: me, toast } = useApp()
  const [users, setUsers] = useState<User[]>([])
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [resetting, setResetting] = useState<User | null>(null)
  const load = () => { api.get<User[]>('/api/users').then(setUsers) }
  useEffect(load, [])
  const [added, setAdded] = useState<Added | null>(null)
  const toggle = async (u: User) => {
    if (u.active && !confirm(`Disable ${displayName(u)} (${u.username})? They're signed out everywhere now and can't sign in until an admin enables the account again. Their records stay as they are.`)) return
    try { await api.patch(`/api/users/${u.id}`, { active: !u.active }); toast(u.active ? `${u.username} is disabled and signed out` : `${u.username} can sign in again`) }
    catch (e: any) { toast(e.message) }
    load()
  }
  const role = async (u: User, r: Role) => {
    try { await api.patch(`/api/users/${u.id}`, { role: r }); toast(`${u.username} is now ${r === 'admin' ? 'an admin' : r === 'viewer' ? 'a viewer (read-only)' : 'a scientist'}`) }
    catch (e: any) { toast(e.message) }
    load()
  }
  const signOut = async (u: User) => {
    try { const r = await api.post<{ ended: number }>(`/api/users/${u.id}/sign-out`); toast(`Signed ${u.username} out of ${r.ended} device${r.ended === 1 ? '' : 's'}`) }
    catch (e: any) { toast(e.message) }
  }
  return (
    <section className="stack" style={{ gap: 8 }}>
      <div className="spread"><h2 style={{ fontSize: 17 }}>Users</h2><button className="btn sm" onClick={() => setAdding(true)}>Add user</button></div>
      <div className="panel grid-wrap" style={{ border: 0 }}>
        <table className="plain"><thead><tr><th>Username</th><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th><span className="vh">Actions</span></th></tr></thead>
          <tbody>{users.map(u => (
            <tr key={u.id}><td>{u.username}</td><td>{u.full_name}</td><td className="faint">{u.email}</td>
              <td><select className="select" style={{ padding: '2px 6px' }} value={u.role} disabled={u.id === me.id} onChange={e => role(u, e.target.value as Role)} aria-label={`Role for ${u.username}`}>
                <option value="scientist">scientist</option><option value="viewer">viewer</option><option value="admin">admin</option></select></td>
              <td>{u.active ? (u.must_change_password ? 'Active · must set password' : u.has_password === false ? 'Active · single sign-on' : 'Active') : 'Disabled'}</td>
              <td style={{ whiteSpace: 'nowrap' }}><button className="btn ghost sm" onClick={() => setEditing(u)}>Edit</button>
                <button className="btn ghost sm" onClick={() => setResetting(u)}>Reset password</button>
                {u.id !== me.id && <button className="btn ghost sm" onClick={() => signOut(u)} title="End all of their sessions, e.g. a lost laptop">Sign out everywhere</button>}
                {u.id !== me.id && <button className="btn ghost sm" onClick={() => toggle(u)}>{u.active ? 'Disable' : 'Enable'}</button>}</td></tr>
          ))}</tbody></table>
      </div>
      {editing && <EditUser u={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); load(); toast('User updated') }} />}
      {resetting && <ResetPassword u={resetting} onClose={() => setResetting(null)} onDone={() => { setResetting(null); load(); toast(`Password reset; ${resetting.username} was signed out and will choose a new one`) }} />}
      {adding && <UserForm onClose={() => setAdding(false)} onSaved={a => { setAdding(false); load(); toast(`Added ${a.u.username}`); setAdded(a) }} />}
      {added && <UserAdded {...added} onClose={() => setAdded(null)} />}
    </section>
  )
}

function AuditTrail() {
  const { groups } = useApp()
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [people, setPeople] = useState<User[]>([])
  const [chain, setChain] = useState<any>(null)
  useEffect(() => { api.get<AuditEvent[]>('/api/audit?limit=150').then(setEvents); api.get<User[]>('/api/users').then(setPeople).catch(() => {}) }, [])
  const names: AuditNames = {
    people: Object.fromEntries(people.map(u => [u.username, displayName(u)])),
    userIds: Object.fromEntries(people.map(u => [u.id, displayName(u)])),
    groups: Object.fromEntries(groups.map(g => [g.id, g.name])),
  }
  return (
    <section className="stack" style={{ gap: 8 }}>
      <div className="spread"><h2 style={{ fontSize: 17 }}>Audit trail</h2>
        <button className="btn sm" onClick={async () => setChain(await api.get('/api/audit/verify'))}>Verify hash chain</button></div>
      <p className="faint" style={{ margin: 0 }}>Every change anyone makes, newest first (the latest 150 here). Each event is chained to the one before by its fingerprint, so an event changed or removed outside Benchtop breaks the chain: Verify hash chain checks all of them. Open Details to see an event exactly as it is stored.</p>
      {chain && <p className={chain.ok ? 'notice' : 'error'} style={chain.ok ? { borderLeftColor: 'var(--witnessed)' } : undefined}>
        {chain.ok ? `All ${chain.checked} events verified. Chain head ${chain.head.slice(0, 16)}…${chain.forks ? ` (${chain.forks} pair${chain.forks === 1 ? ' was' : 's were'} written at the same moment by an older version; both intact, not tampering.)` : ''}` : `Chain broken at event #${chain.broken_at} (${chain.reason}). Records from that point may have been altered.`}</p>}
      <div className="panel panel-pad">
        <ul className="timeline audit-list">{events.map(e => {
          const said = auditSentence(e, names)
          return <li key={e.id}><span className="faint">{when(e.ts)}</span>
            <span className="audit-what">{said ?? <><span className="act">{e.action}</span> {e.object_type} {e.object_id ?? ''} <span className="faint">by {e.username}</span></>}
              <details className="audit-raw"><summary>Details</summary>
                <dl className="kv">
                  <dt>Action</dt><dd className="mono">{e.action}</dd>
                  <dt>On</dt><dd className="mono">{e.object_type}{e.object_id != null ? ` #${e.object_id}` : ''}</dd>
                  <dt>By</dt><dd className="mono">{e.username}</dd>
                  <dt>Details</dt><dd className="mono">{JSON.stringify(e.details)}</dd>
                  <dt>Event</dt><dd className="mono">#{e.id} · {e.hash}</dd>
                </dl></details></span></li>
        })}</ul>
      </div>
    </section>
  )
}
