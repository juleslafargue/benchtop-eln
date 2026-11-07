import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useApp } from '../App'
import { ago, api, ApiError, depictUrl, type ExperimentSummary } from '../api'
import { Mol } from '../components/bits'
import Modal from '../components/Modal'
import { checkLines, siFileName, siModel, techniqueLabel, type SiCompound, type SiDocument, type SiImage, type SiItem } from '../si'
import { hydrogensInFormula } from '../spectra'
import { header, prepareSpectrum, spectrumPng } from '../spectrumDraw'
import { canViewSpectrum } from '../viewable'

interface Listed { id: number; title: string; compounds: number; page_size: string; updated_at: string }
type Draft = Pick<SiDocument, 'title' | 'general' | 'page_size' | 'items'>

const GENERAL_HINT = 'What usually goes here: where reagents and solvents came from and how they were dried or purified; '
  + 'how reactions were run and followed (inert atmosphere, TLC plates and stains); the silica used for chromatography; '
  + 'and the instruments: NMR (field, temperature, how shifts are referenced), HRMS (instrument, ionisation), IR, melting points.'

/** Spectra in the Word file: the viewer's Export PNG drawing, at a size whose labels stay readable across a page. */
const FIGURE = { w: 820, h: 380, scale: 3 }

export default function SupportingInfo() {
  const { id } = useParams()
  return id ? <SiEditor id={Number(id)} /> : <SiList />
}

// ------------------------------------------------------------------ the list of documents

function SiList() {
  const { canWrite, toast } = useApp()
  const nav = useNavigate()
  const [docs, setDocs] = useState<Listed[] | null>(null)
  const [err, setErr] = useState('')
  const [title, setTitle] = useState('')
  useEffect(() => { api.get<Listed[]>('/api/si-documents').then(setDocs).catch(e => setErr(e.message)) }, [])
  const create = async () => {
    try {
      const d = await api.post<SiDocument>('/api/si-documents', { title: title.trim() })
      nav(`/si/${d.id}`)
    } catch (e: any) { toast(e.message) }
  }
  return (
    <div className="stack" style={{ gap: 18 }}>
      <div>
        <h1 className="page-title">Supporting information</h1>
        <p className="lede">One Word document per paper: each compound's procedure, characterisation data and spectra, taken from
          its experiment, with a check of what journals usually ask for. Building it never changes an experiment. Only you can see your documents.</p>
      </div>
      {canWrite && <form className="panel panel-pad row si-new" onSubmit={e => { e.preventDefault(); create() }}>
        <label className="field" style={{ flex: '1 1 260px' }}>Paper title
          <input className="input" value={title} onChange={e => setTitle(e.target.value)} placeholder="e.g. Suzuki couplings of electron-poor aryl bromides" maxLength={300} /></label>
        <button className="btn primary" type="submit" style={{ alignSelf: 'flex-end' }}>New document</button>
      </form>}
      {err && <p className="error">{err}</p>}
      <div className="panel">
        {docs?.map(d => (
          <Link key={d.id} to={`/si/${d.id}`} className="list-row si-list-row">
            <span><b style={{ fontWeight: 600 }}>{d.title || 'Untitled'}</b></span>
            <span className="faint">{d.compounds} compound{d.compounds === 1 ? '' : 's'} · {d.page_size === 'a4' ? 'A4' : 'US Letter'}</span>
            <span className="faint">changed {ago(d.updated_at)}</span>
          </Link>
        ))}
        {docs && !docs.length && <p className="empty">No documents yet. Start one for each paper; you can add compounds over weeks.</p>}
        {!docs && !err && <p className="empty">Loading…</p>}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ one document

function SiEditor({ id }: { id: number }) {
  const { canWrite, toast } = useApp()
  const nav = useNavigate()
  const [doc, setDoc] = useState<SiDocument | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [err, setErr] = useState('')
  const [saveState, setSaveState] = useState<'saved' | 'saving' | 'unsaved' | 'conflict' | 'failed'>('saved')
  const [picking, setPicking] = useState(false)
  const [progress, setProgress] = useState('')
  const [built, setBuilt] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const version = useRef(0)
  const draftRef = useRef<Draft | null>(null)
  const savedRef = useRef<Draft | null>(null)  // the draft the server has
  const inflight = useRef<Promise<void> | null>(null)
  const queued = useRef(false)

  const load = useCallback(() => {
    api.get<SiDocument>(`/api/si-documents/${id}`).then(d => {
      version.current = d.version; setDoc(d)
      const dr = { title: d.title, general: d.general, page_size: d.page_size, items: d.items }
      draftRef.current = savedRef.current = dr; setDraft(dr); setSaveState('saved')
    }).catch(e => setErr(e.message))
  }, [id])
  useEffect(load, [load])

  // saved a moment after each change; one save at a time, each from the version the last one left
  const save = useCallback(async (): Promise<void> => {
    if (inflight.current) { queued.current = true; return inflight.current }
    const body = draftRef.current
    if (!body) return
    setSaveState('saving')
    inflight.current = (async () => {
      try {
        const d = await api.put<SiDocument>(`/api/si-documents/${id}`, { ...body, expected_version: version.current })
        version.current = d.version; setDoc(d); savedRef.current = body
        setSaveState(draftRef.current === body ? 'saved' : 'unsaved')
      } catch (e: any) {
        if (e instanceof ApiError && e.status === 409) setSaveState('conflict')
        else { setSaveState('failed'); toast(e.message) }
        queued.current = false
      } finally { inflight.current = null }
    })()
    await inflight.current
    if (queued.current) { queued.current = false; await save() }
  }, [id, toast])
  const timer = useRef<number>()
  const change = (p: Partial<Draft>) => {
    if (!draftRef.current) return
    const next = { ...draftRef.current, ...p }
    draftRef.current = next; setDraft(next); setSaveState('unsaved')
    clearTimeout(timer.current); timer.current = window.setTimeout(() => { save() }, 700)
  }
  const flush = async () => {
    clearTimeout(timer.current)
    if (inflight.current) await inflight.current
    if (draftRef.current !== savedRef.current) await save()
  }
  useEffect(() => () => clearTimeout(timer.current), [])

  const byExp = useMemo(() => new Map((doc?.compounds || []).map(c => [c.experiment_id, c])), [doc])
  if (err) return <p className="error">{err}</p>
  if (!doc || !draft) return <p className="faint">Loading…</p>

  const items = draft.items
  const setItem = (k: number, p: Partial<SiItem>) => change({ items: items.map((it, j) => (j === k ? { ...it, ...p } : it)) })
  const move = (k: number, to: number) => {
    if (to < 0 || to >= items.length) return
    const next = [...items]; const [it] = next.splice(k, 1); next.splice(to, 0, it); change({ items: next })
  }
  const add = (e: ExperimentSummary) => {
    if (items.some(it => it.experiment_id === e.id)) return
    change({ items: [...items, { experiment_id: e.id, number: '', name: '', spectra: null }] })
  }
  const shown = items.map(it => byExp.get(it.experiment_id)).filter((c): c is SiCompound => !!c)
  const lines = checkLines(shown.map(c => ({ ...c, number: items.find(it => it.experiment_id === c.experiment_id)?.number || c.number })))

  const build = async () => {
    setBuilt(''); setProgress('Saving…')
    try {
      await flush()
      const d = await api.get<SiDocument>(`/api/si-documents/${id}`)
      setDoc(d)
      const docx = import('../siDocx')  // the Word library loads only now, while the pictures are drawn
      const comps = d.compounds.filter(c => c.visible)
      const structures = new Map<number, SiImage | null>()
      const withStructure = comps.filter(c => c.product?.smiles)
      for (const [k, c] of withStructure.entries()) {
        setProgress(`Drawing structure ${k + 1} of ${withStructure.length}…`)
        structures.set(c.index, await structurePng(c.product!.smiles!).catch(() => null))
      }
      const todo = comps.flatMap(c => (c.spectra || []).filter(s => s.included).map(s => ({ c, s })))
      const spectra = new Map<number, { image?: SiImage; header?: string; error?: string }>()
      for (const [k, { c, s }] of todo.entries()) {
        setProgress(`Drawing spectrum ${k + 1} of ${todo.length}…`)
        await new Promise(r => setTimeout(r, 0))  // let the page answer between spectra
        if (!canViewSpectrum({ filename: s.filename, size: s.size })) { spectra.set(s.attachment_id, { error: 'the spectrum viewer can’t draw this kind of file' }); continue }
        try {
          const buf = await (await api.raw(`/api/attachments/${s.attachment_id}?inline=true`)).arrayBuffer()
          const p = await prepareSpectrum(buf, s.filename, s.analysis as any, {
            expectedH: /^\s*(1H|¹H)/i.test(s.technique) ? hydrogensInFormula(c.product?.formula) : null,
            productMW: c.product?.mw ?? null, productSmiles: c.product?.smiles,
          })
          spectra.set(s.attachment_id, { image: { data: await spectrumPng(p, FIGURE), width: FIGURE.w * FIGURE.scale, height: FIGURE.h * FIGURE.scale }, header: header(p.spec) })
        } catch (e: any) { spectra.set(s.attachment_id, { error: e.message }) }
      }
      setProgress('Putting the Word file together…')
      const { buildSiDocx } = await docx
      const bytes = await buildSiDocx(siModel(d, { structures, spectra }))
      const name = siFileName(d.title)
      const url = URL.createObjectURL(new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }))
      const a = document.createElement('a'); a.href = url; a.download = name; a.click()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
      const unshown = [...spectra.values()].filter(v => !v.image).length
      setBuilt(`Downloaded “${name}”: ${comps.length} compound${comps.length === 1 ? '' : 's'}, ${todo.length - unshown} spectr${todo.length - unshown === 1 ? 'um' : 'a'}`
        + (unshown ? `; ${unshown} file${unshown === 1 ? '' : 's'} couldn't be drawn (named in the document).` : '.'))
    } catch (e: any) { toast(`The Word file wasn't made: ${e.message}`) } finally { setProgress('') }
  }

  const remove = async () => {
    try { await api.del(`/api/si-documents/${id}`); nav('/si') } catch (e: any) { toast(e.message) }
  }

  return (
    <div className="stack si-page" style={{ gap: 18 }}>
      <div className="spread">
        <div style={{ minWidth: 0, flex: '1 1 320px' }}>
          <h1 className="page-title">{draft.title || 'Untitled'}</h1>
          <p className="lede">Supporting information · {saveState === 'saved' ? 'saved' : saveState === 'saving' ? 'saving…' : saveState === 'unsaved' ? 'not saved yet'
            : saveState === 'conflict' ? 'changed in another window' : 'couldn’t save'}</p>
        </div>
        <div className="row">
          <button className="btn primary" onClick={build} disabled={!!progress || !shown.some(c => c.visible)}>{progress ? 'Building…' : 'Download Word file'}</button>
        </div>
      </div>
      {saveState === 'conflict' && <p className="notice" role="alert">This document was changed in another window or tab. <button className="btn sm" onClick={load}>Reload it</button> (your last change here isn't saved).</p>}
      {(progress || built) && <p className="notice" role="status" aria-live="polite">{progress || built}</p>}

      <div className="si-doc">
        <div className="stack" style={{ gap: 18, minWidth: 0 }}>
          <section className="panel panel-pad stack" style={{ gap: 12 }}>
            <label className="field">Paper title
              <input className="input" value={draft.title} maxLength={300} disabled={!canWrite} onChange={e => change({ title: e.target.value })} /></label>
            <label className="field">Page size
              <select className="select" style={{ maxWidth: 220 }} value={draft.page_size} disabled={!canWrite} onChange={e => change({ page_size: e.target.value as 'letter' | 'a4' })}>
                <option value="letter">US Letter</option><option value="a4">A4</option></select></label>
            <label className="field">General information
              <textarea className="input si-general" rows={7} value={draft.general} maxLength={50_000} disabled={!canWrite}
                placeholder="Materials, methods and instruments, in your own words. A blank line starts a new paragraph."
                onChange={e => change({ general: e.target.value })} /></label>
            <p className="faint si-hint">{GENERAL_HINT}</p>
          </section>

          <section className="panel">
            <div className="spread si-head">
              <h2 className="si-h2">Compounds</h2>
              {canWrite && <button className="btn" onClick={() => setPicking(true)}>Add a compound</button>}
            </div>
            {!items.length && <p className="empty">No compounds yet. Add the experiment each one was made in.</p>}
            <ol className="si-compounds">
              {items.map((it, k) => {
                const c = byExp.get(it.experiment_id)
                const label = it.number || c?.name || `compound ${k + 1}`
                return (
                  <li key={it.experiment_id} className="si-compound">
                    <div className="si-thumb mat-thumb">{c?.product?.smiles ? <Mol smiles={c.product.smiles} w={192} h={120} alt={`Structure of ${c.name}`} /> : <span className="faint">No structure</span>}</div>
                    <div style={{ minWidth: 0 }}>
                      <div className="si-fields">
                        <label className="fld"><span className="fld-cap">Number</span>
                          <input className="input si-num" value={it.number} maxLength={20} placeholder="3a" disabled={!canWrite}
                            onChange={e => setItem(k, { number: e.target.value })} /></label>
                        <label className="fld"><span className="fld-cap">Name in the paper</span>
                          <input className="input" value={it.name} maxLength={300} placeholder={c?.name_default || c?.name || 'Name'} disabled={!canWrite}
                            onChange={e => setItem(k, { name: e.target.value })} /></label>
                      </div>
                      {c && !c.visible && <p className="error" style={{ margin: '8px 0 0' }}>{c.eid} is no longer one you can open, so it's left out of the document.</p>}
                      {c?.visible && <div className="faint si-from">From <Link to={`/experiments/${c.experiment_id}`}>{c.eid}</Link> · {c.title}{' '}
                        <span className={`status ${c.status}`}>{c.status === 'open' ? 'not signed' : c.status}</span></div>}
                      {!c && <div className="faint si-from">Saving…</div>}
                      {c?.visible && !!c.spectra?.length && <div className="si-spectra" role="group" aria-label={`Spectra of ${label} in the document`}>
                        {c.spectra.map(s => {
                          const on = it.spectra ? it.spectra.includes(s.attachment_id) : s.included
                          const toggle = () => {
                            const current = c.spectra!.filter(x => (it.spectra ? it.spectra.includes(x.attachment_id) : x.included)).map(x => x.attachment_id)
                            setItem(k, { spectra: on ? current.filter(x => x !== s.attachment_id) : [...current, s.attachment_id] })
                          }
                          return <label key={s.attachment_id} className={`chip${on ? ' on' : ''}`} title={s.filename}>
                            <input type="checkbox" checked={on} disabled={!canWrite} onChange={toggle} />
                            {techniqueLabel(s.technique) || 'Spectrum'} <span className="faint">{s.filename}</span></label>
                        })}
                      </div>}
                      {c?.visible && !c.spectra?.length && <div className="faint si-from">No spectrum files in this record.</div>}
                    </div>
                    {canWrite && <div className="si-moves">
                      <button className="btn sm" onClick={() => move(k, k - 1)} disabled={k === 0} aria-label={`Move ${label} up`}>↑ Up</button>
                      <button className="btn sm" onClick={() => move(k, k + 1)} disabled={k === items.length - 1} aria-label={`Move ${label} down`}>↓ Down</button>
                      <button className="btn sm ghost" onClick={() => change({ items: items.filter((_, j) => j !== k) })} aria-label={`Remove ${label}`}>Remove</button>
                    </div>}
                  </li>
                )
              })}
            </ol>
          </section>
          {canWrite && <div>{confirmDelete
            ? <p className="notice row">Delete this document? The experiments stay as they are.
              <button className="btn sm danger" onClick={remove}>Delete document</button><button className="btn sm" onClick={() => setConfirmDelete(false)}>Cancel</button></p>
            : <button className="btn ghost sm" onClick={() => setConfirmDelete(true)}>Delete document</button>}</div>}
        </div>

        <aside className="panel panel-pad si-check" aria-labelledby="si-check-title">
          <h2 className="si-h2" id="si-check-title">Before you download</h2>
          <p className="faint" style={{ marginTop: 4 }}>What journals usually ask for that the records don't have yet. It never stops you downloading.</p>
          {!shown.length ? <p className="faint">Add compounds to see what's missing.</p>
            : lines.length ? <ul className="check-list si-check-list">{lines.map(l => <li key={l} className="bad"><span>•</span> {l}</li>)}</ul>
              : <p className="pill ok" style={{ display: 'inline-block' }}>Nothing missing that journals usually ask for.</p>}
        </aside>
      </div>
      {picking && <ExperimentPicker taken={new Set(items.map(it => it.experiment_id))} onPick={add} onClose={() => setPicking(false)} />}
    </div>
  )
}


/** The structure as a PNG, drawn at three times its size so it prints sharply. */
async function structurePng(smiles: string): Promise<SiImage> {
  const w = 300, h = 180, scale = 3
  const img = new Image()
  img.src = depictUrl(smiles, w, h)
  await img.decode()
  const c = document.createElement('canvas'); c.width = w * scale; c.height = h * scale
  const g = c.getContext('2d')!
  g.fillStyle = '#ffffff'; g.fillRect(0, 0, c.width, c.height)
  g.drawImage(img, 0, 0, c.width, c.height)
  const blob = await new Promise<Blob | null>(res => c.toBlob(res, 'image/png'))
  if (!blob) throw new Error('no picture')
  return { data: new Uint8Array(await blob.arrayBuffer()), width: c.width, height: c.height }
}

// ------------------------------------------------------------------ choosing an experiment

function ExperimentPicker({ taken, onPick, onClose }: { taken: Set<number>; onPick: (e: ExperimentSummary) => void; onClose: () => void }) {
  const { notebooks } = useApp()
  const [exps, setExps] = useState<(ExperimentSummary & { sample_codes?: string[] })[] | null>(null)
  const [q, setQ] = useState('')
  const [nb, setNb] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => { api.get<ExperimentSummary[]>('/api/experiments?limit=1000').then(setExps).catch(e => setErr(e.message)) }, [])
  const found = useMemo(() => {
    const t = q.trim().toLowerCase()
    const num = /^(exp-?)?0*(\d+)$/i.exec(t)
    return (exps || []).filter(e => (!nb || String(e.notebook_id) === nb) && (!t
      || (num && e.id === Number(num[2])) || e.eid.toLowerCase().includes(t) || e.title.toLowerCase().includes(t)
      || (e.sample_codes || []).some(s => s.toLowerCase().includes(t)))).slice(0, 40)
  }, [exps, q, nb])
  return (
    <Modal title="Add a compound" onClose={onClose} footer={<button className="btn" onClick={onClose}>Done</button>}>
      <p className="faint" style={{ marginTop: 0 }}>Choose the experiment the compound was made in. Its product, procedure, data and spectra come from there.</p>
      <div className="row si-pick-bar">
        <input className="input" autoFocus placeholder="EXP number, title or sample code" value={q} onChange={e => setQ(e.target.value)} aria-label="Find an experiment" style={{ flex: '1 1 200px' }} />
        <select className="select" value={nb} onChange={e => setNb(e.target.value)} aria-label="Notebook" style={{ flex: '0 1 200px' }}>
          <option value="">All notebooks</option>{notebooks.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}</select>
      </div>
      {err && <p className="error">{err}</p>}
      <ul className="si-pick-list">
        {found.map(e => {
          const added = taken.has(e.id)
          return <li key={e.id}><button className="btn ghost si-pick" disabled={added} onClick={() => onPick(e)}>
            <b>{e.eid}</b> <span>{e.title}</span> <span className="faint">{e.notebook_name || ''}{added ? ' · added' : ''}</span></button></li>
        })}
        {exps && !found.length && <li className="faint" style={{ padding: 8 }}>No experiment matches.</li>}
        {!exps && !err && <li className="faint" style={{ padding: 8 }}>Loading…</li>}
      </ul>
    </Modal>
  )
}
