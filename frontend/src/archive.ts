/** The archive PDF (0.34): what the page says and decides, as plain data. No imports, so `npm test` checks it
 *  directly; pages/Archive.tsx draws the spectra (spectrumDraw.ts) and talks to the server. */

export type PageSize = 'letter' | 'a4'
/** From and To are days ('2026-11-03'), both included, in this browser's calendar. */
export interface Choices { from: string; to: string; notebook_id: number | null; author_id: number | null; page_size: PageSize }
export interface Counts { records: number; witnessed: number; awaiting_witness: number; voided: number }
export interface Preview extends Counts {
  files: number | null; scope: string; title: string; label: string; phrase: string; from_date: string; to_date: string
}
export interface ArchiveSpectrum {
  attachment_id: number; experiment_id: number; eid: string; filename: string; size: number; sha256: string
  technique: string; conditions: string; analysis: unknown
  product: { name?: string | null; smiles?: string | null; formula?: string | null; mw?: number | null } | null
  done: boolean
}
export type JobState = 'drawing' | 'queued' | 'building' | 'done' | 'stopped' | 'failed'
export interface ArchiveJob {
  id: string; state: JobState; scope: string; counts: Counts; title: string; file_name: string
  params: { from_date: string; to_date: string; tz: string; notebook_id: number | null; author_id: number | null; page_size: PageSize }
  progress: { done: number; total: number; message: string }; error: string; pages: number | null; bytes: number | null
  created_at: string; spectra_total: number; spectra_done: number; spectra_drawn: number; spectra?: ArchiveSpectrum[]
}
export interface Options {
  notebooks: { id: number; name: string }[]; authors: { id: number; name: string }[]; job: ArchiveJob | null; pdfa: boolean
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

// ------------------------------------------------------------------ the days it covers

const pad = (n: number) => String(n).padStart(2, '0')
/** A day in this browser's calendar, as '2026-11-03'. */
export const ymd = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
const dayOf = (s: string) => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d) }
const addDays = (d: Date, n: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n)

export interface QuickPick { label: string; from: string; to: string }

/** The quick choices: this week (from Monday) and last week, this month and last, this year and last, and the four most
 *  recent full calendar years. "This …" runs to today: nothing is signed in the future. */
export function quickPicks(now = new Date()): QuickPick[] {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const monday = addDays(today, -((today.getDay() + 6) % 7))
  const y = today.getFullYear(), m = today.getMonth()
  const span = (label: string, a: Date, b: Date): QuickPick => ({ label, from: ymd(a), to: ymd(b) })
  return [
    span('This week', monday, today),
    span('Last week', addDays(monday, -7), addDays(monday, -1)),
    span('This month', new Date(y, m, 1), today),
    span('Last month', new Date(y, m - 1, 1), new Date(y, m, 0)),
    span('This year', new Date(y, 0, 1), today),
    span('Last year', new Date(y - 1, 0, 1), new Date(y - 1, 11, 31)),
    ...[1, 2, 3, 4].map(k => span(String(y - k), new Date(y - k, 0, 1), new Date(y - k, 11, 31))),
  ]
}

/** What the page starts with: this year so far. */
export const defaultSpan = (now = new Date()): QuickPick => quickPicks(now).find(p => p.label === 'This year')!

/** Why From and To can't be used as they are, or '' when they can. */
export function rangeProblem(from: string, to: string): string {
  const ok = (s: string) => /^\d{4}-\d{2}-\d{2}$/.test(s) && ymd(dayOf(s)) === s
  if (!ok(from) || !ok(to)) return 'Choose a “From” and a “To” day.'
  if (from > to) return '“From” is later than “To”: choose a “From” day on or before the “To” day.'
  return ''
}

/** When From starts and the day after To starts, by this browser's clock, in UTC: what the server compares signing
 *  times with, so a week is this person's week wherever Benchtop runs (and across a change of the clocks). */
export function instants(from: string, to: string): { start: string; end: string } {
  return { start: dayOf(from).toISOString(), end: addDays(dayOf(to), 1).toISOString() }
}

/** The days and the choices, as POST /api/archive/jobs takes them. */
export function spanBody(c: Choices, tz = ''): Record<string, string | number | null> {
  return { from_date: c.from, to_date: c.to, ...instants(c.from, c.to), tz, notebook_id: c.notebook_id, author_id: c.author_id,
    page_size: c.page_size }
}

/** The query for GET /api/archive/preview: the same, without the page size. */
export function previewQuery(c: Choices, tz = ''): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(spanBody(c, tz))) if (v != null && v !== '' && k !== 'page_size') q.set(k, String(v))
  return q.toString()
}

// ------------------------------------------------------------------ the job

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
