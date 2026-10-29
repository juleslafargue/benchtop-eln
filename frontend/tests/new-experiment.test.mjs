// The New experiment dialog (src/components/NewExperimentDialog.tsx) picks the notebook the experiment goes in. Experiments
// can't be deleted, so it must never pick someone else's notebook on its own: 0.28 picked the first in the list, which
// for a newcomer could be a colleague's notebook open to everyone.
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let g

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-newexp-'))
  const out = join(dir, 'newexp.mjs')
  const stub = { name: 'app-stub', setup(b) {
    b.onResolve({ filter: /^\.\.\/App$/ }, () => ({ path: 'app', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const useApp = () => globalThis.__benchtopApp', loader: 'js' }))
  } }
  await build({
    stdin: { contents: `export { default as NewExperimentDialog } from './components/NewExperimentDialog'
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

const me = { id: 7, username: 'nnewbie', full_name: 'Nadia Newbie' }
const colleague = { id: 2, username: 'slaurent', full_name: 'Sophie Laurent' }
const nb = (id, name, owner, created_at) => ({ id, name, owner, created_at, archived: false, description: '', visibility: 'everyone', group: null })
const theirs = nb(1, 'Amide couplings', colleague, '2026-01-05T10:00:00Z')

function render(notebooks, { prefs = {}, notebookId } = {}) {
  globalThis.__benchtopApp = { user: { ...me, role: 'scientist', preferences: prefs }, notebooks, refreshNotebooks: async () => {}, toast: () => {}, groups: [] }
  return g.renderToStaticMarkup(g.createElement(g.MemoryRouter, null, g.createElement(g.NewExperimentDialog, { notebookId, onClose: () => {} })))
}
const picked = html => (/<option value="(\d*)" selected="">/.exec(html) || [])[1]
const createDisabled = html => /<button class="btn primary" disabled="">Create experiment<\/button>/.test(html)

test('a newcomer with no notebook of their own: nothing is picked, they are offered their own notebook', () => {
  const html = render([theirs])
  assert.notEqual(picked(html), '1', "picked a colleague's notebook")
  assert.match(html, /<option value="" selected="">Choose a notebook<\/option>/)
  assert.ok(createDisabled(html), 'Create could be pressed with no notebook chosen')
  assert.match(html, /Make your own notebook/)
  assert.match(html, /Amide couplings \(Sophie Laurent\)/, "someone else's notebook doesn't say whose it is")
})

test('without a default notebook, their own most recent notebook is picked, not the first in the list', () => {
  const html = render([theirs, nb(5, 'First year', me, '2026-02-01T09:00:00Z'), nb(6, 'Heterocycles', me, '2026-03-01T09:00:00Z')])
  assert.equal(picked(html), '6')
  assert.doesNotMatch(html, /Make your own notebook/)
})

test('their default notebook comes first, and the notebook they are looking at wins over both', () => {
  const list = [theirs, nb(5, 'First year', me, '2026-02-01T09:00:00Z'), nb(6, 'Heterocycles', me, '2026-03-01T09:00:00Z')]
  assert.equal(picked(render(list, { prefs: { default_notebook_id: 5 } })), '5')
  assert.equal(picked(render(list, { prefs: { default_notebook_id: 5 }, notebookId: 1 })), '1')
  assert.equal(picked(render([theirs], { notebookId: 1 })), '1')
})
