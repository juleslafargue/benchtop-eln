import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type ExperimentSummary } from '../api'
import type { EntryEditorProps } from './EntryCard'

interface Cell { value: number | null; note?: string; experiment_id?: number }

// one-hue sequential ramp (light = low, dark = high); text flips to white on the darker half
const RAMP = ['#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef', '#6da7ec', '#5598e7', '#3987e5', '#2a78d6', '#256abf', '#1c5cab', '#184f95', '#104281']

function levelsText(v: string[]) { return v.join(', ') }
function parseLevels(t: string) { return t.split(',').map(s => s.trim()).filter(Boolean).slice(0, 12) }

export default function ScreenEntry({ content, onChange, readOnly, experimentId }: EntryEditorProps) {
  const xs: string[] = content.x_levels || [], ys: string[] = content.y_levels || []
  const cells: Record<string, Cell> = content.cells || {}
  const better: boolean = content.higher_is_better ?? true
  const [edit, setEdit] = useState<string | null>(null)
  const [xText, setXText] = useState(levelsText(xs))
  const [yText, setYText] = useState(levelsText(ys))
  const [exps, setExps] = useState<ExperimentSummary[]>([])
  useEffect(() => { setXText(levelsText(xs)); setYText(levelsText(ys)) }, [xs.join('|'), ys.join('|')]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (edit && !exps.length) api.get<ExperimentSummary[]>('/api/experiments').then(setExps).catch(() => {}) }, [edit, exps.length])

  const set = (patch: any) => onChange({ ...content, ...patch })
  const setCell = (k: string, c: Cell) => set({ cells: { ...cells, [k]: c } })
  const vals = Object.values(cells).map(c => c.value).filter((v): v is number => v !== null && v !== undefined)
  const lo = Math.min(...vals), hi = Math.max(...vals)
  const best = vals.length ? (better ? hi : lo) : null
  const color = (v: number | null) => {
    if (v === null || !vals.length) return null
    const t = hi === lo ? 1 : (v - lo) / (hi - lo)
    const idx = Math.round((better ? t : 1 - t) * (RAMP.length - 1))
    return { bg: RAMP[idx], fg: idx >= 6 ? '#ffffff' : '#0d1b2e' }
  }
  const filled = vals.length, total = xs.length * ys.length
  const summary = useMemo(() => {
    if (!filled || best === null) return null
    const k = Object.keys(cells).find(key => cells[key].value === best)!
    const [i, j] = k.split(',').map(Number)
    return `Best: ${best}${content.response?.includes('%') ? '%' : ''} at ${content.y_name || 'row'} ${ys[i]}, ${content.x_name || 'column'} ${xs[j]}`
  }, [cells, best, filled, xs, ys, content.response, content.x_name, content.y_name])

  return (
    <div className="stack" style={{ gap: 10 }}>
      {!readOnly && <div className="form-grid">
        <label className="field">Columns: factor<input className="input" value={content.x_name || ''} onChange={e => set({ x_name: e.target.value })} placeholder="e.g. Base" /></label>
        <label className="field">Column levels (comma-separated)<input className="input" value={xText} onChange={e => setXText(e.target.value)}
          onBlur={() => set({ x_levels: parseLevels(xText) })} placeholder="K2CO3, Cs2CO3, K3PO4" /></label>
        <label className="field">Rows: factor<input className="input" value={content.y_name || ''} onChange={e => set({ y_name: e.target.value })} placeholder="e.g. Temperature (°C)" /></label>
        <label className="field">Row levels (comma-separated)<input className="input" value={yText} onChange={e => setYText(e.target.value)}
          onBlur={() => set({ y_levels: parseLevels(yText) })} placeholder="60, 80, 100" /></label>
        <label className="field">Response<input className="input" value={content.response || ''} onChange={e => set({ response: e.target.value })} placeholder="Yield (%)" /></label>
        <label className="field">Best is<select className="select" value={better ? 'high' : 'low'} onChange={e => set({ higher_is_better: e.target.value === 'high' })}>
          <option value="high">the highest value</option><option value="low">the lowest value (e.g. impurity %)</option></select></label>
      </div>}

      <div className="grid-wrap">
        <table className="heat" aria-label={`${content.response} by ${content.y_name} and ${content.x_name}`}>
          <thead>
            <tr><td aria-hidden="true" />{xs.length > 0 && <th colSpan={xs.length} style={{ textAlign: 'center' }}>{content.x_name}</th>}</tr>
            <tr><th style={{ textAlign: 'right' }}>{content.y_name}</th>{xs.map((x, j) => <th key={j}>{x}</th>)}</tr>
          </thead>
          <tbody>
            {ys.map((y, i) => <tr key={i}><th style={{ textAlign: 'right' }}>{y}</th>
              {xs.map((_, j) => {
                const k = `${i},${j}`, c = cells[k], col = color(c?.value ?? null)
                return <td key={j}>
                  <button className={`cell${c?.value === best && best !== null ? ' best' : ''}`} disabled={readOnly && !c?.experiment_id}
                    style={col ? { background: col.bg, color: col.fg } : { background: 'var(--paper-2)', color: 'var(--ink-3)' }}
                    title={[c?.note, c?.experiment_id ? 'linked experiment' : ''].filter(Boolean).join(' · ') || undefined}
                    onClick={() => (readOnly ? null : setEdit(k))} aria-label={`${content.y_name} ${y}, ${content.x_name} ${xs[j]}: ${c?.value ?? 'no value'}`}>
                    {c?.value ?? (readOnly ? '' : '+')}
                    {(c?.note || c?.experiment_id) && <small>{c?.experiment_id ? (exps.find(e => e.id === c.experiment_id)?.eid || '↗') : ''}{c?.note ? ' ✎' : ''}</small>}
                  </button>
                </td>
              })}
            </tr>)}
          </tbody>
        </table>
      </div>

      <div className="spread" style={{ gap: 8 }}>
        <span className="faint" style={{ fontSize: 13 }}>{summary || `${content.response || 'Values'}: click a cell to enter a result.`} · {filled}/{total} filled</span>
        {vals.length > 1 && <span className="heat-scale">{better ? lo : hi}<span className="ramp" style={{ background: `linear-gradient(90deg, ${RAMP[0]}, ${RAMP[RAMP.length - 1]})` }} />{better ? hi : lo}
          <span>(darker = better)</span></span>}
      </div>
      {Object.values(cells).some(c => c.experiment_id) && <div className="chips">{Object.entries(cells).filter(([, c]) => c.experiment_id).map(([k, c]) => {
        const [i, j] = k.split(',').map(Number)
        return <Link key={k} className="chip" to={`/experiments/${c.experiment_id}`}>{ys[i]} / {xs[j]}: {exps.find(e => e.id === c.experiment_id)?.eid || `experiment ${c.experiment_id}`}</Link>
      })}</div>}

      {edit && (() => {
        const [i, j] = edit.split(',').map(Number)
        const c = cells[edit] || { value: null }
        return (
          <div className="lookup-card" role="dialog" aria-label="Edit cell">
            <div className="stack" style={{ gap: 8, flex: 1 }}>
              <b>{content.y_name} {ys[i]} · {content.x_name} {xs[j]}</b>
              <div className="form-grid">
                <label className="field">{content.response || 'Value'}<input className="input" autoFocus inputMode="decimal" defaultValue={c.value ?? ''}
                  onBlur={e => setCell(edit, { ...c, value: e.target.value.trim() === '' ? null : Number(e.target.value) })} /></label>
                <label className="field">Experiment<select className="select" value={c.experiment_id ?? ''} onChange={e => setCell(edit, { ...c, experiment_id: e.target.value ? Number(e.target.value) : undefined })}>
                  <option value="">None</option>{exps.filter(e => e.id !== experimentId).map(e => <option key={e.id} value={e.id}>{e.eid} · {e.title}</option>)}</select></label>
              </div>
              <label className="field">Note<input className="input" defaultValue={c.note || ''} onBlur={e => setCell(edit, { ...c, note: e.target.value })} placeholder="e.g. 85:15 conversion by LCMS" /></label>
              <div className="row"><button className="btn sm primary" onClick={() => setEdit(null)}>Done</button>
                <button className="btn ghost sm" onClick={() => { const n = { ...cells }; delete n[edit]; set({ cells: n }); setEdit(null) }}>Clear cell</button></div>
            </div>
          </div>
        )
      })()}
    </div>
  )
}
