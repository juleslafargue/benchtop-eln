// Minimal JCAMP-DX 4.24/5.x reader (XYDATA and XYPOINTS/PEAK TABLE blocks), plus CSV/TSV.
// Handles AFFN and the ASDF compressions (SQZ, DIF, DUP) used by most instrument exports.

export interface Spectrum {
  title: string
  x: Float64Array
  y: Float64Array
  xLabel: string
  yLabel: string
  kind: 'nmr' | 'ir' | 'uv' | 'ms' | 'other'
  reversed: boolean // draw high→low (NMR ppm, IR wavenumber)
  dips: boolean     // peaks point down (transmittance)
  nucleus?: string
  frequency?: number
  solvent?: string
  isStick: boolean  // peak table / MS: draw as sticks
  fid?: import('./fid').FidData // present when Benchtop processed a raw FID itself (phase can be adjusted)
  technique?: string  // set by a plugin file reader (e.g. "HPLC")
  conditions?: string
  reader?: string     // which reader opened it
  /** Things the person should know before trusting the numbers (a raw FID shorter than recorded, which columns of a
   *  table were drawn): the viewer shows them above the plot. */
  warnings?: string[]
  /** Only for analyses saved before 0.36.5 on a file that doesn't give the spectrometer frequency: they were worked
   *  out as if it were 400 MHz, and keep showing what they showed (with a note saying so) until re-analysed. Never
   *  shown as the spectrum's frequency. */
  assumedFrequency?: number
}

// ---------------------------------------------------------------- plugin file readers

/** An instrument-file reader from a plugin (see docs/PLUGINS.md); the server validates the definition. */
export interface FileReader {
  plugin: string; id: string; name: string; technique: string; conditions: string; kind: Spectrum['kind']
  extensions: string[]; detect: string; filename: string; delimiter: string; decimal: '.' | ','; data_after: string
  header: boolean; x: number | string; y: number | string; x_label: string; y_label: string
  x_scale: number; y_scale: number; reversed: boolean; dips: boolean
}
let readersP: Promise<FileReader[]> | null = null
export const resetFileReaders = () => { readersP = null }
export function fileReaders(): Promise<FileReader[]> {
  if (!readersP) {
    let token: string | null = null
    try { token = localStorage.getItem('benchtop.token') } catch { /* private mode */ }
    readersP = fetch('/api/plugins/readers', { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .then(r => (r.ok ? r.json() : [])).catch(() => [])
  }
  return readersP
}
/** Python-style patterns from plugin files: a leading (?i) becomes JS's i flag; matching is case-insensitive anyway. */
const rx = (p: string) => { try { return new RegExp(p.replace(/^\(\?i\)/, ''), 'im') } catch { return null } }

export function matchReader(readers: FileReader[], filename: string, head: string): FileReader | null {
  const ext = (filename.match(/\.[^.]+$/)?.[0] || '').toLowerCase()
  return readers.find(r => (!r.extensions.length || r.extensions.includes(ext))
    && (!r.filename || !!rx(r.filename)?.test(filename))
    && (!r.detect || !!rx(r.detect)?.test(head))) || null
}

export function parseWithReader(raw: string, filename: string, r: FileReader): Spectrum {
  let lines = normaliseText(raw).split('\n')
  if (r.data_after) {
    const re = rx(r.data_after)
    const i = re ? lines.findIndex(l => re.test(l)) : -1
    if (i < 0) throw new Error(`${r.name}: couldn't find where the data starts in this file`)
    lines = lines.slice(r.header || typeof r.x === 'string' || typeof r.y === 'string' ? i : i + 1)
  }
  lines = lines.filter(l => l.trim())
  const sample = lines.slice(0, 5).join('\n')
  const delim = r.delimiter !== 'auto' ? r.delimiter
    : sample.includes('\t') ? '\t' : sample.includes(';') ? ';' : r.decimal === '.' && sample.includes(',') ? ',' : 'whitespace'
  const split = (l: string) => (delim === 'whitespace' ? l.trim().split(/\s+/) : l.split(delim)).map(c => c.trim().replace(/^"(.*)"$/, '$1'))
  const num = (c: string | undefined) => Number((c ?? '').replace(r.decimal === ',' ? /,/g : /(?!)/, '.'))
  const needHeader = r.header || typeof r.x === 'string' || typeof r.y === 'string'
  let start = 0, xi = typeof r.x === 'number' ? r.x - 1 : -1, yi = typeof r.y === 'number' ? r.y - 1 : -1
  if (needHeader) {
    const head = split(lines[0] || '').map(h => h.toLowerCase())
    const find = (want: number | string, fallback: number) => typeof want === 'number' ? want - 1
      : Math.max(head.findIndex(h => h === String(want).toLowerCase()), head.findIndex(h => h.startsWith(String(want).toLowerCase())), fallback)
    xi = find(r.x, -1); yi = find(r.y, -1)
    if (xi < 0 || yi < 0) throw new Error(`${r.name}: column “${xi < 0 ? r.x : r.y}” isn't in this file's header`)
    start = 1
  }
  const xs: number[] = [], ys: number[] = []
  for (const l of lines.slice(start)) {
    const c = split(l)
    const a = num(c[xi]), b = num(c[yi])
    if (Number.isFinite(a) && Number.isFinite(b)) { xs.push(a * r.x_scale); ys.push(b * r.y_scale) }
  }
  if (xs.length < 2) throw new Error(`${r.name}: no numeric data found in columns ${xi + 1} and ${yi + 1}`)
  return finish({
    title: filename.replace(/\.[^.]+$/, ''), xLabel: r.x_label, yLabel: r.y_label, kind: r.kind, reversed: r.reversed, dips: r.dips,
    isStick: r.kind === 'ms', technique: r.technique, conditions: r.conditions, reader: r.name,
  }, xs, ys)
}
export interface FidOptions { lb?: number; ph0?: number; ph1?: number; groupDelay?: number; reverse?: boolean }

const SQZ: Record<string, number> = { '@': 0, A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8, I: 9, a: -1, b: -2, c: -3, d: -4, e: -5, f: -6, g: -7, h: -8, i: -9 }
const DIF: Record<string, number> = { '%': 0, J: 1, K: 2, L: 3, M: 4, N: 5, O: 6, P: 7, Q: 8, R: 9, j: -1, k: -2, l: -3, m: -4, n: -5, o: -6, p: -7, q: -8, r: -9 }
const DUP: Record<string, number> = { S: 1, T: 2, U: 3, V: 4, W: 5, X: 6, Y: 7, Z: 8, s: 9 }

// ---------------------------------------------------------------- limits and plain messages

/** The most points Benchtop opens in the browser (2^23: a 1D spectrum is rarely over a million). Bigger files, and a
 *  small file that says it unpacks to billions of points (a DUP count of 99999999), get a plain message instead of
 *  freezing the page and then failing with "Invalid array length". */
export const MAX_POINTS = 8_388_608
const n0 = (n: number) => Math.round(n).toLocaleString('en-GB')
const tooMany = (n?: number) => new Error(`This file has ${n ? `${n0(n)} points` : `more than ${n0(MAX_POINTS)} points`}: Benchtop opens spectra of up to ${n0(MAX_POINTS)} points. Download it to open it in the instrument's software.`)
const damaged = (what: string) => new Error(`This file looks damaged: ${what} Copy or export it from the instrument again.`)
/** What a word in the data looks like, for a message: odd characters shown as code points, and kept short. */
const shown = (w: string) => {
  const t = w.length > 20 ? `${w.slice(0, 20)}…` : w
  return t.replace(/[^\x21-\x7e]/g, c => `U+${c.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}`)
}
const notANumber = (w: string) => damaged(`its data has “${shown(w)}”, which isn't a number.`)

type Tok = { kind: 'abs' | 'dif' | 'dup'; text: string }

function tokenize(line: string): Tok[] {
  const toks: Tok[] = []
  let cur: Tok | null = null
  const push = () => { if (cur) toks.push(cur); cur = null }
  for (const ch of line) {
    if (ch === ' ' || ch === ',' || ch === '\t' || ch === ';') { push(); continue }
    if ((ch >= '0' && ch <= '9') || ch === '.') {
      if (!cur) cur = { kind: 'abs', text: '' }
      cur.text += ch
    } else if (ch === '+' || ch === '-') {
      push(); cur = { kind: 'abs', text: ch === '-' ? '-' : '' }
    } else if (ch in SQZ) {
      push(); const v = SQZ[ch]; cur = { kind: 'abs', text: (v < 0 ? '-' : '') + Math.abs(v) }
    } else if (ch in DIF) {
      push(); const v = DIF[ch]; cur = { kind: 'dif', text: (v < 0 ? '-' : '') + Math.abs(v) }
    } else if (ch in DUP) {
      push(); cur = { kind: 'dup', text: String(DUP[ch]) }
    } else if (ch === '?') {
      push(); toks.push({ kind: 'abs', text: '?' })
    }
  }
  push()
  return toks
}

/** Decimal places a number was written with ("12.250" → 3, "4000" → 0, "1.5E+03" → -2): how far an X check
 *  written that way can be from the exact value. */
function decimalsOf(t: string) {
  const m = /^[+-]?\d*(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(t)
  if (!m) return 0
  return (m[1]?.length || 0) - Number(m[2] || 0)
}

const AFFN = /^[\s\d.,;+\-eE]+$/
// characters that can be in a JCAMP-DX data line: digits, signs, separators, the SQZ/DIF/DUP letters, "?" (missing)
const NOT_ASDF = /[^\s\d.,;+\-@%?A-Za-s]/

/** A plain AFFN line ("8191 12 15 -3"), read straight into `ys` with its X check: false (nothing read) if it isn't
 *  one, or has a word that isn't a plain number, which `parseLine` then reads or reports. */
function affnLine(line: string, ys: number[], checks: XCheck[]): boolean {
  if (!AFFN.test(line)) return false
  const start = ys.length
  let x = NaN, xDec = 0, k = 0
  for (let i = 0, n = line.length; i < n;) {
    let c = line.charCodeAt(i)
    if (c === 32 || c === 9 || c === 44 || c === 59) { i++; continue }  // space, tab, comma, semicolon
    const a = i
    while (i < n && (c = line.charCodeAt(i)) !== 32 && c !== 9 && c !== 44 && c !== 59) i++
    const t = line.slice(a, i), v = Number(t)
    if (!Number.isFinite(v)) { ys.length = start; return false }
    if (k++ === 0) { x = v; xDec = decimalsOf(t) } else ys.push(v)
    if (ys.length > MAX_POINTS) throw tooMany()
  }
  checks.push({ at: start, x, dec: xDec })
  return true
}

/** One XYDATA line: its X value (and how many decimals it was written with), its Y values, and whether it ends in DIF
 *  form (then the next line starts with a check value). `room`: how many more points may be read. */
function parseLine(line: string, room: number): { x: number; xDec: number; ys: number[]; endsDif: boolean } {
  if (AFFN.test(line)) { // plain AFFN, possibly with exponents
    const parts: string[] = [], re = /[^\s,;]+/g
    for (let m: RegExpExecArray | null; (m = re.exec(line));) { parts.push(m[0]); if (parts.length > room + 1) throw tooMany() }
    const nums = parts.map(Number)
    for (let k = 0; k < nums.length; k++) if (!Number.isFinite(nums[k])) throw notANumber(parts[k])
    return { x: nums[0], xDec: decimalsOf(parts[0]), ys: nums.slice(1), endsDif: false }
  }
  // words that would otherwise be read as SQZ/DIF letters ("NaN" is DIF 5, SQZ -1, DIF 5)
  for (const w of line.split(/[\s,;]+/)) if (/^[+-]?(nan|inf|infinity)$/i.test(w)) throw notANumber(w)
  const odd = NOT_ASDF.exec(line)
  if (odd) throw damaged(`its data has a character that doesn't belong in JCAMP-DX data (“${shown(odd[0])}”).`)
  const toks = tokenize(line)
  if (!toks.length) return { x: NaN, xDec: 0, ys: [], endsDif: false }
  const num = (t: Tok) => {
    if (t.text === '?') return NaN
    const v = Number(t.text)
    if (!Number.isFinite(v) || t.text === '' || t.text === '-') throw notANumber(t.text || line)
    return v
  }
  const x = num(toks[0])
  const ys: number[] = []
  let last: Tok | null = null
  let endsDif = false
  for (const t of toks.slice(1)) {
    if (t.kind === 'abs') { ys.push(num(t)); last = t; endsDif = false }
    else if (t.kind === 'dif') { ys.push((ys.length ? ys[ys.length - 1] : 0) + num(t)); last = t; endsDif = true }
    else if (t.kind === 'dup' && last) {
      const count = num(t)
      if (count - 1 > room - ys.length) throw tooMany()
      for (let k = 1; k < count; k++) {
        if (last.kind === 'dif') ys.push(ys[ys.length - 1] + num(last))
        else ys.push(num(last))
      }
    }
  }
  return { x, xDec: decimalsOf(toks[0].text), ys, endsDif }
}

function labels(raw: string): Map<string, string> {
  const m = new Map<string, string>()
  const re = /^##([^=\n]+)=(.*)$/gm
  let r: RegExpExecArray | null
  while ((r = re.exec(raw))) {
    const key = r[1].replace(/[\s\-/_]/g, '').toUpperCase()
    if (!m.has(key)) m.set(key, r[2].trim())
  }
  return m
}

/** Where a line of the table starts: the index of its first Y value, and the X value written there (the "X check"). */
interface XCheck { at: number; x: number; dec: number }

/** The Y values of one XYDATA (or NTUPLES page) table, from the line after its header up to the next ##label,
 * and the X value each line starts with. A line in DIF form is followed by a line starting with the same Y value
 * again (the "Y check"): if they differ, a line is missing or changed, and the file is refused. */
function readXY(lines: string[], from: number): { ys: number[]; checks: XCheck[] } {
  const ys: number[] = [], checks: XCheck[] = []
  let prevDif = false, row = 0
  for (let i = from; i < lines.length && !lines[i].startsWith('##'); i++) {
    const line = lines[i].trim()
    if (!line) continue
    row++
    if (!prevDif && affnLine(line, ys, checks)) continue  // the usual plain line, read straight into ys (big files)
    const r = parseLine(line, MAX_POINTS - ys.length)
    let v = r.ys, at = ys.length
    if (prevDif && v.length && ys.length) {
      const check = v[0], last = ys[ys.length - 1]
      if (Number.isFinite(check) && Number.isFinite(last) && check !== last)
        throw damaged(`line ${n0(row)} of its data doesn't start where the line before ended (JCAMP-DX's own check), so a line is missing or has been changed.`)
      v = v.slice(1); at = ys.length - 1
    }
    checks.push({ at, x: r.x, dec: r.xDec })
    for (const y of v) ys.push(Number.isFinite(y) ? y : (ys.length ? ys[ys.length - 1] : NaN)) // "?": hold the last value
    if (ys.length > MAX_POINTS) throw tooMany()
    prevDif = r.endsDif
  }
  return { ys, checks }
}

/** The number of points a file says it has, when it says (a positive whole number). */
const declaredCount = (v: string | undefined) => {
  const n = Number((v || '').replace(/[^\d.eE+-]/g, ''))
  return v !== undefined && Number.isInteger(n) && n > 0 ? n : undefined
}
/** Whether a file holds the number of points it says. One more or less is allowed: some write one point too many in
 *  NPOINTS (Varian's 16,385 for 16,384), and one point is at most a point's width on the axis. */
const countOK = (declared: number | undefined, n: number) => !declared || Math.abs(n - declared) <= 1
const countMismatch = (declared: number, n: number) => n < declared
  ? new Error(`This file looks cut off: it says it has ${n0(declared)} points but only ${n0(n)} are there, so its ppm scale can't be trusted. Copy or export it from the instrument again.`)
  : new Error(`This file says it has ${n0(declared)} points but holds ${n0(n)}, so its scale can't be trusted. Export it from the instrument again.`)

/** Each line's X check against the axis the header gives (FIRSTX, LASTX and the number of points): a line missing in
 *  the middle, or a header that doesn't belong to the data, shows up as X values that don't fit. Each is allowed to be
 *  off by its rounding (the decimals it was written with) and a point and a half. Some instruments write the X checks
 *  another way (Varian and Bruker as point numbers, Oxford Instruments without dividing by the factor): then they are
 *  only checked to go up (or down) evenly with the points, which still shows a line missing. */
function checkX(checks: XCheck[], firstX: number, step: number, factor: number) {
  const pts = checks.filter(c => Number.isFinite(c.x))
  if (pts.length < 2) return
  // how far off a check may be, in points: writers differ by a point or two over a whole spectrum (Oxford Instruments'
  // header ends two points after their last X), but a missing line is a whole line of points (5 to 80)
  const slack = 3
  const round = (c: XCheck, f: number) => 0.5 * 10 ** -c.dec * Math.abs(f)
  const off = (c: XCheck, f: number) => Math.abs(c.x * f - (firstX + c.at * step))
  const tol = (c: XCheck, f: number) => Math.max(slack * Math.abs(step), round(c, f)) * 1.001 + 1e-9 * Math.abs(c.x * f)
  const factors = factor === 1 ? [1] : [factor, 1]
  if (factors.some(f => pts.every(c => off(c, f) <= tol(c, f)))) return
  const a = pts[0], b = pts[pts.length - 1]
  const missing = 'its data skip or repeat some points (the X value at the start of each line doesn\'t follow on from the line before), so a line may be missing and the scale can\'t be trusted.'
  // written in the header's units (they start where the header does and go up at about its rate), but they don't fit
  for (const f of factors) {
    const rate = b.at === a.at ? 1 : (b.x - a.x) * f / (b.at - a.at) / step
    const bad = pts.find(c => off(c, f) > tol(c, f))
    if (bad && off(a, f) <= tol(a, f) && rate > 0.5 && rate < 2) {
      throw damaged(`the X values written in its data (${+(bad.x * f).toPrecision(6)} where the header puts ${+(firstX + bad.at * step).toPrecision(6)}) don't fit the axis its header gives, so a line may be missing and the scale can't be trusted.`)
    }
  }
  if (b.at === a.at) return
  const slope = (b.x - a.x) / (b.at - a.at)
  for (const c of pts) {
    const d = Math.abs(c.x - (a.x + slope * (c.at - a.at)))
    if (d > (slack * Math.abs(slope) + round(c, 1)) * 1.001 + 1e-9 * Math.abs(c.x)) throw damaged(missing)
  }
}

/** The data pages of an NTUPLES block, by symbol (R, I…), already multiplied by their factors (for tests and checks). */
export function ntuplesPages(raw: string): Record<string, number[]> {
  const block = spectrumBlock(normaliseText(raw).replace(/\$\$.*$/gm, ''))
  const L = labels(block)
  const syms = listOf(L.get('SYMBOL')).map(s => s.toUpperCase())
  const factor = listOf(L.get('FACTOR')).map(Number)
  const lines = block.split('\n')
  const out: Record<string, number[]> = {}
  lines.forEach((l, at) => {
    const m = l.match(/^##DATA\s*TABLE\s*=\s*\(\s*X\+\+\(\s*(\w+)\.\./i)
    if (m) { const f = factor[syms.indexOf(m[1].toUpperCase())]; out[m[1].toUpperCase()] = readXY(lines, at + 1).ys.map(v => v * (Number.isFinite(f) ? f : 1)) }
  })
  return out
}

/** Hz to ppm. ##.SHIFT REFERENCE (method, compound, data point, shift) says which point has which shift: Bruker's
 * NTUPLES export counts Hz from the right edge, so dividing by the frequency alone would be off. */
function hzToPpm(xs: number[], L: Map<string, string>, freq: number): number[] {
  const ref = (L.get('.SHIFTREFERENCE') || '').replace(/[()]/g, '').split(',').map(s => s.trim())
  const point = Number(ref[2]), shift = Number(ref[3])
  if (ref.length >= 4 && ref[2] !== '' && Number.isFinite(point) && Number.isFinite(shift) && xs.length) {
    const k = Math.min(xs.length - 1, Math.max(0, Math.round(point) - 1)) // data points count from 1
    const base = shift - xs[k] / freq
    return xs.map(v => v / freq + base)
  }
  return xs.map(v => v / freq)
}

/** A JCAMP-DX file can hold several blocks (MestReNova: structure, peak list, spectrum): use the spectrum's. */
function spectrumBlock(text: string): string {
  const starts = [0]
  for (let i = text.indexOf('\n##TITLE'); i >= 0; i = text.indexOf('\n##TITLE', i + 1)) if (/^\s*=/.test(text.slice(i + 8, i + 40))) starts.push(i + 1)
  if (starts.length === 1) return text
  const blocks = starts.map((a, k) => text.slice(a, starts[k + 1] ?? text.length)).filter(b => /^##/m.test(b))
  const has = (b: string, re: RegExp) => re.test(b)
  const type = (b: string) => ((b.match(/^##DATA\s*TYPE\s*=\s*(.*)$/mi) || [])[1] || '').toUpperCase()
  const rank = (b: string) => {
    if (has(b, /^##(XYDATA|NTUPLES)\s*=/mi)) return /SPECTRUM|FID/.test(type(b)) ? 4 : 3
    if (has(b, /^##(XYPOINTS|PEAK\s*TABLE)\s*=/mi)) return /ASSIGNMENT/.test(type(b)) ? 1 : 2
    return 0
  }
  let best = '', score = 0
  for (const b of blocks) { const r = rank(b); if (r > score) { score = r; best = b } }
  return best || text
}

const listOf = (v: string | undefined) => (v || '').split(',').map(s => s.trim())

/** NTUPLES: a processed spectrum stored as real and imaginary pages (TopSpin's JCAMP export), or a complex FID
 * (benchtop spectrometers: Nanalysis, Oxford Instruments, Q Magnetics). The real page is the spectrum; an FID is
 * processed like a raw Bruker one. */
function parseNtuples(block: string, L: Map<string, string>, fidOpts: FidOptions): Spectrum {
  const syms = listOf(L.get('SYMBOL')).map(s => s.toUpperCase())
  const units = listOf(L.get('UNITS')).map(s => s.toUpperCase())
  const nums = (k: string) => listOf(L.get(k)).map(s => (s ? Number(s.replace(/[^\d.eE+-]/g, '')) : NaN))
  const first = nums('FIRST'), last = nums('LAST'), factor = nums('FACTOR')
  const xi = syms.indexOf('X'), ri = syms.indexOf('R'), ii = syms.indexOf('I')
  // 2D (COSY, HSQC…) and series (pseudo-2D T1): two axes, F1/F2 or T1/T2, or an "nD" data type
  if (syms.some(v => /^(F|T)\d$/.test(v)) || /\bn\s*D\b|NDNMR/i.test(`${L.get('NTUPLES') || ''} ${L.get('DATA TYPE') || ''}`)) throw new Error(TWO_D)
  if (xi < 0 || ri < 0) throw new Error('This JCAMP-DX file holds data Benchtop doesn\'t read (NTUPLES without a real spectrum or FID).')
  const dims = listOf(L.get('VARDIM')).map(declaredCount)
  for (const d of dims) if (d && d > MAX_POINTS) throw tooMany(d)
  const lines = block.split('\n')
  const page = (sym: string) => {
    const at = lines.findIndex(l => new RegExp(`^##DATA\\s*TABLE\\s*=\\s*\\(\\s*X\\+\\+\\(\\s*${sym}\\.\\.${sym}\\s*\\)\\s*\\)`, 'i').test(l))
    if (at < 0) return null
    const f = Number.isFinite(factor[syms.indexOf(sym)]) ? factor[syms.indexOf(sym)] : 1
    const t = readXY(lines, at + 1)
    const want = dims[syms.indexOf(sym)]
    if (want && !countOK(want, t.ys.length)) throw countMismatch(want, t.ys.length)
    return { ys: t.ys.map(v => v * f), checks: t.checks }
  }
  if (lines.filter(l => /^##PAGE\s*=/i.test(l)).length > 2) throw new Error('This is a 2D experiment (or a series). Benchtop shows 1D spectra; this one is kept with the record as a file.')
  const rp = page('R')
  if (!rp || !rp.ys.length) throw new Error('This JCAMP-DX file has no real data page that Benchtop can read.')
  const re = rp.ys
  const freq = Number((L.get('.OBSERVEFREQUENCY') || '').replace(/[^\d.eE+-]/g, '')) || undefined
  const nucleus = L.get('.OBSERVENUCLEUS')?.replace(/[\^\s]/g, '')
  const solvent = L.get('.SOLVENTNAME')
  const title = L.get('TITLE') || 'Spectrum'
  const n = re.length
  const x0 = first[xi], x1 = last[xi]
  const isFid = /FID/.test((L.get('DATATYPE') || '').toUpperCase()) || /SECOND|TIME/.test(units[xi] || '')
  if (isFid) {
    const ip = ii >= 0 ? page('I') : null
    const im = ip?.ys
    if (!im || im.length < n) throw new Error('This JCAMP-DX FID has no imaginary page, so it can\'t be processed.')
    if (!(Number.isFinite(x0) && Number.isFinite(x1) && x1 > x0)) throw new Error('This JCAMP-DX FID doesn\'t say how long it was acquired for, so it can\'t be processed.')
    checkX(rp.checks, x0, (x1 - x0) / (n - 1), Number.isFinite(factor[xi]) ? factor[xi] : 1)
    const sw = (n - 1) / (x1 - x0)
    const num = (k: string) => Number((L.get(k) || '').replace(/[^\d.eE+-]/g, ''))
    const sf = num('$SF') || freq
    if (!sf) throw new Error('This JCAMP-DX FID doesn\'t give the spectrometer frequency, so it can\'t be processed.')
    // the ppm at the spectrum's centre: from the transmitter and reference frequencies, a referenced point, or the left edge
    const ref = listOf((L.get('.SHIFTREFERENCE') || '').replace(/[()]/g, ''))
    const sfo1 = num('$SFO1')
    const centre = sfo1 && num('$SF') ? (sfo1 - num('$SF')) / num('$SF') * 1e6
      : ref.length >= 4 && ref[2] !== '' && Number.isFinite(Number(ref[2])) && Number.isFinite(Number(ref[3]))
        ? Number(ref[3]) + (Number(ref[2]) - 1) * sw / n / sf - sw / 2 / sf
        : Number.isFinite(num('$OFFSET')) && L.has('$OFFSET') ? num('$OFFSET') - sw / 2 / sf : 0
    const reverse = fidOpts.reverse ?? /^\s*y/i.test(L.get('$REVERSE') || '')  // Oxford Instruments: "$REVERSE= yes"
    // the time shift to apply: Bruker's JCAMP export states its digital filter's delay ($GRPDLY, or the firmware
    // table); Oxford Instruments gives TDoff (negative: points missed at the start, rebuilt by back-prediction; it
    // counts real and imaginary values, so -16 is 8 complex points: with 16 the baseline rolls and the lines need a
    // large extra phase correction, with 8 it is flat); otherwise the FID starts at time zero (where it peaks is just
    // signals beating, not a delay)
    const bruker = L.has('$GRPDLY') || L.has('$DSPFVS')
    const groupDelay = fidOpts.groupDelay ?? (bruker ? brukerGroupDelay(Object.fromEntries(L))
      : L.has('$TDOFF') && Number.isFinite(num('$TDOFF')) ? num('$TDOFF') / 2 : 0)
    const f = processComplexFid(re, im, { sw, sf, centre, nucleus: nucleus || '1H', reverse, groupDelay }, fidOpts)
    return checked({ title, xLabel: 'δ (ppm)', yLabel: 'Intensity', kind: 'nmr', reversed: true, dips: false, nucleus, frequency: sf, solvent,
      isStick: false, x: f.x, y: f.y, fid: f })
  }
  if (!Number.isFinite(x0) || !Number.isFinite(x1)) throw new Error('This JCAMP-DX file doesn\'t give its x range (##FIRST and ##LAST) as numbers, so its scale is unknown.')
  if (n > 1 && x0 === x1) throw new Error('This JCAMP-DX file gives the same value for the start and end of its x axis, so its scale is unknown.')
  const step = n > 1 ? (x1 - x0) / (n - 1) : 0
  checkX(rp.checks, x0, step, Number.isFinite(factor[xi]) ? factor[xi] : 1)
  let xs = Array.from({ length: n }, (_, i) => x0 + (n > 1 ? i * (x1 - x0) / (n - 1) : 0))  // as 0.36.4 worked it out, to the last digit
  if (units[xi] === 'HZ' && freq) xs = hzToPpm(xs, L, freq)
  return finish({ title, xLabel: 'δ (ppm)', yLabel: 'Intensity', kind: 'nmr', reversed: true, dips: false, nucleus, frequency: freq, solvent,
    isStick: false }, xs, re)
}

/** Text as one kind of line end: old Mac files end lines with CR alone (0.36.4: "No ##XYDATA"). */
const normaliseText = (raw: string) => raw.replace(/^﻿/, '').replace(/\r\n?/g, '\n')

/** (X, Y) pairs (or triples, for a peak table with widths) from the lines of an XYPOINTS, PEAK TABLE or
 * XYDATA=(XY..XY) block. */
function readPairs(lines: string[], from: number, group: number): { xs: number[]; ys: number[] } {
  const xs: number[] = [], ys: number[] = []
  const nums: number[] = []
  for (let i = from; i < lines.length && !lines[i].startsWith('##'); i++) {
    for (const t of lines[i].split(/[\s,;]+/)) {
      if (!t) continue
      const v = t === '?' ? NaN : Number(t)
      if (t !== '?' && !Number.isFinite(v)) throw notANumber(t)
      nums.push(v)
    }
    if (nums.length > group * MAX_POINTS) throw tooMany()
  }
  for (let i = 0; i + 1 < nums.length; i += group) {
    if (Number.isFinite(nums[i]) && Number.isFinite(nums[i + 1])) { xs.push(nums[i]); ys.push(nums[i + 1]) }
  }
  return { xs, ys }
}

export function parseJcamp(raw: string, fidOpts: FidOptions = {}): Spectrum {
  const text = spectrumBlock(normaliseText(raw).replace(/\$\$.*$/gm, '')) // strip comments
  const L = labels(text)
  if (L.has('NTUPLES')) return parseNtuples(text, L, fidOpts)
  const num = (k: string) => { if (!L.has(k)) return undefined; const t = L.get(k)!.replace(/[^\d.eE+-]/g, ''); return t ? Number(t) : NaN }
  const factorOf = (k: string) => { const v = num(k); return v === undefined || !Number.isFinite(v) || v === 0 ? undefined : v }
  const xFactor = factorOf('XFACTOR') ?? 1
  const yFactor = num('YFACTOR') ?? 1
  if (!Number.isFinite(yFactor)) throw new Error('This JCAMP-DX file\'s ##YFACTOR isn\'t a number, so its intensities can\'t be worked out.')
  const xUnits = (L.get('XUNITS') || '').toUpperCase()
  const yUnits = (L.get('YUNITS') || '').toUpperCase()
  const dataType = (L.get('DATATYPE') || '').toUpperCase()
  const freq = num('.OBSERVEFREQUENCY')
  const lines = text.split('\n')

  let xs: ArrayLike<number> = []
  let ys: ArrayLike<number> = []
  let isStick = false
  const xyIdx = lines.findIndex(l => /^##XYDATA\s*=/i.test(l))
  const ptIdx = lines.findIndex(l => /^##(XYPOINTS|PEAK\s*TABLE)\s*=/i.test(l))
  const declared = declaredCount(L.get('NPOINTS'))
  if (declared && declared > MAX_POINTS) throw tooMany(declared)
  const pairForm = xyIdx >= 0 && /\(\s*XYW?\s*\.\.\s*XYW?\s*\)/i.test(lines[xyIdx])
  if (xyIdx >= 0 && !pairForm) {
    const t = readXY(lines, xyIdx + 1)
    const n = t.ys.length
    if (!n) throw new Error('This JCAMP-DX file has no data points in its ##XYDATA block.')
    if (declared && !countOK(declared, n)) throw countMismatch(declared, n)
    const yv = new Float64Array(n)
    for (let i = 0; i < n; i++) yv[i] = t.ys[i] * yFactor
    // the axis from the header (FIRSTX, LASTX, the number of points), checked against the X value each line starts
    // with; without FIRSTX/LASTX, from those X values themselves (0.36.4: the last line's X was taken for the last point's)
    let firstX = num('FIRSTX'), lastX = num('LASTX')
    if (firstX === undefined || lastX === undefined) {
      const c = t.checks.filter(k => Number.isFinite(k.x)), a = c[0], b = c[c.length - 1]
      if (!a || !b || b.at === a.at) throw new Error('This JCAMP-DX file doesn\'t give its x range (##FIRSTX and ##LASTX), so its scale is unknown.')
      const step = (b.x - a.x) * xFactor / (b.at - a.at)
      firstX = firstX ?? a.x * xFactor - a.at * step
      lastX = lastX ?? firstX + (n - 1) * step
    }
    if (!Number.isFinite(firstX) || !Number.isFinite(lastX)) throw new Error('This JCAMP-DX file doesn\'t give its x range (##FIRSTX and ##LASTX) as numbers, so its scale is unknown.')
    if (n > 1 && firstX === lastX) throw new Error('This JCAMP-DX file gives the same value for ##FIRSTX and ##LASTX, so its scale is unknown.')
    const step = n > 1 ? (lastX - firstX) / (n - 1) : 0
    checkX(t.checks, firstX, step, xFactor)
    const xv = new Float64Array(n)
    for (let i = 0; i < n; i++) xv[i] = firstX + i * step
    xs = xv; ys = yv
  } else if (xyIdx >= 0 || ptIdx >= 0) {
    const at = xyIdx >= 0 ? xyIdx : ptIdx
    isStick = xyIdx < 0 && (/PEAK/i.test(lines[ptIdx]) || dataType.includes('MASS'))
    const p = readPairs(lines, at + 1, /XYW/i.test(lines[at]) ? 3 : 2)
    xs = p.xs.map(v => v * xFactor); ys = p.ys.map(v => v * yFactor)
    const px = xs
    if (!xs.length) throw new Error('This JCAMP-DX file has no data points that are numbers.')
    if (declared && !countOK(declared, xs.length) && !isStick) throw countMismatch(declared, xs.length)
    // a "peak table" of thousands of evenly spaced points is a continuous spectrum (simulators write them this way)
    if (isStick && !dataType.includes('MASS') && xs.length > 2000) {
      const d = px[1] - px[0]
      let even = d !== 0
      for (let i = 1; even && i < px.length; i++) if (Math.abs(px[i] - px[i - 1] - d) > 0.01 * Math.abs(d)) even = false
      if (even) isStick = false
    }
  } else {
    throw new Error('No ##XYDATA or ##PEAK TABLE block found in this JCAMP-DX file.')
  }

  let xLabel = xUnits || 'x'
  if (xUnits === 'HZ' && freq) { xs = hzToPpm(Array.from(xs), L, freq); xLabel = 'PPM' }
  const kind: Spectrum['kind'] = dataType.includes('NMR') || xLabel === 'PPM' ? 'nmr'
    : dataType.includes('INFRARED') || xUnits.includes('1/CM') ? 'ir'
    : dataType.includes('MASS') ? 'ms' : dataType.includes('UV') ? 'uv' : 'other'
  if (kind === 'ms') isStick = true
  return finish({
    title: L.get('TITLE') || 'Spectrum', xLabel: kind === 'nmr' ? 'δ (ppm)' : kind === 'ir' ? 'Wavenumber (cm⁻¹)' : xLabel,
    yLabel: yUnits || 'Intensity', kind, reversed: kind === 'nmr' || kind === 'ir', dips: yUnits.includes('TRANSMITTANCE'),
    nucleus: L.get('.OBSERVENUCLEUS')?.replace(/\^/g, ''), frequency: freq && Number.isFinite(freq) && freq > 0 ? freq : undefined,
    solvent: L.get('.SOLVENTNAME'), isStick,
  }, xs, ys)
}

// ---------------------------------------------------------------- text tables (CSV, TSV, two columns)

const NOT_SPECTRUM = "this isn't a spectrum Benchtop can show: it needs two columns of numbers (x and y), or JCAMP-DX. Download it to open it in another program."
/** Names a header may give the x and the y column. */
const X_NAMES = /^(x|ppm|δ|delta|shift|chemical shift|δ \(ppm\)|wavenumbers?|cm-1|cm⁻¹|1\/cm|wavelength|nm|lambda|λ|m\/z|mz|mass|time|min|minutes|retention time|rt|t|hz|frequency|2θ|2theta|2-theta|angle|potential|voltage|e|v|energy|ev|temperature|t \(°c\)|°c)\b/i
const Y_NAMES = /^(y|intensity|int|absorbance|abs|a|transmittance|%t|t%|reflectance|counts|cps|signal|abundance|response|real|amplitude|au|a\.u\.|current|i|heat flow|mass %|weight %|weight)\b/i

/** A cell as a number: plain or with a decimal comma (when the file uses them), quotes removed; null if it isn't one
 *  (text, dates, "NaN", "inf", hexadecimal). Written for speed: a table can have millions of rows. */
function cellNumber(c: string, decimalComma: boolean): number | null {
  let t = c.trim()
  if (t === '') return null
  if (t.charCodeAt(0) === 34 /* " */) {
    if (t.length < 2 || t.charCodeAt(t.length - 1) !== 34) return null
    t = t.slice(1, -1).trim()
    if (!decimalComma && /^[+-]?\d{1,3}(,\d{3})+\.\d+$/.test(t)) t = t.replace(/,/g, '') // "1,234.5": thousands, with a decimal point
  }
  if (decimalComma) { if (t.includes('.')) return null; t = t.replace(',', '.') }
  const v = Number(t)
  if (!Number.isFinite(v) || t === '') return null
  const k = t.charCodeAt(0) === 43 || t.charCodeAt(0) === 45 ? 1 : 0  // after a sign: no 0x, 0b, 0o
  if (t.charCodeAt(k) === 48 && t.length > k + 1 && /[xXbBoO]/.test(t[k + 1])) return null
  return v
}

/** Split a line on the file's separator, keeping a quoted cell's commas. */
function cells(line: string, sep: string): string[] {
  if (sep === 'ws') return line.trim().split(/\s+/)
  if (!line.includes('"')) return line.split(sep)
  const out: string[] = []
  let cur = '', q = false
  for (const ch of line) {
    if (ch === '"') { q = !q; cur += ch } else if (ch === sep && !q) { out.push(cur); cur = '' } else cur += ch
  }
  out.push(cur)
  return out
}

/** A line's numbers, or null if any of its cells isn't one (or it has fewer than two). */
function rowNumbers(line: string, sep: string, decimalComma: boolean): number[] | null {
  if (line.length > 100_000) return null  // not a row of a table
  const cs = cells(line, sep)
  let len = cs.length
  while (len && cs[len - 1].trim() === '') len--  // a trailing separator
  if (len < 2) return null
  const out = new Array<number>(len)
  for (let k = 0; k < len; k++) { const v = cellNumber(cs[k], decimalComma); if (v === null) return null; out[k] = v }
  return out
}

/** The separator and decimal mark a table uses, from its first lines: tab, semicolon, comma or spaces; and whether
 *  numbers have a decimal comma (7,26 in French or German Excel). 0.36.4 split on commas, semicolons and spaces at
 *  once, so "7,26;1234,5" became x = 7, y = 26. */
export function tableFormat(sample: string[]): { sep: string; decimalComma: boolean } {
  const has = (re: RegExp) => sample.some(l => re.test(l))
  const commaNumber = /^[+-]?\d+,\d+([eE][+-]?\d+)?$/
  if (has(/\t/)) return { sep: '\t', decimalComma: has(/\d,\d/) && !has(/\d\.\d/) }
  if (has(/;/)) return { sep: ';', decimalComma: has(/\d,\d/) && !has(/\d\.\d/) }
  if (has(/,/)) {
    // "1,5 10,25": decimal commas between spaces; "1.5,10", "1,10" or "1, 10": a comma between columns
    const spaced = sample.filter(l => l.trim()).every(l => { const w = l.trim().split(/\s+/); return w.length >= 2 && w.every(c => !c.includes(',') || commaNumber.test(c)) })
    if (spaced && has(/\d,\d/) && !has(/\d\.\d/)) return { sep: 'ws', decimalComma: true }
    return { sep: ',', decimalComma: false }
  }
  return { sep: 'ws', decimalComma: false }
}

export function parseDelimited(raw: string, name: string): Spectrum {
  const text = normaliseText(raw)
  // binary files renamed .csv (a PDF, a picture): not a table at all
  const head = text.slice(0, 65536)
  if (/^%PDF|^\x89PNG|^GIF8|^\xff\xd8/.test(text) || /\0/.test(head) || (head.match(/[\x01-\x08\x0e-\x1f\ufffd]/g) || []).length > head.length / 100) throw new Error(NOT_SPECTRUM)
  const lines = text.split('\n')
  const sample: string[] = []
  for (let k = 0; k < lines.length && sample.length < 200; k++) if (lines[k].trim()) sample.push(lines[k])
  if (!sample.length) throw new Error(NOT_SPECTRUM)
  const { sep, decimalComma } = tableFormat(sample)
  // where the numbers start (the line before is the header, if any), and how many columns most rows have
  let firstData = -1
  const widths: number[] = []
  for (let k = 0; k < lines.length && widths.length < 200; k++) {
    if (!lines[k].trim()) continue
    const r = rowNumbers(lines[k], sep, decimalComma)
    if (r) { if (firstData < 0) firstData = k; widths.push(r.length) }
    else if (firstData < 0 && k > 100000) break
  }
  if (firstData < 0) throw new Error(NOT_SPECTRUM)
  let hk = firstData - 1
  while (hk >= 0 && !lines[hk].trim()) hk--
  const header = hk >= 0 ? cells(lines[hk], sep).map(c => c.trim().replace(/^"(.*)"$/, '$1')) : null
  const width = mode(widths)
  const cols: number[][] = Array.from({ length: width }, () => [])
  let other = 0, otherWidth = 0, partial = 0, n = 0
  for (let k = firstData; k < lines.length; k++) {
    const line = lines[k]
    if (!line.trim()) continue
    const r = rowNumbers(line, sep, decimalComma)
    if (!r) {
      // a row of the table with a cell that isn't a number ("NaN", "inf", 1e999), or a line of text
      const cs = line.length > 100_000 ? [] : cells(line, sep)
      if (cs.length >= 2 && cs.some(c => cellNumber(c, decimalComma) !== null)) partial++; else other++
      continue
    }
    if (r.length !== width) { otherWidth++; continue }
    for (let c = 0; c < width; c++) cols[c].push(r[c])
    if (++n > MAX_POINTS) throw tooMany()
  }
  if (n < 2) throw new Error(NOT_SPECTRUM)
  if (other > n) throw new Error(NOT_SPECTRUM)  // mostly not numbers: a document with a few numbers in it
  const warnings: string[] = []
  let xi = 0, yi = 1
  if (width > 2) {
    const h = header && header.length === width ? header.map(c => c.toLowerCase()) : null
    // x by its name: a chemical shift, wavenumber, wavelength or m/z before a time or an index
    const xRank = (c: string) => (!X_NAMES.test(c) ? 0 : /^(ppm|δ|delta|shift|chemical shift|wavenumbers?|cm|1\/cm|wavelength|nm|λ|lambda|m\/z|mz|mass|hz|frequency|2θ|2theta|2-theta|potential)/.test(c) ? 2 : 1)
    const ranks = h ? h.map(xRank) : []
    const best = Math.max(0, ...ranks)
    const xs = h && best ? ranks.map((r, k) => (r === best ? k : -1)).filter(k => k >= 0) : []
    const ysN = h ? h.map((c, k) => (Y_NAMES.test(c) && !xs.includes(k) ? k : -1)).filter(k => k >= 0) : []
    let isIndex = Number.isInteger(cols[0][0])
    for (let k = 1; isIndex && k < n; k++) if (cols[0][k] !== cols[0][0] + k) isIndex = false
    if (xs.length === 1 && ysN.length === 1) { xi = xs[0]; yi = ysN[0] }
    else if (width === 3 && isIndex && (!h || (!xs.includes(0) && xs.length <= 1))) { xi = 1; yi = 2 }
    else {
      const named = h ? ` (${header!.map(c => `“${shown(c)}”`).join(', ')})` : ''
      throw new Error(`This table has ${width} columns of numbers${named}, and Benchtop can't tell which are x and y. Save it with two columns (x, then y), or give the columns names such as “ppm” and “intensity” in a header row.`)
    }
    const label = (k: number) => (h && header![k] ? `“${shown(header![k])}”` : `column ${k + 1}`)
    warnings.push(`This table has ${width} columns: Benchtop drew ${label(yi)} against ${label(xi)}${isIndex && xi === 1 ? ' (the first column looks like row numbers)' : ''}.`)
  }
  if (partial) warnings.push(`${n0(partial)} row${partial === 1 ? ' has' : 's have'} a value that isn't a number (such as “NaN”), so ${partial === 1 ? 'it was' : 'they were'} left out.`)
  if (otherWidth) warnings.push(`${n0(otherWidth)} row${otherWidth === 1 ? '' : 's'} with a different number of columns ${otherWidth === 1 ? 'was' : 'were'} left out.`)
  const xs = cols[xi], ys = cols[yi]
  const [xmin, xmax] = extent(xs) // no spread: a million-point file would overflow the call stack
  const ppmLike = xmin > -2 && xmax < 250 && /nmr|1h|13c|ppm/i.test(name)
  const irLike = xmin >= 350 && xmax <= 4500 && /ir|ftir/i.test(name)
  return finish({
    title: name, xLabel: ppmLike ? 'δ (ppm)' : irLike ? 'Wavenumber (cm⁻¹)' : 'x', yLabel: 'Intensity',
    kind: ppmLike ? 'nmr' : irLike ? 'ir' : 'other', reversed: ppmLike || irLike, dips: false, isStick: false,
    ...(warnings.length ? { warnings } : {}),
  }, xs, ys)
}

function mode(a: number[]): number {
  const c = new Map<number, number>()
  for (const v of a) c.set(v, (c.get(v) || 0) + 1)
  let best = a[0], k = 0
  for (const [v, m] of c) if (m > k) { best = v; k = m }
  return best
}

/** A spectrum checked and stored ascending in x (drawing handles reversal). Refused with a plain message when it
 *  couldn't be drawn or measured truthfully: no points, an x or y that isn't a number, x that doesn't change, or x
 *  that goes up and down (0.36.4 integrated rows out of order: 35 instead of 120). Peak lists are sorted. */
function finish(meta: Omit<Spectrum, 'x' | 'y'>, xs: ArrayLike<number>, ys: ArrayLike<number>): Spectrum {
  const n = Math.min(xs.length, ys.length)
  if (n === 0) throw new Error('This file has no data points in it.')
  if (n === 1 && !meta.isStick) throw new Error('This file has only one data point, so there is no spectrum to draw.')
  if (n > MAX_POINTS) throw tooMany(n)
  for (let i = 0; i < n; i++) {
    if (!Number.isFinite(xs[i])) throw new Error('This file\'s x axis can\'t be worked out: a value it needs (such as the first and last x, the spectrometer frequency or the offset) is missing or isn\'t a number.')
    if (!Number.isFinite(ys[i])) throw new Error('This file has intensities that aren\'t numbers (or are too large to hold), so it can\'t be drawn. The file may be damaged, or its settings (data type, byte order) may be wrong.')
  }
  if (meta.isStick) {
    const x = new Float64Array(n), y = new Float64Array(n)
    const idx = Array.from({ length: n }, (_, i) => i).sort((a, b) => xs[a] - xs[b])
    idx.forEach((k, i) => { x[i] = xs[k]; y[i] = ys[k] })
    return { ...meta, x, y }
  }
  let up = false, down = false
  for (let i = 1; i < n; i++) { if (xs[i] > xs[i - 1]) up = true; else if (xs[i] < xs[i - 1]) down = true }
  if (!up && !down) throw new Error('Every x value in this file is the same, so there is no axis to draw it on.')
  const x = new Float64Array(n), y = new Float64Array(n)
  if (up && down) throw new Error('The x values in this file go up and down (rows out of order, or a scan that goes back, such as a cyclic voltammogram), so Benchtop can\'t draw it as a spectrum or measure areas. Sort the rows by x, or open it in another program.')
  if (!down && xs instanceof Float64Array && ys instanceof Float64Array && xs.length === n && ys.length === n) return { ...meta, x: xs, y: ys }
  for (let i = 0; i < n; i++) { const k = down ? n - 1 - i : i; x[i] = xs[k]; y[i] = ys[k] }
  return { ...meta, x, y }
}

/** A spectrum made elsewhere (a processed FID): the same checks as `finish`. */
function checked(s: Spectrum): Spectrum {
  const { x, y, ...meta } = s
  const f = finish(meta, x, y)
  return { ...s, x: f.x, y: f.y }
}

export { SPECTRUM_EXT, canViewSpectrum } from './viewable'
import { solventKey } from './solvents'
import { brukerGroupDelay, processComplexFid, processFid } from './fid'

/** Where a JCAMP-DX file starts when something else comes first (an e-mail header, $$ comments, a note): its first
 *  ##label line, if a ##TITLE or ##JCAMP-DX label comes early. 0.36.4 read such a file as a table of numbers. */
function jcampStart(text: string): number {
  const lead = /^(?:[ \t]*(?:\$\$[^\n]*)?\n)*[ \t]*##/.exec(text)  // only blank lines and $$ comments before it
  if (lead) return lead[0].length - 2
  const head = text.slice(0, 1 << 20)
  if (!/^##(TITLE|JCAMP)/im.test(head)) return -1
  const m = /^##/m.exec(head)
  return m ? m.index : -1
}

export function parseSpectrumFile(raw: string, filename: string, fidOpts: FidOptions = {}): Spectrum {
  const text = normaliseText(raw)
  const at = jcampStart(text)
  if (at < 0) return parseDelimited(text, filename)
  const before = text.slice(0, at)
  const s = parseJcamp(text.slice(at), fidOpts)
  if (before.replace(/^\s*\$\$.*$/gm, '').trim()) s.warnings = [...(s.warnings || []), 'Text before the JCAMP-DX header was left out.']
  return s
}

// ---------------------------------------------------------------- analysis

/** Min and max without spreading the array into arguments. */
export function extent(a: ArrayLike<number>): [number, number] {
  let lo = Infinity, hi = -Infinity
  for (let i = 0; i < a.length; i++) { const v = a[i]; if (v < lo) lo = v; if (v > hi) hi = v }
  return [lo, hi]
}

export function lowerBound(a: Float64Array, v: number) {
  let lo = 0, hi = a.length
  while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < v) lo = m + 1; else hi = m }
  return lo
}

/** Area between x0 and x1 (trapezoid rule, absolute x). */
export function integrate(s: Spectrum, x0: number, x1: number) {
  const a = Math.min(x0, x1), b = Math.max(x0, x1)
  const i0 = lowerBound(s.x, a), i1 = Math.min(lowerBound(s.x, b), s.x.length - 1)
  let area = 0
  for (let i = i0; i < i1; i++) area += (s.x[i + 1] - s.x[i]) * (s.y[i] + s.y[i + 1]) / 2
  return area
}

/** Local maxima (or minima for transmittance) above `frac` of the range's extreme. */
export function pickPeaks(s: Spectrum, x0: number, x1: number, frac: number, limit = 60) {
  const i0 = lowerBound(s.x, Math.min(x0, x1)), i1 = Math.min(lowerBound(s.x, Math.max(x0, x1)), s.x.length - 1)
  if (s.isStick) {
    const out: { x: number; y: number }[] = []
    let max = 0
    for (let i = i0; i <= i1; i++) max = Math.max(max, s.y[i])
    for (let i = i0; i <= i1; i++) if (s.y[i] >= frac * max) out.push({ x: s.x[i], y: s.y[i] })
    return out.sort((a, b) => b.y - a.y).slice(0, limit)
  }
  const sign = s.dips ? -1 : 1
  let lo = Infinity, hi = -Infinity
  for (let i = i0; i <= i1; i++) { const v = sign * s.y[i]; if (v < lo) lo = v; if (v > hi) hi = v }
  const cut = lo + frac * (hi - lo)
  const out: { x: number; y: number }[] = []
  for (let i = Math.max(i0, 2); i <= Math.min(i1, s.x.length - 3); i++) {
    const v = sign * s.y[i]
    if (v > cut && v >= sign * s.y[i - 1] && v > sign * s.y[i + 1] && v >= sign * s.y[i - 2] && v > sign * s.y[i + 2]) {
      // parabolic interpolation for sub-point accuracy
      const a = sign * s.y[i - 1], c = sign * s.y[i + 1]
      const d = (a - c) / (2 * (a - 2 * v + c) || 1)
      out.push({ x: s.x[i] + d * (s.x[i + 1] - s.x[i]), y: s.y[i] })
    }
  }
  return out.sort((a, b) => sign * (b.y - a.y)).slice(0, limit)
}

// ---------------------------------------------------------------- multiplets (first-order)

/** The spectrometer frequency (MHz) for J and for windows given in Hz: the file's, one the person typed, or, for an
 *  analysis saved before 0.36.5, the 400 MHz it was worked out with. Undefined when unknown: then there is no J
 *  (0.36.4 used 400 MHz for any file that didn't say, so a 7.1 Hz quartet at 60 MHz came out as J = 47.3 Hz). */
export const frequencyOf = (s: Pick<Spectrum, 'frequency' | 'assumedFrequency'>) => s.frequency || s.assumedFrequency || undefined
/** Only for telling lines apart when the frequency isn't known; never used for a number that is shown. */
const NOMINAL_MHZ = 400
/** What the analysis says when the frequency isn't known. */
export const NO_FREQUENCY = 'The file doesn\'t say the spectrometer frequency, so Benchtop can\'t work out J or group the lines into signals. Type the frequency to analyse it.'

export interface Multiplet {
  shift: number          // ppm (centre)
  lo: number; hi: number // ppm range of the signal
  mult: string           // s, d, t, q, p, dd, dt, td, m
  j: number[]            // Hz, largest first
  noJ?: boolean          // J not worked out: the spectrometer frequency isn't known
}

const near = (a: number, b: number, tolHz: number) => Math.abs(a - b) <= Math.max(tolHz, 0.12 * Math.max(a, b))

const binomial = (n: number) => { let row = [1]; for (let k = 1; k < n; k++) row = [1, ...row.slice(1).map((v, i) => v + row[i]), 1]; return row }

/** Equally spaced lines with heights close to Pascal's triangle (1:5:10:10:5:1, 1:6:15:20:15:6:1): a sextet or
 *  septet. Their outer lines are weak, so they are looked for again below the usual line-picking threshold. */
function binomialPattern(s: Spectrum, lo: number, hi: number, freq: number) {
  let top = 0
  const i0 = lowerBound(s.x, lo), i1 = Math.min(lowerBound(s.x, hi), s.x.length - 1)
  for (let i = i0; i <= i1; i++) top = Math.max(top, s.y[i])
  if (top <= 0) return null
  const noise = noiseLevel(s)
  const picked = pickPeaks(s, lo, hi, Math.max(0.02, 6 * noise / top), 40).sort((p, q) => p.x - q.x)
  // keep lines that rise clear of the dips either side (a wiggle of noise in a valley doesn't)
  const dip = (x0: number, x1: number) => { let v = Infinity; for (let i = lowerBound(s.x, x0); i <= Math.min(lowerBound(s.x, x1), s.x.length - 1); i++) v = Math.min(v, s.y[i]); return v }
  const lines = picked.filter((l, k) => {
    const left = dip(k ? picked[k - 1].x : lo, l.x), right = dip(l.x, k < picked.length - 1 ? picked[k + 1].x : hi)
    return l.y - Math.max(left, right) > Math.max(6 * noise, 0.01 * top)
  })
  const n = lines.length
  if (n < 6) return null
  const gaps = lines.slice(1).map((l, i) => (l.x - lines[i].x) * freq)
  const mean = gaps.reduce((p, q) => p + q, 0) / gaps.length
  if (!gaps.every(g => near(g, mean, 0.7))) return null
  const span = { j: mean, lo: lines[0].x, hi: lines[n - 1].x }
  // more lines than a septet (ibuprofen's CH, split by 8 protons): no name for it, it's a multiplet
  if (n > 7) return { mult: 'm', ...span }
  const want = binomial(n), wmax = Math.max(...want), ymax = Math.max(...lines.map(l => l.y))
  // each line within a factor of 2 of its share (roof effect and overlap tilt them a little)
  const fits = lines.every((l, k) => { const r = (l.y / ymax) / (want[k] / wmax); return r > 0.5 && r < 2 })
  if (!fits) return n === 7 ? { mult: 'm', ...span } : null  // seven lines too tall at the edges: the middle of a bigger one
  return { mult: n === 6 ? 'sext' : 'hept', ...span }
}

/** Classify the lines inside [a, b] (ppm) as a first-order multiplet. */
export function analyzeMultiplet(s: Spectrum, a: number, b: number): Multiplet {
  const lo = Math.min(a, b), hi = Math.max(a, b)
  const known = frequencyOf(s)
  const freq = known || NOMINAL_MHZ  // the pattern of lines (how many, their heights) doesn't need the frequency; J does
  let m = classify(s, lo, hi, freq)
  if (!s.isStick && ['q', 'p', 'dd', 'm'].includes(m.mult)) {
    const wide = binomialPattern(s, lo, hi, freq)
    if (wide) m = { shift: (wide.lo + wide.hi) / 2, lo: wide.lo, hi: wide.hi, mult: wide.mult, j: wide.mult === 'm' ? [] : [Math.round(wide.j * 10) / 10] }
  }
  return known ? m : { ...m, j: [], noJ: true }
}

function classify(s: Spectrum, lo: number, hi: number, freq: number): Multiplet {
  const lines = pickPeaks(s, lo, hi, 0.12, 40).sort((p, q) => p.x - q.x)
  const n = lines.length
  const xs = lines.map(l => l.x), ys = lines.map(l => l.y)
  const centre = n ? (xs[0] + xs[n - 1]) / 2 : (lo + hi) / 2
  const out = (mult: string, j: number[] = []): Multiplet => ({ shift: centre, lo: n ? xs[0] : lo, hi: n ? xs[n - 1] : hi, mult, j: j.map(v => Math.round(v * 10) / 10).sort((p, q) => q - p) })
  if (n <= 1) return { shift: n ? xs[0] : (lo + hi) / 2, lo, hi, mult: 's', j: [] }
  const gaps = xs.slice(1).map((x, i) => (x - xs[i]) * freq) // Hz
  const tol = 0.7
  const allEqual = gaps.every(g => near(g, gaps[0], tol))
  const mean = gaps.reduce((p, q) => p + q, 0) / gaps.length
  const ratio = (i: number, k: number) => ys[i] / ys[k]
  if (n === 2) return near(ys[0], ys[1], 0) || (ratio(0, 1) > 0.5 && ratio(0, 1) < 2) ? out('d', [gaps[0]]) : out('m')
  if (n === 3 && allEqual) {
    const r = (ys[1] * 2) / (ys[0] + ys[2])
    if (r > 1.3 && r < 3) return out('t', [mean])
  }
  if (n === 4) {
    if (allEqual) {
      const r = (ys[1] + ys[2]) / (ys[0] + ys[3])
      if (r > 2 && r < 4.5) return out('q', [mean])
    }
    if (near(gaps[0], gaps[2], tol) && ratio(0, 3) > 0.5 && ratio(0, 3) < 2) {
      const small = (gaps[0] + gaps[2]) / 2
      const large = (xs[2] - xs[0]) * freq
      if (large > small + tol) return out('dd', [large, small])
    }
  }
  if (n === 5 && allEqual) return out('p', [mean])
  if (n === 6) {
    // dt: two triplets  |  td: three doublets
    const t1 = [gaps[0], gaps[1]], t2 = [gaps[3], gaps[4]]
    if (near(t1[0], t1[1], tol) && near(t2[0], t2[1], tol) && near(t1[0], t2[0], tol)) {
      return out('dt', [(xs[3] - xs[0]) * freq, (t1[0] + t2[0]) / 2])
    }
    if (near(gaps[0], gaps[2], tol) && near(gaps[2], gaps[4], tol) && near(gaps[1], gaps[3], tol)) {
      return out('td', [(xs[2] - xs[0]) * freq, gaps[0]])
    }
  }
  if (n === 8) {
    // ddd (eight equal lines) or dq (two 1:3:3:1 quartets): rebuild the lines from the couplings and compare
    const rel = xs.map(x => (x - xs[0]) * freq)
    const fits = (pos: number[], h: number[]) => {
      const order = pos.map((p, k) => [p, h[k]]).sort((p, q) => p[0] - q[0])
      const hmax = Math.max(...h), ymax = Math.max(...ys)
      return order.every(([p, w], k) => near(rel[k], p, tol) && (ys[k] / ymax) / (w / hmax) > 0.5 && (ys[k] / ymax) / (w / hmax) < 2)
    }
    const j3 = rel[1], j2 = rel[2]
    for (const j1 of [rel[4], rel[3]]) {
      if (j1 > j2 + tol && j2 > j3 + tol && fits([0, j3, j2, j2 + j3, j1, j1 + j3, j1 + j2, j1 + j2 + j3], [1, 1, 1, 1, 1, 1, 1, 1])) return out('ddd', [j1, j2, j3])
    }
    const q = (rel[1] + rel[2] / 2 + rel[3] / 3) / 3, big = rel[4]
    if (big > 3 * q + tol && fits([0, q, 2 * q, 3 * q, big, big + q, big + 2 * q, big + 3 * q], [1, 3, 3, 1, 1, 3, 3, 1])) return out('dq', [big, q])
  }
  return out('m')
}

export function formatSignal(m: Multiplet, nH: number | null, decimals = 2) {
  const shift = m.mult === 'm'
    ? `${m.hi.toFixed(decimals)}–${m.lo.toFixed(decimals)}`
    : m.shift.toFixed(decimals)
  const parts = [m.mult]
  if (m.j.length) parts.push(`J = ${m.j.map(j => j.toFixed(1)).join(', ')} Hz`)
  if (nH !== null) parts.push(`${Number(nH.toFixed(nH < 1.5 ? 1 : 0))}H`)
  return `${shift} (${parts.join(', ')})`
}

// ---------------------------------------------------------------- solvents & referencing

export interface Marker { label: string; ppm: number }

// Residual solvent and water signals (Fulmer et al., Organometallics 2010, 29, 2176)
const H1: Record<string, Marker[]> = {
  CDCL3: [{ label: 'CHCl₃', ppm: 7.26 }, { label: 'H₂O', ppm: 1.56 }],
  DMSO: [{ label: 'DMSO-d₅', ppm: 2.50 }, { label: 'H₂O', ppm: 3.33 }],
  CD3OD: [{ label: 'CHD₂OD', ppm: 3.31 }, { label: 'H₂O', ppm: 4.87 }],
  C6D6: [{ label: 'C₆D₅H', ppm: 7.16 }, { label: 'H₂O', ppm: 0.40 }],
  D2O: [{ label: 'HDO', ppm: 4.79 }],
  CD2CL2: [{ label: 'CHDCl₂', ppm: 5.32 }, { label: 'H₂O', ppm: 1.52 }],
  ACETONE: [{ label: 'acetone-d₅', ppm: 2.05 }, { label: 'H₂O', ppm: 2.84 }],
  CD3CN: [{ label: 'CHD₂CN', ppm: 1.94 }, { label: 'H₂O', ppm: 2.13 }],
}
const C13: Record<string, Marker[]> = {
  CDCL3: [{ label: 'CDCl₃', ppm: 77.16 }], DMSO: [{ label: 'DMSO-d₆', ppm: 39.52 }], CD3OD: [{ label: 'CD₃OD', ppm: 49.00 }],
  C6D6: [{ label: 'C₆D₆', ppm: 128.06 }], CD2CL2: [{ label: 'CD₂Cl₂', ppm: 53.84 }],
  ACETONE: [{ label: 'acetone-d₆', ppm: 29.84 }, { label: 'acetone-d₆', ppm: 206.26 }], CD3CN: [{ label: 'CD₃CN', ppm: 1.32 }, { label: 'CD₃CN', ppm: 118.26 }],
}


export function solventMarkers(s: Spectrum): Marker[] {
  if (s.kind !== 'nmr') return []
  const table = /13C/i.test(s.nucleus || '') ? C13 : H1
  return table[solventKey(s.solvent)] || []
}

export function referenceChoices(s: Spectrum): Marker[] {
  const tms = { label: 'TMS', ppm: 0 }
  // water moves with temperature and concentration, so it isn't offered as a reference (except HDO in D2O)
  const ok = solventMarkers(s).filter(m => m.label !== 'H₂O' && (m.label !== 'HDO' || solventKey(s.solvent) === 'D2O'))
  return [...ok, tms]
}

export function shifted(s: Spectrum, offset: number): Spectrum {
  if (!offset) return s
  return { ...s, x: s.x.map(v => v + offset) }
}

/** Nearest local maximum (peak top) to x within ±win; falls back to x if there is none.
 *  Nearest rather than tallest, so clicking a small solvent peak next to a big multiplet works. */
export function snapToPeak(s: Spectrum, x: number, win: number) {
  const sign = s.dips ? -1 : 1
  const i0 = Math.max(1, lowerBound(s.x, x - win)), i1 = Math.min(lowerBound(s.x, x + win), s.x.length - 2)
  let lo = Infinity, hi = -Infinity
  for (let i = i0; i <= i1; i++) { const v = sign * s.y[i]; if (v < lo) lo = v; if (v > hi) hi = v }
  let best = -1
  for (let i = i0; i <= i1; i++) {
    const v = sign * s.y[i]
    if (v > lo + 0.02 * (hi - lo) && v >= sign * s.y[i - 1] && v > sign * s.y[i + 1]) {
      if (best < 0 || Math.abs(s.x[i] - x) < Math.abs(s.x[best] - x)) best = i
    }
  }
  return best < 0 ? x : s.x[best]
}

export const MULTIPLICITIES = ['s', 'br s', 'd', 't', 'q', 'p', 'sext', 'hept', 'dd', 'dt', 'td', 'ddd', 'dq', 'm']

// ---------------------------------------------------------------- Bruker processed data (zipped experiment folder)

function brukerParams(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of normaliseText(text).matchAll(/^##\$?([A-Za-z0-9_]+)=\s*(.*)$/gm)) out[m[1].toUpperCase()] = m[2].trim().replace(/^<(.*)>$/, '$1')
  return out
}

/** Read pdata/N/1r + procs (+ acqus for nucleus/solvent) from a zipped Bruker experiment. */
const TWO_D = 'This is a 2D experiment (or a series, such as T1 or diffusion). Benchtop shows 1D spectra; this one is kept with the record as a file.'

/** The most a zip entry may unpack to: a spectrum's data (1r, fid) up to MAX_POINTS values of 8 bytes; a parameter
 *  file a few MB; a JCAMP-DX file what the viewer opens. Checked from the zip's own listing before anything is
 *  unpacked, and each entry is unpacked into a buffer of the size the listing gives (0.36.4 unpacked every entry,
 *  so a 1 MB zip holding 1 GB of zeros ran the page out of memory). */
const ZIP_DATA_MAX = MAX_POINTS * 8, ZIP_TEXT_MAX = 4_000_000, ZIP_JCAMP_MAX = 100_000_000
const mb = (n: number) => `${(n / 1e6).toLocaleString('en-GB', { maximumFractionDigits: n < 1e7 ? 1 : 0 })} MB`

/** A zip's entries, by path with forward slashes (zips made on Windows use back-slashes), found whatever their case
 *  (FID, ACQUS), unpacking only the ones asked for. */
function zipReader(buf: Uint8Array, unzipSync: typeof import('fflate').unzipSync) {
  const list: { name: string; key: string; size: number }[] = []
  try {
    unzipSync(buf, { filter: f => { list.push({ name: f.name, key: f.name.replace(/\\/g, '/').replace(/^\/+/, ''), size: f.originalSize }); return false } })
  } catch { throw new Error('This zip file is damaged (it may have been cut off while copying), so Benchtop can\'t open it. Download it to check, or zip the folder again.') }
  const keys = list.filter(e => !/(^|\/)__MACOSX\//.test(e.key) && !/(^|\/)\._/.test(e.key) && !e.key.endsWith('/'))  // macOS zip junk, folders
  const byLower = new Map<string, (typeof list)[number]>()
  for (const e of keys) if (!byLower.has(e.key.toLowerCase())) byLower.set(e.key.toLowerCase(), e)
  const names = keys.map(e => e.key)
  const entry = (path: string) => byLower.get(path.toLowerCase())
  const has = (path: string) => !!entry(path)
  const read = (path: string, max: number, what = path.split('/').pop() || path): Uint8Array | undefined => {
    const e = entry(path)
    if (!e) return undefined
    if (e.size > max) throw new Error(`The ${what} file in this zip unpacks to ${mb(e.size)}, more than Benchtop opens in the browser (${mb(max)}). Download the zip to open it in the instrument's software.`)
    let out: Uint8Array | undefined
    try { out = unzipSync(buf, { filter: f => f.name === e.name })[e.name] } catch { throw new Error(`The ${what} file in this zip is damaged, so Benchtop can't unpack it. Zip the folder again.`) }
    return out
  }
  return { names, has, read, text: (path: string) => { const b = read(path, ZIP_TEXT_MAX); return b ? strFromBytes(b) : undefined } }
}
const strFromBytes = (b: Uint8Array) => new TextDecoder('latin1').decode(b)
const byDepth = (a: string, b: string) => a.split('/').length - b.split('/').length || a.localeCompare(b, undefined, { numeric: true })

export async function parseBrukerZip(buf: ArrayBuffer, filename: string, fidOpts: FidOptions = {}): Promise<Spectrum> {
  const { unzipSync } = await import('fflate')
  const z = zipReader(new Uint8Array(buf), unzipSync)
  const names = z.names
  const real = names.filter(n => /(^|\/)pdata\/\d+\/1r$/i.test(n)).sort(byDepth)
  if (!real.length) {
    const fids = names.filter(n => /(^|\/)fid$/i.test(n)).sort(byDepth)
    if (fids.length) {
      const dir = fids[0].slice(0, -3)
      const acqusText = z.text(dir + 'acqus')
      if (acqusText === undefined) throw new Error('Found a raw FID but not its acqus file, so it can\'t be processed. Zip the whole experiment folder.')
      const acqus = brukerParams(acqusText)
      if (Number(acqus.PARMODE || 0) > 0 || z.has(dir + 'ser')) throw new Error(TWO_D)
      // referenced in TopSpin (SR)? Its procs SF says so; keep that referencing
      const procsText = z.text(dir + 'pdata/1/procs')
      const procs = procsText !== undefined ? brukerParams(procsText) : null
      const raw = z.read(fids[0], ZIP_DATA_MAX / 2, 'fid')!
      const width = acqus.DTYPA === '2' ? 8 : 4
      const td = Number(acqus.TD), have = Math.floor(raw.byteLength / width)
      let zf = 1; while (zf < Math.min(td > 0 ? td : Infinity, have) / 2) zf <<= 1
      if (2 * zf > MAX_POINTS) throw tooMany(2 * zf)  // zero-filled to twice the next power of two
      const f = processFid(raw, acqus, { ...fidOpts, sf: Number(procs?.SF) || undefined })
      const nucleus = acqus.NUC1 || '1H'
      const warnings = td > 0 && have < td * 0.99
        ? [`The fid file holds ${n0(have)} of the ${n0(td)} points its acqus file says were recorded, so it may have been cut off while copying: lines come out broader and small signals may be missing. Copy the experiment folder again if you can.`]
        : []
      return checked({
        title: `${filename.replace(/\.zip$/i, '')} (${dir.replace(/\/$/, '') || 'experiment'})`, xLabel: 'δ (ppm)', yLabel: 'Intensity',
        kind: 'nmr', reversed: true, dips: false, nucleus, frequency: Number(acqus.SFO1) || Number(acqus.BF1), solvent: acqus.SOLVENT || '',
        isStick: false, x: f.x, y: f.y, fid: f, ...(warnings.length ? { warnings } : {}),
      })
    }
    // a JCAMP-DX spectrum zipped on its own; not a JCAMP inside a Bruker experiment (TopSpin writes fit curves there)
    const jdx = names.find(n => /\.(jdx|dx|jcamp)$/i.test(n) && !names.some(m => /acqus$/i.test(m) && n.toLowerCase().startsWith(m.slice(0, -5).toLowerCase())))
    if (jdx) return parseSpectrumFile(new TextDecoder().decode(z.read(jdx, ZIP_JCAMP_MAX, 'JCAMP-DX')!), jdx, fidOpts)
    if (names.some(n => /(^|\/)(ser|pdata\/\d+\/2rr)$/i.test(n))) throw new Error(TWO_D)
    throw new Error('No processed Bruker spectrum (pdata/…/1r) or JCAMP-DX file found in this zip.')
  }
  const path = real[0]
  const dir = path.slice(0, -2)
  const procsText = z.text(dir + 'procs')
  if (procsText === undefined) throw new Error('Found 1r but not its procs file, so the ppm scale is unknown.')
  const procs = brukerParams(procsText)
  const expRoot = dir.replace(/pdata\/\d+\/$/i, '')
  const acqusText = z.text(expRoot + 'acqus')
  const acqus = acqusText !== undefined ? brukerParams(acqusText) : {}
  const si = Number(procs.SI), sf = Number(procs.SF), offset = Number(procs.OFFSET), swp = Number(procs.SW_P)
  const nc = Number(procs.NC_PROC || 0), big = procs.BYTORDP === '1', dtype = Number(procs.DTYPP || 0)
  if (!(sf > 0) || !(swp > 0) || !Number.isFinite(offset)) throw new Error('The procs file doesn\'t give the spectrometer frequency, spectral width and offset (SF, SW_p, OFFSET) as numbers, so the ppm scale is unknown.')
  if (si > MAX_POINTS) throw tooMany(si)
  const raw = z.read(path, ZIP_DATA_MAX, '1r')!
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength)
  const width = dtype === 2 ? 8 : 4
  const have = Math.floor(raw.byteLength / width)
  const n = Math.min(si || Infinity, have)
  if (si && n < si * 0.99) throw new Error(`The 1r file holds ${n.toLocaleString('en-GB')} of the ${si.toLocaleString('en-GB')} points its procs file promises, so it looks incomplete. Re-export or re-copy the experiment folder.`)
  if (si && have > si * 1.01) throw new Error(`The 1r file holds ${n0(have)} points but its procs file says ${n0(si)}, so they don't belong together and the ppm scale can't be trusted. Re-export or re-copy the experiment folder.`)
  const scale = 2 ** nc
  const ys = new Float64Array(n)
  for (let i = 0; i < n; i++) ys[i] = (dtype === 2 ? view.getFloat64(i * 8, !big) : view.getInt32(i * 4, !big)) * scale
  const step = swp / sf / n
  const xs = new Float64Array(n)
  for (let i = 0; i < n; i++) xs[i] = offset - i * step
  const nucleus = (acqus.NUC1 || procs.AXNUC || '1H').replace(/^off$/i, '1H')
  const solvent = acqus.SOLVENT || ''
  const title = `${filename.replace(/\.zip$/i, '')} (${expRoot.replace(/\/$/, '') || 'experiment'})`
  return finish({
    title, xLabel: 'δ (ppm)', yLabel: 'Intensity', kind: 'nmr', reversed: true, dips: false,
    nucleus, frequency: sf, solvent, isStick: false,
  }, xs, ys)
}

/** A message for a person: our own plain messages as they are; anything else (a RangeError from a damaged file, a
 *  zip library's "invalid zip data") in words. 0.36.4 showed "Invalid array length". */
function plainError(e: unknown): Error {
  if (e instanceof Error && e.constructor === Error && !('code' in e)) return e
  return new Error('Benchtop couldn\'t read this file: it may be damaged, or in a format Benchtop doesn\'t know. Download it to open it in another program.')
}

/** Text from bytes: UTF-16 when the file starts with its byte-order mark (Excel's "Unicode text"), otherwise UTF-8. */
function decodeText(buf: ArrayBuffer): string {
  const h = new Uint8Array(buf, 0, Math.min(2, buf.byteLength))
  if (h[0] === 0xff && h[1] === 0xfe) return new TextDecoder('utf-16le').decode(buf)
  if (h[0] === 0xfe && h[1] === 0xff) return new TextDecoder('utf-16be').decode(buf)
  return new TextDecoder().decode(buf)
}

/** Any supported file, from raw bytes. */
export async function parseSpectrumBuffer(buf: ArrayBuffer, filename: string, fidOpts: FidOptions = {}): Promise<Spectrum> {
  try {
    const head = new Uint8Array(buf, 0, Math.min(4, buf.byteLength))
    if (head[0] === 0x50 && head[1] === 0x4b) return await parseBrukerZip(buf, filename, fidOpts) // "PK" = zip
    const text = decodeText(buf)
    if (jcampStart(normaliseText(text.slice(0, 1 << 20))) < 0) { // not JCAMP: a plugin reader may know this instrument's export
      const r = matchReader(await fileReaders(), filename, text.slice(0, 4096))
      if (r) return parseWithReader(text, filename, r)
    }
    return parseSpectrumFile(text, filename, fidOpts)
  } catch (e) { throw plainError(e) }
}

/** Whether a text file is worth offering "View spectrum" for, from its first few KB only: the record page asks this of
 *  every .txt/.csv/.tsv it shows, and 0.36.4 read each whole file there (a 361-byte file that unpacks to millions of
 *  points froze the page on opening). The full reading happens when the person clicks View. */
export async function looksLikeSpectrum(buf: ArrayBuffer, filename: string): Promise<boolean> {
  const part = buf.byteLength > 16384 ? buf.slice(0, 16384) : buf
  const text = normaliseText(decodeText(part))
  if (jcampStart(text) >= 0) return /^##(XYDATA|NTUPLES|XYPOINTS|PEAK\s*TABLE)\s*=/im.test(text) || buf.byteLength > 16384
  if (matchReader(await fileReaders(), filename, text.slice(0, 4096))) return true
  const lines = text.split('\n')
  if (buf.byteLength > 16384) lines.pop()  // cut part-way through a line
  try { parseDelimited(lines.join('\n'), filename); return true } catch { return false }
}

/** Characterisation label for a spectrum: technique + conditions. */
export function techniqueFor(s: Spectrum): { technique: string; conditions: string } {
  if (s.technique) return { technique: s.technique, conditions: s.conditions || '' }
  if (s.kind === 'nmr') {
    const nuc = (s.nucleus || '1H').replace(/[^0-9A-Za-z]/g, '')
    const mhz = s.frequency ? `${Math.round(s.frequency)} MHz` : ''
    return { technique: `${nuc} NMR`, conditions: [mhz, s.solvent].filter(Boolean).join(', ') }
  }
  if (s.kind === 'ir') return { technique: 'IR', conditions: '' }
  if (s.kind === 'ms') return { technique: 'MS', conditions: '' }
  if (s.kind === 'uv') return { technique: 'UV-vis', conditions: '' }
  return { technique: s.title || 'Spectrum', conditions: '' }
}

// ---------------------------------------------------------------- automatic analysis

/** Robust noise estimate: median absolute deviation of the point-to-point differences. */
export function noiseLevel(s: Spectrum) {
  const d: number[] = []
  const stride = Math.max(1, Math.floor(s.y.length / 20000))
  for (let i = stride; i < s.y.length; i += stride) d.push(Math.abs(s.y[i] - s.y[i - stride]))
  d.sort((a, b) => a - b)
  return (d[Math.floor(d.length / 2)] || 0) * 1.4826 / Math.SQRT2
}

/** Proton count from a Hill formula such as C13H12O (ignores Hg, Hf, Ho, He…). */
export function hydrogensInFormula(formula?: string | null): number | null {
  if (!formula) return null
  const m = formula.match(/H(\d*)(?![a-z])/)
  return m ? Number(m[1] || 1) : null
}

export interface ImpurityResult {
  short: string; name: string; molPct: number | null; wtPct: number | null
  windows: { lo: number; hi: number }[]; signals: number[] // ppm of each signal, for labels
}
export interface AutoResult {
  regions: { a: number; b: number }[]; refIdx: number; refValue: number; note: string
  impurities: ImpurityResult[]
  needsFrequency?: boolean  // nothing done: the spectrometer frequency isn't known
}

function baseline(s: Spectrum) {
  const sample: number[] = []
  const stride = Math.max(1, Math.floor(s.y.length / 20000))
  for (let i = 0; i < s.y.length; i += stride) sample.push(s.y[i])
  sample.sort((a, b) => a - b)
  return sample[Math.floor(sample.length / 2)] || 0
}

/** Group lines above the noise into signals, skipping anything inside `cuts`. */
function segment(s: Spectrum, cuts: { lo: number; hi: number }[]) {
  const noise = noiseLevel(s), base = baseline(s)
  let max = 0  // the tallest line that isn't cut out: a huge solvent peak mustn't make the compound's look like noise
  for (let i = 0; i < s.y.length; i++) if (s.y[i] > max && !cuts.some(c => s.x[i] >= c.lo && s.x[i] <= c.hi)) max = s.y[i]
  const on = base + Math.max(8 * noise, 0.006 * (max - base))
  const freq = frequencyOf(s) || NOMINAL_MHZ
  const gap = 12 / freq // merge lines closer than ~12 Hz into one signal
  const pad = 4 / freq
  const cut = (x: number) => cuts.some(c => x >= c.lo && x <= c.hi)
  // a stretch that starts right at a cut and only falls away from it is the tail of what was cut (a big solvent
  // peak's tails reach far at high field); anything that rises again on the way out is a signal of its own
  const rise = 6 * noise
  const tail = (i0: number, i1: number) => {
    const fromLeft = i0 > 0 && cut(s.x[i0 - 1]), fromRight = i1 < s.y.length - 1 && cut(s.x[i1 + 1])
    if (fromLeft === fromRight) return false  // between two cuts, or touching none
    let low = Infinity
    for (let k = 0; k <= i1 - i0; k++) {
      const v = s.y[fromLeft ? i0 + k : i1 - k]
      if (v > low + rise) return false
      low = Math.min(low, v)
    }
    return true
  }
  const runs: { a: number; b: number }[] = []
  let start = -1
  for (let i = 0; i < s.y.length; i++) {
    const above = s.y[i] > on && !cut(s.x[i])
    if (above && start < 0) start = i
    if ((!above || i === s.y.length - 1) && start >= 0) {
      const end = above ? i : i - 1
      if (!tail(start, end)) runs.push({ a: s.x[start], b: s.x[i] })
      start = -1
    }
  }
  const merged: { a: number; b: number }[] = []
  for (const r of runs) {
    const last = merged[merged.length - 1]
    if (last && r.a - last.b < gap) last.b = r.b
    else merged.push({ ...r })
  }
  let regions = merged.map(r => ({ a: r.a - pad, b: r.b + pad }))
    .filter(r => !cuts.some(c => (r.a + r.b) / 2 >= c.lo - 0.01 && (r.a + r.b) / 2 <= c.hi + 0.01 && r.b - r.a < 0.03))
  let areas = regions.map(r => integrate(s, r.a, r.b))
  const big = Math.max(...areas, 0)
  const keep = areas.map(a => a > 0.04 * big) // drops 13C satellites and baseline wiggles
  regions = regions.filter((_, i) => keep[i]); areas = areas.filter((_, i) => keep[i])
  return { regions, areas }
}

/** Find ¹H signals and set integrals to whole protons.
 *  - Deuterated-solvent residual, water and TMS lines are cut out where they actually are (`solventShift`).
 *  - Residual lab solvents (EtOAc, Et₂O, hexane…) are recognised, kept out of the integrals and quantified,
 *    but only when every one of their signals is present and they are small next to the product, so a
 *    product's own ethyl ester or methyl singlet is never mistaken for a solvent.
 *  - With `expectedH` (protons in the product formula) the integrals are scaled to add up to it.
 *  - With `productShifts` (predicted 1H shifts of the product), a solvent signal sitting where the product
 *    is expected to have one isn't evidence for the solvent: ibuprofen's 3.70 q and 1.85 m look exactly
 *    like THF, and its aromatic doublets like toluene. A solvent then needs two signals the product
 *    can't explain. */
// residuals that are multiplets (H–D coupling): their centre line is the tallest
const MULTIPLET_RESIDUALS = new Set(['DMSO-d₅', 'CHD₂OD', 'acetone-d₅', 'CHD₂CN', 'CHDCl₂'])

/** The nearest line to `at` (within `search` ppm) that stands out from what's around it: a noise wiggle on a big
 * peak's tail doesn't count. For a multiplet residual, the multiplet's centre (its tallest line, within 5 Hz). */
function nearestLine(s: Spectrum, at: number, search: number, label = '', lone = false): number {
  const freq = frequencyOf(s) || NOMINAL_MHZ, noise = noiseLevel(s), base = baseline(s)
  const i0 = Math.max(1, lowerBound(s.x, at - search)), i1 = Math.min(lowerBound(s.x, at + search), s.x.length - 2)
  const step = Math.abs(s.x[1] - s.x[0]) || 1e-9
  const k = Math.max(2, Math.round(3 / freq / step))  // look 3 Hz either side for the valleys
  const lines: number[] = []
  const pad = 12 / freq  // also look a little outside, for the partners of lines near the edge
  for (let i = Math.max(1, lowerBound(s.x, at - search - pad)); i <= Math.min(lowerBound(s.x, at + search + pad), s.x.length - 2); i++) {
    if (!(s.y[i] > s.y[i - 1] && s.y[i] >= s.y[i + 1]) || s.y[i] - base < 10 * noise) continue
    let left = s.y[i], right = s.y[i]
    for (let j = Math.max(0, i - k); j < i; j++) left = Math.min(left, s.y[j])
    for (let j = i + 1; j <= Math.min(s.x.length - 1, i + k); j++) right = Math.min(right, s.y[j])
    if (s.y[i] - Math.max(left, right) < 5 * noise) continue  // not a line of its own
    lines.push(i)
  }
  const inside = lines.filter(i => i >= i0 && i <= i1)
  const dist = (i: number) => Math.abs(s.x[i] - at)
  // A singlet residual (CHCl₃, water) is a line on its own. When the spectrum is referenced a little off, the
  // nearest line to 7.26 can be one line of the compound's own doublet (ibuprofen's at 7.25, with CHCl₃ at 7.29):
  // a line with a partner of about the same height 1.5–12 Hz away is part of a multiplet, so a line standing
  // clear of everything else close by is preferred (if CHCl₃ sits on the compound's lines, there is none, and the
  // nearest line is kept). The residual multiplets (DMSO-d₅, CHD₂OD…) are left as they were.
  const within12 = (i: number, lo: number, hi: number) => lines.some(j => {
    const d = Math.abs(s.x[j] - s.x[i]) * freq, r = (s.y[j] - base) / (s.y[i] - base)
    return j !== i && d > 1.5 && d < 12 && r > lo && r < hi
  })
  const nearest = (c: number[]) => c.reduce((b, i) => (b < 0 || dist(i) < dist(b) ? i : b), -1)
  let best = nearest(inside)
  if (best >= 0 && !MULTIPLET_RESIDUALS.has(label) && within12(best, 0.5, 2)) {
    const alone = nearest(inside.filter(i => !within12(i, 0.3, Infinity) && (lone || dist(i) < 0.05)))
    // water must be a line of its own: one line of the compound's doublet is never taken for it (0.36.4 cut half of
    // ibuprofen's CH₃ doublet as water when the real water line was lost in the noise)
    if (alone >= 0) best = alone
    else if (lone) return -1
  }
  if (best >= 0 && MULTIPLET_RESIDUALS.has(label)) {
    const span = 5 / freq
    let top = best
    for (let i = lowerBound(s.x, s.x[best] - span); i < s.x.length && s.x[i] <= s.x[best] + span; i++) if (s.y[i] > s.y[top]) top = i
    best = top
  }
  return best
}

/** Where a solvent's residual and water lines actually are in this spectrum, and how wide: each is looked for near
 * its usual position (water moves with temperature and concentration, so further), then followed out from its top
 * until it has fallen back to the baseline, or starts rising into a neighbouring signal. A residual's multiplet
 * (CHD₂OD and DMSO-d₅ are quintets) is always covered whole. Capped, so it can't swallow the compound's own signals. */
export function solventCuts(s: Spectrum, solventShift = 0, productShifts: number[] = []): { lo: number; hi: number }[] {
  const freq = frequencyOf(s) || NOMINAL_MHZ
  const noise = noiseLevel(s), base = baseline(s)
  const out: { lo: number; hi: number }[] = []
  for (const m of solventMarkers(s)) {
    const water = m.label === 'H₂O' || m.label === 'HDO'
    const at = m.ppm + solventShift
    const search = water ? 0.08 : 0.03
    // always cut a multiplet residual whole (a quintet spans about ±4 Hz); a single line only needs its top
    const core = (MULTIPLET_RESIDUALS.has(m.label) ? 6 : 2) / freq, cap = water ? Math.max(0.04, 30 / freq) : 25 / freq
    const top = nearestLine(s, at, search, m.label, water)
    const ours = top >= 0 && water && productShifts.some(p => Math.abs(p + solventShift - s.x[top]) < 0.05)  // the product's own signal
    if (top < 0 || ours || s.y[top] - base < 20 * noise) { out.push({ lo: at - 3.5 / freq, hi: at + 3.5 / freq }); continue }  // as before
    const c = s.x[top], floor = base + Math.max(4 * noise, 0.01 * (s.y[top] - base))
    const rise = Math.max(6 * noise, 0.03 * (s.y[top] - base))  // a real neighbouring signal, not noise on the tail
    const follow = (dir: 1 | -1) => {
      let i = top, low = s.y[top]
      while (i + dir >= 0 && i + dir < s.x.length) {
        const nx = i + dir, d = Math.abs(s.x[nx] - c)
        if (d > cap) break
        if (d > core && (s.y[nx] < floor || s.y[nx] > low + rise)) break  // back at the baseline, or another signal
        low = Math.min(low, s.y[nx]); i = nx
      }
      return s.x[i]
    }
    const a = follow(-1), b = follow(1)
    out.push({ lo: Math.min(a, b), hi: Math.max(a, b) })
  }
  return out
}

/** With no formula: the area of one proton, normally the smallest signal's. But a small impurity (grease, a trace of
 * solvent) mustn't become "1 H" and make everything else 8 H: when the smallest signals are well apart from the rest
 * (2.5 times smaller or more, under a tenth of the total) and the rest come out as clearly better whole numbers
 * without them, they are left out of the choice. */
export function protonUnit(areas: number[]): number {
  const a = [...areas].sort((p, q) => p - q)
  const total = a.reduce((p, q) => p + q, 0)
  const off = (u: number, from: number) => { let d = 0; for (let k = from; k < a.length; k++) d += Math.abs(a[k] / u - Math.round(a[k] / u)); return d / (a.length - from) }
  let below = 0
  for (let i = 0; i + 3 < a.length && i < 3; i++) {
    below += a[i]
    if (below > 0.1 * total) break
    if (a[i + 1] >= 2.5 * a[i] && off(a[i + 1], i + 1) + 0.03 < 0.6 * off(a[0], i + 1)) return a[i + 1]
  }
  return a[0]
}

export function autoAnalyse1H(s: Spectrum, expectedH: number | null, solventShift = 0, productMW: number | null = null, productShifts: number[] = []): AutoResult {
  const freq = frequencyOf(s)
  if (!freq) return { regions: [], refIdx: 0, refValue: 1, note: NO_FREQUENCY, impurities: [], needsFrequency: true }
  const point = (p: number) => ({ lo: p - 3.5 / freq, hi: p + 3.5 / freq })
  const fixed = [...solventCuts(s, solventShift, productShifts), point(solventShift)]  // the solvent's lines, and TMS
  const explained = (w: { lo: number; hi: number }) => productShifts.some(p => p + solventShift > w.lo - 0.12 && p + solventShift < w.hi + 0.12)
  const cands = findImpurityCandidates(s, solventShift)
    // its distinctive signals (singlets, triplets, quartets…) must all be there: two aromatic doublets in the 7.1–7.3
    // region aren't toluene without toluene's CH₃ singlet at 2.36
    .filter(c => c.windows.filter(w => w.mult !== 'm').length === c.def.signals.filter(g => g.mult !== 'm').length)
    // and about the same size per proton: a ¹³C satellite at 2.36 is not toluene's CH₃ next to aromatic doublets 100
    // times bigger (one signal overlapping the compound's can be bigger, so a factor of 4 is allowed)
    .filter(c => { const ph = c.windows.map(w => integrate(s, w.lo - 4 / freq, w.hi + 4 / freq) / w.nH); return Math.max(...ph) < 4 * Math.min(...ph) })
    .filter(c => !productShifts.length || c.windows.filter(w => !explained(w)).length >= Math.min(2, c.def.signals.length))

  const scale = (areas: number[]) => {
    const total = areas.reduce((p, q) => p + q, 0)
    return expectedH && expectedH > 0 ? total / expectedH : protonUnit(areas)
  }
  // measure a little wider than the cut window so the Lorentzian tails are counted
  const perH = (c: FoundImpurity) => Math.min(...c.windows.map(w => integrate(s, w.lo - 4 / freq, w.hi + 4 / freq) / w.nH))

  // pass 1: assume every candidate is an impurity
  let { regions, areas } = segment(s, [...fixed, ...cands.flatMap(c => c.windows)])
  if (!regions.length) return { regions: [], refIdx: 0, refValue: 1, note: 'No signals stood out from the noise.', impurities: [] }
  let unit = scale(areas)
  // pass 2: keep only candidates that are small next to the product. A product's own singlet can mimic
  // a one-line solvent with more protons (a 3H CH3 at 2.14 looks like 50 mol% of 6H acetone), so lone
  // singlets must be under 20 mol%; multi-signal matches, already much more specific, under 50 mol%.
  const confirmed = cands.filter(c => perH(c) < (c.confident ? 0.5 : 0.2) * unit)
  if (confirmed.length !== cands.length) {
    ({ regions, areas } = segment(s, [...fixed, ...confirmed.flatMap(c => c.windows)]))
    unit = scale(areas)
  }

  const impurities: ImpurityResult[] = confirmed.map(c => {
    const mol = 100 * perH(c) / unit
    return {
      short: c.def.short, name: c.def.name,
      molPct: Math.round(mol * 10) / 10,
      wtPct: c.def.mw && productMW ? Math.round(mol * c.def.mw / productMW * 10) / 10 : null,
      windows: c.windows.map(w => ({ lo: w.lo, hi: w.hi })),
      signals: c.windows.map(w => (w.lo + w.hi) / 2),
    }
  })

  let note = expectedH && expectedH > 0
    ? `Scaled so the integrals add up to ${expectedH} H, the product's formula.`
    : areas.some(a => a < 0.9 * unit)
      ? `Scaled so the smallest signal is 1 H, apart from ${areas.filter(a => a < 0.9 * unit).length === 1 ? 'one much smaller signal (an impurity?)' : 'a few much smaller signals (impurities?)'}. No product formula to check against.`
      : 'Scaled so the smallest signal is 1 H (no product formula to check against).'
  // reference = the signal whose value is closest to a whole number
  let refIdx = 0, best = Infinity
  areas.forEach((a, i) => { const v = a / unit, d = Math.abs(v - Math.round(v)) / Math.max(1, v); if (Math.round(v) >= 1 && d < best) { best = d; refIdx = i } })
  const refValue = Math.max(1, Math.round(areas[refIdx] / unit))
  const sum = areas.reduce((p, q) => p + q / areas[refIdx] * refValue, 0)
  if (expectedH && Math.abs(sum - expectedH) > 0.6) note += ` They sum to ${sum.toFixed(1)} H: some signals may overlap the solvent or be exchangeable (OH/NH).`
  if (impurities.length) note += ` Left out as residual solvent: ${impurities.map(i => i.short).join(', ')}.`
  return { regions: regions.sort((p, q) => q.a - p.a), refIdx: regions.length - 1 - refIdx, refValue, note, impurities }
}

/** 13C (and other non-1H) peak list above the noise, solvent signals removed. With `negative`, lines pointing down
 *  (DEPT-135: CH₂; APT: CH and CH₃) are listed too, with their negative height (0.36.4 left them out). */
export function autoPeaks13C(s: Spectrum, snr = 6, opts: { negative?: boolean } = {}) {
  const noise = noiseLevel(s), base = baseline(s)
  let max = -Infinity, min = Infinity
  for (let i = 0; i < s.y.length; i++) { if (s.y[i] > max) max = s.y[i]; if (s.y[i] < min) min = s.y[i] }
  const cutY = base + snr * noise
  const solv = solventMarkers(s).map(m => m.ppm)
  const out: { x: number; y: number; neg?: boolean }[] = []
  const step = Math.abs(s.x[1] - s.x[0]) || 1e-3
  const w = Math.max(3, Math.round(0.05 / step)) // ±0.05 ppm neighbourhood for prominence
  for (let i = 2; i < s.y.length - 2; i++) {
    const v = s.y[i]
    if (v > cutY && v >= s.y[i - 1] && v > s.y[i + 1] && v >= s.y[i - 2] && v > s.y[i + 2] && !solv.some(c => Math.abs(s.x[i] - c) < 0.6)) {
      let lowL = v, lowR = v
      for (let k = Math.max(0, i - w); k < i; k++) lowL = Math.min(lowL, s.y[k])
      for (let k = i + 1; k <= Math.min(s.y.length - 1, i + w); k++) lowR = Math.min(lowR, s.y[k])
      if (v - Math.max(lowL, lowR) < snr * noise) continue // a shoulder on a bigger peak, not a peak
      if (!out.some(q => Math.abs(q.x - s.x[i]) < 0.05)) out.push({ x: s.x[i], y: v })
    }
  }
  if (opts.negative) {
    const top = out.reduce((m, p) => Math.max(m, p.y - base), 0)
    const down: { x: number; y: number; neg: boolean }[] = []
    for (let i = 2; i < s.y.length - 2; i++) {
      const v = s.y[i]
      if (!(v < base - snr * noise && v <= s.y[i - 1] && v < s.y[i + 1] && v <= s.y[i - 2] && v < s.y[i + 2]) || solv.some(c => Math.abs(s.x[i] - c) < 0.6)) continue
      let highL = v, highR = v
      for (let k = Math.max(0, i - w); k < i; k++) highL = Math.max(highL, s.y[k])
      for (let k = i + 1; k <= Math.min(s.y.length - 1, i + w); k++) highR = Math.max(highR, s.y[k])
      if (Math.min(highL, highR) - v < snr * noise) continue  // a shoulder, not a line
      // a real line, not the dip beside a big one (a phase error) nor a baseline roll: at least 5% of the tallest, and
      // not within 0.1 ppm of an upward line more than twice as tall
      if (base - v < 0.05 * top || out.some(p => Math.abs(p.x - s.x[i]) < 0.1 && p.y - base > 2 * (base - v))) continue
      if (!down.some(q => Math.abs(q.x - s.x[i]) < 0.05)) down.push({ x: s.x[i], y: v, neg: true })
    }
    out.push(...down)
  }
  // the viewer's threshold is a fraction of the visible range; express the same cut that way
  return { peaks: out, threshold: Math.min(0.5, Math.max(0.002, (cutY - min) / ((max - min) || 1))) }
}

/** Where the solvent residual actually is, if it's close to where it should be. */
export function findResidual(s: Spectrum): { label: string; expected: number; found: number } | null {
  const m = solventMarkers(s)[0]
  if (!m) return null
  const win = /13C/i.test(s.nucleus || '') ? 1.0 : 0.15
  const i0 = lowerBound(s.x, m.ppm - win), i1 = Math.min(lowerBound(s.x, m.ppm + win), s.x.length - 1)
  if (i1 - i0 < 3) return null
  const noise = noiseLevel(s)
  if (win < 1) {  // ¹H: the nearest line that stands out (not a noise wiggle on the residual's own tail), centred if a multiplet
    const i = nearestLine(s, m.ppm, win, m.label)
    return i < 0 ? null : { label: m.label, expected: m.ppm, found: s.x[i] }
  }
  let best = -1
  for (let i = i0 + 1; i < i1; i++) {
    if (s.y[i] > s.y[i - 1] && s.y[i] >= s.y[i + 1] && (best < 0 || Math.abs(s.x[i] - m.ppm) < Math.abs(s.x[best] - m.ppm))) {
      if (s.y[i] > 10 * noise) best = i
    }
  }
  return best < 0 ? null : { label: m.label, expected: m.ppm, found: s.x[best] }
}

// ---------------------------------------------------------------- residual solvents & common impurities
// ¹H shifts from Fulmer et al., Organometallics 2010, 29, 2176 (values vary ±0.02–0.05 ppm with concentration)

interface ImpSignal { ppm: number; mult: 's' | 'd' | 't' | 'q' | 'sept' | 'm'; nH: number }
export interface ImpurityDef { name: string; short: string; mw: number | null; signals: ImpSignal[] }

const S = (ppm: number, nH: number, mult: ImpSignal['mult'] = 's'): ImpSignal => ({ ppm, mult, nH })
const IMP: Record<string, ImpurityDef[]> = {
  CDCL3: [
    { name: 'ethyl acetate', short: 'EtOAc', mw: 88.11, signals: [S(2.05, 3), S(4.12, 2, 'q'), S(1.26, 3, 't')] },
    { name: 'diethyl ether', short: 'Et₂O', mw: 74.12, signals: [S(3.48, 4, 'q'), S(1.21, 6, 't')] },
    { name: 'tetrahydrofuran', short: 'THF', mw: 72.11, signals: [S(3.76, 4, 'm'), S(1.85, 4, 'm')] },
    { name: 'triethylamine', short: 'Et₃N', mw: 101.19, signals: [S(2.53, 6, 'q'), S(1.03, 9, 't')] },
    { name: 'ethanol', short: 'EtOH', mw: 46.07, signals: [S(3.72, 2, 'q'), S(1.25, 3, 't')] },
    { name: '2-propanol', short: 'iPrOH', mw: 60.10, signals: [S(4.04, 1, 'sept'), S(1.22, 6, 'd')] },
    { name: 'tert-butyl methyl ether', short: 'MTBE', mw: 88.15, signals: [S(3.22, 3), S(1.19, 9)] },
    { name: 'N,N-dimethylformamide', short: 'DMF', mw: 73.09, signals: [S(8.02, 1), S(2.96, 3), S(2.88, 3)] },
    { name: 'toluene', short: 'toluene', mw: 92.14, signals: [S(2.36, 3), S(7.17, 3, 'm'), S(7.25, 2, 'm')] },
    { name: 'hexane', short: 'hexane', mw: 86.18, signals: [S(0.88, 6, 't'), S(1.26, 8, 'm')] },
    { name: 'acetone', short: 'acetone', mw: 58.08, signals: [S(2.17, 6)] },
    { name: 'dichloromethane', short: 'CH₂Cl₂', mw: 84.93, signals: [S(5.30, 2)] },
    { name: 'methanol', short: 'MeOH', mw: 32.04, signals: [S(3.49, 3)] },
    { name: 'acetonitrile', short: 'MeCN', mw: 41.05, signals: [S(2.10, 3)] },
    { name: 'dimethyl sulfoxide', short: 'DMSO', mw: 78.13, signals: [S(2.62, 6)] },
    { name: '1,4-dioxane', short: 'dioxane', mw: 88.11, signals: [S(3.71, 8)] },
    { name: 'cyclohexane', short: 'cyclohexane', mw: 84.16, signals: [S(1.43, 12)] },
    { name: 'benzene', short: 'benzene', mw: 78.11, signals: [S(7.36, 6)] },
    { name: 'silicone grease', short: 'grease (Si)', mw: null, signals: [S(0.07, 6)] },
  ],
  DMSO: [
    { name: 'ethyl acetate', short: 'EtOAc', mw: 88.11, signals: [S(1.99, 3), S(4.03, 2, 'q'), S(1.17, 3, 't')] },
    { name: 'diethyl ether', short: 'Et₂O', mw: 74.12, signals: [S(3.38, 4, 'q'), S(1.09, 6, 't')] },
    { name: 'tetrahydrofuran', short: 'THF', mw: 72.11, signals: [S(3.60, 4, 'm'), S(1.76, 4, 'm')] },
    { name: 'triethylamine', short: 'Et₃N', mw: 101.19, signals: [S(2.43, 6, 'q'), S(0.93, 9, 't')] },
    { name: 'ethanol', short: 'EtOH', mw: 46.07, signals: [S(3.44, 2, 'q'), S(1.06, 3, 't')] },
    { name: '2-propanol', short: 'iPrOH', mw: 60.10, signals: [S(3.78, 1, 'sept'), S(1.04, 6, 'd')] },
    { name: 'N,N-dimethylformamide', short: 'DMF', mw: 73.09, signals: [S(7.95, 1), S(2.89, 3), S(2.73, 3)] },
    { name: 'hexane', short: 'hexane', mw: 86.18, signals: [S(0.86, 6, 't'), S(1.25, 8, 'm')] },
    { name: 'toluene', short: 'toluene', mw: 92.14, signals: [S(2.30, 3), S(7.18, 3, 'm'), S(7.25, 2, 'm')] },
    { name: 'acetone', short: 'acetone', mw: 58.08, signals: [S(2.09, 6)] },
    { name: 'dichloromethane', short: 'CH₂Cl₂', mw: 84.93, signals: [S(5.76, 2)] },
    { name: 'methanol', short: 'MeOH', mw: 32.04, signals: [S(3.16, 3)] },
    { name: 'acetonitrile', short: 'MeCN', mw: 41.05, signals: [S(2.07, 3)] },
    { name: 'chloroform', short: 'CHCl₃', mw: 119.38, signals: [S(8.32, 1)] },
    { name: '1,4-dioxane', short: 'dioxane', mw: 88.11, signals: [S(3.57, 8)] },
  ],
  CD3OD: [
    { name: 'ethyl acetate', short: 'EtOAc', mw: 88.11, signals: [S(2.01, 3), S(4.09, 2, 'q'), S(1.24, 3, 't')] },
    { name: 'diethyl ether', short: 'Et₂O', mw: 74.12, signals: [S(3.49, 4, 'q'), S(1.18, 6, 't')] },
    { name: 'tetrahydrofuran', short: 'THF', mw: 72.11, signals: [S(3.71, 4, 'm'), S(1.87, 4, 'm')] },
    { name: 'triethylamine', short: 'Et₃N', mw: 101.19, signals: [S(2.58, 6, 'q'), S(1.05, 9, 't')] },
    { name: 'ethanol', short: 'EtOH', mw: 46.07, signals: [S(3.60, 2, 'q'), S(1.19, 3, 't')] },
    { name: '2-propanol', short: 'iPrOH', mw: 60.10, signals: [S(3.92, 1, 'sept'), S(1.15, 6, 'd')] },
    { name: 'N,N-dimethylformamide', short: 'DMF', mw: 73.09, signals: [S(7.97, 1), S(2.99, 3), S(2.86, 3)] },
    { name: 'hexane', short: 'hexane', mw: 86.18, signals: [S(0.90, 6, 't'), S(1.29, 8, 'm')] },
    { name: 'toluene', short: 'toluene', mw: 92.14, signals: [S(2.32, 3), S(7.16, 3, 'm'), S(7.23, 2, 'm')] },
    { name: 'acetone', short: 'acetone', mw: 58.08, signals: [S(2.15, 6)] },
    { name: 'dichloromethane', short: 'CH₂Cl₂', mw: 84.93, signals: [S(5.49, 2)] },
    { name: 'acetonitrile', short: 'MeCN', mw: 41.05, signals: [S(2.03, 3)] },
    { name: 'chloroform', short: 'CHCl₃', mw: 119.38, signals: [S(7.90, 1)] },
    { name: '1,4-dioxane', short: 'dioxane', mw: 88.11, signals: [S(3.66, 8)] },
  ],
}

export function impurityTable(s: Spectrum): ImpurityDef[] {
  if (s.kind !== 'nmr' || /13C/i.test(s.nucleus || '')) return []
  return IMP[solventKey(s.solvent)] || []
}

const LINES: Record<ImpSignal['mult'], number> = { s: 1, d: 2, t: 3, q: 4, sept: 7, m: 1 }

/** Half-width (ppm) of a solvent signal's window, assuming J ≈ 7 Hz. */
function halfWidth(sig: ImpSignal, freq: number) {
  if (sig.mult === 'm') return 0.06
  return ((LINES[sig.mult] - 1) * 7 / 2 + 3.5) / freq
}

export interface FoundImpurity {
  def: ImpurityDef
  windows: { lo: number; hi: number; nH: number; mult: ImpSignal['mult'] }[] // where its signals actually are
  matched: number                                  // how many of its signals were seen
  confident: boolean                               // multi-signal match (vs a lone singlet)
}

/** Candidate impurities: signals present (±0.04 ppm of the table, after `shift`). */
export function findImpurityCandidates(s: Spectrum, shift = 0): FoundImpurity[] {
  const table = impurityTable(s)
  if (!table.length) return []
  const freq = frequencyOf(s) || NOMINAL_MHZ
  const noise = noiseLevel(s), base = baseline(s)
  const out: FoundImpurity[] = []
  for (const def of table) {
    const windows: FoundImpurity['windows'] = []
    for (const sig of def.signals) {
      const centre = sig.ppm + shift
      const i0 = lowerBound(s.x, centre - 0.035), i1 = Math.min(lowerBound(s.x, centre + 0.035), s.x.length - 1)
      let best = -1
      for (let i = Math.max(1, i0); i < i1; i++) {
        if (s.y[i] > base + 10 * noise && s.y[i] >= s.y[i - 1] && s.y[i] > s.y[i + 1] && (best < 0 || s.y[i] > s.y[best])) best = i
      }
      if (best < 0) continue
      // must be a real peak top, not a point on the flank of a bigger neighbour
      const near = (hz: number) => { const j0 = lowerBound(s.x, s.x[best] - hz / freq), j1 = Math.min(lowerBound(s.x, s.x[best] + hz / freq), s.x.length - 1); let m = -Infinity; for (let j = j0; j <= j1; j++) m = Math.max(m, s.y[j]); return m }
      if (near(4) > s.y[best] * 1.001) continue
      // nor the outer line of a bigger multiplet that is mostly outside the window: a line much taller than it within
      // 12 Hz is that multiplet's (0.36.4 took ibuprofen's CH quartet edge at 3.75 and its CH at 1.87 for THF)
      if (near(12) > 1.5 * (s.y[best] - base) + base) continue
      // nor a ¹³C satellite: about 0.5% of a big line 50–90 Hz away (half of ¹J(CH), 100–180 Hz)
      const parent = Math.max(...[-1, 1].map(d => { const a = s.x[best] + d * 50 / freq, b = s.x[best] + d * 90 / freq; let m = -Infinity; for (let j = lowerBound(s.x, Math.min(a, b)); j <= Math.min(lowerBound(s.x, Math.max(a, b)), s.x.length - 1); j++) m = Math.max(m, s.y[j]); return m }))
      if (parent - base > 40 * (s.y[best] - base)) continue
      const hw = halfWidth(sig, freq)
      if (def.signals.length === 1) {
        // a lone solvent singlet stands apart: the baseline returns on both sides within ~10 Hz
        const quiet = (from: number, to: number) => { const j0 = lowerBound(s.x, from), j1 = Math.min(lowerBound(s.x, to), s.x.length - 1); let m = -Infinity; for (let j = j0; j <= j1; j++) m = Math.max(m, s.y[j]); return m < base + 0.2 * (s.y[best] - base) + 6 * noise }
        if (!quiet(s.x[best] - 14 / freq, s.x[best] - 6 / freq) || !quiet(s.x[best] + 6 / freq, s.x[best] + 14 / freq)) continue
      }
      // for a multiplet, the tallest line may be off-centre; re-centre on the lines found nearby
      let lo = s.x[best] - hw, hi = s.x[best] + hw
      if (LINES[sig.mult] > 1) {
        const lines = pickPeaks(s, lo - hw, hi + hw, 0.15, 12).filter(p => p.y > base + 6 * noise)
        if (lines.length < 2) continue // a lone line where a triplet/quartet should be: not this solvent
        const xs = lines.map(p => p.x).sort((a, b) => a - b)
        const c = (xs[0] + xs[xs.length - 1]) / 2
        lo = c - hw; hi = c + hw
      }
      windows.push({ lo, hi, nH: sig.nH, mult: sig.mult })
    }
    // a solvent known only by multiplets (THF): each must be a signal of its own, back at the baseline just outside its
    // window; a multiplet of the compound's that runs on past it (ibuprofen's CH quartet and CH₂/CH band at 60 MHz)
    // isn't THF
    if (def.signals.every(g => g.mult === 'm') && !windows.every(w => {
      const j0 = lowerBound(s.x, w.lo), j1 = Math.min(lowerBound(s.x, w.hi), s.x.length - 1)
      let top = -Infinity; for (let j = j0; j <= j1; j++) top = Math.max(top, s.y[j])
      const quiet = (from: number, to: number) => { let m = -Infinity; for (let j = lowerBound(s.x, from); j <= Math.min(lowerBound(s.x, to), s.x.length - 1); j++) m = Math.max(m, s.y[j]); return m < base + 0.2 * (top - base) + 6 * noise }
      return quiet(w.lo - 0.03, w.lo) && quiet(w.hi, w.hi + 0.03)
    })) continue
    const need = def.signals.length === 1 ? 1 : 2
    if (windows.length >= need) out.push({ def, windows, matched: windows.length, confident: def.signals.length > 1 })
  }
  return out
}
