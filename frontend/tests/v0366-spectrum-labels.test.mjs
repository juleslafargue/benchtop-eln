// 0.36.6: on a phone (390 px), an integral number under the spectrum (2.99 under a peak near 0 ppm) was drawn over the
// "δ (ppm)" axis label. Every text drawn under the axis must keep clear of every other.
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let draw
before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-draw-'))
  await build({ stdin: { contents: "export * from './spectrumDraw'", resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile: join(dir, 'draw.mjs'), logLevel: 'error' })
  draw = await import(pathToFileURL(join(dir, 'draw.mjs')).href)
  rmSync(dir, { recursive: true, force: true })
})

/** A canvas that records the text drawn, as boxes (about 0.55 em a character, cap height 0.75 em above the baseline). */
function recorder() {
  const texts = []
  const st = { font: '10px x', textAlign: 'left' }
  const size = () => Number(/(\d+(?:\.\d+)?)px/.exec(st.font)[1])
  const g = new Proxy({}, {
    get: (_, k) => k === 'measureText' ? t => ({ width: String(t).length * size() * 0.55 })
      : k === 'fillText' ? (t, x, y) => {
        const w = String(t).length * size() * 0.55, s = size()
        const x0 = st.textAlign === 'center' ? x - w / 2 : st.textAlign === 'right' ? x - w : x
        texts.push({ t: String(t), x0, x1: x0 + w, y0: y - 0.75 * s, y1: y + 0.2 * s })
      } : k in st ? st[k] : () => {},
    set: (_, k, v) => { st[k] = v; return true },
  })
  return { g, texts }
}

const overlap = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1

function spectrum() {
  const n = 4000, x = new Float64Array(n), y = new Float64Array(n)
  for (let i = 0; i < n; i++) {
    x[i] = 10 * i / (n - 1)
    y[i] = 1e3 * (Math.exp(-(((x[i] - 0.05) / 0.01) ** 2)) + Math.exp(-(((x[i] - 7.26) / 0.01) ** 2)) + Math.exp(-(((x[i] - 3.8) / 0.01) ** 2)))
  }
  return { title: 'TMS', x, y, xLabel: 'δ (ppm)', yLabel: '', kind: 'nmr', reversed: true, dips: false, nucleus: '1H', frequency: 400, isStick: false }
}

for (const width of [358, 330, 1000]) {
  test(`at ${width} px wide, no integral value is drawn over the axis label or another value (regression: 2.99 over “δ (ppm)” on a phone)`, () => {
    const spec = spectrum()
    const withArea = [{ a: 0.12, b: -0.02, area: 2.99 }, { a: 7.35, b: 7.18, area: 1 }, { a: 3.9, b: 3.7, area: 2.02 }, { a: 0.3, b: 0.14, area: 0.5 }]
    const { g, texts } = recorder()
    draw.drawSpectrum(g, width, 300, draw.LIGHT, { spec, view: [-0.5, 10.5], yScale: 1, withArea, refIdx: 1, refH: 1, peaks: [], showPeaks: false,
      markers: [], impurities: [], overlays: [], overlayMode: 'stack', signalTags: [], pred: null, showPred: false, drag: null, hover: null })
    const label = texts.find(t => t.t === 'δ (ppm)')
    assert.ok(label, 'the axis label is drawn')
    const values = texts.filter(t => /^\d+\.\d\d$/.test(t.t))
    assert.equal(values.length, 4)
    for (const v of values) assert.ok(!overlap(v, label), `${v.t} at x ${v.x0.toFixed(0)}–${v.x1.toFixed(0)}, y ${v.y0.toFixed(0)}–${v.y1.toFixed(0)} is over the label (x ${label.x0.toFixed(0)}–${label.x1.toFixed(0)}, y ${label.y0.toFixed(0)}–${label.y1.toFixed(0)})`)
    for (const v of values) assert.ok(v.y1 <= 300, `${v.t} is drawn below the canvas`)
    for (let i = 0; i < values.length; i++) for (let j = i + 1; j < values.length; j++) assert.ok(!overlap(values[i], values[j]), `${values[i].t} and ${values[j].t} overlap`)
    assert.ok(label.y1 <= 300 && label.x0 >= 0 && label.x1 <= width)
  })
}
