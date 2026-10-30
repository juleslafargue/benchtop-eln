import { useEffect, useState } from 'react'
import { THEMES } from '../themeList'
import { applyPalette } from '../theme'
import { ApiTokens } from '../components/Integrations'
import { ZenodoAccount } from '../components/Publish'
import { useApp } from '../App'
import { ago, api, downloadAuthed, when, type Preferences, type User } from '../api'
import { initials } from '../components/Layout'
import PasswordDialog from '../components/PasswordDialog'
import Modal from '../components/Modal'

function UsernameDialog({ current, onDone, onClose }: { current: string; onDone: (u: User) => void; onClose: () => void }) {
  const [name, setName] = useState(current)
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const save = async () => {
    try { onDone(await api.post<User>('/api/auth/username', { new_username: name, password })) } catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title="Change username" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name.trim() || !password || name === current} onClick={save}>Change username</button></>}>
      <div className="stack">
        <label className="field">New username<input className="input" autoFocus value={name} onChange={e => setName(e.target.value.toLowerCase())} /></label>
        <p className="faint" style={{ margin: 0 }}>2–32 characters: lower-case letters, digits, “.”, “_” or “-”. You'll sign in with this from now on. Past records keep showing who did what.</p>
        <label className="field">Current password<input className="input" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} /></label>
        {err && <p className="error">{err}</p>}
      </div>
    </Modal>
  )
}

export default function Account() {
  const { user, setUser, notebooks, archivedNotebooks = [], toast } = useApp()
  const archivedDefault = archivedNotebooks.find(n => n.id === user.preferences?.default_notebook_id)
  const [name, setName] = useState(user.full_name)
  const [email, setEmail] = useState(user.email)
  const [orcid, setOrcid] = useState(user.orcid || '')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [pw, setPw] = useState(false)
  const [uname, setUname] = useState(false)
  const dirty = name.trim() !== user.full_name || email.trim() !== user.email || orcid.trim() !== (user.orcid || '')

  const saveProfile = async () => {
    setBusy(true); setErr('')
    try { const u = await api.patch<User>('/api/auth/me', { full_name: name, email, orcid }); setUser(u); setOrcid(u.orcid || ''); toast('Profile saved') }
    catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  const setPref = async (p: Preferences) => {
    try { setUser(await api.patch<User>('/api/auth/me', { preferences: p })) } catch (e: any) { toast(e.message) }
  }
  const theme = user.preferences?.theme || 'system'

  return (
    <div className="stack" style={{ gap: 22 }}>
      <div className="row" style={{ gap: 14 }}>
        <span className="avatar lg" aria-hidden>{initials(name || user.username)}</span>
        <div><h1 className="page-title">Account</h1><p className="lede" style={{ margin: 0 }}>Signed in as <b>{user.username}</b> · {user.role}</p></div>
      </div>

      <div className="settings">
        <div><h2>Profile</h2><p className="help">How you appear to colleagues and on records.</p></div>
        <form className="panel" onSubmit={e => { e.preventDefault(); if (dirty) saveProfile() }}>
          <div className="section stack" style={{ gap: 14 }}>
            <label className="field">Full name<input className="input" value={name} onChange={e => setName(e.target.value)} autoComplete="name" /></label>
            <div>
              <div className="faint" style={{ marginBottom: 6 }}>Your signatures will read</div>
              <div className="sig-preview">{name.trim() || user.username}</div>
              <p className="help" style={{ marginTop: 6 }}>Records you've already signed keep the name you used at the time.</p>
            </div>
            <label className="field">Email<input className="input" type="email" value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" placeholder="you@university.edu" /></label>
            <label className="field">ORCID iD
              <input className="input" value={orcid} onChange={e => setOrcid(e.target.value)} placeholder="0000-0002-1825-0097" spellCheck={false} autoComplete="off" />
              <span className="help">Optional: your researcher ID from <a href="https://orcid.org" target="_blank" rel="noreferrer noopener">orcid.org</a>. Exports (.eln) name
                you by it, so your records stay credited to you wherever they go.</span></label>
            {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
            <div className="row"><button className="btn primary" disabled={!dirty || busy}>Save profile</button>
              {dirty && <button type="button" className="btn ghost" onClick={() => { setName(user.full_name); setEmail(user.email); setOrcid(user.orcid || '') }}>Discard changes</button>}</div>
          </div>
        </form>

        <div><h2>Your records</h2><p className="help">Take them with you: when you finish your degree, for a thesis appendix, a data
          repository such as Zenodo, or another lab's notebook.</p></div>
        <div className="panel"><div className="section stack" style={{ gap: 8 }}>
          <div className="spread" style={{ alignItems: 'center', gap: 12 }}>
            <span>Everything you can open in Benchtop (your experiments, your groups', and those shared with you), with every file,
              in the <b>.eln</b> format that eLabFTW, RSpace, Kadi4Mat and other lab notebooks read.</span>
            <button className="btn" onClick={() => { toast('Preparing the export; a big one takes a minute…'); downloadAuthed('/api/export.eln', `Benchtop - ${(user.full_name || user.username).replace(/[\\/:*?"<>|#^[\]{}%]+/g, ' ')} - ${new Date().toISOString().slice(0, 10)}.eln`).catch(e => toast(e.message)) }}>Export everything (.eln)</button>
          </div>
          <p className="help" style={{ margin: 0 }}>Each record comes with its printable report, so it can be read without any software. To bring records
            in from another notebook, open a notebook and choose <b>Import .eln…</b></p>
        </div></div>

        <div><h2>Sample codes</h2><p className="help">Short labels for your samples, like JL-013, that instruments understand. Type one as the sample
          name on the NMR or HPLC, and the data goes straight into the right experiment.</p></div>
        <SamplePrefix />

        <div><h2>Sign-in</h2><p className="help">Your username and password. Both changes are recorded in the audit trail.</p></div>
        <div className="panel">
          <div className="section spread" style={{ alignItems: 'center' }}>
            <div><div className="faint">Username</div><b style={{ fontWeight: 600 }}>{user.username}</b></div>
            <button className="btn" onClick={() => setUname(true)}>Change username</button>
          </div>
          <div className="section spread" style={{ alignItems: 'center' }}>
            <div><div className="faint">{user.has_password === false ? 'Signing password' : 'Password'}</div>{user.has_password === false
              ? <span className="warn-text">Not set: you sign in with single sign-on, and need a password to sign records</span> : '••••••••'}</div>
            <button className={`btn${user.has_password === false ? ' primary' : ''}`} onClick={() => setPw(true)}>{user.has_password === false ? 'Set signing password' : 'Change password'}</button>
          </div>
        </div>

        <div><h2>Where you're signed in</h2><p className="help">Each browser you've signed in with. Sign out any you don't recognise, or that you can't get to any more (a lab computer, a lost laptop). Changing your password signs out all the others automatically.</p></div>
        <Sessions />

        {user.role !== 'viewer' && <>
          <div><h2>Zenodo</h2><p className="help">To publish your signed records as open datasets with a DOI (each record's page has <i>Publish on Zenodo</i>).
            Benchtop uses your own Zenodo account, through a personal access token.</p></div>
          <ZenodoAccount />
        </>}

        <div><h2>API tokens</h2><p className="help">For scripts and other programs (an instrument PC, a data pipeline) that need to read or add to your notebooks. A token acts as you, but can never sign records or change settings.</p></div>
        <ApiTokens />

        <div><h2>Email</h2><p className="help">Which emails Benchtop sends you. You always see these as notifications in Benchtop too.</p></div>
        <EmailPrefs />

        <div><h2>Benchtop as an app</h2><p className="help">Its own window and Dock icon, without the browser's tabs and address bar. Your notebook stays where it is; the app is a window onto it.</p></div>
        <InstallApp />

        <div><h2>Preferences</h2><p className="help">Saved to your account, so they follow you to other computers.</p></div>
        <div className="panel">
          <div className="section">
            <div className="faint" style={{ marginBottom: 6 }}>Light or dark</div>
            <div className="choice" role="group" aria-label="Light or dark">
              {(['system', 'light', 'dark'] as const).map(t => (
                <button key={t} aria-pressed={theme === t} onClick={() => setPref({ theme: t })}>{{ system: 'Match system', light: 'Light', dark: 'Dark' }[t]}</button>
              ))}
            </div>
          </div>
          <div className="section">
            <div className="faint" style={{ marginBottom: 8 }}>Theme</div>
            <ThemePicker value={user.preferences?.palette || 'benchtop'} onChange={id => { applyPalette(id); setPref({ palette: id }) }} />
          </div>
          <div className="section">
            <label className="field" style={{ maxWidth: 360 }}>Default notebook for new experiments
              <select className="select" value={user.preferences?.default_notebook_id ?? ''} onChange={e => setPref({ default_notebook_id: e.target.value ? Number(e.target.value) : null })}>
                <option value="">First in the list</option>
                {notebooks.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
                {archivedDefault && <option value={archivedDefault.id}>{archivedDefault.name} (archived, so not used)</option>}
              </select>
            </label>
          </div>
          <div className="section">
            <label className="row" style={{ gap: 8 }}>
              <input type="checkbox" checked={!!user.preferences?.spectrum_expanded} onChange={e => setPref({ spectrum_expanded: e.target.checked })} />
              Open spectra full-screen
            </label>
          </div>
        </div>
      </div>
      {pw && <PasswordDialog onClose={() => setPw(false)} />}
      {uname && <UsernameDialog current={user.username} onClose={() => setUname(false)} onDone={u => { setUser(u); setUname(false); toast('Username changed') }} />}
    </div>
  )
}


interface SessionRow { id: string; created_at: string; last_seen_at: string; expires_at: string; method: string; user_agent: string; ip: string; current: boolean }

function device(ua: string) {
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : ua ? 'Browser' : 'Unknown browser'
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android' : /Mac OS X/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : ''
  return os ? `${browser} on ${os}` : browser
}

function Sessions() {
  const { toast } = useApp()
  const [rows, setRows] = useState<SessionRow[] | null>(null)
  const load = () => api.get<SessionRow[]>('/api/auth/sessions').then(setRows).catch(() => setRows([]))
  useEffect(() => { load() }, [])
  const end = async (id: string) => { try { await api.del(`/api/auth/sessions/${id}`); toast('Signed out that device'); load() } catch (e: any) { toast(e.message) } }
  const others = async () => { try { const r = await api.post<{ ended: number }>('/api/auth/sessions/end-others'); toast(`Signed out ${r.ended} other device${r.ended === 1 ? '' : 's'}`); load() } catch (e: any) { toast(e.message) } }
  if (!rows) return <div className="panel"><div className="section faint">Loading…</div></div>
  return (
    <div className="panel">
      {rows.map(r => (
        <div key={r.id} className="section spread session-row">
          <div>
            <b style={{ fontWeight: 600 }}>{device(r.user_agent)}</b>{r.current && <span className="tag lead" style={{ marginLeft: 8 }}>This browser</span>}
            <div className="faint">{r.ip ? `${r.ip} · ` : ''}{r.method === 'sso' ? 'single sign-on · ' : ''}signed in {when(r.created_at)} · active {ago(r.last_seen_at)}</div>
          </div>
          {!r.current && <button className="btn sm" onClick={() => end(r.id)}>Sign out</button>}
        </div>
      ))}
      {rows.length > 1 && <div className="section"><button className="btn sm" onClick={others}>Sign out all other devices</button></div>}
    </div>
  )
}


/** A miniature Benchtop window in a theme's colours. */
function ThemePreview({ c }: { c: Record<string, string> }) {
  return (
    <span className="tp-win" style={{ background: c.ground }} aria-hidden>
      <span className="tp-side">{[70, 52, 60].map((w, i) => <span key={i} style={{ width: `${w}%`, background: i === 0 ? c.accent : c['ink-3'] }} />)}</span>
      <span className="tp-card" style={{ background: c.paper, borderColor: c.rule }}>
        <span className="tp-title" style={{ background: c.ink }} />
        <span className="tp-line" style={{ background: c['ink-3'] }} />
        <span className="tp-line short" style={{ background: c['ink-3'] }} />
        <span className="tp-row"><span className="tp-chip" style={{ background: c.witnessed }} /><span className="tp-chip" style={{ background: c.signed }} />
          <span className="tp-btn" style={{ background: c.accent }}><span style={{ background: c['accent-ink'] }} /></span></span>
      </span>
    </span>
  )
}

function ThemePicker({ value, onChange }: { value: string; onChange: (id: string) => void }) {
  return (
    <div className="theme-grid" role="radiogroup" aria-label="Theme">
      {THEMES.map(t => (
        <button key={t.id} type="button" role="radio" aria-checked={value === t.id} className="theme-opt" onClick={() => onChange(t.id)}>
          <span className="tp-pair"><ThemePreview c={t.preview.light} /><ThemePreview c={t.preview.dark} /></span>
          <span className="theme-name">{t.label}</span>
          <span className="theme-desc">{t.description}</span>
        </button>
      ))}
    </div>
  )
}


/** "Install Benchtop" where the browser can (Chrome, Edge), and how to do it elsewhere. */
function InstallApp() {
  const [offer, setOffer] = useState<any>(() => (window as any).__benchtopInstall || null)
  const standalone = typeof window.matchMedia === 'function' && (window.matchMedia('(display-mode: standalone)').matches || (navigator as any).standalone)
  useEffect(() => {
    const on = () => setOffer((window as any).__benchtopInstall || null)
    window.addEventListener('benchtop:installable', on)
    return () => window.removeEventListener('benchtop:installable', on)
  }, [])
  const ua = navigator.userAgent
  const safari = /Safari/.test(ua) && !/Chrome|Chromium|Edg\//.test(ua)
  const phone = /iPhone|iPad|Android/.test(ua)
  const install = async () => { await offer.prompt(); const r = await offer.userChoice; if (r?.outcome === 'accepted') setOffer(null) }
  return (
    <div className="panel"><div className="section stack" style={{ gap: 10 }}>
      {standalone ? <p style={{ margin: 0 }}>You're using the Benchtop app. To remove it, quit it and delete it like any app (or right-click its Dock icon and choose Options → Remove from Dock).</p>
        : offer ? <div className="row" style={{ gap: 10, flexWrap: 'wrap' }}><button className="btn primary" onClick={install}>Install Benchtop</button>
          <span className="faint">Adds Benchtop to your Applications and Dock.</span></div>
        : <ul className="install-steps">
          {phone ? <li><b>iPhone / iPad:</b> Share button → <i>Add to Home Screen</i>. <b>Android:</b> browser menu → <i>Install app</i>.</li> : <>
            <li className={safari ? 'here' : ''}><b>Safari</b> (macOS Sonoma or later): <i>File → Add to Dock</i>.</li>
            <li className={!safari ? 'here' : ''}><b>Chrome or Edge:</b> the install icon at the right of the address bar, or the browser menu → <i>Cast, save and share → Install page as app</i>.</li>
            <li><b>Firefox</b> doesn't install web apps on Mac; use Safari or Chrome for this.</li></>}
        </ul>}
      {!standalone && !window.isSecureContext && <p className="faint" style={{ margin: 0 }}>This computer reaches Benchtop over the network (http), and Chrome only installs secure addresses, so use Safari's <i>Add to Dock</i> here. On the computer running Benchtop, any browser works.</p>}
      <p className="faint" style={{ margin: 0 }}>Benchtop itself still has to be running (<code>./benchtop</code>, or the lab computer). Upgrades show in the app the next time it opens.</p>
    </div></div>
  )
}


function EmailPrefs() {
  const { user, setUser, toast } = useApp()
  const [configured, setConfigured] = useState<boolean | null>(null)
  useEffect(() => { api.get<{ configured: boolean }>('/api/auth/email-status').then(r => setConfigured(r.configured)).catch(() => setConfigured(false)) }, [])
  const prefs = user.preferences?.email_notify || {}
  const set = async (k: 'witness' | 'results' | 'system', v: boolean) => {
    try { setUser(await api.patch<User>('/api/auth/me', { preferences: { email_notify: { ...prefs, [k]: v } } })) } catch (e: any) { toast(e.message) }
  }
  const noAddress = !(user.email || '').trim()
  return (
    <div className="panel"><div className="section stack" style={{ gap: 8 }}>
      <label className="check-row"><input type="checkbox" checked={prefs.witness !== false} onChange={e => set('witness', e.target.checked)} disabled={noAddress} />
        When someone asks me to witness a record, and reminders while it waits</label>
      <label className="check-row"><input type="checkbox" checked={prefs.results !== false} onChange={e => set('results', e.target.checked)} disabled={noAddress} />
        When my records are witnessed or returned to me</label>
      {user.role === 'admin' && <label className="check-row"><input type="checkbox" checked={prefs.system !== false} onChange={e => set('system', e.target.checked)} disabled={noAddress} />
        When the daily data check finds a problem, or a disk Benchtop uses runs low on space (admins; always shown on the dashboard too)</label>}
      {noAddress ? <p className="faint" style={{ margin: 0 }}>Add your email address under Profile to get these.</p>
        : configured === false && <p className="faint" style={{ margin: 0 }}>This Benchtop doesn't send email yet (an admin sets it up in Admin → Email), so for now these arrive as notifications only.</p>}
    </div></div>
  )
}

/** The start of this person's sample codes (their initials). */
function SamplePrefix() {
  const { user, setUser, toast } = useApp()
  const [v, setV] = useState(user.sample_prefix || '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const shown = (v.trim() || 'JL').toUpperCase()
  const dirty = v.trim().toUpperCase() !== (user.sample_prefix || '')
  const save = async () => {
    setBusy(true); setErr('')
    try { const u = await api.put<User>('/api/auth/me/sample-prefix', { prefix: v.trim() || null }); setUser(u); setV(u.sample_prefix || ''); toast(u.sample_prefix ? `Your sample codes start with ${u.sample_prefix}` : 'Sample codes turned off') }
    catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <form className="panel" onSubmit={e => { e.preventDefault(); if (dirty) save() }}>
      <div className="section stack" style={{ gap: 12 }}>
        <label className="field">Your initials<input className="input mono" style={{ maxWidth: 140, textTransform: 'uppercase' }} maxLength={6} value={v}
          onChange={e => setV(e.target.value.replace(/[^A-Za-z0-9]/g, ''))} placeholder="JL" /></label>
        <p className="help" style={{ margin: 0 }}>On an experiment, <b>New sample code</b> gives you the next number: <span className="mono">{shown}-013</span>.
          Label the vial with it and type it as the instrument's sample name; a date after it is fine (<span className="mono">{shown}-013-20261021</span>).
          A code Benchtop doesn't know yet goes to your Inbox, never to a guess.</p>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
        <div className="row"><button className="btn primary" disabled={!dirty || busy}>{busy ? 'Saving…' : 'Save'}</button>
          {user.sample_prefix && !dirty && <span className="faint">Codes you've already handed out keep working if you change this.</span>}</div>
      </div>
    </form>
  )
}
