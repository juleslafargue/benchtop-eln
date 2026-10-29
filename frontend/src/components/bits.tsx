import { lazy, Suspense, useEffect, useState, type ComponentProps } from 'react'
import { depictUrl } from '../api'

const StructureEditorLazy = lazy(() => import('./StructureEditor'))

export function StructureEditor(props: ComponentProps<typeof StructureEditorLazy>) {
  return (
    <Suspense fallback={<div className="editor-fallback" role="status"><div className="panel panel-pad">Loading the structure editor… <span className="faint">(the first time takes a few seconds)</span></div></div>}>
      <StructureEditorLazy {...props} />
    </Suspense>
  )
}

// Unit-aware entry: "1.2 g" in a mg field becomes 1200, "250 µL" in a mL field becomes 0.25.
const UNITS: Record<string, Record<string, number>> = {
  mg: { mg: 1, g: 1000, kg: 1e6, ug: 1e-3, 'µg': 1e-3, 'μg': 1e-3 },
  mL: { ml: 1, l: 1000, ul: 1e-3, 'µl': 1e-3, 'μl': 1e-3 },
  mmol: { mmol: 1, mol: 1000, umol: 1e-3, 'µmol': 1e-3, 'μmol': 1e-3 },
  h: { h: 1, hr: 1, hrs: 1, hour: 1, hours: 1, min: 1 / 60, mins: 1 / 60, m: 1 / 60, d: 24, day: 24, days: 24, s: 1 / 3600 },
  M: { m: 1, mm: 1e-3, 'µm': 1e-6, 'μm': 1e-6, um: 1e-6 },
}

/** Parse "1.2 g" → 1200 for unit "mg". Returns undefined if the text isn't a number (with an optional known unit). */
export function parseAmount(text: string, unit?: keyof typeof UNITS): number | null | undefined {
  const t = text.trim().replace(',', '.')
  if (t === '') return null
  const m = t.match(/^([-+]?\d*\.?\d+(?:e[-+]?\d+)?)\s*([a-zA-Zµμ]*)$/i)
  if (!m) return undefined
  const n = Number(m[1])
  if (!m[2]) return n
  if (!unit) return undefined
  const f = UNITS[unit][unit === 'M' ? m[2] === 'M' ? 'm' : m[2].toLowerCase() === 'mm' ? 'mm' : m[2].toLowerCase() : m[2].toLowerCase()]
  return f === undefined ? undefined : n * f
}

/** Numeric input. While you type, it keeps your text (so "1." isn't eaten); otherwise it shows the stored value.
 *  Leaving the box saves only what you typed in it since you entered it: a box you only passed through (Tab, Enter,
 *  a click elsewhere) shows and keeps the value recalculated meanwhile, instead of saving the old one it showed.
 *  With `unit`, a typed unit is converted (e.g. "1.2 g" into a mg field). */
export function NumInput({ value, onCommit, disabled, className, label, nd = 4, unit }: {
  value: number | null | undefined; onCommit: (v: number | null) => void
  disabled?: boolean; className?: string; label: string; nd?: number; unit?: keyof typeof UNITS
}) {
  // tiny amounts (after a big scale-down) keep 3 significant figures instead of rounding to 0.00
  const shown = value === null || value === undefined ? ''
    : value !== 0 && Math.abs(value) < 10 ** (1 - nd) ? String(Number(Number(value).toPrecision(3))) : String(Number(Number(value).toFixed(nd)))
  // null: not typed in since entering the box. Otherwise the typed text, and what the box showed when typing began.
  const [draft, setDraft] = useState<{ text: string; from: string } | null>(null)
  const commit = () => {
    if (!draft) return
    setDraft(null)
    const t = draft.text.trim()
    if (t === draft.from || t === shown) return  // typed, but back to what was there: nothing to save
    const n = parseAmount(t, unit)
    if (n === undefined || (n !== null && Number.isNaN(n))) return  // not a number: the stored value comes back
    onCommit(n)
  }
  return (
    <input
      aria-label={label} inputMode={unit ? 'text' : 'decimal'} className={className} disabled={disabled} value={draft ? draft.text : shown}
      title={unit ? `Number in ${unit}, or type a unit (e.g. ${({ mg: '1.2 g', mL: '250 µL', mmol: '0.5 mol', h: '30 min', M: '500 mM' })[unit]})` : undefined}
      onFocus={() => setDraft(null)}
      onBlur={commit}
      onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
      onChange={e => { const text = e.target.value; setDraft(d => ({ text, from: d ? d.from : shown })) }}
    />
  )
}

export function Mol({ smiles, w = 220, h = 130, className, rxn, highlight }: { smiles: string; w?: number; h?: number; className?: string; rxn?: boolean; highlight?: string }) {
  if (!smiles) return null
  return <img className={className} src={depictUrl(smiles, w, h, rxn, highlight)} alt={`Structure ${smiles}`} loading="lazy" />
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t) }, [value, ms])
  return v
}
