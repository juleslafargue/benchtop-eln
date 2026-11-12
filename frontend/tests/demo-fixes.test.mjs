// 0.36.1, fixes from a practice run of the demo, in the browser's code:
// - Cells pasted from a spreadsheet into a plugin section's table (Western blot → Band intensities) all went into the
//   cell pasted into, and nothing was saved since that was a number column (src/tablePaste.ts, CustomEntry.tsx).
// - "Mentioned in" only looked when the record opened, not when you came back to its tab (src/onReturn.ts).
// - A signed record's risk assessment said "Save these hazards with the record keeps…" with no such button
//   (src/components/SafetyEntry.tsx).
// (The archive's timestamp wording is checked in backend/tests/test_v36_1_demo_fixes.py, and the 125% zoom layout in
// backend/tests/smoke_browser.py, "zoomed".)
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'

const src = p => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8')

/** Bundle a few of the app's modules for node (each test its own, so one missing module fails only its own tests). */
async function load(contents) {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-v0361-'))
  const out = join(dir, 'm.mjs')
  const stub = { name: 'app-stub', setup(b) {
    b.onResolve({ filter: /^\.\.\/App$/ }, () => ({ path: 'app', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const useApp = () => globalThis.__benchtopApp', loader: 'js' }))
  } }
  try {
    await build({ stdin: { contents, resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'tsx' },
      bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'silent', jsx: 'automatic',
      define: { 'process.env.NODE_ENV': '"production"' }, plugins: [stub],
      banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" } })
    return await import(pathToFileURL(out).href)
  } finally { rmSync(dir, { recursive: true, force: true }) }
}

// the example plugin's Western blot tables (plugins/cell-biology.toml)
const BANDS = [{ id: 'lane', label: 'Lane', type: 'number' }, { id: 'sample', label: 'Sample', type: 'text' },
  { id: 'target', label: 'Target', type: 'number' }, { id: 'loading', label: 'Loading control', type: 'number' }]
const ANTIBODIES = [{ id: 'target', label: 'Target', type: 'text' }, { id: 'kind', label: 'Primary or secondary', type: 'select', options: ['Primary', 'Secondary'] },
  { id: 'clone', label: 'Supplier, catalogue no. and lot', type: 'text' }, { id: 'dilution', label: 'Dilution', type: 'text' }]
const blank = cols => Object.fromEntries(cols.map(c => [c.id, c.type === 'checkbox' ? false : null]))

// ---------------------------------------------------------------- 1. pasting into a plugin section's table

test('cells pasted into a plugin table fill its rows and columns from the cell pasted into, adding rows', async () => {
  const t = await load("export * from './tablePaste'")
  const p = t.pasteFields(BANDS, [blank(BANDS)], '1\tWT\t1200\t900\n2\tKO\t640.5\t880\n3\tKO + rescue\t1100\t910\n', 0, 0)
  assert.equal(p.rows.length, 3)
  assert.equal(p.added, 2)
  assert.deepEqual(p.rows[0], { lane: 1, sample: 'WT', target: 1200, loading: 900 })
  assert.deepEqual(p.rows[1], { lane: 2, sample: 'KO', target: 640.5, loading: 880 })
  assert.equal(p.rows[2].sample, 'KO + rescue')
  assert.equal(p.filled, 12)
  assert.deepEqual(p.skipped, [])
  assert.equal(t.fieldPasteMessage(p), '', 'nothing to say when it all went in')
  // from the second column of the second row: the rest of the line goes right, extra cells are left out
  const q = t.pasteFields(BANDS, p.rows, 'WT again\t1\t2\t3\t4', 1, 1)
  assert.deepEqual(q.rows[1], { lane: 2, sample: 'WT again', target: 1, loading: 2 })
  assert.equal(q.extra, 2)
  assert.match(t.fieldPasteMessage(q), /2 cells beyond the last column were left out/)
})

test('a cell its column can\'t take is never stored as something else, and the person is told how many', async () => {
  const t = await load("export * from './tablePaste'")
  const start = [{ lane: 7, sample: 'kept', target: 5, loading: 6 }]
  const p = t.pasteFields(BANDS, start, 'one\tWT\t1,000\t900\n2\tKO\tn.d.\t1.5e3', 0, 0)
  assert.equal(p.rows[0].lane, 7, 'a word in a number column leaves the cell as it was')
  assert.equal(p.rows[0].target, 5, '"1,000" could be one thousand or one: not kept, as when typed')
  assert.equal(p.rows[1].target, null, 'a new row keeps its empty cell')
  assert.equal(p.rows[1].loading, 1500)
  assert.equal(p.skipped.length, 3)
  const say = t.fieldPasteMessage(p)
  assert.match(say, /^3 cells not pasted, left as they were: /)
  assert.match(say, /Lane takes numbers only/)
  assert.match(say, /Target takes numbers only/)
  // a choice must be one of its options (in any case: it's stored as the option is written)
  const a = t.pasteFields(ANTIBODIES, [blank(ANTIBODIES)], 'β-actin\tprimary\tAbcam ab8226\t1:5000\nGAPDH\tmonoclonal\tCST 2118\t1:1000', 0, 0)
  assert.equal(a.rows[0].kind, 'Primary')
  assert.equal(a.rows[1].kind, null)
  assert.equal(a.rows[1].target, 'GAPDH')
  assert.equal(t.fieldPasteMessage(a), '1 cell not pasted, left as it was: Primary or secondary takes Primary or Secondary.')
  // the other kinds of column
  assert.deepEqual(t.fieldValue({ id: 'x', label: 'Done', type: 'checkbox' }, 'yes'), { value: true })
  assert.deepEqual(t.fieldValue({ id: 'x', label: 'Done', type: 'checkbox' }, ''), { value: false })
  assert.ok('skip' in t.fieldValue({ id: 'x', label: 'Done', type: 'checkbox' }, 'maybe'))
  assert.deepEqual(t.fieldValue({ id: 'd', label: 'Day', type: 'date' }, '2026-11-12'), { value: '2026-11-12' })
  assert.ok('skip' in t.fieldValue({ id: 'd', label: 'Day', type: 'date' }, '12/11/2026'), 'a day that reads two ways isn\'t guessed')
  assert.deepEqual(t.fieldValue({ id: 'w', label: 'When', type: 'datetime' }, '2026-11-12 14:30'), { value: '2026-11-12T14:30' })
})

test('a pasted heading row that matches the column labels is left out, and nothing is renamed', async () => {
  const t = await load("export * from './tablePaste'")
  const p = t.pasteFields(BANDS, [blank(BANDS)], 'lane\tSAMPLE\tTarget\tLoading control\n1\tWT\t1200\t900', 0, 0)
  assert.equal(p.header, true)
  assert.equal(p.rows.length, 1)
  assert.deepEqual(p.rows[0], { lane: 1, sample: 'WT', target: 1200, loading: 900 })
  assert.equal(t.fieldPasteMessage(p), 'The heading row was left out.')
  assert.equal(t.isFieldHeader([{ id: 'c', label: 'Concentration', unit: 'µM', type: 'number' }], 0, ['Concentration (µM)']), true)
  // headings that aren't this table's are data (and here, not numbers)
  const q = t.pasteFields(BANDS, [blank(BANDS)], 'Lane\tName\n1\tWT', 0, 0)
  assert.equal(q.header, false)
  assert.equal(q.skipped.length, 1)
})

test('the plugin table hands a paste of several cells to pasteFields and says what was skipped', () => {
  const page = src('components/CustomEntry.tsx')
  assert.match(page, /onPaste=\{e => pasteInto\(e, t, r, i, ci\)\}/, 'no paste handler on the table\'s cells (0.36)')
  const at = page.indexOf('const pasteInto')
  const body = page.slice(at, page.indexOf('\n  }\n', at))
  assert.match(body, /pasteFields\(t\.columns, r, text, ri, ci\)/)
  assert.match(body, /e\.preventDefault\(\)/)
  assert.match(body, /toast\(say\)/)
  assert.match(body, /if \(readOnly\) return/)
})

// ---------------------------------------------------------------- 2. Mentioned in, when you come back

test('coming back to the tab or window calls the refresh; a hidden tab doesn\'t', async () => {
  const { onReturn } = await load("export { onReturn } from './onReturn'")
  const doc = new EventTarget(); doc.visibilityState = 'visible'
  const win = new EventTarget()
  let n = 0
  const stop = onReturn(() => { n++ }, doc, win)
  doc.dispatchEvent(new Event('visibilitychange'))
  win.dispatchEvent(new Event('focus'))
  assert.equal(n, 2)
  doc.visibilityState = 'hidden'
  doc.dispatchEvent(new Event('visibilitychange'))
  assert.equal(n, 2)
  stop()
  doc.visibilityState = 'visible'
  win.dispatchEvent(new Event('focus'))
  assert.equal(n, 2, 'nothing after the page has gone')
})

test('Mentioned in refreshes at the same moments as the rest of the record\'s page', () => {
  const page = src('pages/ExperimentPage.tsx')
  const at = page.indexOf('function MentionedIn(')
  const body = page.slice(at, page.indexOf('\n}\n', at))
  assert.match(body, /onReturn\(load\)/, 'Mentioned in only looked when the record opened (0.36)')
  assert.match(body, /\/mentioned-in`/)
  const rec = page.slice(page.indexOf('function ExperimentRecord('))
  assert.match(rec, /useEffect\(\(\) => onReturn\(load\), \[load\]\)/, 'the page itself uses the same')
  assert.doesNotMatch(rec, /addEventListener\('visibilitychange'/, 'one way of noticing, not two')
})

// ---------------------------------------------------------------- 3. a signed record's hazards

test('a signed or read-only risk assessment states what is so, never an action it can\'t take', async () => {
  const g = await load(`export { default as SafetyEntry } from './components/SafetyEntry'
    export { renderToStaticMarkup } from 'react-dom/server'
    export { createElement } from 'react'`)
  globalThis.__benchtopApp = { user: { role: 'scientist' } }
  const html = props => g.renderToStaticMarkup(g.createElement(g.SafetyEntry, { content: {}, onChange: () => {}, experimentId: 1,
    attachments: [], onAttachmentsChanged: () => {}, ...props })).replace(/&#x27;/g, "'")
  const ctx = { productFormula: null, productSmiles: null, productMW: null, openAttachmentId: null }
  const signed = html({ readOnly: true, context: { ...ctx, signed: true } })
  assert.match(signed, /Hazards weren't saved with this record when it was signed/)
  assert.doesNotMatch(signed, /Save these hazards/, 'it pointed to a button a signed record doesn\'t have (0.36)')
  const viewOnly = html({ readOnly: true, context: ctx })
  assert.match(viewOnly, /Hazards haven't been saved with this record/)
  assert.doesNotMatch(viewOnly, /Save these hazards|when it was signed/)
  const editable = html({ readOnly: false, context: ctx })
  assert.match(editable, /Save these hazards with the record<\/b> keeps/, 'whoever can save them is still told how')
  const saved = html({ readOnly: true, context: { ...ctx, signed: true }, content: { snapshot: { taken_at: '2026-11-01T10:00:00Z', chemicals: [], warnings: [] } } })
  assert.match(saved, /Hazards saved with the record/)
})
