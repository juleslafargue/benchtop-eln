// A recalculated reaction table arrives a moment after it was asked for (2–3 s on a slow network). Meanwhile the
// chemist may have typed elsewhere in the section: a name, a note, the conditions. The answer is merged into the
// section as it is now, never into the copy that was sent (0.30.1: that wiped what was typed in the meantime).

type Row = Record<string, unknown>
interface Content { rows?: object[]; [k: string]: unknown }
export interface StoichAnswer { rows: object[]; totals: unknown; warnings: string[] }

/** Typed into a row without a recalculation: kept as it is now when it changed meanwhile. Anything else that changed
 *  meanwhile means the answer is out of date (null: ask again). */
const NOT_CALCULATED = new Set(['name', 'notes', 'deducted', 'material_id', 'lot_id'])

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/** The section with the server's numbers, keeping everything changed here since `sent` was sent. null when rows were
 *  added, removed or recalculated differently meanwhile: then recalculate the rows as they are now instead. */
export function mergeStoich(sent: Content, now: Content, answer: StoichAnswer): Content | null {
  const before = (sent.rows || []) as Row[], rows = (now.rows || []) as Row[]
  if (rows.length !== before.length || answer.rows.length !== before.length) return null
  const merged: Row[] = []
  for (let i = 0; i < rows.length; i++) {
    const out: Row = { ...(answer.rows[i] as Row) }
    for (const k of new Set([...Object.keys(before[i]), ...Object.keys(rows[i])])) {
      // unchanged here, or changed to what the answer says (a save brought back the server's own recalculation)
      if (same(before[i][k], rows[i][k]) || same(rows[i][k], out[k])) continue
      if (!NOT_CALCULATED.has(k)) return null
      out[k] = rows[i][k]
    }
    merged.push(out)
  }
  return { ...now, rows: merged, totals: answer.totals, warnings: answer.warnings }
}

// "Deduct amounts from inventory" records one withdrawal per row, each a request (seconds on a slow network). Its
// marks ("✓ used") go into the section as it is now, so what was typed meanwhile stays (0.36.4: the section was put
// back as it was when Deduct was clicked, wiping names and notes typed while it ran).
export interface DeductMark { tx_id: number; amount: number; unit: string }
const markOf = (r: Row) => r.deducted as DeductMark | null | undefined

/** The section with `mark` on the row it was made for: the row at `at` if it is still on that lot and unmarked, else
 *  the first unmarked row on that lot (rows were added or removed meanwhile). null when there is no such row. */
export function withDeduction(now: Content, at: number, lotId: unknown, mark: DeductMark): Content | null {
  const rows = (now.rows || []) as Row[]
  const fits = (r: Row | undefined) => !!r && r.lot_id === lotId && !markOf(r)
  const i = fits(rows[at]) ? at : rows.findIndex(fits)
  if (i < 0) return null
  return { ...now, rows: rows.map((r, j) => (j === i ? { ...r, deducted: mark } : r)) }
}

/** May this row (as it is now) still be deducted? The confirmation names rows as they were when it opened. */
export function stillUnmarked(now: Content, at: number, lotId: unknown): boolean {
  const r = ((now.rows || []) as Row[])[at]
  return !!r && r.lot_id === lotId && !markOf(r)
}

/** The section without the mark for this withdrawal (it was undone), wherever that row is now. */
export function withoutDeduction(now: Content, txId: number): Content {
  const rows = (now.rows || []) as Row[]
  return { ...now, rows: rows.map(r => (markOf(r)?.tx_id === txId ? { ...r, deducted: null } : r)) }
}
