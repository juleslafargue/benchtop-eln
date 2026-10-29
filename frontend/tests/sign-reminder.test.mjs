// The sign dialog's reminder (src/signCheck.ts): it lists the sections still as they were added, an isolated mass
// that's missing and hazards not recorded. It is only shown: SignDialog never sends it, and never disables signing for it.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let m

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-sign-'))
  const out = join(dir, 'sign.mjs')
  await build({ entryPoints: [new URL('../src/signCheck.ts', import.meta.url).pathname], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error' })
  m = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

// a new experiment, as the server makes it (routers/experiments.py DEFAULT_SECTIONS)
const fresh = () => [
  { kind: 'text', title: 'Aim', content: { html: '' } },
  { kind: 'reaction', title: 'Reaction', content: { reaction_smiles: '', rows: [], totals: {}, conditions: {} } },
  { kind: 'safety', title: 'Safety & risk assessment', content: { controls: '', waste: '', emergency: '', notes_html: '', snapshot: null } },
  { kind: 'text', title: 'Procedure', content: { html: '<p></p>' } },
  { kind: 'purification', title: 'Work-up & purification', content: { method: 'column', stationary_phase: '', eluent: '', rf: '', rf_eluent: '', isolated_mg: null, appearance: '', notes_html: '' } },
  { kind: 'characterization', title: 'Characterisation', content: { items: [{ technique: '1H NMR', conditions: '', data: '', attachment_id: null }] } },
  { kind: 'text', title: 'Observations & results', content: { html: '' } },
]

test('a new experiment: every section is listed, and the hazards', () => {
  const r = m.stillEmpty(fresh())
  assert.deepEqual(r.sections, ['Aim', 'Reaction', 'Procedure', 'Work-up & purification', 'Characterisation', 'Observations & results'])
  assert.deepEqual(r.notes, ['hazards not recorded'])
  assert.equal(m.reminderText(fresh()), 'Aim, Reaction, Procedure, Work-up & purification, Characterisation, Observations & results · hazards not recorded')
})

test('filled sections drop out; a product without an isolated mass is said', () => {
  const e = fresh()
  e[0].content.html = '<p>Make the triazole.</p>'
  e[1].content.rows = [{ role: 'reactant', name: 'Benzyl azide' }, { role: 'product', name: 'Triazole', actual_mass_mg: null }]
  e[2].content.snapshot = { taken_at: '2026-10-01T10:00:00Z', chemicals: [], warnings: [] }
  e[3].content.html = '<p><img data-attachment-id="3"></p>'  // a picture alone is something
  e[5].content.items[0].attachment_id = 12
  assert.deepEqual(m.stillEmpty(e), { sections: ['Work-up & purification', 'Observations & results'], notes: ['no isolated mass'] })
  e[4].content.isolated_mg = 212.5  // the mass typed in the work-up counts as well as the product row's
  assert.equal(m.reminderText(e), 'Observations & results')
  e[6].content.html = '<p>White solid.</p>'
  assert.equal(m.reminderText(e), '')
})

test('the dialog only shows it: signing is not held back by it, and it is not sent', () => {
  const dialog = readFileSync(new URL('../src/components/SignDialog.tsx', import.meta.url), 'utf8')
  const button = dialog.match(/disabled=\{([^}]+)\}\s+onClick=\{go\}/)
  assert.ok(button, 'the sign button')
  assert.doesNotMatch(button[1], /empty|remind/i, 'the sign button waits for the reminder')
  const body = dialog.match(/const body: Record<string, unknown> = (\{[^}]*\})/)
  assert.equal(body[1], '{ password, comment }', 'what is sent when signing changed')
})
