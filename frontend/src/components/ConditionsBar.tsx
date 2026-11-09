import { useState } from 'react'
import { NumInput } from './bits'

export interface Conditions {
  temperature_c?: number | null; temperature_label?: string | null; time_h?: number | null
  atmosphere?: string | null; target_conc_m?: number | null; notes?: string | null
}

const TEMP_LABELS = ['rt', 'reflux', 'ice bath', 'dry ice/acetone', 'microwave', 'sealed tube']
const ATMOSPHERES = ['', 'air', 'N2', 'Ar', 'H2', 'O2', 'CO']

export default function ConditionsBar({ value, onChange, readOnly }: {
  value: Conditions; onChange: (patch: Partial<Conditions>) => void; readOnly: boolean
}) {
  // what a box couldn't take ("warm" as a temperature), said under the bar so the boxes stay where they are
  const [problems, setProblems] = useState<Record<string, string>>({})
  const onProblem = (label: string, problem: string) => setProblems(p => {
    if ((p[label] || '') === problem) return p
    const next = { ...p }
    if (problem) next[label] = problem; else delete next[label]
    return next
  })
  return (<>
    <div className="conditions" role="group" aria-label="Reaction conditions">
      <label className="cond">
        <span>Temperature</span>
        <span className="cond-in">
          <NumInput label="Temperature in °C" className="input" disabled={readOnly} value={value.temperature_c ?? null} nd={1} unit="C" onProblem={onProblem}
            onCommit={v => onChange({ temperature_c: v })} /><i>°C</i>
          <input className="input" list="temp-labels" aria-label="Temperature description" placeholder={readOnly ? undefined : 'or rt, reflux…'} disabled={readOnly}
            value={value.temperature_label ?? ''} onChange={e => onChange({ temperature_label: e.target.value })} />
          <datalist id="temp-labels">{TEMP_LABELS.map(t => <option key={t} value={t} />)}</datalist>
        </span>
      </label>
      <label className="cond">
        <span>Time</span>
        <span className="cond-in"><NumInput label="Time in hours" className="input" disabled={readOnly} value={value.time_h ?? null} nd={2} unit="h" onProblem={onProblem}
          onCommit={v => onChange({ time_h: v })} /><i>h</i></span>
      </label>
      <label className="cond">
        <span>Atmosphere</span>
        <select className="select" disabled={readOnly} value={value.atmosphere ?? ''} onChange={e => onChange({ atmosphere: e.target.value })}>
          {ATMOSPHERES.map(a => <option key={a} value={a}>{a ? a.replace('2', '₂') : '—'}</option>)}
        </select>
      </label>
      <label className="cond">
        <span>Target conc.</span>
        <span className="cond-in"><NumInput label="Target concentration in mol/L" className="input" disabled={readOnly} value={value.target_conc_m ?? null} unit="M" onProblem={onProblem}
          onCommit={v => onChange({ target_conc_m: v })} /><i>M</i></span>
      </label>
      <label className="cond grow">
        <span>Notes</span>
        <input className="input" disabled={readOnly} placeholder={readOnly ? undefined : 'e.g. degassed, slow addition over 30 min'} value={value.notes ?? ''}
          onChange={e => onChange({ notes: e.target.value })} />
      </label>
    </div>
    {Object.entries(problems).map(([label, problem]) => <p className="error num-problem-line" role="alert" key={label}>{label}: {problem}</p>)}
  </>)
}
