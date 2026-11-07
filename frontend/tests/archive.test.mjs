// 0.34: the archive PDF page's helpers (src/archive.ts): the quick choices of days, From and To checked, the instants
// sent to the server (in this browser's time zone), what it says about the records and the job, and which spectra are
// left to draw when the page is opened again. Run in Vancouver's time zone, so a day isn't a UTC day.
process.env.TZ = 'America/Vancouver'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import {
  active, countLine, defaultSpan, fraction, instants, megabytes, previewQuery, PRINT, progressLine, quickPicks, rangeProblem, spanBody, toDraw,
} from '../src/archive.ts'

const job = (over = {}) => ({ state: 'drawing', progress: { done: 0, total: 85, message: '' }, spectra_total: 140, spectra_done: 11, error: '', ...over })
const picks = now => Object.fromEntries(quickPicks(now).map(p => [p.label, `${p.from}..${p.to}`]))

test('the count line says how many records and of which kind', () => {
  assert.equal(countLine({ records: 85, witnessed: 70, awaiting_witness: 12, voided: 3 }), '85 records: 70 witnessed, 12 awaiting witness, 3 voided')
  assert.equal(countLine({ records: 1, witnessed: 0, awaiting_witness: 1, voided: 0 }), '1 record: 0 witnessed, 1 awaiting witness')
  assert.equal(countLine({ records: 0, witnessed: 0, awaiting_witness: 0, voided: 0 }), 'No records')
})

// Every date here is in the past, where Vancouver's clock rules are settled: time-zone data changes with the law
// (British Columbia's permanent daylight time, for one), and a test of a future date would follow it.
test('quick choices: this and last week (from Monday), month and year, and the four full years before', () => {
  // Saturday 9 November 2024, late in the evening in Vancouver (UTC−8: already Sunday in UTC)
  assert.deepEqual(picks(new Date('2024-11-10T06:30:00Z')), {
    'This week': '2024-11-04..2024-11-09', 'Last week': '2024-10-28..2024-11-03',
    'This month': '2024-11-01..2024-11-09', 'Last month': '2024-10-01..2024-10-31',
    'This year': '2024-01-01..2024-11-09', 'Last year': '2023-01-01..2023-12-31',
    2023: '2023-01-01..2023-12-31', 2022: '2022-01-01..2022-12-31', 2021: '2021-01-01..2021-12-31', 2020: '2020-01-01..2020-12-31',
  })
  assert.deepEqual(quickPicks(new Date(2024, 10, 9)).map(p => p.label),
    ['This week', 'Last week', 'This month', 'Last month', 'This year', 'Last year', '2023', '2022', '2021', '2020'])
})

test('quick choices at the edges: a Monday, a Sunday, the 1st of January, a leap February', () => {
  const mon = picks(new Date(2024, 10, 4, 9))
  assert.equal(mon['This week'], '2024-11-04..2024-11-04')
  assert.equal(mon['Last week'], '2024-10-28..2024-11-03')
  assert.equal(picks(new Date(2024, 10, 10, 23, 59))['This week'], '2024-11-04..2024-11-10')
  const jan1 = picks(new Date(2025, 0, 1, 0, 5))
  assert.equal(jan1['This year'], '2025-01-01..2025-01-01')
  assert.equal(jan1['Last month'], '2024-12-01..2024-12-31')
  assert.equal(jan1['Last week'], '2024-12-23..2024-12-29')
  assert.equal(picks(new Date(2024, 2, 10))['Last month'], '2024-02-01..2024-02-29')
  assert.deepEqual(defaultSpan(new Date(2024, 10, 9)), { label: 'This year', from: '2024-01-01', to: '2024-11-09' })
})

test('From later than To is refused plainly; so is a day that is not a day', () => {
  assert.equal(rangeProblem('2024-11-11', '2024-11-17'), '')
  assert.equal(rangeProblem('2024-11-11', '2024-11-11'), '')
  assert.match(rangeProblem('2024-11-17', '2024-11-11'), /“From” is later than “To”/)
  assert.match(rangeProblem('', '2024-11-11'), /Choose a “From” and a “To” day/)
  assert.match(rangeProblem('2025-02-30', '2025-03-03'), /Choose/)
})

test('the days go to the server as this browser\'s midnights, in UTC, across a change of the clocks too', () => {
  assert.deepEqual(instants('2024-11-11', '2024-11-17'), { start: '2024-11-11T08:00:00.000Z', end: '2024-11-18T08:00:00.000Z' })
  // the clocks went back on 3 November 2024 in Vancouver: the first midnight is at UTC−7, the last at UTC−8
  assert.deepEqual(instants('2024-10-28', '2024-11-10'), { start: '2024-10-28T07:00:00.000Z', end: '2024-11-11T08:00:00.000Z' })
  // and forward on 9 March 2025: the other way round
  assert.deepEqual(instants('2025-03-03', '2025-03-16'), { start: '2025-03-03T08:00:00.000Z', end: '2025-03-17T07:00:00.000Z' })
  const c = { from: '2024-11-11', to: '2024-11-17', notebook_id: 4, author_id: null, page_size: 'a4' }
  assert.deepEqual(spanBody(c, 'America/Vancouver'), { from_date: '2024-11-11', to_date: '2024-11-17', start: '2024-11-11T08:00:00.000Z',
    end: '2024-11-18T08:00:00.000Z', tz: 'America/Vancouver', notebook_id: 4, author_id: null, page_size: 'a4' })
  assert.equal(previewQuery(c, 'America/Vancouver'), 'from_date=2024-11-11&to_date=2024-11-17&start=2024-11-11T08%3A00%3A00.000Z'
    + '&end=2024-11-18T08%3A00%3A00.000Z&tz=America%2FVancouver&notebook_id=4')
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
  assert.match(page, /type="date"/, 'From and To are days')
  assert.match(readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'), /<Route path="\/archive" element=\{<Archive \/>\} \/>/)
  assert.match(readFileSync(new URL('../src/pages/Account.tsx', import.meta.url), 'utf8'), /to="\/archive">Archive PDF</)
  assert.match(readFileSync(new URL('../src/pages/NotebookPage.tsx', import.meta.url), 'utf8'), /to=\{`\/archive\?notebook=\$\{nb\.id\}`\}/)
})
