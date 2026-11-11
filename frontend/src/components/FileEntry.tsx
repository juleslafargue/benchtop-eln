import { useEffect, useState } from 'react'
import { api, downloadAuthed, type Attachment } from '../api'
import { useApp } from '../App'
import type { EntryEditorProps } from './EntryCard'
import { hydrogensInFormula, parseSpectrumBuffer } from '../spectra'
import { canViewSpectrum, formatName, needsReadCheck, pictureKind } from '../viewable'
import SpectrumViewer, { type SavedAnalysis } from './SpectrumViewer'

/** An attached picture. A TIFF is shown from a PNG the server makes for viewing (0.36: browsers can't show TIFF, so it
 *  was a broken image); the file itself is unchanged, and Download gives it as it is. A format no browser shows gets a
 *  line saying so instead of a broken image. */
function ImagePreview({ att }: { att: Attachment }) {
  const kind = pictureKind(att)
  const [pic, setPic] = useState<{ url: string; pages: number; scaled: boolean } | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (kind !== 'img' && kind !== 'preview') return
    let u: string | null = null, live = true
    api.raw(kind === 'preview' ? `/api/attachments/${att.id}/preview` : `/api/attachments/${att.id}?inline=true`).then(async r => {
      const b = await r.blob()
      if (!live) return
      u = URL.createObjectURL(b)
      setPic({ url: u, pages: Number(r.headers.get('X-Pages')) || 1, scaled: r.headers.get('X-Scaled') === '1' })
    }).catch(() => { if (live) setFailed(true) })
    return () => { live = false; if (u) URL.revokeObjectURL(u) }
  }, [att.id, kind])
  if (kind === 'none' || (kind === 'preview' && failed))
    return <p className="faint no-preview">No preview for {formatName(att)}. Download it to view.</p>
  if (!pic) return null
  const notes = kind === 'preview' ? [pic.pages > 1 ? `page 1 of ${pic.pages}` : '', pic.scaled ? 'brightness scaled for the screen' : ''].filter(Boolean) : []
  return <>
    <img className="preview" src={pic.url} alt={att.filename} />
    {kind === 'preview' && <p className="faint no-preview">Preview of the TIFF{notes.length ? ` (${notes.join(', ')})` : ''}; Download gives the file itself.</p>}
  </>
}

const size = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n > 1e3 ? `${(n / 1e3).toFixed(0)} kB` : `${n} B`)

export default function FileEntry({ content, onChange, readOnly, experimentId, attachments, onAttachmentsChanged, context }: EntryEditorProps) {
  const [over, setOver] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [viewing, setViewing] = useState<number | null>(null)
  const { toast } = useApp()
  /** A file that came from an instrument folder but isn't this experiment's (a mistyped number): back to the Inbox. */
  const notMine = async (a: Attachment) => {
    if (!confirm(`Send “${a.filename}” back to the Inbox? It leaves this experiment, and someone can assign it to the right one.`)) return
    try {
      await api.post(`/api/attachments/${a.id}/not-mine`)
      toast('Sent back to the Inbox')
      window.dispatchEvent(new CustomEvent('benchtop:reload', { detail: { experimentId } }))  // the server changed the section
      window.dispatchEvent(new Event('benchtop:inbox'))
    } catch (e: any) { toast(e.message) }
  }
  const ids: number[] = content.attachment_ids || []
  const mine = ids.map(id => attachments.find(a => a.id === id)).filter(Boolean) as Attachment[]
  // a .txt/.csv/.tsv is read first: "View spectrum" only for one the viewer can show (0.28 offered it for any .txt,
  // which then failed with "Expected two numeric columns"). Unknown (couldn't fetch it): offered, as before.
  const [readable, setReadable] = useState<Record<number, boolean>>({})
  const toCheck = mine.filter(a => needsReadCheck(a) && !(a.id in readable)).map(a => a.id).join(',')
  useEffect(() => {
    if (!toCheck) return
    let alive = true
    for (const a of mine.filter(x => toCheck.split(',').includes(String(x.id)))) {
      api.raw(`/api/attachments/${a.id}?inline=true`).then(r => r.arrayBuffer())
        .then(buf => parseSpectrumBuffer(buf, a.filename).then(() => true, () => false), () => true)
        .then(ok => { if (alive) setReadable(m => ({ ...m, [a.id]: ok })) })
    }
    return () => { alive = false }
  }, [toCheck]) // eslint-disable-line react-hooks/exhaustive-deps
  const offerViewer = (a: Attachment) => canViewSpectrum(a) && (!needsReadCheck(a) || readable[a.id] === true)

  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    setBusy(true); setErr('')
    const added: number[] = []
    for (const f of Array.from(files)) {
      const fd = new FormData()
      fd.append('file', f)
      try { added.push((await api.post<Attachment>(`/api/experiments/${experimentId}/attachments`, fd)).id) }
      catch (e: any) { setErr(`${f.name}: ${e.message}`) }
    }
    onAttachmentsChanged()
    onChange({ ...content, attachment_ids: [...ids, ...added] })
    setBusy(false)
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      {!readOnly && (
        <label className={`dropzone${over ? ' over' : ''}`}
          onDragOver={e => { e.preventDefault(); setOver(true) }} onDragLeave={() => setOver(false)}
          onDrop={e => { e.preventDefault(); e.stopPropagation(); setOver(false); upload(e.dataTransfer.files) }}>
          {busy ? 'Uploading…' : 'Drop spectra, images or data files here, or click to choose'}
          <input type="file" multiple hidden onChange={e => upload(e.target.files)} />
        </label>
      )}
      {err && <p className="error">{err}</p>}
      {mine.length > 0 && (
        <ul className="files">
          {mine.map(a => (
            <li key={a.id} style={{ flexWrap: 'wrap' }}>
              <span className="grow"><b style={{ fontWeight: 600 }}>{a.filename}</b> <span className="faint">{size(a.size)} · sha256 {a.sha256.slice(0, 12)}…</span></span>
              {offerViewer(a) && <button className={`btn sm${viewing === a.id ? ' primary' : ''}`} onClick={() => setViewing(viewing === a.id ? null : a.id)}>{viewing === a.id ? 'Hide spectrum' : 'View spectrum'}</button>}
              <button className="btn sm" onClick={() => downloadAuthed(`/api/attachments/${a.id}`, a.filename)}>Download</button>
              {!readOnly && a.from_instrument && <button className="btn ghost sm" onClick={() => notMine(a)} title="It isn't this experiment's: send it back to the Inbox">Not mine</button>}
              {!readOnly && !a.from_instrument && <button className="btn ghost sm" onClick={() => onChange({ ...content, attachment_ids: ids.filter(i => i !== a.id) })}>Unlink</button>}
              {pictureKind(a) && <div style={{ flexBasis: '100%' }}><ImagePreview att={a} /></div>}
              {viewing === a.id && <div style={{ flexBasis: '100%' }}><SpectrumViewer attachmentId={a.id} filename={a.filename} expectedH={hydrogensInFormula(context.productFormula)} productSmiles={context.productSmiles} productMW={context.productMW}
                saved={(content.analyses || {})[a.id] as SavedAnalysis | undefined}
                onSave={readOnly ? undefined : an => onChange({ ...content, analyses: { ...(content.analyses || {}), [a.id]: an } })} /></div>}
            </li>
          ))}
        </ul>
      )}
      <input className="input" placeholder={readOnly ? undefined : 'Caption'} disabled={readOnly} value={content.caption || ''}
        onChange={e => onChange({ ...content, caption: e.target.value })} />
    </div>
  )
}
