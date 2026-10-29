import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useApp } from '../App'
import { day, api, displayName, downloadAuthed, when, type AuditEvent, type EntryKind, type Experiment, type ExperimentSummary, type Signature, type WitnessCandidate } from '../api'
import EntryCard from '../components/EntryCard'
import { AddSection } from '../components/AddSection'
import Discussion from '../components/Discussion'
import ShareDialog from '../components/ShareDialog'
import { AudienceBadge } from '../components/Audience'
import SignDialog, { type SignAction } from '../components/SignDialog'
import WriteupDialog from '../components/WriteupDialog'
import { BenchBar, PhoneDialog } from '../components/BenchTools'
import { isCollapsed, setCollapsedMany } from '../collapse'
import ScaleDialog from '../components/ScaleDialog'
import FollowUpDialog from '../components/FollowUpDialog'
import { useBackTarget } from '../components/BackButton'
import { parseSpectrumBuffer, techniqueFor } from '../spectra'
import { DoiLine, PublishPanel } from '../components/Publish'
import type { Attachment, Entry, User, UserBrief } from '../api'

const STATUS_WORD = { open: 'Draft', signed: 'Signed', witnessed: 'Witnessed', voided: 'Voided', locked: 'Locked' }
const STEPS = ['Draft', 'Signed', 'Witnessed'] as const
const shortDate = day

const StatusIcon = ({ status }: { status: Experiment['status'] }) => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {status === 'open' && <path d="M10.5 2.5l3 3L6 13H3v-3z" />}
    {status === 'signed' && <><rect x="3" y="7" width="10" height="7" rx="1.5" /><path d="M5.5 7V5a2.5 2.5 0 015 0v2" /></>}
    {status === 'witnessed' && <><path d="M8 1.8l5 2v4c0 3.2-2.2 5.4-5 6.4-2.8-1-5-3.2-5-6.4v-4z" /><path d="M5.6 8.2l1.7 1.7 3.2-3.4" /></>}
    {status === 'voided' && <><circle cx="8" cy="8" r="5.5" /><path d="M4.2 11.8l7.6-7.6" /></>}
    {status === 'locked' && <><rect x="3" y="7" width="10" height="7" rx="1.5" /><path d="M5.5 7V5a2.5 2.5 0 015 0v2" /><path d="M8 9.6v2" /></>}
  </svg>
)

/** The record's state: a quiet pill, where it is in draft → signed → witnessed, and who did the last step. */
function StatusCard({ exp, fresh }: { exp: Experiment; fresh: boolean }) {
  const lastOf = (m: string) => [...exp.signatures].reverse().find(s => s.meaning === m)
  const who = (s?: Signature) => s ? `${s.signer_name || displayName(s.user)} · ${shortDate(s.signed_at)}` : ''
  const author = lastOf('author'), witness = lastOf('witness'), voided = lastOf('void')
  // a draft whose last signature event is a witness returning it: say so, with the reason, where the state is shown
  const latest = exp.signatures[exp.signatures.length - 1]
  const returned = exp.status === 'open' && latest?.meaning === 'rejected' ? latest : undefined
  const step = { open: 0, signed: 1, witnessed: 2, voided: -1, locked: -1 }[exp.status]
  const line = returned ? <>Returned by {who(returned)}{returned.comment && <><br /><span className="sc-reason">“{returned.comment}”</span></>}
    <br /><span className="faint">Editable until signed again</span></>
    : exp.status === 'open' ? 'Editable until signed'
    : exp.status === 'signed' ? <>Signed by {who(author)}<br /><span className={exp.witness_request?.overdue ? 'overdue-text' : 'faint'}>
      {exp.witness_request ? `Waiting for ${displayName(exp.witness_request.witness)}${exp.witness_request.overdue ? ' · overdue' : ''}` : 'Waiting for a witness'}</span></>
      : exp.status === 'witnessed' ? <>Witnessed by {who(witness)}</>
        : exp.status === 'locked' ? <>Imported read-only from {exp.origin?.source || 'another ELN'}</>
          : <>Voided by {who(voided)}</>
  return (
    <div className={`status-card ${exp.status}${returned ? ' returned' : ''}${fresh ? ' fresh' : ''}`} role="group" aria-label={`Status: ${returned ? 'Returned to the author' : STATUS_WORD[exp.status]}`}>
      <span className="sc-pill"><StatusIcon status={exp.status} />{returned ? 'Returned' : STATUS_WORD[exp.status]}</span>
      {step >= 0 && <ol className="sc-steps" aria-label="Signing progress">
        {STEPS.map((label, i) => (
          <li key={label} className={i < step ? 'done' : i === step ? 'now' : ''} aria-current={i === step ? 'step' : undefined}
            title={i === 1 && author ? `Signed by ${who(author)}` : i === 2 && witness ? `Witnessed by ${who(witness)}` : undefined}>
            <i />{label}
          </li>
        ))}
      </ol>}
      <div className="sc-by">{line}</div>
    </div>
  )
}

/** Where an imported record came from, what was checked on the way in, and (when it was signed there) why it's read-only. */
function OriginBanner({ exp, canFollowUp, onFollowUp }: { exp: Experiment; canFollowUp: boolean; onFollowUp: () => void }) {
  const o = exp.origin!
  const day = (iso: string | null) => iso ? shortDate(iso) : ''
  const facts = [o.original_id && `original ID ${o.original_id}`, o.original_created && `created ${day(o.original_created)}`,
    o.authors.length > 0 && `by ${o.authors.join(', ')}`].filter(Boolean).join(' · ')
  return (
    <div className="notice origin-banner" role="note">
      <div><b>Imported from {o.source}</b> on {day(o.imported_at)} by {o.imported_by}{o.file ? ` (${o.file})` : ''}.
        {facts && <> Originally: {facts}.</>}
        {o.original_url && /^https?:\/\//.test(o.original_url) && <> <a href={o.original_url} target="_blank" rel="noreferrer noopener">Open the original</a>.</>}</div>
      {o.files.imported > 0 && (o.files.mismatched
        ? <div className="error">{o.files.mismatched} of {o.files.imported} files didn't match the fingerprint {o.source} recorded; see “Imported files”.</div>
        : <div className="faint">{o.files.matched === o.files.imported
          ? `All ${o.files.imported} file${o.files.imported === 1 ? '' : 's'} matched the fingerprints ${o.source} recorded.`
          : `${o.files.imported} file${o.files.imported === 1 ? '' : 's'} imported (${o.files.matched} with a fingerprint to check, all matching).`}</div>)}
      {o.signatures.length > 0 && <ul className="origin-sigs">
        {o.signatures.map((s, i) => <li key={i}>{s.meaning === 'witness' ? 'Witnessed' : s.meaning === 'void' ? 'Voided' : s.meaning === 'rejected' ? 'Returned' : 'Signed'} in {o.source} by {s.signer}
          {s.signed_at ? ` on ${day(s.signed_at)}` : ''}{s.matches ? '. The content matches what was signed (checked on import)' : '. The content could not be matched to this signature'}
          {s.timestamp ? '; its trusted-timestamp proof is attached' : ''}.</li>)}
      </ul>}
      {exp.status === 'locked' && <div>Read-only: {o.locked_reason || `it was signed or locked in ${o.source}`}. Signatures from another system are
        kept as history; they aren't Benchtop signatures.{canFollowUp && <> <button className="btn sm" onClick={onFollowUp}>Make a follow-up…</button> to continue the work.</>}</div>}
      {o.warnings.map(w => <div key={w} className="faint">{w}</div>)}
    </div>
  )
}

/** The experiment's sample codes (JL-013): click one to copy it; drafts can get a new one. */
function SampleCodes({ exp, editable, onChange }: { exp: Experiment; editable: boolean; onChange: () => void }) {
  const { user, toast } = useApp()
  const [busy, setBusy] = useState(false)
  const [typing, setTyping] = useState(false)
  const [num, setNum] = useState('')
  const add = async (number?: number) => {
    setBusy(true)
    try {
      const sc = await api.post<{ code: string }>(`/api/experiments/${exp.id}/samples`, number ? { number } : {})
      onChange(); setTyping(false); setNum('')
      navigator.clipboard?.writeText(sc.code).catch(() => {})
      toast(`${sc.code} is now this experiment's (copied)`)
    } catch (e: any) { toast(e.message) } finally { setBusy(false) }
  }
  const retire = async (s: { id: number; code: string }) => {
    if (!confirm(`Take ${s.code} off ${exp.eid}? Its number won't be handed out again; files carrying it will wait in your Inbox.`)) return
    try { await api.del(`/api/samples/${s.id}`); onChange() } catch (e: any) { toast(e.message) }
  }
  const samples = exp.samples || []
  return (
    <div className="sample-codes">
      <span className="faint">Samples</span>
      {samples.map(s => <span key={s.id} className="sample-chip">
        <button className="mono" title="Copy" onClick={() => navigator.clipboard?.writeText(s.code).then(() => toast(`Copied ${s.code}`)).catch(() => toast(s.code))}>{s.code}</button>
        {editable && <button className="x" aria-label={`Take ${s.code} off this experiment`} onClick={() => retire(s)}>×</button>}</span>)}
      {editable && (user.sample_prefix
        ? typing
          ? <span className="row" style={{ gap: 4 }}><span className="mono faint">{user.sample_prefix}-</span>
            <input className="input inline-num" autoFocus inputMode="numeric" value={num} onChange={e => setNum(e.target.value.replace(/\D/g, ''))}
              onKeyDown={e => { if (e.key === 'Enter' && num) add(Number(num)); if (e.key === 'Escape') setTyping(false) }} aria-label="Sample number" />
            <button className="btn sm" disabled={!num || busy} onClick={() => add(Number(num))}>Add</button>
            <button className="btn ghost sm" onClick={() => setTyping(false)}>Cancel</button></span>
          : <><button className="btn sm" disabled={busy} onClick={() => add()}>New sample code</button>
            <button className="btn ghost sm" onClick={() => setTyping(true)} title="A number you've already written on a vial">Use a number I already have…</button></>
        : <Link className="faint" to="/account" style={{ fontSize: 13 }}>Set your initials in Account to give samples codes</Link>)}
    </div>
  )
}

const SIG_LABEL: Record<string, string> = { author: 'Signed by author', witness: 'Witnessed', rejected: 'Returned to author', withdrawn: 'Signature withdrawn', void: 'Voided' }

/** The independent, certified time of a signature (RFC 3161), if this lab uses trusted timestamps. */
function TimestampLine({ s, eid }: { s: Signature; eid: string }) {
  const t = s.timestamp
  if (!t) return null
  if (t.status !== 'ok') return <div className="ts-line pending" title={t.error}>
    {t.status === 'pending' ? (t.attempts === 0 ? 'Trusted timestamp: fetching…' : 'Trusted timestamp: the service couldn’t be reached; Benchtop retries hourly')
      : 'Trusted timestamp: not obtained (an admin can retry)'}</div>
  const late = t.delay_seconds != null && t.delay_seconds > 300
  return (
    <div className="ts-line" title={`An independent timestamp service (${t.tsa}) certifies this signature existed at this time.`}>
      <span aria-hidden>🕒</span> Certified {when(t.gen_time!)} by {t.tsa}{late ? ` (fetched ${Math.round(t.delay_seconds! / 3600) || '<1'} h after signing)` : ''}
      {' '}<button className="linkish" onClick={() => downloadAuthed(`/api/signatures/${s.id}/timestamp`, `${eid}-signature-${s.id}-timestamp.zip`)}>Proof</button>
    </div>
  )
}

function SignatureRail({ sigs, eid }: { sigs: Signature[]; eid: string }) {
  if (!sigs.length) return <p className="faint">No signatures yet.</p>
  return <>{sigs.map(s => (
    <div key={s.id} className={`sig ${s.meaning}`}>
      <div style={{ fontWeight: 600 }}>{SIG_LABEL[s.meaning] || s.meaning}</div>
      <div className="faint">{s.signer_name || displayName(s.user)} · {when(s.signed_at)}</div>
      {s.signer_name && s.user && s.signer_name !== (s.user.full_name || s.user.username) && <div className="faint">now {displayName(s.user)}</div>}
      {s.comment && <div style={{ fontSize: 13 }}>“{s.comment}”</div>}
      <div className="hash" title={s.content_hash}>{s.content_hash.slice(0, 24)}…</div>
      <TimestampLine s={s} eid={eid} />
    </div>
  ))}</>
}

/** Who has been asked to witness, with Remind and Change (author or admin). */
function WitnessPanel({ exp, canManage, onChange }: { exp: Experiment; canManage: boolean; onChange: () => void }) {
  const { toast } = useApp()
  const r = exp.witness_request
  const [picking, setPicking] = useState(false)
  const [people, setPeople] = useState<WitnessCandidate[] | null>(null)
  const [busy, setBusy] = useState(false)
  if (exp.status !== 'signed' || (!r && !canManage)) return null
  const open = () => { setPicking(true); api.get<{ candidates: WitnessCandidate[] }>(`/api/experiments/${exp.id}/witness-candidates`).then(x => setPeople(x.candidates)).catch(e => toast(e.message)) }
  const ask = async (id: number) => {
    setBusy(true)
    try { await api.put(`/api/experiments/${exp.id}/witness-request`, { witness_id: id }); setPicking(false); toast('Asked to witness'); onChange() }
    catch (e: any) { toast(e.message) } finally { setBusy(false) }
  }
  const remind = async () => {
    try { await api.post(`/api/experiments/${exp.id}/witness-request/remind`); toast(`Reminded ${displayName(r!.witness)}`); onChange() } catch (e: any) { toast(e.message) }
  }
  return (
    <div className={`witness-panel${r?.overdue ? ' overdue' : ''}`}>
      {r ? <>
        <div><b>Waiting for {displayName(r.witness)}</b></div>
        <div className="faint" style={{ fontSize: 12.5 }}>Asked {when(r.asked_at)}{r.overdue ? ' · overdue' : r.due_at ? ` · due ${when(r.due_at)}` : ''}
          {r.reminded_at ? ` · reminded ${when(r.reminded_at)}` : ''}{r.email ? '' : ' · no email (notification only)'}</div>
      </> : <div className="faint" style={{ fontSize: 13 }}>Nobody in particular has been asked to witness this record.</div>}
      {canManage && !picking && <div className="row" style={{ gap: 6, marginTop: 6 }}>
        {r && <button className="btn sm" disabled={!r.can_remind_now} title={r.can_remind_now ? '' : 'A reminder went out less than an hour ago'} onClick={remind}>Remind</button>}
        <button className="btn ghost sm" onClick={open}>{r ? 'Ask someone else' : 'Ask someone to witness'}</button>
      </div>}
      {picking && <div className="stack" style={{ gap: 6, marginTop: 6 }}>
        {people === null ? <span className="faint">Loading…</span> : people.length === 0 ? <span className="faint">Nobody else can open this record. Share it first.</span>
          : <select className="select" disabled={busy} defaultValue="" onChange={e => e.target.value && ask(Number(e.target.value))} aria-label="Witness">
            <option value="" disabled>Choose a colleague…</option>
            {people.filter(p => p.id !== r?.witness.id).map(p => <option key={p.id} value={p.id}>{displayName(p)}</option>)}
          </select>}
        <button className="btn ghost sm" onClick={() => setPicking(false)}>Cancel</button>
      </div>}
    </div>
  )
}

function AuditTab({ expId }: { expId: number }) {
  const [events, setEvents] = useState<AuditEvent[]>([])
  useEffect(() => { api.get<AuditEvent[]>(`/api/experiments/${expId}/audit`).then(setEvents) }, [expId])
  const describe = (e: AuditEvent) => {
    const d = e.details || {}
    if (e.object_type === 'entry') return `${e.action} section${d.title ? ` “${d.title}”` : ''}${d.version ? ` → v${d.version}` : ''}`
    if (e.object_type === 'attachment') return `uploaded ${d.filename}`
    if (e.action.startsWith('sign:')) return `${e.action.slice(5)} signature${d.comment ? ` — “${d.comment}”` : ''}`
    if (e.action === 'update') return `changed ${Object.keys(d.after || {}).join(', ')}`
    if (e.action === 'zenodo_prepare') return `prepared a draft on ${d.site}${d.doi ? ` (DOI ${d.doi} reserved)` : ''}`
    if (e.action === 'zenodo_prepare_failed') return `draft on ${d.site} not finished: ${d.error || ''}`
    if (e.action === 'zenodo_publish') return `published on ${d.site} as DOI ${d.doi}`
    if (e.action === 'zenodo_discard') return `discarded the draft on ${d.site}`
    return e.action
  }
  return (
    <ul className="timeline">
      {events.map(e => (
        <li key={e.id}>
          <span className="faint">{when(e.ts)}</span>
          <span>{e.username}</span>
          <span><span className="act">{describe(e)}</span> <span className="hash" title={e.hash}>#{e.id} {e.hash.slice(0, 10)}</span></span>
        </li>
      ))}
    </ul>
  )
}

export default function ExperimentPage() {
  const { id } = useParams()
  const nav = useNavigate()
  const { user, canWrite, toast, refreshNotebooks, setUser } = useApp()
  const [exp, setExp] = useState<Experiment | null>(null)
  // the window / tab title names the record (handy with several app windows open)
  useEffect(() => { if (exp) document.title = `${exp.eid} ${exp.title} · Benchtop`; return () => { document.title = 'Benchtop' } }, [exp?.eid, exp?.title])
  const [err, setErr] = useState('')
  const [tab, setTab] = useState<'record' | 'history'>('record')
  const [dialog, setDialog] = useState<SignAction | null>(null)
  const [fresh, setFresh] = useState(false)
  const [verify, setVerify] = useState<{ intact: boolean; content_hash: string } | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const [writeup, setWriteup] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [importing, setImporting] = useState(false)
  const [justAdded, setJustAdded] = useState<number | null>(null)
  const [importNotes, setImportNotes] = useState<string[]>([])
  const [scaling, setScaling] = useState(false)
  const [sharing, setSharing] = useState<UserBrief[] | null>(null)
  const [followUp, setFollowUp] = useState(false)
  const [focusEntry, setFocusEntry] = useState<number | null>(null)
  const [commentCounts, setCommentCounts] = useState<Record<number, number>>({})
  const dragDepth = useRef(0)
  const [bench, setBench] = useState<null | 'log' | 'timer'>(null)
  const [phone, setPhone] = useState(false)
  const [, redraw] = useState(0)
  useEffect(() => { const on = () => redraw(n => n + 1); window.addEventListener('benchtop:collapse', on); return () => window.removeEventListener('benchtop:collapse', on) }, [])

  const tsPolls = useRef(0)
  useEffect(() => {  // a signature's trusted timestamp arrives a moment after signing: refresh a few times
    if (!exp || !exp.signatures.some(s => s.timestamp?.status === 'pending' && s.timestamp.attempts === 0) || tsPolls.current >= 6) return
    const t = setTimeout(() => { tsPolls.current += 1; load() }, 1500)
    return () => clearTimeout(t)
  }, [exp]) // eslint-disable-line react-hooks/exhaustive-deps
  const load = useCallback(async () => {
    try { setExp(await api.get<Experiment>(`/api/experiments/${id}`)); setErr('') } catch (e: any) { setErr(e.message) }
  }, [id])
  useEffect(() => { load(); setVerify(null); setTab('record'); setBench(null) }, [load])
  // pick up changes made elsewhere (the bench log, your phone, a colleague) when you come back to the tab
  useEffect(() => {
    const again = () => { if (document.visibilityState === 'visible') load() }
    const logged = (e: Event) => { if ((e as CustomEvent).detail?.experimentId === Number(id)) load() }
    document.addEventListener('visibilitychange', again)
    window.addEventListener('focus', again)
    const reload = async (e: Event) => { if ((e as CustomEvent).detail?.experimentId === Number(id)) { await load(); setReloadKey(k => k + 1) } }
    window.addEventListener('benchtop:logged', logged)
    window.addEventListener('benchtop:reload', reload)
    return () => { document.removeEventListener('visibilitychange', again); window.removeEventListener('focus', again); window.removeEventListener('benchtop:logged', logged); window.removeEventListener('benchtop:reload', reload) }
  }, [id, load])
  // L: log a note · T: timer · P: phone · ⌘S: save every section now
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault(); window.dispatchEvent(new Event('benchtop:save-now')); return
      }
      const el = e.target as HTMLElement
      if (/INPUT|TEXTAREA|SELECT/.test(el.tagName) || el.isContentEditable || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]')) return
      const ed = !!exp && canWrite && exp.status === 'open' && (exp.can_edit ?? exp.owner.id === user.id)
      const k = e.key.toLowerCase()
      if (k === 'l' && ed) { e.preventDefault(); setBench('log') }
      else if (k === 't' && ed) { e.preventDefault(); setBench('timer') }
      else if (k === 'p' && exp && !exp.is_template) { e.preventDefault(); setPhone(true) }
    }
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [exp, canWrite, user.id])
  // opening an experiment clears its notifications
  useEffect(() => { if (id) api.post('/api/notifications/read', { experiment_id: Number(id) }).catch(() => {}) }, [id])

  useBackTarget(exp?.notebook_id ? `/notebooks/${exp.notebook_id}` : exp?.is_template ? '/templates' : null,
    exp?.notebook_name || (exp?.is_template ? 'Templates' : 'Dashboard'))
  if (err) return <p className="empty">{err}</p>
  if (!exp) return null

  const rxnRows: any[] = exp.entries.find(e => e.kind === 'reaction' && e.content?.rows?.length)?.content.rows || []
  const product = rxnRows.filter(r => r.role === 'product').sort((a, b) => (b.yield_pct ?? -1) - (a.yield_pct ?? -1))[0]
  const context = { productFormula: product?.formula || null, productSmiles: product?.smiles || null, productMW: product?.mw ?? null, openAttachmentId: justAdded }

  /** Drop files anywhere: spectra become characterisation items (technique and conditions read from
   *  the file); everything else goes into a Files section. */
  const importFiles = async (files: File[]) => {
    if (!files.length) return
    setImporting(true)
    const added: string[] = []
    const notes: string[] = []
    let firstSpectrum: number | null = null
    try {
      for (const f of files) {
        let spec = null
        try { spec = await parseSpectrumBuffer(await f.arrayBuffer(), f.name) } catch (e: any) {
          // explain why a zip of NMR data wasn't read; other non-spectrum files are just files
          if (/\.zip$/i.test(f.name) && /FID|pdata|JCAMP/.test(e?.message || '')) notes.push(`${f.name}: ${e.message}`)
        }
        const fd = new FormData(); fd.append('file', f)
        const att = await api.post<Attachment>(`/api/experiments/${exp.id}/attachments`, fd)
        const fresh = await api.get<Experiment>(`/api/experiments/${exp.id}`)
        if (spec) {
          const { technique, conditions } = techniqueFor(spec)
          const ch = fresh.entries.find(e => e.kind === 'characterization')
          const item = { technique, conditions, data: '', attachment_id: att.id }
          if (ch) {
            const items = [...(ch.content.items || [])]
            const slot = items.findIndex((it: any) => !it.attachment_id && !it.data && it.technique.replace(/\s/g, '').toLowerCase() === technique.replace(/\s/g, '').toLowerCase())
            if (slot >= 0) items[slot] = { ...items[slot], attachment_id: att.id, conditions: items[slot].conditions || conditions }
            else items.push(item)
            await api.put<Entry>(`/api/entries/${ch.id}`, { content: { ...ch.content, items }, expected_version: ch.version })
          } else {
            await api.post(`/api/experiments/${exp.id}/entries`, { kind: 'characterization', title: 'Characterisation', content: { items: [item] } })
          }
          firstSpectrum = firstSpectrum ?? att.id
          added.push(`${technique} (${f.name})`)
        } else {
          const fe = fresh.entries.find(e => e.kind === 'file')
          if (fe) await api.put<Entry>(`/api/entries/${fe.id}`, { content: { ...fe.content, attachment_ids: [...(fe.content.attachment_ids || []), att.id] }, expected_version: fe.version })
          else await api.post(`/api/experiments/${exp.id}/entries`, { kind: 'file', title: 'Spectra & files', content: { attachment_ids: [att.id], caption: '' } })
          added.push(f.name)
        }
      }
      setJustAdded(firstSpectrum)
      await load()
      setReloadKey(k => k + 1)
      toast(`Added ${added.join(', ')}`)
      if (notes.length) setImportNotes(notes)
    } catch (e: any) { toast(e.message) } finally { setImporting(false) }
  }

  const isOwner = exp.owner.id === user.id
  const editable = canWrite && exp.status === 'open' && (exp.can_edit ?? (isOwner || user.role === 'admin' || exp.is_template))

  const patch = async (body: Partial<Experiment>) => {
    try { const r = await api.patch<ExperimentSummary>(`/api/experiments/${exp.id}`, body); setExp({ ...exp, ...r }) }
    catch (e: any) { toast(e.message) }
  }
  // insert before section `before` (an index into exp.entries), or at the end; then bring it into view
  const addSection = async (kind: EntryKind, before?: number, plugin?: string) => {
    const titles: Record<EntryKind, string> = { text: 'Notes', reaction: 'Reaction', table: 'Data', file: 'Spectra & files', purification: 'Work-up & purification', characterization: 'Characterisation', safety: 'Safety & risk assessment', screen: 'Screening', references: 'References', custom: '' }
    try {
      const position = before != null && exp.entries[before] ? exp.entries[before].position : undefined
      const e = await api.post<Entry>(`/api/experiments/${exp.id}/entries`, { kind, title: titles[kind], ...(plugin ? { plugin_section: plugin } : {}), ...(position != null ? { position } : {}) })
      await load()
      requestAnimationFrame(() => {
        const el = document.getElementById(`entry-${e.id}`)
        if (!el) return
        el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        el.classList.add('just-added'); setTimeout(() => el.classList.remove('just-added'), 1600)
      })
    } catch (err: any) { toast(err.message) }
  }
  const kindsPresent = exp.entries.map(e => e.kind)
  const move = async (idx: number, dir: -1 | 1) => {
    const ids = exp.entries.map(e => e.id)
    ;[ids[idx], ids[idx + dir]] = [ids[idx + dir], ids[idx]]
    await api.post(`/api/experiments/${exp.id}/reorder`, { entry_ids: ids })
    await load()
    setReloadKey(k => k + 1)
  }
  const scaleUp = async (factor: number) => {
    try {
      const r = await api.post<ExperimentSummary>(`/api/experiments/${exp.id}/clone?scale=${factor}`)
      await refreshNotebooks(); setScaling(false)
      toast(`New experiment at ×${factor} scale`)
      nav(`/experiments/${r.id}`)
    } catch (e: any) { toast(e.message) }
  }
  const clone = async (asTemplate: boolean) => {
    const r = await api.post<ExperimentSummary>(`/api/experiments/${exp.id}/clone?as_template=${asTemplate}`)
    await refreshNotebooks()
    toast(asTemplate ? 'Saved as template' : 'Copy created')
    nav(`/experiments/${r.id}`)
  }
  const runVerify = async () => setVerify(await api.get(`/api/experiments/${exp.id}/verify`))
  const attachmentsChanged = async () => {
    const r = await api.get<Experiment>(`/api/experiments/${exp.id}`)
    setExp(e => (e ? { ...e, attachments: r.attachments } : e))
  }

  return (
    <article className="exp-page"
      onDragEnter={e => { if (editable && e.dataTransfer.types.includes('Files')) { dragDepth.current++; setDragging(true) } }}
      onDragLeave={() => { if (--dragDepth.current <= 0) { dragDepth.current = 0; setDragging(false) } }}
      onDragOver={e => { if (editable && e.dataTransfer.types.includes('Files')) e.preventDefault() }}
      onDrop={e => {
        dragDepth.current = 0; setDragging(false)
        if (!editable || e.isDefaultPrevented()) return // a section's own drop zone already handled it
        e.preventDefault(); importFiles(Array.from(e.dataTransfer.files))
      }}>
      {(dragging || importing) && <div className="drop-overlay"><div>
        {importing ? 'Adding files…' : <>Drop to add to <b>{exp.eid}</b><br /><span className="faint" style={{ fontSize: 14 }}>
          Spectra (JCAMP-DX, Bruker .zip, CSV) go into Characterisation with the technique filled in; other files go into a Files section.</span></>}
      </div></div>}
      <header className="exp-head">
        <div style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 8 }}>
            <span className="eid">{exp.eid}</span>
            {exp.notebook_id && <Link className="faint" to={`/notebooks/${exp.notebook_id}`}>{exp.notebook_name}</Link>}
            {exp.is_template && <span className="tag">Template</span>}
          </div>
          <h1 className="vh">{exp.eid} {exp.title}</h1>
          <textarea className="exp-title-input" aria-label="Experiment title" disabled={!editable} defaultValue={exp.title} key={`t${exp.id}-${exp.title}`} rows={1} maxLength={300}
            ref={el => { if (el) { el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px` } }}
            onInput={e => { const el = e.currentTarget; el.value = el.value.replace(/\n/g, ' '); el.style.height = 'auto'; el.style.height = `${el.scrollHeight}px` }}
            onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur() } }}
            onBlur={e => { const v = e.target.value.trim(); if (v && v !== exp.title) patch({ title: v }) }} />
          <div className="meta-line">
            <span>Author: {displayName(exp.owner)}</span>
            <span>Created {when(exp.created_at)}</span>
            <span>Modified {when(exp.updated_at)}</span>
            <DoiLine pub={exp.publication} />
          </div>
          {(exp.follows || !!exp.follow_ups?.length) && <div className="follow-links">
            {exp.follows && ('hidden' in exp.follows
              ? <span>Follow-up to a record you don't have access to</span>
              : <span>Follow-up to <Link to={`/experiments/${exp.follows.id}`}><b>{exp.follows.eid}</b> {exp.follows.title}</Link></span>)}
            {!!exp.follow_ups?.length && <span>Follow-up{exp.follow_ups.length > 1 ? 's' : ''}: {exp.follow_ups.map((f, i) =>
              <Fragment key={f.id}>{i > 0 && ', '}<Link to={`/experiments/${f.id}`} title={f.title}><b>{f.eid}</b></Link></Fragment>)}</span>}
          </div>}
          {!exp.is_template && (!!exp.samples?.length || editable) && <SampleCodes exp={exp} editable={editable} onChange={load} />}
          {exp.audience && !exp.is_template && <div className="exp-access">
            <AudienceBadge audience={exp.audience} />
            {exp.shared_with_me && <span className="tag">Shared with you · {exp.shared_with_me === 'edit' ? 'can edit' : 'can view'}</span>}
            <button className="btn ghost sm" onClick={() => setSharing([])}>Share…</button>
          </div>}
          <div className="row" style={{ marginTop: 8, gap: 6 }}>
            {exp.tags.map(t => <span className="tag" key={t}>{t}{editable && <button className="btn ghost sm" style={{ padding: '0 0 0 4px' }} aria-label={`Remove tag ${t}`} onClick={() => patch({ tags: exp.tags.filter(x => x !== t) })}>×</button>}</span>)}
            {editable && <input className="input" style={{ width: 110, padding: '1px 8px', fontSize: 13 }} placeholder="Add tag" aria-label="Add tag"
              onKeyDown={e => { const v = (e.target as HTMLInputElement).value.trim(); if (e.key === 'Enter' && v) { patch({ tags: [...exp.tags, v] }); (e.target as HTMLInputElement).value = '' } }} />}
          </div>
        </div>
        {!exp.is_template && <StatusCard exp={exp} fresh={fresh} />}
      </header>

      <div className="tabs">
        <div className="tabs-list" role="tablist" aria-label="Record or audit trail">
          <button role="tab" aria-selected={tab === 'record'} onClick={() => setTab('record')}>Record</button>
          <button role="tab" aria-selected={tab === 'history'} onClick={() => setTab('history')}>Audit trail</button>
        </div>
        {tab === 'record' && <div className="tabs-end">
          {exp.entries.length > 1 && (() => {
            const keys = exp.entries.map(e => `entry.${e.id}`)
            const allHidden = keys.every(isCollapsed)
            return <button className="btn ghost sm" onClick={() => setCollapsedMany(keys, !allHidden)}
              title={allHidden ? 'Show every section' : 'Hide every section, leaving just the titles'}>{allHidden ? 'Expand all' : 'Collapse all'}</button>
          })()}
          {editable && <AddSection variant="button" existing={kindsPresent} onAdd={(k, p) => addSection(k, undefined, p)} />}
        </div>}
      </div>

      {tab === 'history' ? <AuditTab expId={exp.id} /> : (
        <div className="layout-2">
          <div>
            {importNotes.length > 0 && <div className="notice" style={{ marginBottom: 14 }}>
              {importNotes.map(n => <div key={n}>{n}</div>)}
              <button className="btn ghost sm" onClick={() => setImportNotes([])}>Dismiss</button></div>}
            {exp.status === 'signed' && <p className="notice">This record is signed and locked. {isOwner ? 'Withdraw your signature to make changes.' : 'Review it, then witness it or return it to the author.'}</p>}
            {exp.status === 'voided' && <p className="notice">This record has been voided. It is kept for reference.</p>}
            {exp.origin && <OriginBanner exp={exp} canFollowUp={canWrite && exp.status === 'locked'} onFollowUp={() => setFollowUp(true)} />}
            <div style={{ marginTop: exp.status === 'open' ? 0 : 14 }}>
              {exp.entries.map((e, i) => (
                <Fragment key={`${e.id}-${reloadKey}`}>
                  {i > 0 && (editable
                    ? <AddSection variant="between" existing={kindsPresent} onAdd={(k, p) => addSection(k, i, p)} label="Add section here" />
                    : <div className="entry-gap" />)}
                  <EntryCard entry={e} readOnly={!editable} experimentId={exp.id} context={context}
                    attachments={exp.attachments} onAttachmentsChanged={attachmentsChanged}
                    onDeleted={async () => {
                      await load()
                      toast(`Removed “${e.title}”`, { label: 'Undo', run: async () => {
                        try { await api.post(`/api/entries/${e.id}/restore`); await load(); setReloadKey(k => k + 1) } catch (er: any) { toast(er.message) }
                      } })
                    }} onMove={d => move(i, d)} isFirst={i === 0} isLast={i === exp.entries.length - 1}
                    comments={commentCounts[e.id] || 0} onComment={() => setFocusEntry(e.id)} />
                </Fragment>
              ))}
            </div>
            {editable && <AddSection variant="end" existing={kindsPresent} onAdd={(k, p) => addSection(k, undefined, p)} label={exp.entries.length ? 'Add a section' : 'Add the first section'} />}
          </div>

          <aside className="rail">
            {!exp.is_template && canWrite && (
              <div className="stack" style={{ gap: 8 }}>
                {exp.status === 'open' && isOwner && <button className="btn primary" onClick={() => setDialog('sign')}>Sign as author</button>}
                {exp.status === 'signed' && !isOwner && <>
                  <button className="btn primary" onClick={() => setDialog('witness')}>Witness</button>
                  <button className="btn" onClick={() => setDialog('reject')}>Return to author</button>
                </>}
                {exp.status === 'signed' && isOwner && <button className="btn" onClick={() => setDialog('withdraw')}>Withdraw signature</button>}
              </div>
            )}
            {!exp.is_template && <div>
              <h2 className="rail-h">Signatures</h2>
              <WitnessPanel exp={exp} canManage={isOwner || user.role === 'admin'} onChange={load} />
              <SignatureRail sigs={exp.signatures} eid={exp.eid} />
              <button className="btn ghost sm" onClick={runVerify}>Verify integrity</button>
              {verify && <p style={{ fontSize: 13, margin: '6px 0 0' }} className={verify.intact ? '' : 'error'}>
                {verify.intact ? 'Content matches the latest signature.' : 'Content does not match the signature. The record may have been altered outside the app.'}
                <span className="hash" style={{ display: 'block' }}>{verify.content_hash}</span></p>}
            </div>}
            {!exp.is_template && <PublishPanel exp={exp} onChange={load} />}
            {!exp.is_template && <div className="stack bench-tools" style={{ gap: 6 }}>
              <h2 className="rail-h">At the bench</h2>
              {editable && <button className="btn sm" onClick={() => setBench('log')} title="Shortcut: L">Log a note <kbd>L</kbd></button>}
              {editable && <button className="btn sm" onClick={() => setBench('timer')} title="Shortcut: T">Start a timer <kbd>T</kbd></button>}
              <button className="btn sm" onClick={() => setPhone(true)} title="Shortcut: P">Open on your phone <kbd>P</kbd></button>
            </div>}
            <Discussion experimentId={exp.id} entries={exp.entries} focusEntry={focusEntry} onFocusDone={() => setFocusEntry(null)}
              onCountsChange={per => setCommentCounts(per)} canShare={isOwner || user.role === 'admin'} onShare={us => setSharing(us)} />
            <div className="stack" style={{ gap: 6 }}>
              <h2 className="rail-h">Record</h2>
              {!exp.is_template && (() => {
                const pins = user.preferences?.pinned || []
                const pinned = pins.includes(exp.id)
                return <button className={`btn sm${pinned ? ' primary' : ''}`} aria-pressed={pinned} onClick={async () => {
                  try { setUser(await api.patch<User>('/api/auth/me', { preferences: { pinned: pinned ? pins.filter(i => i !== exp.id) : [exp.id, ...pins] } })); toast(pinned ? 'Unpinned' : 'Pinned to your dashboard') }
                  catch (e: any) { toast(e.message) }
                }}>{pinned ? 'Pinned to dashboard' : 'Pin to dashboard'}</button>
              })()}
              {!exp.is_template && <button className="btn sm" onClick={() => setWriteup(true)}>Draft write-up</button>}
              {canWrite && !exp.is_template && (exp.status === 'signed' || exp.status === 'witnessed' || exp.status === 'locked') &&
                <button className="btn sm" onClick={() => setFollowUp(true)} title="A new draft linked to this one, for results that came after signing">Make a follow-up…</button>}
              <button className="btn sm" onClick={() => downloadAuthed(`/api/experiments/${exp.id}/export?format=pdf`, `${exp.eid} ${exp.title.replace(/[\\/:*?"<>|#^[\]]+/g, ' ').slice(0, 80)}.pdf`).catch(e => toast(e.message))}>Download PDF</button>
              <button className="btn sm" onClick={() => downloadAuthed(`/api/experiments/${exp.id}/export`, `${exp.eid}.html`, true)}>Open printable report</button>
              <button className="btn sm" onClick={() => downloadAuthed(`/api/experiments/${exp.id}/export?format=md`, `${exp.eid} ${exp.title.replace(/[\\/:*?"<>|#^[\]]+/g, ' ').slice(0, 80)}.md`)}>Download Markdown</button>
              <button className="btn sm" onClick={() => downloadAuthed(`/api/experiments/${exp.id}/export?format=json`, `${exp.eid}.json`)}>Download JSON</button>
              {!exp.is_template && <button className="btn sm" title="The shared format of electronic lab notebooks: opens in eLabFTW, RSpace, Kadi4Mat or another Benchtop"
                onClick={() => downloadAuthed(`/api/experiments/${exp.id}/export.eln`, `${exp.eid} - ${exp.title.replace(/[\\/:*?"<>|#^[\]{}%]+/g, ' ').slice(0, 70)}.eln`).catch(e => toast(e.message))}>Download .eln</button>}
              {canWrite && <button className="btn sm" onClick={() => clone(false)}>Duplicate</button>}
              {canWrite && !exp.is_template && <button className="btn sm" onClick={() => setScaling(true)}>Repeat at another scale…</button>}
              {canWrite && !exp.is_template && <button className="btn sm" onClick={() => clone(true)}>Save as template</button>}
              {canWrite && (isOwner || user.role === 'admin') && exp.status !== 'voided' && !exp.is_template &&
                <button className="btn ghost sm danger" onClick={() => setDialog('void')}>Void…</button>}
            </div>
          </aside>
        </div>
      )}

      {bench && editable && <BenchBar key={bench} experimentId={exp.id} eid={exp.eid} initialTimer={bench === 'timer'} onClose={() => setBench(null)} />}
      {phone && <PhoneDialog experimentId={exp.id} onClose={() => setPhone(false)} />}
      {followUp && <FollowUpDialog eid={exp.eid} title={exp.title} notebookId={exp.notebook_id}
        url={`/api/experiments/${exp.id}/follow-up`} onClose={() => setFollowUp(false)} />}
      {sharing && <ShareDialog experimentId={exp.id} eid={exp.eid} preselect={sharing} onClose={() => { setSharing(null); load() }} />}
      {scaling && (() => {
        const rx = exp.entries.find(e => e.kind === 'reaction')?.content
        const lim = (rx?.rows || []).find((r: any) => r.limiting)
        return <ScaleDialog limitingMmol={rx?.totals?.limiting_mmol ?? null} limitingName={lim?.name || null} onScale={scaleUp} onClose={() => setScaling(false)} />
      })()}
      {writeup && <WriteupDialog experimentId={exp.id} canInsert={editable} onClose={() => setWriteup(false)}
        onInserted={async () => { await load(); toast('Write-up added as a section') }} />}
      {dialog && <SignDialog exp={exp} action={dialog} onClose={() => setDialog(null)} onDone={r => {
        setExp(r); setDialog(null); setVerify(null); setFresh(true); setReloadKey(k => k + 1)
        toast({ sign: 'Signed and locked', witness: 'Witnessed', reject: 'Returned to author', withdraw: 'Signature withdrawn', void: 'Voided' }[dialog])
        refreshNotebooks()
      }} />}
    </article>
  )
}
