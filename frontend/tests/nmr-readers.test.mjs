// NMR file readers and raw FID processing: every JCAMP-DX encoding, TopSpin's NTUPLES export, multi-block files,
// benchtop spectrometers' FIDs (Nanalysis, Oxford Instruments), Bruker zips (raw, processed, 2D), Bruker's digital
// filter, and broken files. Real files are in tests/data/nmr (see its README); the rest are made here with known answers.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { before, test } from 'node:test'
import { gunzipSync } from 'node:zlib'
import { zipSync, strToU8 } from 'fflate'
import { loadSpectra } from './bundle.mjs'

let sp
before(async () => { sp = await loadSpectra() })

const data = name => {
  const b = readFileSync(new URL(`./data/nmr/${name}`, import.meta.url))
  return name.endsWith('.gz') ? gunzipSync(b) : b
}
const buf = b => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)
const open = (name, opts) => sp.parseSpectrumBuffer(buf(data(name)), name.replace(/\.gz$/, ''), opts)
const max = y => { let m = -Infinity; for (const v of y) if (v > m) m = v; return m }
const valueAt = (s, ppm) => s.y[Math.min(s.x.length - 1, sp.lowerBound(s.x, ppm))]
/** a local maximum above `frac` of the tallest line within `tol` ppm of each position */
const linesAt = (s, positions, tol = 0.02, frac = 0.05) => {
  const peaks = sp.pickPeaks(s, s.x[0], s.x[s.x.length - 1], frac, 400)
  return positions.filter(p => !peaks.some(q => Math.abs(q.x - p) <= tol))
}
const corr = (a, b) => { const n = a.length; let ma = 0, mb = 0; for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i] } ma /= n; mb /= n; let sa = 0, sb = 0, sab = 0; for (let i = 0; i < n; i++) { sa += (a[i] - ma) ** 2; sb += (b[i] - mb) ** 2; sab += (a[i] - ma) * (b[i] - mb) } return sab / Math.sqrt(sa * sb) }
const onGrid = (s, xs) => Array.from(xs, x => valueAt(s, x))

// ---------------------------------------------------------------- JCAMP-DX encodings, written here, read back exactly

const SQZ = '@ABCDEFGHI', SQZn = 'abcdefghi', DIF = '%JKLMNOPQR', DIFn = 'jklmnopqr', DUP = 'STUVWXYZs'
const sqz = v => (v >= 0 ? SQZ[+String(v)[0]] + String(v).slice(1) : SQZn[+String(-v)[0] - 1] + String(-v).slice(1))
const dif = d => (d >= 0 ? DIF[+String(d)[0]] + String(d).slice(1) : DIFn[+String(-d)[0] - 1] + String(-d).slice(1))
const dup = n => DUP[+String(n)[0] - 1] + String(n).slice(1)

/** DIF/DIFDUP with Y checks, written plainly: each line = x, first value (SQZ), differences (DIF), last value repeated next line */
function encodeDif(ys, withDup) {
  const out = []
  let start = 0
  while (start < ys.length - 1) {
    const end = Math.min(ys.length - 1, start + 9) // a line covers ys[start..end]; the next line starts again at ys[end]
    let s = `${start}${sqz(ys[start])}`
    const diffs = []
    for (let k = start + 1; k <= end; k++) diffs.push(ys[k] - ys[k - 1])
    for (let k = 0; k < diffs.length;) {
      let run = 1
      while (withDup && k + run < diffs.length && diffs[k + run] === diffs[k]) run++
      s += dif(diffs[k]) + (run > 1 ? dup(run) : '')
      k += run
    }
    out.push(s)
    start = end
  }
  return out
}

function jcamp(lines, n, extra = '') {
  return [`##TITLE=test`, `##JCAMP-DX=4.24`, `##DATA TYPE=NMR SPECTRUM`, `##.OBSERVE FREQUENCY=400.13`, `##.OBSERVE NUCLEUS=^1H`,
    `##XUNITS=PPM`, `##YUNITS=ARBITRARY UNITS`, `##XFACTOR=1`, `##YFACTOR=1`, `##FIRSTX=0`, `##LASTX=${n - 1}`, `##NPOINTS=${n}`, extra,
    `##XYDATA=(X++(Y..Y))`, ...lines, `##END=`].filter(Boolean).join('\n')
}

test('XYDATA in every JCAMP-DX encoding (AFFN, SQZ, DIF, DIFDUP) reads back exactly, Y checks included', () => {
  let seed = 3
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647)
  const ys = []
  for (let i = 0; i < 503; i++) ys.push(i % 50 < 10 ? 1234 : Math.round((rnd() - 0.4) * 1e6) * (i % 7 ? 1 : -1)) // runs, sign changes
  for (const [name, lines] of [['AFFN', ys.reduce((a, _, i) => (i % 10 ? a : [...a, `${i} ${ys.slice(i, i + 10).join(' ')}`]), [])],
    ['SQZ', ys.reduce((a, _, i) => (i % 10 ? a : [...a, `${i}${ys.slice(i, i + 10).map(sqz).join('')}`]), [])],
    ['DIF', encodeDif(ys, false)], ['DIFDUP', encodeDif(ys, true)]]) {
    const s = sp.parseJcamp(jcamp(lines, ys.length))
    assert.equal(s.y.length, ys.length, name)
    assert.deepEqual(Array.from(s.y), ys, name)
    assert.equal(s.x[0], 0); assert.equal(s.x[s.x.length - 1], ys.length - 1)
  }
})

test('a JCAMP-DX "peak table" of thousands of evenly spaced points is a spectrum, a short one is a peak list', () => {
  const dense = Array.from({ length: 4000 }, (_, i) => `${(i * 0.001).toFixed(3)}, ${Math.round(1000 * Math.exp(-(((i - 2000) / 30) ** 2)))}`)
  const body = rows => `##TITLE=t\n##JCAMP-DX=4.24\n##DATA TYPE=NMR SPECTRUM\n##XUNITS=PPM\n##.OBSERVE FREQUENCY=600\n##PEAK TABLE=(XY..XY)\n${rows.join('\n')}\n##END=`
  assert.equal(sp.parseJcamp(body(dense)).isStick, false)
  assert.equal(sp.parseJcamp(body(['1.0, 100', '2.5, 40', '7.26, 20'])).isStick, true)
})

test('Hz axes use the file\'s own shift reference (Bruker counts from the right edge), or are absolute without one', () => {
  const head = ref => `##TITLE=t\n##JCAMP-DX=6.0\n##DATA TYPE=NMR SPECTRUM\n##.OBSERVE FREQUENCY=400\n${ref}\n##XUNITS=HZ\n##FIRSTX=4000\n##LASTX=0\n##NPOINTS=5\n##XYDATA=(X++(Y..Y))\n4000 1 2 3 4 5\n##END=`
  const withRef = sp.parseJcamp(head('##.SHIFT REFERENCE= INTERNAL, CDCl3, 1, 12.5'))
  assert.ok(Math.abs(withRef.x[withRef.x.length - 1] - 12.5) < 1e-9 && Math.abs(withRef.x[0] - 2.5) < 1e-9) // first point 12.5 ppm, 10 ppm wide
  const absolute = sp.parseJcamp(head(''))
  assert.ok(Math.abs(absolute.x[absolute.x.length - 1] - 10) < 1e-9 && Math.abs(absolute.x[0]) < 1e-9)
})

// ---------------------------------------------------------------- real files

test('TopSpin\'s JCAMP-DX export (NTUPLES, real and imaginary pages) reads like the processed Bruker spectrum (regression: refused)', async () => {
  const j = await open('aspirin_1h_topspin_ntuples.dx.gz'), z = await open('aspirin_1h_bruker_processed.zip')
  assert.equal(j.kind, 'nmr'); assert.equal(j.nucleus, '1H'); assert.equal(j.solvent, 'CDCl3'); assert.equal(j.fid, undefined)
  assert.ok(Math.abs(j.x[0] - z.x[0]) < 0.001 && Math.abs(j.x[j.x.length - 1] - z.x[z.x.length - 1]) < 0.001, `${j.x[0]}..${j.x[j.x.length - 1]}`)
  assert.ok(corr(onGrid(j, z.x), z.y) > 0.999)
})

test('a raw FID as JCAMP-DX (Bruker\'s export) is processed like the raw Bruker FID itself', async () => {
  const j = await open('aspirin_1h_fid.dx.gz'), z = await open('aspirin_1h_bruker_fid.zip'), ref = await open('aspirin_1h_bruker_processed.zip')
  assert.ok(j.fid && z.fid)
  assert.ok(Math.abs(j.fid.groupDelay - 61.020833) < 1e-3, `firmware DSPFVS 10 / DECIM 24 delay: ${j.fid.groupDelay}`)
  for (const s of [j, z]) assert.ok(corr(onGrid(s, ref.x), ref.y) > 0.95, 'matches TopSpin\'s processing')
})

test('a MestReNova file with several blocks reads the spectrum block\'s own units (regression: Hz taken for ppm)', async () => {
  const s = await open('cytisine_1h_mestrenova_blocks.jdx.gz')
  assert.ok(s.x[0] > 0.5 && s.x[0] < 0.7 && s.x[s.x.length - 1] > 8.2 && s.x[s.x.length - 1] < 8.3, `${s.x[0]}..${s.x[s.x.length - 1]}`)
  assert.equal(s.isStick, false)
})

test('Nanalysis 60 MHz FID (JCAMP-DX): ibuprofen\'s lines where they belong, phased', async () => {
  const s = await open('nanalysis_ibuprofen_1h_fid.jdx.gz')
  assert.ok(s.fid); assert.equal(s.solvent, 'Chloroform-d')
  // the doublets at 0.90, 1.50 and 2.45 and the quartet at 3.70, as lines 7 Hz (0.12 ppm) apart at 60 MHz
  assert.deepEqual(linesAt(s, [0.96, 0.85, 1.56, 1.44, 2.51, 2.40, 3.77, 3.65], 0.02), [])
  let neg = 0, pos = 0; for (const v of s.y) { if (v < 0) neg -= v; else pos += v }
  assert.ok(neg / pos < 0.03, `negative/positive ${neg / pos}`)
})

test('Oxford Instruments 60 MHz FID: mirrored ($REVERSE), its missing first points (TDoff) rebuilt, ethyl crotonate comes out upright on a flat baseline (regression: mirror image, twisted lines, then a baseline rolling in waves that auto-analysis took for signals at 13, -7 and -38 ppm)', async () => {
  const s = await open('oxford_ethylcrotonate_1h_fid.jdx.gz')
  assert.ok(s.fid); assert.equal(s.fid.groupDelay, -8); assert.equal(s.fid.predicted, 8)  // TDoff = -16 values = 8 complex points
  const mx = max(s.y)
  for (const [a, z] of [[13, 17], [9, 12], [7.6, 8.4], [5.0, 5.6], [2.5, 3.5], [-9, -5], [-40, -36]]) {
    const v = Array.from(s.y.slice(sp.lowerBound(s.x, a), sp.lowerBound(s.x, z))).sort((p, q) => p - q)
    assert.ok(Math.abs(v[v.length >> 1]) < 0.01 * mx, `baseline at ${a}–${z} ppm: ${(100 * v[v.length >> 1] / mx).toFixed(2)}% of the tallest line`)
  }
  assert.ok(Math.abs(s.fid.ph1) < 720, `hardly any first-order phase left to correct: ${s.fid.ph1}°`)
  const found = sp.autoAnalyse1H(s, 10).regions.map(r => (r.a + r.b) / 2)
  assert.ok(found.every(c => c > 0.5 && c < 7.5), `signals only where ethyl crotonate has them: ${found.map(c => c.toFixed(2))}`)
  // CH3 triplet 1.28, CH3 dd 1.87, OCH2 quartet 4.18, =CH 5.84 and 6.97
  for (const ppm of [1.28, 1.87, 4.18, 5.84, 6.97]) assert.deepEqual(linesAt(s, [ppm], 0.12, 0.03), [], `${ppm}`)
  const i0 = sp.lowerBound(s.x, 0), i1 = sp.lowerBound(s.x, 8)
  let lo = Infinity, hi = -Infinity; for (let i = i0; i < i1; i++) { lo = Math.min(lo, s.y[i]); hi = Math.max(hi, s.y[i]) }
  assert.ok(lo > -0.1 * hi, `the lines point up: min ${lo / hi} of max`)
  // the processing settings (line broadening) are honoured for JCAMP FIDs too
  const broad = await open('oxford_ethylcrotonate_1h_fid.jdx.gz', { lb: 3 })
  assert.equal(broad.fid.lb, 3)
})

test('missing first FID points are rebuilt from the rest (backward linear prediction), to within 1%', () => {
  const n = 4096, sw = 5000, re = new Float64Array(n), im = new Float64Array(n)
  let seed = 3
  for (let i = 0; i < n; i++) {
    for (const [hz, amp, t2] of [[-1210, 1, 0.3], [-1203, 1, 0.3], [415, 3, 0.5], [1890, 0.5, 0.2]]) {
      const t = i / sw, a = amp * Math.exp(-t / t2)
      re[i] += a * Math.cos(2 * Math.PI * hz * t); im[i] += a * Math.sin(2 * Math.PI * hz * t)
    }
    seed = (seed * 16807) % 2147483647; re[i] += 0.002 * (seed / 2147483647 - 0.5)
  }
  const missing = 10, got = sp.fid.predictBackward(re.subarray(missing), im.subarray(missing), missing)
  for (let k = 0; k < missing; k++) {
    const err = Math.hypot(got.re[k] - re[k], got.im[k] - im[k]) / Math.hypot(re[k], im[k])
    assert.ok(err < 0.01, `point ${k}: off by ${(100 * err).toFixed(2)}%`)
  }
})

test('Varian and JEOL exports read on their own ppm scales', async () => {
  const v = await open('varian_1h.jdx.gz'), j = await open('jeol_1h.dx.gz')
  assert.ok(Math.abs(v.x[0] + 0.7465) < 0.01 && Math.abs(v.x[v.x.length - 1] - 16.0895) < 0.01)
  assert.ok(Math.abs(j.x[0] + 4.887) < 0.01 && Math.abs(j.x[j.x.length - 1] - 15.103) < 0.01)
})

// ---------------------------------------------------------------- Bruker zips made here

/** acqus text and a fid with Lorentzian lines at given ppm (int32 or float64, either byte order), with an optional
 * digital-filter delay: the FID is the signal sampled from t = -delay */
function brukerExperiment({ lines, bf1 = 400.13, o1 = 2000, sw = 8000, td = 16384, delay = 0, dtypa = 0, big = false, extra = {} }) {
  const n = td / 2, dt = 1 / sw
  const width = dtypa === 2 ? 8 : 4
  const fid = new Uint8Array(td * width)
  const view = new DataView(fid.buffer)
  for (let i = 0; i < n; i++) {
    const t = (i - delay) * dt
    let re = 0, im = 0
    for (const [ppm, amp] of lines) {
      const f = ppm * bf1 - o1 // Hz from the carrier
      const decay = t >= 0 ? Math.exp(-Math.PI * 1.0 * t) : 0 // nothing before the delay, as on the spectrometer
      re += amp * Math.cos(2 * Math.PI * f * t) * decay
      im += amp * Math.sin(2 * Math.PI * f * t) * decay
    }
    const a = re, b = im
    if (dtypa === 2) { view.setFloat64(2 * i * 8, a, !big); view.setFloat64((2 * i + 1) * 8, b, !big) }
    else { view.setInt32(2 * i * 4, Math.round(a), !big); view.setInt32((2 * i + 1) * 4, Math.round(b), !big) }
  }
  const p = { TD: td, SW_h: sw, BF1: bf1, SFO1: bf1 + o1 / 1e6, O1: o1, NUC1: '<1H>', SOLVENT: '<CDCl3>', DTYPA: dtypa, BYTORDA: big ? 1 : 0, PARMODE: 0, GRPDLY: -1, ...extra }
  const acqus = Object.entries(p).map(([k, v]) => `##$${k}= ${v}`).join('\n')
  return { acqus: strToU8(`##TITLE= test\n${acqus}\n##END=\n`), fid }
}
const zip = files => buf(zipSync(files))
const peaksNear = (s, positions, tol) => linesAt(s, positions, tol, 0.2)

test('a raw Bruker FID: lines at the right ppm whatever the data type and byte order', async () => {
  const lines = [[1.0, 1e6], [3.5, 5e5], [7.26, 3e5]]
  for (const opts of [{}, { big: true }, { dtypa: 2 }, { dtypa: 2, big: true }]) {
    const e = brukerExperiment({ lines, ...opts })
    const s = await sp.parseSpectrumBuffer(zip({ '1/acqus': e.acqus, '1/fid': e.fid }), 't.zip')
    assert.deepEqual(peaksNear(s, [1.0, 3.5, 7.26], 0.003), [], JSON.stringify(opts))
  }
})

test('Bruker\'s digital filter delay: GRPDLY, or the firmware table when GRPDLY is -1 (DSPFVS 10-12)', async () => {
  const lines = [[1.0, 1e6], [2.5, 1e6], [6.0, 1e6], [8.0, 1e6]]
  for (const [extra, delay] of [[{ GRPDLY: 67.9869 }, 67.9869], [{ GRPDLY: -1, DSPFVS: 10, DECIM: 24 }, 61.020833333], [{ GRPDLY: -1, DSPFVS: 12, DECIM: 8 }, 53.25]]) {
    const e = brukerExperiment({ lines, delay, extra })
    const s = await sp.parseSpectrumBuffer(zip({ '1/acqus': e.acqus, '1/fid': e.fid }), 't.zip')
    assert.ok(Math.abs(s.fid.groupDelay - delay) < 1e-6, `${s.fid.groupDelay}`)
    let neg = 0, pos = 0; for (const v of s.y) { if (v < 0) neg -= v; else pos += v }
    assert.ok(neg / pos < 0.05, `phased: negative/positive ${neg / pos}`)
    assert.deepEqual(peaksNear(s, [1.0, 2.5, 6.0, 8.0], 0.003), [])
  }
})

test('a raw FID referenced in TopSpin (procs SF) keeps that referencing (regression: shifted by SR)', async () => {
  const e = brukerExperiment({ lines: [[1.0, 1e6], [7.0, 1e6]] })
  const plain = await sp.parseSpectrumBuffer(zip({ '1/acqus': e.acqus, '1/fid': e.fid }), 't.zip')
  const sf = 400.13 - 2.5e-6 // SR = -2.5 Hz
  const procs = strToU8(`##$SF= ${sf}\n##$SI= 32768\n##END=\n`)
  const ref = await sp.parseSpectrumBuffer(zip({ '1/acqus': e.acqus, '1/fid': e.fid, '1/pdata/1/procs': procs }), 't.zip')
  const top = s => sp.pickPeaks(s, 0.9, 1.1, 0.5, 5).sort((a, b) => b.y - a.y)[0].x
  assert.ok(Math.abs((top(ref) - top(plain)) - 2.5 / 400.13) < 0.0005, `${top(ref) - top(plain)}`)
})

test('2D experiments and series (ser, 2rr) say so plainly; a JCAMP fit curve inside one isn\'t taken for the spectrum (regression)', async () => {
  const e = brukerExperiment({ lines: [[1.0, 1e6]] })
  const t1t2 = strToU8('##TITLE= List of Points\n##JCAMPDX= 5.0\n##DATA TYPE= LINK\n##XUNITS= sec\n##TITLE= Peak 1\n##XYPOINTS=(XY..XY)\n0.01, -0.98\n0.1, -0.8\n1, 0.33\n##END=\n')
  for (const files of [{ '1/acqus': e.acqus, '1/ser': e.fid }, { '1/acqus': e.acqus, '1/ser': e.fid, '1/pdata/1/2rr': e.fid, '1/pdata/1/t1t2.dx': t1t2 }]) {
    await assert.rejects(sp.parseSpectrumBuffer(zip(files), 'cosy.zip'), /2D experiment/)
  }
  // and as JCAMP-DX: a processed COSY, a 2D FID, a Nanalysis pseudo-2D T1 series (headers as those programs write them)
  for (const head of ['##DATA TYPE= nD NMR SPECTRUM\n##NTUPLES= nD NMR SPECTRUM\n##SYMBOL=    F1,            F2,              Y',
    '##DATA TYPE= nD NMR FID\n##NTUPLES= nD NMR FID\n##SYMBOL=    T1,            T2,              R,               I',
    '##DATA TYPE=NDNMRFID\n##NTUPLES= NDNMRFID\n##SYMBOL= T1, T2, R, I']) {
    assert.throws(() => sp.parseJcamp(`##TITLE=x\n##JCAMP-DX=6.0\n${head}\n##PAGE= F1=1\n##DATA TABLE= (F2++(Y..Y)), PROFILE\n1 2 3\n##END NTUPLES=\n##END=\n`), /2D experiment/, head)
  }
})

test('broken files give a plain message, never a crash or a wrong spectrum', async () => {
  const e = brukerExperiment({ lines: [[1.0, 1e6]] })
  await assert.rejects(sp.parseSpectrumBuffer(zip({ '1/fid': e.fid }), 't.zip'), /acqus/)
  await assert.rejects(sp.parseSpectrumBuffer(zip({ '1/acqus': strToU8('##$TD= 16\n'), '1/fid': new Uint8Array(32) }), 't.zip'), /too short|spectral width/)
  await assert.rejects(sp.parseSpectrumBuffer(zip({ 'readme.txt': strToU8('hello') }), 't.zip'), /No processed Bruker spectrum/)
  const procs = strToU8('##$SI= 65536\n##$SF= 400.13\n##$OFFSET= 12\n##$SW_p= 8000\n')
  await assert.rejects(sp.parseSpectrumBuffer(zip({ '1/acqus': e.acqus, '1/pdata/1/procs': procs, '1/pdata/1/1r': new Uint8Array(4000) }), 't.zip'), /incomplete/)
  assert.throws(() => sp.parseJcamp('##TITLE=x\n##JCAMP-DX=5\n##END='), /No ##XYDATA/)
  assert.throws(() => sp.parseJcamp('##TITLE=x\n##JCAMP-DX=6\n##DATA TYPE=NMR SPECTRUM\n##NTUPLES=NMR SPECTRUM\n##SYMBOL=X,R,I\n##PAGE=N=1\n##END NTUPLES=\n'), /no real data page/)
  // a file cut off half-way is refused with a plain message: 0.36.4 read what was there and stretched it over the whole
  // axis, every peak at the wrong ppm (see spectra-numbers.test.mjs)
  const half = data('varian_1h.jdx.gz').toString('latin1')
  assert.throws(() => sp.parseJcamp(half.slice(0, Math.floor(half.length / 2))), /looks cut off: it says it has 16,385 points but only 8,213 are there/)
})
