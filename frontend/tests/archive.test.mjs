// 0.34: the yearly archive page's helpers (src/archive.ts): what it says about the records and the job, the
// choices it sends, and which spectra are left to draw when the page is opened again.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { active, countLine, defaultYear, fraction, megabytes, previewQuery, PRINT, progressLine, toDraw } from '../src/archive.ts'

const job = (over = {}) => ({ state: 'drawing', progress: { done: 0, total: 85, message: '' }, spectra_total: 140, spectra_done: 11, error: '', ...over })

test('the count line says how many records and of which kind', () => {
  assert.equal(countLine({ records: 85, witnessed: 70, awaiting_witness: 12, voided: 3 }), '85 records: 70 witnessed, 12 awaiting witness, 3 voided')
  assert.equal(countLine({ records: 1, witnessed: 0, awaiting_witness: 1, voided: 0 }), '1 record: 0 witnessed, 1 awaiting witness')
  assert.equal(countLine({ records: 0, witnessed: 0, awaiting_witness: 0, voided: 0 }), 'No records')
})

test('the choices go to the server as they are, and only those made', () => {
  assert.equal(previewQuery({ year: 2026, notebook_id: null, author_id: null, page_size: 'a4' }), 'year=2026')
  assert.equal(previewQuery({ year: 2025, notebook_id: 4, author_id: 7, page_size: 'letter' }), 'year=2025&notebook_id=4&author_id=7')
})

test('the year offered first: the one asked for, else the latest with records, else this year', () => {
  const years = [{ year: 2026 }, { year: 2024 }]
  assert.equal(defaultYear(years, 2024), 2024)
  assert.equal(defaultYear(years, 2019), 2026)
  assert.equal(defaultYear(years, null), 2026)
  assert.equal(defaultYear([], null, new Date('2031-03-01T00:00:00Z')), 2031)
})

test('what the page says while it works', () => {
  assert.equal(progressLine(job(), 12), 'Drawing spectrum 12 of 140')
  assert.equal(progressLine(job()), 'Drawing spectrum 12 of 140')  // reopened: the next one to draw
  assert.equal(progressLine(job({ spectra_total: 0, spectra_done: 0 })), 'Getting ready')
  assert.equal(progressLine(job({ state: 'building', progress: { done: 29, total: 85, message: 'Adding record 30 of 85' } })), 'Adding record 30 of 85')
  assert.equal(progressLine(job({ state: 'queued', progress: { done: 0, total: 85, message: '' } })), 'Waiting for another archive to finish')
  assert.equal(progressLine(job({ state: 'done' })), 'Ready')
  assert.match(progressLine(job({ state: 'stopped', error: 'Benchtop stopped while this archive was being made. Start it again.' })), /Start it again/)
  assert.ok(active('drawing') && active('queued') && active('building') && !active('done') && !active('stopped') && !active('failed'))
})

test('progress runs from 0 to 1: drawing first, then laying out', () => {
  assert.equal(fraction(job({ spectra_done: 0 })), 0)
  assert.equal(fraction(job({ spectra_done: 70 })), 0.25)
  assert.equal(fraction(job({ state: 'building', progress: { done: 85, total: 85, message: '' } })), 1)
  assert.equal(fraction(job({ state: 'building', spectra_total: 0, progress: { done: 17, total: 85, message: '' } })), 0.2)
  assert.equal(fraction(job({ state: 'done' })), 1)
})

test('reopening the page draws only what is missing, in order', () => {
  const s = [1, 2, 3, 4].map(i => ({ attachment_id: i, done: i % 2 === 0 }))
  assert.deepEqual(toDraw(s).map(x => x.attachment_id), [1, 3])
})

test('spectra are drawn at print resolution, sizes read plainly', () => {
  assert.ok(PRINT.w * PRINT.scale >= 2100, 'about 300 dpi across 18 cm')
  assert.ok(PRINT.w * PRINT.scale <= 6000 && PRINT.h * PRINT.scale <= 6000, 'within what the server takes')
  assert.equal(megabytes(2_400_000), '2.4 MB')
  assert.equal(megabytes(31_000_000), '31 MB')
  assert.equal(megabytes(80_000), '80 kB')
  assert.equal(megabytes(null), '')
})

test('the page draws with the viewer\'s own code, never a copy of it, and is reachable from Account and a notebook', () => {
  const page = readFileSync(new URL('../src/pages/Archive.tsx', import.meta.url), 'utf8')
  assert.match(page, /import \{ prepareSpectrum, spectrumPng \} from '\.\.\/spectrumDraw'/)
  assert.doesNotMatch(page, /drawSpectrum\(/)
  assert.match(page, /canViewSpectrum/)
  assert.match(readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'), /<Route path="\/archive" element=\{<Archive \/>\} \/>/)
  assert.match(readFileSync(new URL('../src/pages/Account.tsx', import.meta.url), 'utf8'), /to="\/archive">Yearly archive</)
  assert.match(readFileSync(new URL('../src/pages/NotebookPage.tsx', import.meta.url), 'utf8'), /to=\{`\/archive\?notebook=\$\{nb\.id\}`\}/)
})
