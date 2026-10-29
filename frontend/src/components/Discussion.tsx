import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { useApp } from '../App'
import { ago, api, displayName, when, type Comment, type Entry, type User, type UserBrief } from '../api'
import { initials } from './Layout'
import { rankMentions } from '../mentions'

let peopleCache: Promise<User[]> | null = null
const people = () => (peopleCache ??= api.get<User[]>('/api/users').then(us => us.filter(u => u.active)).catch(() => { peopleCache = null; return [] }))

/** Textarea that offers @mentions as you type. Ctrl/⌘+Enter sends. */
export function Composer({ placeholder, onSend, autoFocus, initial = '', submitLabel = 'Comment', onCancel }: {
  placeholder: string; onSend: (body: string) => Promise<boolean>; autoFocus?: boolean; initial?: string; submitLabel?: string; onCancel?: () => void
}) {
  const [text, setText] = useState(initial)
  const [users, setUsers] = useState<User[]>([])
  const [menu, setMenu] = useState<{ q: string; start: number } | null>(null)
  const [active, setActive] = useState(0)
  const [busy, setBusy] = useState(false)
  const ta = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { people().then(setUsers) }, [])
  const matches = menu ? rankMentions(users, menu.q) : []

  const update = (v: string, caret: number) => {
    setText(v)
    const m = /(^|[^\w@])@([\w.-]*)$/.exec(v.slice(0, caret))
    setMenu(m ? { q: m[2], start: caret - m[2].length - 1 } : null); setActive(0)
  }
  const pick = (u: User) => {
    if (!menu || !ta.current) return
    const caret = ta.current.selectionStart
    const v = `${text.slice(0, menu.start)}@${u.username} ${text.slice(caret)}`
    setText(v); setMenu(null)
    requestAnimationFrame(() => { const p = menu.start + u.username.length + 2; ta.current?.setSelectionRange(p, p); ta.current?.focus() })
  }
  const send = async () => {
    if (!text.trim() || busy) return
    setBusy(true)
    try { if (await onSend(text.trim())) setText('') } finally { setBusy(false) }
  }
  return (
    <div className="composer">
      <textarea ref={ta} className="input" rows={text.split('\n').length > 2 ? 4 : 2} value={text} placeholder={placeholder} autoFocus={autoFocus}
        aria-label={placeholder} onChange={e => update(e.target.value, e.target.selectionStart)}
        onKeyDown={e => {
          if (menu && matches.length) {
            if (e.key === 'ArrowDown') { e.preventDefault(); setActive(a => (a + 1) % matches.length); return }
            if (e.key === 'ArrowUp') { e.preventDefault(); setActive(a => (a - 1 + matches.length) % matches.length); return }
            if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick(matches[active]); return }
            if (e.key === 'Escape') { e.preventDefault(); setMenu(null); return }
          }
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send() }
          if (e.key === 'Escape' && onCancel) onCancel()
        }} onBlur={() => setTimeout(() => setMenu(null), 150)} />
      {menu && matches.length > 0 && <ul className="mention-menu" role="listbox">
        {matches.map((u, i) => <li key={u.id} role="option" aria-selected={i === active} className={i === active ? 'on' : ''}
          onMouseDown={e => { e.preventDefault(); pick(u) }}>
          <span className="avatar xs" aria-hidden>{initials(u.full_name || u.username)}</span>{u.full_name || u.username} <span className="faint">@{u.username}</span></li>)}
      </ul>}
      <div className="composer-foot">
        <span className="faint">@ to mention someone · {navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'}+Enter to send</span>
        {onCancel && <button className="btn ghost sm" onClick={onCancel}>Cancel</button>}
        <button className="btn primary sm" disabled={!text.trim() || busy} onClick={send}>{busy ? 'Sending…' : submitLabel}</button>
      </div>
    </div>
  )
}

/** Comment text with @mentions highlighted and links made clickable; never rendered as HTML. */
function Body({ text }: { text: string }) {
  const parts: ReactNode[] = []
  const re = /(@[\w.-]{2,40})|(https?:\/\/[^\s<]+)/g
  let last = 0, m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    parts.push(m[1] ? <span key={m.index} className="mention">{m[1]}</span>
      : <a key={m.index} href={m[2]} target="_blank" rel="noreferrer noopener">{m[2]}</a>)
    last = m.index + m[0].length
  }
  parts.push(text.slice(last))
  return <div className="comment-body">{parts}</div>
}

export default function Discussion({ experimentId, entries, focusEntry, onFocusDone, onCountsChange, canShare, onShare }: {
  experimentId: number; entries: Entry[]; focusEntry: number | null; onFocusDone: () => void
  onCountsChange: (perEntry: Record<number, number>, open: number) => void
  canShare: boolean; onShare: (users: UserBrief[]) => void
}) {
  const { toast } = useApp()
  const loc = useLocation()
  const [comments, setComments] = useState<Comment[] | null>(null)
  const [target, setTarget] = useState<number | ''>('')
  const [replying, setReplying] = useState<number | null>(null)
  const [editing, setEditing] = useState<number | null>(null)
  const [showResolved, setShowResolved] = useState(false)
  const [unseen, setUnseen] = useState<UserBrief[]>([])
  const [flash, setFlash] = useState<number | null>(null)
  const box = useRef<HTMLDivElement>(null)

  const load = () => api.get<Comment[]>(`/api/experiments/${experimentId}/comments`).then(setComments).catch(() => setComments([]))
  useEffect(() => { load() }, [experimentId]) // eslint-disable-line react-hooks/exhaustive-deps

  const threads = useMemo(() => {
    const roots = (comments || []).filter(c => !c.parent_id)
    return roots.map(r => ({ root: r, replies: (comments || []).filter(c => c.parent_id === r.id) }))
  }, [comments])
  useEffect(() => {
    const per: Record<number, number> = {}
    for (const t of threads) if (t.root.entry_id && !t.root.resolved_at && !t.root.deleted) per[t.root.entry_id] = (per[t.root.entry_id] || 0) + 1
    onCountsChange(per, threads.filter(t => !t.root.resolved_at && !t.root.deleted).length)
  }, [threads]) // eslint-disable-line react-hooks/exhaustive-deps

  // a section's "comment" button, or a link to #comment-12
  useEffect(() => {
    if (focusEntry == null) return
    setTarget(focusEntry)
    box.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    setTimeout(() => box.current?.querySelector<HTMLTextAreaElement>('.discussion > .composer textarea')?.focus(), 350)
    onFocusDone()
  }, [focusEntry]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const m = /^#comment-(\d+)$/.exec(loc.hash)
    if (!m || !comments) return
    const id = Number(m[1])
    const c = comments.find(x => x.id === id)
    if (c && (c.resolved_at || comments.find(x => x.id === c.parent_id)?.resolved_at)) setShowResolved(true)
    setTimeout(() => {
      document.getElementById(`comment-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setFlash(id); setTimeout(() => setFlash(null), 2200)
    }, 200)
  }, [loc.hash, comments])

  const post = async (body: string, extra: { entry_id?: number | null; parent_id?: number }) => {
    try {
      const c = await api.post<Comment>(`/api/experiments/${experimentId}/comments`, { body, ...extra })
      setUnseen(c.unseen_mentions || [])
      await load(); setReplying(null)
      return true
    } catch (e: any) { toast(e.message); return false }
  }
  const patch = async (id: number, body: Record<string, unknown>) => {
    try { await api.patch(`/api/comments/${id}`, body); await load(); return true } catch (e: any) { toast(e.message); return false }
  }
  const remove = async (id: number) => {
    if (!confirm('Delete this comment? It stays in the audit trail.')) return
    try { await api.del(`/api/comments/${id}`); await load() } catch (e: any) { toast(e.message) }
  }
  const sectionTitle = (id: number | null) => (id == null ? null : entries.find(e => e.id === id)?.title || 'a removed section')
  const goSection = (id: number) => document.getElementById(`entry-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  const one = (c: Comment, isRoot: boolean, resolved: boolean) => (
    <div key={c.id} id={`comment-${c.id}`} className={`comment${flash === c.id ? ' flash' : ''}${isRoot ? '' : ' reply'}`}>
      <span className="avatar xs" aria-hidden>{c.deleted ? '' : initials(c.user.full_name || c.user.username)}</span>
      <div className="comment-main">
        <div className="comment-meta">
          <b>{c.deleted ? 'Deleted comment' : displayName(c.user)}</b>
          <span className="faint" title={when(c.created_at)}>{ago(c.created_at)}{c.edited_at ? ' · edited' : ''}</span>
        </div>
        {editing === c.id
          ? <Composer placeholder="Edit comment" initial={c.body} autoFocus submitLabel="Save" onCancel={() => setEditing(null)}
              onSend={async b => { const ok = await patch(c.id, { body: b }); if (ok) setEditing(null); return ok }} />
          : !c.deleted && <Body text={c.body} />}
        {!c.deleted && editing !== c.id && <div className="comment-actions">
          {isRoot && !resolved && <button className="link-btn" onClick={() => setReplying(replying === c.id ? null : c.id)}>Reply</button>}
          {isRoot && <button className="link-btn" onClick={() => patch(c.id, { resolved: !resolved })}>{resolved ? 'Reopen' : 'Resolve'}</button>}
          {c.mine && <button className="link-btn" onClick={() => setEditing(c.id)}>Edit</button>}
          {c.mine && <button className="link-btn" onClick={() => remove(c.id)}>Delete</button>}
        </div>}
      </div>
    </div>
  )

  const open = threads.filter(t => !t.root.resolved_at)
  const resolved = threads.filter(t => t.root.resolved_at)
  const thread = (t: typeof threads[number]) => {
    const isResolved = !!t.root.resolved_at
    const sec = sectionTitle(t.root.entry_id)
    return (
      <div key={t.root.id} className={`thread${isResolved ? ' resolved' : ''}`}>
        {sec && <button className="thread-on" onClick={() => goSection(t.root.entry_id!)}>on <b>{sec}</b></button>}
        {isResolved && <div className="thread-resolved faint">✓ Resolved by {displayName(t.root.resolved_by)} {ago(t.root.resolved_at!)}</div>}
        {one(t.root, true, isResolved)}
        {t.replies.map(r => one(r, false, isResolved))}
        {replying === t.root.id && <div className="reply-box"><Composer placeholder={`Reply to ${displayName(t.root.user)}`} autoFocus submitLabel="Reply"
          onCancel={() => setReplying(null)} onSend={b => post(b, { parent_id: t.root.id })} /></div>}
      </div>
    )
  }

  return (
    <div ref={box} className="discussion" id="discussion">
      <h2 className="rail-h">Discussion{open.length ? <span className="count-pill">{open.length}</span> : null}</h2>
      <label className="discussion-target"><span className="faint">On</span>
        <select className="select sm" value={target} onChange={e => setTarget(e.target.value ? Number(e.target.value) : '')}>
          <option value="">The whole experiment</option>
          {entries.map(e => <option key={e.id} value={e.id}>{e.title || 'Untitled section'}</option>)}
        </select>
      </label>
      <Composer placeholder={target ? `Comment on ${sectionTitle(Number(target))}…` : 'Ask a question or leave a note…'}
        onSend={b => post(b, { entry_id: target === '' ? null : target })} />
      {unseen.length > 0 && <div className="notice unseen">
        {unseen.map(u => displayName(u)).join(', ')} can't open this experiment, so {unseen.length === 1 ? "wasn't" : "weren't"} notified.
        {canShare && <button className="btn sm" onClick={() => { onShare(unseen); setUnseen([]) }}>Share it with {unseen.length === 1 ? displayName(unseen[0]) : 'them'}</button>}
        <button className="btn ghost sm" onClick={() => setUnseen([])}>Dismiss</button>
      </div>}
      {comments === null ? <p className="faint">Loading…</p>
        : threads.length === 0 ? <p className="faint" style={{ margin: '4px 0 0' }}>No comments yet. Comments aren't part of the signed record, so they can be added at any time, including during review.</p>
          : <>
            {open.map(thread)}
            {resolved.length > 0 && <Fragment>
              <button className="link-btn" onClick={() => setShowResolved(s => !s)}>{showResolved ? 'Hide' : 'Show'} {resolved.length} resolved</button>
              {showResolved && resolved.map(thread)}
            </Fragment>}
          </>}
    </div>
  )
}
