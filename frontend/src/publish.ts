/** Publishing on Zenodo (0.28): the pieces of the publish dialog that are plain logic. No imports, so `npm test`
 *  can check them directly. */

export interface Creator { name: string; affiliation: string; orcid: string }
export interface PublishDetails {
  title: string; description: string; creators: Creator[]; keywords: string[]
  licence: 'cc-by-4.0' | 'cc0-1.0'; access: 'open' | 'embargoed'; embargo_date: string; include_comments: boolean
}
export interface PublicationInfo {
  status: 'working' | 'draft' | 'failed' | 'published'; site: string; sandbox: boolean; doi: string; doi_url: string
  url: string; draft_url: string; published_at: string | null; error: string
}

/** "suzuki, cross-coupling; Pd" → ["suzuki", "cross-coupling", "Pd"], each once (whatever its capitals). */
export function parseKeywords(text: string): string[] {
  const out: string[] = []
  for (const k of text.split(/[,;\n]/)) {
    const v = k.replace(/\s+/g, ' ').trim().slice(0, 100)
    if (v && !out.some(x => x.toLowerCase() === v.toLowerCase())) out.push(v)
  }
  return out.slice(0, 30)
}

/** yyyy-mm-dd for a date, in the person's own time zone (what a date picker shows). */
export function isoDay(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** The earliest end of an embargo: tomorrow. */
export function firstEmbargoDay(today = new Date()): string {
  const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1)
  return isoDay(d)
}

/** What still stops the details going to Zenodo, in plain words; empty when they're ready. */
export function problems(d: PublishDetails, today = new Date()): string[] {
  const out: string[] = []
  if (!d.title.trim()) out.push('Give it a title.')
  if (!d.description.trim()) out.push('Zenodo needs a description.')
  if (!d.creators.some(c => c.name.trim())) out.push('List at least one author.')
  if (d.access === 'embargoed') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(d.embargo_date)) out.push('Choose the date the files become public.')
    else if (d.embargo_date < firstEmbargoDay(today)) out.push('The embargo has to end after today.')
  }
  return out
}

/** Where a DOI should link: doi.org for a real one; the record itself on the sandbox, whose DOIs don't resolve. */
export function doiHref(p: Pick<PublicationInfo, 'doi_url' | 'url'>): string {
  return p.doi_url || p.url
}

export function fileSize(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`
  if (n >= 1e3) return `${Math.round(n / 1e3)} kB`
  return `${n} bytes`
}
