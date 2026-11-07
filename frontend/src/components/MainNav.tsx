import { useCallback, useEffect, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { useApp } from '../App'
import { api, type User } from '../api'

/** The sidebar's main links. Everyone can reorder them (drag, or the arrow buttons) and hide the
 *  ones they don't use; the choice is saved to their account, so it follows them to any computer. */
const ITEMS = [
  { key: 'dashboard', to: '/', label: 'Dashboard', end: true },
  { key: 'inbox', to: '/inbox', label: 'Inbox' },
  { key: 'reactions', to: '/reactions', label: 'Reactions' },
  { key: 'compounds', to: '/compounds', label: 'Compounds' },
  { key: 'si', to: '/si', label: 'Supporting information' },
  { key: 'search', to: '/search', label: 'Search' },
  { key: 'materials', to: '/materials', label: 'Materials' },
  { key: 'inventory', to: '/inventory', label: 'Inventory' },
  { key: 'templates', to: '/templates', label: 'Templates' },
  { key: 'groups', to: '/groups', label: 'Groups' },
  { key: 'admin', to: '/admin', label: 'Admin', admin: true },
] as const
type Key = typeof ITEMS[number]['key']
type Item = typeof ITEMS[number]

export function orderedItems(order: string[] | undefined, isAdmin: boolean): Item[] {
  const avail = ITEMS.filter(i => !('admin' in i) || isAdmin)
  const pos = (k: string) => { const i = (order || []).indexOf(k); return i < 0 ? 100 + ITEMS.findIndex(x => x.key === k) : i }
  return [...avail].sort((a, b) => pos(a.key) - pos(b.key))
}

const Eye = ({ off }: { off: boolean }) => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
    <path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" /><circle cx="8" cy="8" r="2" />{off && <path d="M2.5 13.5l11-11" />}
  </svg>
)

/** How many instrument files wait in the Inbox for this person (checked every minute and on navigation). */
function useInboxCount() {
  const [n, setN] = useState(0)
  const loc = useLocation()
  const load = useCallback(() => { api.get<{ waiting: number }>('/api/inbox/count').then(r => setN(r.waiting)).catch(() => {}) }, [])
  useEffect(() => { load(); const t = setInterval(load, 60_000); addEventListener('benchtop:inbox', load)
    return () => { clearInterval(t); removeEventListener('benchtop:inbox', load) } }, [load])
  useEffect(() => { const t = setTimeout(load, 800); return () => clearTimeout(t) }, [loc.pathname, load])
  return n
}

export default function MainNav() {
  const { user, setUser, toast } = useApp()
  const inboxCount = useInboxCount()
  const isAdmin = user.role === 'admin'
  const [editing, setEditing] = useState(false)
  const [drag, setDrag] = useState<Key | null>(null)
  const [over, setOver] = useState<Key | null>(null)
  const items = orderedItems(user.preferences?.nav, isAdmin)
  const hidden = new Set(user.preferences?.nav_hidden || [])

  const save = async (nav: string[], nav_hidden: string[]) => {
    const prev = user
    setUser({ ...user, preferences: { ...user.preferences, nav, nav_hidden } })  // feels instant
    try { setUser(await api.patch<User>('/api/auth/me', { preferences: { nav, nav_hidden } })) }
    catch (e: any) { setUser(prev); toast(e.message) }
  }
  const keys = items.map(i => i.key as string)
  const move = (k: Key, to: number) => {
    const from = keys.indexOf(k); if (from < 0 || to < 0 || to >= keys.length || to === from) return
    const next = [...keys]; next.splice(from, 1); next.splice(to, 0, k); save(next, [...hidden])
  }
  const toggle = (k: Key) => { const h = new Set(hidden); h.has(k) ? h.delete(k) : h.add(k); save(keys, [...h]) }

  if (!editing) return (
    <nav className="nav">
      {items.filter(i => !hidden.has(i.key)).map(i => <NavLink key={i.key} to={i.to} end={'end' in i}>{i.label}
        {i.key === 'inbox' && inboxCount > 0 && <span className="nav-count" aria-label={`${inboxCount} waiting`}>{inboxCount > 99 ? '99+' : inboxCount}</span>}</NavLink>)}
      <button className="btn ghost sm nav-custom-btn" onClick={() => setEditing(true)}>Customise menu</button>
    </nav>
  )
  return (
    <div className="nav-edit" aria-label="Customise menu">
      <p className="faint" style={{ fontSize: 12, margin: '0 2px 4px' }}>Drag to reorder, or use the arrows. The eye hides an item.</p>
      {items.map((i, n) => (
        <div key={i.key} draggable
          className={`nav-edit-row${drag === i.key ? ' dragging' : ''}${over === i.key && drag !== i.key ? ' over' : ''}${hidden.has(i.key) ? ' hidden-item' : ''}`}
          onDragStart={e => { setDrag(i.key); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', i.key) }}
          onDragOver={e => { e.preventDefault(); setOver(i.key) }}
          onDragLeave={() => setOver(o => (o === i.key ? null : o))}
          onDrop={e => { e.preventDefault(); if (drag) move(drag, n); setDrag(null); setOver(null) }}
          onDragEnd={() => { setDrag(null); setOver(null) }}>
          <span className="grip" aria-hidden>⋮⋮</span>
          <span className="nav-edit-name">{i.label}</span>
          <span className="btns">
            <button onClick={() => move(i.key, n - 1)} disabled={n === 0} aria-label={`Move ${i.label} up`}>▲</button>
            <button onClick={() => move(i.key, n + 1)} disabled={n === items.length - 1} aria-label={`Move ${i.label} down`}>▼</button>
            <button onClick={() => toggle(i.key)} aria-pressed={hidden.has(i.key)} aria-label={`${hidden.has(i.key) ? 'Show' : 'Hide'} ${i.label}`}
              title={hidden.has(i.key) ? 'Show in the menu' : 'Hide from the menu'}><Eye off={hidden.has(i.key)} /></button>
          </span>
        </div>
      ))}
      <div className="nav-edit-foot">
        <button className="btn ghost sm" onClick={() => save([], [])}>Reset</button>
        <button className="btn primary sm" onClick={() => setEditing(false)}>Done</button>
      </div>
    </div>
  )
}
