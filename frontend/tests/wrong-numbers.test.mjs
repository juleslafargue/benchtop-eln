// 0.30.1: wrong numbers and error messages found by the 0.30 stress test.
// - "1,000" in a mass box was stored as 1 mg (the comma read as a decimal point): it could be either, so it's refused
//   with a short message; "0,5" is still 0.5 (src/components/bits.tsx).
// - When Benchtop can't be reached, the message says so plainly instead of the browser's "Failed to fetch" (src/api.ts).
// - Impossible green metrics held by older records (a negative atom economy, a PMI under 1) aren't shown (src/api.ts).
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let m

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-numbers-'))
  const out = join(dir, 'numbers.mjs')
  const stub = { name: 'editor-stub', setup(b) {  // the structure editor (Ketcher) isn't needed to read numbers
    b.onResolve({ filter: /\/StructureEditor$/ }, () => ({ path: 'editor', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export default function StructureEditor() { return null }', loader: 'js' }))
  } }
  await build({
    stdin: { contents: `export { parseAmount, amountProblem } from './components/bits'; export { api, greenOk } from './api'`,
      resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error', jsx: 'automatic', plugins: [stub],
    define: { 'process.env.NODE_ENV': '"production"' },
    banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
  })
  m = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

test('"1,000" is refused: it could be one thousand or one', () => {
  for (const [text, a, b] of [['1,000', '1000', '1'], ['2,500 g', '2500', '2.5'], ['12,345', '12345', '12.345'], ['-1,000', '-1000', '-1']]) {
    assert.equal(m.parseAmount(text, 'mg'), undefined, text)
    const msg = m.amountProblem(text)
    assert.ok(msg && msg.includes(a) && msg.includes(b), `${text}: ${msg}`)
  }
  assert.equal(m.amountProblem('1,000'), 'Did you mean 1000 or 1? Type it without the comma.')
})

test('a comma that can only be a decimal point still is one', () => {
  assert.equal(m.parseAmount('0,5', 'mg'), 0.5)
  assert.equal(m.parseAmount('0,500 g', 'mg'), 500)  // a leading 0 is never a thousands separator
  assert.equal(m.parseAmount('1,5', 'mg'), 1.5)
  assert.equal(m.parseAmount('1,25'), 1.25)
  assert.equal(m.parseAmount('1,0000'), 1)
  assert.equal(m.parseAmount('1234,567'), 1234.567)  // four digits before it: not thousands
  assert.equal(m.parseAmount('1.2 g', 'mg'), 1200)
  assert.equal(m.parseAmount('1000', 'mg'), 1000)
  assert.equal(m.parseAmount(''), null)
  for (const ok of ['0,5', '1,5', '1000', '1.000', '1.2 g', '']) assert.equal(m.amountProblem(ok), null, ok)
})

// ------------------------------------------------------------------ Benchtop can't be reached

const answer = (status, body, type = 'application/json') => async () => new Response(body, { status, headers: { 'content-type': type } })
async function failure(fetchImpl, call = () => m.api.get('/api/experiments')) {
  globalThis.fetch = fetchImpl
  try { await call() } catch (e) { return e }
  assert.fail('expected the request to fail')
}

test('no answer at all says Benchtop can\'t be reached, not "Failed to fetch"', async () => {
  const e = await failure(async () => { throw new TypeError('Failed to fetch') })
  assert.equal(e.status, 0)
  assert.equal(e.message, "Can't reach Benchtop. Check your connection and try again.")
})

test('a proxy saying Benchtop is down is worded the same way; Benchtop\'s own messages are kept', async () => {
  for (const status of [502, 503, 504]) {
    const e = await failure(answer(status, '<html>Bad Gateway</html>', 'text/html'))
    assert.match(e.message, /^Can't reach Benchtop/, String(status))
  }
  const own = await failure(answer(503, JSON.stringify({ detail: 'Benchtop is restoring a backup; try again in a minute' })))
  assert.equal(own.status, 503)
  assert.equal(own.message, 'Benchtop is restoring a backup; try again in a minute')
  const err = await failure(answer(409, JSON.stringify({ detail: 'This section was changed elsewhere' })))
  assert.equal(err.status, 409)
  assert.equal(err.message, 'This section was changed elsewhere')
})

test('the connection dropping while a file downloads is worded the same way', async () => {
  globalThis.fetch = async () => ({ ok: true, status: 200, headers: new Headers({ 'content-type': 'application/pdf' }),
    blob: async () => { throw new TypeError('network error') }, arrayBuffer: async () => { throw new TypeError('network error') },
    text: async () => { throw new TypeError('network error') }, json: async () => { throw new TypeError('network error') } })
  const res = await m.api.raw('/api/attachments/1')
  await assert.rejects(res.blob(), /Can't reach Benchtop/)
  await assert.rejects(res.arrayBuffer(), /Can't reach Benchtop/)
  await assert.rejects(m.api.get('/api/dashboard'), /Can't reach Benchtop/)
})

// ------------------------------------------------------------------ green metrics

test('impossible green metrics held by an older record are not shown', () => {
  const g = m.greenOk({ atom_economy_pct: -200, rme_pct: 130, pmi: 0.67, e_factor: -0.33, solvent_l_per_kg: -5 })
  assert.equal(g.ae, null); assert.equal(g.rme, null); assert.equal(g.pmi, null); assert.equal(g.eFactor, null); assert.equal(g.solvent, null)
  assert.ok(g.aeImpossible && g.rmeImpossible && g.pmiImpossible)
  const h = m.greenOk({ impossible: ['pmi'] })
  assert.ok(h.pmiImpossible && h.pmi === null)
  const ok = m.greenOk({ atom_economy_pct: 80, rme_pct: 0, pmi: 1, e_factor: 0, solvent_l_per_kg: 12 })
  assert.deepEqual([ok.ae, ok.rme, ok.pmi, ok.eFactor, ok.solvent], [80, 0, 1, 0, 12])
  assert.ok(!ok.aeImpossible && !ok.rmeImpossible && !ok.pmiImpossible)
})
