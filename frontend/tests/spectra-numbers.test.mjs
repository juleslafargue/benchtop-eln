// 0.36.5: spectra that gave wrong numbers without a word. A damaged or ambiguous file now gets a plain message (or a
// visible warning where the numbers are still right); a ¹H file without a spectrometer frequency gets no J instead of
// J worked out at 400 MHz; auto-analysis no longer cuts one line of a doublet as water or calls the compound's own
// signals THF; small hostile files no longer freeze the page. Real files are in tests/data/nmr (see its README).
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'
import { gunzipSync } from 'node:zlib'
import { zipSync, strToU8 } from 'fflate'
import { loadSpectra } from './bundle.mjs'

let sp, draw
before(async () => {
  sp = await loadSpectra()
  // spectrumDraw.ts (restoring a saved analysis, drawing) bundled the same way
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-draw-'))
  await build({ stdin: { contents: "export * from './spectrumDraw'", resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile: join(dir, 'draw.mjs'), logLevel: 'error' })
  draw = await import(pathToFileURL(join(dir, 'draw.mjs')).href)
  rmSync(dir, { recursive: true, force: true })
})

const data = name => { const b = readFileSync(new URL(`./data/nmr/${name}`, import.meta.url)); return name.endsWith('.gz') ? gunzipSync(b) : b }
const buf = b => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)
const open = (name, opts) => sp.parseSpectrumBuffer(buf(data(name)), name.replace(/\.gz$/, ''), opts)
const text = (t, name = 't.jdx') => sp.parseSpectrumBuffer(buf(Buffer.from(t)), name)
const top = s => sp.pickPeaks(s, s.x[0], s.x[s.x.length - 1], 0.5, 1)[0].x
const hdr = (n, extra = '') => `##TITLE=t\n##JCAMP-DX=5.01\n##DATA TYPE=NMR SPECTRUM\n##.OBSERVE FREQUENCY=400\n##XUNITS=PPM\n##YUNITS=ARBITRARY\n##XFACTOR=1\n##YFACTOR=1\n##FIRSTX=10\n##LASTX=0\n${n ? `##NPOINTS=${n}\n` : ''}${extra}##XYDATA=(X++(Y..Y))\n`
// a Gaussian line at point 300 of 1000 on a 10..0 ppm axis: 7.0 ppm; written ten values a line, each line with its X
const ys = Array.from({ length: 1000 }, (_, i) => Math.round(1000 * Math.exp(-(((i - 300) / 20) ** 2))))
const affn = (lines = 100) => Array.from({ length: lines }, (_, k) => `${(10 - 10 * k * 10 / 999).toFixed(5)} ${ys.slice(k * 10, k * 10 + 10).join(' ')}`).join('\n') + '\n'

// ---------------------------------------------------------------- N1: cut-off JCAMP-DX files

test('a JCAMP-DX file cut off part-way is refused plainly (regression: drawn stretched over the whole axis, every peak at the wrong ppm)', async () => {
  const real = data('indometacin_1h.dx.gz').toString('latin1')
  assert.ok(sp.pickPeaks(await text(real), -5, 17, 0.05, 6).some(p => Math.abs(p.x - 7.669) < 0.01))
  await assert.rejects(text(real.slice(0, Math.floor(real.length * 0.9))), /looks cut off: it says it has 32,768 points but only 28,869 are there, so its ppm scale can't be trusted/)
  await assert.rejects(text(hdr(1000) + affn(50) + '##END=\n'), /cut off: it says it has 1,000 points but only 500/)
  await assert.rejects(text(hdr(500) + affn() + '##END=\n'), /says it has 500 points but holds 1,000/)
  // TopSpin's NTUPLES export, cut off: its VAR_DIM is checked the same way
  const nt = data('aspirin_1h_topspin_ntuples.dx.gz').toString('latin1')
  await assert.rejects(text(nt.slice(0, Math.floor(nt.length * 0.3))), /cut off|damaged/)
})

test('a line missing in the middle is found from the X values each line starts with, with or without NPOINTS', async () => {
  const lines = affn().split('\n'); lines.splice(40, 5)
  await assert.rejects(text(hdr(1000) + lines.join('\n') + '##END=\n'), /cut off/)
  await assert.rejects(text(hdr(0) + lines.join('\n') + '##END=\n'), /damaged: the X values written in its data .* don't fit the axis its header gives/)
  // DIF data: each line starts by repeating the last value of the line before (the Y check)
  await assert.rejects(text(hdr(8) + '10A0J0J0J0\n5I9J0J0J0\n##END=\n'), /doesn't start where the line before ended/)
})

test('without FIRSTX/LASTX the axis comes from the X values in the data (regression: the last line\'s X was taken for the last point\'s)', async () => {
  const s = await text(hdr(1000).replace(/##FIRSTX.*\n##LASTX.*\n/, '') + affn() + '##END=\n')
  assert.ok(Math.abs(s.x[0]) < 1e-6 && Math.abs(s.x[s.x.length - 1] - 10) < 1e-6, `${s.x[0]}..${s.x[s.x.length - 1]}`)
  assert.ok(Math.abs(top(s) - 7.0) < 0.02, `${top(s)}`)
})

test('real files whose X checks or point counts follow their own conventions still read (Varian: NPOINTS one too many, point numbers as X; Bruker: point numbers; Oxford: X not divided by its factor)', async () => {
  for (const f of ['varian_1h.jdx.gz', 'indometacin_1h.dx.gz', 'oxford_ethylcrotonate_1h_fid.jdx.gz', 'jeol_1h.dx.gz', 'cytisine_1h_mestrenova_blocks.jdx.gz', 'aspirin_1h_topspin_ntuples.dx.gz', 'nanalysis_ibuprofen_1h_fid.jdx.gz', 'aspirin_1h_fid.dx.gz']) {
    const s = await open(f)
    assert.ok(s.x.length > 1000 && !s.warnings, f)
  }
})

// ---------------------------------------------------------------- N2: text tables

test('CSV with decimal commas, semicolons or tabs reads the right numbers (regression: "7,26;1234,5" was x = 7, y = 26)', async () => {
  const first = async (t, name = 'data.csv') => { const s = await text(t, name); return [Array.from(s.x), Array.from(s.y)] }
  assert.deepEqual(await first('ppm;intensité\n7,26;1234,5\n7,27;2000,25\n7,28;1500,75\n'), [[7.26, 7.27, 7.28], [1234.5, 2000.25, 1500.75]])
  assert.deepEqual(await first('1,5\t10,25\n2,5\t20,5\n3,5\t30,75\n'), [[1.5, 2.5, 3.5], [10.25, 20.5, 30.75]])
  assert.deepEqual(await first('1,5 10,25\n2,5 20,5\n3,5 30,75\n'), [[1.5, 2.5, 3.5], [10.25, 20.5, 30.75]])
  assert.deepEqual(await first('1.5,10\n2.5,20\n3.5,30\n'), [[1.5, 2.5, 3.5], [10, 20, 30]])
  assert.deepEqual(await first('x,y\n1.0,"1,234.5"\n2.0,"2,345.5"\n'), [[1, 2], [1234.5, 2345.5]])
  assert.deepEqual(await first('﻿1\t10\n2\t20\n3\t30\n'), [[1, 2, 3], [10, 20, 30]])
  assert.deepEqual(await first('1,10\r2,20\r3,30\r'), [[1, 2, 3], [10, 20, 30]], 'old Mac line ends')
  const u16 = await sp.parseSpectrumBuffer(buf(Buffer.from('﻿1\t10\n2\t20\n3\t30\n', 'utf16le')), 'excel unicode.txt')
  assert.deepEqual(Array.from(u16.y), [10, 20, 30])
})

test('three or more columns: x and y by the header\'s names, or a row-number column skipped, and said; otherwise a plain message (regression: drew column 1 against column 2)', async () => {
  const idx = await text('1,7.1,100\n2,7.2,200\n3,7.3,300\n', 'data.csv')
  assert.deepEqual([Array.from(idx.x), Array.from(idx.y)], [[7.1, 7.2, 7.3], [100, 200, 300]])
  assert.match(idx.warnings.join(' '), /3 columns: Benchtop drew column 3 against column 2 \(the first column looks like row numbers\)/)
  const named = await text('time,ppm,intensity\n5,7.1,100\n9,7.2,200\n12,7.3,300\n', 'data.csv')
  assert.deepEqual(Array.from(named.y), [100, 200, 300])
  assert.match(named.warnings.join(' '), /drew “intensity” against “ppm”/)
  await assert.rejects(text('time,254nm,280nm\n0.0,1,9\n0.1,2,8\n0.2,3,7\n', 'dad.csv'), /3 columns of numbers \(“time”, “254nm”, “280nm”\), and Benchtop can't tell which are x and y/)
})

test('x that goes up and down is refused plainly (regression: integrated out of order, 35 instead of 120)', async () => {
  await assert.rejects(text('3,30\n1,10\n2,20\n5,50\n4,40\n', 'data.csv'), /go up and down/)
  await assert.rejects(text('1,10\n1,20\n1,30\n', 'data.csv'), /Every x value in this file is the same/)
  const desc = await text('5,50\n4,40\n3,30\n2,20\n1,10\n', 'data.csv')  // falling is fine: stored rising
  assert.equal(sp.integrate(desc, 1, 5), 120)
})

test('a PDF or picture renamed .csv is not a spectrum; a JCAMP-DX file with text before its header reads as JCAMP-DX (regression: a 3-point "spectrum"; every 10th point as a table)', async () => {
  await assert.rejects(text('%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n2 0 obj\n3 4\n', 'report.csv'), /isn't a spectrum/)
  await assert.rejects(sp.parseSpectrumBuffer(buf(Buffer.from([0x89, 0x50, 0x4e, 0x47, 13, 10, 26, 10, ...Array.from({ length: 500 }, (_, i) => i % 256)])), 'x.csv'), /isn't a spectrum/)
  for (const before of ['From: nmr@lab\n\n', '$$ comment\n'.repeat(3000)]) {
    const s = await text(before + hdr(1000) + affn() + '##END=\n', 'mail.txt')
    assert.equal(s.x.length, 1000); assert.ok(Math.abs(top(s) - 7.0) < 0.02)
    assert.equal(!!s.warnings?.some(w => /Text before the JCAMP-DX header was left out/.test(w)), before.startsWith('From'))
  }
  const nan = await text('1,5\n2,NaN\n3,7\n4,8\n5,9\n', 'data.csv')
  assert.deepEqual(Array.from(nan.x), [1, 3, 4, 5]); assert.match(nan.warnings.join(' '), /1 row has a value that isn't a number/)
})

// ---------------------------------------------------------------- N3: no spectrometer frequency

/** an ethyl group at `freq` MHz (quartet 4.12, triplet 1.25, J 7.1 Hz), as a spectrum whose file doesn't say the frequency */
function ethyl(freq) {
  const n = 16384, x = new Float64Array(n), y = new Float64Array(n), w = 0.6 / freq / 2
  for (let i = 0; i < n; i++) x[i] = -1 + 13 * i / (n - 1)
  for (const [ppm, js, a] of [[4.12, [7.1, 7.1, 7.1], 2], [1.25, [7.1, 7.1], 3]]) {
    let ls = [[0, 1]]; for (const j of js) ls = ls.flatMap(([o, h]) => [[o - j / 2, h / 2], [o + j / 2, h / 2]])
    for (const [o, h] of ls) { const c = ppm + o / freq; for (let i = 0; i < n; i++) { const d = x[i] - c; y[i] += a * h * w * w / (d * d + w * w) / w } }
  }
  return { title: 'ethyl 1H nmr 60MHz', x, y, xLabel: 'δ (ppm)', yLabel: '', kind: 'nmr', reversed: true, dips: false, nucleus: '1H', isStick: false }
}

test('a ¹H file that doesn\'t give the frequency gets no J and no automatic analysis, never J worked out at 400 MHz (regression: "q, J = 47.3 Hz" for a 7.1 Hz quartet at 60 MHz)', () => {
  const s = ethyl(60)
  const m = sp.analyzeMultiplet(s, 3.8, 4.45)
  assert.equal(m.mult, 'q'); assert.deepEqual(m.j, []); assert.equal(m.noJ, true)
  assert.equal(sp.formatSignal(m, 2), '4.12 (q, 2H)')
  const r = sp.autoAnalyse1H(s, null)
  assert.equal(r.needsFrequency, true); assert.equal(r.regions.length, 0); assert.match(r.note, /doesn't say the spectrometer frequency/)
  // typed in: the right J, and the automatic analysis
  const typed = draw.withFrequency(s, 60, false)
  assert.ok(Math.abs(sp.analyzeMultiplet(typed, 3.8, 4.45).j[0] - 7.1) < 0.2)
  assert.equal(draw.header(typed), '¹H NMR (60 MHz)')
  assert.equal(sp.autoAnalyse1H(typed, null).regions.length, 2)
  assert.equal(draw.frequencyHint(s, 'ethyl 1H nmr 60MHz.csv'), 60)
  assert.equal(draw.frequencyHint(s, 'ethyl 400 MHz.csv'), 400)
  assert.equal(draw.frequencyHint({ ...s, title: 'x' }, 'sample 12.csv'), null)
})

test('an analysis saved before 0.36.5 on such a file shows what it showed, and says so; one saved since keeps the frequency typed in', () => {
  const s = ethyl(60)
  const old = { integrals: [[3.8, 4.45], [1.0, 1.5]], ref_idx: 0, ref_value: 2, threshold: 0.05, labels: [{ mult: '', j: '' }, { mult: '', j: '' }], offset: 0 }
  const r = draw.restoreAnalysis(s, old)
  assert.equal(r.legacy400, true); assert.equal(r.frequency, null)
  const asBefore = draw.withFrequency(s, r.frequency, r.legacy400)
  assert.equal(asBefore.assumedFrequency, 400); assert.equal(draw.header(asBefore), '¹H NMR (? MHz)')  // still not claimed as the frequency
  assert.ok(Math.abs(sp.analyzeMultiplet(asBefore, 3.8, 4.45).j[0] - 47.3) < 0.5, 'the J it was saved with')
  const since = draw.restoreAnalysis(s, { ...old, frequency: 60 })
  assert.equal(since.legacy400, false); assert.equal(since.frequency, 60)
  assert.equal(draw.restoreAnalysis(s, { ...old, frequency: null }).legacy400, false)
  // a file that gives its frequency is never affected
  assert.equal(draw.restoreAnalysis({ ...s, frequency: 60 }, old).legacy400, false)
})

// ---------------------------------------------------------------- N4: water and residual solvents at ordinary signal-to-noise

const noisy = (s, k) => {
  let seed = 7; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647)
  let m = -Infinity; for (const v of s.y) if (v > m) m = v
  return { ...s, y: Float64Array.from(s.y, v => { let u = 0; for (let i = 0; i < 6; i++) u += rnd(); return v + k * m * (u - 3) * Math.SQRT2 }) }
}

test('one line of a doublet is never cut as water, nor the compound\'s own signals called THF (regression: ibuprofen\'s CH₃ doublet came out as "1.52 s" with half its area, and "THF 13 mol%")', async () => {
  const ibu = await open('ibuprofen_1h_bruker_fid.zip')
  for (const k of [0, 0.0005, 0.001]) {
    const s = k ? noisy(ibu, k) : ibu
    const res = sp.findResidual(s), shift = res.found - res.expected
    const r = sp.autoAnalyse1H(s, null, shift)
    assert.deepEqual(r.impurities.map(i => i.short).filter(i => i !== 'grease (Si)'), [], `noise ${k}`)
    const unit = sp.integrate(s, r.regions[r.refIdx].a, r.regions[r.refIdx].b) / r.refValue
    const ch3 = r.regions.find(g => Math.abs((g.a + g.b) / 2 - 1.52) < 0.04)
    assert.ok(ch3, `noise ${k}: the CH₃ doublet is a signal`)
    assert.equal(sp.analyzeMultiplet(s, ch3.a, ch3.b).mult, 'd', `noise ${k}`)
    assert.ok(Math.abs(sp.integrate(s, ch3.a, ch3.b) / unit - 3) < 0.2, `noise ${k}: 3 H`)
    for (const c of sp.solventCuts(s, shift)) assert.ok(c.hi < 1.50 || c.lo > 1.545, `noise ${k}: nothing cut out of the doublet ${c.lo}–${c.hi}`)
  }
})

test('real THF and water are still found and left out', () => {
  const freq = 400, n = 131072, x = new Float64Array(n), y = new Float64Array(n)
  for (let i = 0; i < n; i++) x[i] = -1 + 13 * i / (n - 1)
  const lor = (c, h, wHz = 1.2) => { const w = wHz / freq / 2; for (let i = 0; i < n; i++) { const d = x[i] - c; y[i] += h * w * w / (d * d + w * w) } }
  lor(7.26, 5); lor(1.56, 4, 3)  // CHCl₃, water
  for (const c of [7.30, 7.30 + 8 / freq]) lor(c, 20); lor(3.40, 60); lor(2.30, 60)  // the compound: a doublet, two CH₃ singlets
  for (const [c, h] of [[3.76 - 3 / freq, 2], [3.76, 3], [3.76 + 3 / freq, 2], [1.85 - 3 / freq, 2], [1.85, 3], [1.85 + 3 / freq, 2]]) lor(c, h)  // THF
  const s = { title: 'sim', x, y, xLabel: 'ppm', yLabel: '', kind: 'nmr', reversed: true, dips: false, nucleus: '1H', frequency: freq, solvent: 'CDCl3', isStick: false }
  const r = sp.autoAnalyse1H(s, null)
  assert.deepEqual(r.impurities.map(i => i.short), ['THF'])
  const centres = r.regions.map(g => (g.a + g.b) / 2)
  assert.ok(!centres.some(c => Math.abs(c - 1.56) < 0.05), `water: ${centres.map(c => c.toFixed(2))}`)
})

// ---------------------------------------------------------------- N5: small files that would freeze the page

/** a zip whose listing says an entry unpacks to `size` bytes (it doesn't): what a zip bomb's listing looks like */
function claimSize(z, name, size) {
  const b = Buffer.from(z), sig = Buffer.from([0x50, 0x4b, 0x01, 0x02])
  for (let i = b.indexOf(sig); i >= 0; i = b.indexOf(sig, i + 4)) {
    const len = b.readUInt16LE(i + 28)
    if (b.subarray(i + 46, i + 46 + len).toString() === name) b.writeUInt32LE(size, i + 24)
  }
  return buf(b)
}
const acqus = strToU8('##TITLE= t\n##$TD= 16384\n##$SW_h= 8000\n##$BF1= 400.13\n##$SFO1= 400.132\n##$O1= 2000\n##$NUC1= <1H>\n##$DTYPA= 0\n##$BYTORDA= 0\n##$PARMODE= 0\n##$GRPDLY= 0\n##END=\n')
function fid(points = 8192) {
  const f = new Uint8Array(points * 8), v = new DataView(f.buffer)
  for (let i = 0; i < points; i++) { const t = i / 8000, a = 1e6 * Math.exp(-3 * t), ph = 2 * Math.PI * (1.0 * 400.13 - 2000) * t; v.setInt32(8 * i, Math.round(a * Math.cos(ph)), true); v.setInt32(8 * i + 4, Math.round(a * Math.sin(ph)), true) }
  return f
}

test('a zip entry is only unpacked when it is needed, and never past what its listing says (regression: a 1 MB zip holding 1 GB ran the page out of memory)', async () => {
  // a junk entry beside the experiment that claims to unpack to 1 GB is never unpacked
  const z = zipSync({ 'junk.bin': new Uint8Array(1000), '1/acqus': acqus, '1/fid': fid() })
  let t = performance.now()
  const s = await sp.parseSpectrumBuffer(claimSize(z, 'junk.bin', 2 ** 30), 'bomb.zip')
  assert.ok(s.fid && performance.now() - t < 3000)
  // the data itself claiming 1 GB: refused from the listing, before anything is unpacked
  const procs = strToU8('##$SI= 1024\n##$SF= 400.13\n##$OFFSET= 12\n##$SW_p= 6402\n')
  const p = zipSync({ '1/pdata/1/procs': procs, '1/pdata/1/1r': new Uint8Array(4096) })
  t = performance.now()
  await assert.rejects(sp.parseSpectrumBuffer(claimSize(p, '1/pdata/1/1r', 2 ** 30), 'bomb.zip'), /The 1r file in this zip unpacks to 1,074 MB, more than Benchtop opens/)
  await assert.rejects(sp.parseSpectrumBuffer(buf(zipSync({ '1/pdata/1/procs': strToU8('##$SI= 268435456\n##$SF= 400.13\n##$OFFSET= 12\n##$SW_p= 6402\n'), '1/pdata/1/1r': new Uint8Array(4096) })), 'bomb.zip'), /268,435,456 points: Benchtop opens spectra of up to 8,388,608/)
  assert.ok(performance.now() - t < 1000)
})

test('a huge point count is refused at once with a plain message (regression: 6–16 s frozen, then "Invalid array length")', async () => {
  const t = performance.now()
  await assert.rejects(text(hdr(8) + '10A0s99999999\n##END=\n'), /more than 8,388,608 points: Benchtop opens spectra of up to 8,388,608 points/)
  await assert.rejects(text(hdr(0) + Array.from({ length: 50 }, (_, i) => `${i}A0Z9999999`).join('\n') + '\n##END=\n'), /more than 8,388,608 points/)
  await assert.rejects(text(hdr(4e9) + '10 1 2 3 4 5 6 7 8 9 10\n##END=\n'), /4,000,000,000 points/)
  assert.ok(performance.now() - t < 1000, `${performance.now() - t} ms`)
  // nothing that reaches a person is a raw JavaScript or zip-library error
  await assert.rejects(sp.parseSpectrumBuffer(buf(Buffer.from([0x50, 0x4b, 3, 4, ...new Array(2000).fill(7)])), 't.zip'), /This zip file is damaged/)
})

test('the record page decides whether to offer "View spectrum" from a file\'s first 16 KB (regression: a 361-byte file froze the record page for 12 s on opening)', async () => {
  const bomb = buf(Buffer.from(hdr(0) + Array.from({ length: 20 }, (_, i) => `${i}A0Z9999999`).join('\n') + '\n##END=\n'))
  const t = performance.now()
  assert.equal(await sp.looksLikeSpectrum(bomb, 'dup bomb.txt'), true)  // offered; clicking View gives the plain message
  assert.ok(performance.now() - t < 200)
  assert.equal(await sp.looksLikeSpectrum(buf(Buffer.from('ppm,intensity\n' + Array.from({ length: 5000 }, (_, i) => `${i / 100},${i % 7}`).join('\n'))), 'trace.csv'), true)
  assert.equal(await sp.looksLikeSpectrum(buf(Buffer.from('Notes from the meeting\nTwo things to do\n')), 'notes.txt'), false)
})

test('a big spectrum is drawn from a summary of its points, not all 8 million on every move of the mouse (regression: the tab froze 31 s)', () => {
  const n = 8_000_000, x = new Float64Array(n), y = new Float64Array(n)
  for (let i = 0; i < n; i++) { x[i] = 10 * i / (n - 1); y[i] = i % 977 === 0 ? 1e5 : (i * 7) % 300 }
  const spec = { title: 'big', x, y, xLabel: 'δ (ppm)', yLabel: '', kind: 'nmr', reversed: true, dips: false, nucleus: '1H', frequency: 400, isStick: false }
  let lines = 0
  const g = new Proxy({}, { get: (_, k) => (k === 'lineTo' ? () => { lines++ } : k === 'measureText' ? () => ({ width: 10 }) : () => {}), set: () => true })
  const state = { spec, view: [0, 10], yScale: 1, withArea: [], refIdx: 0, refH: 1, peaks: [], showPeaks: false, markers: [], impurities: [], overlays: [], overlayMode: 'stack', signalTags: [], pred: null, showPred: false, drag: null, hover: null }
  draw.drawSpectrum(g, 1000, 420, draw.LIGHT, state)  // the first draw makes the summary
  lines = 0
  const t = performance.now()
  for (let k = 0; k < 10; k++) draw.drawSpectrum(g, 1000, 420, draw.LIGHT, state)
  assert.ok((performance.now() - t) / 10 < 30, `${((performance.now() - t) / 10).toFixed(1)} ms a draw`)
  assert.ok(lines / 10 < 5000, `${lines / 10} line segments a draw`)
  // zoomed in, every point is drawn as before
  lines = 0
  draw.drawSpectrum(g, 1000, 420, draw.LIGHT, { ...state, view: [5, 5.0005] })
  assert.ok(lines > 0)
})

// ---------------------------------------------------------------- N6: the small ones

test('old Mac line ends, XYDATA in (XY..XY) pairs, a peak table with widths, numbers that aren\'t', async () => {
  const s = await text((hdr(1000) + affn() + '##END=\n').replace(/\n/g, '\r'))
  assert.ok(Math.abs(top(s) - 7.0) < 0.02)
  const pairs = await text(hdr(5).replace('(X++(Y..Y))', '(XY..XY)') + '10, 1; 9, 2; 8, 50\n7, 2; 6, 1\n##END=\n')
  assert.deepEqual([Array.from(pairs.x), Array.from(pairs.y)], [[6, 7, 8, 9, 10], [1, 2, 50, 2, 1]])
  const xyw = await text('##TITLE=t\n##JCAMP-DX=5\n##DATA TYPE=MASS SPECTRUM\n##PEAK TABLE=(XYW..XYW)\n100, 50, 1\n200, 30, 1\n150, 99, 1\n##END=\n')
  assert.deepEqual([Array.from(xyw.x), Array.from(xyw.y)], [[100, 150, 200], [50, 99, 30]])
  await assert.rejects(text(hdr(10) + '10 1 NaN 3 inf -inf 6 7 8 9 10\n##END=\n'), /“NaN”, which isn't a number/)
  await assert.rejects(text(hdr(10) + '10 1 2 1e999 4 5 6 7 8 9 10\n##END=\n'), /“1e999”, which isn't a number/)
  await assert.rejects(text(hdr(20) + '10@A1B2!!~~C3#D4\n##END=\n'), /a character that doesn't belong in JCAMP-DX data \(“!”\)/)
  await assert.rejects(text(hdr(10).replace('##FIRSTX=10', '##FIRSTX=abc') + '10 1 2 3 4 5 6 7 8 9 10\n##END=\n'), /doesn't give its x range/)
  await assert.rejects(text(hdr(0) + '##END=\n'), /no data points/)
})

test('Bruker zips: upper-case names and Windows back-slashes are found; a fid shorter than recorded warns; a procs file without SF says so', async () => {
  for (const files of [{ '1/ACQUS': acqus, '1/FID': fid() }, { '1\\acqus': acqus, '1\\fid': fid() }]) {
    const s = await sp.parseSpectrumBuffer(buf(zipSync(files)), 't.zip')
    assert.ok(s.fid && !s.warnings, Object.keys(files).join())
  }
  const short = await sp.parseSpectrumBuffer(buf(zipSync({ '1/acqus': acqus, '1/fid': fid(4096) })), 't.zip')
  assert.match(short.warnings.join(' '), /The fid file holds 8,192 of the 16,384 points its acqus file says were recorded, so it may have been cut off/)
  const procs = strToU8('##$SI= 1024\n##$SF= 0\n##$OFFSET= 12\n##$SW_p= 6402\n')
  await assert.rejects(sp.parseSpectrumBuffer(buf(zipSync({ '1/pdata/1/procs': procs, '1/pdata/1/1r': new Uint8Array(4096) })), 't.zip'), /doesn't give the spectrometer frequency, spectral width and offset/)
})

test('negative ¹³C lines (DEPT, APT) are listed, marked as negative, in analyses made from 0.36.5 on', async () => {
  const freq = 100.6, n = 65536, x = new Float64Array(n), y = new Float64Array(n), w = 1.2 / freq / 2
  for (let i = 0; i < n; i++) x[i] = -20 + 250 * i / (n - 1)
  for (const [c, h] of [[128.5, 1], [55.2, 1], [30.0, -1], [14.1, 1]]) for (let i = 0; i < n; i++) { const d = x[i] - c; y[i] += h * w * w / (d * d + w * w) / w }
  let seed = 3; for (let i = 0; i < n; i++) { seed = (seed * 16807) % 2147483647; y[i] += 2 * (seed / 2147483647 - 0.5) }
  const s = { title: 'dept', x, y, xLabel: 'δ (ppm)', yLabel: '', kind: 'nmr', reversed: true, dips: false, nucleus: '13C', frequency: freq, solvent: 'CDCl3', isStick: false }
  assert.ok(!sp.autoPeaks13C(s, 6).peaks.some(p => Math.abs(p.x - 30) < 0.05), 'as before without the option')
  const peaks = sp.autoPeaks13C(s, 6, { negative: true }).peaks
  const neg = peaks.filter(p => p.neg)
  assert.equal(neg.length, 1); assert.ok(Math.abs(neg[0].x - 30) < 0.02)
  assert.equal(peaks.length, 4)
  // new analyses list them; one saved before (with a threshold and no "negatives") keeps its list
  assert.equal(draw.restoreAnalysis(s, null).negatives, true)
  assert.equal(draw.restoreAnalysis(s, { integrals: [], ref_idx: 0, ref_value: 1, threshold: 6 }).negatives, false)
})
