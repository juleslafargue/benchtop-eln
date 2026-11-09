import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApp } from '../App'
import { api, ApiError, displayName, type ExperimentSummary, type Notebook } from '../api'
import Modal from './Modal'
import { defaultAudience } from './Audience'

/** The notebook a new experiment goes in unless the person picks another: the one they were looking at, their default
 *  notebook, else their own most recent one. Never someone else's: experiments can't be deleted, only voided, so a
 *  newcomer's first record mustn't land in a colleague's notebook just because it's first in the list. */
export function startingNotebook(notebooks: Notebook[], userId: number, defaultId?: number | null, here?: number): number | '' {
  if (here !== undefined && notebooks.some(n => n.id === here)) return here
  if (defaultId && notebooks.some(n => n.id === defaultId)) return defaultId
  const mine = notebooks.filter(n => n.owner?.id === userId).sort((a, b) => b.created_at.localeCompare(a.created_at))
  return mine[0]?.id ?? ''
}

/** The notebook made for someone who has none yet (0.35): "Sam Patel's notebook". */
export const ownNotebookName = (fullName: string, username: string) => `${(fullName || username).trim()}'s notebook`

export default function NewExperimentDialog({ notebookId, onClose }: { notebookId?: number; onClose: () => void }) {
  const { notebooks, refreshNotebooks, user } = useApp()
  const nav = useNavigate()
  const [title, setTitle] = useState('')
  // 0.35: someone with no notebook of their own gets one made with their first experiment ('new'), in one step
  const hasOwn = notebooks.some(n => n.owner?.id === user.id)
  const [nb, setNb] = useState<number | '' | 'new'>(() => startingNotebook(notebooks, user.id, user.preferences?.default_notebook_id, notebookId) || (hasOwn ? '' : 'new'))
  // opened (N) before the notebooks had loaded: the starting one is picked when they arrive, unless one was chosen
  // meanwhile (0.30.1: it stayed on "Choose a notebook")
  const chosen = useRef(false)
  useEffect(() => {
    if (!chosen.current && (nb === '' || nb === 'new') && notebooks.length) setNb(startingNotebook(notebooks, user.id, user.preferences?.default_notebook_id, notebookId) || (hasOwn ? '' : 'new'))
  }, [notebooks]) // eslint-disable-line react-hooks/exhaustive-deps
  const [tpl, setTpl] = useState<number | ''>('')
  const [templates, setTemplates] = useState<ExperimentSummary[]>([])
  const [err, setErr] = useState('')
  const { groups } = useApp()
  const audience = defaultAudience(groups)
  const sharedWith = audience.group_id ? groups.find(g => g.id === audience.group_id)?.name : null
  const newName = ownNotebookName(user.full_name, user.username)
  // one experiment per click: Create is off while it's being made (0.30.1: a double click made two, and experiments
  // can't be deleted). The ref also stops a second click that arrives before the button has redrawn.
  const [creating, setCreating] = useState(false)
  const busy = useRef(false)

  useEffect(() => { api.get<ExperimentSummary[]>('/api/experiments?templates=true').then(setTemplates) }, [])

  const mine = notebooks.filter(n => n.owner?.id === user.id)
  const others = notebooks.filter(n => n.owner?.id !== user.id)

  const ready = !!title.trim() && !!nb
  const why = !title.trim() ? 'Give it a title first.' : !nb ? 'Choose a notebook first.' : ''
  const create = async () => {
    if (busy.current || !ready) return
    busy.current = true; setCreating(true); setErr('')
    let e: ExperimentSummary
    let into = nb
    if (into === 'new') {  // their own notebook, shared as new notebooks are (with their group, if they're in one)
      try {
        into = (await api.post<{ id: number }>('/api/notebooks', { name: newName, description: '', ...audience })).id
        chosen.current = true; setNb(into)
        await refreshNotebooks().catch(() => {})
      } catch (x: any) { busy.current = false; setCreating(false); setErr(x.message); return }
    }
    try {
      e = await api.post<ExperimentSummary>('/api/experiments', {
        title, notebook_id: into || null, template_id: tpl || null,
      })
    } catch (x: any) {
      busy.current = false; setCreating(false)
      if (x instanceof ApiError && x.status === 404 && nb) {  // deleted meanwhile (in another tab): choose another
        setErr('This notebook was deleted, or you can no longer open it. Choose another one.')
        chosen.current = true; setNb(''); refreshNotebooks().catch(() => {})
      } else setErr(x.message)
      return
    }
    await refreshNotebooks().catch(() => {})  // the counts beside the notebooks: the experiment is made either way
    onClose()
    nav(`/experiments/${e.id}`)
  }

  return (
    <Modal title="New experiment" onClose={onClose} footer={<>
      {why && <span className="faint modal-why">{why}</span>}
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!ready || creating} onClick={create}>{creating ? 'Creating…' : 'Create experiment'}</button>
    </>}>
      <div className="stack">
        <label className="field">Title<input className="input" autoFocus maxLength={300} value={title} onChange={e => setTitle(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); create() } }} placeholder="e.g. Boc protection of 4-aminopiperidine" /></label>
        <label className="field">Notebook
          <select className="select" value={nb} onChange={e => { chosen.current = true; setNb(e.target.value === 'new' ? 'new' : e.target.value ? Number(e.target.value) : '') }}>
            {nb === '' && <option value="">Choose a notebook</option>}
            {mine.length > 0 && <optgroup label="Your notebooks">
              {mine.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
            </optgroup>}
            {!mine.length && <option value="new">{newName} (new)</option>}
            {others.length > 0 && <optgroup label="Other people's notebooks">
              {others.map(n => <option key={n.id} value={n.id}>{n.name} ({displayName(n.owner)})</option>)}
            </optgroup>}
          </select>
        </label>
        {!mine.length && nb === 'new' && <p className="faint" style={{ margin: 0 }}>
          You don't have a notebook yet, so {newName} is made for you with this experiment, {sharedWith ? `shared with ${sharedWith}` : 'open to everyone in this Benchtop'}.
          Records are kept for good, so yours go in a notebook of your own.
        </p>}
        <label className="field">Start from
          <select className="select" value={tpl} onChange={e => setTpl(e.target.value ? Number(e.target.value) : '')}>
            <option value="">Standard sections (aim, reaction, procedure, results)</option>
            {templates.map(t => <option key={t.id} value={t.id}>Template: {t.title}</option>)}
          </select>
        </label>
        {err && <p className="error">{err}</p>}
      </div>
    </Modal>
  )
}
