import { useEffect, useState, type ReactNode } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName } from '../api'
import NewNotebookDialog from './NewNotebookDialog'
import NewExperimentDialog from './NewExperimentDialog'
import CommandPalette from './CommandPalette'
import BackButton, { BackProvider } from './BackButton'
import { AudIcon } from './Audience'
import Notifications from './Notifications'
import MainNav from './MainNav'
import Logo from './Logo'
import { TimerTray } from './BenchTools'
import { useCollapsed } from '../collapse'

export const initials = (n: string) => n.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?'

export default function Layout({ children }: { children: ReactNode }) {
  const { user, notebooks, archivedNotebooks = [], logout, canWrite } = useApp()
  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [palette, setPalette] = useState(false)
  const [newExp, setNewExp] = useState(false)
  const [demo, setDemo] = useState(false)
  useEffect(() => { api.get<{ demo?: boolean }>('/api/health').then(h => setDemo(!!h.demo)).catch(() => {}) }, [])
  const [narrow, setNarrow] = useState(() => typeof matchMedia !== 'undefined' && matchMedia('(max-width: 900px)').matches)
  useEffect(() => {
    const mq = matchMedia('(max-width: 900px)'); const on = () => setNarrow(mq.matches)
    mq.addEventListener('change', on); return () => mq.removeEventListener('change', on)
  }, [])
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

  // ⌘K / Ctrl+K anywhere; "n" for a new experiment when not typing
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = /INPUT|TEXTAREA|SELECT/.test((e.target as HTMLElement).tagName) || (e.target as HTMLElement).isContentEditable
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setPalette(p => !p) }
      else if (!typing && !e.metaKey && !e.ctrlKey && !e.altKey && e.key === 'n' && canWrite && !document.querySelector('dialog[open]')) { e.preventDefault(); setNewExp(true) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canWrite])
  const loc = useLocation()
  const [nbHidden, setNbHidden] = useCollapsed('side.notebooks')
  useEffect(() => setOpen(false), [loc.pathname])
  // N on a notebook's page starts the experiment in that notebook
  const hereId = (() => { const m = /^\/notebooks\/(\d+)/.exec(loc.pathname); return m ? Number(m[1]) : NaN })()
  const hereNb = notebooks.some(n => n.id === hereId && !n.archived) ? hereId : undefined
  // archived notebooks: a small list under the others, shut unless asked for (or one of them is open)
  const [archOpen, setArchOpen] = useState(false)
  const showArch = archOpen || archivedNotebooks.some(n => n.id === hereId)

  return (
    <BackProvider>
    <div className="shell">
      <a className="skip-link" href="#main" onClick={e => { e.preventDefault(); document.getElementById('main')?.focus() }}>Skip to content</a>
      <aside className={`side${open ? ' open' : ''}`} aria-label="Main navigation">
        <div className="brand-row"><NavLink to="/" className="brand"><Logo size={26} />Benchtop{demo && <span className="demo-badge" title="Demo data: nothing here is real">Demo</span>}</NavLink>{!narrow && <Notifications />}</div>
        <button className="palette-trigger" onClick={() => setPalette(true)}>
          <span>Find or do anything…</span><kbd>{isMac ? '⌘K' : 'Ctrl K'}</kbd>
        </button>
        <MainNav />
        <TimerTray />
        <div>
          <div className="side-heading">
            <button className="side-toggle" onClick={() => setNbHidden(!nbHidden)} aria-expanded={!nbHidden} aria-controls="side-notebooks"
              title={nbHidden ? 'Show notebooks' : 'Hide notebooks'}>
              <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6l4 4 4-4" /></svg>
              Notebooks{nbHidden && notebooks.length > 0 && <span className="count">{notebooks.length}</span>}
            </button>
            {canWrite && <button className="btn ghost sm" onClick={() => setCreating(true)} aria-label="New notebook">+ New</button>}
          </div>
          <div className="nav" id="side-notebooks" hidden={nbHidden}>
            {notebooks.map(nb => (
              <NavLink key={nb.id} to={`/notebooks/${nb.id}`} className="nb-link">
                <span className="nb-name">{nb.name}{nb.visibility && nb.visibility !== 'everyone' &&
                  <span className={`nb-aud aud-${nb.visibility}`} title={nb.visibility === 'group' ? `${nb.group?.name} only` : 'Private'}><AudIcon v={nb.visibility} /></span>}</span>
                <span className="count">{nb.experiment_count}</span>
              </NavLink>
            ))}
            {notebooks.length === 0 && <p className="faint" style={{ padding: '0 8px' }}>No notebooks yet.{canWrite && ' Click + New to make one.'}</p>}
            {archivedNotebooks.length > 0 && <>
              <button className="side-toggle archived-toggle" onClick={() => setArchOpen(!showArch)} aria-expanded={showArch} aria-controls="side-archived"
                title={showArch ? 'Hide archived notebooks' : 'Show archived notebooks'}>
                <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6l4 4 4-4" /></svg>
                Archived ({archivedNotebooks.length})
              </button>
              <div className="nav archived-list" id="side-archived" hidden={!showArch}>
                {archivedNotebooks.map(nb => (
                  <NavLink key={nb.id} to={`/notebooks/${nb.id}`} className="nb-link">
                    <span className="nb-name">{nb.name}</span>
                    <span className="count">{nb.experiment_count}</span>
                  </NavLink>
                ))}
              </div>
            </>}
          </div>
        </div>
        <div className="side-foot">
          <NavLink to="/guide" className="guide-link"><span className="q" aria-hidden>?</span>Getting started</NavLink>
          <NavLink to="/account" className="account-link">
            <span className="avatar" aria-hidden>{initials(user.full_name || user.username)}</span>
            <span><span style={{ display: 'block', color: 'var(--ink)', fontWeight: 500 }}>{displayName(user)}</span><span className="faint">Account settings</span></span>
          </NavLink>
          <button className="btn ghost sm" style={{ alignSelf: 'flex-start' }} onClick={logout}>Sign out</button>
        </div>
      </aside>
      <main className="main" id="main" tabIndex={-1}>
        <div className="mobile-bar"><button className="btn menu-btn" onClick={() => setOpen(o => !o)} aria-expanded={open}>Menu</button>{narrow && <Notifications />}</div>
        <div className="page"><BackButton />{children}</div>
      </main>
      {creating && <NewNotebookDialog onClose={() => setCreating(false)} />}
      {newExp && <NewExperimentDialog notebookId={hereNb} onClose={() => setNewExp(false)} />}
      {palette && <CommandPalette onClose={() => setPalette(false)} onNewExperiment={() => setNewExp(true)} onNewNotebook={() => setCreating(true)} />}
    </div>
    </BackProvider>
  )
}
