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
    b.onLoad({ filter: /^app$/, namespace: 'stub' }, () => ({ contents: 'export const useApp = () => globalThis.__benchtopApp', loader: 'js' }))
    // the rich-text editor doesn't render outside a browser: the purification section's notes box is left out here
    b.onResolve({ filter: /^\.\/TextEntry$/ }, () => ({ path: 'text', namespace: 'stub' }))
    b.onLoad({ filter: /^text$/, namespace: 'stub' }, () => ({ contents: 'export default function TextEntry() { return null }', loader: 'js' }))
  } }
  await build({
    stdin: { contents: `export { Setup } from './pages/Login'
      export { DEFAULT_WIDGETS, emptyChart, hiddenOnFirstDay, wantsFirstRecord, FirstSteps } from './pages/Dashboard'
      export { default as PurificationEntry } from './components/PurificationEntry'
      export { sigLine, sig4 } from './display'
      export { renderToStaticMarkup } from 'react-dom/server'
      export { MemoryRouter } from 'react-router-dom'
      export { createElement } from 'react'`, resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'tsx' },
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error', jsx: 'automatic', loader: { '.css': 'empty' },
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

test('purification: a new section has no method until one is chosen, and says so', () => {
  globalThis.__benchtopApp = { user: { id: 1, role: 'scientist', preferences: {} }, toast: () => {} }
  const props = m => ({ content: { method: m, stationary_phase: '', eluent: '', rf: '', rf_eluent: '', isolated_mg: null, appearance: '', notes_html: '' },
    onChange: () => {}, experimentId: 1, attachments: [], onAttachmentsChanged: () => {}, context: {} })
  const fresh = html(g.PurificationEntry, { ...props(''), readOnly: false })
  assert.match(fresh, /<option value="" selected="">Choose…<\/option>/, 'an untouched section shows a method as if chosen')
  assert.doesNotMatch(fresh, /<option value="column" selected="">/)
  const signed = html(g.PurificationEntry, { ...props(''), readOnly: true })
  assert.match(signed, /<option value="" selected="">Not given<\/option>/)
  assert.doesNotMatch(signed, /placeholder=/, 'a read-only record shows example text as if it were data')
  // a record that has "column" (chosen, or made before 0.35) keeps showing it
  assert.match(html(g.PurificationEntry, { ...props('column'), readOnly: true }), /<option value="column" selected="">Flash column<\/option>/)
})

test('dashboard: your own records stay in view after signing', () => {
  const lists = g.DEFAULT_WIDGETS.filter(w => w.type === 'list').map(w => w.list)
  for (const k of ['open', 'waiting', 'recent', 'awaiting', 'returned']) assert.ok(lists.includes(k), `${k} isn't on the default dashboard`)
  const ids = g.DEFAULT_WIDGETS.map(w => w.i)
  assert.equal(new Set(ids).size, ids.length)
})

test('dashboard: on a first day, empty charts wait until there is something to show', () => {
  const none = { stats: { yields: [], this_month: 0, last_month: 0 }, spectra: [], inventory: { low: [], expiring: [], safety_changes: [] } }
  for (const type of ['yield', 'month', 'spectra', 'inventory']) assert.ok(g.emptyChart({ type }, none), type)
  for (const type of ['list', 'actions', 'note']) assert.ok(!g.emptyChart({ type }, none), type)
  const some = { stats: { yields: [{ yield_pct: 80 }], this_month: 1, last_month: 0 }, spectra: [{ id: 1 }], inventory: { low: [{ lot_id: 1 }], expiring: [] } }
  for (const type of ['yield', 'month', 'spectra', 'inventory']) assert.ok(!g.emptyChart({ type }, some), type)
})

test('dashboard: the first-record card, and "Add your group" for an admin on their own', () => {
  const f = props => html(g.FirstSteps, { canWrite: true, onNew: () => {}, onHide: () => {}, ...props })
  const first = f({ firstRecord: true, alone: false })
  assert.match(first, /Your first record/)
  assert.match(first, /Sign it and choose a witness/)
  // 0.36: written for any lab, not only chemists
  assert.match(first, /<b>Write what you did\.<\/b> For a reaction, draw or paste it and the table works out the amounts; for anything\s+else, add text, tables and files\./)
  assert.doesNotMatch(first, /Write the reaction and the procedure/)
  assert.match(first, />Hide</)
  assert.doesNotMatch(first, /Add your group/)
  const admin = f({ firstRecord: true, alone: true })
  assert.match(admin, /Add your group/)
  assert.match(admin, /href="\/admin\/users"/)
  assert.match(f({ firstRecord: false, alone: true }), /<h2 id="first-steps-h">Add your group<\/h2>/)
  assert.equal(f({ firstRecord: false, alone: false }), '')
  assert.equal(f({ firstRecord: true, alone: false, canWrite: false }), '', 'a read-only account is told to make a record')
})

// 0.35.1: a PI who writes no records (the demo lab's Ruiz group lead) saw "Your first record" and three empty lists of
// their own records above the group's yield trend
test('dashboard: on a first day, the lists of your own records wait too; the ones with other people\'s stay', () => {
  const none = { stats: { yields: [], this_month: 0, last_month: 0 }, spectra: [], inventory: { low: [], expiring: [], safety_changes: [] } }
  const some = { stats: { yields: [{ yield_pct: 80 }], this_month: 1, last_month: 0 }, spectra: [{ id: 1 }], inventory: { low: [{ lot_id: 1 }], expiring: [] } }
  for (const d of [none, some]) {
    for (const list of ['open', 'waiting', 'recent', 'returned', 'stale']) assert.ok(g.hiddenOnFirstDay({ type: 'list', list }, d), list)
    for (const list of ['awaiting', 'pinned']) assert.ok(!g.hiddenOnFirstDay({ type: 'list', list }, d), list)
    assert.ok(!g.hiddenOnFirstDay({ type: 'actions' }, d))
  }
  // charts as before: hidden while empty, shown with something in them
  for (const type of ['yield', 'month', 'spectra', 'inventory']) {
    assert.ok(g.hiddenOnFirstDay({ type }, none), type)
    assert.ok(!g.hiddenOnFirstDay({ type }, some), type)
  }
  // the default dashboard on a lead's first day with the group's yields: actions, asked to witness, then the charts
  const shown = g.DEFAULT_WIDGETS.filter(w => !g.hiddenOnFirstDay(w, some)).map(w => w.i)
  assert.deepEqual(shown, ['actions', 'awaiting', 'yield', 'month', 'inventory', 'spectra'])
})

test('dashboard: "Your first record" is not shown to someone who leads a group', () => {
  const f = (d, props = {}) => html(g.FirstSteps, { canWrite: true, onNew: () => {}, onHide: () => {}, alone: false, firstRecord: g.wantsFirstRecord(d), ...props })
  const empty = { recent: [] }
  assert.equal(f({ lists: empty, leads_group: true }), '', 'a group lead with no records is told to write a first one')
  const student = f({ lists: empty, leads_group: false })
  assert.match(student, /Your first record/, 'a new member with no records no longer gets the card')
  assert.match(f({ lists: empty }), /Your first record/, 'an older server (no leads_group) loses the card')
  assert.equal(f({ lists: { recent: [{ id: 1 }] }, leads_group: false }), '')
  // the admin on their own still gets "Add your group", lead or not, exactly as before
  const adminLead = f({ lists: empty, leads_group: true }, { alone: true })
  assert.match(adminLead, /<h2 id="first-steps-h">Add your group<\/h2>/)
  assert.match(adminLead, /href="\/admin\/users"/)
  assert.doesNotMatch(adminLead, /Your first record/)
  assert.equal(adminLead, f({ lists: { recent: [{ id: 1 }] } }, { alone: true }))
  const adminNew = f({ lists: empty, leads_group: false }, { alone: true })
  assert.match(adminNew, /Your first record/)
  assert.match(adminNew, /<h3>Add your group<\/h3>/)
})

test('a signature in one plain line: "time certified" only when it was', () => {
  const s = { meaning: 'author', signer_name: 'Sam Patel', user: { id: 1, username: 'sam', full_name: 'Sam Patel' }, signed_at: '2026-11-09T14:42:00Z' }
  assert.match(g.sigLine({ ...s, timestamp: { status: 'ok' } }), /^Signed by Sam Patel, .+ · time certified$/)
  for (const timestamp of [null, undefined, { status: 'pending' }, { status: 'failed' }])
    assert.doesNotMatch(g.sigLine({ ...s, timestamp }), /certified/)
  assert.match(g.sigLine({ ...s, meaning: 'witness', signer_name: 'Helen Carter', timestamp: null }), /^Witnessed by Helen Carter, /)
})

test('reaction table amounts to 4 significant figures, for display only', () => {
  assert.equal(g.sig4(9.98985), '9.99')
  assert.equal(g.sig4(247.1805), '247.2')
  assert.equal(g.sig4(1360.13), '1360')
  assert.equal(g.sig4(0.000123456), '0.0001235')
  assert.equal(g.sig4(null), '')
})
