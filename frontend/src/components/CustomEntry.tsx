import { useApp } from '../App'
import type { EntryEditorProps } from './EntryCard'
import { NumInput as AmountInput } from './bits'
import { blankFieldRow, fieldPasteMessage, pasteFields } from '../tablePaste'

/** A section whose layout comes from a plugin file. The definition travels with the record
 *  (content.schema), so it renders the same way even if the plugin later changes or is removed. */
export interface CField {
  id: string; label: string; type: 'text' | 'longtext' | 'number' | 'select' | 'checkbox' | 'date' | 'datetime'
  unit?: string; help?: string; placeholder?: string; required?: boolean; options?: string[]
  min?: number; max?: number; warn_below?: number; warn_above?: number; decimals?: number; width?: 'half' | 'third' | 'full'
}
interface CTable { id: string; label: string; columns: CField[]; min_rows: number; help?: string }
interface Schema { plugin: string; id: string; name: string; description: string; fields: CField[]; tables: CTable[] }

const empty = (v: unknown) => v === null || v === undefined || v === false || (typeof v === 'string' && v.trim() === '')

function warning(f: CField, v: unknown): string | null {
  if (f.type !== 'number' || typeof v !== 'number') return null
  if (f.min != null && v < f.min) return `Below the minimum (${f.min}${f.unit ? ' ' + f.unit : ''})`
  if (f.max != null && v > f.max) return `Above the maximum (${f.max}${f.unit ? ' ' + f.unit : ''})`
  if (f.warn_above != null && v > f.warn_above) return `Above ${f.warn_above}${f.unit ? ' ' + f.unit : ''}`
  if (f.warn_below != null && v < f.warn_below) return `Below ${f.warn_below}${f.unit ? ' ' + f.unit : ''}`
  return null
}

function Input({ f, v, set, readOnly, compact, label }: { f: CField; v: any; set: (v: any) => void; readOnly: boolean; compact?: boolean; label?: string }) {
  const common = { disabled: readOnly, 'aria-label': label || f.label, 'aria-required': f.required || undefined }
  if (f.type === 'checkbox') return <input type="checkbox" checked={!!v} onChange={e => set(e.target.checked)} {...common} />
  if (f.type === 'select') return (
    <select className="select" value={v ?? ''} onChange={e => set(e.target.value || null)} {...common}>
      <option value="">{compact ? '' : 'Choose…'}</option>
      {(f.options || []).map(o => <option key={o} value={o}>{o}</option>)}
    </select>)
  if (f.type === 'longtext') return <textarea className="input" rows={compact ? 1 : 3} value={v ?? ''} placeholder={readOnly ? undefined : f.placeholder} onChange={e => set(e.target.value)} {...common} />
  if (f.type === 'number') return <NumInput f={f} v={v} set={set} compact={compact} label={label || f.label} disabled={readOnly} />
  const type = f.type === 'date' ? 'date' : f.type === 'datetime' ? 'datetime-local' : 'text'
  return <input className="input" type={type} value={type === 'datetime-local' ? String(v ?? '').slice(0, 16) : v ?? ''} placeholder={readOnly ? undefined : f.placeholder}
    onChange={e => set(e.target.value || null)} {...common} />
}

// a plugin field's unit that the reaction table's number boxes also convert ("1.2 g" in a mg field is 1200)
const AMOUNT_UNIT: Record<string, 'mg' | 'mL' | 'mmol' | 'h' | 'M' | 'C'> = { mg: 'mg', mL: 'mL', mmol: 'mmol', h: 'h', M: 'M', '°C': 'C' }

/** The reaction table's number box (bits.tsx), so plugin sections behave the same way (0.36.4: "1,000", "25 °C",
 *  "abc" or "1e400" emptied the box and erased the value without a word, "0x10" was 16, Escape didn't put the
 *  value back, and a whole-number field took 0.5). Text that can't be read stays in the box, with why, and the value
 *  is kept; "1 000" is 1000 and "0,5" is 0.5; Escape puts back what was there. Saved after a pause in typing. */
function NumInput({ f, v, set, compact, label, disabled }: { f: CField; v: any; set: (v: any) => void; compact?: boolean; label: string; disabled: boolean }) {
  return (
    <span className="cf-num">
      <AmountInput className="input" label={label} value={typeof v === 'number' ? v : null} onCommit={set} live={600} disabled={disabled}
        unit={f.unit ? AMOUNT_UNIT[f.unit] : undefined} whole={f.decimals === 0} nd={f.decimals ?? 6} placeholder={f.placeholder} required={f.required} />
      {f.unit && !compact && <span className="cf-unit">{f.unit}</span>}
    </span>)
}

export default function CustomEntry({ content, onChange, readOnly }: EntryEditorProps) {
  const { toast } = useApp()
  const schema: Schema | undefined = content?.schema
  if (!schema) return <p className="error">This section's definition is missing.</p>
  const values: Record<string, any> = content.values || {}
  const rows: Record<string, Record<string, any>[]> = content.rows || {}
  const setVal = (id: string, v: any) => onChange({ ...content, values: { ...values, [id]: v } })
  const setRows = (id: string, r: Record<string, any>[]) => onChange({ ...content, rows: { ...rows, [id]: r } })
  const blankRow = (t: CTable) => blankFieldRow(t.columns)
  /** Cells copied from a spreadsheet fill the table from the cell pasted into, each as its column takes it (0.36.1:
   *  the whole block went into that one cell). A single value is left to the cell itself, as typing. */
  const pasteInto = (e: React.ClipboardEvent, t: CTable, r: Record<string, any>[], ri: number, ci: number) => {
    if (readOnly) return
    const text = e.clipboardData.getData('text')
    if (!text.includes('\t') && !text.includes('\n')) return
    e.preventDefault()
    ;(e.target as HTMLElement).blur?.()  // a number box shows what it holds once it isn't being typed in
    const p = pasteFields(t.columns, r, text, ri, ci)
    setRows(t.id, p.rows)
    const say = fieldPasteMessage(p)
    if (say) toast(say)
  }
  const missing = schema.fields.filter(f => f.required && empty(values[f.id])).length
  const checks = schema.fields.filter(f => f.type === 'checkbox')
  const ticked = checks.filter(f => values[f.id]).length

  return (
    <div className="cf">
      {schema.description && <p className="faint cf-desc">{schema.description}</p>}
      {(missing > 0 || checks.length > 2) && <div className={`cf-status${missing ? ' todo' : ' done'}`} role="status">
        {missing ? `${missing} required item${missing === 1 ? '' : 's'} to complete before signing` : 'All required items complete'}
        {checks.length > 2 && <span className="faint"> · {ticked} of {checks.length} ticked</span>}
      </div>}
      <div className="cf-grid">
        {schema.fields.map(f => {
          const v = values[f.id], warn = warning(f, v), need = f.required && empty(v)
          if (f.type === 'checkbox') return (
            <label key={f.id} className={`cf-check${need ? ' need' : ''}${v ? ' on' : ''}`}>
              <Input f={f} v={v} set={x => setVal(f.id, x)} readOnly={readOnly} />
              <span>{f.label}{f.required && <span className="cf-req" title="Required before signing"> *</span>}
                {f.help && <span className="cf-help">{f.help}</span>}</span>
            </label>)
          return (
            <label key={f.id} className={`field cf-field w-${f.width || (f.type === 'longtext' ? 'full' : 'half')}${need ? ' need' : ''}`}>
              <span>{f.label}{f.required && <span className="cf-req" title="Required before signing"> *</span>}</span>
              <Input f={f} v={v} set={x => setVal(f.id, x)} readOnly={readOnly} />
              {warn ? <span className="cf-warn">⚠ {warn}{f.help ? `. ${f.help}` : ''}</span> : f.help && <span className="cf-help">{f.help}</span>}
            </label>)
        })}
      </div>
      {schema.tables.map(t => {
        const r = rows[t.id] || []
        return (
          <div key={t.id} className="cf-table">
            <div className="cf-table-head"><b>{t.label}</b>{t.help && <span className="faint"> · {t.help}</span>}
              {t.min_rows > 0 && <span className="faint"> · at least {t.min_rows} row{t.min_rows === 1 ? '' : 's'}</span>}</div>
            <div className="grid-wrap">
              <table className="grid">
                <thead><tr>{t.columns.map(c => <th key={c.id}>{c.label}{c.unit ? ` (${c.unit})` : ''}{c.required && <span className="cf-req"> *</span>}</th>)}{!readOnly && <th><span className="vh">Remove</span></th>}</tr></thead>
                <tbody>
                  {r.map((row, i) => (
                    <tr key={i}>
                      {t.columns.map((c, ci) => <td key={c.id} className={c.type === 'checkbox' ? 'cf-cb' : undefined} onPaste={e => pasteInto(e, t, r, i, ci)}>
                        <Input f={c} v={row[c.id]} compact label={`${c.label}, row ${i + 1}`} readOnly={readOnly}
                          set={x => setRows(t.id, r.map((rr, j) => j === i ? { ...rr, [c.id]: x } : rr))} /></td>)}
                      {!readOnly && <td className="cf-del"><button className="btn ghost sm" aria-label={`Delete row ${i + 1}`} onClick={() => setRows(t.id, r.filter((_, j) => j !== i))}>×</button></td>}
                    </tr>))}
                  {!r.length && <tr><td colSpan={t.columns.length + 1} className="faint" style={{ padding: 8 }}>No rows yet.</td></tr>}
                </tbody>
              </table>
            </div>
            {!readOnly && <button className="btn ghost sm" onClick={() => setRows(t.id, [...r, blankRow(t)])}>+ Add row</button>}
          </div>)
      })}
      <p className="faint cf-src">Section type “{schema.name}” from the <b>{schema.plugin}</b> plugin.</p>
    </div>
  )
}
