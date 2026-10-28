import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName, downloadAuthed, type ExperimentSummary, type Status } from '../api'
import ExperimentList from '../components/ExperimentList'
import NewExperimentDialog from '../components/NewExperimentDialog'
import Modal from '../components/Modal'
import ImportElnDialog from '../components/ImportElnDialog'
import { AudienceBadge, AudiencePicker, type AudienceValue } from '../components/Audience'

export default function NotebookPage() {
  const { id } = useParams()
  const { notebooks, canWrite, user, refreshNotebooks, toast } = useApp()
  const nb = notebooks.find(n => n.id === Number(id))
  const [items, setItems] = useState<ExperimentSummary[]>([])
  const [filter, setFilter] = useState<Status | ''>('')
  const [creating, setCreating] = useState(false)
  const [importing, setImporting] = useState(false)
  const [audEdit, setAudEdit] = useState<AudienceValue | null>(null)
  const saveAudience = async () => {
    try {
      await api.patch(`/api/notebooks/${id}`, audEdit)
      await refreshNotebooks(); setAudEdit(null); toast('Updated who can open this notebook')
    } catch (e: any) { toast(e.message) }
  }

  const PAGE = 200
  const [more, setMore] = useState(false)
  const load = (offset: number) =>
    api.get<ExperimentSummary[]>(`/api/experiments?notebook_id=${id}${filter ? `&status=${filter}` : ''}&limit=${PAGE}&offset=${offset}`).then(xs => {
      setItems(old => (offset ? [...old, ...xs] : xs)); setMore(xs.length === PAGE)
    })
  useEffect(() => { load(0) }, [id, filter]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!nb) return <p className="empty">Notebook not found.</p>
  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="spread">
        <div>
          <h1 className="page-title">{nb.name}</h1>
          <p className="lede">{nb.description || 'No description.'} <span className="faint">Owned by {displayName(nb.owner)}.</span></p>
          <div className="row" style={{ gap: 8 }}>
            <AudienceBadge audience={nb} />
            {(nb.owner.id === user.id || user.role === 'admin') && <button className="btn ghost sm"
              onClick={() => setAudEdit({ visibility: nb.visibility, group_id: nb.group?.id ?? null })}>Change who can open it</button>}
          </div>
        </div>
        <div className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
          {nb.experiment_count > 0 && <button className="btn" title="Every experiment here you can open, in the shared format of lab notebooks (.eln)"
            onClick={() => { toast('Preparing the export…'); downloadAuthed(`/api/notebooks/${nb.id}/export.eln`, `${nb.name.replace(/[\\/:*?"<>|#^[\]{}%]+/g, ' ').slice(0, 80)} - ${new Date().toISOString().slice(0, 10)}.eln`).catch(e => toast(e.message)) }}>Export (.eln)</button>}
          {canWrite && !nb.archived && <button className="btn" onClick={() => setImporting(true)} title="Bring experiments in from eLabFTW, RSpace, another Benchtop…">Import .eln…</button>}
          {canWrite && <button className="btn primary" onClick={() => setCreating(true)}>New experiment</button>}
        </div>
      </div>
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
    </div>
  )
}
