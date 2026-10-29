import assert from 'node:assert/strict'
import { test } from 'node:test'
import * as viewable from '../src/viewable.ts'

const { canViewSpectrum } = viewable

test('a .txt, .csv or .tsv is read before "View spectrum" is offered (regression: a .txt of notes offered it)', () => {
  const { needsReadCheck } = viewable
  assert.equal(typeof needsReadCheck, 'function', 'no check: every .txt offers View spectrum')
  for (const filename of ['notes.txt', 'trace.csv', 'uv.TSV']) assert.ok(needsReadCheck({ filename, size: 20_000 }), filename)
  for (const filename of ['crude.jdx', 'JL-013_1.zip', 'scan.xy', 'report.pdf']) assert.equal(needsReadCheck({ filename, size: 20_000 }), false, filename)
  assert.equal(needsReadCheck({ filename: 'big.csv', size: 50_000_000 }), false, 'a big one is not downloaded just to check it')
})

test('spectra and zipped NMR datasets open in the viewer', () => {
  assert.ok(canViewSpectrum({ filename: 'crude.jdx', size: 40_000 }))
  assert.ok(canViewSpectrum({ filename: 'JL-013_1.zip', size: 900_000 }))
  assert.ok(canViewSpectrum({ filename: 'trace.csv', size: 1_000 }))
})

test('raw LC-MS and other vendor data do not (regression: a 2 GB .D zip offered "View spectrum")', () => {
  for (const filename of ['EXP-00042-purity.D.zip', 'run_waters.raw.zip', 'sample.wiff.zip', 'PROTON_01.fid.zip'])
    assert.equal(canViewSpectrum({ filename, size: 5_000_000 }), false, filename)
})

test('very large files are download-only', () => {
  assert.equal(canViewSpectrum({ filename: 'huge.zip', size: 2_000_000_000 }), false)
  assert.equal(canViewSpectrum({ filename: 'report.pdf', size: 10 }), false)
})
