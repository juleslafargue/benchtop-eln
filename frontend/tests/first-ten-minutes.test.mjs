// 0.35, "a simpler first ten minutes": what a newcomer sees on a fresh install. A walk-through found the setup
// screen's demo box ticked by default (leaving it ticked made two example accounts with a published password in the
// real lab), and a crowded first experiment page. Pages are rendered to HTML here, as new-experiment.test.mjs does.
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let g

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-first10-'))
  const out = join(dir, 'first10.mjs')
  const stub = { name: 'app-stub', setup(b) {
    b.onResolve({ filter: /^\.\.\/App$/ }, () => ({ path: 'app', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const useApp = () => globalThis.__benchtopApp', loader: 'js' }))
  } }
  await build({
    stdin: { contents: `export { Setup } from './pages/Login'
      export { renderToStaticMarkup } from 'react-dom/server'
      export { MemoryRouter } from 'react-router-dom'
      export { createElement } from 'react'`, resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'tsx' },
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error', jsx: 'automatic',
    define: { 'process.env.NODE_ENV': '"production"' }, plugins: [stub],
    banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
  })
  g = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

const html = (el, props = {}) => g.renderToStaticMarkup(g.createElement(g.MemoryRouter, null, g.createElement(el, props)))

test('setup: the example records and accounts are not added unless asked for', () => {
  const h = html(g.Setup, { onLogin: () => {} })
  const box = /<input type="checkbox"[^>]*>/.exec(h)?.[0]
  assert.ok(box, 'no checkbox on the setup screen')
  assert.doesNotMatch(box, /checked/, 'the demo box is ticked by default')
  assert.match(h, /for trying Benchtop out/)
  assert.match(h, /Don(’|'|&#x27;)t tick this on your lab(’|'|&#x27;)s real server/)
  assert.match(h, /administrator of this Benchtop/)
  assert.doesNotMatch(h, /administrator of this notebook/)
})
