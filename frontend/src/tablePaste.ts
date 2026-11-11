// Pasting cells from a spreadsheet into a Data table section (0.36). No imports, so `npm test` can check it directly.
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
