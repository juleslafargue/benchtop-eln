// Which of Delete, Archive and Unarchive a notebook's page offers. Records are never deleted, so only an empty notebook
// (no experiment of any kind in it) can be deleted; one with experiments can be archived instead. Only its owner or an
// admin sees these, never a read-only account. The server checks the same rules (DELETE / PATCH /api/notebooks/{id}).

export interface NotebookLike { empty: boolean; archived: boolean; owner: { id: number } }
export interface PersonLike { id: number; role: 'admin' | 'scientist' | 'viewer' }

export function notebookActions(nb: NotebookLike, me: PersonLike): { delete: boolean; archive: boolean; unarchive: boolean } {
  const manages = me.role !== 'viewer' && (me.role === 'admin' || nb.owner.id === me.id)
  if (!manages) return { delete: false, archive: false, unarchive: false }
  return { delete: nb.empty, archive: !nb.empty && !nb.archived, unarchive: nb.archived }
}
