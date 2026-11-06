// 0.32: barcode scanning. The decoder Benchtop ships (zxing-wasm's reader, loaded by src/scanReader.ts) really reads
// the barcodes found on chemical bottles and lab labels, through the same code the scanner uses (readCodes in
// src/scan.ts), with no network: the .wasm file comes from node_modules, as Vite copies it into the build.
// Also the pure helpers the scan results use: cleaning a code, what's wrong with a bottle, what to weigh.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

const require = createRequire(import.meta.url)
let m, reader, writer

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-scan-'))
  const out = join(dir, 'scan.mjs')
  await build({
    stdin: { contents: "export * from './scan'", resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error',
  })
  m = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })

  // no network from here on: a decoder that tried to fetch its .wasm from a CDN would fail these tests
  globalThis.fetch = async (url) => { throw new Error(`no network in this test (asked for ${url})`) }
  if (typeof globalThis.ImageData === 'undefined') {  // what a canvas gives the scanner for a camera frame or a photo
    globalThis.ImageData = class ImageData { constructor(data, width, height) { this.data = data; this.width = width; this.height = height } }
  }
  reader = await import('zxing-wasm/reader')
  writer = await import('zxing-wasm/writer')
  // the same file scanReader.ts asks Vite for ('zxing-wasm/reader/zxing_reader.wasm?url')
  const wasm = readFileSync(require.resolve('zxing-wasm/reader/zxing_reader.wasm'))
  reader.prepareZXingModule({ overrides: { wasmBinary: wasm.buffer.slice(wasm.byteOffset, wasm.byteOffset + wasm.byteLength) } })
  const w = readFileSync(require.resolve('zxing-wasm/writer/zxing_writer.wasm'))
  writer.prepareZXingModule({ overrides: { wasmBinary: w.buffer.slice(w.byteOffset, w.byteOffset + w.byteLength) } })
})

/** A barcode as a camera would see it: grey on off-white, scaled up, with a margin, optionally turned on its side. */
function pixels(symbol, { scale = 4, margin = 12, sideways = false } = {}) {
  const sw = symbol.width, sh = symbol.height
  const W0 = (sw + 2 * margin) * scale, H0 = (sh + 2 * margin) * scale
  const [W, H] = sideways ? [H0, W0] : [W0, H0]
  const data = new Uint8ClampedArray(W * H * 4)
  for (let y = 0; y < H0; y++) for (let x = 0; x < W0; x++) {
    const sx = Math.floor(x / scale) - margin, sy = Math.floor(y / scale) - margin
    const dark = sx >= 0 && sy >= 0 && sx < sw && sy < sh && symbol.data[sy * sw + sx] === 0
    const v = (dark ? 40 : 225) + ((x * 7 + y * 13) % 11)  // not pure black and white
    const [px, py] = sideways ? [H0 - 1 - y, x] : [x, y]
    const i = (py * W + px) * 4
    data[i] = data[i + 1] = data[i + 2] = v; data[i + 3] = 255
  }
  return new ImageData(data, W, H)
}

const SAMPLES = [
  ['Code128', 'MAT-00012-L0034'], ['Code128', 'VIM 0042137'], ['DataMatrix', 'HECHMET-VIM-118204'], ['QRCode', 'EXP-00025'],
  ['Code39', 'SA-0042'], ['EAN13', '4006381333931'], ['EAN8', '96385074'], ['ITF', '12345678'],
]

test('the shipped decoder reads Code 128 and Data Matrix from a camera frame, offline', async () => {
  for (const [format, text] of SAMPLES.slice(0, 3)) {
    const w = await writer.writeBarcode(text, { format })
    assert.equal(w.error, '', `${format}: ${w.error}`)
    const found = await m.readCodes(reader.readBarcodes, pixels(w.symbol), false)
    assert.deepEqual(found.map(f => f.text), [text], format)
    assert.equal(found[0].format, format)
  }
})

test('every barcode type on a bottle or a label is read, from a photo too, and turned sideways', async () => {
  for (const [format, text] of SAMPLES) {
    const w = await writer.writeBarcode(text, { format, scale: 3 })
    assert.equal(w.error, '', `${format}: ${w.error}`)
    // a photo: the image file itself (the reader's own decoder), and the pixels a canvas gives
    assert.deepEqual((await m.readCodes(reader.readBarcodes, w.image, true)).map(f => f.text), [text], `${format} from a file`)
    assert.deepEqual((await m.readCodes(reader.readBarcodes, pixels(w.symbol, { sideways: true }), true)).map(f => f.text), [text], `${format} sideways`)
  }
  // UPC-A, as on American suppliers' boxes: read as EAN-13 with a 0 in front (the server matches both)
  const upc = await writer.writeBarcode('036000291452', { format: 'UPCA' })
  assert.deepEqual((await m.readCodes(reader.readBarcodes, pixels(upc.symbol), true)).map(f => f.text), ['0036000291452'])
})

test("a GS1 Data Matrix (a supplier's batch code) is read as the library gives it, without anything around it", async () => {
  const w = await writer.writeBarcode('(01)09506000134352(10)ABC123', { format: 'DataMatrix', options: 'gs1' })
  assert.equal(w.error, '')
  const found = await m.readCodes(reader.readBarcodes, pixels(w.symbol), false)
  assert.equal(found.length, 1)
  assert.equal(found[0].text, '(01)09506000134352(10)ABC123')
})

test('a photo with no barcode in it reads as nothing, not an error', async () => {
  const blank = new ImageData(new Uint8ClampedArray(200 * 120 * 4).fill(230), 200, 120)
  assert.deepEqual(await m.readCodes(reader.readBarcodes, blank, true), [])
})

test('a photo with two codes gives both, once each', async () => {
  const a = (await writer.writeBarcode('LOT-A-0001', { format: 'Code128' })).symbol
  const b = (await writer.writeBarcode('LOT-B-0002', { format: 'Code128' })).symbol
  const both = { width: Math.max(a.width, b.width), height: a.height + b.height + 30, data: null }
  both.data = new Uint8ClampedArray(both.width * both.height).fill(255)
  for (let y = 0; y < a.height; y++) for (let x = 0; x < a.width; x++) both.data[y * both.width + x] = a.data[y * a.width + x]
  for (let y = 0; y < b.height; y++) for (let x = 0; x < b.width; x++) both.data[(y + a.height + 30) * both.width + x] = b.data[y * b.width + x]
  const found = await m.readCodes(reader.readBarcodes, pixels(both), true)
  assert.deepEqual(found.map(f => f.text).sort(), ['LOT-A-0001', 'LOT-B-0002'])
})

test('readCodes trims what a reading can carry around the code, and drops repeats', async () => {
  const fake = async () => [{ text: 'VIM-1\r\n', format: 'Code128' }, { text: ' VIM-1', format: 'Code128' }, { text: '\x1d', format: 'DataMatrix' },
    { text: 'BAD', format: 'Code39', isValid: false }]
  assert.deepEqual(await m.readCodes(fake, null, true), [{ text: 'VIM-1', format: 'Code128' }])
})

test('the scanner asks for the right barcode types', async () => {
  let asked
  await m.readCodes(async (_img, opts) => { asked = opts; return [] }, null, false)
  for (const f of ['Code128', 'Code39', 'EAN13', 'EAN8', 'UPCA', 'ITF', 'QRCode', 'DataMatrix']) assert.ok(asked.formats.includes(f), f)
  assert.equal(asked.maxNumberOfSymbols, 1)  // a camera frame: the first code is enough
})

test('the decoder is loaded from Benchtop itself, never a CDN', () => {
  const src = readFileSync(new URL('../src/scanReader.ts', import.meta.url), 'utf8')
  assert.match(src, /import\('zxing-wasm\/reader\/zxing_reader\.wasm\?url'\)/)
  assert.match(src, /locateFile/)
  assert.doesNotMatch(src, /https?:\/\//)
  for (const f of ['src/scan.ts', 'src/components/Scanner.tsx']) {
    assert.doesNotMatch(readFileSync(new URL(`../${f}`, import.meta.url), 'utf8'), /^import .*zxing-wasm/m, `${f} must load the decoder lazily`)
  }
})

test('cleanCode: spaces and control characters around a code go, inside it nothing changes', () => {
  assert.equal(m.cleanCode('  MAT-00012-L0034\r\n'), 'MAT-00012-L0034')
  assert.equal(m.cleanCode('\tVIM 0042\x00'), 'VIM 0042')
  assert.equal(m.cleanCode('0109506000134352\x1d10ABC\x1d'), '0109506000134352\x1d10ABC')  // the group separator inside stays
  assert.equal(m.cleanCode(null), '')
  assert.equal(m.cleanCode(' \r\n'), '')
})

test('the live camera only on a secure page that has one', () => {
  const cam = { mediaDevices: { getUserMedia: () => {} } }
  assert.equal(m.canUseLiveCamera({ isSecureContext: true, navigator: cam }), true)
  assert.equal(m.canUseLiveCamera({ isSecureContext: false, navigator: cam }), false)  // http://192.168.1.20:8000
  assert.equal(m.canUseLiveCamera({ isSecureContext: true, navigator: {} }), false)
})

test("what's wrong with a bottle", () => {
  const now = new Date('2026-11-06T12:00:00Z')
  assert.equal(m.lotProblem({ status: 'available', expires_at: null }, now), null)
  assert.equal(m.lotProblem({ status: 'available', expires_at: '2027-01-01T00:00:00Z' }, now), null)
  assert.match(m.lotProblem({ status: 'available', expires_at: '2026-10-01T00:00:00Z' }, now), /^This bottle expired on /)
  assert.equal(m.lotProblem({ status: 'empty', expires_at: null }, now), 'This bottle is marked empty.')
  assert.equal(m.lotProblem({ status: 'disposed', expires_at: null }, now), 'This bottle was disposed of.')
  assert.match(m.lotProblem({ status: 'quarantined', expires_at: null }, now), /quarantined/)
})

const row = (o) => ({ role: 'reagent', name: '', formula: '', smiles: '', mw: null, material_id: null, lot_id: null, equiv: null, mmol: null,
  mass_mg: null, volume_ml: null, density: null, conc_m: null, purity: 100, limiting: false, basis: 'equiv', actual_mass_mg: null,
  theoretical_mass_mg: null, yield_pct: null, notes: '', ...o })

test('what to weigh or measure, as the bench view says it', () => {
  assert.equal(m.rowLine(row({ name: '4-Bromotoluene', equiv: 1.2, mass_mg: 205.2 })), '4-Bromotoluene, reagent, 1.2 eq: weigh 205.2 mg')
  assert.equal(m.rowLine(row({ name: 'Triethylamine', equiv: 2, mass_mg: 202.4, volume_ml: 0.279, density: 0.726 })), 'Triethylamine, reagent, 2 eq: measure 0.279 mL')
  assert.equal(m.rowLine(row({ name: 'Toluene', role: 'solvent', volume_ml: 10, basis: 'volume' })), 'Toluene, solvent: measure 10 mL')
  assert.equal(m.rowLine(row({ name: 'K2CO3', equiv: 3, mass_mg: 1243 })), 'K2CO3, reagent, 3 eq: weigh 1.24 g')
  assert.equal(m.rowLine(row({ formula: 'C7H7Br', role: 'reactant', equiv: 1 })), 'C7H7Br, reactant, 1 eq')
})

test("which rows a bottle is for, and using it for one", () => {
  const rows = [row({ name: 'A', role: 'reactant', material_id: 7, lot_id: 70, purity: 100 }), row({ name: 'B', material_id: 8, lot_id: null }),
    row({ name: 'A again', material_id: 7, lot_id: 71, deducted: { tx_id: 5 } }), row({ name: 'P', role: 'product', material_id: 9 })]
  assert.deepEqual(m.rowsForLot(rows, { material_id: 7 }).map(x => x.index), [0, 2])
  assert.deepEqual(m.rowsForLot(rows, { material_id: 99 }), [])
  const next = m.withBottle(rows, 0, { id: 72, material_id: 7, purity: 97.5 })
  assert.equal(next[0].lot_id, 72)
  assert.equal(next[0].purity, 97.5)
  assert.deepEqual(next.slice(1), rows.slice(1))  // nothing else changes
  assert.equal(rows[0].lot_id, 70)  // the table as shown isn't changed in place
  assert.equal(m.withBottle(rows, 1, { id: 80, material_id: 8, purity: null })[1].purity, 100)  // no purity given: 100 %
  assert.equal(m.withBottle(rows, 1, { id: 72, material_id: 7, purity: 99 }), null)  // another material's row
  assert.equal(m.withBottle(rows, 2, { id: 72, material_id: 7, purity: 99 }), null)  // already taken from another bottle
  assert.equal(m.withBottle(rows, 9, { id: 72, material_id: 7, purity: 99 }), null)
})
