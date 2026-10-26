import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApp } from '../App'
import { api, when, type Experiment } from '../api'
import { doiHref, fileSize, firstEmbargoDay, parseKeywords, problems, type Creator, type PublicationInfo, type PublishDetails } from '../publish'
import Modal from './Modal'

/** Publishing a signed record on Zenodo (0.28): an open dataset with a DOI. Two steps: prepare a draft (files uploaded,
 *  DOI reserved, can be discarded), then publish it (permanent). */

interface Site { enabled: boolean; url: string; site: string; sandbox: boolean; community: string; fixed: string[]; demo: boolean; unavailable: string }
interface Publication extends PublicationInfo {
  id: number; deposition_id: number | null; details: Partial<PublishDetails>
  files: { name: string; what: string; size: number; sha256: string }[]; created_at: string; updated_at: string
}
interface Status extends Site { why_not: string; connected: boolean; token_site: string | null; publication: Publication | null }
interface Preview extends Status {
  details: PublishDetails; licences: Record<string, string>; references: string[]
  files: { name: string; what: string; contains?: string[]; attached?: { filename: string; size: number }[]; size?: number; comments?: number }[]
}

const LICENCE_HELP: Record<string, string> = {
  'cc-by-4.0': 'Anyone may reuse it, as long as they credit you. The usual choice for data.',
  'cc0-1.0': 'No conditions at all (public domain). Some funders and journals ask for this.',
}

/** The DOI under the record's title, once it's published. */
export function DoiLine({ pub }: { pub: Experiment['publication'] }) {
  if (!pub || pub.status !== 'published' || !pub.doi) return null
  return <span className="doi-line">DOI <a href={doiHref(pub)} target="_blank" rel="noreferrer noopener">{pub.doi}</a>
    {pub.sandbox && <span className="faint"> (sandbox, not a real DOI)</span>}</span>
}

/** The rail's "Publishing" box: what's on Zenodo, and the next step. */
export function PublishPanel({ exp, onChange }: { exp: Experiment; onChange: () => void }) {
  const { user, toast } = useApp()
  const [st, setSt] = useState<Status | null>(null)
  const [dialog, setDialog] = useState<'' | 'prepare' | 'publish'>('')
  const [busy, setBusy] = useState(false)
  const mine = exp.owner.id === user.id && user.role !== 'viewer'
  const signed = exp.status === 'signed' || exp.status === 'witnessed'
  const pubStatus = exp.publication?.status
  const load = () => api.get<Status>(`/api/experiments/${exp.id}/zenodo`).then(setSt).catch(() => setSt(null))
  useEffect(() => { if (mine && (signed || pubStatus)) load() }, [exp.id, exp.status, pubStatus, mine, signed]) // eslint-disable-line react-hooks/exhaustive-deps
  const pub = exp.publication
  if (pub?.status === 'published') return (
    <div className="publish-panel">
      <h3>Publishing</h3>
      <div className="published-box">
        <div><b>Published on {pub.site}</b>{pub.published_at ? ` · ${new Date(pub.published_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}` : ''}</div>
        <div>DOI <a href={doiHref(pub)} target="_blank" rel="noreferrer noopener">{pub.doi}</a></div>
        {pub.sandbox && <div className="faint">On the Zenodo sandbox, for trying things out: this DOI doesn't work outside it.</div>}
        <a className="faint" href={pub.url} target="_blank" rel="noreferrer noopener">Open the record on Zenodo</a>
      </div>
    </div>
  )
  if (!mine || !st || (!signed && !pub)) return null
  if (!pub && (st.why_not || (!st.enabled && !st.demo))) return null
  const p = st.publication
  const discard = async () => {
    if (!confirm(`Discard the draft of ${exp.eid} on ${p?.site || st.site}? Its files are deleted there and its reserved DOI is never used. ${exp.eid} itself doesn't change.`)) return
    setBusy(true)
    try { await api.del(`/api/experiments/${exp.id}/zenodo`); toast('Draft discarded'); onChange(); load() } catch (e: any) { toast(e.message) } finally { setBusy(false) }
  }
  const done = () => { setDialog(''); onChange(); load() }
  return (
    <div className="publish-panel">
      <h3>Publishing</h3>
      {!p && <>
        <p className="faint" style={{ margin: 0 }}>Make this record an open dataset on {st.site}, with a DOI to cite in a thesis or a paper's
          supporting information.</p>
        <button className="btn sm" onClick={() => setDialog('prepare')}>Publish on Zenodo…</button>
      </>}
      {p && (p.status === 'draft') && <>
        <div><b>Draft on {p.site}</b>, not public yet</div>
        <div className="faint">DOI reserved: <span className="mono">{p.doi}</span></div>
        {p.error && <div className="error" style={{ fontSize: 13 }}>{p.error}</div>}
        <a className="faint" href={p.draft_url} target="_blank" rel="noreferrer noopener">Look at the draft on Zenodo</a>
        <div className="row" style={{ gap: 6, marginTop: 4 }}>
          <button className="btn sm primary" disabled={busy || !!st.unavailable || !!st.why_not} onClick={() => setDialog('publish')}>Publish…</button>
          <button className="btn sm" disabled={busy} onClick={() => setDialog('prepare')}>Change details…</button>
          <button className="btn ghost sm" disabled={busy} onClick={discard}>Discard draft</button>
        </div>
      </>}
      {p && (p.status === 'failed' || p.status === 'working') && <>
        <div><b>Not finished</b>: the draft on {p.site} isn't complete</div>
        {p.error && <div className="error" style={{ fontSize: 13 }}>{p.error}</div>}
        <div className="row" style={{ gap: 6, marginTop: 4 }}>
          <button className="btn sm" disabled={busy || !!st.why_not} onClick={() => setDialog('prepare')}>Try again…</button>
          <button className="btn ghost sm" disabled={busy} onClick={discard}>Discard</button>
        </div>
      </>}
      {dialog === 'prepare' && <PrepareDialog exp={exp} onClose={() => setDialog('')} onDone={done} />}
      {dialog === 'publish' && p && <PublishConfirm exp={exp} pub={p} onClose={() => setDialog('')} onDone={done} />}
    </div>
  )
}

/** Step 1: the preview. Everything that will go to Zenodo, editable, before anything is sent. */
function PrepareDialog({ exp, onClose, onDone }: { exp: Experiment; onClose: () => void; onDone: () => void }) {
  const { toast } = useApp()
  const [pv, setPv] = useState<Preview | null>(null)
  const [d, setD] = useState<PublishDetails | null>(null)
  const [keywords, setKeywords] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    api.get<Preview>(`/api/experiments/${exp.id}/zenodo/preview`).then(x => { setPv(x); setD(x.details); setKeywords(x.details.keywords.join(', ')) })
      .catch(e => setErr(e.message))
  }, [exp.id])
  const withComments = async (on: boolean) => {
    if (!d) return
    setD({ ...d, include_comments: on })
    try { const x = await api.get<Preview>(`/api/experiments/${exp.id}/zenodo/preview?comments=${on}`); setPv(x) } catch { /* the list stays as it was */ }
  }
  const set = <K extends keyof PublishDetails>(k: K, v: PublishDetails[K]) => d && setD({ ...d, [k]: v })
  const setCreator = (i: number, k: keyof Creator, v: string) => d && setD({ ...d, creators: d.creators.map((c, j) => j === i ? { ...c, [k]: v } : c) })
  const full = d ? { ...d, keywords: parseKeywords(keywords) } : null
  const todo = full ? problems(full) : []
  const blocked = pv ? (pv.unavailable || pv.why_not || (!pv.connected ? 'connect' : '')) : 'loading'
  const go = async () => {
    if (!full) return
    setBusy(true); setErr('')
    try {
      await api.post(`/api/experiments/${exp.id}/zenodo/prepare`, full)
      toast(`Draft ready on ${pv?.site}: look at it there, then publish`)
      onDone()
    } catch (e: any) { setErr(e.message); setBusy(false) }
  }
  return (
    <Modal wide title={`Publish ${exp.eid} on Zenodo`} onClose={busy ? () => {} : onClose} footer={<>
      <button className="btn" disabled={busy} onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={busy || !!blocked || todo.length > 0} title={todo.join(' ')} onClick={go}>
        {busy ? 'Uploading…' : pv?.publication ? 'Send to the draft again' : 'Prepare on Zenodo'}</button>
    </>}>
      {!pv || !d ? <p className="faint">{err || 'Loading…'}</p> : <div className="stack publish-form" style={{ gap: 16 }}>
        <p className="notice" style={{ margin: 0 }}><b>Step 1 of 2.</b> Benchtop makes a draft on {pv.site} with a reserved DOI, and uploads the files
          listed below. Nothing is public yet: you can look at the draft on Zenodo, and discard it. Publishing is the second step.</p>
        {exp.status === 'signed' && <p className="faint" style={{ margin: 0 }}>{exp.eid} hasn't been witnessed yet, so the files carry your signature only.
          A witness signature added after publishing won't be in them; to include it, wait until it's witnessed.</p>}
        {pv.sandbox && !pv.unavailable && <p className="notice sandbox-note" style={{ margin: 0 }}>This Benchtop uses the Zenodo <b>sandbox</b>, for trying
          things out: records there aren't kept for good, and their DOIs don't work outside it.</p>}
        {pv.unavailable && <p className="notice" style={{ margin: 0 }}>{pv.unavailable}</p>}
        {!pv.unavailable && !pv.connected && <p className="notice warn" style={{ margin: 0 }}>Connect your {pv.site} account first, in{' '}
          <Link to="/account#zenodo" onClick={onClose}>Account settings → Zenodo</Link>{pv.token_site && pv.token_site !== pv.site ? ` (your token is for ${pv.token_site})` : ''}.
          You can look through the details here meanwhile.</p>}

        <section className="stack" style={{ gap: 10 }}>
          <h3>Details</h3>
          <label className="field">Title<input className="input" value={d.title} maxLength={300} onChange={e => set('title', e.target.value)} /></label>
          <label className="field">Description
            <textarea className="input" rows={7} value={d.description} onChange={e => set('description', e.target.value)} />
            <span className="help">Shown on the record's page on Zenodo. A blank line starts a new paragraph.</span></label>
          <label className="field">Keywords<input className="input" value={keywords} onChange={e => setKeywords(e.target.value)} placeholder="suzuki, cross-coupling" />
            <span className="help">From the record's tags. Separate them with commas.</span></label>
        </section>

        <section className="stack" style={{ gap: 8 }}>
          <h3>Authors</h3>
          <p className="help" style={{ margin: 0 }}>In the order they should be cited, family name first (as Zenodo lists them). An ORCID iD links the dataset to the person's ORCID record.</p>
          {d.creators.map((c, i) => (
            <div key={i} className="creator-row">
              <input className="input" aria-label={`Author ${i + 1}: name`} placeholder="Family name, Given names" value={c.name} onChange={e => setCreator(i, 'name', e.target.value)} />
              <input className="input" aria-label={`Author ${i + 1}: affiliation`} placeholder="Affiliation (optional)" value={c.affiliation} onChange={e => setCreator(i, 'affiliation', e.target.value)} />
              <input className="input mono" aria-label={`Author ${i + 1}: ORCID iD`} placeholder="ORCID iD (optional)" value={c.orcid} spellCheck={false} onChange={e => setCreator(i, 'orcid', e.target.value)} />
              <button className="btn ghost sm" aria-label={`Remove author ${i + 1}`} disabled={d.creators.length === 1} onClick={() => setD({ ...d, creators: d.creators.filter((_, j) => j !== i) })}>×</button>
            </div>
          ))}
          <div><button className="btn sm" onClick={() => setD({ ...d, creators: [...d.creators, { name: '', affiliation: '', orcid: '' }] })}>Add an author</button></div>
        </section>

        <section className="stack" style={{ gap: 10 }}>
          <h3>Licence and access</h3>
          <div className="field"><span>Licence</span>
            <div className="choice" role="group" aria-label="Licence">
              {Object.entries(pv.licences).map(([k, label]) => <button key={k} type="button" aria-pressed={d.licence === k} onClick={() => set('licence', k as PublishDetails['licence'])}>{label}</button>)}
            </div>
            <span className="help">{LICENCE_HELP[d.licence]}</span></div>
          <div className="field"><span>Access</span>
            <div className="row" style={{ gap: 10 }}>
              <div className="choice" role="group" aria-label="Access">
                <button type="button" aria-pressed={d.access === 'open'} onClick={() => set('access', 'open')}>Open</button>
                <button type="button" aria-pressed={d.access === 'embargoed'} onClick={() => setD({ ...d, access: 'embargoed', embargo_date: d.embargo_date || firstEmbargoDay() })}>Embargoed until a date</button>
              </div>
              {d.access === 'embargoed' && <input className="input" type="date" aria-label="Embargo ends" min={firstEmbargoDay()} value={d.embargo_date} onChange={e => set('embargo_date', e.target.value)} />}
            </div>
            <span className="help">{d.access === 'open' ? 'Everyone can download the files as soon as it’s published.'
              : 'The title, authors and description are public at once; the files from this date (say, after the paper comes out).'}</span></div>
        </section>

        {(pv.references.length > 0 || pv.community) && <section className="stack" style={{ gap: 6 }}>
          <h3>Links</h3>
          {pv.references.length > 0 && <p style={{ margin: 0 }}>The {pv.references.length === 1 ? 'paper' : `${pv.references.length} papers`} in the References sections
            {pv.references.length === 1 ? ' is' : ' are'} listed as references: <span className="mono" style={{ fontSize: 13 }}>{pv.references.join(', ')}</span></p>}
          {pv.community && <p style={{ margin: 0 }}>It's submitted to the <b>{pv.community}</b> community on Zenodo (set by this Benchtop's admin); the community's curators accept it.</p>}
        </section>}

        <section className="stack" style={{ gap: 8 }}>
          <h3>Files that become public</h3>
          <ul className="publish-files">
            {pv.files.map(f => <li key={f.name}><span className="mono">{f.name}</span><div className="faint">{f.what}</div>
              {f.contains && <details><summary className="faint">What's inside{f.attached?.length ? ` (${f.attached.length} attached file${f.attached.length === 1 ? '' : 's'}${f.size ? `, ${fileSize(f.size)}` : ''})` : ''}</summary>
                <ul>{f.contains.map(c => <li key={c}>{c}</li>)}</ul></details>}</li>)}
          </ul>
          {!!pv.files[1]?.comments && <label className="check-row"><input type="checkbox" checked={d.include_comments} onChange={e => withComments(e.target.checked)} />
            Include the discussion ({pv.files[1].comments} comment{pv.files[1].comments === 1 ? '' : 's'}) in the .eln file. It's left out unless you tick this.</label>}
        </section>
        {todo.length > 0 && <p className="faint" style={{ margin: 0 }}>{todo.join(' ')}</p>}
        {err && <p className="error" role="alert" style={{ margin: 0 }}>{err}</p>}
        {busy && <p className="faint" style={{ margin: 0 }}>Uploading to {pv.site}. A record with large spectra or LC-MS data takes a while; keep this window open.</p>}
      </div>}
    </Modal>
  )
}

/** Step 2: publishing can't be undone, so it says exactly what becomes permanent and asks to confirm. */
function PublishConfirm({ exp, pub, onClose, onDone }: { exp: Experiment; pub: Publication; onClose: () => void; onDone: () => void }) {
  const { toast } = useApp()
  const [sure, setSure] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const d = pub.details
  const go = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.post<Publication>(`/api/experiments/${exp.id}/zenodo/publish`, { confirm: true })
      toast(`Published: DOI ${r.doi}`)
      onDone()
    } catch (e: any) { setErr(e.message); setBusy(false) }
  }
  return (
    <Modal title={`Publish ${exp.eid}? This can't be undone`} onClose={busy ? () => {} : onClose} footer={<>
      <button className="btn" disabled={busy} onClick={onClose}>Cancel</button>
      <button className="btn danger" disabled={!sure || busy} onClick={go}>{busy ? 'Publishing…' : 'Publish for good'}</button>
    </>}>
      <div className="stack" style={{ gap: 12 }}>
        <p className="notice warn" style={{ margin: 0 }}>Once published, the files and the DOI <b className="mono">{pub.doi}</b> are <b>permanent and public</b>.
          Zenodo keeps them for good: nobody can delete them or change the files, you included. (On Zenodo you can add a corrected new version later; this one stays.)</p>
        {pub.sandbox && <p className="faint" style={{ margin: 0 }}>This is the Zenodo sandbox, for trying things out, so here it's only a rehearsal.</p>}
        <dl className="kv">
          <dt>Title</dt><dd>{d.title}</dd>
          <dt>Authors</dt><dd>{(d.creators || []).map(c => c.name).join('; ')}</dd>
          <dt>Licence</dt><dd>{d.licence === 'cc0-1.0' ? 'CC0 1.0' : 'CC BY 4.0'}</dd>
          <dt>Access</dt><dd>{d.access === 'embargoed' ? `Embargoed: files public from ${d.embargo_date}` : 'Open'}</dd>
          <dt>Files</dt><dd>{pub.files.map(f => <div key={f.name}><span className="mono">{f.name}</span> <span className="faint nowrap">{fileSize(f.size)}</span></div>)}</dd>
          <dt>Prepared</dt><dd>{when(pub.updated_at)}</dd>
        </dl>
        <p style={{ margin: 0 }}><a href={pub.draft_url} target="_blank" rel="noreferrer noopener">Look at the draft on Zenodo</a> first, if you haven't.</p>
        <label className="check-row"><input type="checkbox" checked={sure} onChange={e => setSure(e.target.checked)} />
          I understand that publishing is permanent and that these files will be public.</label>
        {err && <p className="error" role="alert" style={{ margin: 0 }}>{err}</p>}
      </div>
    </Modal>
  )
}

// ====================================================================== Account settings

interface Account extends Site { connected: boolean; token_site: string | null; hint: string; unreadable: boolean; connected_at: string | null; checked_at: string | null }

/** Account → Zenodo: the person's own personal access token. Kept on the server, encrypted; never shown again. */
export function ZenodoAccount() {
  const { toast } = useApp()
  const [a, setA] = useState<Account | null>(null)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [replacing, setReplacing] = useState(false)
  const load = () => api.get<Account>('/api/auth/zenodo').then(setA).catch(() => {})
  useEffect(() => { load() }, [])
  if (!a) return <div className="panel"><div className="section faint">Loading…</div></div>
  const save = async () => {
    setBusy(true); setErr('')
    try {
      const r = await api.put<Account & { checked: boolean }>('/api/auth/zenodo', { token })
      setA(r); setToken(''); setReplacing(false)
      toast(r.checked ? `Connected: ${r.site} accepted the token` : `Saved. ${r.site} couldn't be reached to check it; it will be checked when you publish`)
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  const remove = async () => {
    if (!confirm(`Disconnect your ${a.token_site} account? Benchtop forgets the token. Records already published stay published.`)) return
    setA(await api.del<Account>('/api/auth/zenodo')); toast('Disconnected')
  }
  const tokenPage = `${a.url}/account/settings/applications/tokens/new/`
  if (a.unavailable && !a.connected) return <div className="panel"><div className="section faint">{a.unavailable}</div></div>
  return (
    <div className="panel" id="zenodo"><div className="section stack" style={{ gap: 10 }}>
      {a.sandbox && <p className="notice sandbox-note" style={{ margin: 0 }}>This Benchtop publishes on the Zenodo <b>sandbox</b> (sandbox.zenodo.org), a
        test site for trying things out. It has its own accounts and tokens; its DOIs don't work outside it.</p>}
      {a.connected && !replacing ? <div className="spread" style={{ alignItems: 'center', gap: 12 }}>
        <div><b style={{ fontWeight: 600 }}>Connected to {a.site}</b>
          <div className="faint">Token …{a.hint}{a.checked_at ? ` · accepted by Zenodo ${when(a.checked_at)}` : ' · not checked yet (Zenodo couldn’t be reached)'}</div></div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn sm" onClick={() => setReplacing(true)}>Use another token</button>
          <button className="btn ghost sm" onClick={remove}>Disconnect</button></div>
      </div> : <>
        {a.token_site && a.token_site !== a.site && <p className="notice warn" style={{ margin: 0 }}>Your token is for {a.token_site}, but this Benchtop now publishes on {a.site}: enter a token from {a.site}.</p>}
        {a.unreadable && <p className="notice warn" style={{ margin: 0 }}>The token you entered before can't be read any more (this Benchtop's secret key changed). Enter it again.</p>}
        <ol className="steps">
          <li>Sign in at <a href={a.url} target="_blank" rel="noreferrer noopener">{a.site}</a> (make an account there if you don't have one; signing in with ORCID works).</li>
          <li>Open <a href={tokenPage} target="_blank" rel="noreferrer noopener">Applications → Personal access tokens → New token</a>, give it a name
            (“Benchtop”), tick <b>deposit:write</b> and <b>deposit:actions</b>, and create it.</li>
          <li>Copy the token and paste it here.</li>
        </ol>
        <form className="row" style={{ gap: 8 }} onSubmit={e => { e.preventDefault(); if (token.trim()) save() }}>
          <input className="input mono grow" type="password" autoComplete="off" spellCheck={false} placeholder="Zenodo personal access token" aria-label="Zenodo personal access token"
            value={token} onChange={e => setToken(e.target.value)} style={{ flex: 1, minWidth: 0 }} />
          <button className="btn primary" disabled={busy || !token.trim()}>{busy ? 'Checking…' : 'Connect'}</button>
          {replacing && <button type="button" className="btn ghost" onClick={() => { setReplacing(false); setToken(''); setErr('') }}>Cancel</button>}
        </form>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      </>}
      <p className="help" style={{ margin: 0 }}>The token is checked with Zenodo, then kept on this Benchtop's server, encrypted. It's never shown again,
        and it's used only when you prepare or publish one of your records. You can delete it on Zenodo at any time.</p>
    </div></div>
  )
}

// ====================================================================== Admin → Integrations

/** Admin → Integrations: on or off, zenodo.org or the sandbox, and a community. */
export function ZenodoSettingsPanel() {
  const { toast } = useApp()
  const [s, setS] = useState<Site | null>(null)
  const [community, setCommunity] = useState('')
  const load = () => api.get<Site>('/api/admin/zenodo').then(x => { setS(x); setCommunity(x.community) }).catch(() => {})
  useEffect(() => { load() }, [])
  if (!s) return null
  const fixed = (k: string) => s.fixed.includes(k)
  const save = async (changes: Record<string, unknown>) => {
    try { const x = await api.put<Site>('/api/admin/zenodo', changes); setS(x); setCommunity(x.community); toast('Saved') } catch (e: any) { toast(e.message) }
  }
  return (
    <section className="stack" style={{ gap: 10 }}>
      <div className="spread" style={{ alignItems: 'center' }}><h2 style={{ fontSize: 17 }}>Publishing on Zenodo</h2>
        <label className="switch-row"><input type="checkbox" checked={s.enabled} disabled={fixed('enabled')} onChange={e => save({ enabled: e.target.checked })} /> On</label></div>
      <p className="faint" style={{ margin: 0 }}>Authors can publish their signed records on Zenodo as open datasets with a DOI: the PDF and the .eln export,
        from their own Zenodo account (a token in Account settings). Publishing is permanent, so it takes two steps and a confirmation.
        Use the sandbox to try it out first: it has its own accounts, and its DOIs aren't real.</p>
      {s.demo && <p className="notice" style={{ margin: 0 }}>This is the demo lab: it never publishes anything, whatever is chosen here.</p>}
      <div className="panel admin-card">
        <div className="card-row"><span className="card-label">Zenodo site</span>
          <div className="choice" role="group" aria-label="Zenodo site">
            <button type="button" aria-pressed={s.url === 'https://zenodo.org'} disabled={fixed('url')} onClick={() => save({ site: 'zenodo' })}>zenodo.org</button>
            <button type="button" aria-pressed={s.url === 'https://sandbox.zenodo.org'} disabled={fixed('url')} onClick={() => save({ site: 'sandbox' })}>sandbox.zenodo.org (for trying it out)</button>
          </div>
          {fixed('url') && <span className="faint">Set by BENCHTOP_ZENODO_URL: {s.url}</span>}</div>
        <div className="card-row url-row"><span className="card-label">Community</span>
          <input className="input grow" value={community} disabled={fixed('community')} placeholder="optional, e.g. ruiz-lab" aria-label="Zenodo community"
            onChange={e => setCommunity(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') save({ community }) }} />
          <button className="btn" disabled={community === s.community || fixed('community')} onClick={() => save({ community })}>Save</button></div>
        <p className="faint" style={{ margin: 0, fontSize: 12.5 }}>A community (zenodo.org/communities/<i>name</i>) gathers a group's or a department's
          records; each one published from here is submitted to it, and its curators accept it. Leave it empty for none.</p>
      </div>
    </section>
  )
}
