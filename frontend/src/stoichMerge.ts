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
