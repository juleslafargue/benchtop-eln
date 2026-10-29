import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName, fmt, loadAttachmentImages, type Attachment, type Experiment } from '../api'
import { logLine, parseTimer, startTimer, fmtDuration } from '../bench'
import { TimerTray } from '../components/BenchTools'

const mass = (mg: number) => (mg >= 1000 ? `${fmt(mg / 1000, 2)} g` : `${fmt(mg, 1)} mg`)
const vol = (ml: number) => (ml >= 1000 ? `${fmt(ml / 1000, 2)} L` : `${fmt(ml, ml < 1 ? 3 : 1)} mL`)
const QUICK = ['Started heating', 'Added dropwise', 'TLC taken', 'Colour change', 'Quenched', 'Work-up started']
const PRESETS = [5, 10, 15, 30, 60]

/** The phone view of an experiment: photos, one-line notes and timers, big and one-handed. */
export default function BenchPage() {
  const { id } = useParams()
  const { user, toast, canWrite } = useApp()
  const [exp, setExp] = useState<Experiment | null>(null)
  const [err, setErr] = useState('')
  const [text, setText] = useState('')
  const [busy, setBusy] = useState('')
  const [pending, setPending] = useState<{ file: File; url: string } | null>(null)
  const [caption, setCaption] = useState('')
  const camera = useRef<HTMLInputElement>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const load = useCallback(() => api.get<Experiment>(`/api/experiments/${id}`).then(e => { setExp(e); setErr('') }).catch(e => setErr(e.message)), [id])
  useEffect(() => { load(); const iv = setInterval(load, 30_000); return () => clearInterval(iv) }, [load])
  useEffect(() => { loadAttachmentImages(logRef.current) })
  if (err) return <main className="bench-page"><p className="error">{err}</p><Link to="/">Go to Benchtop</Link></main>
  if (!exp) return <main className="bench-page"><p className="faint">Loading…</p></main>

  const editable = canWrite && exp.status === 'open' && (exp.can_edit ?? exp.owner.id === user.id)
  const log = exp.entries.find(e => e.kind === 'text' && (e.content?.bench_log || e.title === 'Bench log'))
  const rx = exp.entries.find(e => e.kind === 'reaction' && e.content?.rows?.length)?.content
  const timer = parseTimer(text)

  const submit = async () => {
    const t = text.trim()
    if (!t) return
    setBusy('log')
    try {
      if (timer) { startTimer(exp.id, exp.eid, timer.seconds, timer.label); toast(`Timer started: ${timer.label}`) }
      else { await logLine(exp.id, t); toast('Logged') }
      setText(''); load()
    } catch (e: any) { toast(e.message) } finally { setBusy('') }
  }
  const upload = async () => {
    if (!pending) return
    setBusy('photo')
    try {
      const fd = new FormData()
      const ext = (pending.file.type.split('/')[1] || 'jpg').replace('jpeg', 'jpg')
      const now = new Date()
      const name = /^image\.|^IMG_|^\d+$/.test(pending.file.name) || !pending.file.name ? `photo_${now.toISOString().slice(0, 16).replace(/[:T]/g, '-')}.${ext}` : pending.file.name
      fd.append('file', pending.file, name)
      const att = await api.post<Attachment>(`/api/experiments/${exp.id}/attachments`, fd)
      await logLine(exp.id, caption.trim() || 'Photo', att.id)
      URL.revokeObjectURL(pending.url); setPending(null); setCaption(''); toast('Photo added'); load()
    } catch (e: any) { toast(e.message) } finally { setBusy('') }
  }

  return (
    <main className="bench-page">
      <header className="bench-head">
        <div className="faint">{exp.eid} · {displayName(exp.owner)}</div>
        <h1>{exp.title}</h1>
        <div className="row" style={{ gap: 8 }}>
          <span className={`status ${exp.status}`}>{exp.status === 'open' ? 'Draft' : exp.status}</span>
          <Link to={`/experiments/${exp.id}`} className="faint">Full experiment</Link>
        </div>
      </header>
      {!editable && <p className="notice">{exp.status === 'open' ? "You can view this experiment but not add to it." : 'This record is signed, so it can no longer be changed.'}</p>}

      {editable && <>
        <section className="bench-card">
          <input ref={camera} type="file" accept="image/*" capture="environment" hidden
            onChange={e => { const f = e.target.files?.[0]; if (f) setPending({ file: f, url: URL.createObjectURL(f) }); e.target.value = '' }} />
          {!pending
            ? <button className="btn primary bench-big" onClick={() => camera.current?.click()}>📷 Take a photo</button>
            : <div className="stack" style={{ gap: 8 }}>
              <img src={pending.url} alt="Photo to add" className="bench-preview" />
              <input className="input" value={caption} onChange={e => setCaption(e.target.value)} placeholder="What is it? e.g. TLC after 1 h, 3:1 hex/EtOAc" />
              <div className="row" style={{ gap: 8 }}>
                <button className="btn primary grow" onClick={upload} disabled={busy === 'photo'}>{busy === 'photo' ? 'Adding…' : 'Add to the record'}</button>
                <button className="btn" onClick={() => { URL.revokeObjectURL(pending.url); setPending(null) }}>Retake</button>
              </div>
            </div>}
        </section>

        <section className="bench-card">
          <label className="fld-cap" htmlFor="bench-note">Log a note</label>
          <textarea id="bench-note" className="input" rows={2} value={text} onChange={e => setText(e.target.value)}
            placeholder="What just happened? (“20 min stir” starts a timer)" />
          <div className="chips">{QUICK.map(q => <button key={q} className="chip" onClick={() => setText(t => (t ? `${t} ` : '') + q)}>{q}</button>)}</div>
          <button className="btn primary bench-big" onClick={submit} disabled={!text.trim() || busy === 'log'}>
            {timer ? `Start ${fmtDuration(timer.seconds)} timer` : busy === 'log' ? 'Logging…' : 'Log it'}</button>
        </section>

        <section className="bench-card">
          <div className="fld-cap">Timer</div>
          <div className="bench-presets">{PRESETS.map(m => <button key={m} className="btn" onClick={() => { startTimer(exp.id, exp.eid, m * 60, `${m} min`); toast(`${m} min timer started`) }}>{m} min</button>)}</div>
          <TimerTray compact />
        </section>
      </>}

      {rx && <section className="bench-card">
        <h2 className="fld-cap">Reaction</h2>
        <table className="bench-rx"><colgroup><col /><col className="c-eq" /><col className="c-amt" /></colgroup><tbody>
          {rx.rows.map((r: any, i: number) => <tr key={i} className={r.role === 'product' ? 'faint' : ''}>
            <td>{r.name || r.formula}</td><td className="num">{r.equiv != null && r.role !== 'solvent' && r.role !== 'product' ? `${fmt(r.equiv, 2)} eq` : ''}</td>
            <td className="num">{r.role === 'product' ? (r.theoretical_mass_mg ? <>{mass(r.theoretical_mass_mg)}<span className="bench-theor">theor.</span></> : '')
              : r.volume_ml && (r.density || r.role === 'solvent' || r.conc_m) ? vol(r.volume_ml) : r.mass_mg ? mass(r.mass_mg) : ''}</td>
          </tr>)}
        </tbody></table>
      </section>}

      <section className="bench-card">
        <h2 className="fld-cap">Bench log</h2>
        {log?.content?.html ? <div ref={logRef} className="prose bench-log" dangerouslySetInnerHTML={{ __html: log.content.html }} />
          : <p className="faint">Nothing logged yet.</p>}
      </section>
      <p className="faint" style={{ textAlign: 'center', fontSize: 12 }}>Signed in as {displayName(user)} on this phone · <Link to="/account">Account</Link></p>
    </main>
  )
}
