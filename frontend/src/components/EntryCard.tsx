import { useCallback, useEffect, useRef, useState } from 'react'
import ErrorBoundary from './ErrorBoundary'
import { KindIcon } from './AddSection'
import { api, type Attachment, type Entry } from '../api'
import TextEntry from './TextEntry'
import ReactionEntry from './ReactionEntry'
import TableEntry from './TableEntry'
import FileEntry from './FileEntry'
import PurificationEntry from './PurificationEntry'
import CharacterizationEntry from './CharacterizationEntry'
import VersionsDialog from './VersionsDialog'
import SafetyEntry from './SafetyEntry'
import ScreenEntry from './ScreenEntry'
import CustomEntry from './CustomEntry'
import ReferencesEntry from './ReferencesEntry'
import { useCollapsed } from '../collapse'
import { useApp } from '../App'
import { browserStore, clearDraft, draftSituation, draftText, loadDraft, saveDraft, sweepDrafts, tabId, type Draft } from '../drafts'

// unsaved typing is kept in this browser until the server has it (0.27, drafts.ts)
const STORE = typeof window === 'undefined' ? null : browserStore()
const TAB = typeof window === 'undefined' ? 'tab' : tabId()
sweepDrafts(STORE)
let channel: BroadcastChannel | null | undefined
/** Tabs of this browser ask each other "is anyone still editing this section?" before offering a kept copy. */
function drafts(): BroadcastChannel | null {
  if (channel === undefined) { try { channel = new BroadcastChannel('benchtop-drafts') } catch { channel = null } }
  return channel
}

type SaveState = 'saved' | 'dirty' | 'saving' | 'error'
const KIND_LABEL = { text: 'Text', reaction: 'Reaction & stoichiometry', table: 'Data table', file: 'Files', purification: 'Work-up & purification', characterization: 'Characterisation', safety: 'Safety & risk assessment', screen: 'Screening grid', references: 'References', custom: 'Plugin section' }

export interface EntryEditorProps {
  content: any
  onChange: (c: any) => void
  readOnly: boolean
  experimentId: number
  attachments: Attachment[]
  onAttachmentsChanged: () => void
  context: EntryContext
}

/** Experiment-level facts an editor may need, e.g. the product formula for scaling NMR integrals. */
export interface EntryContext { productFormula: string | null; productSmiles: string | null; productMW: number | null; openAttachmentId: number | null }

export default function EntryCard({ entry, readOnly, experimentId, attachments, onAttachmentsChanged, onDeleted, onMove, isFirst, isLast, context, comments, onComment }: {
  entry: Entry; readOnly: boolean; experimentId: number; attachments: Attachment[]; context: EntryContext
  onAttachmentsChanged: () => void; onDeleted: () => void
  onMove: (dir: -1 | 1) => void; isFirst: boolean; isLast: boolean
  comments?: number; onComment?: () => void
}) {
  const [content, setContent] = useState(entry.content)
  const [title, setTitle] = useState(entry.title)
  const [state, setState] = useState<SaveState>('saved')
  const [error, setError] = useState('')
  const [showVersions, setShowVersions] = useState(false)
  const [version, setVersion] = useState(entry.version)
  const versionRef = useRef(entry.version)
  const edits = useRef(0)
  const latest = useRef({ content, title })
  latest.current = { content, title }
  const { user } = useApp()
  const unsaved = useRef(false)
  unsaved.current = state !== 'saved'
  const [offer, setOffer] = useState<Draft | null>(null)
  const offerRef = useRef<Draft | null>(null)
  offerRef.current = offer
  const [copied, setCopied] = useState(false)

  const save = useCallback(async () => {
    const mark = edits.current
    setState('saving')
    try {
      const r = await api.put<Entry>(`/api/entries/${entry.id}`, {
        title: latest.current.title, content: latest.current.content, expected_version: versionRef.current,
      })
      versionRef.current = r.version
      setVersion(r.version)
      if (edits.current === mark) {
        setContent(r.content) // adopt server-normalised content (sanitised HTML, recomputed stoichiometry)
        setState('saved')
        if (!offerRef.current) clearDraft(STORE, user.id, entry.id)  // the server has it: the kept copy isn't needed
        if (entry.kind === 'reaction') window.dispatchEvent(new CustomEvent('benchtop:reaction-saved', { detail: { experimentId: entry.experiment_id } }))
      } else setState('dirty')
      setError('')
    } catch (e: any) {
      setState('error')
      setError(e.message)
    }
  }, [entry.id, user.id])

  useEffect(() => {
    if (state !== 'dirty') return
    const t = setTimeout(save, 900)
    return () => clearTimeout(t)
  }, [state, content, title, save])

  useEffect(() => {
    if (state === 'saved') return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [state])

  const change = (c: any) => { edits.current++; setContent(c); setState('dirty') }

  // keep a copy of what the server hasn't confirmed yet (not while a kept copy is waiting to be put back or discarded)
  const keep = useCallback(() => {
    if (!unsaved.current || readOnly) return
    saveDraft(STORE, user.id, { entryId: entry.id, experimentId, baseVersion: versionRef.current, title: latest.current.title,
      content: latest.current.content, at: Date.now(), tab: TAB })
  }, [entry.id, experimentId, user.id, readOnly])
  useEffect(() => {
    if (state === 'saved' || offer) return
    const t = setTimeout(keep, 300)
    return () => clearTimeout(t)
  }, [state, content, title, offer, keep])
  useEffect(() => {  // the tab closing, or going to the background (a phone may never come back to it)
    if (offer) return
    const now = () => keep()
    const hidden = () => { if (document.visibilityState === 'hidden') keep() }
    window.addEventListener('pagehide', now)
    document.addEventListener('visibilitychange', hidden)
    return () => { window.removeEventListener('pagehide', now); document.removeEventListener('visibilitychange', hidden) }
  }, [offer, keep])

  // a copy kept from before (a reload, a closed tab, a crash): offer it, unless another open tab is still editing it
  useEffect(() => {
    const d = loadDraft(STORE, user.id, entry.id)
    if (!d) return
    const server = { ...d, title: entry.title, content: entry.content }
    if (d.title === entry.title && (JSON.stringify(d.content) === JSON.stringify(entry.content) || draftText(d) === draftText(server))) {
      clearDraft(STORE, user.id, entry.id)  // the save did reach the server (the tab closed before it heard back)
      return
    }
    if (d.tab === TAB) { setOffer(d); return }
    const ch = drafts()
    let mine = false
    const heard = (e: MessageEvent) => { if (e.data?.editing === entry.id && e.data?.user === user.id) mine = true }
    ch?.addEventListener('message', heard)
    ch?.postMessage({ ask: entry.id, user: user.id })
    const t = setTimeout(() => { ch?.removeEventListener('message', heard); if (!mine) setOffer(loadDraft(STORE, user.id, entry.id)) }, 400)
    return () => { clearTimeout(t); ch?.removeEventListener('message', heard) }
  }, [entry.id, user.id])
  useEffect(() => {  // answer another tab that asks about this section while it has unsaved changes here
    const ch = drafts()
    if (!ch) return
    const answer = (e: MessageEvent) => { if (e.data?.ask === entry.id && e.data?.user === user.id && unsaved.current) ch.postMessage({ editing: entry.id, user: user.id }) }
    ch.addEventListener('message', answer)
    return () => ch.removeEventListener('message', answer)
  }, [entry.id, user.id])

  const putBack = () => {
    if (!offer) return
    edits.current++
    setTitle(offer.title)
    setContent(offer.content)
    setOffer(null)
    setState('dirty')  // saved a moment later, on top of the section as it is now (an earlier version stays in History)
  }
  const discard = () => { clearDraft(STORE, user.id, entry.id); setOffer(null) }
  const copy = async () => {
    if (!offer) return
    try { await navigator.clipboard.writeText(draftText(offer)); setCopied(true) } catch { setCopied(false) }
  }

  // no "are you sure?": the page offers Undo for a few seconds, and the section stays in the history anyway
  const remove = async () => {
    await api.del(`/api/entries/${entry.id}`)
    clearDraft(STORE, user.id, entry.id)
    onDeleted()
  }

  // a newer version arrived from elsewhere (bench log, phone, a colleague): take it if nothing is unsaved here
  useEffect(() => {
    if (entry.version > versionRef.current && state === 'saved') {
      versionRef.current = entry.version
      setVersion(entry.version)
      setContent(entry.content)
      setTitle(entry.title)
    }
  }, [entry.version]) // eslint-disable-line react-hooks/exhaustive-deps

  // ⌘S: save now instead of waiting for the pause
  useEffect(() => {
    const now = () => { if (state === 'dirty') save() }
    window.addEventListener('benchtop:save-now', now)
    return () => window.removeEventListener('benchtop:save-now', now)
  }, [state, save])

  const [collapsed, setCollapsed] = useCollapsed(`entry.${entry.id}`)
  const props: EntryEditorProps = { content, onChange: change, readOnly, experimentId, attachments, onAttachmentsChanged, context }
  const stateText = { saved: `v${version}`, dirty: 'Unsaved', saving: 'Saving…', error: 'Not saved' }[state]

  return (
    <section className="entry" id={`entry-${entry.id}`} aria-label={title}>
      <header className={`entry-head${collapsed ? ' collapsed' : ''}`}>
        <button className="entry-toggle" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed} aria-controls={`entry-body-${entry.id}`}
          aria-label={collapsed ? `Show ${title}` : `Hide ${title}`} title={collapsed ? 'Show this section' : 'Hide this section'}>
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 6l4 4 4-4" /></svg>
        </button>
        <input className="entry-title" value={title} disabled={readOnly} aria-label="Section title"
          onChange={e => { edits.current++; setTitle(e.target.value); setState('dirty') }} />
        <span className="entry-kind" title={entry.kind === 'custom' && content?.schema?.plugin ? `From the ${content.schema.plugin} plugin` : undefined}><KindIcon kind={entry.kind} />{entry.kind === 'custom' ? (content?.schema?.name || 'Plugin section') : KIND_LABEL[entry.kind]}</span>
        <span className={`save-state${state === 'dirty' || state === 'saving' ? ' dirty' : ''}${state === 'error' ? ' err' : ''}`} title={error}>{stateText}</span>
        {onComment && <button className="btn ghost sm entry-comments" onClick={onComment} aria-label={comments ? `${comments} open comment${comments === 1 ? '' : 's'} on this section` : 'Comment on this section'}
          title="Comment on this section">
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg>
          {comments ? <span className="n">{comments}</span> : null}</button>}
        <button className="btn ghost sm" onClick={() => setShowVersions(true)}>History</button>
        {!readOnly && <>
          <button className="btn ghost sm" disabled={isFirst} onClick={() => onMove(-1)} aria-label="Move section up">↑</button>
          <button className="btn ghost sm" disabled={isLast} onClick={() => onMove(1)} aria-label="Move section down">↓</button>
          <button className="btn ghost sm" onClick={remove} aria-label="Remove section">Remove</button>
        </>}
      </header>
      {offer && <DraftOffer d={offer} kind={entry.kind} situation={draftSituation(offer, version, readOnly)} copied={copied} onPutBack={putBack} onDiscard={discard} onCopy={copy} />}
      {state === 'error' && <p className="error" style={{ padding: '6px 16px 0' }}>{error} {error.includes('reload') && <button className="btn sm" onClick={() => location.reload()}>Reload</button>}</p>}
      {collapsed && <button className="entry-peek" onClick={() => setCollapsed(false)} title="Show this section">
        <span>{safePeek(entry.kind, content) || 'Hidden'}</span>{state === 'dirty' || state === 'saving' ? <span className="faint"> · saving…</span> : null}</button>}
      <div className="entry-body" id={`entry-body-${entry.id}`} hidden={collapsed}>
        <ErrorBoundary label={`section ${entry.title}`} raw={content} resetKey={entry.version}>
        {entry.kind === 'text' && <TextEntry {...props} />}
        {entry.kind === 'reaction' && <ReactionEntry {...props} />}
        {entry.kind === 'table' && <TableEntry {...props} />}
        {entry.kind === 'file' && <FileEntry {...props} />}
        {entry.kind === 'purification' && <PurificationEntry {...props} />}
        {entry.kind === 'characterization' && <CharacterizationEntry {...props} />}
        {entry.kind === 'safety' && <SafetyEntry {...props} />}
        {entry.kind === 'screen' && <ScreenEntry {...props} />}
        {entry.kind === 'references' && <ReferencesEntry {...props} />}
        {entry.kind === 'custom' && <CustomEntry {...props} />}
        </ErrorBoundary>
      </div>
      {showVersions && <VersionsDialog entryId={entry.id} kind={entry.kind} onClose={() => setShowVersions(false)} />}
    </section>
  )
}

/** "Unsaved changes from 14:32 were kept in this browser": put them back, or discard them. */
function DraftOffer({ d, kind, situation, copied, onPutBack, onDiscard, onCopy }: {
  d: Draft; kind: string; situation: 'readonly' | 'changed' | 'same'; copied: boolean; onPutBack: () => void; onDiscard: () => void; onCopy: () => void
}) {
  const at = new Date(d.at)
  const today = at.toDateString() === new Date().toDateString()
  const when = today ? at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : at.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
  const what = safePeek(kind, d.content)
  return (
    <div className="draft-offer" role="alert">
      <p><b>Changes from {today ? `today at ${when}` : when} weren't saved</b>, but this browser kept them{what ? <>: <span className="faint">“{what}”</span></> : '.'}</p>
      {situation === 'changed' && <p className="faint">This section was changed since then (here or by someone else). Putting yours back replaces that version; it stays in History.</p>}
      {situation === 'readonly' && <p className="faint">This record can't be edited any more (it was signed, or you can only view it), so they can't go back in. Copy them to use elsewhere, for example in a follow-up.</p>}
      <div className="row" style={{ gap: 6 }}>
        {situation !== 'readonly' && <button className="btn primary sm" onClick={onPutBack}>Put them back</button>}
        {situation === 'readonly' && <button className="btn primary sm" onClick={onCopy}>{copied ? 'Copied' : 'Copy them'}</button>}
        <button className="btn ghost sm" onClick={onDiscard}>Discard</button>
      </div>
    </div>
  )
}

const safePeek = (kind: string, c: any) => { try { return peek(kind, c) } catch { return '' } }

/** One line saying what a hidden section holds. */
function peek(kind: string, c: any): string {
  const text = (html: string) => (html || '').replace(/<(br|\/p|\/li|\/h\d)>/g, ' · ').replace(/<[^>]+>/g, '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/\s+/g, ' ').replace(/^( · )+|( · )+$/g, '').trim()
  const cut = (s: string) => (s.length > 140 ? s.slice(0, 140) + '…' : s)
  if (!c) return ''
  if (kind === 'text') return cut(text(c.html))
  if (kind === 'reaction') {
    const rows = c.rows || [], prod = rows.find((r: any) => r.role === 'product')
    return cut([rows.filter((r: any) => r.role !== 'product').map((r: any) => r.name).filter(Boolean).slice(0, 4).join(', '),
      prod ? `→ ${prod.name || 'product'}${prod.yield_pct != null ? ` (${Math.round(prod.yield_pct)}%)` : ''}` : ''].filter(Boolean).join(' '))
  }
  if (kind === 'purification') return cut([c.method, c.eluent, c.isolated_mg != null ? `${c.isolated_mg} mg` : ''].filter(Boolean).join(' · '))
  if (kind === 'characterization') return cut((c.items || []).map((i: any) => i.technique).filter(Boolean).join(', '))
  if (kind === 'file') return `${(c.attachment_ids || []).length} file${(c.attachment_ids || []).length === 1 ? '' : 's'}`
  if (kind === 'table') return `${(c.rows || []).length} row${(c.rows || []).length === 1 ? '' : 's'} × ${(c.columns || []).length} columns`
  if (kind === 'screen') return `${c.y_name || ''} × ${c.x_name || ''}`
  if (kind === 'references') { const n = (c.items || []).length; return n ? `${n} reference${n === 1 ? '' : 's'}: ${cut((c.items || []).map((r: any) => r.title).filter(Boolean).join(' · '))}` : 'No references yet' }
  if (kind === 'safety') return c.snapshot ? `${(c.snapshot.chemicals || []).length} chemicals assessed` : 'Hazards not recorded yet'
  if (kind === 'custom') return cut(Object.entries(c.values || {}).filter(([, v]) => v !== null && v !== '' && v !== false).slice(0, 4).map(([k, v]) => `${(c.schema?.fields || []).find((f: any) => f.id === k)?.label || k}: ${v === true ? '✓' : v}`).join(' · '))
  return ''
}
