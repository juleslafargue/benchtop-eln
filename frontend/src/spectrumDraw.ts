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
  /** The spectrometer frequency (MHz) typed in for a ¹H file that doesn't give it (null: not typed). Saved from 0.36.5
   *  on, only for such files; an older analysis of one has no such key, and was worked out as if at 400 MHz. */
  frequency?: number | null
  /** 0.36.5 on: negative lines (DEPT, APT) are listed in a ¹³C peak list. Older analyses keep the list they had. */
  negatives?: boolean
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
export function peaksIn(spec: Spectrum, view: [number, number], threshold: number, negatives = false) {
  if (spec.kind === 'nmr' && is13C(spec)) { // carbon: signal-to-noise picking, solvent excluded
    const lo = Math.min(view[0], view[1]), hi = Math.max(view[0], view[1])
    return autoPeaks13C(spec, threshold >= 1 ? threshold : 6, { negative: negatives }).peaks.filter(p => p.x >= lo && p.x <= hi)
  }
  return pickPeaks(spec, view[0], view[1], threshold, 50)
}

/** One label per integrated 1H signal: "7.42 t, 2H", drawn above the multiplet. */
export function signalTagsFor(spec: Spectrum, withArea: (Region & { area: number })[], labels: Label[], refIdx: number, refH: number, mults?: Multiplet[]): SignalTag[] {
  const ref = withArea[refIdx]?.area
  return withArea.map((r, i) => {
    const m = applyLabel(mults?.[i] ?? analyzeMultiplet(spec, r.a, r.b), labels[i])
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

/** Minimum and maximum of each block of 16, 64, 256… points of a trace, made once per spectrum: a spectrum with far more
 *  points than the plot has pixels is drawn from these (0.36.4 went through all 8 million points of a big file on each
 *  move of the mouse, and the tab froze). */
const envelopes = new WeakMap<ArrayLike<number>, { size: number; mn: Float64Array; mx: Float64Array }[]>()
function envelope(y: ArrayLike<number>) {
  let levels = envelopes.get(y)
  if (levels) return levels
  levels = []
  let size = 16, src: { mn: ArrayLike<number>; mx: ArrayLike<number> } = { mn: y, mx: y }, step = 16
  while (y.length / size >= 2048) {
    const n = Math.ceil(y.length / size), mn = new Float64Array(n), mx = new Float64Array(n)
    const len = src.mn.length
    for (let b = 0; b < n; b++) {
      let lo = Infinity, hi = -Infinity
      for (let i = b * step, e = Math.min(len, i + step); i < e; i++) { if (src.mn[i] < lo) lo = src.mn[i]; if (src.mx[i] > hi) hi = src.mx[i] }
      mn[b] = lo; mx[b] = hi
    }
    levels.push({ size, mn, mx })
    src = { mn, mx }; size *= 4; step = 4
  }
  envelopes.set(y, levels)
  return levels
}

/** Lowest and highest of points i0..i1, from the summary blocks where they cover whole blocks. */
function rangeOf(y: ArrayLike<number>, i0: number, i1: number): [number, number] {
  let lo = Infinity, hi = -Infinity
  const lv = i1 - i0 > 1 << 16 ? envelope(y)[0] : undefined
  if (!lv) { for (let i = i0; i <= i1; i++) { if (y[i] < lo) lo = y[i]; if (y[i] > hi) hi = y[i] } return [lo, hi] }
  const b0 = Math.ceil(i0 / lv.size), b1 = Math.floor((i1 + 1) / lv.size)  // whole blocks b0..b1-1
  for (let i = i0; i < Math.min(i1 + 1, b0 * lv.size); i++) { if (y[i] < lo) lo = y[i]; if (y[i] > hi) hi = y[i] }
  for (let b = b0; b < b1; b++) { if (lv.mn[b] < lo) lo = lv.mn[b]; if (lv.mx[b] > hi) hi = lv.mx[b] }
  for (let i = Math.max(i0, b1 * lv.size); i <= i1; i++) { if (y[i] < lo) lo = y[i]; if (y[i] > hi) hi = y[i] }
  return [lo, hi]
}

/** Points i0..i1 of a trace as a line, one vertical stroke per pixel from its lowest to its highest value. */
function trace(g: CanvasRenderingContext2D, xs: Float64Array, ys: ArrayLike<number>, i0: number, i1: number,
  toPx: (x: number) => number, toPy: (y: number) => number, w: number) {
  const perPx = (i1 - i0) / Math.max(1, w - M.l - M.r)
  const level = perPx >= 64 ? [...envelope(ys)].reverse().find(l => l.size * 4 <= perPx) : undefined
  let last = -1, mn = 0, mx = 0, first = true
  const add = (px: number, lo: number, hi: number, y0: number) => {
    if (px !== last) {
      if (last >= 0) { g.lineTo(last, toPy(mn)); g.lineTo(last, toPy(mx)) }
      last = px; mn = lo; mx = hi
      if (first) { g.moveTo(px, toPy(y0)); first = false }
    } else { if (lo < mn) mn = lo; if (hi > mx) mx = hi }
  }
  if (level) {
    const b0 = Math.floor(i0 / level.size), b1 = Math.min(level.mn.length - 1, Math.floor(i1 / level.size))
    for (let b = b0; b <= b1; b++) add(Math.round(toPx(xs[Math.min(xs.length - 1, b * level.size)])), level.mn[b], level.mx[b], ys[b * level.size])
  } else {
    for (let i = i0; i <= i1; i++) add(Math.round(toPx(xs[i])), ys[i], ys[i], ys[i])
  }
  if (last >= 0) { g.lineTo(last, toPy(mn)); g.lineTo(last, toPy(mx)) }
}

export function drawSpectrum(g: CanvasRenderingContext2D, w: number, h: number, pal: Palette, st: DrawState, forExport = false) {
  const { spec, view, yScale, withArea, refIdx, refH, peaks, showPeaks, markers, impurities, overlays, overlayMode, signalTags, pred, showPred, drag, hover } = st
  const { toPx } = geometry(spec, view, w)
  g.clearRect(0, 0, w, h)
  if (forExport) { g.fillStyle = pal.paper; g.fillRect(0, 0, w, h) }
  const [x0, x1] = view
  const i0 = Math.max(0, lowerBound(spec.x, x0) - 1), i1 = Math.min(spec.x.length - 1, lowerBound(spec.x, x1) + 1)
  let [lo, hi] = rangeOf(spec.y, i0, i1)
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
  } else trace(g, spec.x, spec.y, i0, i1, toPx, toPy, w)
  g.stroke()
  g.restore()

  // comparison spectra
  overlays.forEach((o, k) => {
    const os = o.spec
    const j0 = Math.max(0, lowerBound(os.x, Math.min(x0, x1)) - 1), j1 = Math.min(os.x.length - 1, lowerBound(os.x, Math.max(x0, x1)) + 1)
    if (j1 <= j0) return
    const [olo, ohi] = rangeOf(os.y, j0, j1)
    const bandH = (plotBottom - fullTop) / bands
    const b0 = stacked ? plotBottom - (k + 1) * bandH : plotBottom, hgt = stacked ? bandH * 0.92 : plotH
    const oy = (y: number) => b0 - ((y - olo) / ((ohi - olo) || 1)) * hgt * yScale
    g.save(); g.beginPath(); g.rect(M.l, (stacked ? b0 - bandH : fullTop) - 2, w - M.l - M.r, (stacked ? bandH : plotBottom - fullTop) + 4); g.clip()
    g.strokeStyle = OVERLAY_COLORS[k % OVERLAY_COLORS.length]; g.lineWidth = 1; g.globalAlpha = stacked ? 1 : 0.85; g.beginPath()
    trace(g, os.x, os.y, j0, j1, toPx, oy, w)
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
    // about two points of the curve a pixel: a region of a big spectrum can hold millions of points (the sum still
    // goes through every one, in a tight loop)
    const len = j1 - j0 + 1
    const every = Math.max(1, Math.floor(len / (2 * Math.max(1, Math.abs(toPx(spec.x[j1]) - toPx(spec.x[j0]))))))
    let acc = 0, prev = spec.reversed ? j1 : j0
    g.beginPath()
    for (let n = 0; n < len; n = n + every >= len && n < len - 1 ? len - 1 : n + every) {
      const j = spec.reversed ? j1 - n : j0 + n
      if (spec.reversed) for (let k = prev - 1; k >= j; k--) acc += Math.abs(spec.x[k + 1] - spec.x[k]) * (spec.y[k] + spec.y[k + 1]) / 2
      else for (let k = prev + 1; k <= j; k++) acc += Math.abs(spec.x[k] - spec.x[k - 1]) * (spec.y[k] + spec.y[k - 1]) / 2
      prev = j
      const px = toPx(spec.x[j]), py = base - (r.area ? acc / r.area : 0) * height
      if (n === 0) g.moveTo(px, py); else g.lineTo(px, py)
    }
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
      const j0 = lowerBound(spec.x, lo2), j1 = Math.min(spec.x.length, lowerBound(spec.x, hi2 + 1e-12)) - 1
      const top = j1 >= j0 ? rangeOf(spec.y, j0, j1)[1] : -Infinity
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
  // a ¹H file that doesn't say its frequency: the one typed in for it; or, for an analysis saved before 0.36.5 (no
  // frequency key), the 400 MHz its J values were worked out with, so it shows what it showed (and says so)
  const noFreq = isProton(s) && !s.frequency
  const frequency = noFreq && typeof saved?.frequency === 'number' && saved.frequency > 0 ? saved.frequency : null
  const legacy400 = noFreq && !!saved && !('frequency' in saved) && !!saved.integrals?.length
  const negatives = is13C(s) && (auto || !!saved?.negatives)
  return { offset, view, saved: kept, threshold: saved?.threshold || 0, residual, solventShift, auto, frequency, legacy400, negatives }
}

/** The spectrum as analysed: with the frequency typed in for it, or the 400 MHz an older analysis assumed. */
export function withFrequency(s: Spectrum, frequency: number | null, legacy400: boolean): Spectrum {
  if (s.frequency) return s
  if (frequency) return { ...s, frequency }
  return legacy400 ? { ...s, assumedFrequency: 400 } : s
}

/** A frequency named in a file's name or title ("60MHz", "400 MHz"), offered for the person to confirm. */
export function frequencyHint(s: Spectrum, filename: string): number | null {
  for (const t of [filename, s.title]) {
    const m = /(?:^|[^\d.])(\d{2,4}(?:\.\d+)?)\s*-?\s*MHz/i.exec(t || '')
    if (m && Number(m[1]) >= 20 && Number(m[1]) <= 1300) return Number(m[1])
  }
  return null
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
  const parsed = await parseSpectrumBuffer(buf, filename, fidOptions(saved))
  const r = restoreAnalysis(parsed, saved)
  const s = withFrequency(parsed, r.frequency, r.legacy400)
  const spec = shifted(s, r.offset)
  const view = r.view
  let regions: Region[] = r.saved?.regions || [], labels: Label[] = r.saved?.labels || []
  let refIdx = r.saved?.refIdx ?? 0, refH = r.saved?.refH ?? 1, threshold = r.threshold || 0.05
  let impurities: Impurities = saved?.impurities || []
  const automatic = r.auto
  if (r.auto) {  // as the viewer's runAuto does when it opens a file with nothing saved
    if (spec.kind === 'nmr' && !is13C(spec)) {
      // with no frequency known this finds nothing (and says why): no J, and no signals grouped by a guess
      const a = autoAnalyse1H(spec, opts.expectedH ?? null, r.solventShift, opts.productMW ?? null, await predictedShifts(opts.productSmiles))
      impurities = a.impurities.map(i => ({ short: i.short, name: i.name, molPct: i.molPct, wtPct: i.wtPct, signals: i.signals.map(p => +p.toFixed(4)) }))
      regions = a.regions; labels = a.regions.map(() => ({ mult: '', j: '' }))
      refIdx = a.refIdx; refH = a.refValue; threshold = 0.05
    } else threshold = 6
  }
  const withArea = regions.map(r => ({ ...r, area: integrate(spec, r.a, r.b) }))
  const state: DrawState = {
    spec, view, yScale: 1, withArea, refIdx, refH, peaks: peaksIn(spec, view, threshold, r.negatives), showPeaks: true,
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
