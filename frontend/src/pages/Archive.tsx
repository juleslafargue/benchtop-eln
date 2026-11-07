import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useApp } from '../App'
import { api, downloadAuthed, when } from '../api'
import {
  active, countLine, defaultSpan, fraction, megabytes, previewQuery, PRINT, progressLine, quickPicks, rangeProblem, spanBody, toDraw,
  type ArchiveJob, type ArchiveSpectrum, type Choices, type Options, type Preview,
} from '../archive'
import { hydrogensInFormula } from '../spectra'
import { prepareSpectrum, spectrumPng } from '../spectrumDraw'
import { canViewSpectrum } from '../viewable'

const SIZE_KEY = 'benchtop.archive.pageSize'
const readSize = (): Choices['page_size'] => { try { return localStorage.getItem(SIZE_KEY) === 'a4' ? 'a4' : 'letter' } catch { return 'letter' } }
const TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || '' } catch { return '' } })()
const isDay = (s: string | null) => !!s && /^\d{4}-\d{2}-\d{2}$/.test(s)

/** The archive PDF (0.34): the records signed from one day to another (a year, a month, a week) as one archival PDF,
 *  to print and keep. */
export default function Archive() {
  const { toast } = useApp()
  const [params] = useSearchParams()
  const [opts, setOpts] = useState<Options | null>(null)
  const [err, setErr] = useState('')
  const [choices, setChoices] = useState<Choices>(() => {
    const d = defaultSpan(), f = params.get('from'), t = params.get('to')
    return { from: isDay(f) ? f! : d.from, to: isDay(t) ? t! : d.to, notebook_id: null, author_id: null, page_size: readSize() }
  })
  const picks = quickPicks()
  const problem = rangeProblem(choices.from, choices.to)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [job, setJob] = useState<ArchiveJob | null>(null)
  const [drawing, setDrawing] = useState<number | undefined>()
  const [starting, setStarting] = useState(false)
  const running = useRef<string | null>(null)  // the job this page is working on (one loop at a time)
  const downloadWhenDone = useRef(false)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  // ------------------------------------------------------------------ the job: draw, then lay out, then download
  const download = useCallback((j: ArchiveJob) => {
    downloadAuthed(`/api/archive/jobs/${j.id}/download`, j.file_name, false, true).catch(e => toast(e.message))
  }, [toast])

  const follow = useCallback(async (id: string) => {
    for (;;) {
      if (!alive.current || running.current !== id) return
      let j: ArchiveJob
      try { j = await api.get<ArchiveJob>(`/api/archive/jobs/${id}`) } catch (e: any) {
        if (e.status === 404) { setJob(null); running.current = null; return }
        await new Promise(r => setTimeout(r, 3000)); continue  // Benchtop unreachable for a moment: keep trying
      }
      setJob(j)
      if (!active(j.state)) {
        running.current = null
        if (j.state === 'done' && downloadWhenDone.current) { downloadWhenDone.current = false; download(j) }
        return
      }
      await new Promise(r => setTimeout(r, 1000))
    }
  }, [download])

  const draw = useCallback(async (j: ArchiveJob) => {
    if (running.current) return
    running.current = j.id
    try {
      const all = j.spectra || []
      const left = toDraw(all)
      let k = all.length - left.length
      for (const s of left) {
        if (!alive.current || running.current !== j.id) return
        k++
        setDrawing(k)
        await new Promise(r => setTimeout(r, 0))  // let the page answer between spectra
        await drawOne(j.id, s)
        setJob(cur => (cur && cur.id === j.id ? { ...cur, spectra_done: k } : cur))
      }
      setDrawing(undefined)
      const built = await api.post<ArchiveJob>(`/api/archive/jobs/${j.id}/build`)
      setJob(built)
      downloadWhenDone.current = true
    } catch (e: any) {
      running.current = null
      setDrawing(undefined)
      toast(`The archive wasn't made: ${e.message}`)
      api.get<ArchiveJob>(`/api/archive/jobs/${j.id}`).then(setJob).catch(() => {})
      return
    }
    follow(j.id)  // laying out runs on the server: follow it (running.current stays this job)
  }, [follow, toast])

  const resume = useCallback((j: ArchiveJob | null) => {
    setJob(j)
    if (!j) return
    if (j.state === 'drawing') draw(j)
    else if (active(j.state) && !running.current) { running.current = j.id; follow(j.id) }
  }, [draw, follow])

  // ------------------------------------------------------------------ choices and the count
  useEffect(() => {
    api.get<Options>('/api/archive/options').then(o => {
      setOpts(o)
      const nb = Number(params.get('notebook')) || null
      setChoices(c => ({ ...c, notebook_id: nb && o.notebooks.some(n => n.id === nb) ? nb : null }))
      resume(o.job)
    }).catch(e => setErr(e.message))
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setPreview(null)
    if (problem) return
    let stale = false
    api.get<Preview>(`/api/archive/preview?${previewQuery(choices, TZ)}`).then(p => { if (!stale) setPreview(p) }).catch(e => toast(e.message))
    return () => { stale = true }
  }, [choices.from, choices.to, choices.notebook_id, choices.author_id, problem, toast])  // eslint-disable-line react-hooks/exhaustive-deps

  const set = (p: Partial<Choices>) => setChoices(c => {
    const next = { ...c, ...p }
    if (p.page_size) { try { localStorage.setItem(SIZE_KEY, p.page_size) } catch { /* private window: not remembered */ } }
    return next
  })

  const start = async () => {
    if (problem) return
    setStarting(true)
    try {
      running.current = null
      const j = await api.post<ArchiveJob>('/api/archive/jobs', spanBody(choices, TZ))
      setJob(j)
      draw(j)
    } catch (e: any) { toast(e.message) } finally { setStarting(false) }
  }
  const discard = async () => {
    if (!job) return
    running.current = null
    try { await api.del(`/api/archive/jobs/${job.id}`) } catch { /* already gone */ }
    setJob(null)
  }

  if (err) return <p className="error">{err}</p>
  if (!opts) return <p className="faint">Loading…</p>
  const busy = !!job && active(job.state)

  return (
    <div className="stack archive-page" style={{ gap: 18 }}>
      <div>
        <h1 className="page-title">Archive PDF</h1>
        <p className="lede">One PDF of the records signed over a span of days (a year, a month, a week), made to be printed and
          kept: each record as Benchtop prints it, with its spectra drawn, in the order they were signed. It's an archival PDF (PDF/A): its fonts are inside it, and so is
          each record's data with the proof of its signatures. Making it never changes a record.</p>
      </div>

      <section className="panel panel-pad stack" aria-labelledby="archive-what" style={{ gap: 14 }}>
        <h2 className="si-h2" id="archive-what">What goes in</h2>
        <div className="row archive-picks" role="group" aria-label="Quick choices">
          {picks.map(p => <button key={p.label} type="button" disabled={busy}
            className={`chip${p.from === choices.from && p.to === choices.to ? ' on' : ''}`} onClick={() => set({ from: p.from, to: p.to })}>{p.label}</button>)}
        </div>
        <div className="archive-fields">
          <label className="field">Signed from
            <input className="input" type="date" value={choices.from} disabled={busy} required onChange={e => set({ from: e.target.value })} /></label>
          <label className="field">To (included)
            <input className="input" type="date" value={choices.to} disabled={busy} required onChange={e => set({ to: e.target.value })} /></label>
          <label className="field">Notebook
            <select className="select" value={choices.notebook_id ?? ''} disabled={busy} onChange={e => set({ notebook_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">All notebooks</option>
              {opts.notebooks.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
            </select></label>
          <label className="field">Author
            <select className="select" value={choices.author_id ?? ''} disabled={busy} onChange={e => set({ author_id: e.target.value ? Number(e.target.value) : null })}>
              <option value="">Everyone</option>
              {opts.authors.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select></label>
          <label className="field">Page size
            <select className="select" value={choices.page_size} disabled={busy} onChange={e => set({ page_size: e.target.value as Choices['page_size'] })}>
              <option value="letter">US Letter</option><option value="a4">A4</option>
            </select></label>
        </div>
        <p className={`archive-count${problem ? ' error' : ''}`} role="status" aria-live="polite">
          {problem || (preview ? <><b>{preview.records ? countLine(preview) : 'No records you can open'}</b> signed {preview.phrase}
            {preview.records ? (preview.files ? `, with ${preview.files} characterisation file${preview.files === 1 ? '' : 's'} to draw.` : '.')
              : ' with these choices: nothing to archive.'}</> : 'Counting…')}
        </p>
        <p className="help" style={{ margin: 0 }}>Only records you can open. Witnessed records and those awaiting a witness both go in, and each
          says which. Voided records go in too, marked <b>VOIDED</b> with the reason and date. Drafts are left out.</p>
        <div className="row">
          <button className="btn primary" onClick={start} disabled={starting || busy || !!problem || !preview?.records}>
            {busy ? 'Making the archive…' : 'Make the archive'}</button>
          {job && !busy && <span className="faint archive-note">Making a new one replaces the one below.</span>}
        </div>
      </section>

      {job && <JobPanel job={job} drawing={drawing} onDownload={() => download(job)} onDiscard={discard} />}
    </div>
  )
}

function JobPanel({ job, drawing, onDownload, onDiscard }: { job: ArchiveJob; drawing?: number; onDownload: () => void; onDiscard: () => void }) {
  const busy = active(job.state)
  const unshown = job.spectra_done - job.spectra_drawn
  return (
    <section className="panel panel-pad stack archive-job" aria-labelledby="archive-job-title" style={{ gap: 10 }}>
      <div className="spread" style={{ alignItems: 'baseline', gap: 8 }}>
        <h2 className="si-h2" id="archive-job-title">{job.title}</h2>
        <span className="faint">{job.scope} · {job.params.page_size === 'a4' ? 'A4' : 'US Letter'} · started {when(job.created_at)}</span>
      </div>
      <p style={{ margin: 0 }}>{countLine(job.counts)}.</p>
      {busy && <>
        <progress className="archive-progress" max={1} value={fraction(job)} aria-label="Progress" />
        <p className="notice" role="status" aria-live="polite">{progressLine(job, drawing)}…
          {job.state === 'drawing' && <span className="faint"> Keep this page open while the spectra are drawn; laying out the PDF then
            carries on without it.</span>}</p>
      </>}
      {job.state === 'done' && <div className="notice archive-done row" role="status">
        <span><b>Ready:</b> {job.pages} pages, {megabytes(job.bytes)}{job.spectra_drawn ? `, ${job.spectra_drawn} spectr${job.spectra_drawn === 1 ? 'um' : 'a'} drawn` : ''}
          {unshown > 0 ? `; ${unshown} file${unshown === 1 ? '' : 's'} couldn't be drawn (listed by name and fingerprint)` : ''}.
          Kept here for a day.</span>
        <button className="btn primary" onClick={onDownload}>Download PDF</button>
      </div>}
      {(job.state === 'stopped' || job.state === 'failed') && <p className="notice warn" role="alert">{progressLine(job)}</p>}
      {!busy && <div><button className="btn ghost sm" onClick={onDiscard}>Delete it now</button></div>}
    </section>
  )
}

/** Draw one spectrum as the viewer does and give it to the job; a file the viewer can't draw is named instead. */
async function drawOne(jobId: string, s: ArchiveSpectrum): Promise<void> {
  const skip = (reason: string) => api.post(`/api/archive/jobs/${jobId}/spectra/${s.attachment_id}/skip`, { reason: reason.slice(0, 280) })
  if (!canViewSpectrum({ filename: s.filename, size: s.size })) return void await skip('not a kind of file the spectrum viewer draws')
  let png: Uint8Array
  try {
    const buf = await (await api.raw(`/api/attachments/${s.attachment_id}?inline=true`)).arrayBuffer()
    const p = await prepareSpectrum(buf, s.filename, s.analysis as any, {
      expectedH: /^\s*(1H|¹H)/i.test(s.technique) ? hydrogensInFormula(s.product?.formula) : null,
      productMW: s.product?.mw ?? null, productSmiles: s.product?.smiles,
    })
    png = await spectrumPng(p, PRINT)
  } catch (e: any) {
    if (e?.status === 0) throw e  // Benchtop unreachable: stop here, and carry on when the page is opened again
    return void await skip(`the spectrum viewer couldn't read it${e?.message ? `: ${e.message}` : ''}`)
  }
  const form = new FormData()
  form.append('file', new Blob([png], { type: 'image/png' }), `${s.attachment_id}.png`)
  await api.put(`/api/archive/jobs/${jobId}/spectra/${s.attachment_id}`, form)
}
