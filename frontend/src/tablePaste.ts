// Pasting cells from a spreadsheet into a Data table section (0.36), and into a plugin section's table (0.36.1, below).
// No imports, so `npm test` can check it directly.
//
// Pasted into a table that's still as it was added (its own column names, every cell empty), a first line that looks
// like column headings becomes the column names instead of the first row. The section says so, with a way to keep it as
// data instead (src/components/TableEntry.tsx).

export interface Col { name: string; unit?: string }

/** The tab-separated lines a spreadsheet puts on the clipboard, as rows of cells (blank lines dropped). */
export const parseTsv = (text: string): string[][] =>
  text.replace(/\r/g, '').split('\n').filter(Boolean).map(l => l.split('\t'))

/** A number as a spreadsheet would copy it: 12, -0.5, 1,000, 1 000, 2.5e-3, 45%. */
const NUMBER = /^[-+−]?(\d{1,3}([,\s  ]\d{3})+|\d+)?([.,]\d+)?([eE][-+]?\d+)?\s*%?$/
export const isNumber = (cell: string): boolean => {
  const c = cell.trim()
  return /\d/.test(c) && NUMBER.test(c)
}

/** A column name the table gave itself: Sample and Value (a new section's), Column 3 and so on, or none. */
const ownName = (name: string, i: number) => {
  const n = name.trim()
  return n === '' || n === `Column ${i + 1}` || (i === 0 && n === 'Sample') || (i === 1 && n === 'Value')
}

/** Is the table still as it was added: its own column names, and nothing typed in any cell? */
export const isBlankTable = (cols: Col[], rows: string[][]): boolean =>
  cols.every((c, i) => ownName(c.name || '', i)) && rows.every(r => r.every(v => !String(v ?? '').trim()))

/** Does the first pasted line look like column headings: every cell filled in and none a number, with at least one
 *  more line after it? (A heading row of numbers, such as doses or time points, is left as data.) */
export const looksLikeHeader = (grid: string[][]): boolean =>
  grid.length >= 2 && grid[0].length > 0 && grid[0].every(c => c.trim() !== '' && !isNumber(c))

/** Put pasted cells into the table at row `ri`, column `ci`, adding rows and columns as needed. With `header`, the
 *  first line names the columns instead (from column `ci` on) and the rest goes in from row `ri`. */
export function pasteCells(cols: Col[], rows: string[][], grid: string[][], ri: number, ci: number, header: boolean): { cols: Col[]; rows: string[][] } {
  const body = header ? grid.slice(1) : grid
  const width = Math.max(cols.length, ci + Math.max(0, ...grid.map(g => g.length)))
  const nc = cols.map(c => ({ ...c }))
  while (nc.length < width) nc.push({ name: `Column ${nc.length + 1}` })
  if (header) grid[0].forEach((name, j) => { nc[ci + j] = { ...nc[ci + j], name: name.trim() } })
  const nr = rows.map(r => [...r, ...Array(Math.max(0, width - r.length)).fill('')])
  body.forEach((g, i) => {
    while (nr.length <= ri + i) nr.push(Array(width).fill(''))
    g.forEach((v, j) => { nr[ri + i][ci + j] = v })
  })
  return { cols: nc, rows: nr }
}

/** What a paste does: { cols, rows, header } where header says the first line became the column names. */
export function paste(cols: Col[], rows: string[][], text: string, ri: number, ci: number): { cols: Col[]; rows: string[][]; header: boolean } {
  const grid = parseTsv(text)
  const header = isBlankTable(cols, rows) && looksLikeHeader(grid)
  return { ...pasteCells(cols, rows, grid, ri, ci, header), header }
}

// ---------------------------------------------------------------- a plugin section's table (0.36.1)
// Its columns are fixed and typed (number, choice, tick box, date…). Pasted cells fill rows and columns from the cell
// pasted into, adding rows as needed; a cell its column can't take is left as it was and counted, never stored
// silently as something else (0.36: the whole block went into the first cell, and nothing was saved).

export interface FieldCol { id: string; label: string; type: string; unit?: string; options?: string[] }
export type FieldRow = Record<string, unknown>

/** A new, empty row, as "+ Add row" makes it. */
export const blankFieldRow = (cols: FieldCol[]): FieldRow =>
  Object.fromEntries(cols.map(c => [c.id, c.type === 'checkbox' ? false : null]))

const YES = ['yes', 'y', 'true', '1', 'x', '✓', '✔', 'done', 'ok']
const NO = ['no', 'n', 'false', '0', '']

/** A pasted cell as its column takes it, by the same rules as typing: { value }, or { skip } saying what the column
 *  takes. An empty cell empties the cell. */
export function fieldValue(c: FieldCol, raw: string): { value: unknown } | { skip: string } {
  const t = raw.trim()
  if (c.type === 'checkbox') {
    if (YES.includes(t.toLowerCase())) return { value: true }
    if (NO.includes(t.toLowerCase())) return { value: false }
    return { skip: 'takes a tick (yes or no)' }
  }
  if (t === '') return { value: null }
  if (c.type === 'number') {
    // as typed: "1,000" could be one thousand or one, so it isn't kept (src/components/bits.tsx, amountProblem)
    if (/^[-+]?[1-9]\d{0,2},\d{3}$/.test(t)) return { skip: 'takes numbers only (type 1000 or 1.000 without the comma)' }
    const n = Number(t.replace(',', '.'))
    return Number.isFinite(n) && /\d/.test(t) ? { value: n } : { skip: 'takes numbers only' }
  }
  if (c.type === 'select') {
    const o = (c.options || []).find(x => x.trim().toLowerCase() === t.toLowerCase())
    return o !== undefined ? { value: o } : { skip: `takes ${(c.options || []).join(' or ')}` }
  }
  if (c.type === 'date') return /^\d{4}-\d{2}-\d{2}$/.test(t) ? { value: t } : { skip: 'takes dates written as 2026-11-30' }
  if (c.type === 'datetime') {
    const m = t.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/)
    return m ? { value: `${m[1]}T${m[2]}` } : { skip: 'takes dates and times written as 2026-11-30 14:30' }
  }
  return { value: raw.replace(/\r/g, '') }
}

/** Is this pasted line the table's own headings (each cell its column's label, with or without its unit, in any case)? */
export function isFieldHeader(cols: FieldCol[], ci: number, line: string[]): boolean {
  const cells = line.slice(0, Math.max(0, cols.length - ci))
  const norm = (s: string) => s.trim().toLowerCase()
  return cells.length > 0 && cells.every((cell, j) => {
    const c = cols[ci + j]
    return norm(cell) !== '' && [c.label, c.unit ? `${c.label} (${c.unit})` : c.label].some(x => norm(x) === norm(cell))
  })
}

export interface FieldPaste { rows: FieldRow[]; header: boolean; filled: number; added: number; extra: number; skipped: { row: number; column: string; why: string }[] }

/** Paste spreadsheet cells into a plugin table at row `ri`, column `ci`. */
export function pasteFields(cols: FieldCol[], rows: FieldRow[], text: string, ri: number, ci: number): FieldPaste {
  let grid = parseTsv(text)
  const header = grid.length > 0 && isFieldHeader(cols, ci, grid[0])
  if (header) grid = grid.slice(1)
  const out = rows.map(r => ({ ...r }))
  const before = out.length
  const res: FieldPaste = { rows: out, header, filled: 0, added: 0, extra: 0, skipped: [] }
  grid.forEach((line, i) => {
    while (out.length <= ri + i) out.push(blankFieldRow(cols))
    line.forEach((raw, j) => {
      const c = cols[ci + j]
      if (!c) { if (raw.trim()) res.extra++; return }
      const v = fieldValue(c, raw)
      if ('skip' in v) res.skipped.push({ row: ri + i + 1, column: c.label, why: v.skip })
      else { out[ri + i][c.id] = v.value; res.filled++ }
    })
  })
  res.added = out.length - before
  return res
}

/** What to tell the person after a paste, or '' when it all went in as it was. */
export function fieldPasteMessage(p: FieldPaste): string {
  const bits: string[] = []
  if (p.skipped.length) {
    const by = new Map<string, number>()
    for (const s of p.skipped) by.set(`${s.column} ${s.why}`, (by.get(`${s.column} ${s.why}`) || 0) + 1)
    const n = p.skipped.length
    bits.push(`${n} cell${n === 1 ? '' : 's'} not pasted, left as ${n === 1 ? 'it was' : 'they were'}: ` +
      [...by].map(([k, c]) => `${k}${by.size > 1 || c > 1 ? ` (${c})` : ''}`).join('; ') + '.')
  }
  if (p.header) bits.push('The heading row was left out.')
  if (p.extra) bits.push(`${p.extra} cell${p.extra === 1 ? '' : 's'} beyond the last column ${p.extra === 1 ? 'was' : 'were'} left out.`)
  return bits.join(' ')
}
