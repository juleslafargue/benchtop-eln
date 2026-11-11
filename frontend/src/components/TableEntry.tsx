import { useApp } from '../App'
import type { EntryEditorProps } from './EntryCard'
import { type Col, parseTsv, paste, pasteCells } from '../tablePaste'

/** A column is as wide as its name and what's in it, up to a point; the table scrolls sideways when they don't fit
 *  (0.36: long names pasted from a spreadsheet were cut off). */
const columnWidth = (name: string, rows: string[][], ci: number) =>
  `${Math.min(36, Math.max(8, name.length + 2, ...rows.map(r => String(r[ci] ?? '').length + 2)))}ch`

export default function TableEntry({ content, onChange, readOnly }: EntryEditorProps) {
  const { toast } = useApp()
  const cols: Col[] = content.columns || []
  const rows: string[][] = content.rows || []
  const set = (c: Col[], r: string[][]) => onChange({ ...content, columns: c, rows: r })

  /** Cells copied from a spreadsheet. Into a table that's still blank, a first line of headings names the columns
   *  (0.36: they went into row 1, under "Sample / Value / Column 3"), with a way to keep it as data instead. */
  const pasteTsv = (e: React.ClipboardEvent, ri: number, ci: number) => {
    const text = e.clipboardData.getData('text')
    if (!text.includes('\t') && !text.includes('\n')) return
    e.preventDefault()
    const r = paste(cols, rows, text, ri, ci)
    set(r.cols, r.rows)
    if (r.header) {
      const asData = pasteCells(cols, rows, parseTsv(text), ri, ci, false)
      toast('First row used as column names', { label: 'Keep it as data', run: () => set(asData.cols, asData.rows) })
    }
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="grid-wrap">
        <table className="data">
          <thead><tr>
            {cols.map((c, ci) => (
              <th key={ci}><input aria-label={`Column ${ci + 1} name`} disabled={readOnly} value={c.name} title={c.name}
                style={{ minWidth: columnWidth(c.name, rows, ci) }}
                onChange={e => set(cols.map((x, j) => (j === ci ? { ...x, name: e.target.value } : x)), rows)} /></th>
            ))}
            {!readOnly && <th style={{ width: 34 }}><button className="btn ghost sm" aria-label="Add column"
              onClick={() => set([...cols, { name: `Column ${cols.length + 1}` }], rows.map(r => [...r, '']))}>+</button></th>}
          </tr></thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                {cols.map((_, ci) => (
                  <td key={ci}><input aria-label={`Row ${ri + 1} ${cols[ci].name}`} disabled={readOnly} value={r[ci] ?? ''}
                    onPaste={e => pasteTsv(e, ri, ci)}
                    onChange={e => set(cols, rows.map((x, j) => (j === ri ? cols.map((_, k) => (k === ci ? e.target.value : x[k] ?? '')) : x)))} /></td>
                ))}
                {!readOnly && <td><button className="btn ghost sm" aria-label={`Remove row ${ri + 1}`} onClick={() => set(cols, rows.filter((_, j) => j !== ri))}>×</button></td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!readOnly && <div className="row">
        <button className="btn sm" onClick={() => set(cols, [...rows, cols.map(() => '')])}>Add row</button>
        {cols.length > 1 && <button className="btn ghost sm" onClick={() => set(cols.slice(0, -1), rows.map(r => r.slice(0, cols.length - 1)))}>Remove last column</button>}
        <span className="faint">Paste cells from Excel or Numbers to fill the table.</span>
      </div>}
    </div>
  )
}
