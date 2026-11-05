import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApp } from '../App'
import { ago, api, displayName, downloadAuthed, type InboxItem } from '../api'
import CutShort from '../components/CutShort'
import FollowUpDialog from '../components/FollowUpDialog'
import { addPage } from '../listNotice'

type Tab = 'inbox' | 'attached' | 'ignored'
interface Target { id: number; eid: string; title: string; mine: boolean; owner: { full_name: string; username: string } | null }

const size = (n: number) => (n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n > 1e3 ? `${(n / 1e3).toFixed(0)} kB` : `${n} B`)

/** Pick a draft to put a file in: your recent drafts first, or search by number or title. */
function AssignPicker({ item, onDone, onCancel }: { item: InboxItem; onDone: () => void; onCancel: () => void }) {
  const { toast } = useApp()
  const [q, setQ] = useState('')
  const [targets, setTargets] = useState<Target[] | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  useEffect(() => {
    const t = setTimeout(() => api.get<Target[]>(`/api/inbox/targets?q=${encodeURIComponent(q)}`).then(setTargets).catch(() => setTargets([])), q ? 250 : 0)
    return () => clearTimeout(t)
  }, [q])
  const assign = async (t: Target) => {
    setBusy(t.id)
    try { await api.post(`/api/inbox/${item.id}/assign`, { experiment_id: t.id }); toast(`Attached to ${t.eid}`); onDone() }
    catch (e: any) { toast(e.message) } finally { setBusy(null) }
  }
  return (
    <div className="assign-picker">
      <div className="row" style={{ gap: 6 }}>
        <input className="input grow" autoFocus placeholder="Search your drafts by number or title" value={q} onChange={e => setQ(e.target.value)} aria-label="Search drafts" />
        <button className="btn ghost sm" onClick={onCancel}>Cancel</button>
      </div>
      {targets === null ? <p className="faint" style={{ margin: 0 }}>Loading…</p>
        : targets.length === 0 ? <p className="faint" style={{ margin: 0 }}>{q ? 'No draft you can edit matches that.' : 'You have no drafts you can add files to. Signed records take files through a follow-up.'}</p>
          : <ul className="assign-list">{targets.map(t => (
            <li key={t.id}><button disabled={busy !== null} onClick={() => assign(t)}>
              <span className="eid">{t.eid}</span> <span className="grow">{t.title}</span>
              {!t.mine && <span className="faint">{displayName(t.owner as any)}</span>}
              <span className="go">{busy === t.id ? 'Attaching…' : 'Attach here'}</span>
            </button></li>))}</ul>}
    </div>
  )
}

function ItemCard({ item, tab, reload }: { item: InboxItem; tab: Tab; reload: () => void }) {
  const { canWrite, toast } = useApp()
  const [assigning, setAssigning] = useState(false)
  const [following, setFollowing] = useState(false)
  const act = async (what: 'ignore' | 'unignore') => {
    try { await api.post(`/api/inbox/${item.id}/${what}`); toast(what === 'ignore' ? 'Ignored; it stays under Ignored' : 'Back in the Inbox'); reload() }
    catch (e: any) { toast(e.message) }
  }
  const orig = item.matched.length === 1 ? item.matched[0] : null
  return (
    <li className={`panel inbox-item ${item.state}`}>
      <div className="inbox-top">
        <div style={{ minWidth: 0 }}>
          <div className="inbox-path">{item.path}{item.revision > 1 && <span className="tag" style={{ marginLeft: 6 }}>changed · version {item.revision}</span>}</div>
          <div className="faint inbox-meta">{item.instrument.name} · {tab === 'attached' && item.handled_at ? `attached ${ago(item.handled_at)}` : `arrived ${ago(item.arrived_at)}`} · {size(item.size)}
            {item.handled_by && tab !== 'inbox' && <> · by {displayName(item.handled_by)}</>}</div>
        </div>
        {item.for_you && <span className="pill">Your sample code</span>}
      </div>
      {tab === 'attached' && item.experiment
        ? <p style={{ margin: 0 }}>In <Link to={`/experiments/${item.experiment.id}`}><b>{item.experiment.eid}</b> {item.experiment.title}</Link></p>
        : item.note && <p className="inbox-note">{item.note}</p>}
      {item.files.length > 0 && <ul className="inbox-files">{item.files.map(f => (
        <li key={f.n}><span className="grow">{f.name} <span className="faint">{size(f.size)}</span></span>
          <button className="btn ghost sm" onClick={() => downloadAuthed(`/api/inbox/${item.id}/files/${f.n}`, f.name).catch(e => toast(e.message))}>Download</button></li>))}</ul>}
      {canWrite && tab !== 'attached' && !assigning && <div className="row inbox-actions">
        {item.follow_up && orig && <button className="btn primary sm" onClick={() => setFollowing(true)}>Attach to a new follow-up of {orig.eid}</button>}
        {item.files.length > 0 && <button className={`btn sm${item.follow_up ? '' : ' primary'}`} onClick={() => setAssigning(true)}>Assign to…</button>}
        {tab === 'inbox' ? <button className="btn ghost sm" onClick={() => act('ignore')}>Ignore</button>
          : <button className="btn ghost sm" onClick={() => act('unignore')}>Bring back</button>}
      </div>}
      {assigning && <AssignPicker item={item} onDone={() => { setAssigning(false); reload() }} onCancel={() => setAssigning(false)} />}
      {following && orig && <FollowUpDialog eid={orig.eid} title={orig.title} notebookId={orig.notebook_id ?? null}
        url={`/api/inbox/${item.id}/follow-up`} onClose={() => setFollowing(false)} onDone={reload} />}
    </li>
  )
}

const PAGE = 200  // the server's own page size for the Inbox

export default function Inbox() {
  const [tab, setTab] = useState<Tab>('inbox')
  const [items, setItems] = useState<InboxItem[] | null>(null)
  const [total, setTotal] = useState(0)
  const [err, setErr] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  // 0.31: the list comes in pages and says how many there are in all, instead of stopping at 200 without a word.
  // Reloading (every minute, and after each action) fetches as many as are shown, so Show more isn't undone.
  const want = useRef(PAGE)
  const latest = useRef(0)
  const load = useCallback(async () => {
    const my = ++latest.current
    window.dispatchEvent(new Event('benchtop:inbox'))  // the menu's count
    try {
      let rows: InboxItem[] = [], all = 0
      while (rows.length < want.current) {
        const r = await api.getList<InboxItem>(`/api/inbox?state=${tab}&limit=${Math.min(500, want.current - rows.length)}&offset=${rows.length}`)
        rows = addPage(rows, r.rows); all = r.total
        if (!r.rows.length || rows.length >= all) break
      }
      if (my === latest.current) { setItems(rows); setTotal(all); setErr('') }
    } catch (e: any) { if (my === latest.current) setErr(e.message) }
  }, [tab])
  useEffect(() => { want.current = PAGE; setItems(null); load() }, [load])
  const showMore = async () => {
    want.current = (items?.length ?? 0) + PAGE
    setLoadingMore(true)
    await load()
    setLoadingMore(false)
  }
  useEffect(() => { const t = setInterval(load, 60_000); return () => clearInterval(t) }, [load])
  const empty = { inbox: 'Nothing waiting. Files from instrument folders that name an experiment (like EXP-00042) go straight into it; the rest wait here.',
    attached: 'Nothing attached from instrument folders yet.', ignored: 'Nothing ignored.' }[tab]
  return (
    <div className="stack" style={{ gap: 18 }}>
      <div><h1 className="page-title">Inbox</h1>
        <p className="lede">Files from instrument folders that Benchtop couldn't place on its own. It never guesses: assign each one to a draft,
          or attach it to a follow-up when its record is already signed.</p></div>
      <div className="tabs" role="tablist">
        {(['inbox', 'attached', 'ignored'] as Tab[]).map(t => <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
          {{ inbox: 'Waiting', attached: 'Recently attached', ignored: 'Ignored' }[t]}{t === 'inbox' && items && tab === 'inbox' && total > 0 ? ` (${total})` : ''}</button>)}
      </div>
      {err && <p className="error">{err}</p>}
      {items && (items.length === 0 ? <p className="empty">{empty}</p>
        : <ul className="inbox-list">{items.map(i => <ItemCard key={i.id} item={i} tab={tab} reload={load} />)}</ul>)}
      {items && <CutShort shown={items.length} total={total} things="files" busy={loadingMore} onMore={showMore} />}
    </div>
  )
}
