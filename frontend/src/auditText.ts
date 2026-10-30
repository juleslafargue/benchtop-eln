// The admin's audit trail in plain sentences ("Nadia Newbie signed EXP-00034"). Only the wording: the events, their
// details and the hash chain are shown unchanged under each sentence, and nothing here touches them.

export interface AuditLike { action: string; object_type: string; object_id: number | null; username: string; details: any }
export interface AuditNames {
  /** username → full name */
  people: Record<string, string>
  /** user id → full name (the account an event is about) */
  userIds: Record<number, string>
  /** group id → group name */
  groups: Record<number, string>
}

const q = (s: unknown) => `“${String(s ?? '')}”`
const list = (xs: string[]) => xs.length <= 1 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs[xs.length - 1]}`
const FIELD: Record<string, string> = { title: 'title', tags: 'tags', notebook_id: 'notebook', status: 'status', full_name: 'name', username: 'username',
  email: 'email', role: 'role', active: 'access', password: 'password', is_template: 'template setting' }

/** One sentence for the common events; null for the rest, which are shown as they're stored. */
export function auditSentence(e: AuditLike, n: AuditNames): string | null {
  const d = e.details || {}
  const who = e.username === 'system' ? 'Benchtop' : n.people[e.username] || e.username
  const person = (u: unknown) => typeof u === 'string' ? n.people[u] || u : typeof u === 'number' ? n.userIds[u] || `account #${u}` : 'someone'
  const target = e.object_id != null ? n.userIds[e.object_id] : undefined
  const exp = d.eid || d.experiment || (e.object_type === 'experiment' && e.object_id != null ? `experiment #${e.object_id}` : 'an experiment')
  switch (`${e.action} ${e.object_type}`) {
    case 'login user': return `${who} signed in`
    case 'logout user': return `${who} signed out`
    case 'login_failed user': return `Someone failed to sign in as ${d.username || 'an unknown user'}${d.reason ? ` (${d.reason})` : ''}`
    case 'password_changed user': return `${who} changed their password`
    case 'sessions_ended user': return d.by_admin ? `${who} signed ${target || 'an account'} out everywhere` : `${who} signed out their other devices`
    case 'update_profile user': return `${who} changed their own settings`
    case 'setup user': return `${who} set up this Benchtop`
    case 'create user': return `${who} added the account ${d.username}${d.role ? ` (${d.role})` : ''}`
    case 'update user': {
      const c = d.changes || {}
      if (c.active === false) return `${who} disabled ${target || 'an account'}`
      if (c.active === true) return `${who} enabled ${target || 'an account'} again`
      if (c.password) return `${who} reset the password of ${target || 'an account'}`
      if (c.role) return `${who} made ${target || 'an account'} ${c.role === 'admin' ? 'an admin' : `a ${c.role}`}`
      return `${who} changed the ${list(Object.keys(c).map(k => FIELD[k] || k))} of ${target || 'an account'}`
    }
    case 'sign:author experiment': return `${who} signed ${exp}`
    case 'sign:witness experiment': return `${who} witnessed ${exp}`
    case 'sign:rejected experiment': return `${who} returned ${exp} to its author`
    case 'sign:withdrawn experiment': return `${who} withdrew their signature from ${exp}`
    case 'sign:void experiment': return `${who} voided ${exp}`
    case 'witness_request experiment': return `${who} asked ${person(d.witness_id)} to witness ${exp}`
    case 'create experiment': return `${who} started ${exp}${d.title ? ` ${q(d.title)}` : ''}`
    case 'update experiment': return `${who} changed the ${list(Object.keys(d.after || {}).map(k => FIELD[k] || k)) || 'details'} of ${exp}`
    case 'clone experiment': return `${who} ${d.as_template ? 'saved' : 'copied'} ${d.source || 'an experiment'}${d.as_template ? ' as a template' : ''}${d.scale && d.scale !== 1 ? ` at ×${d.scale} scale` : ''}`
    case 'comment experiment': return `${who} commented on ${exp}`
    case 'comment_resolve experiment': return `${who} resolved a comment on ${exp}`
    case 'comment_delete experiment': return `${who} deleted a comment on ${exp}`
    case 'share experiment': return `${who} shared ${exp} with ${person(d.with_user)} (${d.permission === 'edit' ? 'can edit' : 'can view'})`
    case 'unshare experiment': return `${who} stopped sharing ${exp} with ${person(d.with_user)}`
    case 'create entry': return `${who} added the section ${q(d.title)} to ${exp}`
    case 'update entry': return `${who} edited a section of ${exp}${d.version ? ` (now version ${d.version})` : ''}`
    case 'delete entry': return `${who} removed the section ${q(d.title)} from ${exp}`
    case 'restore entry': return `${who} put back the section ${q(d.title)} in ${exp}`
    case 'upload attachment': return `${who} added the file ${d.filename} to ${exp}`
    case 'create notebook': return `${who} made the notebook ${q(d.name)}`
    case 'update notebook': {  // the name is recorded from 0.30 on
      const c = d.changes || {}, nb = d.name ? `the notebook ${q(d.name)}` : `notebook #${e.object_id}`
      if (Object.keys(c).length === 1 && c.archived === true) return `${who} archived ${nb}`
      if (Object.keys(c).length === 1 && c.archived === false) return `${who} unarchived ${nb}`
      return null
    }
    case 'delete notebook': return `${who} deleted the empty notebook ${q(d.name)}`
    case 'create group': return `${who} made the group ${q(d.name)}`
    case 'group_member group': return `${who} added ${person(d.member)} to ${e.object_id != null && n.groups[e.object_id] ? n.groups[e.object_id] : 'a group'}${d.role === 'lead' ? ' as a lead' : ''}`
    case 'group_member_remove group': return `${who} took ${person(d.member)} out of ${e.object_id != null && n.groups[e.object_id] ? n.groups[e.object_id] : 'a group'}`
    case 'create material': return `${who} registered ${d.mid || 'a material'}${d.source ? ` from ${d.source}` : ''}`
    case 'create lot': return `${who} added a lot of ${d.material || 'a material'}${d.amount != null ? ` (${d.amount} ${d.unit === 'uL' ? 'µL' : d.unit || ''})` : ''}`
    case 'inventory lot': return typeof d.delta === 'number'
      ? `${who} ${d.delta < 0 ? 'took' : 'added'} ${Math.abs(d.delta)} ${d.unit === 'uL' ? 'µL' : d.unit || ''} ${d.delta < 0 ? 'from' : 'to'} ${d.barcode || 'a lot'}` : null
    case 'zenodo_publish experiment': return `${who} published ${exp} on ${d.site || 'Zenodo'}${d.doi ? ` (DOI ${d.doi})` : ''}`
    default: return null
  }
}
