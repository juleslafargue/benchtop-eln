// The Getting started guide (src/pages/Guide.tsx): it renders, its contents list jumps to every part, its links go to
// pages that exist, its wording is British, and a newcomer can find it from the sidebar, the ⌘K box and the "?" list.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

const src = p => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8')
let g

/** The guide, React, the router and a stand-in for the app's context (who's signed in, their notebooks), in one bundle. */
before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-guide-'))
  const out = join(dir, 'guide.mjs')
  const stub = { name: 'app-stub', setup(b) {
    b.onResolve({ filter: /^\.\.\/App$/ }, () => ({ path: 'app', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const useApp = () => globalThis.__benchtopApp', loader: 'js' }))
  } }
  await build({
    stdin: { contents: `export { default as Guide, GUIDE_SECTIONS } from './pages/Guide'
      export { renderToStaticMarkup } from 'react-dom/server'
      export { MemoryRouter } from 'react-router-dom'
      export { createElement } from 'react'`, resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'tsx' },
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error', jsx: 'automatic',
    define: { 'process.env.NODE_ENV': '"production"' }, plugins: [stub],
    banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },  // react-dom/server needs node's own modules
  })
  g = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

function render({ canWrite = true, notebooks = [{ id: 4, name: 'Heterocycles', archived: false }], prefs = {} } = {}) {
  globalThis.__benchtopApp = { user: { id: 1, username: 'jl', full_name: 'J L', role: canWrite ? 'scientist' : 'viewer', preferences: prefs }, notebooks, canWrite }
  return g.renderToStaticMarkup(g.createElement(g.MemoryRouter, { initialEntries: ['/guide'] }, g.createElement(g.Guide)))
}
const text = html => html.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ')

test('every part has a heading, an anchor and a line in the contents', () => {
  const html = render()
  const ids = g.GUIDE_SECTIONS.map(s => s.id)
  assert.equal(new Set(ids).size, ids.length, 'two parts share an id')
  assert.ok(g.GUIDE_SECTIONS.length >= 10)
  for (const s of g.GUIDE_SECTIONS) {
    assert.ok(html.includes(`<section id="${s.id}"`), `${s.id}: no section`)
    assert.ok(html.includes(`<a href="#${s.id}">${s.title}</a>`), `${s.id}: not in the contents`)
    assert.match(html, new RegExp(`<h2 id="${s.id}-h">${s.title}</h2>`), `${s.id}: no heading`)
  }
  assert.ok(html.includes('<h1 class="page-title">Getting started</h1>'))
})

test('every link in the guide goes to a page the app has', () => {
  const routes = [...src('App.tsx').matchAll(/<Route path="([^"]+)"/g)].map(m => m[1]).filter(p => p !== '*')
  const known = p => routes.some(r => new RegExp(`^${r.replace(/:\w+\??/g, '[^/]+').replace(/\/\[\^\/\]\+$/, '(/[^/]+)?')}$`).test(p))
  const links = [...render().matchAll(/href="(\/[^"#]*)"/g)].map(m => m[1])
  assert.ok(links.length >= 8, `only ${links.length} links into the app`)
  for (const l of links) assert.ok(known(l), `${l} isn't a page`)
  assert.ok(links.includes('/notebooks/4'), 'links to the person\'s notebook')
})

test('the default notebook is the one it opens, and a newcomer without notebooks or write access is not told otherwise', () => {
  const two = [{ id: 4, name: 'Heterocycles' }, { id: 9, name: 'Click chemistry' }]
  assert.ok(render({ notebooks: two, prefs: { default_notebook_id: 9 } }).includes('href="/notebooks/9">Open Click chemistry'))
  const none = render({ notebooks: [] })
  assert.doesNotMatch(none, /href="\/notebooks\//, 'offers to open a notebook that is not there')
  assert.ok(text(none).includes('+ New'))
  assert.ok(!text(render({ canWrite: false })).includes('+ New'), 'a read-only account is told to make notebooks')
})

test('the wording is British and plain', () => {
  const t = text(render())
  for (const us of [/\bcolor/i, /analyz/i, /\bcenter/i, /organiz/i, /behavior/i, /\blicense\b/i, /characteriz/i, /\bcustomiz/i])
    assert.doesNotMatch(t, us)
  assert.match(t, /Characterisation/)
  assert.match(t, /colour themes/)
  assert.match(t, /ask your Benchtop admin/)
  assert.ok(t.length < 14000, `${t.length} characters: too long to read in five minutes`)
})

test('it says only what the app does: the shortcuts it names exist', () => {
  const t = text(render())
  const shortcuts = src('components/Shortcuts.tsx')
  for (const k of ['L', 'T', 'P', 'N', '?']) assert.match(shortcuts, new RegExp(`\\['${k.replace('?', '\\?')}',`), `${k} is not in the shortcuts list`)
  assert.match(shortcuts, /Z \/ I \/ R/, 'the spectrum viewer\'s R (reference) is listed')
  assert.match(src('components/SpectrumViewer.tsx'), /e\.key === 'r'\) setTool\('reference'\)/)
  assert.doesNotMatch(t, /calculator/i, 'the ⌘K calculator was removed on purpose')
})

test('a newcomer can find it from the sidebar, the ⌘K box, the shortcuts list and the dashboard', () => {
  assert.match(src('components/Layout.tsx'), /<NavLink to="\/guide" className="guide-link">/)
  assert.match(src('components/CommandPalette.tsx'), /label: 'Getting started guide'.*go\('\/guide'\)/)
  assert.match(src('components/Shortcuts.tsx'), /<Link to="\/guide"/)
  const dash = src('pages/Dashboard.tsx')
  assert.match(dash, /New to Benchtop\?<\/b> Read <Link to="\/guide">Getting started<\/Link> \(5 minutes\)/)
  assert.match(dash, /!user\.preferences\?\.guide_dismissed/, 'the card follows the account preference')
  assert.match(dash, /preferences: \{ guide_dismissed: true \}/, 'Dismiss saves it to the account')
})
