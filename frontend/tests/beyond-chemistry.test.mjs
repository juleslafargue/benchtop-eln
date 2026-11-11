// 0.36, "easier for biologists and pharmacologists": what a walk-through of 0.35.1 by a cell biologist and a
// pharmacologist found, in the browser's code.
// - The follow-up dialog stayed open after Create and reopened about the new draft (src/pages/ExperimentPage.tsx).
// - New experiment offers a start without chemistry, and remembers the last choice (NewExperimentDialog.tsx).
// - A table pasted from a spreadsheet with a header row: the headings become the column names (src/tablePaste.ts).
// - TIFF pictures are shown from a PNG the server makes; formats no browser shows say so (src/viewable.ts).
// - In Add section, "table" + Enter added a Reaction (its description says "stoichiometry table") (AddSection.tsx).
// - The sign dialog lists a plugin section's required items before the password, by the server's rule (signCheck.ts).
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

const src = p => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8')
const CASES = JSON.parse(readFileSync(new URL('./data/required-cases.json', import.meta.url), 'utf8')).cases
let g

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-v036-'))
  const out = join(dir, 'v036.mjs')
  const stub = { name: 'app-stub', setup(b) {
    b.onResolve({ filter: /^\.\.\/App$/ }, () => ({ path: 'app', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export const useApp = () => globalThis.__benchtopApp', loader: 'js' }))
  } }
  await build({
    stdin: { contents: `export * from './tablePaste'
      export { pictureKind, formatName, isTiff } from './viewable'
      export { matchSections, SECTION_KINDS } from './components/AddSection'
      export { missingRequired, requiredText, reminderText } from './signCheck'
      export { default as NewExperimentDialog, startingChoice } from './components/NewExperimentDialog'
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

// ---------------------------------------------------------------- 1. the follow-up dialog

test('the follow-up dialog closes once the follow-up is made, and nothing stays open going to another record', () => {
  const page = src('pages/ExperimentPage.tsx')
  const at = page.indexOf('<FollowUpDialog ')
  const dialog = at < 0 ? '' : page.slice(at, page.indexOf('/>', at))
  assert.match(dialog, /onDone=\{\(\) => setFollowUp\(false\)\}/, 'the dialog stays open after Create (0.35.1)')
  // one page per record: going to another record (the new follow-up, a link) starts afresh, every dialog closed
  assert.match(page, /export default function ExperimentPage\(\) \{\s*const \{ id \} = useParams\(\)\s*return <ExperimentRecord key=\{id\} \/>/)
  // and the dialog leaves when it's done, before going to the new record
  const fu = src('components/FollowUpDialog.tsx')
  assert.ok(fu.indexOf('onDone?.()') > 0 && fu.indexOf('onDone?.()') < fu.indexOf('nav(`/experiments/${r.id}`)'))
})

// ---------------------------------------------------------------- 2. a start without chemistry

const me = { id: 7, username: 'pnair', full_name: 'Priya Nair', role: 'scientist' }
const nb = { id: 5, name: 'Assays', owner: me, created_at: '2026-02-01T09:00:00Z', archived: false }
const dialog = prefs => {
  globalThis.__benchtopApp = { user: { ...me, preferences: prefs }, notebooks: [nb], refreshNotebooks: async () => {}, toast: () => {}, groups: [], setUser: () => {} }
  return g.renderToStaticMarkup(g.createElement(g.MemoryRouter, null, g.createElement(g.NewExperimentDialog, { onClose: () => {} })))
}

test('New experiment offers a start without chemistry, and the standard one stays the default', () => {
  const h = dialog({})
  assert.match(h, /<option value="standard" selected="">Standard sections \(aim, reaction, procedure, results\)<\/option>/)
  assert.match(h, /<option value="no_chemistry">Without chemistry \(aim, procedure, results\)<\/option>/)
  assert.match(dialog({ start_from: 'no_chemistry' }), /<option value="no_chemistry" selected="">Without chemistry/)
  const send = src('components/NewExperimentDialog.tsx')
  assert.match(send, /start === 'no_chemistry' \? \{ sections: 'no_chemistry' \} : \{\}/)
  assert.match(send, /preferences: \{ start_from: start \}/, 'the choice is not remembered in the account')
})

test('the remembered start: a template that has gone falls back to the standard sections', () => {
  assert.equal(g.startingChoice(undefined, null), 'standard')
  assert.equal(g.startingChoice('no_chemistry', []), 'no_chemistry')
  assert.equal(g.startingChoice('standard', [{ id: 3 }]), 'standard')
  assert.equal(g.startingChoice(3, [{ id: 3 }, { id: 4 }]), 3)
  assert.equal(g.startingChoice(9, [{ id: 3 }]), 'standard', 'a deleted template')
  assert.equal(g.startingChoice(9, null), 9, 'kept while the templates load')
  assert.equal(g.startingChoice('9', [{ id: 9 }]), 'standard')
  // while it's being checked, Create waits rather than sending a template that may have gone
  assert.match(dialog({ start_from: 9 }), /Template \(loading…\)/)
})

test('"Your first record" fits work without a reaction', () => {
  const dash = src('pages/Dashboard.tsx')
  assert.match(dash, /<b>Write what you did\.<\/b> For a reaction, draw or paste it and the table works out the amounts; for anything\s+else, add text, tables and files\./)
  assert.doesNotMatch(dash, /Write the reaction and the procedure/)
})

// ---------------------------------------------------------------- 3. pasting a table with a header row

const blank = () => ({ cols: [{ name: 'Sample' }, { name: 'Value' }], rows: [['', '']] })

test('a header row pasted into a blank table names the columns', () => {
  const { cols, rows } = blank()
  const text = 'Sample\tConcentration (µM)\tViability, 72 h (% of vehicle)\r\nVehicle\t0\t100\nKX-104\t0.1\t96.2\nKX-104\t1\t71.5\n'
  const r = g.paste(cols, rows, text, 0, 0)
  assert.equal(r.header, true)
  assert.deepEqual(r.cols.map(c => c.name), ['Sample', 'Concentration (µM)', 'Viability, 72 h (% of vehicle)'])
  assert.deepEqual(r.rows, [['Vehicle', '0', '100'], ['KX-104', '0.1', '96.2'], ['KX-104', '1', '71.5']])
  // "Keep it as data": the same paste with the first line as a row, as before 0.36
  const asData = g.pasteCells(cols, rows, g.parseTsv(text), 0, 0, false)
  assert.deepEqual(asData.cols.map(c => c.name), ['Sample', 'Value', 'Column 3'])
  assert.deepEqual(asData.rows[0], ['Sample', 'Concentration (µM)', 'Viability, 72 h (% of vehicle)'])
  assert.equal(asData.rows.length, 4)
})

test('numbers, a single line, or a table already in use: the first line stays data', () => {
  const { cols, rows } = blank()
  const numbers = g.paste(cols, rows, '0\t100\n0.1\t96.2\n1\t71.5', 0, 0)
  assert.equal(numbers.header, false)
  assert.deepEqual(numbers.rows[0], ['0', '100'])
  assert.deepEqual(numbers.cols.map(c => c.name), ['Sample', 'Value'])
  assert.equal(g.paste(cols, rows, 'Sample\tConcentration', 0, 0).header, false, 'one line: nothing to tell headings from data')
  assert.equal(g.paste(cols, rows, 'Dose\t1\t10\nKX-104\t96\t71', 0, 0).header, false, 'a number among the headings')
  assert.equal(g.paste(cols, rows, 'Sample\t\tViability\nWT\t1\t2', 0, 0).header, false, 'an empty heading')
  const named = [{ name: 'Clone' }, { name: 'Value' }]
  assert.equal(g.paste(named, rows, 'Sample\tViability\nWT\t95', 0, 0).header, false, 'columns already named')
  assert.equal(g.paste(cols, [['WT', '']], 'Sample\tViability\nKO\t95', 1, 0).header, false, 'a cell already typed in')
  assert.equal(g.paste([{ name: 'Column 1' }, { name: '' }, { name: 'Column 3' }], [['', ' ', '']], 'A\tB\tC\n1\t2\t3', 0, 0).header, true)
  for (const n of ['12', '-0.5', '1,000', '1 000', '2.5e-3', '45%', '−3', '0,25']) assert.ok(g.isNumber(n), n)
  for (const t of ['n.d.', '<0.01', 'KX-104', 'P12', '72 h']) assert.ok(!g.isNumber(t), t)
})

test('long column names are not cut off', () => {
  const t = src('components/TableEntry.tsx')
  assert.match(t, /title=\{c\.name\}\s+style=\{\{ minWidth: columnWidth\(c\.name, rows, ci\) \}\}/)
  assert.match(t, /toast\('First row used as column names', \{ label: 'Keep it as data'/)
})

// ---------------------------------------------------------------- 4. TIFF

test('a TIFF is shown from a preview; a format no browser shows says so; other pictures as they are', () => {
  const a = (filename, content_type) => ({ filename, content_type })
  assert.equal(g.pictureKind(a('blot.tif', 'image/tiff')), 'preview')
  assert.equal(g.pictureKind(a('Stack.TIFF', 'application/octet-stream')), 'preview')
  assert.equal(g.pictureKind(a('photo.jpg', 'image/jpeg')), 'img')
  assert.equal(g.pictureKind(a('plate.png', 'image/png')), 'img')
  assert.equal(g.pictureKind(a('IMG_0001.HEIC', 'image/heic')), 'none')
  assert.equal(g.pictureKind(a('raw.cr2', 'image/x-canon-cr2')), 'none')
  assert.equal(g.pictureKind(a('data.csv', 'text/csv')), null)
  assert.equal(g.formatName(a('blot.tif', 'image/tiff')), 'TIFF')
  assert.equal(g.formatName(a('IMG_0001.HEIC', 'image/heic')), 'HEIC')
  const fe = src('components/FileEntry.tsx')
  assert.match(fe, /No preview for \{formatName\(att\)\}\. Download it to view\./)
  assert.match(fe, /`\/api\/attachments\/\$\{att\.id\}\/preview`/)
  assert.doesNotMatch(fe, /content_type\.startsWith\('image\/'\) && <div/, 'every image/* still gets an <img>')
})

// ---------------------------------------------------------------- 6. Add section

test('"table" in Add section puts the Data table first, not the Reaction (0.35.1: Enter added a Reaction)', () => {
  const names = q => g.matchSections(g.SECTION_KINDS, q).map(k => k.label)
  assert.equal(names('table')[0], 'Data table')
  assert.ok(names('table').includes('Reaction'), 'its description still matches, lower down')
  assert.ok(names('table').indexOf('Reaction') > names('table').indexOf('Data table'))
  assert.equal(names('react')[0], 'Reaction')
  assert.equal(names('Files')[0], 'Files')
  assert.equal(names('nmr')[0], 'Characterisation')
  assert.deepEqual(names(''), g.SECTION_KINDS.map(k => k.label))
  assert.deepEqual(names('text procedure'), ['Text'], 'a name and description together still match')
  assert.deepEqual(names('zzz'), [])
})

// ---------------------------------------------------------------- 7. required items in the sign dialog

test('the sign dialog lists exactly the required items the server would refuse (backend/app/plugins.py)', () => {
  assert.ok(CASES.length >= 6)
  for (const c of CASES) assert.deepEqual(g.missingRequired(c.content), c.missing, c.name)
  const entries = [
    { kind: 'text', title: 'Aim', content: { html: '<p>x</p>' } },
    { kind: 'custom', title: 'Western blot', content: CASES.find(c => c.name === 'new').content },
    { kind: 'custom', title: 'Dose–response', content: CASES.find(c => c.name === 'complete').content },
    { kind: 'custom', title: 'Transfection', content: CASES.find(c => c.name === 'a filled row without its required cell').content },
  ]
  assert.equal(g.requiredText(entries), 'Western blot: Compound, Passage, Result, Uncropped image added, Concentrations (at least 3 rows), '
    + 'Wells (at least 1 row); Transfection: Concentrations: Concentration')
  assert.equal(g.requiredText(entries.slice(2, 3)), '')
})

test('the sign dialog says so before the password, and Sign stays off; nothing more is sent', () => {
  const d = src('components/SignDialog.tsx')
  const required = d.indexOf('Fill in the required items before signing:'), password = d.indexOf('Re-enter your password')
  assert.ok(required > 0 && required < password, 'shown after the password box, or not at all')
  assert.match(/disabled=\{([^}]+)\}\s+onClick=\{go\}/.exec(d)[1], /action === 'sign' && !!required/)
  assert.match(d, /setRequired\(requiredText\(x\.entries\)\)/, 'read from what is saved, as the server reads it')
  assert.match(d, /const body: Record<string, unknown> = \{ password, comment \}/)
  assert.match(d, /\{!required && <span className="faint"> You can sign anyway/, '"You can sign anyway" beside items that stop signing')
})
