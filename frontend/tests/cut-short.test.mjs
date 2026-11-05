// 0.31: lists that come in pages say so when they're cut short. Until 0.31 the Materials page stopped at 500, Search at
// 100 of each kind and the Inbox at 200, without a word. The server now sends how many match (X-Total-Count, and
// X-Total-Experiments / -Materials / -Files for search); the browser reads it (src/api.ts: api.getList, api.getCounted)
// and says "Showing the first 500 of 812 materials." with Show more (src/listNotice.ts, src/components/CutShort.tsx).
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let m

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-cut-short-'))
  const out = join(dir, 'cut-short.mjs')
  await build({
    stdin: { contents: "export { cutShortText, addPage } from './listNotice'; export { api, countHeader } from './api'",
      resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error',
  })
  m = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

test('a list cut short says how many of how many', () => {
  assert.equal(m.cutShortText(500, 812, 'materials'), 'Showing the first 500 of 812 materials.')
  assert.equal(m.cutShortText(1000, 12345, 'materials'), 'Showing the first 1,000 of 12,345 materials.')
  assert.equal(m.cutShortText(100, 340, 'experiments'), 'Showing the first 100 of 340 experiments.')
  assert.equal(m.cutShortText(812, 812, 'materials'), '')  // all shown: nothing to say
  assert.equal(m.cutShortText(3, 2, 'files'), '')  // one went meanwhile
})

test('the next page is added without any row twice', () => {
  const shown = [{ id: 1 }, { id: 2 }, { id: 3 }]
  assert.deepEqual(m.addPage(shown, [{ id: 3 }, { id: 4 }]).map(r => r.id), [1, 2, 3, 4])
  assert.deepEqual(m.addPage([], [{ id: 7 }]).map(r => r.id), [7])
})

test('count headers are read as whole numbers, or not at all', () => {
  assert.equal(m.countHeader('812'), 812)
  assert.equal(m.countHeader(' 0 '), 0)
  for (const bad of [null, undefined, '', 'many', '-1', '1.5', '1e3']) assert.equal(m.countHeader(bad), null, String(bad))
})

function answer(body, headers = {}) {
  return async () => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json', ...headers } })
}

test('api.getList gives the page and the total the server sent', async () => {
  const real = globalThis.fetch
  try {
    globalThis.fetch = answer([{ id: 1 }, { id: 2 }], { 'X-Total-Count': '812' })
    assert.deepEqual(await m.api.getList('/api/materials'), { rows: [{ id: 1 }, { id: 2 }], total: 812 })
    globalThis.fetch = answer([{ id: 1 }, { id: 2 }])  // an older server: no header, so the page is all there is
    assert.deepEqual(await m.api.getList('/api/materials'), { rows: [{ id: 1 }, { id: 2 }], total: 2 })
    globalThis.fetch = answer({ experiments: [], materials: [], files: [], terms: [] },
      { 'X-Total-Experiments': '340', 'X-Total-Materials': '0', 'X-Total-Count': '340' })
    const r = await m.api.getCounted('/api/search?q=x')
    assert.equal(r.count('X-Total-Experiments'), 340)
    assert.equal(r.count('X-Total-Materials'), 0)
    assert.equal(r.count('X-Total-Files'), null)
  } finally {
    globalThis.fetch = real
  }
})
