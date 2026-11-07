/** The yearly archive (0.34): what the page says and decides, as plain data. No imports, so `npm test` checks it
 *  directly; pages/Archive.tsx draws the spectra (spectrumDraw.ts) and talks to the server. */

export type PageSize = 'letter' | 'a4'
export interface Choices { year: number | null; notebook_id: number | null; author_id: number | null; page_size: PageSize }
export interface Counts { records: number; witnessed: number; awaiting_witness: number; voided: number }
export interface Preview extends Counts { files: number | null; scope: string; year: number }
export interface ArchiveSpectrum {
  attachment_id: number; experiment_id: number; eid: string; filename: string; size: number; sha256: string
  technique: string; conditions: string; analysis: unknown
  product: { name?: string | null; smiles?: string | null; formula?: string | null; mw?: number | null } | null
  done: boolean
}
export type JobState = 'drawing' | 'queued' | 'building' | 'done' | 'stopped' | 'failed'
export interface ArchiveJob {
  id: string; state: JobState; params: Choices & { year: number }; scope: string; counts: Counts; title: string; file_name: string
  progress: { done: number; total: number; message: string }; error: string; pages: number | null; bytes: number | null
  created_at: string; spectra_total: number; spectra_done: number; spectra_drawn: number; spectra?: ArchiveSpectrum[]
}
export interface Options {
  years: { year: number; records: number }[]; notebooks: { id: number; name: string }[]; authors: { id: number; name: string }[]
  job: ArchiveJob | null
}

/** Spectra in the archive: the viewer's Export PNG drawing, at about 300 dpi across the width of a page
 *  (2200 pixels over 18 cm). */
export const PRINT = { w: 1000, h: 420, scale: 2.2 }

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

/** "85 records: 70 witnessed, 12 awaiting witness, 3 voided" */
export function countLine(c: Counts): string {
  if (!c.records) return 'No records'
  const parts = [`${c.witnessed} witnessed`, `${c.awaiting_witness} awaiting witness`, ...(c.voided ? [`${c.voided} voided`] : [])]
  return `${plural(c.records, 'record')}: ${parts.join(', ')}`
}

/** The query for GET /api/archive/preview. */
export function previewQuery(c: Choices): string {
  const q = new URLSearchParams()
  if (c.year != null) q.set('year', String(c.year))
  if (c.notebook_id != null) q.set('notebook_id', String(c.notebook_id))
  if (c.author_id != null) q.set('author_id', String(c.author_id))
  return q.toString()
}

/** The year to offer first: the one asked for if it has records, else the latest with records, else this year. */
export function defaultYear(years: { year: number }[], asked: number | null, now = new Date()): number {
  if (asked != null && years.some(y => y.year === asked)) return asked
  return years.length ? Math.max(...years.map(y => y.year)) : now.getUTCFullYear()
}

/** The spectra still to draw, in order (on reopening the page, those drawn before are kept). */
export const toDraw = (spectra: ArchiveSpectrum[]) => spectra.filter(s => !s.done)

/** What the page says while a job runs. `drawing` is the spectrum the browser is drawing now (1-based). */
export function progressLine(job: Pick<ArchiveJob, 'state' | 'progress' | 'spectra_total' | 'spectra_done' | 'error'>, drawing?: number): string {
  switch (job.state) {
    case 'drawing':
      return job.spectra_total ? `Drawing spectrum ${Math.min(drawing ?? job.spectra_done + 1, job.spectra_total)} of ${job.spectra_total}` : 'Getting ready'
    case 'queued': return job.progress.message || 'Waiting for another archive to finish'
    case 'building': return job.progress.message || 'Laying out the PDF'
    case 'done': return 'Ready'
    default: return job.error || 'This archive stopped. Start it again.'
  }
}

/** How far along, from 0 to 1: drawing is the first half when there are spectra, laying out the rest. */
export function fraction(job: Pick<ArchiveJob, 'state' | 'progress' | 'spectra_total' | 'spectra_done'>): number {
  const draw = job.spectra_total ? 0.5 : 0
  if (job.state === 'done') return 1
  if (job.state === 'drawing') return job.spectra_total ? draw * job.spectra_done / job.spectra_total : 0
  if (job.state === 'building') return draw + (1 - draw) * (job.progress.total ? Math.min(1, job.progress.done / job.progress.total) : 0)
  return draw
}

export const active = (s: JobState) => s === 'drawing' || s === 'queued' || s === 'building'

/** "2.4 MB" */
export function megabytes(bytes: number | null | undefined): string {
  if (!bytes) return ''
  return bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} kB` : `${(bytes / 1e6).toFixed(bytes < 1e7 ? 1 : 0)} MB`
}
