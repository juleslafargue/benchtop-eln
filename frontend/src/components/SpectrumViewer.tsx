import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useApp } from '../App'
import { applyOverrides, assign, groupLabel, predMult, PredictionPanel, usePrediction, type ConfirmedAssignments, type ObservedSignal, type Prediction } from './Prediction'
import { baselineCorrect, phased } from '../fid'
import Modal from './Modal'
import { day, api } from '../api'
import {
  analyzeMultiplet, autoAnalyse1H, autoPeaks13C, findResidual, formatSignal, integrate, lowerBound, MULTIPLICITIES,
  parseSpectrumBuffer, pickPeaks,
  referenceChoices, shifted, snapToPeak, solventMarkers, type ImpurityResult, type Multiplet, type Spectrum,
} from '../spectra'

export interface SavedAnalysis {
  integrals: [number, number][]; ref_idx: number; ref_value: number; threshold: number
  labels?: { mult: string; j: string }[]; offset?: number
  impurities?: Pick<ImpurityResult, 'short' | 'name' | 'molPct' | 'wtPct' | 'signals'>[]
  overlays?: number[]; overlay_mode?: 'stack' | 'overlay'
  fid?: { ph0: number; ph1: number; lb: number | null }
  assignments?: ConfirmedAssignments
}
interface Overlay { id: number; label: string; spec: Spectrum }
interface SpectrumListing { id: number; filename: string; eid: string; title: string; same_experiment: boolean; created_at: string }
const PRED_COLOR = '#8c62d9'
const OVERLAY_COLORS = ['#3a7ae6', '#c27a00', '#8c62d9', '#2a9d4b'] // validated for colour-blind separation and contrast on both themes
interface StructureInfo { formula: string; total_h: number; exchangeable_h: number; h_environments: { count: number }[]; unique_carbons: number; total_carbons: number }
interface Region { a: number; b: number }
interface Label { mult: string; j: string }
type Tool = 'zoom' | 'integrate' | 'reference'

const M = { l: 14, r: 14, t: 28, b: 62 }
const is13C = (s: Spectrum) => /13C/i.test(s.nucleus || '')
const isProton = (s: Spectrum) => s.kind === 'nmr' && !is13C(s)
const LIGHT = { ink: '#17212c', ink3: '#7a8896', accent: '#b8336a', rule: '#cbd5de', green: '#1d7466', paper: '#ffffff' }

function niceTicks(lo: number, hi: number, n = 8): number[] {
  const span = hi - lo
  if (span <= 0) return []
  const raw = span / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map(k => k * mag).find(s => s >= raw) || raw
  const out: number[] = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10))
  return out
}
const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()
const fmtX = (s: Spectrum, x: number) => s.kind === 'nmr' ? x.toFixed(is13C(s) ? 1 : 2) : s.kind === 'ir' ? Math.round(x).toString() : x.toFixed(1)

function applyLabel(m: Multiplet, lab?: Label): Multiplet {
  if (!lab) return m
  const j = lab.j.trim() ? lab.j.split(/[,;/\s]+/).map(Number).filter(v => Number.isFinite(v) && v > 0) : m.j
  const mult = lab.mult || m.mult
  return { ...m, mult, j: mult === 's' || mult === 'br s' || mult === 'm' ? [] : j }
}

function header(s: Spectrum) {
  if (s.kind === 'nmr') {
    const nuc = (s.nucleus || '1H').replace(/^1H$/, '¹H').replace(/^13C$/, '¹³C').replace(/^19F$/, '¹⁹F')
    return `${nuc} NMR (${s.frequency ? `${Math.round(s.frequency)} MHz` : '? MHz'}${s.solvent ? `, ${s.solvent}` : ''})`
  }
  return s.kind === 'ir' ? 'IR (cm⁻¹)' : s.kind === 'ms' ? 'MS (m/z)' : s.title
}

export function summarise(s: Spectrum, regions: (Region & { area: number })[], labels: Label[], refIdx: number, refH: number, peaks: { x: number }[]) {
  const head = header(s)
  if (regions.length && isProton(s)) {
    const ref = regions[refIdx]?.area || regions[0].area
    const parts = regions.map((r, i) => ({ r, i })).sort((p, q) => (q.r.a + q.r.b) - (p.r.a + p.r.b))
      .map(({ r, i }) => formatSignal(applyLabel(analyzeMultiplet(s, r.a, r.b), labels[i]), r.area / ref * refH))
    return `${head} δ ${parts.join(', ')}`
  }
  if (regions.length) {
    const ref = regions[refIdx]?.area || regions[0].area
    const parts = [...regions].sort((p, q) => (s.reversed ? (q.a + q.b) - (p.a + p.b) : (p.a + p.b) - (q.a + q.b)))
      .map(r => `${fmtX(s, Math.max(r.a, r.b))}–${fmtX(s, Math.min(r.a, r.b))} (${(r.area / ref * refH).toFixed(2)})`)
    return `${head}: ${parts.join(', ')}`
  }
  // for 13C and other nuclei, leave out solvent signals and near-duplicates
  const solv = s.kind === 'nmr' ? solventMarkers(s).map(m => m.ppm) : []
  const kept: { x: number }[] = []
  for (const p of peaks) if (!solv.some(c => Math.abs(p.x - c) < 0.6) && !kept.some(q => Math.abs(q.x - p.x) < 0.05)) kept.push(p)
  const xs = kept.sort((p, q) => (s.reversed ? q.x - p.x : p.x - q.x)).map(p => fmtX(s, p.x))
  return s.kind === 'nmr' ? `${head} δ ${xs.join(', ')}` : `${head}: ${xs.join(', ')}`
}

export default function SpectrumViewer({ attachmentId, filename, onInsert, saved, onSave, expectedH, productSmiles, productMW }: {
  attachmentId: number; filename: string; onInsert?: (text: string) => void
  saved?: SavedAnalysis | null; onSave?: (a: SavedAnalysis) => void
  expectedH?: number | null // protons in the product's formula, used to scale automatic integrals
  productSmiles?: string | null; productMW?: number | null // for the structure check and impurity wt%
}) {
  const { user } = useApp()
  const [raw, setRaw] = useState<Spectrum | null>(null)
  const [err, setErr] = useState('')
  const [offset, setOffset] = useState(0)
  const [view, setView] = useState<[number, number] | null>(null)
  const [yScale, setYScale] = useState(1)
  const [tool, setTool] = useState<Tool>('zoom')
  const [regions, setRegions] = useState<Region[]>([])
  const [labels, setLabels] = useState<Label[]>([])
  const [refIdx, setRefIdx] = useState(0)
  const [refH, setRefH] = useState(1)
  const [showPeaks, setShowPeaks] = useState(true)
  const [showSignals, setShowSignals] = useState(true)
  const [threshold, setThreshold] = useState(0.05)
  const [expanded, setExpanded] = useState(!!user.preferences?.spectrum_expanded)
  const [hover, setHover] = useState<number | null>(null)
  const [refPick, setRefPick] = useState<{ x: number; left: number; top: number; target: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const canvas = useRef<HTMLCanvasElement>(null)
  const wrap = useRef<HTMLDivElement>(null)
  const drag = useRef<{ x0: number; x1: number } | null>(null)
  const restored = useRef(false)
  const [note, setNote] = useState<string | null>(null)
  const [impurities, setImpurities] = useState<NonNullable<SavedAnalysis['impurities']>>(saved?.impurities || [])
  const [structure, setStructure] = useState<StructureInfo | null>(null)
  const [overlays, setOverlays] = useState<Overlay[]>([])
  const [overlayMode, setOverlayMode] = useState<'stack' | 'overlay'>(saved?.overlay_mode || 'stack')
  const [picking, setPicking] = useState(false)
  const [overlayErr, setOverlayErr] = useState('')
  const [showPred, setShowPred] = useState(true)
  const [overrides, setOverrides] = useState<Record<number, number | null>>({})
  const [confirmed, setConfirmed] = useState<ConfirmedAssignments | null>(saved?.assignments || null)
  const bufRef = useRef<ArrayBuffer | null>(null)
  const addOverlay = async (id: number, label: string) => {
    if (overlays.some(o => o.id === id) || overlays.length >= 4) return
    try {
      const buf = await (await api.raw(`/api/attachments/${id}?inline=true`)).arrayBuffer()
      const o = await parseSpectrumBuffer(buf, label)
      if (raw && (o.kind !== raw.kind || (o.kind === 'nmr' && (o.nucleus || '1H') !== (raw.nucleus || '1H')))) {
        setOverlayErr(`${label} is a different kind of spectrum, so it can't share this axis.`); return
      }
      setOverlays(os => (os.some(x => x.id === id) ? os : [...os, { id, label, spec: o }])); setOverlayErr('')
    } catch (e: any) { setOverlayErr(`${label}: ${e.message}`) }
  }
  useEffect(() => {
    if (!productSmiles) { setStructure(null); return }
    api.get<StructureInfo>(`/api/chem/structure-check?smiles=${encodeURIComponent(productSmiles)}`).then(setStructure).catch(() => setStructure(null))
  }, [productSmiles])
  const [residual, setResidual] = useState<{ label: string; expected: number; found: number } | null>(null)

  // predicted product 1H shifts (confident, non-exchangeable groups) so auto-analysis doesn't call them solvent
  const productShifts = async (): Promise<number[]> => {
    if (!productSmiles) return []
    try {
      const p = await api.get<Prediction>(`/api/nmr/predict?smiles=${encodeURIComponent(productSmiles)}&nucleus=1H`)
      return p.groups.filter(g => g.shift != null && !g.exchangeable && (g.confidence === 'high' || g.confidence === 'medium')).map(g => g.shift!)
    } catch { return [] }
  }
  const runAuto = async (s: Spectrum, announce = true, solventShift = 0) => {
    if (s.kind === 'nmr' && !is13C(s)) {
      const r = autoAnalyse1H(s, expectedH ?? null, solventShift, productMW ?? null, await productShifts())
      // impurity positions are found in the unreferenced spectrum; store them on the current axis
      setImpurities(r.impurities.map(i => ({ short: i.short, name: i.name, molPct: i.molPct, wtPct: i.wtPct, signals: i.signals.map(p => +p.toFixed(4)) })))
      setRegions(r.regions); setLabels(r.regions.map(() => ({ mult: '', j: '' })))
      setRefIdx(r.refIdx); setRefH(r.refValue); setShowPeaks(true); setThreshold(0.05)
      if (announce) setNote(`Auto-analysed: ${r.regions.length} signals. ${r.note} Check the integrals and multiplicities before using the text.`)
    } else {
      const r = autoPeaks13C(s, 6)
      setThreshold(6); setShowPeaks(true)
      if (announce) setNote(`Auto-picked ${r.peaks.length} peaks with signal-to-noise above 6${s.kind === 'nmr' ? '; solvent signals are left out of the list' : ''}.`)
    }
  }

  // ---------------------------------------------------------------- load + restore
  useEffect(() => {
    let alive = true
    api.raw(`/api/attachments/${attachmentId}?inline=true`).then(r => r.arrayBuffer()).then(async buf => {
      if (!alive) return
      try {
        bufRef.current = buf
        const s = await parseSpectrumBuffer(buf, filename, saved?.fid ? { ph0: saved.fid.ph0, ph1: saved.fid.ph1, lb: saved.fid.lb ?? undefined } : {})
        if (!alive) return
        const off = saved?.offset || 0
        setRaw(s); setOffset(off)
        setView([s.x[0] + off, s.x[s.x.length - 1] + off])
        if (saved?.integrals?.length) {
          setRegions(saved.integrals.map(([a, b]) => ({ a, b })))
          setLabels(saved.labels || saved.integrals.map(() => ({ mult: '', j: '' })))
          setRefIdx(Math.min(saved.ref_idx ?? 0, saved.integrals.length - 1))
          setRefH(saved.ref_value || 1)
        }
        if (saved?.threshold) setThreshold(saved.threshold)
        let solventShift = 0
        if (s.kind === 'nmr' && !off) {
          const res = findResidual(s)
          if (res && Math.abs(res.found - res.expected) > 0.005) { setResidual(res); solventShift = res.found - res.expected }
        }
        if (!saved?.integrals?.length && s.kind === 'nmr' && !(is13C(s) && saved?.threshold)) runAuto(shifted(s, off), true, solventShift)
        setTimeout(() => { restored.current = true }, 0)
      } catch (e: any) { setErr(e.message) }
    }).catch(e => setErr(e.message))
    return () => { alive = false }
  }, [attachmentId, filename]) // eslint-disable-line react-hooks/exhaustive-deps

  const spec = useMemo(() => (raw ? shifted(raw, offset) : null), [raw, offset])
  useEffect(() => {
    if (!raw || !saved?.overlays?.length) return
    api.get<SpectrumListing[]>(`/api/spectra?near=${attachmentId}&limit=200`).then(list => {
      for (const id of saved.overlays!) { const l = list.find(x => x.id === id); if (l) addOverlay(id, `${l.filename} (${l.eid})`) }
    }).catch(() => {})
  }, [raw]) // eslint-disable-line react-hooks/exhaustive-deps
  const withArea = useMemo(() => (spec ? regions.map(r => ({ ...r, area: integrate(spec, r.a, r.b) })) : []), [spec, regions])
  const peaks = useMemo(() => {
    if (!spec || !view || !showPeaks) return []
    if (spec.kind === 'nmr' && is13C(spec)) { // carbon: signal-to-noise picking, solvent excluded
      const lo = Math.min(view[0], view[1]), hi = Math.max(view[0], view[1])
      return autoPeaks13C(spec, threshold >= 1 ? threshold : 6).peaks.filter(p => p.x >= lo && p.x <= hi)
    }
    return pickPeaks(spec, view[0], view[1], threshold, 50)
  }, [spec, view, showPeaks, threshold])
  const markers = useMemo(() => (spec ? solventMarkers(spec) : []), [spec])
  const observed: ObservedSignal[] = useMemo(() => {
    if (!spec || spec.kind !== 'nmr') return []
    if (is13C(spec)) return peaks.map(p => ({ lo: p.x, hi: p.x, center: p.x, h: null }))
    const ref = withArea[refIdx]?.area
    return withArea.map(r => {
      const m = isProton(spec) ? analyzeMultiplet(spec, r.a, r.b) : null
      return { lo: Math.min(r.a, r.b), hi: Math.max(r.a, r.b), center: m ? m.shift : (r.a + r.b) / 2, h: ref ? r.area / ref * refH : null }
    })
  }, [spec, withArea, peaks, refIdx, refH])
  // one label per integrated 1H signal: "7.42 t, 2H", drawn above the multiplet
  const signalTags = useMemo(() => {
    if (!spec || !isProton(spec) || !showSignals) return []
    const ref = withArea[refIdx]?.area
    return withArea.map((r, i) => {
      const m = applyLabel(analyzeMultiplet(spec, r.a, r.b), labels[i])
      const h = ref ? r.area / ref * refH : 0
      const shift = m.mult === 'm' ? `${m.hi.toFixed(2)}–${m.lo.toFixed(2)}` : m.shift.toFixed(2)
      return { a: r.a, b: r.b, text: `${shift} ${m.mult}${h ? `, ${h < 0.95 ? h.toFixed(1) : Math.round(h)}H` : ''}` }
    })
  }, [spec, withArea, labels, refIdx, refH, showSignals])

  useEffect(() => {
    if (!restored.current || !onSave) return
    onSave({
      integrals: regions.map(r => [+r.a.toFixed(5), +r.b.toFixed(5)]), labels, ref_idx: refIdx, ref_value: refH,
      threshold, offset: +offset.toFixed(5), impurities,
      ...(overlays.length ? { overlays: overlays.map(o => o.id), overlay_mode: overlayMode } : {}),
      ...(raw?.fid ? { fid: { ph0: +raw.fid.ph0.toFixed(2), ph1: +raw.fid.ph1.toFixed(2), lb: raw.fid.lb } } : {}),
      ...(confirmed ? { assignments: confirmed } : {}),
    })
  }, [regions, labels, refIdx, refH, threshold, offset, impurities, overlays, overlayMode, raw?.fid?.ph0, raw?.fid?.ph1, raw?.fid?.lb, confirmed]) // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- raw FID: phase and line broadening
  const setPhase = (ph0: number, ph1: number) => setRaw(r => (r?.fid ? { ...r, y: baselineCorrect(phased(r.fid, ph0, ph1)), fid: { ...r.fid, ph0, ph1 } } : r))
  const setLB = async (lb: number) => {
    if (!raw?.fid || !bufRef.current) return
    const s = await parseSpectrumBuffer(bufRef.current, filename, { lb, ph0: raw.fid.ph0, ph1: raw.fid.ph1 })
    setRaw(s)
  }

  // ---------------------------------------------------------------- prediction (NMRShiftDB2) and assignment
  const nucleus: '1H' | '13C' | null = raw?.kind === 'nmr' ? (is13C(raw) ? '13C' : isProton(raw) ? '1H' : null) : null
  const { pred, status: predStatus, err: predErr, startUpdate } = usePrediction(productSmiles, nucleus)
  const auto = useMemo(() => (pred && observed.length ? assign(pred.groups, observed, pred.nucleus) : null), [pred, observed])
  // a saved confirmation becomes overrides once the prediction for the same structure is back
  const confirmedFor = useRef<string | null>(null)
  useEffect(() => {
    if (!pred || !confirmed || confirmed.smiles !== pred.smiles || confirmed.nucleus !== pred.nucleus || confirmedFor.current === pred.smiles) return
    confirmedFor.current = pred.smiles
    const ov: Record<number, number | null> = {}
    const used = new Set<number>()
    pred.groups.forEach((g, gi) => {
      const k = confirmed.items.findIndex((it, j) => !used.has(j) && it.atoms.join() === g.atoms.join())
      if (k >= 0) { ov[gi] = confirmed.items[k].shift; used.add(k) } else ov[gi] = null
    })
    setOverrides(ov)
  }, [pred, confirmed])
  const assignment = useMemo(() => (auto && pred ? applyOverrides(auto, overrides, observed, pred.groups, pred.nucleus) : auto), [auto, overrides, observed, pred])
  const confirm = () => {
    if (!pred || !assignment) return
    const items = pred.groups.map((g, gi) => {
      const a = assignment.byGroup[gi]
      const shift = a.manual ?? (a.signal != null ? observed[a.signal]?.center : null)
      return shift == null ? null : { atoms: g.atoms, shift: +shift.toFixed(3) }
    }).filter(Boolean) as ConfirmedAssignments['items']
    confirmedFor.current = pred.smiles
    setConfirmed({ nucleus: pred.nucleus, smiles: pred.smiles, items, solvent: raw?.solvent || '', confirmed_by: user.full_name || user.username, confirmed_at: new Date().toISOString() })
  }

  // ---------------------------------------------------------------- geometry
  const geom = useCallback((w: number) => {
    const [x0, x1] = view!
    const span = x1 - x0 || 1
    const toPx = (x: number) => spec!.reversed ? M.l + (x1 - x) / span * (w - M.l - M.r) : M.l + (x - x0) / span * (w - M.l - M.r)
    const toX = (px: number) => spec!.reversed ? x1 - (px - M.l) / (w - M.l - M.r) * span : x0 + (px - M.l) / (w - M.l - M.r) * span
    return { toPx, toX }
  }, [spec, view])

  // ---------------------------------------------------------------- drawing
  const drawTo = useCallback((g: CanvasRenderingContext2D, w: number, h: number, pal: typeof LIGHT, forExport = false) => {
    if (!spec || !view) return
    const { toPx } = geom(w)
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
    if (drag.current) {
      const a = toPx(drag.current.x0), b = toPx(drag.current.x1)
      g.fillStyle = (tool === 'integrate' ? pal.green : pal.accent) + '2a'
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
  }, [spec, view, yScale, withArea, refIdx, refH, peaks, showPeaks, tool, markers, hover, geom, impurities, overlays, overlayMode, signalTags, pred, showPred])

  const draw = useCallback(() => {
    const c = canvas.current
    if (!c || !spec) return
    const dpr = window.devicePixelRatio || 1
    const w = c.clientWidth, h = c.clientHeight
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr) }
    const g = c.getContext('2d')!
    g.setTransform(dpr, 0, 0, dpr, 0, 0)
    drawTo(g, w, h, { ink: css('--ink'), ink3: css('--ink-3'), accent: css('--accent'), rule: css('--rule'), green: css('--witnessed'), paper: css('--paper') })
  }, [drawTo, spec])

  useEffect(() => { draw() }, [draw, expanded])
  useEffect(() => {
    const ro = new ResizeObserver(() => draw())
    if (canvas.current) ro.observe(canvas.current)
    return () => ro.disconnect()
  }, [draw])

  // wheel: zoom around the cursor (vertical), pan (horizontal or shift)
  useEffect(() => {
    const c = canvas.current
    if (!c || !spec || !view) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = c.getBoundingClientRect()
      const { toX } = geom(rect.width)
      const [x0, x1] = view
      const full = [spec.x[0], spec.x[spec.x.length - 1]]
      const span = x1 - x0
      let n0 = x0, n1 = x1
      const panPx = e.shiftKey ? e.deltaY : e.deltaX
      if (Math.abs(panPx) > Math.abs(e.shiftKey ? 0 : e.deltaY)) {
        const dx = panPx / rect.width * span * (spec.reversed ? -1 : 1)
        n0 += dx; n1 += dx
      } else {
        const at = toX(e.clientX - rect.left)
        const f = Math.exp(e.deltaY * 0.0015)
        n0 = at - (at - x0) * f; n1 = at + (x1 - at) * f
      }
      if (n1 - n0 > full[1] - full[0]) { n0 = full[0]; n1 = full[1] }
      if (n0 < full[0]) { n1 += full[0] - n0; n0 = full[0] }
      if (n1 > full[1]) { n0 -= n1 - full[1]; n1 = full[1] }
      if (n1 - n0 > 1e-4) setView([n0, n1])
    }
    c.addEventListener('wheel', onWheel, { passive: false })
    return () => c.removeEventListener('wheel', onWheel)
  }, [spec, view, geom])

  useEffect(() => {
    if (!expanded) return
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape' && !refPick) setExpanded(false) }
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [expanded, refPick])

  if (err) return <p className="error">{filename}: {err}</p>
  if (!spec || !view) return <p className="faint">Loading spectrum…</p>

  // ---------------------------------------------------------------- interaction
  const xAt = (e: React.PointerEvent) => { const r = canvas.current!.getBoundingClientRect(); return geom(r.width).toX(e.clientX - r.left) }
  const onDown = (e: React.PointerEvent) => {
    if (tool === 'reference') {
      const r = canvas.current!.getBoundingClientRect()
      const span = Math.abs(view[1] - view[0])
      const x = snapToPeak(spec, xAt(e), span * 8 / r.width) // within ~8 px of the click
      const choices = referenceChoices(spec)
      const nearest = choices.reduce((p, q) => (Math.abs(q.ppm - x) < Math.abs(p.ppm - x) ? q : p), choices[0])
      setRefPick({ x, left: Math.min(e.clientX - r.left, r.width - 260), top: e.clientY - r.top + 12, target: nearest.ppm.toFixed(2) })
      return
    }
    (e.target as Element).setPointerCapture(e.pointerId)
    const x = xAt(e); drag.current = { x0: x, x1: x }; draw()
  }
  const onMove = (e: React.PointerEvent) => {
    setHover(xAt(e))
    if (drag.current) { drag.current.x1 = xAt(e); draw() }
  }
  const onUp = () => {
    const d = drag.current; drag.current = null
    if (!d) return
    const { toPx } = geom(canvas.current!.clientWidth)
    if (Math.abs(toPx(d.x1) - toPx(d.x0)) < 4) { draw(); return }
    const a = Math.min(d.x0, d.x1), b = Math.max(d.x0, d.x1)
    if (tool === 'zoom') setView([a, b])
    else { setRegions(rs => [...rs, { a, b }]); setLabels(ls => [...ls, { mult: '', j: '' }]) }
  }
  const fit = () => { setView([spec.x[0], spec.x[spec.x.length - 1]]); setYScale(1) }
  const applyReference = (target: number) => {
    const delta = target - refPick!.x
    setImpurities(is => is.map(i => ({ ...i, signals: i.signals.map(p => p + delta) })))
    setOffset(o => o + delta)
    setRegions(rs => rs.map(r => ({ a: r.a + delta, b: r.b + delta })))
    setView(v => (v ? [v[0] + delta, v[1] + delta] : v))
    setRefPick(null); setTool('zoom')
  }
  const resetReference = () => {
    const delta = -offset
    setImpurities(is => is.map(i => ({ ...i, signals: i.signals.map(p => p + delta) })))
    setOffset(0)
    setRegions(rs => rs.map(r => ({ a: r.a + delta, b: r.b + delta })))
    setView(v => (v ? [v[0] + delta, v[1] + delta] : v))
  }
  const removeRegion = (k: number) => {
    setRegions(rs => rs.filter((_, j) => j !== k)); setLabels(ls => ls.filter((_, j) => j !== k))
    if (refIdx >= k && refIdx > 0) setRefIdx(refIdx - 1)
  }
  const setLabel = (k: number, p: Partial<Label>) => setLabels(ls => ls.map((l, j) => (j === k ? { ...l, ...p } : l)))
  const exportPng = () => {
    const w = 1000, h = 420, scale = 2
    const c = document.createElement('canvas'); c.width = w * scale; c.height = h * scale
    const g = c.getContext('2d')!; g.scale(scale, scale)
    drawTo(g, w, h, LIGHT, true)
    c.toBlob(b => {
      if (!b) return
      const a = document.createElement('a'); a.href = URL.createObjectURL(b)
      a.download = `${filename.replace(/\.[^.]+$/, '')}.png`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 30000)
    })
  }

  const ref = withArea[refIdx]?.area
  const summary = summarise(spec, withArea, labels, refIdx, refH, peaks)
  const freq = spec.frequency
  const order = withArea.map((r, i) => ({ r, i })).sort((p, q) => (spec.reversed ? (q.r.a + q.r.b) - (p.r.a + p.r.b) : (p.r.a + p.r.b) - (q.r.a + q.r.b)))
  const fid = raw?.fid
  const hints: Record<Tool, string> = {
    zoom: 'Drag to zoom · scroll to zoom at the cursor · shift-scroll to pan · double-click to fit',
    integrate: 'Drag across a signal to integrate it',
    reference: 'Click a peak to set its chemical shift',
  }

  return (
    <>
      {expanded && <div className="spectrum-backdrop" onClick={() => setExpanded(false)} />}
      <div ref={wrap} className={`spectrum${expanded ? ' expanded' : ''}`} tabIndex={-1}
        onKeyDown={e => {
          if ((e.target as HTMLElement).tagName === 'INPUT' || (e.target as HTMLElement).tagName === 'SELECT') return
          if (e.key === 'z') setTool('zoom'); else if (e.key === 'i') setTool('integrate'); else if (e.key === 'r') setTool('reference'); else if (e.key === 'f') fit()
        }}>
        <div className="sp-head">
          <div style={{ minWidth: 0 }}>
            <div className="sp-title">{header(spec)}</div>
            <div className="faint" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {spec.title}{offset ? ` · referenced (${offset > 0 ? '+' : ''}${offset.toFixed(3)} ppm)` : ''}</div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn sm" onClick={() => setPicking(true)} title="Stack or overlay other spectra (e.g. crude vs pure, starting material vs product)">Compare…</button>
            <button className="btn sm" onClick={exportPng} title="Download a PNG for slides or SI">Export PNG</button>
            <button className="btn sm" onClick={() => setExpanded(x => !x)} aria-pressed={expanded}>{expanded ? 'Close' : 'Expand'}</button>
          </div>
        </div>

        <div className="sp-toolbar">
          <span className="sp-group"><span className="sp-cap">Mouse does</span>
          <div className="seg" role="group" aria-label="Mouse tool">
            {(['zoom', 'integrate', 'reference'] as Tool[]).filter(t => t !== 'reference' || spec.kind === 'nmr').map(t => (
              <button key={t} aria-pressed={tool === t} onClick={() => { setTool(t); setRefPick(null) }}
                title={{ zoom: 'Drag to zoom into a region (Z)', integrate: 'Drag across a signal to integrate it (I)', reference: 'Click a peak to set its chemical shift (R)' }[t]}>
                {{ zoom: 'Zoom', integrate: 'Integrate', reference: 'Reference' }[t]} <kbd>{t[0].toUpperCase()}</kbd></button>
            ))}
          </div></span>
          <span className="sp-group"><span className="sp-cap">Peak height</span>
          <div className="seg" role="group" aria-label="Peak height">
            <button onClick={() => setYScale(v => Math.max(1, v / 2))} title="Make peaks shorter" aria-label="Make peaks shorter">−</button>
            <button disabled style={{ cursor: 'default' }} title="Vertical magnification">×{yScale}</button>
            <button onClick={() => setYScale(v => Math.min(256, v * 2))} title="Make peaks taller (to see small signals)" aria-label="Make peaks taller">+</button>
          </div></span>
          <button className="btn sm" onClick={fit} title="Show the whole spectrum again (F, or double-click the plot)">Fit <kbd>F</kbd></button>
          {spec.kind === 'nmr' && <button className="btn sm" onClick={() => runAuto(spec, true, residual ? residual.found - residual.expected : 0)} title="Find signals, integrate and pick peaks automatically">Auto-analyse</button>}
          {isProton(spec) && <label className="row sp-peaks" style={{ gap: 6 }} title="Label each integrated signal with its shift, multiplicity and proton count">
            <input type="checkbox" checked={showSignals} onChange={e => setShowSignals(e.target.checked)} /> Signal labels</label>}
          {pred && <label className="row sp-peaks" style={{ gap: 6 }} title="Show shifts predicted for your product from NMRShiftDB2">
            <input type="checkbox" checked={showPred} onChange={e => setShowPred(e.target.checked)} /> <span className="swatch" style={{ background: PRED_COLOR }} />Predicted</label>}
          <label className="row sp-peaks" style={{ gap: 6 }} title="Label individual peak tops with their position">
            <input type="checkbox" checked={showPeaks} onChange={e => setShowPeaks(e.target.checked)} /> Peak positions
            {showPeaks && (is13C(spec) && spec.kind === 'nmr'
              ? <><span className="faint">S/N above</span><input type="range" min={3} max={40} step={1} value={threshold >= 1 ? threshold : 6} onChange={e => setThreshold(Number(e.target.value))} aria-label="Signal-to-noise threshold" />
                <span className="faint" style={{ minWidth: 20 }}>{threshold >= 1 ? threshold : 6}</span></>
              : <><input type="range" min={0.005} max={0.5} step={0.005} value={threshold < 1 ? threshold : 0.05} onChange={e => setThreshold(Number(e.target.value))} aria-label="Peak threshold" />
                <span className="faint" style={{ minWidth: 30 }}>{Math.round((threshold < 1 ? threshold : 0.05) * 100)}%</span></>)}
          </label>
          <span className="grow" />
          <span className="sp-readout" aria-live="off">
            {hover !== null ? <>
              <b>{spec.kind === 'nmr' ? 'δ ' : ''}{hover.toFixed(spec.kind === 'nmr' ? (is13C(spec) ? 2 : 3) : 1)}</b>
              {spec.kind === 'nmr' && freq ? <span className="faint"> · {(hover * freq).toFixed(1)} Hz</span> : null}
            </> : <span className="faint">{hints[tool]}</span>}
          </span>
        </div>

        {fid && (
          <div className="sp-fid">
            <span className="fld-cap" title={`Processed here from the raw FID: ${fid.points.toLocaleString()} complex points, zero-filled to ${fid.zeroFilled.toLocaleString()}, digital-filter delay ${fid.groupDelay.toFixed(1)} points removed, polynomial baseline.`}>Raw FID</span>
            <label className="row" style={{ gap: 4 }}>LB
              <select className="select" value={fid.lb} onChange={e => setLB(Number(e.target.value))} aria-label="Line broadening (Hz)">
                {[0, 0.1, 0.3, 0.5, 1, 2, 5].map(v => <option key={v} value={v}>{v} Hz</option>)}
              </select></label>
            <label className="row sp-phase" style={{ gap: 4 }}>Phase 0
              <input type="range" min={0} max={360} step={0.5} value={fid.ph0} onChange={e => setPhase(Number(e.target.value), fid.ph1)} aria-label="Zero-order phase (degrees)" />
              <span className="num">{fid.ph0.toFixed(0)}°</span></label>
            <label className="row sp-phase" style={{ gap: 4 }}>1
              <input type="range" min={Math.min(-720, Math.floor(fid.auto.ph1 - 360))} max={Math.max(720, Math.ceil(fid.auto.ph1 + 360))} step={1} value={fid.ph1} onChange={e => setPhase(fid.ph0, Number(e.target.value))} aria-label="First-order phase (degrees)" />
              <span className="num">{fid.ph1.toFixed(0)}°</span></label>
            <button className="btn ghost sm" onClick={() => setPhase(fid.auto.ph0, fid.auto.ph1)} title="Back to the automatic phase">Auto phase</button>
          </div>
        )}
        {(overlays.length > 0 || overlayErr) && (
          <div className="sp-legend">
            <span className="swatch" style={{ background: 'var(--ink)' }} />This spectrum
            {overlays.map((o, k) => <span key={o.id} className="sp-leg-item">
              <span className="swatch" style={{ background: OVERLAY_COLORS[k % OVERLAY_COLORS.length] }} />{o.label}
              <button className="btn ghost sm" style={{ padding: '0 4px' }} aria-label={`Remove ${o.label}`} onClick={() => setOverlays(os => os.filter(x => x.id !== o.id))}>×</button></span>)}
            {overlays.length > 0 && <span className="seg" role="group" aria-label="Layout">
              <button aria-pressed={overlayMode === 'stack'} onClick={() => setOverlayMode('stack')}>Stacked</button>
              <button aria-pressed={overlayMode === 'overlay'} onClick={() => setOverlayMode('overlay')}>Overlaid</button></span>}
            {overlayErr && <span className="error">{overlayErr}</span>}
          </div>
        )}
        {(residual || note) && (
          <div className="sp-notes">
            {residual && <div className="sp-note warn" role="status">
              <span>The {residual.label} residual is at <b>{residual.found.toFixed(3)}</b> ppm; it's usually {residual.expected.toFixed(2)}.</span>
              <button className="btn sm primary" onClick={() => {
                const delta = residual.expected - residual.found
                setOffset(o => o + delta); setRegions(rs => rs.map(r => ({ a: r.a + delta, b: r.b + delta }))); setView(v => (v ? [v[0] + delta, v[1] + delta] : v)); setResidual(null)
                setImpurities(is => is.map(i => ({ ...i, signals: i.signals.map(p => p + delta) })))
              }}>Reference to {residual.expected.toFixed(2)}</button>
              <button className="btn ghost sm" onClick={() => setResidual(null)}>Leave as is</button>
            </div>}
            {note && <div className="sp-note" role="status"><span>{note}</span>
              <button className="btn ghost sm" onClick={() => setNote(null)} aria-label="Dismiss">×</button></div>}
          </div>
        )}
        <div className="sp-plot">
          <canvas ref={canvas} className={`tool-${tool}`} onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp}
            onPointerLeave={() => setHover(null)} onDoubleClick={fit} role="img" aria-label={`${header(spec)}. ${summary}`} />
          {refPick && (
            <div className="sp-popover" style={{ left: Math.max(8, refPick.left), top: refPick.top }} role="dialog" aria-label="Set reference">
              <div className="faint" style={{ marginBottom: 6 }}>Peak at {refPick.x.toFixed(3)} ppm. Set it to:</div>
              <form className="row" style={{ gap: 6, flexWrap: 'nowrap' }} onSubmit={e => { e.preventDefault(); const v = Number(refPick.target); if (Number.isFinite(v)) applyReference(v) }}>
                <input className="input" autoFocus style={{ width: 90 }} value={refPick.target} onChange={e => setRefPick({ ...refPick, target: e.target.value })} aria-label="New chemical shift" />
                <span className="faint">ppm</span>
                <button className="btn sm primary">Apply</button>
                <button type="button" className="btn ghost sm" onClick={() => setRefPick(null)}>Cancel</button>
              </form>
              <div className="chips" style={{ marginTop: 8 }}>
                {referenceChoices(spec).map(m => <button key={m.label + m.ppm} className="chip" onClick={() => applyReference(m.ppm)}>{m.label} {m.ppm.toFixed(2)}</button>)}
              </div>
            </div>
          )}
        </div>

        <div className="sp-side">
          <h4 className="sp-h4">Signals{withArea.length ? <span className="faint"> · {withArea.length}</span> : null}</h4>
          {withArea.length > 0 ? (
            <div className="sp-table-wrap"><table className="sp-table">
              <thead><tr>
                <th>{isProton(spec) ? 'δ (ppm)' : 'Range'}</th>
                {isProton(spec) && <><th>Mult.</th><th>J (Hz)</th></>}
                <th title="Type a value to make that signal the reference">{isProton(spec) ? 'H' : 'Integral'}</th>
                {pred && nucleus === '1H' && <th title="Atoms (numbered as in the structure below) whose predicted shift falls in this signal">Assigned to</th>}
                <th><span className="vh">Remove</span></th>
              </tr></thead>
              <tbody>
                {order.map(({ r, i }) => {
                  const auto = isProton(spec) ? analyzeMultiplet(spec, r.a, r.b) : null
                  const shown = auto ? applyLabel(auto, labels[i]) : null
                  const val = ref ? r.area / ref * refH : 0
                  return (
                    <tr key={i} className={i === refIdx ? 'is-ref' : ''}>
                      <td>{shown ? (shown.mult === 'm' ? `${shown.hi.toFixed(2)}–${shown.lo.toFixed(2)}` : shown.shift.toFixed(2)) : `${fmtX(spec, Math.max(r.a, r.b))}–${fmtX(spec, Math.min(r.a, r.b))}`}</td>
                      {auto && <>
                        <td><select className="select" value={labels[i]?.mult || ''} onChange={e => setLabel(i, { mult: e.target.value })} aria-label="Multiplicity">
                          <option value="">{auto.mult} (auto)</option>{MULTIPLICITIES.map(m => <option key={m} value={m}>{m}</option>)}</select></td>
                        <td><input className="input sp-j" placeholder={auto.j.map(j => j.toFixed(1)).join(', ') || '—'} value={labels[i]?.j || ''}
                          onChange={e => setLabel(i, { j: e.target.value })} aria-label="Coupling constants in Hz" /></td>
                      </>}
                      <td><RefInput value={val} onCommit={v => { setRefIdx(i); setRefH(v) }} /></td>
                      {pred && nucleus === '1H' && (() => {
                        const a = assignment?.bySignal[i]
                        if (!a || !a.groups.length) return <td className="warn-text" title="No predicted proton falls here: an impurity, solvent or a different compound?">none</td>
                        const off = Math.abs(a.predH - val) > Math.max(0.6, 0.15 * a.predH)
                        return <td title={a.groups.map(gi => `${groupLabel(pred.groups[gi])}: predicted ${pred.groups[gi].shift!.toFixed(2)} ${predMult(pred.groups[gi])}, ${pred.groups[gi].nH}H`).join('\n')}>
                          {a.groups.map(gi => groupLabel(pred.groups[gi])).join(' + ')}
                          <span className={off ? ' warn-text' : ' faint'}> ({a.predH}H)</span></td>
                      })()}
                      <td><button className="btn ghost sm" onClick={() => removeRegion(i)} aria-label="Remove integral">×</button></td>
                    </tr>
                  )
                })}
              </tbody>
            </table></div>
          ) : <p className="faint" style={{ margin: '4px 0 0' }}>{spec.kind === 'nmr'
            ? 'Switch to Integrate and drag across each signal. Multiplicity and J are worked out automatically; type a proton count in any row to set the reference.'
            : 'Peaks above the threshold are listed below. Use Integrate to measure areas.'}</p>}
          {nucleus && <PredictionPanel pred={pred} assignment={assignment} nucleus={nucleus} status={predStatus} err={productSmiles ? predErr : ''}
            observed={observed} editable={!!onSave} confirmed={confirmed && pred && confirmed.smiles === pred.smiles ? confirmed : null}
            onOverride={(gi, v) => { setOverrides(o => ({ ...o, [gi]: v })); setConfirmed(null) }} onConfirm={confirm}
            onUnconfirm={() => setConfirmed(null)}
            onStartUpdate={startUpdate} observedLabel={k => {
              const o = observed[k]; if (!o) return ''
              return nucleus === '1H' ? (o.hi - o.lo > 0.05 && o.center === (o.lo + o.hi) / 2 ? `${o.hi.toFixed(2)}–${o.lo.toFixed(2)}` : o.center.toFixed(2)) : o.center.toFixed(1)
            }} />}
          <Checks spec={spec} structure={structure} regions={withArea} refIdx={refIdx} refH={refH} peaks={peaks}
            impurities={impurities} onRemoveImpurity={k => setImpurities(is => is.filter((_, j) => j !== k))} />
          <h4 className="sp-h4">Text for the record</h4>
          <div className="sp-summary">{summary}</div>
          <div className="row">
            <button className="btn sm" onClick={async () => { try { await navigator.clipboard.writeText(summary); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* blocked */ } }}>{copied ? 'Copied' : 'Copy text'}</button>
            {onInsert && <button className="btn sm primary" onClick={() => onInsert(summary)}>Use as characterisation data</button>}
            {offset !== 0 && <button className="btn ghost sm" onClick={resetReference}>Undo referencing</button>}
            {withArea.length > 0 && <button className="btn ghost sm" onClick={() => { setRegions([]); setLabels([]); setRefIdx(0) }}>Clear integrals</button>}
          </div>
        </div>
      </div>
      {picking && <OverlayPicker near={attachmentId} exclude={overlays.map(o => o.id)} onClose={() => setPicking(false)}
        onPick={l => { addOverlay(l.id, `${l.filename} (${l.eid})`); setPicking(false) }} />}
    </>
  )
}

function OverlayPicker({ near, exclude, onPick, onClose }: { near: number; exclude: number[]; onPick: (l: SpectrumListing) => void; onClose: () => void }) {
  const [q, setQ] = useState('')
  const [items, setItems] = useState<SpectrumListing[] | null>(null)
  useEffect(() => {
    const t = setTimeout(() => api.get<SpectrumListing[]>(`/api/spectra?near=${near}&q=${encodeURIComponent(q)}`).then(setItems).catch(() => setItems([])), 150)
    return () => clearTimeout(t)
  }, [q, near])
  return (
    <Modal title="Compare with another spectrum" onClose={onClose} footer={<button className="btn" onClick={onClose}>Close</button>}>
      <input className="input" style={{ width: '100%', marginBottom: 10 }} autoFocus placeholder="Filter by file name, experiment ID or title" value={q} onChange={e => setQ(e.target.value)} />
      <ul className="list" style={{ maxHeight: 360, overflowY: 'auto' }}>
        {(items || []).filter(i => !exclude.includes(i.id)).map(i => (
          <li key={i.id}><button className="list-row btn ghost" style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 12, alignItems: 'center', width: '100%', textAlign: 'left', borderRadius: 0 }} onClick={() => onPick(i)}>
            <span style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}><b style={{ fontWeight: 600 }}>{i.filename}</b><span className="faint" style={{ display: 'block' }}>{i.eid} · {i.title}</span></span>
            <span className="faint">{i.same_experiment ? 'this experiment' : day(i.created_at)}</span>
          </button></li>
        ))}
        {items && !items.filter(i => !exclude.includes(i.id)).length && <li className="empty">No other spectra yet. Drop more files onto any experiment to compare them here.</li>}
      </ul>
    </Modal>
  )
}

function RefInput({ value, onCommit }: { value: number; onCommit: (v: number) => void }) {
  const [text, setText] = useState(value.toFixed(2))
  const [focus, setFocus] = useState(false)
  useEffect(() => { if (!focus) setText(value.toFixed(2)) }, [value, focus])
  return (
    <input className="input sp-h" inputMode="decimal" value={text} aria-label="Relative integral"
      onFocus={() => setFocus(true)} onChange={e => setText(e.target.value)}
      onBlur={() => { setFocus(false); const v = Number(text); if (v > 0 && Math.abs(v - value) > 0.004) onCommit(v); else setText(value.toFixed(2)) }}
      onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }} />
  )
}


/** Quick sanity checks: does the spectrum fit the product's structure, and what residual solvent is there? */
function Checks({ spec, structure, regions, refIdx, refH, peaks, impurities, onRemoveImpurity }: {
  spec: Spectrum; structure: StructureInfo | null; regions: { a: number; b: number; area: number }[]; refIdx: number; refH: number
  peaks: { x: number }[]; impurities: NonNullable<SavedAnalysis['impurities']>; onRemoveImpurity: (k: number) => void
}) {
  const rows: { ok: boolean | null; text: string }[] = []
  if (structure && spec.kind === 'nmr') {
    if (isProton(spec) && regions.length) {
      const ref = regions[refIdx]?.area || regions[0].area
      const sum = regions.reduce((p, r) => p + r.area / ref * refH, 0)
      const exp = structure.total_h, ex = structure.exchangeable_h
      const okH = Math.abs(sum - exp) < 0.6 || (ex > 0 && sum < exp && exp - sum <= ex + 0.6)
      rows.push({ ok: okH, text: `${structure.formula}: ${exp} H expected; integrals add up to ${sum.toFixed(1)} H${!okH && ex ? ` (${ex} exchangeable OH/NH may not show)` : okH && ex && sum < exp - 0.6 ? ` (missing ${(exp - sum).toFixed(0)} H fits the ${ex} exchangeable OH/NH)` : ''}.` })
      // a product signal is at least ~1 H; well below that points to a second compound
      const minor = regions.filter(r => r.area / ref * refH < 0.7).map(r => ((r.a + r.b) / 2).toFixed(2))
      if (minor.length) rows.push({ ok: false, text: `Signal${minor.length > 1 ? 's' : ''} at ${minor.join(', ')} ppm integrate${minor.length > 1 ? '' : 's'} to well under 1 H: probably another compound (starting material, by-product or a solvent not in the list). Try Compare… with the starting material, and remove ${minor.length > 1 ? 'them' : 'it'} before using the text.` })
      const envs = structure.h_environments.length, n = regions.length - minor.length
      rows.push({
        ok: n === envs ? true : n < envs ? null : false,
        text: n === envs ? `${envs} proton environments expected, ${n} signals found.`
          : n < envs ? `${envs} proton environments expected, ${n} signals found: some probably overlap (look for multiplets with several protons).`
          : `${envs} proton environments expected, ${n} signals found: the extra signals may be an impurity, rotamers or diastereotopic protons.`,
      })
    } else if (is13C(spec)) {
      const solv = solventMarkers(spec).map(m => m.ppm)
      const kept: number[] = []
      for (const p of peaks) if (!solv.some(c => Math.abs(p.x - c) < 0.6) && !kept.some(q => Math.abs(q - p.x) < 0.05)) kept.push(p.x)
      const exp = structure.unique_carbons, n = kept.length
      rows.push({
        ok: n === exp ? true : n < exp ? null : false,
        text: n === exp ? `${exp} distinct carbons expected (${structure.total_carbons} in total, allowing for symmetry), ${n} peaks found.`
          : n < exp ? `${exp} distinct carbons expected, ${n} peaks found: quaternary carbons are weak, or two signals overlap. Try lowering the S/N threshold.`
          : `${exp} distinct carbons expected, ${n} peaks found: extra peaks may be impurities or noise. Try raising the S/N threshold.`,
      })
    }
  }
  if (!rows.length && !impurities.length) return null
  const totalWt = impurities.reduce((p, i) => p + (i.wtPct ?? 0), 0)
  return (
    <div className="sp-checks">
      <h4 className="sp-h4">Checks against the structure</h4>
      {rows.map((r, k) => <div key={k} className={`chk ${r.ok === true ? 'ok' : r.ok === false ? 'bad' : 'maybe'}`}>
        <span aria-hidden>{r.ok === true ? '✓' : r.ok === false ? '!' : '?'}</span>{r.text}</div>)}
      {impurities.length > 0 && <div className="chk maybe"><span aria-hidden>≈</span><span>
        Residual solvent, left out of the integrals:{' '}
        {impurities.map((i, k) => <span key={k} className="imp-chip">{i.short} {i.molPct != null ? `${i.molPct} mol%` : ''}{i.wtPct != null ? ` (${i.wtPct} wt%)` : ''}
          <button className="btn ghost sm" style={{ padding: '0 3px' }} aria-label={`Not ${i.name}`} title="Not this solvent: stop labelling it" onClick={() => onRemoveImpurity(k)}>×</button></span>)}
        {totalWt > 0 && <> · product ≈ {(100 - totalWt).toFixed(1)} wt% by ¹H NMR (excluding water)</>}
      </span></div>}
    </div>
  )
}
