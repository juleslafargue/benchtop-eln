import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useApp } from '../App'
import { api, displayName, type Group, type User } from '../api'
import Modal from '../components/Modal'
import { initials } from '../components/Layout'

function GroupForm({ g, onClose, onSaved }: { g?: Group; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(g?.name || '')
  const [description, setDescription] = useState(g?.description || '')
  const [iLead, setILead] = useState(true)  // 0.35: whoever makes the group usually leads it
  const [err, setErr] = useState('')
  const save = async () => {
    try {
      if (g) await api.patch(`/api/groups/${g.id}`, { name, description })
      else await api.post('/api/groups', { name, description, i_lead: iLead })
      onSaved()
    } catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title={g ? `Edit ${g.name}` : 'New group'} onClose={onClose} footer={<>
      <button className="btn" onClick={onClose}>Cancel</button>
      <button className="btn primary" disabled={!name.trim()} onClick={save}>{g ? 'Save' : 'Create group'}</button></>}>
      <div className="stack">
        <label className="field">Name<input className="input" autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Batey group, Process chemistry" /></label>
        <label className="field">Description<textarea className="input" rows={2} value={description} onChange={e => setDescription(e.target.value)} /></label>
        {!g && <label className="check-row"><input type="checkbox" checked={iLead} onChange={e => setILead(e.target.checked)} /> I lead this group</label>}
        {!g && <p className="faint" style={{ margin: 0 }}>After creating it, add people{iLead ? '' : ' and choose who leads it'}. Leads (a PI, a manager) can see every notebook in the group, add and remove members, and share the group's experiments.</p>}
        {err && <p className="error">{err}</p>}
      </div>
    </Modal>
  )
}

export default function Groups() {
  const { user, groups, refreshGroups, refreshNotebooks, notebooks, toast } = useApp()
  const [users, setUsers] = useState<User[]>([])
  const [editing, setEditing] = useState<Group | 'new' | null>(null)
  const [adding, setAdding] = useState<Record<number, string>>({})
  const [confirmDelete, setConfirmDelete] = useState<Group | null>(null)
  useEffect(() => { refreshGroups(); api.get<User[]>('/api/users').then(setUsers).catch(() => {}) }, [refreshGroups])

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    try { await fn(); await refreshGroups(); await refreshNotebooks(); if (msg) toast(msg) } catch (e: any) { toast(e.message) }
  }
  const mine = groups.filter(g => g.my_role)
  const others = groups.filter(g => !g.my_role)
  const card = (g: Group) => {
    const nbs = notebooks.filter(n => n.group?.id === g.id)
    const candidates = users.filter(u => u.active && !g.members.some(m => m.id === u.id))
    return (
      <section key={g.id} className="panel group-card">
        <header className="group-head">
          <div>
            <h2>{g.name}</h2>
            {g.description && <p className="faint" style={{ margin: '2px 0 0' }}>{g.description}</p>}
          </div>
          <div className="row" style={{ gap: 6 }}>
            {g.my_role && <span className={`tag${g.my_role === 'lead' ? ' lead' : ''}`}>{g.my_role === 'lead' ? 'You lead this group' : 'Member'}</span>}
            {g.can_manage && <button className="btn ghost sm" onClick={() => setEditing(g)}>Edit</button>}
            {user.role === 'admin' && <button className="btn ghost sm" onClick={() => setConfirmDelete(g)}>Delete</button>}
          </div>
        </header>
        <ul className="member-list">
          {g.members.map(m => (
            <li key={m.id}>
              <span className="avatar sm" aria-hidden>{initials(m.full_name || m.username)}</span>
              <span className="member-name">{displayName(m)} <span className="faint">@{m.username}</span></span>
              {g.can_manage
                ? <select className="select sm" value={m.role} aria-label={`Role of ${displayName(m)}`}
                    onChange={e => act(() => api.put(`/api/groups/${g.id}/members/${m.id}`, { role: e.target.value }))}>
                    <option value="member">Member</option><option value="lead">Lead</option></select>
                : <span className={`tag${m.role === 'lead' ? ' lead' : ''}`}>{m.role === 'lead' ? 'Lead' : 'Member'}</span>}
              {(g.can_manage || m.id === user.id) && <button className="btn ghost sm" onClick={() => act(() => api.del(`/api/groups/${g.id}/members/${m.id}`),
                m.id === user.id ? `You left ${g.name}` : `Removed ${displayName(m)}`)}>{m.id === user.id ? 'Leave' : 'Remove'}</button>}
            </li>
          ))}
          {g.members.length === 0 && <li className="faint">No members yet.</li>}
        </ul>
        {g.can_manage && candidates.length > 0 && (
          <div className="row add-member">
            <select className="select" value={adding[g.id] || ''} aria-label={`Add someone to ${g.name}`} onChange={e => setAdding({ ...adding, [g.id]: e.target.value })}>
              <option value="">Add someone…</option>
              {candidates.map(u => <option key={u.id} value={u.id}>{u.full_name || u.username} (@{u.username})</option>)}
            </select>
            <button className="btn sm" disabled={!adding[g.id]} onClick={() => act(async () => {
              await api.put(`/api/groups/${g.id}/members/${adding[g.id]}`, { role: g.members.length ? 'member' : 'lead' })
              setAdding({ ...adding, [g.id]: '' })
            })}>Add{g.members.length ? '' : ' as lead'}</button>
          </div>
        )}
        <div className="faint group-nbs">
          {nbs.length ? <>Notebooks: {nbs.map((n, i) => <span key={n.id}>{i ? ', ' : ''}<Link to={`/notebooks/${n.id}`}>{n.name}</Link></span>)}</>
            : g.notebook_count ? `${g.notebook_count} notebook${g.notebook_count === 1 ? '' : 's'} (not visible to you)` : 'No notebooks yet.'}
        </div>
      </section>
    )
  }

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="spread">
        <div>
          <h1 className="page-title">Groups</h1>
          <p className="lede">Research groups and teams. A notebook can be open to everyone, to one group, or kept private; group leads see all of their group's notebooks.</p>
        </div>
        {user.role === 'admin' && <button className="btn primary" onClick={() => setEditing('new')}>New group</button>}
      </div>
      {groups.length === 0 && <div className="panel empty-state">
        <p><b>No groups yet.</b></p>
        <p className="faint">{user.role === 'admin' ? 'Create one for each research group or team, add its people, and pick its lead.' : 'An admin can create groups for each research group or team.'} Until then every notebook is open to everyone, as before.</p>
      </div>}
      {mine.length > 0 && <>{others.length > 0 && <h3 className="section-label">Your groups</h3>}{mine.map(card)}</>}
      {others.length > 0 && <>{mine.length > 0 && <h3 className="section-label">Other groups</h3>}{others.map(card)}</>}
      {editing && <GroupForm g={editing === 'new' ? undefined : editing} onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); act(async () => {}, editing === 'new' ? 'Group created' : 'Saved') }} />}
      {confirmDelete && <Modal title={`Delete ${confirmDelete.name}?`} onClose={() => setConfirmDelete(null)} footer={<>
        <button className="btn" onClick={() => setConfirmDelete(null)}>Cancel</button>
        <button className="btn danger" onClick={() => { const g = confirmDelete; setConfirmDelete(null); act(() => api.del(`/api/groups/${g.id}`), `Deleted ${g.name}`) }}>Delete group</button></>}>
        <p style={{ marginTop: 0 }}>Its {confirmDelete.notebook_count} notebook{confirmDelete.notebook_count === 1 ? '' : 's'} stay with their owners. Notebooks that were open only to this group become private, so nobody gains access they didn't have.</p>
      </Modal>}
    </div>
  )
}
