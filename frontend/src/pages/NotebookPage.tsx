import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName, downloadAuthed, type ExperimentSummary, type Notebook, type Status } from '../api'
import ExperimentList from '../components/ExperimentList'
import NewExperimentDialog from '../components/NewExperimentDialog'
import Modal from '../components/Modal'
import ImportElnDialog from '../components/ImportElnDialog'
import { AudienceBadge, AudiencePicker, type AudienceValue } from '../components/Audience'
import { notebookActions } from '../notebookActions'

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

export default function NotebookPage() {
  const { id } = useParams()
  const { notebooks, archivedNotebooks, canWrite, user, refreshNotebooks, toast } = useApp()
  const nav = useNavigate()
  const nb = notebooks.find(n => n.id === Number(id)) || archivedNotebooks.find(n => n.id === Number(id))
  const [items, setItems] = useState<ExperimentSummary[]>([])
  const [filter, setFilter] = useState<Status | ''>('')
  const [creating, setCreating] = useState(false)
  const [importing, setImporting] = useState(false)
  const [confirm, setConfirm] = useState<'delete' | 'archive' | null>(null)
  const [busy, setBusy] = useState(false)
  const [audEdit, setAudEdit] = useState<AudienceValue | null>(null)
  const saveAudience = async () => {
    try {
      await api.patch(`/api/notebooks/${id}`, audEdit)
      await refreshNotebooks(); setAudEdit(null); toast('Updated who can open this notebook')
    } catch (e: any) { toast(e.message) }
  }
  const setArchived = async (n: Notebook, archived: boolean) => {
    setBusy(true)
    try {
      await api.patch(`/api/notebooks/${n.id}`, { archived })
      await refreshNotebooks(); setConfirm(null)
      toast(archived ? `Archived ${n.name}` : `${n.name} is back in the notebook list`)
    } catch (e: any) { toast(e.message) } finally { setBusy(false) }
  }
  const remove = async (n: Notebook) => {
    setBusy(true)
    try {
      await api.del(`/api/notebooks/${n.id}`)
      setConfirm(null); nav('/')
      await refreshNotebooks()
      toast(`Deleted ${n.name}`)
    } catch (e: any) { toast(e.message); await refreshNotebooks() } finally { setBusy(false) }
  }

  const PAGE = 200
  const [more, setMore] = useState(false)
  const load = (offset: number) =>
    api.get<ExperimentSummary[]>(`/api/experiments?notebook_id=${id}${filter ? `&status=${filter}` : ''}&limit=${PAGE}&offset=${offset}`).then(xs => {
      setItems(old => (offset ? [...old, ...xs] : xs)); setMore(xs.length === PAGE)
    })
  useEffect(() => { load(0) }, [id, filter]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!nb) return <p className="empty">Notebook not found.</p>
  const can = notebookActions(nb, user)
  const drafts = nb.status_counts.open ?? 0
  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="spread">
        <div>
          <h1 className="page-title">{nb.name}{nb.archived && <span className="badge-archived">Archived</span>}</h1>
          <p className="lede">{nb.description || 'No description.'} <span className="faint">Owned by {displayName(nb.owner)}.</span></p>
          <div className="row nb-manage" style={{ gap: 8 }}>
            <AudienceBadge audience={nb} />
            {(nb.owner.id === user.id || user.role === 'admin') && canWrite && <button className="btn ghost sm"
              onClick={() => setAudEdit({ visibility: nb.visibility, group_id: nb.group?.id ?? null })}>Change who can open it</button>}
            {can.archive && <button className="btn ghost sm" onClick={() => setConfirm('archive')}
              title="Notebooks with experiments can't be deleted; archive them instead.">Archive notebook</button>}
            {can.delete && <button className="btn ghost sm danger" onClick={() => setConfirm('delete')}>Delete notebook</button>}
          </div>
        </div>
        <div className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
          {nb.experiment_count > 0 && <button className="btn" title="Every experiment here you can open, in the shared format of lab notebooks (.eln)"
            onClick={() => { toast('Preparing the export…'); downloadAuthed(`/api/notebooks/${nb.id}/export.eln`, `${nb.name.replace(/[\\/:*?"<>|#^[\]{}%]+/g, ' ').slice(0, 80)} - ${new Date().toISOString().slice(0, 10)}.eln`).catch(e => toast(e.message)) }}>Export (.eln)</button>}
          {canWrite && !nb.archived && <button className="btn" onClick={() => setImporting(true)} title="Bring experiments in from eLabFTW, RSpace, another Benchtop…">Import .eln…</button>}
          {canWrite && !nb.archived && <button className="btn primary" onClick={() => setCreating(true)}>New experiment</button>}
        </div>
      </div>
      {nb.archived && <div className="notice archived-note" role="note">
        <span><b>This notebook is archived.</b> It's out of the sidebar's notebook list (it's under <i>Archived</i> at the bottom of it), and no
          new experiments or imports can go into it. Its experiments are unchanged: they can still be opened, found in search and
          linked to, signatures stay valid, and drafts can still be edited and signed.</span>
        {can.unarchive && <button className="btn sm" disabled={busy} onClick={() => setArchived(nb, false)}>Unarchive</button>}
      </div>}
      <div className="row" role="group" aria-label="Filter by status">
        {(['', 'open', 'signed', 'witnessed', 'voided', ...(nb.status_counts.locked ? ['locked'] as const : [])] as const).map(s => (
          <button key={s} className={`btn sm${filter === s ? ' primary' : ''}`} onClick={() => setFilter(s)}>
            {s ? `${s[0].toUpperCase()}${s.slice(1)} (${nb.status_counts[s] ?? 0})` : `All (${nb.experiment_count})`}
          </button>
        ))}
      </div>
      <div className="panel"><ExperimentList items={items} showNotebook={false}
        empty={filter ? `No ${filter} experiments in this notebook.`
          : canWrite && !nb.archived ? 'No experiments here yet. Press N or click New experiment to start one.' : 'No experiments here yet.'} /></div>
      {more && <div style={{ textAlign: 'center' }}><button className="btn" onClick={() => load(items.length)}>Show older experiments</button></div>}
      {creating && <NewExperimentDialog notebookId={nb.id} onClose={() => setCreating(false)} />}
      {importing && <ImportElnDialog notebookId={nb.id} onClose={() => setImporting(false)} onDone={() => { refreshNotebooks(); load(0) }} />}
      {audEdit && <Modal title={`Who can open ${nb.name}`} onClose={() => setAudEdit(null)} footer={<>
        <button className="btn" onClick={() => setAudEdit(null)}>Cancel</button>
        <button className="btn primary" onClick={saveAudience}>Save</button></>}>
        <AudiencePicker value={audEdit} onChange={setAudEdit} />
        <p className="faint">This applies to all {nb.experiment_count} experiment{nb.experiment_count === 1 ? '' : 's'} in the notebook. People an experiment was shared with individually keep their access.</p>
      </Modal>}
      {confirm === 'delete' && <Modal title={`Delete ${nb.name}?`} onClose={() => setConfirm(null)} footer={<>
        <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
        <button className="btn danger" disabled={busy} onClick={() => remove(nb)}>{busy ? 'Deleting…' : 'Delete notebook'}</button></>}>
        <p style={{ marginTop: 0 }}>The notebook <b>{nb.name}</b> has no experiments, so nothing else is lost. It goes for everyone who
          can open it, and anyone who had it as their default notebook goes back to choosing one.</p>
        <p className="faint" style={{ marginBottom: 0 }}>The audit trail keeps a note of the notebook and who deleted it. This can't be undone.</p>
      </Modal>}
      {confirm === 'archive' && <Modal title={`Archive ${nb.name}?`} onClose={() => setConfirm(null)} footer={<>
        <button className="btn" onClick={() => setConfirm(null)}>Cancel</button>
        <button className="btn primary" disabled={busy} onClick={() => setArchived(nb, true)}>{busy ? 'Archiving…' : 'Archive notebook'}</button></>}>
        <ul className="archive-points">
          <li>It leaves the sidebar's notebook list. You'll find it under <b>Archived</b> at the bottom of that list.</li>
          <li>No new experiments or .eln imports can go into it.</li>
          <li>Its {plural(nb.experiment_count, 'experiment')} stay exactly as they are: the same people can open them, search finds them,
            links to them work and signatures stay valid.</li>
          {drafts > 0 && <li>{drafts === 1 ? 'The draft' : `The ${drafts} drafts`} still open in it can still be edited and signed.</li>}
          <li>You can unarchive it at any time from its page.</li>
        </ul>
        <p className="faint" style={{ marginBottom: 0 }}>Notebooks with experiments can't be deleted; archive them instead.</p>
      </Modal>}
    </div>
  )
}
