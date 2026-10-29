import { useEffect, useState } from 'react'
import { useApp } from '../App'
import { day, ago, api, type FolderPreview, type Instrument } from '../api'
import Modal from './Modal'

interface ListOut { instruments: Instrument[]; kinds: Record<string, string>; scan_seconds: number; max_mb: number }

const KIND_HELP: Record<string, string> = {
  bruker_nmr: 'Each experiment-number folder (the one holding acqus) arrives as one zip; spectra open in the viewer. The title typed in TopSpin or IconNMR is searched for the experiment number too.',
  agilent_nmr: 'Each .fid folder (VnmrJ, OpenVnmrJ) arrives as one zip. The sample name and comment typed on the spectrometer are searched for the experiment number too.',
  chromatography: 'Vendor data is stored as it is: an Agilent or Bruker .D folder, or a Waters .raw folder, as one zip; Thermo .raw, Sciex .wiff (with its .wiff.scan), Shimadzu and mzML files as they are. PDF and CSV reports inside a .D are also attached on their own, and exports saved beside the data arrive as their own files. Long runs keep writing, so a quiet time of 5 minutes is safer.',
  files: 'Every file arrives on its own: PDF reports, CSV or JCAMP exports, IR, UV, balance printouts.',
}

function Preview({ p }: { p: FolderPreview }) {
  return (
    <div className="folder-preview">
      <p style={{ margin: 0 }}><b>Benchtop can read this folder.</b> {p.items === 0 ? 'Nothing of this type is in it yet.'
        : <>{p.items.toLocaleString()} {p.items === 1 ? 'arrival' : 'arrivals'} in it now{p.scan_seconds >= 1 && <span className="faint"> (took {Math.round(p.scan_seconds)} s to look through)</span>}.
          When you save, these are noted as already there and left alone.</>}</p>
      {p.since && <p style={{ margin: 0 }}>Changed since the date you chose: <b>{p.since.items}</b>. Of those, {p.since.attach} would be attached and {p.since.inbox} would go to the Inbox.</p>}
      {p.hints.map(h => <p key={h} className="notice" style={{ margin: 0 }}>{h}</p>)}
      {p.latest.length > 0 && <>
        <p className="faint" style={{ margin: '4px 0 0', fontSize: 13 }}>The newest, and what would happen to them if they arrived now:</p>
        <ul className="preview-list">{p.latest.map(x => (
          <li key={x.path}><span className="grow mono">{x.path}</span>
            <span className={x.action === 'attach' ? 'ok-text' : 'faint'}>{x.action === 'attach' ? `→ ${x.experiment}` : 'Inbox'}</span></li>))}</ul>
      </>}
    </div>
  )
}

function InstrumentForm({ kinds, editing, onClose, onSaved }: {
  kinds: Record<string, string>; editing: Instrument | null; onClose: () => void; onSaved: () => void
}) {
  const { groups, toast } = useApp()
  const [name, setName] = useState(editing?.name || '')
  const [kind, setKind] = useState(editing?.kind || 'bruker_nmr')
  const [quietTouched, setQuietTouched] = useState(!!editing)
  const [folder, setFolder] = useState(editing?.folder || '')
  const [restrict, setRestrict] = useState((editing?.audience.length || 0) > 0)
  const [aud, setAud] = useState<number[]>(editing?.audience.map(g => g.id) || [])
  const [quiet, setQuiet] = useState(editing?.settle_minutes ?? 2)
  const [older, setOlder] = useState(!!editing?.import_since)
  const [since, setSince] = useState(editing?.import_since ? editing.import_since.slice(0, 10) : '')
  const [preview, setPreview] = useState<FolderPreview | null>(null)
  const [busy, setBusy] = useState<'' | 'check' | 'save'>('')
  const [err, setErr] = useState('')
  const sinceIso = older && since ? new Date(`${since}T00:00:00`).toISOString() : null
  const check = async () => {
    setBusy('check'); setErr(''); setPreview(null)
    try { setPreview(await api.post<FolderPreview>('/api/instruments/check', { folder, kind, import_since: sinceIso })) }
    catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  const save = async () => {
    setBusy('save'); setErr('')
    const body = { name, kind, folder, audience: restrict ? aud : [], settle_minutes: quiet,
      ...(sinceIso ? { import_since: sinceIso } : editing ? { clear_import_since: !older } : {}) }
    try {
      if (editing) await api.patch(`/api/instruments/${editing.id}`, body)
      else await api.post('/api/instruments', body)
      toast(editing ? 'Saved' : `Watching ${name}`); onSaved()
    } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  return (
    <Modal wide title={editing ? `Edit ${editing.name}` : 'Add an instrument folder'} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!!busy || !name.trim() || !folder.trim() || (restrict && !aud.length) || (older && !since)} onClick={save}>
        {busy === 'save' ? 'Checking the folder…' : editing ? 'Save' : 'Start watching'}</button>
    </>}>
      <div className="stack" style={{ gap: 14 }}>
        <div className="form-grid">
          <label className="field">Name<input className="input" value={name} maxLength={80} onChange={e => setName(e.target.value)} placeholder="NMR – Bruker 400" /></label>
          <label className="field">Type<select className="select" value={kind} onChange={e => { setKind(e.target.value); setPreview(null); if (!quietTouched) setQuiet(e.target.value === 'chromatography' ? 5 : 2) }}>
            {Object.entries(kinds).map(([k, label]) => <option key={k} value={k}>{label}</option>)}</select></label>
        </div>
        <p className="faint" style={{ margin: '-6px 0 0', fontSize: 13 }}>{KIND_HELP[kind]}</p>
        <label className="field">Folder
          <div className="row" style={{ gap: 6 }}>
            <input className="input grow mono" value={folder} onChange={e => { setFolder(e.target.value); setPreview(null) }} placeholder="/Volumes/NMR/data/ruizlab" />
            <button className="btn" disabled={!!busy || !folder.trim()} onClick={check}>{busy === 'check' ? 'Checking…' : 'Check folder'}</button>
          </div></label>
        <p className="faint" style={{ margin: '-6px 0 0', fontSize: 13 }}>Any folder this computer can open: a local one, a mounted network drive, or a OneDrive or Dropbox folder.
          On a Mac, select the folder in Finder, press <kbd>⌥⌘C</kbd> (Copy as Pathname) and paste it here. Benchtop only reads it; nothing there is moved or deleted.</p>
        {preview && <Preview p={preview} />}
        <fieldset className="field-group">
          <legend>Who sees files that match no experiment</legend>
          <label className="row" style={{ gap: 8 }}><input type="radio" checked={!restrict} onChange={() => setRestrict(false)} /> Everyone in Benchtop</label>
          <label className="row" style={{ gap: 8 }}><input type="radio" checked={restrict} onChange={() => setRestrict(true)} /> Only members of these groups</label>
          {restrict && <div className="row" style={{ gap: 6, flexWrap: 'wrap', paddingLeft: 24 }}>
            {groups.length === 0 ? <span className="faint">No groups yet: make one under Groups first.</span>
              : groups.map(g => <label key={g.id} className={`chip${aud.includes(g.id) ? ' on' : ''}`}>
                <input type="checkbox" checked={aud.includes(g.id)} onChange={e => setAud(e.target.checked ? [...aud, g.id] : aud.filter(x => x !== g.id))} /> {g.name}</label>)}</div>}
          <p className="faint" style={{ margin: 0, fontSize: 13 }}>File names can give away compounds or projects. For a shared departmental instrument, choose your group.
            Files that name an experiment are only ever shown to people who can see that experiment.</p>
        </fieldset>
        <div className="stack" style={{ gap: 4 }}>
          <label className="inline-field">Wait until a file has been quiet for
            <input className="input inline-num" type="number" min={0.25} max={1440} step={0.5} value={quiet} onChange={e => { setQuiet(Number(e.target.value)); setQuietTouched(true) }} /> minutes</label>
          <p className="faint" style={{ margin: 0, fontSize: 13 }}>Benchtop takes an arrival once nothing in it has changed for this long, so files still being
            copied (USB, OneDrive, a network drive) aren't taken half-finished. 2 minutes suits most; make it longer for slow syncs.</p>
        </div>
        <label className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
          <input type="checkbox" checked={older} onChange={e => { setOlder(e.target.checked); setPreview(null) }} style={{ marginTop: 3 }} />
          <span>Also take files that are already in the folder, if changed since
            <input className="input" type="date" disabled={!older} value={since} onChange={e => { setSince(e.target.value); setPreview(null) }} style={{ width: 160, marginLeft: 8, padding: '1px 8px' }} />
            <br /><span className="faint" style={{ fontSize: 13 }}>Normally only files that appear after you save are taken. Use <b>Check folder</b> first to see how many this would be.</span></span>
        </label>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      </div>
    </Modal>
  )
}

interface OldItem { path: string; gone: boolean; size: number; modified?: string; action?: string; note?: string; experiment?: string | null }

const mb = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`)

/** What was already in a folder when it was added: pick the ones to bring in after all. */
function OldData({ inst, onClose, onTaken }: { inst: Instrument; onClose: () => void; onTaken: () => void }) {
  const { toast } = useApp()
  const [q, setQ] = useState('')
  const [items, setItems] = useState<OldItem[]>([])
  const [total, setTotal] = useState(0)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState<'' | 'load' | 'take'>('load')
  const [err, setErr] = useState('')
  const load = async (offset = 0) => {
    setBusy('load'); setErr('')
    try {
      const r = await api.get<{ total: number; items: OldItem[] }>(`/api/instruments/${inst.id}/old?q=${encodeURIComponent(q)}&offset=${offset}`)
      setItems(offset ? [...items, ...r.items] : r.items); setTotal(r.total)
    } catch (e: any) { setErr(e.message) } finally { setBusy('') }
  }
  useEffect(() => { const t = setTimeout(() => load(0), 250); return () => clearTimeout(t) }, [q]) // eslint-disable-line react-hooks/exhaustive-deps
  const takeable = items.filter(x => !x.gone)
  const allShown = takeable.length > 0 && takeable.every(x => picked.has(x.path))
  const toggle = (p: string) => { const n = new Set(picked); n.has(p) ? n.delete(p) : n.add(p); setPicked(n) }
  const take = async () => {
    setBusy('take'); setErr('')
    try {
      const r = await api.post<{ taken: string[]; gone: string[]; not_old: string[] }>(`/api/instruments/${inst.id}/take`, { paths: [...picked] })
      const skipped = r.gone.length + r.not_old.length
      toast(`Took ${r.taken.length}${skipped ? `; ${skipped} no longer there or already taken` : ''}. See where they went in the Inbox and the drafts.`)
      onTaken()
    } catch (e: any) { setErr(e.message); setBusy('') }
  }
  return (
    <Modal wide title={`Already in ${inst.name} when it was added`} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!picked.size || busy === 'take'} onClick={take}>
        {busy === 'take' ? 'Taking…' : `Take ${picked.size || ''} ${picked.size === 1 ? 'dataset' : 'datasets'}`}</button>
    </>}>
      <div className="stack" style={{ gap: 10 }}>
        <p className="faint" style={{ margin: 0 }}>These were in the folder when you added it, so Benchtop left them alone. Tick the ones to
          bring in: each is then handled like a new arrival, attached to the draft its name points to, or put in the Inbox.</p>
        <input className="input" value={q} onChange={e => setQ(e.target.value)} placeholder="Filter by name, e.g. EXP-00042 or JL-013" aria-label="Filter" />
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
        {total === 0 && busy !== 'load' ? <p className="faint" style={{ margin: 0 }}>{q ? `Nothing matches “${q}”.` : 'Nothing left: everything that was there has been taken or has changed since.'}</p>
          : <>
            <label className="row old-all" style={{ gap: 8 }}><input type="checkbox" checked={allShown} disabled={!takeable.length}
              onChange={() => { const n = new Set(picked); takeable.forEach(x => (allShown ? n.delete(x.path) : n.add(x.path))); setPicked(n) }} />
              Select all {items.length < total ? `${items.length} shown` : 'of them'} <span className="faint">({total.toLocaleString()} {q ? 'matching' : 'in all'})</span></label>
            <ul className="old-list">{items.map(x => (
              <li key={x.path} className={x.gone ? 'gone' : ''}>
                <label>
                  <input type="checkbox" disabled={x.gone} checked={picked.has(x.path)} onChange={() => toggle(x.path)} />
                  <span className="old-main"><span className="mono old-path">{x.path}</span>
                    <span className="faint old-meta">{x.gone ? 'No longer in the folder' : `${mb(x.size)}${x.modified ? ` · ${day(x.modified)}` : ''}`}</span>
                    {!x.gone && x.action !== 'attach' && x.note && <span className="old-note">{x.note}</span>}</span>
                  {!x.gone && <span className={`old-where ${x.action === 'attach' ? 'ok-text' : 'faint'}`}>
                    {x.action === 'attach' ? `→ ${x.experiment}` : '→ Inbox'}</span>}
                </label>
              </li>))}</ul>
            {items.length < total && <button className="btn sm" disabled={busy === 'load'} onClick={() => load(items.length)}>{busy === 'load' ? 'Loading…' : 'Show more'}</button>}
          </>}
      </div>
    </Modal>
  )
}

/** Admin → Instruments: the folders Benchtop watches. */
export default function InstrumentsPanel() {
  const { toast } = useApp()
  const [data, setData] = useState<ListOut | null>(null)
  const [form, setForm] = useState<Instrument | 'new' | null>(null)
  const [old, setOld] = useState<Instrument | null>(null)
  const [busy, setBusy] = useState(false)
  const load = () => api.get<ListOut>('/api/instruments').then(setData).catch(() => {})
  useEffect(() => { load(); const t = setInterval(load, 30_000); return () => clearInterval(t) }, [])
  if (!data) return null
  const scanNow = async () => { setBusy(true); try { await api.post('/api/instruments/scan'); await load(); toast('Checked every folder') } catch (e: any) { toast(e.message) } finally { setBusy(false) } }
  const pause = async (i: Instrument) => { await api.patch(`/api/instruments/${i.id}`, { enabled: !i.enabled }).catch(e => toast(e.message)); load() }
  const remove = async (i: Instrument) => {
    if (!confirm(`Stop watching ${i.name}? Files already attached stay where they are, and anything waiting stays in the Inbox.`)) return
    await api.del(`/api/instruments/${i.id}`).catch(e => toast(e.message)); load()
  }
  return (
    <section className="stack" style={{ gap: 10 }}>
      <div className="spread" style={{ alignItems: 'center' }}><h2 style={{ fontSize: 17 }}>Instruments</h2>
        <div className="row" style={{ gap: 6 }}>
          {data.instruments.length > 0 && <button className="btn sm" disabled={busy} onClick={scanNow}>{busy ? 'Checking…' : 'Check folders now'}</button>}
          <button className="btn sm" onClick={() => setForm('new')}>Add a folder</button></div></div>
      <p className="faint" style={{ margin: 0 }}>Benchtop looks in these folders every {data.scan_seconds === 60 ? 'minute' : data.scan_seconds > 60 ? `${Math.round(data.scan_seconds / 60)} minutes` : `${data.scan_seconds} seconds`}.
        A file or dataset whose name holds an experiment number (like EXP-00042) is attached to that draft's “Instrument data” section; anything
        else waits in the Inbox. Up to {data.max_mb >= 1024 ? `${data.max_mb / 1024} GB` : `${data.max_mb} MB`} per arrival.</p>
      {data.instruments.length === 0 ? <div className="panel panel-pad faint">No folders yet. Add one for each instrument: your NMR's data folder, the HPLC's export folder, a shared drive.</div>
        : <div className="stack" style={{ gap: 8 }}>{data.instruments.map(i => {
          const c = i.counts
          return (
            <div key={i.id} className={`panel admin-card${i.enabled ? '' : ' paused'}`}>
              <div className="card-row" style={{ justifyContent: 'space-between' }}>
                <span><b>{i.name}</b> <span className="faint">· {i.kind_label}</span>{!i.enabled && <span className="pill" style={{ marginLeft: 6 }}>Paused</span>}</span>
                <span className="row" style={{ gap: 4 }}>
                  <button className="btn ghost sm" onClick={() => setForm(i)}>Edit</button>
                  <button className="btn ghost sm" onClick={() => pause(i)}>{i.enabled ? 'Pause' : 'Resume'}</button>
                  <button className="btn ghost sm danger" onClick={() => remove(i)}>Remove</button></span>
              </div>
              <div className="card-row"><span className="card-label">Folder</span><code className="grow" style={{ wordBreak: 'break-all' }}>{i.folder}</code></div>
              <div className="card-row"><span className="card-label">Unmatched files</span>
                <span>{i.audience.length ? `shown to ${i.audience.map(g => g.name).join(', ')}` : 'shown to everyone'}</span></div>
              {i.last_error && <p className="error" style={{ margin: '0 0 4px' }}>{i.last_error}</p>}
              <div className="card-foot"><span>{i.last_scan_at ? `Checked ${ago(i.last_scan_at)}${i.last_scan_ms != null && i.last_scan_ms >= 1000 ? ` (took ${(i.last_scan_ms / 1000).toFixed(0)} s)` : ''}` : 'Not checked yet'}
                {' · '}{c.attached || 0} attached · {(c.inbox || 0)} waiting{c.settling ? ` · ${c.settling} arriving` : ''}</span>
                {c.baseline ? <span className="foot-group"><span className="faint">{c.baseline.toLocaleString()} left alone: {c.baseline === 1 ? 'it was' : 'they were'} there before you added the folder</span>
                  <button className="btn ghost sm" disabled={!i.enabled} title={i.enabled ? undefined : 'Resume this folder first'} onClick={() => setOld(i)}>Choose some to take</button></span> : null}</div>
            </div>)
        })}</div>}
      {old && <OldData inst={old} onClose={() => setOld(null)} onTaken={() => { setOld(null); load() }} />}
      {form && <InstrumentForm kinds={data.kinds} editing={form === 'new' ? null : form} onClose={() => setForm(null)} onSaved={() => { setForm(null); load() }} />}
    </section>
  )
}
