// What's still empty in a record about to be signed: shown in the sign dialog as a reminder, nothing more. It never
// stops signing, and it isn't sent with the signature: what a signature covers is worked out by the server alone.

interface EntryLike { kind: string; title: string; content: any }

const textOf = (html: unknown) => typeof html === 'string'
  ? html.replace(/<img\b[^>]*>/gi, ' picture ').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').trim() : ''
const filled = (v: unknown) => v !== null && v !== undefined && String(v).trim() !== ''

/** Is this section as it was when it was added (nothing typed, picked or attached)? */
function isEmpty(e: EntryLike): boolean {
  const c = e.content || {}
  switch (e.kind) {
    case 'text': return !textOf(c.html)
    case 'reaction': return !(c.rows || []).length && !c.reaction_smiles
    case 'table': return !(c.rows || []).some((r: unknown[]) => Array.isArray(r) && r.some(filled))
    case 'file': return !(c.attachment_ids || []).length
    case 'purification': return !['stationary_phase', 'eluent', 'rf', 'rf_eluent', 'isolated_mg', 'appearance'].some(k => filled(c[k])) && !textOf(c.notes_html)
    case 'characterization': return !(c.items || []).some((it: any) => it && (filled(it.data) || it.attachment_id))
    case 'references': return !(c.items || []).length
    case 'custom': return !Object.values(c.values || {}).some(v => v !== false && filled(v))
    default: return false  // safety is said separately; a screening grid always has its levels
  }
}

/** The empty sections by title, and what else is missing: { sections: ["Procedure"], notes: ["no isolated mass", "hazards not recorded"] } */
export function stillEmpty(entries: EntryLike[]): { sections: string[]; notes: string[] } {
  const sections = entries.filter(e => e.kind !== 'safety' && isEmpty(e)).map(e => e.title || 'Untitled section')
  const notes: string[] = []
  const products = entries.filter(e => e.kind === 'reaction').flatMap(e => (e.content?.rows || []).filter((r: any) => r?.role === 'product'))
  const isolated = products.some((r: any) => filled(r.actual_mass_mg)) || entries.some(e => e.kind === 'purification' && filled(e.content?.isolated_mg))
  if (products.length && !isolated) notes.push('no isolated mass')
  const safety = entries.filter(e => e.kind === 'safety')
  if (safety.length && !safety.some(e => e.content?.snapshot)) notes.push('hazards not recorded')
  return { sections, notes }
}

/** "Procedure, Observations & results · no isolated mass · hazards not recorded", or '' when nothing is missing */
export function reminderText(entries: EntryLike[]): string {
  const { sections, notes } = stillEmpty(entries)
  return [sections.join(', '), ...notes].filter(Boolean).join(' · ')
}
