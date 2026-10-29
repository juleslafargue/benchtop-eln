import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName, type ExperimentSummary, type Notebook } from '../api'
import Modal from './Modal'
import NewNotebookDialog from './NewNotebookDialog'

/** The notebook a new experiment goes in unless the person picks another: the one they were looking at, their default
 *  notebook, else their own most recent one. Never someone else's: experiments can't be deleted, only voided, so a
 *  newcomer's first record mustn't land in a colleague's notebook just because it's first in the list. */
export function startingNotebook(notebooks: Notebook[], userId: number, defaultId?: number | null, here?: number): number | '' {
  if (here !== undefined && notebooks.some(n => n.id === here)) return here
  if (defaultId && notebooks.some(n => n.id === defaultId)) return defaultId
  const mine = notebooks.filter(n => n.owner?.id === userId).sort((a, b) => b.created_at.localeCompare(a.created_at))
  return mine[0]?.id ?? ''
}

export default function NewExperimentDialog({ notebookId, onClose }: { notebookId?: number; onClose: () => void }) {
  const { notebooks, refreshNotebooks, user } = useApp()
  const nav = useNavigate()
  const [title, setTitle] = useState('')
  const [nb, setNb] = useState<number | ''>(() => startingNotebook(notebooks, user.id, user.preferences?.default_notebook_id, notebookId))
  const [tpl, setTpl] = useState<number | ''>('')
  const [templates, setTemplates] = useState<ExperimentSummary[]>([])
  const [err, setErr] = useState('')
  const [making, setMaking] = useState(false)

  useEffect(() => { api.get<ExperimentSummary[]>('/api/experiments?templates=true').then(setTemplates) }, [])

  const mine = notebooks.filter(n => n.owner?.id === user.id)
  const others = notebooks.filter(n => n.owner?.id !== user.id)

  const create = async () => {
    try {
      const e = await api.post<ExperimentSummary>('/api/experiments', {
        title, notebook_id: nb || null, template_id: tpl || null,
      })
      await refreshNotebooks()
      onClose()
      nav(`/experiments/${e.id}`)
    } catch (e: any) { setErr(e.message) }
  }

  return (
    <Modal title="New experiment" onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!title.trim() || !nb} onClick={create}>Create experiment</button>
    </>}>
      <div className="stack">
        <label className="field">Title<input className="input" autoFocus maxLength={300} value={title} onChange={e => setTitle(e.target.value)} placeholder="e.g. Boc protection of 4-aminopiperidine" /></label>
        <label className="field">Notebook
          <select className="select" value={nb} onChange={e => setNb(e.target.value ? Number(e.target.value) : '')}>
            {nb === '' && <option value="">Choose a notebook</option>}
            {mine.length > 0 && <optgroup label="Your notebooks">
              {mine.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
            </optgroup>}
            {others.length > 0 && <optgroup label="Other people's notebooks">
              {others.map(n => <option key={n.id} value={n.id}>{n.name} ({displayName(n.owner)})</option>)}
            </optgroup>}
          </select>
        </label>
        {!mine.length && <p className="faint" style={{ margin: 0 }}>
          You don't have a notebook of your own yet. Experiments can't be deleted (only voided), so make your own
          notebook to keep yours in.{' '}<button className="btn sm" type="button" onClick={() => setMaking(true)}>Make your own notebook</button>
        </p>}
        <label className="field">Start from
          <select className="select" value={tpl} onChange={e => setTpl(e.target.value ? Number(e.target.value) : '')}>
            <option value="">Standard sections (aim, reaction, procedure, results)</option>
            {templates.map(t => <option key={t.id} value={t.id}>Template: {t.title}</option>)}
          </select>
        </label>
        {err && <p className="error">{err}</p>}
      </div>
      {making && <NewNotebookDialog onClose={() => setMaking(false)} onCreated={id => { setNb(id); setMaking(false) }} />}
    </Modal>
  )
}
