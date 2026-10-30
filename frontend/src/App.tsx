import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import TitleBar from './components/TitleBar'
import { PictureViewer } from './components/editorExtras'
import { applyPalette, syncThemeColor } from './theme'
import ErrorBoundary from './components/ErrorBoundary'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { api, getToken, setToken, setUnauthorizedHandler, type Group, type Notebook, type User } from './api'
import Layout from './components/Layout'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import NotebookPage from './pages/NotebookPage'
import ExperimentPage from './pages/ExperimentPage'
import Materials from './pages/Materials'
import MaterialDetail from './pages/MaterialDetail'
import Inventory from './pages/Inventory'
import Search from './pages/Search'
import Reactions from './pages/Reactions'
import Compounds from './pages/Compounds'
import Templates from './pages/Templates'
import Admin from './pages/Admin'
import Account from './pages/Account'
import Groups from './pages/Groups'
import Inbox from './pages/Inbox'
import PasswordDialog from './components/PasswordDialog'
import BenchPage from './pages/BenchPage'
import Guide from './pages/Guide'
import { Shortcuts } from './components/Shortcuts'

export interface ToastAction { label: string; run: () => void }
interface Ctx {
  user: User
  /** the notebooks this person can open, archived ones left out (the sidebar's list, and where new experiments can go) */
  notebooks: Notebook[]
  /** archived notebooks: out of the main list, found under Archived at the bottom of it */
  archivedNotebooks: Notebook[]
  refreshNotebooks: () => Promise<void>
  groups: Group[]
  refreshGroups: () => Promise<void>
  toast: (msg: string, action?: ToastAction) => void
  logout: () => void
  canWrite: boolean
  setUser: (u: User) => void
}
const AppCtx = createContext<Ctx>(null as unknown as Ctx)
export const useApp = () => useContext(AppCtx)

export default function App() {
  const [user, setUser] = useState<User | null>(null)
  const [booting, setBooting] = useState(!!getToken())
  const [handoffErr, setHandoffErr] = useState('')
  const loc = useLocation()
  const [notebooks, setNotebooks] = useState<Notebook[]>([])
  const [archivedNotebooks, setArchivedNotebooks] = useState<Notebook[]>([])
  const [groups, setGroups] = useState<Group[]>([])
  const [toastMsg, setToastMsg] = useState<{ msg: string; action?: ToastAction } | null>(null)

  const logout = useCallback(() => {
    if (getToken()) api.post('/api/auth/logout').catch(() => {})  // end the session on the server too
    setToken(null); setUser(null)
  }, [])
  useEffect(() => setUnauthorizedHandler(logout), [logout])

  useEffect(() => {
    // coming back from single sign-on: the token arrives in the URL fragment (never sent to a server)
    const m = /[#&]sso=([^&]+)/.exec(location.hash)
    if (m) {
      setToken(decodeURIComponent(m[1]))
      if (/welcome=1/.test(location.hash)) sessionStorage.setItem('benchtop.welcome', '1')
      history.replaceState(null, '', location.pathname + location.search)
      setBooting(true)
    }
    const h = /[#&]handoff=([^&]+)/.exec(location.hash)
    if (h) {
      history.replaceState(null, '', location.pathname + location.search)
      setBooting(true)
      api.post<{ token: string; user: User }>('/api/auth/handoff', { code: decodeURIComponent(h[1]) })
        .then(r => { setToken(r.token); setUser(r.user) })
        .catch(e => { setHandoffErr(e.message); if (getToken()) api.get<User>('/api/auth/me').then(setUser).catch(() => setToken(null)) })
        .finally(() => setBooting(false))
      return
    }
    if (!getToken()) return
    api.get<User>('/api/auth/me').then(setUser).catch(() => setToken(null)).finally(() => setBooting(false))
  }, [])

  const refreshNotebooks = useCallback(async () => {
    const all = await api.get<Notebook[]>('/api/notebooks?include_archived=true')
    setNotebooks(all.filter(n => !n.archived)); setArchivedNotebooks(all.filter(n => n.archived))
  }, [])
  const refreshGroups = useCallback(async () => {
    try { setGroups(await api.get<Group[]>('/api/groups')) } catch { /* older server */ }
  }, [])
  useEffect(() => { if (user && !user.must_change_password) { refreshNotebooks(); refreshGroups() } }, [user?.id, user?.must_change_password, refreshNotebooks, refreshGroups]) // eslint-disable-line react-hooks/exhaustive-deps

  const toast = useCallback((msg: string, action?: ToastAction) => {
    const t = { msg, action }
    setToastMsg(t)
    window.setTimeout(() => setToastMsg(m => (m === t ? null : m)), action ? 7000 : 2800)
  }, [])
  const toastEl = toastMsg && <div className="toast" role="status">{toastMsg.msg}
    {toastMsg.action && <button className="toast-action" onClick={() => { toastMsg.action!.run(); setToastMsg(null) }}>{toastMsg.action.label}</button>}</div>

  useEffect(() => {
    const t = user?.preferences?.theme
    if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t
    else delete document.documentElement.dataset.theme
    syncThemeColor()
  }, [user?.preferences?.theme])
  useEffect(() => { if (user) applyPalette(user.preferences?.palette ?? null) }, [user, user?.preferences?.palette])

  if (booting) return null
  if (!user) return <>{handoffErr && <div className="handoff-err" role="alert">{handoffErr}</div>}<Login onLogin={(t, u) => { setToken(t); setUser(u) }} /></>

  // an admin set this password: choose your own before anything else loads
  if (user.must_change_password) return (
    <AppCtx.Provider value={{ user, notebooks, archivedNotebooks, refreshNotebooks, groups, refreshGroups, toast, logout, canWrite: user.role !== 'viewer', setUser }}>
      <div className="login" />
      <PasswordDialog onClose={() => {}} />
      {toastEl}
    </AppCtx.Provider>
  )

  return (
    <AppCtx.Provider value={{ user, notebooks, archivedNotebooks, refreshNotebooks, groups, refreshGroups, toast, logout, canWrite: user.role !== 'viewer', setUser }}>
      {loc.pathname.startsWith('/bench/') ? <ErrorBoundary page label="the bench view" resetKey={loc.pathname}><Routes><Route path="/bench/:id" element={<BenchPage />} /></Routes></ErrorBoundary> : <>
      <Layout>
        <ErrorBoundary page label="this page" resetKey={loc.pathname}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/notebooks/:id" element={<NotebookPage />} />
          <Route path="/experiments/:id" element={<ExperimentPage />} />
          <Route path="/templates" element={<Templates />} />
          <Route path="/materials" element={<Materials />} />
          <Route path="/materials/:id" element={<MaterialDetail />} />
          <Route path="/inventory" element={<Inventory />} />
          <Route path="/search" element={<Search />} />
          <Route path="/reactions" element={<Reactions />} />
          <Route path="/compounds" element={<Compounds />} />
          <Route path="/account" element={<Account />} />
          <Route path="/groups" element={<Groups />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/guide" element={<Guide />} />
          <Route path="/admin/:tab?" element={user.role === 'admin' ? <Admin /> : <Navigate to="/" />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
        </ErrorBoundary>
      </Layout>
      <Shortcuts />
      </>}
      <TitleBar />
      <PictureViewer />
      {toastEl}
    </AppCtx.Provider>
  )
}
