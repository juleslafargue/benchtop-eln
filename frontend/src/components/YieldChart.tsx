import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { median } from '../stats'
import { day } from '../api'

export interface YieldPoint { experiment_id: number; eid: string; title: string; date: string; yield_pct: number; product: string | null }

/** Yields over time: one series, so one hue and no legend (the widget title names it). */
export default function YieldChart({ points }: { points: YieldPoint[] }) {
  const nav = useNavigate()
  const [hover, setHover] = useState<number | null>(null)
  const W = 600, H = 240, m = { l: 40, r: 12, t: 12, b: 26 }
  const pts = useMemo(() => [...points].sort((a, b) => a.date.localeCompare(b.date)), [points])
  if (!pts.length) return <p className="empty" style={{ padding: 16 }}>Yields appear here once reactions have an isolated product mass.</p>

  const t = pts.map(p => new Date(p.date).getTime())
  let t0 = Math.min(...t), t1 = Math.max(...t)
  if (t1 - t0 < 864e5 * 7) { const mid = (t0 + t1) / 2; t0 = mid - 864e5 * 3.5; t1 = mid + 864e5 * 3.5 } // at least a week wide
  const x = (ms: number) => m.l + (ms - t0) / (t1 - t0) * (W - m.l - m.r)
  const y = (v: number) => m.t + (1 - Math.min(100, Math.max(0, v)) / 100) * (H - m.t - m.b)
  const mid = median(pts.map(p => p.yield_pct)) ?? 0
  const months: number[] = []
  for (let d = new Date(new Date(t0).getFullYear(), new Date(t0).getMonth() + 1, 1); d.getTime() <= t1; d.setMonth(d.getMonth() + 1)) months.push(d.getTime())
  const fmtDay = (ms: number) => new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  const ticks = months.length >= 2 && months.length <= 8 ? months.map(ms => ({ ms, label: new Date(ms).toLocaleDateString(undefined, { month: 'short' }) }))
    : [t0, (t0 + t1) / 2, t1].map(ms => ({ ms, label: fmtDay(ms) }))
  const h = hover !== null ? pts[hover] : null

  // nearest point to the pointer, so small dots are easy to hit
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const px = (e.clientX - r.left) / r.width * W, py = (e.clientY - r.top) / r.height * H
    let best = -1, bd = 24 ** 2
    pts.forEach((p, i) => { const d = (x(t[i]) - px) ** 2 + (y(p.yield_pct) - py) ** 2; if (d < bd) { bd = d; best = i } })
    setHover(best >= 0 ? best : null)
  }

  return (
    <div className="ychart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Yields of ${pts.length} reactions over time; median ${mid.toFixed(0)}%`}
        onPointerMove={onMove} onPointerLeave={() => setHover(null)} onClick={() => h && nav(`/experiments/${h.experiment_id}`)}
        style={{ cursor: h ? 'pointer' : 'default' }}>
        {[0, 25, 50, 75, 100].map(v => <g key={v}>
          <line x1={m.l} x2={W - m.r} y1={y(v)} y2={y(v)} className="grid" />
          <text x={m.l - 6} y={y(v) + 4} textAnchor="end" className="tick">{v}%</text>
        </g>)}
        {ticks.map(tk => { const px = x(tk.ms); return <text key={tk.ms} x={px} y={H - 8} className="tick"
          textAnchor={px > W - m.r - 30 ? 'end' : px < m.l + 30 ? 'start' : 'middle'}>{tk.label}</text> })}
        <line x1={m.l} x2={W - m.r} y1={y(mid)} y2={y(mid)} className="median" />
        <text x={W - m.r} y={y(mid) - 5} textAnchor="end" className="tick">median {mid.toFixed(0)}%</text>
        {pts.map((p, i) => <circle key={`${p.experiment_id}-${i}`} cx={x(t[i])} cy={y(p.yield_pct)} r={hover === i ? 6 : 4.5} className="dot" />)}
      </svg>
      {h && <div className={`ytip${y(h.yield_pct) < H * 0.35 ? ' below' : ''}${x(t[hover!]) < W * 0.2 ? ' left' : x(t[hover!]) > W * 0.8 ? ' right' : ''}`}
        style={{ left: `${x(t[hover!]) / W * 100}%`, top: `${y(h.yield_pct) / H * 100}%` }}>
        <b>{h.yield_pct.toFixed(1)}%</b> {h.product || ''}<br /><span className="faint">{h.eid} · {day(h.date)}</span>
      </div>}
    </div>
  )
}
