import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApp } from '../App'
import { api, type ElnPreview, type ElnImportResult, day as dayOf } from '../api'
import Modal from './Modal'

const mb = (n: number) => n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`
const day = (iso: string | null) => iso ? dayOf(iso) : ''

/** Bring experiments from another ELN (or another Benchtop) in, from a .eln file: look first, then tick what to import. */
export default function ImportElnDialog({ notebookId, onClose, onDone }: { notebookId: number; onClose: () => void; onDone: () => void }) {
  const { notebooks } = useApp()
  const [preview, setPreview] = useState<ElnPreview | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [target, setTarget] = useState(notebookId)
  const [busy, setBusy] = useState<'' | 'reading' | 'importing'>('')
  const [err, setErr] = useState('')
  const [result, setResult] = useState<ElnImportResult | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const token = useRef<string | null>(null)
  // a file looked at but not imported is cleaned up straight away (and after a day regardless)
  useEffect(() => () => { if (token.current) api.del(`/api/eln/import/${token.current}`).catch(() => {}) }, [])

  const read = async (f: File) => {
    setBusy('reading'); setErr(''); setPreview(null)
    const fd = new FormData(); fd.append('file', f)
    try {
      const p = await api.post<ElnPreview>('/api/eln/preview', fd)
      token.current = p.token
      setPreview(p)
      setPicked(new Set(p.items.filter(i => !i.already).map(i => i.key)))
    } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  const run = async () => {
    if (!preview) return
    setBusy('importing'); setErr('')
    try {
      const r = await api.post<ElnImportResult>(`/api/eln/import/${preview.token}`, { notebook_id: target, items: [...picked] })
      if (!r.failed.length) token.current = null
      setResult(r); onDone()
    } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  const toggle = (k: string) => setPicked(s => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n })
  const writable = notebooks.filter(n => !n.archived)
  const chosen = preview?.items.filter(i => picked.has(i.key)) ?? []
  const locked = chosen.filter(i => i.locked).length

  if (result) return (
    <Modal title="Import finished" onClose={onClose} footer={<button className="btn primary" onClick={onClose}>Done</button>}>
      <p style={{ marginTop: 0 }}>{result.created.length} experiment{result.created.length === 1 ? '' : 's'} imported
        {result.failed.length ? `; ${result.failed.length} couldn't be` : ''}.</p>
      <ul className="eln-results">
        {result.created.map(c => <li key={c.id}><Link to={`/experiments/${c.id}`} onClick={onClose}>{c.eid}</Link> {c.title}
          {c.locked && <span className="badge">read-only</span>}
          {c.warnings.map(w => <div key={w} className="faint eln-warn">{w}</div>)}</li>)}
        {result.failed.map(f => <li key={f.title} className="error">{f.title}: {f.error}</li>)}
      </ul>
    </Modal>
  )

  return (
    <Modal wide title="Import from a .eln file" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      {preview && <button className="btn primary" disabled={!chosen.length || !!busy} onClick={run}>
        {busy === 'importing' ? 'Importing…' : `Import ${chosen.length} experiment${chosen.length === 1 ? '' : 's'}`}</button>}
    </>}>
      {!preview && <div className="stack" style={{ gap: 10 }}>
        <p style={{ margin: 0 }}>A <b>.eln</b> file is the shared export format of electronic lab notebooks: eLabFTW, RSpace, Kadi4Mat,
          SampleDB, Chemotion, another Benchtop and others. Export from there, then choose the file here. You'll see what's in it before
          anything is added.</p>
        <input ref={fileRef} type="file" accept=".eln,.zip,application/zip" hidden onChange={e => { const f = e.target.files?.[0]; if (f) read(f); e.target.value = '' }} />
        <div><button className="btn primary" disabled={!!busy} onClick={() => fileRef.current?.click()}>
          {busy === 'reading' ? 'Reading the file…' : 'Choose a .eln file…'}</button></div>
        {busy === 'reading' && <p className="faint" style={{ margin: 0 }}>Large exports take a moment to upload and check.</p>}
      </div>}
      {preview && <div className="stack" style={{ gap: 12 }}>
        <p style={{ margin: 0 }}><b>{preview.filename}</b> · from {preview.source}{preview.exported ? `, exported ${day(preview.exported)}` : ''} · {mb(preview.size)}.
          {' '}{preview.items.length} experiment{preview.items.length === 1 ? '' : 's'} inside.</p>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn ghost sm" onClick={() => setPicked(new Set(preview.items.map(i => i.key)))}>Tick all</button>
          <button className="btn ghost sm" onClick={() => setPicked(new Set())}>Untick all</button>
        </div>
        <ul className="eln-items" aria-label="Experiments in the file">
          {preview.items.map(i => (
            <li key={i.key} className={picked.has(i.key) ? 'on' : ''}>
              <label>
                <input type="checkbox" checked={picked.has(i.key)} onChange={() => toggle(i.key)} />
                <span className="eln-item">
                  <span className="eln-title">{i.title}</span>
                  <span className="faint">{[i.identifier, day(i.created), i.authors.join(', '),
                    i.files ? `${i.files} file${i.files === 1 ? '' : 's'}, ${mb(i.bytes)}` : 'no files'].filter(Boolean).join(' · ')}</span>
                  {i.locked && <span className="eln-lock">Arrives read-only: {i.lock_reason}.</span>}
                  {i.already && <span className="eln-warn">Already imported as <Link to={`/experiments/${i.already.id}`} onClick={onClose}>{i.already.eid}</Link>; tick it to import a second copy.</span>}
                  {i.warnings.map(w => <span key={w} className="eln-warn">{w}</span>)}
                </span>
              </label>
            </li>
          ))}
        </ul>
        <label className="field">Into the notebook
          <select className="input" value={target} onChange={e => setTarget(Number(e.target.value))}>
            {writable.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
          </select></label>
        <p className="help" style={{ margin: 0 }}>Each becomes a new Benchtop record owned by you, with a new EXP number. Where it came from
          (the original ID, dates and authors) stays on it, and every file is checked against the fingerprint the other ELN recorded.
          {locked > 0 && ` ${locked} ${locked === 1 ? 'was' : 'were'} signed or locked there, so ${locked === 1 ? 'it arrives' : 'they arrive'} read-only; “Make a follow-up” continues the work.`}
          {' '}Signatures from another system are kept as history, not as Benchtop signatures.</p>
      </div>}
      {err && <p className="error">{err}</p>}
    </Modal>
  )
}
