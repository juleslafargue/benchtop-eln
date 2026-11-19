// 0.36.4: "Deduct amounts from inventory" puts its marks ("✓ used") into the section as it is now (src/stoichMerge.ts).
// Before, the section was put back as it was when Deduct was clicked: names and notes typed while it ran (seconds on a
// slow network) were wiped; and a second click while it ran deducted everything again.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { stillUnmarked, withDeduction, withoutDeduction } from '../src/stoichMerge.ts'

const row = (name, lot_id, extra = {}) => ({ role: 'reactant', name, lot_id, mass_mg: 50, notes: '', ...extra })
const mark = { tx_id: 41, amount: 50, unit: 'mg' }

test('the mark goes on its row, and what was typed meanwhile stays', () => {
  const now = { conditions: { notes: 'degassed' }, rows: [row('Benzoic acid (typed meanwhile)', 7, { notes: 'balance 2' }), row('Toluene', 9)] }
  const out = withDeduction(now, 0, 7, mark)
  assert.deepEqual(out.rows[0], { ...now.rows[0], deducted: mark })
  assert.equal(out.rows[0].name, 'Benzoic acid (typed meanwhile)')
  assert.equal(out.rows[0].notes, 'balance 2')
  assert.deepEqual(out.rows[1], now.rows[1])
  assert.deepEqual(out.conditions, { notes: 'degassed' })
})

test('a row added above it meanwhile: the mark still finds its row', () => {
  const now = { rows: [row('New row', null), row('Benzoic acid', 7)] }
  const out = withDeduction(now, 0, 7, mark)
  assert.equal(out.rows[0].deducted, undefined)
  assert.deepEqual(out.rows[1].deducted, mark)
})

test('the row was removed meanwhile: nowhere to put the mark (the page says so)', () => {
  assert.equal(withDeduction({ rows: [row('Toluene', 9)] }, 0, 7, mark), null)
})

test('a row already marked is never marked again, so it is not deducted twice', () => {
  const now = { rows: [row('Benzoic acid', 7, { deducted: { tx_id: 40, amount: 50, unit: 'mg' } })] }
  assert.equal(stillUnmarked(now, 0, 7), false)
  assert.equal(withDeduction(now, 0, 7, mark), null)
  assert.equal(stillUnmarked({ rows: [row('Benzoic acid', 7)] }, 0, 7), true)
  assert.equal(stillUnmarked({ rows: [row('Benzoic acid', 8)] }, 0, 7), false)  // another bottle chosen meanwhile
})

test('undo clears the mark for that withdrawal only, wherever its row is now', () => {
  const now = { rows: [row('New row', null), row('Benzoic acid', 7, { deducted: mark, notes: 'typed' }), row('Toluene', 9, { deducted: { tx_id: 42, amount: 1, unit: 'mL' } })] }
  const out = withoutDeduction(now, 41)
  assert.equal(out.rows[1].deducted, null)
  assert.equal(out.rows[1].notes, 'typed')
  assert.deepEqual(out.rows[2].deducted, { tx_id: 42, amount: 1, unit: 'mL' })
})
