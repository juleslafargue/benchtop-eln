// 0.30: deleting an empty notebook and archiving the rest. Which buttons a notebook's page shows (src/notebookActions.ts),
// how the audit trail words it (src/auditText.ts), and that archived notebooks stay out of the places new work goes.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

const src = p => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8')
let m

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-nbactions-'))
  const out = join(dir, 'nb.mjs')
  await build({ stdin: { contents: `export { notebookActions } from './notebookActions'; export { auditSentence } from './auditText'`,
    resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' }, bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error' })
  m = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

const owner = { id: 7, role: 'scientist' }, other = { id: 8, role: 'scientist' }, admin = { id: 1, role: 'admin' }, viewer = { id: 7, role: 'viewer' }
const nb = (empty, archived = false) => ({ empty, archived, owner: { id: 7 } })
const none = { delete: false, archive: false, unarchive: false }

test('an empty notebook offers Delete; one with experiments offers Archive instead, never both', () => {
  assert.deepEqual(m.notebookActions(nb(true), owner), { delete: true, archive: false, unarchive: false })
  assert.deepEqual(m.notebookActions(nb(false), owner), { delete: false, archive: true, unarchive: false })
  assert.deepEqual(m.notebookActions(nb(false), admin), { delete: false, archive: true, unarchive: false })
  assert.deepEqual(m.notebookActions(nb(true), admin), { delete: true, archive: false, unarchive: false })
})

test('an archived notebook offers Unarchive (and Delete, if it is empty)', () => {
  assert.deepEqual(m.notebookActions(nb(false, true), owner), { delete: false, archive: false, unarchive: true })
  assert.deepEqual(m.notebookActions(nb(true, true), owner), { delete: true, archive: false, unarchive: true })
})

test('only the owner or an admin sees them; never a read-only account, even its own notebook', () => {
  for (const n of [nb(true), nb(false), nb(false, true)]) {
    assert.deepEqual(m.notebookActions(n, other), none)
    assert.deepEqual(m.notebookActions(n, viewer), none)
  }
})

test('the audit trail says it plainly', () => {
  const names = { people: { jl: 'Jules Lafargue' }, userIds: {}, groups: {} }
  const ev = (action, details) => ({ action, object_type: 'notebook', object_id: 12, username: 'jl', details })
  assert.equal(m.auditSentence(ev('delete', { name: 'Test', group_id: null, visibility: 'everyone' }), names), 'Jules Lafargue deleted the empty notebook “Test”')
  assert.equal(m.auditSentence(ev('update', { name: 'Old project', before: { archived: false }, changes: { archived: true } }), names), 'Jules Lafargue archived the notebook “Old project”')
  assert.equal(m.auditSentence(ev('update', { name: 'Old project', before: { archived: true }, changes: { archived: false } }), names), 'Jules Lafargue unarchived the notebook “Old project”')
  assert.equal(m.auditSentence(ev('update', { before: { archived: false }, changes: { archived: true } }), names), 'Jules Lafargue archived notebook #12')
  assert.equal(m.auditSentence(ev('update', { changes: { name: 'New name' } }), names), null, 'other changes are shown as stored')
})

test('archived notebooks stay out of the places new experiments go, and can be found again', () => {
  const app = src('App.tsx')
  assert.match(app, /include_archived=true/)
  assert.match(app, /setNotebooks\(all\.filter\(n => !n\.archived\)\); setArchivedNotebooks\(all\.filter\(n => n\.archived\)\)/)
  // the sidebar's Archived (N) list shows only when there are some
  assert.match(src('components/Layout.tsx'), /archivedNotebooks\.length > 0 && <>/)
  assert.match(src('components/Layout.tsx'), /Archived \(\{archivedNotebooks\.length\}\)/)
  // an archived notebook's page: it says so, has Unarchive, and no New experiment or Import
  const page = src('pages/NotebookPage.tsx')
  assert.match(page, /archivedNotebooks\.find\(n => n\.id === Number\(id\)\)/)
  assert.match(page, /This notebook is archived\./)
  assert.match(page, /canWrite && !nb\.archived && <button className="btn primary" onClick=\{\(\) => setCreating\(true\)\}>New experiment/)
  assert.match(page, /Notebooks with experiments can't be deleted; archive them instead\./)
  // the Getting started guide explains it
  assert.match(src('pages/Guide.tsx'), /<b>Delete notebook<\/b>[\s\S]*<b>Archive notebook<\/b>[\s\S]*<b>Unarchive<\/b>/)
  // a record in an archived notebook: no Duplicate or Repeat (the copy would go into it)
  assert.match(src('pages/ExperimentPage.tsx'), /canWrite && !exp\.notebook_archived && <button className="btn sm" onClick=\{\(\) => clone\(false\)\}>Duplicate/)
})
