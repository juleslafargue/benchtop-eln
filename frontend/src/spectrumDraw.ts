/** Drawing a spectrum with its analysis: the spectrum viewer draws with this, and so does the Supporting Information
 *  builder (0.33), which draws each spectrum exactly as the viewer's Export PNG does, without opening the viewer. */
import { api } from './api'
import type { ConfirmedAssignments, Prediction } from './components/Prediction'
import {
  analyzeMultiplet, autoAnalyse1H, autoPeaks13C, findResidual, integrate, lowerBound, parseSpectrumBuffer, pickPeaks,
  shifted, solventMarkers, type ImpurityResult, type Multiplet, type Spectrum,
} from './spectra'

export interface SavedAnalysis {
  integrals: [number, number][]; ref_idx: number; ref_value: number; threshold: number
  labels?: { mult: string; j: string }[]; offset?: number
  impurities?: Pick<ImpurityResult, 'short' | 'name' | 'molPct' | 'wtPct' | 'signals'>[]
  overlays?: number[]; overlay_mode?: 'stack' | 'overlay'
  fid?: { ph0: number; ph1: number; lb: number | null }
  assignments?: ConfirmedAssignments
}
export type Impurities = NonNullable<SavedAnalysis['impurities']>
export interface Region { a: number; b: number }
export interface Label { mult: string; j: string }
export interface SignalTag { a: number; b: number; text: string }
export interface Overlay { id: number; label: string; spec: Spectrum }

export const PRED_COLOR = '#8c62d9'
export const OVERLAY_COLORS = ['#3a7ae6', '#c27a00', '#8c62d9', '#2a9d4b'] // validated for colour-blind separation and contrast on both themes
export const M = { l: 14, r: 14, t: 28, b: 62 }
export const is13C = (s: Spectrum) => /13C/i.test(s.nucleus || '')
export const isProton = (s: Spectrum) => s.kind === 'nmr' && !is13C(s)
export const LIGHT = { ink: '#17212c', ink3: '#7a8896', accent: '#b8336a', rule: '#cbd5de', green: '#1d7466', paper: '#ffffff' }
export type Palette = typeof LIGHT

export function niceTicks(lo: number, hi: number, n = 8): number[] {
  const span = hi - lo
  if (span <= 0) return []
  const raw = span / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map(k => k * mag).find(s => s >= raw) || raw
  const out: number[] = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10))
  return out
}
export const fmtX = (s: Spectrum, x: number) => s.kind === 'nmr' ? x.toFixed(is13C(s) ? 1 : 2) : s.kind === 'ir' ? Math.round(x).toString() : x.toFixed(1)

export function applyLabel(m: Multiplet, lab?: Label): Multiplet {
  if (!lab) return m
  const j = lab.j.trim() ? lab.j.split(/[,;/\s]+/).map(Number).filter(v => Number.isFinite(v) && v > 0) : m.j
  const mult = lab.mult || m.mult
  return { ...m, mult, j: mult === 's' || mult === 'br s' || mult === 'm' ? [] : j }
}

export function header(s: Spectrum) {
  if (s.kind === 'nmr') {
    const nuc = (s.nucleus || '1H').replace(/^1H$/, '¹H').replace(/^13C$/, '¹³C').replace(/^19F$/, '¹⁹F')
    return `${nuc} NMR (${s.frequency ? `${Math.round(s.frequency)} MHz` : '? MHz'}${s.solvent ? `, ${s.solvent}` : ''})`
  }
  return s.kind === 'ir' ? 'IR (cm⁻¹)' : s.kind === 'ms' ? 'MS (m/z)' : s.title
}

/** The peaks the viewer labels in the window shown. */
export function peaksIn(spec: Spectrum, view: [number, number], threshold: number) {
  if (spec.kind === 'nmr' && is13C(spec)) { // carbon: signal-to-noise picking, solvent excluded
    const lo = Math.min(view[0], view[1]), hi = Math.max(view[0], view[1])
    return autoPeaks13C(spec, threshold >= 1 ? threshold : 6).peaks.filter(p => p.x >= lo && p.x <= hi)
  }
  return pickPeaks(spec, view[0], view[1], threshold, 50)
}

/** One label per integrated 1H signal: "7.42 t, 2H", drawn above the multiplet. */
export function signalTagsFor(spec: Spectrum, withArea: (Region & { area: number })[], labels: Label[], refIdx: number, refH: number): SignalTag[] {
  const ref = withArea[refIdx]?.area
  return withArea.map((r, i) => {
    const m = applyLabel(analyzeMultiplet(spec, r.a, r.b), labels[i])
    const h = ref ? r.area / ref * refH : 0
    const shift = m.mult === 'm' ? `${m.hi.toFixed(2)}–${m.lo.toFixed(2)}` : m.shift.toFixed(2)
    return { a: r.a, b: r.b, text: `${shift} ${m.mult}${h ? `, ${h < 0.95 ? h.toFixed(1) : Math.round(h)}H` : ''}` }
  })
}

export function geometry(spec: Spectrum, view: [number, number], w: number) {
  const [x0, x1] = view
  const span = x1 - x0 || 1
  const toPx = (x: number) => spec.reversed ? M.l + (x1 - x) / span * (w - M.l - M.r) : M.l + (x - x0) / span * (w - M.l - M.r)
  const toX = (px: number) => spec.reversed ? x1 - (px - M.l) / (w - M.l - M.r) * span : x0 + (px - M.l) / (w - M.l - M.r) * span
  return { toPx, toX }
}

/** Everything the drawing shows. */
export interface DrawState {
  spec: Spectrum; view: [number, number]; yScale: number
  withArea: (Region & { area: number })[]; refIdx: number; refH: number
  peaks: { x: number; y: number }[]; showPeaks: boolean
  markers: { ppm: number; label: string }[]; impurities: Impurities
  overlays: Overlay[]; overlayMode: 'stack' | 'overlay'
  signalTags: SignalTag[]
  pred: Prediction | null; showPred: boolean
  /** a drag in progress (the viewer only), coloured by the tool */
  drag: { x0: number; x1: number; integrate: boolean } | null
  hover: number | null
}

export function drawSpectrum(g: CanvasRenderingContext2D, w: number, h: number, pal: Palette, st: DrawState, forExport = false) {
  const { spec, view, yScale, withArea, refIdx, refH, peaks, showPeaks, markers, impurities, overlays, overlayMode, signalTags, pred, showPred, drag, hover } = st
  const { toPx } = geometry(spec, view, w)
  g.clearRect(0, 0, w, h)
  if (forExport) { g.fillStyle = pal.paper; g.fillRect(0, 0, w, h) }
  const [x0, x1] = view
  const i0 = Math.max(0, lowerBound(spec.x, x0) - 1), i1 = Math.min(spec.x.length - 1, lowerBound(spec.x, x1) + 1)
  let lo = Infinity, hi = -Infinity
  for (let i = i0; i <= i1; i++) { if (spec.y[i] < lo) lo = spec.y[i]; if (spec.y[i] > hi) hi = spec.y[i] }
  if (spec.isStick) lo = Math.min(0, lo)
  const top = spec.dips ? hi : lo + (hi - lo) / yScale
  const bottom = spec.dips ? hi - (hi - lo) / yScale : lo
  const fullTop = M.t + (forExport ? 18 : 0), plotBottom = h - M.b
  const stacked = overlays.length > 0 && overlayMode === 'stack'
  const bands = overlays.length + 1
  // stacked: this spectrum takes the bottom band and each comparison a band above it
  // a strip along the top holds the signal labels, so they never sit on the trace
  const tagStrip = signalTags.length ? 30 : 0
  const predGroups = showPred && pred ? pred.groups.filter(g => g.shift !== null) : []
  const predStrip = predGroups.length && !forExport ? 30 : 0
  const plotTop = (stacked ? plotBottom - (plotBottom - fullTop) / bands : fullTop) + tagStrip + predStrip
  const plotH = plotBottom - plotTop
  const toPy = (y: number) => plotTop + (1 - (y - bottom) / (top - bottom || 1)) * plotH
  const font = (px: number, weight = 400, italic = false) => `${italic ? 'italic ' : ''}${weight} ${px}px "Instrument Sans", system-ui, sans-serif`

  if (forExport) { g.fillStyle = pal.ink; g.font = font(13, 600); g.textAlign = 'left'; g.fillText(header(spec), M.l, 18) }

  // integral bands
  withArea.forEach((r, k) => {
    const a = toPx(r.a), b = toPx(r.b)
    g.fillStyle = pal.green + (k === refIdx ? '26' : '14')
    g.fillRect(Math.min(a, b), plotTop, Math.abs(b - a), plotH)
  })
  if (drag) {
    const a = toPx(drag.x0), b = toPx(drag.x1)
    g.fillStyle = (drag.integrate ? pal.green : pal.accent) + '2a'
    g.fillRect(Math.min(a, b), plotTop, Math.abs(b - a), plotH)
  }

  // trace (per-pixel min/max decimation)
  g.save(); g.beginPath(); g.rect(M.l, plotTop - 2, w - M.l - M.r, plotH + 4); g.clip()
  g.strokeStyle = pal.ink; g.lineWidth = forExport ? 1.2 : 1; g.beginPath()
  if (spec.isStick) {
    for (let i = i0; i <= i1; i++) { const px = toPx(spec.x[i]); g.moveTo(px, toPy(Math.max(0, bottom))); g.lineTo(px, toPy(spec.y[i])) }
  } else {
    let last = -1, mn = 0, mx = 0, first = true
    for (let i = i0; i <= i1; i++) {
      const px = Math.round(toPx(spec.x[i]))
      if (px !== last) {
        if (last >= 0) { g.lineTo(last, toPy(mn)); g.lineTo(last, toPy(mx)) }
        last = px; mn = mx = spec.y[i]
        if (first) { g.moveTo(px, toPy(spec.y[i])); first = false }
      } else { if (spec.y[i] < mn) mn = spec.y[i]; if (spec.y[i] > mx) mx = spec.y[i] }
    }
    if (last >= 0) { g.lineTo(last, toPy(mn)); g.lineTo(last, toPy(mx)) }
  }
  g.stroke()
  g.restore()

  // comparison spectra
  overlays.forEach((o, k) => {
    const os = o.spec
    const j0 = Math.max(0, lowerBound(os.x, Math.min(x0, x1)) - 1), j1 = Math.min(os.x.length - 1, lowerBound(os.x, Math.max(x0, x1)) + 1)
    if (j1 <= j0) return
    let olo = Infinity, ohi = -Infinity
    for (let i = j0; i <= j1; i++) { if (os.y[i] < olo) olo = os.y[i]; if (os.y[i] > ohi) ohi = os.y[i] }
    const bandH = (plotBottom - fullTop) / bands
    const b0 = stacked ? plotBottom - (k + 1) * bandH : plotBottom, hgt = stacked ? bandH * 0.92 : plotH
    const oy = (y: number) => b0 - ((y - olo) / ((ohi - olo) || 1)) * hgt * yScale
    g.save(); g.beginPath(); g.rect(M.l, (stacked ? b0 - bandH : fullTop) - 2, w - M.l - M.r, (stacked ? bandH : plotBottom - fullTop) + 4); g.clip()
    g.strokeStyle = OVERLAY_COLORS[k % OVERLAY_COLORS.length]; g.lineWidth = 1; g.globalAlpha = stacked ? 1 : 0.85; g.beginPath()
    let last = -1, mn = 0, mx = 0, first = true
    for (let i = j0; i <= j1; i++) {
      const px = Math.round(toPx(os.x[i]))
      if (px !== last) {
        if (last >= 0) { g.lineTo(last, oy(mn)); g.lineTo(last, oy(mx)) }
        last = px; mn = mx = os.y[i]
        if (first) { g.moveTo(px, oy(os.y[i])); first = false }
      } else { if (os.y[i] < mn) mn = os.y[i]; if (os.y[i] > mx) mx = os.y[i] }
    }
    if (last >= 0) { g.lineTo(last, oy(mn)); g.lineTo(last, oy(mx)) }
    g.stroke(); g.restore()
    g.font = font(11, 600); g.fillStyle = OVERLAY_COLORS[k % OVERLAY_COLORS.length]; g.textAlign = 'left'
    g.fillText(o.label, M.l + 4, stacked ? b0 - bandH + 14 : fullTop + 14 + k * 14)
  })
  if (overlays.length) {
    g.font = font(11, 600); g.fillStyle = pal.ink; g.textAlign = 'left'
    g.fillText(forExport ? spec.title : 'This spectrum', M.l + 4, stacked ? plotTop + 14 : fullTop + 14 + overlays.length * 14)
  }
  g.save(); g.beginPath(); g.rect(M.l, plotTop - 2, w - M.l - M.r, plotH + 4); g.clip()

  // integral curves: running integral over each region, scaled together
  const maxArea = Math.max(...withArea.map(r => Math.abs(r.area)), 1e-12)
  g.strokeStyle = pal.green; g.lineWidth = 1.4
  for (const r of withArea) {
    const lo2 = Math.min(r.a, r.b), hi2 = Math.max(r.a, r.b)
    const j0 = lowerBound(spec.x, lo2), j1 = Math.min(lowerBound(spec.x, hi2), spec.x.length - 1)
    const base = plotBottom - plotH * 0.08, height = plotH * 0.42 * (r.area / maxArea)
    // accumulate in drawing direction (left to right on screen)
    const idx: number[] = []
    for (let j = j0; j <= j1; j++) idx.push(j)
    if (spec.reversed) idx.reverse()
    let acc = 0
    g.beginPath()
    idx.forEach((j, n) => {
      if (n > 0) { const p = idx[n - 1]; acc += Math.abs(spec.x[j] - spec.x[p]) * (spec.y[j] + spec.y[p]) / 2 }
      const px = toPx(spec.x[j]), py = base - (r.area ? acc / r.area : 0) * height
      if (n === 0) g.moveTo(px, py); else g.lineTo(px, py)
    })
    g.stroke()
  }
  g.restore()

  // solvent markers just above the axis
  g.font = font(10, 400, true); g.fillStyle = pal.ink3; g.textAlign = 'center'
  for (const m of markers) {
    if (m.ppm < Math.min(x0, x1) || m.ppm > Math.max(x0, x1)) continue
    const px = toPx(m.ppm)
    g.beginPath(); g.moveTo(px - 4, plotBottom); g.lineTo(px + 4, plotBottom); g.lineTo(px, plotBottom - 6); g.closePath(); g.fill()
    g.fillText(m.label, px, plotBottom - 9)
  }

  // residual lab solvents found by auto-analysis
  g.font = font(10, 600); g.fillStyle = pal.accent; g.textAlign = 'center'
  {
    // stack labels that would overlap each other or a solvent-residual marker
    const placed: { px: number; w: number; row: number }[] = markers.map(m => ({ px: toPx(m.ppm), w: g.measureText(m.label).width + 6, row: 0 }))
    const labels = impurities.flatMap(imp => imp.signals.filter(p => p >= Math.min(x0, x1) && p <= Math.max(x0, x1)).map(p => ({ px: toPx(p), text: imp.short })))
      .sort((a, b) => a.px - b.px)
    for (const l of labels) {
      const w = g.measureText(l.text).width + 6
      let row = 0
      while (placed.some(q => q.row === row && Math.abs(q.px - l.px) < (q.w + w) / 2)) row++
      placed.push({ px: l.px, w, row })
      g.fillRect(l.px - 0.5, plotBottom - 5, 1, 5)
      g.fillText(l.text, l.px, plotBottom - 9 - row * 11)
    }
  }

  // axis + ticks
  g.strokeStyle = pal.rule; g.lineWidth = 1
  g.beginPath(); g.moveTo(M.l, plotBottom + 0.5); g.lineTo(w - M.r, plotBottom + 0.5); g.stroke()
  g.fillStyle = pal.ink3; g.font = font(11); g.textAlign = 'center'
  for (const v of niceTicks(Math.min(x0, x1), Math.max(x0, x1), Math.max(3, Math.floor(w / 85)))) {
    const px = toPx(v)
    g.beginPath(); g.moveTo(px, plotBottom); g.lineTo(px, plotBottom + 4); g.stroke()
    g.fillText(String(+v.toFixed(4)), px, plotBottom + 16)
  }
  g.textAlign = 'right'; g.fillText(spec.xLabel, w - M.r, plotBottom + 30)

  // integral values below the axis (NMR convention)
  const ref = withArea[refIdx]?.area
  if (ref && withArea.every(r => Math.min(toPx(r.a), toPx(r.b)) > M.l + 62)) {
    g.font = font(10.5, 400, true); g.fillStyle = pal.ink3; g.textAlign = 'left'; g.fillText('integrals', M.l, plotBottom + 34)
  }
  g.font = font(11, 600); g.fillStyle = pal.green; g.textAlign = 'center'
  // each value goes in the first of three rows where it doesn't collide with one already placed
  const usedRows: { px: number; w: number; row: number }[] = []
  ;[...withArea].sort((p, q) => toPx((p.a + p.b) / 2) - toPx((q.a + q.b) / 2)).forEach(r => {
    const a = toPx(r.a), b = toPx(r.b), px = (a + b) / 2
    const text = ref ? (r.area / ref * refH).toFixed(2) : ''
    const tw = g.measureText(text).width + 6
    let row = 0
    while (row < 2 && usedRows.some(q => q.row === row && Math.abs(q.px - px) < (q.w + tw) / 2)) row++
    usedRows.push({ px, w: tw, row })
    g.fillRect(Math.min(a, b), plotBottom + 22, Math.abs(b - a), 1.5)
    if (row) g.fillRect(px - 0.4, plotBottom + 23.5, 0.8, row * 12)
    if (text) g.fillText(text, px, plotBottom + 34 + row * 12)
  })

  // signal labels in the top strip, spread apart, each with a leader line down to its multiplet
  if (signalTags.length) {
    g.font = font(11, 600); g.textAlign = 'center'
    const tags = signalTags.map(t => {
      const lo2 = Math.min(t.a, t.b), hi2 = Math.max(t.a, t.b)
      let top = -Infinity
      for (let j = lowerBound(spec.x, lo2); j < spec.x.length && spec.x[j] <= hi2; j++) if (spec.y[j] > top) top = spec.y[j]
      const px = toPx((t.a + t.b) / 2)
      return { ...t, px, lx: px, peakY: Math.max(plotTop, toPy(top)), w: g.measureText(t.text).width + 10 }
    }).filter(t => t.px >= M.l && t.px <= w - M.r).sort((p, q) => p.px - q.px)
    const right = w - M.r
    // push labels right until they don't overlap, then pull the row back inside the plot; false if they don't fit
    const spread = () => {
      tags.forEach(t => { t.lx = t.px })
      for (let k = 1; k < tags.length; k++) tags[k].lx = Math.max(tags[k].lx, tags[k - 1].lx + (tags[k - 1].w + tags[k].w) / 2)
      for (let k = tags.length - 1; k >= 0; k--) {
        const lim = k === tags.length - 1 ? right - tags[k].w / 2 : tags[k + 1].lx - (tags[k + 1].w + tags[k].w) / 2
        tags[k].lx = Math.min(tags[k].lx, lim)
      }
      return !tags.length || tags[0].lx >= M.l + tags[0].w / 2
    }
    if (!spread()) {
      // too many for one row (a narrow window, or zoomed out): just the shifts, which the table spells out in full
      tags.forEach(t => { t.text = t.text.split(' ')[0]; t.w = g.measureText(t.text).width + 10 })
      if (!spread()) {
        // still too many: those that have room where they are, the others left out until you zoom in
        let edge = -Infinity
        for (const t of tags) {
          t.lx = Math.min(right - t.w / 2, Math.max(M.l + t.w / 2, t.px))
          if (t.lx - t.w / 2 < edge) t.text = ''
          else edge = t.lx + t.w / 2
        }
      }
    }
    const textY = plotTop - predStrip - tagStrip + 12
    g.strokeStyle = pal.ink3; g.lineWidth = 0.8
    for (const t of tags) {
      if (!t.text) continue
      g.beginPath(); g.moveTo(t.lx, textY + 4); g.lineTo(t.lx, textY + 9); g.lineTo(t.px, plotTop - 1); g.lineTo(t.px, Math.max(plotTop - 1, t.peakY - 4)); g.stroke()
    }
    g.fillStyle = pal.ink
    for (const t of tags) g.fillText(t.text, t.lx, textY)
  }

  // predicted shifts (NMRShiftDB2): a lane of sticks just above the spectrum, height ~ number of nuclei, bar = typical spread
  if (predStrip) {
    const laneBottom = plotTop - 3, laneTop = plotTop - predStrip + 4
    const maxN = Math.max(...predGroups.map(g => (pred!.nucleus === '1H' ? g.nH : g.atoms.length)), 1)
    g.fillStyle = PRED_COLOR; g.strokeStyle = PRED_COLOR
    g.font = font(10, 400, true); g.textAlign = 'left'
    g.globalAlpha = 0.85; g.fillText('predicted', M.l + 2, laneTop + 8); g.globalAlpha = 1
    for (const gr of predGroups) {
      const v = gr.shift!
      if (v < Math.min(x0, x1) || v > Math.max(x0, x1)) continue
      const px = toPx(v)
      const hgt = 5 + (laneBottom - laneTop - 5) * ((pred!.nucleus === '1H' ? gr.nH : gr.atoms.length) / maxN)
      if (gr.sd) {
        const a = toPx(v - gr.sd), b2 = toPx(v + gr.sd)
        g.globalAlpha = 0.22; g.fillRect(Math.min(a, b2), laneBottom - 3, Math.max(1, Math.abs(b2 - a)), 3); g.globalAlpha = 1
      }
      g.lineWidth = gr.confidence === 'low' ? 1 : 2
      g.setLineDash(gr.confidence === 'low' ? [2, 2] : [])
      g.beginPath(); g.moveTo(px, laneBottom); g.lineTo(px, laneBottom - hgt); g.stroke()
      g.setLineDash([])
    }
  }

  // peak labels
  if (showPeaks) {
    g.fillStyle = pal.accent; g.strokeStyle = pal.accent; g.font = font(10)
    let lastLabel = -1e9
    for (const p of [...peaks].sort((a, b) => toPx(a.x) - toPx(b.x))) {
      const px = toPx(p.x), py = toPy(p.y)
      if (py < plotTop) continue
      g.beginPath(); g.moveTo(px, py - 3); g.lineTo(px, py - 8); g.stroke()
      const inSignal = signalTags.some(t => p.x >= Math.min(t.a, t.b) && p.x <= Math.max(t.a, t.b))
      if (!inSignal && px - lastLabel > 11 && py > plotTop + 26) {
        g.save(); g.translate(px + 3.5, py - 10); g.rotate(-Math.PI / 2); g.textAlign = 'left'; g.fillText(fmtX(spec, p.x), 0, 0); g.restore()
        lastLabel = px
      }
    }
  }

  // hover crosshair
  if (!forExport && hover !== null) {
    const px = toPx(hover)
    g.strokeStyle = pal.ink3; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(px + 0.5, plotTop); g.lineTo(px + 0.5, plotBottom); g.stroke(); g.setLineDash([])
  }
}

/** Export PNG's size: 1000 × 420, drawn at twice the resolution. */
export const EXPORT = { w: 1000, h: 420, scale: 2 }

/** The viewer's state for a file when it first opens: the saved analysis (processing, referencing, integrals,
 *  multiplet labels), or, with none saved, the automatic analysis the viewer runs on opening. */
export interface Prepared { spec: Spectrum; state: DrawState; automatic: boolean }

/** How a raw FID is processed (phase, line broadening), as saved. */
export const fidOptions = (saved: SavedAnalysis | null | undefined) =>
  saved?.fid ? { ph0: saved.fid.ph0, ph1: saved.fid.ph1, lb: saved.fid.lb ?? undefined } : {}

/** What the viewer restores when a file opens: referencing, the window, saved integrals and labels, the peak
 *  threshold; the solvent residual's offset; and whether it analyses automatically (nothing saved for an NMR file). */
export function restoreAnalysis(s: Spectrum, saved: SavedAnalysis | null | undefined) {
  const offset = saved?.offset || 0
  const view: [number, number] = [s.x[0] + offset, s.x[s.x.length - 1] + offset]
  const kept = saved?.integrals?.length ? {
    regions: saved.integrals.map(([a, b]) => ({ a, b })) as Region[],
    labels: (saved.labels || saved.integrals.map(() => ({ mult: '', j: '' }))) as Label[],
    refIdx: Math.min(saved.ref_idx ?? 0, saved.integrals.length - 1),
    refH: saved.ref_value || 1,
  } : null
  let solventShift = 0, residual: ReturnType<typeof findResidual> = null
  if (s.kind === 'nmr' && !offset) {
    const res = findResidual(s)
    if (res && Math.abs(res.found - res.expected) > 0.005) { residual = res; solventShift = res.found - res.expected }
  }
  const auto = !saved?.integrals?.length && s.kind === 'nmr' && !(is13C(s) && saved?.threshold)
  return { offset, view, saved: kept, threshold: saved?.threshold || 0, residual, solventShift, auto }
}

/** The product's predicted 1H shifts (confident, non-exchangeable groups), so auto-analysis doesn't call them solvent. */
export async function predictedShifts(productSmiles: string | null | undefined): Promise<number[]> {
  if (!productSmiles) return []
  try {
    const p = await api.get<Prediction>(`/api/nmr/predict?smiles=${encodeURIComponent(productSmiles)}&nucleus=1H`)
    return p.groups.filter(g => g.shift != null && !g.exchangeable && (g.confidence === 'high' || g.confidence === 'medium')).map(g => g.shift!)
  } catch { return [] }
}

export async function prepareSpectrum(buf: ArrayBuffer, filename: string, saved: SavedAnalysis | null | undefined, opts: {
  expectedH?: number | null; productMW?: number | null; productSmiles?: string | null
} = {}): Promise<Prepared> {
  const s = await parseSpectrumBuffer(buf, filename, fidOptions(saved))
  const r = restoreAnalysis(s, saved)
  const spec = shifted(s, r.offset)
  const view = r.view
  let regions: Region[] = r.saved?.regions || [], labels: Label[] = r.saved?.labels || []
  let refIdx = r.saved?.refIdx ?? 0, refH = r.saved?.refH ?? 1, threshold = r.threshold || 0.05
  let impurities: Impurities = saved?.impurities || []
  const automatic = r.auto
  if (r.auto) {  // as the viewer's runAuto does when it opens a file with nothing saved
    if (spec.kind === 'nmr' && !is13C(spec)) {
      const a = autoAnalyse1H(spec, opts.expectedH ?? null, r.solventShift, opts.productMW ?? null, await predictedShifts(opts.productSmiles))
      impurities = a.impurities.map(i => ({ short: i.short, name: i.name, molPct: i.molPct, wtPct: i.wtPct, signals: i.signals.map(p => +p.toFixed(4)) }))
      regions = a.regions; labels = a.regions.map(() => ({ mult: '', j: '' }))
      refIdx = a.refIdx; refH = a.refValue; threshold = 0.05
    } else threshold = 6
  }
  const withArea = regions.map(r => ({ ...r, area: integrate(spec, r.a, r.b) }))
  const state: DrawState = {
    spec, view, yScale: 1, withArea, refIdx, refH, peaks: peaksIn(spec, view, threshold), showPeaks: true,
    markers: solventMarkers(spec), impurities, overlays: [], overlayMode: saved?.overlay_mode || 'stack',
    signalTags: isProton(spec) ? signalTagsFor(spec, withArea, labels, refIdx, refH) : [],
    pred: null, showPred: false, drag: null, hover: null,
  }
  return { spec, state, automatic }
}

/** A spectrum drawn as Export PNG draws it, as PNG bytes (for a document). */
export async function spectrumPng(p: Prepared, size = EXPORT): Promise<Uint8Array> {
  const c = document.createElement('canvas'); c.width = size.w * size.scale; c.height = size.h * size.scale
  const g = c.getContext('2d')!; g.scale(size.scale, size.scale)
  drawSpectrum(g, size.w, size.h, LIGHT, p.state, true)
  const blob = await new Promise<Blob | null>(res => c.toBlob(res, 'image/png'))
  if (!blob) throw new Error('the browser could not make the picture')
  return new Uint8Array(await blob.arrayBuffer())
}
