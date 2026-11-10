import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import GridLayout, { WidthProvider, type Layout } from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import 'react-resizable/css/styles.css'
import { useApp } from '../App'
import { unitLabel, day, api, displayName, fmt, when, type ExperimentSummary, type User, type UserBrief } from '../api'
import { StatusDot } from '../components/ExperimentList'
import NewExperimentDialog from '../components/NewExperimentDialog'
import NewNotebookDialog from '../components/NewNotebookDialog'
import YieldChart, { type YieldPoint } from '../components/YieldChart'

const Grid = WidthProvider(GridLayout)

type ListKind = 'recent' | 'open' | 'awaiting' | 'returned' | 'pinned' | 'stale' | 'waiting'
type WType = 'list' | 'actions' | 'note' | 'header' | 'yield' | 'month' | 'spectra' | 'inventory'
export interface Widget { i: string; type: WType; x: number; y: number; w: number; h: number; list?: ListKind; text?: string; title?: string }

type DashItem = ExperimentSummary & { reason?: string; returned_by?: string; assigned_to_me?: boolean; overdue?: boolean; witness?: UserBrief | null; asked_at?: string | null }
interface DashData {
  // could_witness: signed records nobody was asked to witness, shown apart under "Asked to witness"
  lists: Record<ListKind, DashItem[]> & { could_witness?: DashItem[] }
  // how many there are in all (0.30.1); the lists stop at 10. Older servers don't send it: then the lists' lengths
  totals?: Partial<Record<ListKind | 'could_witness', number>>
  stats: { this_month: number; last_month: number; witnessed_this_month: number; yields: YieldPoint[] }
  spectra: { id: number; filename: string; experiment_id: number; eid: string; title: string; created_at: string }[]
  stale_days?: number
  system?: { backup_warnings: string[]; check_warnings?: string[]; disk_warnings?: string[]; update?: UpdateInfo | null }
  inventory: { low: InvRow[]; expiring: (InvRow & { expires_at: string; expired: boolean })[]; safety_changes?: { material_id: number | null; material: string; changed_at: string; change: { added: string[]; removed: string[] } | null }[]; low_fraction: number; expiry_days: number }
}
interface UpdateInfo { current: string; available: { version: string; name: string; url: string }; versions_behind: number; command: string; where: string }
interface InvRow { lot_id: number; barcode: string; material_id: number; material: string; amount: number; unit: string; location: string; fraction?: number }

const LIST_TITLES: Record<ListKind, string> = {
  recent: 'Recently edited', open: 'My open experiments', awaiting: 'Asked to witness',
  returned: 'Returned to me', pinned: 'Pinned', stale: 'Drafts gone quiet', waiting: 'My records waiting for a witness',
}
const LIST_EMPTY: Record<ListKind, string> = {
  recent: 'Nothing yet. Start an experiment.', open: 'No open experiments.', awaiting: 'Nobody has asked you to witness a record.',
  returned: 'Nothing returned. Good.', pinned: 'Pin experiments from their page to keep them here.', stale: 'No drafts left untouched for two weeks.',
  waiting: 'None of your records is waiting for a witness.',
}
const CATALOG: { type: WType; list?: ListKind; label: string; w: number; h: number }[] = [
  { type: 'actions', label: 'Quick actions', w: 4, h: 4 },
  { type: 'list', list: 'awaiting', label: 'Asked to witness', w: 4, h: 4 },
  { type: 'list', list: 'returned', label: 'Returned to me', w: 4, h: 4 },
  { type: 'list', list: 'waiting', label: 'My records waiting for a witness', w: 4, h: 4 },
  { type: 'list', list: 'open', label: 'My open experiments', w: 4, h: 5 },
  { type: 'list', list: 'recent', label: 'Recently edited', w: 6, h: 5 },
  { type: 'list', list: 'pinned', label: 'Pinned experiments', w: 6, h: 4 },
  { type: 'list', list: 'stale', label: 'Drafts gone quiet (untouched for 2+ weeks)', w: 4, h: 4 },
  { type: 'yield', label: 'Yield trend', w: 8, h: 7 },
  { type: 'month', label: 'This month', w: 4, h: 3 },
  { type: 'spectra', label: 'Recent spectra', w: 6, h: 5 },
  { type: 'inventory', label: 'Inventory alerts', w: 4, h: 4 },
  { type: 'note', label: 'Note', w: 4, h: 4 },
  { type: 'header', label: 'Section heading', w: 12, h: 2 },
]
// 0.35: your own records stay in view after signing ("waiting" and "recent"); a layout someone saved is kept as it is
export const DEFAULT_WIDGETS: Widget[] = [
  { i: 'actions', type: 'actions', x: 0, y: 0, w: 4, h: 4 },
  { i: 'awaiting', type: 'list', list: 'awaiting', x: 4, y: 0, w: 4, h: 4 },
  { i: 'returned', type: 'list', list: 'returned', x: 8, y: 0, w: 4, h: 4 },
  { i: 'open', type: 'list', list: 'open', x: 0, y: 4, w: 4, h: 5 },
  { i: 'waiting', type: 'list', list: 'waiting', x: 4, y: 4, w: 4, h: 5 },
  { i: 'recent', type: 'list', list: 'recent', x: 8, y: 4, w: 4, h: 5 },
  { i: 'yield', type: 'yield', x: 0, y: 9, w: 8, h: 7 },
  { i: 'month', type: 'month', x: 8, y: 9, w: 4, h: 3 },
  { i: 'inventory', type: 'inventory', x: 8, y: 12, w: 4, h: 4 },
  { i: 'spectra', type: 'spectra', x: 0, y: 16, w: 6, h: 5 },
]

/** Charts and lab-wide lists with nothing in them yet: on a first day they wait until there's something to show. */
export function emptyChart(w: Pick<Widget, 'type'>, d: Pick<DashData, 'stats' | 'spectra' | 'inventory'>): boolean {
  if (w.type === 'yield') return !d.stats.yields.length
  if (w.type === 'month') return !d.stats.this_month && !d.stats.last_month
  if (w.type === 'spectra') return !d.spectra.length
  if (w.type === 'inventory') return !d.inventory.low.length && !d.inventory.expiring.length && !(d.inventory.safety_changes || []).length
  return false
}

/** 0.35: someone with no records yet gets the four steps to their first one; an admin who is still alone here, where
 *  to add their group. Put away with Hide, for good (saved to the account). */
export function FirstSteps({ firstRecord, alone, canWrite, onNew, onHide }: {
  firstRecord: boolean; alone: boolean; canWrite: boolean; onNew: () => void; onHide: () => void
}) {
  if (!(firstRecord && canWrite) && !alone) return null
  return (
    <section className="panel panel-pad first-steps" aria-labelledby="first-steps-h">
      <div className="spread" style={{ alignItems: 'baseline' }}>
        <h2 id="first-steps-h">{firstRecord && canWrite ? 'Your first record' : 'Add your group'}</h2>
        <button className="btn ghost sm" onClick={onHide}>Hide</button>
      </div>
      {firstRecord && canWrite && <>
        <ol className="first-steps-list">
          <li><b>A notebook to keep it in.</b> If you don't have one yet, New experiment makes one for you, named after you and
            shared with your group if you're in one.</li>
          <li><b>New experiment.</b> Give it a title. It gets a number, such as EXP-00001, that stays with it.</li>
          <li><b>Write the reaction and the procedure.</b> Draw or paste the reaction and the table works out the amounts.
            Everything saves as you type.</li>
          <li><b>Sign it and choose a witness.</b> When it's done, press Sign as author and pick someone in your group to witness it.</li>
        </ol>
        <div className="row" style={{ gap: 10 }}>
          <button className="btn primary" onClick={onNew}>New experiment</button>
          <Link to="/guide">More in Getting started</Link>
        </div>
      </>}
      {alone && <div className={firstRecord && canWrite ? 'first-steps-admin' : ''}>
        {firstRecord && canWrite && <h3>Add your group</h3>}
        <p style={{ margin: '0 0 8px' }}>You're the only person in this Benchtop so far. Add the people in your lab, and put
          them in a group so they can see and witness each other's records.</p>
        <Link className="btn" to="/admin/users">Add people in Admin → Users</Link>
      </div>}
    </section>
  )
}

/** How many there are in all, where the server says (its lists stop at 10). */
export const totalOf = (d: Pick<DashData, 'lists' | 'totals'>, k: ListKind | 'could_witness') =>
  d.totals?.[k] ?? (d.lists[k]?.length || 0)

function ExpList({ items, empty, kind, also, total, alsoTotal }: { items: DashItem[]; empty: string; kind: ListKind; also?: DashItem[]; total?: number; alsoTotal?: number }) {
  if (also?.length) return <>
    {items.length ? <ExpList items={items} empty={empty} kind={kind} total={total} /> : <p className="w-empty">{empty}</p>}
    <p className="w-sub">Others you could witness <span className="faint">· nobody was asked</span></p>
    <ExpList items={also} empty="" kind={kind} total={alsoTotal} />
  </>
  if (!items.length) return <p className="w-empty">{empty}</p>
  const more = (total ?? items.length) - items.length
  return (
    <ul className="w-list">
      {items.map(e => (
        <li key={e.id}><Link to={`/experiments/${e.id}`}>
          <span className="eid">{e.eid}</span>
          <span className="w-main"><span className="w-title">{e.title}</span>
            <span className="faint">{kind === 'returned' && e.reason ? `“${e.reason}” · ${e.returned_by || ''}`
              : kind === 'awaiting' ? `by ${displayName(e.owner)} · ${when(e.updated_at)}`
                : kind === 'waiting' ? (e.witness ? `asked ${displayName(e.witness)} · ${when(e.asked_at || e.updated_at)}` : `anyone can witness · ${when(e.updated_at)}`)
                  : `${e.notebook_name || ''} · ${when(e.updated_at)}`}
              {e.overdue && <span className="w-badge overdue">overdue</span>}</span></span>
          <StatusDot status={e.status} />
        </Link></li>
      ))}
      {more > 0 && <li className="w-more faint">and {more.toLocaleString()} more</li>}
    </ul>
  )
}

function Note({ w, editing, onText }: { w: Widget; editing: boolean; onText: (t: string) => void }) {
  const [text, setText] = useState(w.text || '')
  const t = useRef<number>()
  useEffect(() => setText(w.text || ''), [w.text])
  return <textarea className="w-note" value={text} placeholder="Write a note: reagents to order, ideas, reminders…" disabled={editing}
    onChange={e => { setText(e.target.value); window.clearTimeout(t.current); const v = e.target.value; t.current = window.setTimeout(() => onText(v), 700) }} />
}

export default function Dashboard() {
  const { user, setUser, canWrite, toast } = useApp()
  const nav = useNavigate()
  const [data, setData] = useState<DashData | null>(null)
  const [quietDismissed, setQuietDismissed] = useState(() => { try { return !!sessionStorage.getItem('benchtop.quiet-dismissed') } catch { return false } })
  const [editing, setEditing] = useState(false)
  const saved = (user.preferences as any)?.dashboard?.widgets as Widget[] | undefined
  const [widgets, setWidgets] = useState<Widget[]>(saved?.length ? saved : DEFAULT_WIDGETS)
  const [adding, setAdding] = useState(false)
  const [newExp, setNewExp] = useState(false)
  const [newNb, setNewNb] = useState(false)
  const [narrow, setNarrow] = useState(() => window.innerWidth < 760)
  const [stepsGone, setStepsGone] = useState(false)
  const [alone, setAlone] = useState(false)
  const stepsHidden = !!user.preferences?.first_steps_dismissed || stepsGone
  useEffect(() => {  // an admin with nobody else here yet (the retired built-in admin doesn't count)
    if (user.role !== 'admin' || stepsHidden) return
    api.get<{ id: number; username: string; active: boolean }[]>('/api/users')
      .then(us => setAlone(!us.some(u => u.id !== user.id && u.active && u.username !== 'admin'))).catch(() => {})
  }, [user.role, user.id, stepsHidden])
  const hideSteps = async () => {
    setStepsGone(true)
    try { setUser(await api.patch<User>('/api/auth/me', { preferences: { first_steps_dismissed: true } })) } catch (e: any) { setStepsGone(false); toast(e.message) }
  }

  useEffect(() => { api.get<DashData>('/api/dashboard').then(setData).catch(e => toast(e.message)) }, [toast])
  useEffect(() => { const f = () => setNarrow(window.innerWidth < 760); window.addEventListener('resize', f); return () => window.removeEventListener('resize', f) }, [])

  // "New to Benchtop?": shown until the person dismisses it, then never again, on any computer (saved to the account)
  const [welcomeGone, setWelcomeGone] = useState(false)
  const showWelcome = !user.preferences?.guide_dismissed && !welcomeGone
  const dismissWelcome = async () => {
    setWelcomeGone(true)
    try { setUser(await api.patch<User>('/api/auth/me', { preferences: { guide_dismissed: true } })) } catch (e: any) { setWelcomeGone(false); toast(e.message) }  // back, to dismiss again
  }

  const persist = useCallback(async (ws: Widget[]) => {
    try { setUser(await api.patch<User>('/api/auth/me', { preferences: { dashboard: { widgets: ws } } })) } catch (e: any) { toast(e.message) }
  }, [setUser, toast])

  const onLayout = (l: Layout[]) => setWidgets(ws => ws.map(w => { const p = l.find(x => x.i === w.i); return p ? { ...w, x: p.x, y: p.y, w: p.w, h: p.h } : w }))
  const add = (c: typeof CATALOG[number]) => {
    const bottom = widgets.reduce((m, w) => Math.max(m, w.y + w.h), 0)
    setWidgets(ws => [...ws, { i: `${c.type}-${Date.now().toString(36)}`, type: c.type, list: c.list, x: 0, y: bottom, w: c.w, h: c.h, ...(c.type === 'header' ? { text: 'Section' } : {}) }])
    setAdding(false)
  }
  const setText = (i: string, text: string) => {
    const next = widgets.map(w => (w.i === i ? { ...w, text } : w))
    setWidgets(next)
    if (!editing) persist(next)
  }
  const greeting = useMemo(() => { const h = new Date().getHours(); return h < 5 ? 'Working late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening' }, [])

  const body = (w: Widget) => {
    if (!data) return <p className="w-empty">Loading…</p>
    switch (w.type) {
      case 'list': { const k = w.list || 'recent'; return <ExpList kind={k} items={data.lists[k]} empty={LIST_EMPTY[k]} total={totalOf(data, k)}
        also={k === 'awaiting' ? data.lists.could_witness : undefined} alsoTotal={totalOf(data, 'could_witness')} /> }
      case 'actions': return (
        <div className="w-actions">
          {canWrite && <button className="btn primary" onClick={() => setNewExp(true)}>New experiment <kbd>N</kbd></button>}
          {canWrite && <button className="btn" onClick={() => setNewNb(true)}>New notebook</button>}
          {canWrite && <button className="btn" onClick={() => nav('/materials?new=1')}>Register material</button>}
          <button className="btn" onClick={() => nav('/search?mode=substructure')}>Structure search</button>
          <button className="btn" onClick={() => nav('/reactions')}>All reactions</button>
        </div>
      )
      case 'note': return <Note w={w} editing={editing} onText={t => setText(w.i, t)} />
      case 'header': return editing
        ? <input className="input w-header-in" value={w.text || ''} onChange={e => setWidgets(ws => ws.map(x => (x.i === w.i ? { ...x, text: e.target.value } : x)))} aria-label="Heading text" />
        : <h2 className="w-header">{w.text || 'Section'}</h2>
      case 'yield': return <>
        <YieldChart points={data.stats.yields} />
        <Link className="faint w-foot" to="/reactions">Open as a table in Reactions</Link>
      </>
      case 'month': {
        const d = data.stats.this_month - data.stats.last_month
        return (
          <div className="w-month">
            <div><span className="hero">{data.stats.this_month}</span><span className="faint"> experiments started</span></div>
            <div className="faint">{d === 0 ? 'Same as' : `${Math.abs(d)} ${d > 0 ? 'more' : 'fewer'} than`} last month ({data.stats.last_month}) · {data.stats.witnessed_this_month} witnessed</div>
          </div>
        )
      }
      case 'spectra': return data.spectra.length ? (
        <ul className="w-list">{data.spectra.map(s => <li key={s.id}><Link to={`/experiments/${s.experiment_id}`}>
          <span className="w-main"><span className="w-title">{s.filename}</span><span className="faint">{s.eid} · {s.title}</span></span>
          <span className="faint">{day(s.created_at)}</span></Link></li>)}</ul>
      ) : <p className="w-empty">Drop spectra onto an experiment and they'll be listed here.</p>
      case 'inventory': {
        const { low, expiring } = data.inventory
        const changes = data.inventory.safety_changes || []
        if (!low.length && !expiring.length && !changes.length) return <p className="w-empty">Nothing running low or expiring in the next {data.inventory.expiry_days} days.</p>
        return (
          <ul className="w-list">
            {changes.map((c, k) => <li key={`s${k}`}><Link to={c.material_id ? `/materials/${c.material_id}` : '/materials'}>
              <span className="w-flag bad">Safety</span>
              <span className="w-main"><span className="w-title">{c.material}: classification changed</span>
                <span className="faint">{[c.change?.added?.length ? `now ${c.change.added.join(', ')}` : '', c.change?.removed?.length ? `dropped ${c.change.removed.join(', ')}` : ''].filter(Boolean).join(' · ') || 'signal word or pictograms changed'} · {day(c.changed_at)}</span></span></Link></li>)}
            {low.map(r => <li key={`l${r.lot_id}`}><Link to={`/materials/${r.material_id}`}>
              <span className="w-flag warn">Low</span>
              <span className="w-main"><span className="w-title">{r.material}</span><span className="faint">{r.barcode} · {fmt(r.amount, 3)} {unitLabel(r.unit)} left ({Math.round((r.fraction || 0) * 100)}%)</span></span></Link></li>)}
            {expiring.map(r => <li key={`e${r.lot_id}`}><Link to={`/materials/${r.material_id}`}>
              <span className={`w-flag ${r.expired ? 'bad' : 'warn'}`}>{r.expired ? 'Expired' : 'Expires'}</span>
              <span className="w-main"><span className="w-title">{r.material}</span><span className="faint">{r.barcode} · {day(r.expires_at)}</span></span></Link></li>)}
          </ul>
        )
      }
    }
  }
  const title = (w: Widget) => w.title || (w.type === 'list' ? LIST_TITLES[w.list || 'recent'] : ({ actions: 'Quick actions', note: 'Note', header: '', yield: 'Yield trend', month: 'This month', spectra: 'Recent spectra', inventory: 'Inventory alerts' } as const)[w.type as Exclude<WType, 'list'>])
  const count = (w: Widget) => (w.type === 'list' && data ? totalOf(data, w.list || 'recent') : null)

  // on a first day (no records of your own yet), empty charts wait until there's data; while customising, all show
  const firstDay = !!data && !data.lists.recent.length
  const shownWidgets = firstDay && !editing ? widgets.filter(w => !emptyChart(w, data!)) : widgets
  const card = (w: Widget) => (
    <div key={w.i} className={`widget wt-${w.type}${editing ? ' editing' : ''}`}>
      {w.type !== 'header' && <div className="w-head">
        <span className="w-drag">{title(w)}{count(w) ? <span className="w-count">{count(w)}</span> : null}</span>
        {editing && <button className="btn ghost sm w-remove" onMouseDown={e => e.stopPropagation()} onClick={() => setWidgets(ws => ws.filter(x => x.i !== w.i))} aria-label={`Remove ${title(w)}`}>Remove</button>}
      </div>}
      {w.type === 'header' && editing && <button className="btn ghost sm w-remove w-remove-float" onMouseDown={e => e.stopPropagation()} onClick={() => setWidgets(ws => ws.filter(x => x.i !== w.i))}>Remove</button>}
      <div className="w-body">{body(w)}</div>
    </div>
  )

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="spread">
        <div><h1 className="page-title">{greeting}, {user.full_name.split(' ')[0] || user.username}</h1>
          {data && <p className="lede" style={{ margin: 0 }}>{totalOf(data, 'awaiting').toLocaleString()} you've been asked to witness · {totalOf(data, 'returned').toLocaleString()} returned to you · {totalOf(data, 'open').toLocaleString()} open</p>}</div>
        {!narrow && (editing ? (
          <div className="row">
            <button className="btn" onClick={() => setAdding(true)}>Add widget</button>
            <button className="btn ghost" onClick={() => setWidgets(DEFAULT_WIDGETS)}>Reset to default</button>
            <button className="btn primary" onClick={() => { setEditing(false); persist(widgets); toast('Dashboard saved') }}>Done</button>
          </div>
        ) : <button className="btn" onClick={() => setEditing(true)}>Customise</button>)}
      </div>
      {(data?.system?.backup_warnings?.length ?? 0) > 0 && <div className="notice dash-warning" role="status">
        <b>Backups:</b> {data!.system!.backup_warnings.join(' ')} <Link to="/admin/backups">Open Admin → Backups</Link></div>}
      {(data?.system?.check_warnings?.length ?? 0) > 0 && <div className="notice dash-warning" role="alert">
        <b>Daily data check:</b> {data!.system!.check_warnings!.join(' ')} <Link to="/admin/backups">Open Admin → Backups</Link></div>}
      {(data?.system?.disk_warnings?.length ?? 0) > 0 && <div className="notice dash-warning" role="status">
        <b>Disk space:</b> {data!.system!.disk_warnings!.join(' ')} <Link to="/admin/backups">Open Admin → Backups</Link></div>}
      {data?.system?.update && <UpdateNotice info={data.system.update} />}
      {(data?.lists.stale?.length ?? 0) > 0 && !quietDismissed && <div className="notice stale-note" role="status">
        <span><b>{totalOf(data!, 'stale').toLocaleString()} draft{totalOf(data!, 'stale') === 1 ? '' : 's'}</b> untouched for {data!.stale_days ?? 14}+ days: {data!.lists.stale.slice(0, 3).map((e, i) =>
          <span key={e.id}>{i > 0 && ', '}<Link to={`/experiments/${e.id}`}>{e.eid}</Link> <span className="faint">{e.title.length > 40 ? e.title.slice(0, 40) + '…' : e.title}</span></span>)}
          {totalOf(data!, 'stale') > 3 && ` and ${(totalOf(data!, 'stale') - 3).toLocaleString()} more`}. Finish and sign them, or void the ones that went nowhere.</span>
        <button className="btn ghost sm" onClick={() => { setQuietDismissed(true); try { sessionStorage.setItem('benchtop.quiet-dismissed', '1') } catch { /* ok */ } }}>Not now</button>
      </div>}
      {showWelcome && <div className="notice welcome-note" role="status">
        <span><b>New to Benchtop?</b> <Link to="/guide">Getting started</Link> takes you through your first record. It's always in the menu, above your name.</span>
        <button className="btn ghost sm" onClick={dismissWelcome}>Dismiss</button>
      </div>}
      {user.has_password === false && <p className="notice" style={{ margin: 0 }}>
        {!showWelcome && sessionStorage.getItem('benchtop.welcome') ? 'Welcome to Benchtop! ' : ''}You sign in with single sign-on. To sign records you'll also need a signing password: <Link to="/account">set one in Account settings</Link>.</p>}
      {data && !stepsHidden && !editing && <FirstSteps firstRecord={!data.lists.recent.length} alone={alone} canWrite={canWrite}
        onNew={() => setNewExp(true)} onHide={hideSteps} />}
      {editing && <p className="notice" style={{ margin: 0 }}>Drag a widget by its title to move it, drag the bottom-right corner to resize it, and use Remove or Add widget to change what's shown. Click Done to save.</p>}
      {narrow ? (
        <div className="stack dash-stack" style={{ gap: 12 }}>{[...shownWidgets].sort((a, b) => a.y - b.y || a.x - b.x).map(card)}</div>
      ) : (
        <Grid className="dash-grid" layout={shownWidgets.map(w => ({ i: w.i, x: w.x, y: w.y, w: w.w, h: w.h, minW: 2, minH: 2 }))}
          cols={12} rowHeight={44} margin={[14, 14]} containerPadding={[0, 0]} isDraggable={editing} isResizable={editing}
          draggableHandle=".w-drag, .w-header, .w-remove-float" onLayoutChange={onLayout} compactType="vertical">
          {shownWidgets.map(card)}
        </Grid>
      )}
      {adding && (
        <div className="palette-backdrop" onMouseDown={() => setAdding(false)}>
          <div className="palette" onMouseDown={e => e.stopPropagation()} role="dialog" aria-label="Add a widget">
            <div className="palette-input" style={{ fontWeight: 600 }}>Add a widget</div>
            <ul className="palette-list">
              {CATALOG.map(c => <li key={c.label} role="option" aria-selected={false} onClick={() => add(c)}>
                <span className="palette-group">{c.type === 'list' ? 'List' : c.type === 'yield' || c.type === 'month' ? 'Stats' : c.type === 'inventory' || c.type === 'spectra' ? 'Lab' : 'Tools'}</span>
                <span className="palette-label">{c.label}</span>
                <span className="faint">{widgets.some(w => w.type === c.type && w.list === c.list && !['note', 'header'].includes(c.type)) ? 'on dashboard' : ''}</span>
              </li>)}
            </ul>
          </div>
        </div>
      )}
      {newExp && <NewExperimentDialog onClose={() => setNewExp(false)} />}
      {newNb && <NewNotebookDialog onClose={() => setNewNb(false)} />}
    </div>
  )
}


// The admin's "a newer Benchtop is out" line. Benchtop never updates itself: it says how, and the command backs up first.
// "Not now" hides it until the next version comes out (remembered in this browser only).
function UpdateNotice({ info }: { info: UpdateInfo }) {
  const key = 'benchtop.update-hidden'
  const [hidden, setHidden] = useState(() => { try { return localStorage.getItem(key) === info.available.version } catch { return false } })
  if (hidden) return null
  const v = info.available.version
  return <div className="notice update-note" role="status">
    <span><b>Benchtop {v} is available</b> (you have {info.current}{info.versions_behind > 1 ? `; ${info.versions_behind} versions behind` : ''}).{' '}
      {info.available.url && <><a href={info.available.url} target="_blank" rel="noreferrer">What's new</a>. </>}
      {info.command
        ? <>To update, {info.where}, run <code>{info.command}</code>. It makes a backup first and can be undone.</>
        : <>The README explains how to update; it keeps your data.</>}</span>
    <button className="btn ghost sm" onClick={() => { setHidden(true); try { localStorage.setItem(key, v) } catch { /* ok */ } }}>Not now</button>
  </div>
}
