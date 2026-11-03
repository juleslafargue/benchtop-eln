import { lazy, Suspense, useEffect, useId, useState, type ComponentProps } from 'react'
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
  C: { '°c': 1, '°': 1, c: 1 },  // temperatures: "25 °C" is 25 (nothing is converted)
}
// what to call each kind of box, and the units it takes, when something typed can't be read
const UNIT_NAME: Record<string, string> = { mg: 'mg', mL: 'mL', mmol: 'mmol', h: 'hours', M: 'mol/L', C: '°C' }
const UNIT_EXAMPLE: Record<string, string> = { mg: '1.2 g', mL: '250 µL', mmol: '0.5 mol', h: '30 min', M: '500 mM', C: '25' }

/** "1,000" is one thousand in English and one in French or German, so Benchtop doesn't guess (0.30.1: it was taken as
 *  1 mg). A comma followed by exactly three digits, and no other separator, is ambiguous; "0,5" and "1,25" are not.
 *  Returns what to tell the person, or null. */
export function amountProblem(text: string): string | null {
  const m = text.trim().match(/^([-+]?)([1-9]\d{0,2}),(\d{3})\s*[a-zA-Zµμ]*$/)
  if (!m) return null
  const thousands = `${m[1]}${m[2]}${m[3]}`, decimal = String(Number(`${m[1]}${m[2]}.${m[3]}`))
  return `Did you mean ${thousands} or ${decimal}? Type it without the comma.`
}

/** Parse "1.2 g" → 1200 for unit "mg". Returns undefined if the text isn't a number (with an optional known unit),
 *  or could be read two ways (see amountProblem). */
export function parseAmount(text: string, unit?: keyof typeof UNITS): number | null | undefined {
  if (amountProblem(text)) return undefined
  let t = text.trim()
  // spaces between groups of three digits are thousands ("1 000", "12 500,5"): nobody writes a decimal point as a space
  const spaced = t.match(/^([-+]?[1-9]\d{0,2}(?:[ \u00a0\u202f]\d{3})+)(?!\d|[ \u00a0\u202f]\d)/)
  if (spaced) t = spaced[1].replace(/[ \u00a0\u202f]/g, '') + t.slice(spaced[1].length)
  t = t.replace(',', '.').replace(/°\s+c$/i, '°c')
  if (t === '') return null
  const m = t.match(/^([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*([a-zA-Zµμ°]*)$/i)  // "5." is 5
  if (!m) return undefined
  const n = Number(m[1])
  if (!m[2]) return n
  if (!unit) return undefined
  const f = UNITS[unit][unit === 'M' ? m[2] === 'M' ? 'm' : m[2].toLowerCase() === 'mm' ? 'mm' : m[2].toLowerCase() : m[2].toLowerCase()]
  return f === undefined ? undefined : n * f
}

/** Why the text in a number box can't be taken, or null if it can (an empty box can: it means no value). */
export function numberProblem(text: string, unit?: keyof typeof UNITS): string | null {
  const comma = amountProblem(text)
  if (comma) return comma
  const n = parseAmount(text, unit)
  if (n !== undefined && !(n !== null && !Number.isFinite(n))) return null
  if (!unit) return 'Not a number.'
  const typed = text.trim().match(/^[-+]?[\d.,\s]*\d[\d.,]*\s*([^\d\s].*)$/)
  if (unit === 'C') return 'Not a number. Type it in °C, e.g. -78.'
  if (typed) return `${typed[1].trim()} can't be turned into ${UNIT_NAME[unit]}. Type e.g. ${UNIT_EXAMPLE[unit]}.`
  return `Not a number. Type e.g. ${UNIT_EXAMPLE[unit]}.`
}

/** Numeric input. While you type, it keeps your text (so "1." isn't eaten); otherwise it shows the stored value.
 *  Leaving the box saves only what you typed in it since you entered it: a box you only passed through (Tab, Enter,
 *  a click elsewhere) shows and keeps the value recalculated meanwhile, instead of saving the old one it showed.
 *  Text that can't be read ("25 °F", "12 mL" in mg) stays in the box with why, and nothing is saved, until it's changed
 *  (0.30.1: it was thrown away without a word). Escape puts back what was there.
 *  With `unit`, a typed unit is converted (e.g. "1.2 g" into a mg field). With `live`, what's typed is used after that
 *  many milliseconds without typing, without leaving the box (filters, and dialogs whose button needs the amount). */
export function NumInput({ value, onCommit, disabled, className, label, nd = 4, unit, sig, onProblem, live, fit }: {
  value: number | null | undefined; onCommit: (v: number | null) => void
  disabled?: boolean; className?: string; label: string; nd?: number; unit?: keyof typeof UNITS
  /** show at most this many significant figures (whole numbers stay whole): the value kept is unchanged */
  sig?: number
  /** where a table shows why a box's text wasn't taken ('' once it's fixed); without it, it's said just below the box */
  onProblem?: (label: string, problem: string) => void
  live?: number
  /** grow to show the whole number (a table's narrow boxes: "123457" mg, "0.000123" equiv were cut) */
  fit?: boolean
}) {
  // tiny amounts (after a big scale-down) keep 3 significant figures instead of rounding to 0.00
  const shown = value === null || value === undefined ? ''
    : value !== 0 && Math.abs(value) < 10 ** (1 - nd) ? String(Number(Number(value).toPrecision(3)))
      : sig && Math.abs(value) < 10 ** sig ? String(Number(Number(Number(value).toPrecision(sig)).toFixed(nd)))
        : String(Number(Number(value).toFixed(sig ? 0 : nd)))
  // null: not typed in since entering the box. Otherwise the typed text, and what the box showed when typing began.
  const [draft, setDraft] = useState<{ text: string; from: string; was: number | null } | null>(null)
  // text that can't be taken as it is ("1,000", "abc"): it stays in the box, with why, until it's changed
  const [problem, setProblem] = useState('')
  const problemId = useId()
  useEffect(() => {  // and cleared when the box goes (its row removed)
    onProblem?.(label, problem)
    return () => { if (problem) onProblem?.(label, '') }
  }, [problem]) // eslint-disable-line react-hooks/exhaustive-deps
  const same = (n: number | null) => n === (value ?? null)
  useEffect(() => {  // live: use what's typed after a pause (what can't be read is said when the box is left)
    if (live === undefined || !draft) return
    const t = setTimeout(() => {
      const n = numberProblem(draft.text, unit) ? undefined : parseAmount(draft.text, unit)
      if (n !== undefined && !same(n)) onCommit(n)
    }, live)
    return () => clearTimeout(t)
  }, [draft?.text]) // eslint-disable-line react-hooks/exhaustive-deps
  const commit = () => {
    if (!draft) return
    const bad = numberProblem(draft.text, unit)
    if (bad) { setProblem(bad); return }
    setDraft(null)
    const t = draft.text.trim()
    const n = parseAmount(t, unit) as number | null
    if (live !== undefined) { if (!same(n)) onCommit(n); return }
    if (t === draft.from || t === shown) return  // typed, but back to what was there: nothing to save
    onCommit(n)
  }
  const text = draft ? draft.text : shown
  // digits are 1ch wide (tabular figures); a point or minus about half that; the padding and border are 8px
  const chars = fit ? text.replace(/\d/g, '').length * 0.6 + (text.match(/\d/g) || []).length : 0
  return (<>
    <input
      style={chars > 5 ? { minWidth: `calc(${chars.toFixed(1)}ch + 10px)` } : undefined}
      aria-label={label} inputMode={unit ? 'text' : 'decimal'} className={[className, problem && 'num-bad'].filter(Boolean).join(' ') || undefined}
      disabled={disabled} value={text} aria-invalid={problem ? true : undefined} aria-describedby={problem && !onProblem ? problemId : undefined}
      title={unit ? unit === 'C' ? 'Temperature in °C' : `Number in ${unit}, or type a unit (e.g. ${UNIT_EXAMPLE[unit]})` : undefined}
      onFocus={() => { if (!problem) setDraft(null) }}
      onBlur={commit}
      onKeyDown={e => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        else if (e.key === 'Escape' && (draft || problem)) {  // back to what was there (and a dialog stays open)
          e.preventDefault(); e.stopPropagation()
          if (live !== undefined && draft && !same(draft.was)) onCommit(draft.was)
          setDraft(null); setProblem('')
        }
      }}
      onChange={e => { const text = e.target.value; setProblem(''); setDraft(d => ({ text, from: d ? d.from : shown, was: d ? d.was : value ?? null })) }}
    />
    {problem && !onProblem && <span className="num-problem" id={problemId} role="alert">{problem}</span>}
  </>)
}

export function Mol({ smiles, w = 220, h = 130, className, rxn, highlight, alt }: { smiles: string; w?: number; h?: number; className?: string; rxn?: boolean; highlight?: string; alt?: string }) {
  if (!smiles) return null
  return <img className={className} src={depictUrl(smiles, w, h, rxn, highlight)} alt={alt || `Structure ${smiles}`} loading="lazy" />
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t) }, [value, ms])
  return v
}
