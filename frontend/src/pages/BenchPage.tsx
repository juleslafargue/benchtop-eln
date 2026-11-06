import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName, fmt, loadAttachmentImages, unitLabel, type Attachment, type Experiment, type StoichRow } from '../api'
import { logLine, parseTimer, startTimer, fmtDuration } from '../bench'
import { TimerTray } from '../components/BenchTools'
import Scanner from '../components/Scanner'
import { lotProblem, mass, rowLine, rowsForLot, vol, withBottle, type ScanAnswer } from '../scan'

/** Scan a bottle at the bench: is it one this reaction needs, how much to weigh, and is it fit to use? The reaction
 *  table can be told to use it for its row. That save says which version of the table it changes, so an edit made
 *  meanwhile on the computer is never overwritten: it's refused, and the page reloads to show the table as it is. */
function CheckBottle({ exp, editable, reload }: { exp: Experiment; editable: boolean; reload: () => Promise<unknown> }) {
  const [scanning, setScanning] = useState(false)
  const [answer, setAnswer] = useState<ScanAnswer | null>(null)
  const [picked, setPicked] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState('')
  const [err, setErr] = useState('')
  const entry = exp.entries.find(e => e.kind === 'reaction' && e.content?.rows?.length)
  const rows: StoichRow[] = entry?.content?.rows || []
  const lot = answer?.lots.find(l => l.id === picked)

  const lookUp = async (code: string) => {
    setScanning(false); setSaid(''); setErr('')
    try {
      const a = await api.get<ScanAnswer>(`/api/scan?code=${encodeURIComponent(code)}`)
      setAnswer(a); setPicked(a.lots.length === 1 ? a.lots[0].id : null)
    } catch (e: any) { setErr(e.message) }
  }
  const use = async (index: number) => {
    if (!entry || !lot) return
    const next = withBottle(rows, index, lot)
    if (!next) { setErr('The reaction table changed; check the row again.'); return }
    setBusy(true); setErr(''); setSaid('')
    try {
      // the version shown here: if the table was changed elsewhere since, the server refuses (409) instead of overwriting it
      await api.put(`/api/entries/${entry.id}`, { content: { ...entry.content, rows: next }, expected_version: entry.version })
      setSaid(`${rows[index].name || 'The row'} now uses this bottle.`)
    } catch (e: any) {
      setErr(/elsewhere/.test(e.message) ? 'The reaction table was changed on another screen since this page loaded. It has been reloaded: check the row and try again.' : e.message)
    } finally { await reload().catch(() => {}); setBusy(false) }
  }

  const problem = lot ? lotProblem(lot) : null
  const usable = !!lot && lot.status === 'available'
  const matches = lot ? rowsForLot(rows, lot).filter(x => x.row.role !== 'product') : []
  const isProduct = !!lot && !matches.length && rowsForLot(rows, lot).length > 0
  return (
    <section className="bench-card check-bottle">
      <h2 className="fld-cap">Check a bottle</h2>
      <button className={`btn bench-big${answer ? '' : ' primary'}`} onClick={() => setScanning(true)}>{answer ? 'Scan another bottle' : 'Scan a bottle'}</button>
      <div role="status" className="stack" style={{ gap: 8 }}>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
        {said && <p className="sp-note" style={{ margin: 0 }}><span>{said}</span></p>}
        {answer && !lot && (answer.lots.length ? <>
          <p className="check-line"><b>{answer.lots.length} bottles</b> have this barcode. Which one is in your hand?</p>
          {answer.lots.map(l => <button key={l.id} className="btn lot-pick" onClick={() => setPicked(l.id)}>
            <b>{l.material.name}</b><span>{fmt(l.amount, 3)} {unitLabel(l.unit)}</span>
            <span className="faint">{[l.location || 'no location', l.status, l.supplier].filter(Boolean).join(' · ')}</span></button>)}
        </> : answer.record ? <p className="check-line">{answer.record.code} is {answer.record.kind === 'sample' ? `a sample of ${answer.record.experiment}` : `${answer.record.kind === 'experiment' ? 'an experiment' : 'a material'}, ${answer.record.title}`}, not a bottle.{' '}
          <Link to={answer.record.kind === 'material' ? `/materials/${answer.record.id}` : `/experiments/${answer.record.id}`}>Open it</Link></p>
          : <p className="check-line">No lot has the barcode <b>{answer.code}</b>.</p>)}
        {lot && <>
          {problem && <p className="risk-warn danger bottle-bad" style={{ margin: 0 }}>{problem}</p>}
          <p className="check-line">This bottle: <b>{lot.material.name}</b>, {fmt(lot.amount, 3)} {unitLabel(lot.unit)} left{lot.location ? ` · ${lot.location}` : ''}
            <span className="faint" style={{ display: 'block' }}>{lot.barcode}{lot.purity != null ? ` · ${lot.purity} %` : ''}</span></p>
          {matches.map(({ row, index }) => <div key={index} className="stack" style={{ gap: 6 }}>
            <p className="check-line"><b>{rowLine(row)}</b></p>
            {row.lot_id === lot.id ? <p className="faint" style={{ margin: 0 }}>The reaction table already uses this bottle for it.</p> : <>
              <p className="faint" style={{ margin: 0 }}>{row.lot_id ? 'The reaction table names a different bottle for it.' : 'The reaction table names no bottle for it yet.'}
                {lot.purity != null && lot.purity !== row.purity ? ` This one is ${lot.purity} % pure (the table has ${row.purity ?? 100} %), so the amount to weigh changes.` : ''}</p>
              {(row as { deducted?: unknown }).deducted ? <p className="faint" style={{ margin: 0 }}>Its amount was already taken from another bottle; undo that in the full experiment first.</p>
                : editable && usable && <button className="btn primary bench-big" disabled={busy} onClick={() => use(index)}>{busy ? 'Saving…' : 'Use this bottle for it'}</button>}
            </>}
          </div>)}
          {!matches.length && (isProduct ? <p className="check-line">This bottle is {lot.material.name}, this reaction's product.</p>
            : rows.length ? <p className="risk-warn warning bottle-bad" style={{ margin: 0 }}>This bottle is {lot.material.name}, which isn't in this reaction.</p>
            : <p className="faint" style={{ margin: 0 }}>This experiment has no reaction table to check it against.</p>)}
        </>}
      </div>
      {scanning && <Scanner title="Check a bottle" onClose={() => setScanning(false)} onCode={lookUp} />}
    </section>
  )
}

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

      <CheckBottle exp={exp} editable={editable} reload={load} />

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
