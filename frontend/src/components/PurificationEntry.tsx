import { PURIFICATION_LABELS } from '../api'
import { NumInput } from './bits'
import TextEntry from './TextEntry'
import type { EntryEditorProps } from './EntryCard'

export default function PurificationEntry(props: EntryEditorProps) {
  const { content, onChange, readOnly } = props
  const set = (patch: Record<string, unknown>) => onChange({ ...content, ...patch })
  const chrom = ['column', 'prep_tlc', 'prep_hplc'].includes(content.method)
  const text = (k: string, label: string, placeholder = '', w?: number) => (
    <label className="cond" style={w ? { width: w } : { flex: '1 1 160px' }}>
      <span>{label}</span>
      <input className="input" disabled={readOnly} placeholder={placeholder} value={content[k] ?? ''} onChange={e => set({ [k]: e.target.value })} />
    </label>
  )
  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="conditions">
        <label className="cond">
          <span>Method</span>
          <select className="select" disabled={readOnly} value={content.method || 'column'} onChange={e => set({ method: e.target.value })}>
            {Object.entries(PURIFICATION_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        {chrom && text('stationary_phase', 'Stationary phase', 'e.g. silica, 40–63 µm')}
        {chrom && text('eluent', 'Eluent', 'e.g. 0→20% EtOAc/hexanes')}
        {content.method === 'recrystallization' && text('eluent', 'Solvent', 'e.g. hot EtOH')}
        {content.method === 'distillation' && text('eluent', 'Conditions', 'e.g. 85 °C at 12 mbar')}
        {text('rf', 'Rf', 'e.g. 0.45', 88)}
        {text('rf_eluent', 'TLC eluent', 'e.g. 10% EtOAc/hexanes')}
        <label className="cond">
          <span>Isolated</span>
          <span className="cond-in"><NumInput label="Isolated mass in mg" className="input" disabled={readOnly} value={content.isolated_mg ?? null} nd={2} unit="mg"
            onCommit={v => set({ isolated_mg: v })} /><i>mg</i></span>
        </label>
        {text('appearance', 'Appearance', 'e.g. white solid')}
      </div>
      <TextEntry {...props} label={`${props.label || 'Work-up'}: notes`} content={{ html: content.notes_html || '' }} onChange={c => set({ notes_html: c.html })} />
    </div>
  )
}
