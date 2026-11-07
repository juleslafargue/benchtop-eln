import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

/** Where "Back" goes when there's no earlier page in this tab (a bookmark, a new tab, a pasted link). */
interface Target { to: string; label: string }
const Ctx = createContext<{ override: Target | null; setOverride: (t: Target | null) => void }>({ override: null, setOverride: () => {} })

export function BackProvider({ children }: { children: ReactNode }) {
  const [override, setOverride] = useState<Target | null>(null)
  return <Ctx.Provider value={{ override, setOverride }}>{children}</Ctx.Provider>
}

/** A page can name its logical parent, e.g. an experiment's notebook. */
export function useBackTarget(to: string | null, label: string) {
  const { setOverride } = useContext(Ctx)
  useEffect(() => {
    if (!to) return
    setOverride({ to, label })
    return () => setOverride(null)
  }, [to, label, setOverride])
}

function parentOf(path: string): Target {
  if (/^\/materials\/\d+/.test(path)) return { to: '/materials', label: 'Materials' }
  if (/^\/si\/\d+/.test(path)) return { to: '/si', label: 'Supporting information' }
  if (/^\/experiments\/\d+/.test(path)) return { to: '/', label: 'Dashboard' }
  return { to: '/', label: 'Dashboard' }
}

export default function BackButton() {
  const nav = useNavigate()
  const { pathname } = useLocation()
  const { override } = useContext(Ctx)
  if (pathname === '/') return null
  // React Router keeps the position in this tab's history; 0 means this is the first page we loaded
  const canGoBack = ((window.history.state as { idx?: number } | null)?.idx ?? 0) > 0
  const fallback = override || parentOf(pathname)
  return (
    <button className="back-btn" onClick={() => (canGoBack ? nav(-1) : nav(fallback.to))}
      title={canGoBack ? 'Back to the previous page (Alt+← or ⌘[ also work)' : `Go to ${fallback.label}`}>
      <span aria-hidden>←</span> {canGoBack ? 'Back' : fallback.label}
    </button>
  )
}
