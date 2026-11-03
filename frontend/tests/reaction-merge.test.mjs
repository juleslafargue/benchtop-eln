// 0.30.1: a recalculated reaction table that arrives late is merged into the section as it is now (src/stoichMerge.ts).
// Before, it was applied to the copy that was sent, so a name, a note or the conditions typed during a slow
// recalculation were put back as they were, and then saved that way.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mergeStoich } from '../src/stoichMerge.ts'

const row = (name, extra = {}) => ({ role: 'reactant', name, smiles: 'C', mw: 16, equiv: 1, mmol: null, mass_mg: null, notes: '', ...extra })
const sent = {
  reaction_smiles: '', conditions: { temperature_c: 25, notes: '' },
  rows: [row('Benzyl azide', { limiting: true, basis: 'mass', mass_mg: 133 }), row('Phenylacetylene', { equiv: 1.2 })],
}
const answer = {
  rows: [row('Benzyl azide', { limiting: true, basis: 'mass', mass_mg: 133, mmol: 1 }), row('Phenylacetylene', { equiv: 1.2, mmol: 1.2, mass_mg: 122.6 })],
  totals: { limiting_mmol: 1 }, warnings: [],
}

test('names, notes and conditions typed while it was calculating stay', () => {
  const now = structuredClone(sent)
  now.rows[1].name = 'Phenylacetylene (freshly distilled)'
  now.rows[0].notes = 'weighed on balance 2'
  now.conditions = { ...now.conditions, notes: 'degassed with argon', atmosphere: 'Ar', temperature_label: 'ice bath' }
  const m = mergeStoich(sent, now, answer)
  assert.equal(m.rows[1].name, 'Phenylacetylene (freshly distilled)')
  assert.equal(m.rows[0].notes, 'weighed on balance 2')
  assert.deepEqual(m.conditions, { temperature_c: 25, notes: 'degassed with argon', atmosphere: 'Ar', temperature_label: 'ice bath' })
  // and the numbers are the server's
  assert.equal(m.rows[1].mmol, 1.2)
  assert.equal(m.rows[1].mass_mg, 122.6)
  assert.deepEqual(m.totals, { limiting_mmol: 1 })
})

test('a save that brought back the server’s own recalculation meanwhile is no reason to throw the typing away', () => {
  const now = structuredClone(answer)  // the autosave's answer: the same numbers, worked out on save
  now.reaction_smiles = ''
  now.conditions = { temperature_c: 25, notes: 'degassed with argon' }
  const m = mergeStoich(sent, now, answer)
  assert.ok(m)
  assert.equal(m.conditions.notes, 'degassed with argon')
  assert.deepEqual(m.rows, answer.rows)
})

test('nothing typed meanwhile: the answer as it came', () => {
  const m = mergeStoich(sent, structuredClone(sent), answer)
  assert.deepEqual(m.rows, answer.rows)
  assert.deepEqual(m.conditions, sent.conditions)
})

test('rows added or removed, or an amount changed meanwhile: worked out again, never applied over them', () => {
  const added = structuredClone(sent); added.rows.push(row('Copper(II) sulfate'))
  assert.equal(mergeStoich(sent, added, answer), null)
  const removed = structuredClone(sent); removed.rows.pop()
  assert.equal(mergeStoich(sent, removed, answer), null)
  const amount = structuredClone(sent); amount.rows[1].equiv = 1.5
  assert.equal(mergeStoich(sent, amount, answer), null)
})
