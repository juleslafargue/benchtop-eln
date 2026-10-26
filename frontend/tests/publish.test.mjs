import assert from 'node:assert/strict'
import { test } from 'node:test'
import { doiHref, fileSize, firstEmbargoDay, parseKeywords, problems } from '../src/publish.ts'

const ready = {
  title: 'Suzuki coupling', description: 'What it is.', creators: [{ name: 'Weiss, Hanna', affiliation: '', orcid: '' }],
  keywords: [], licence: 'cc-by-4.0', access: 'open', embargo_date: '', include_comments: false,
}
const today = new Date(2026, 9, 26)  // 26 October 2026

test('keywords are split on commas and semicolons, each kept once', () => {
  assert.deepEqual(parseKeywords('suzuki, Cross-coupling;  Pd(PPh3)4 ,SUZUKI,,'), ['suzuki', 'Cross-coupling', 'Pd(PPh3)4'])
  assert.deepEqual(parseKeywords(''), [])
})

test('details ready to send have no problems', () => {
  assert.deepEqual(problems(ready, today), [])
})

test('a missing title, description or author is named', () => {
  assert.deepEqual(problems({ ...ready, title: ' ', description: '', creators: [{ name: ' ', affiliation: 'U of T', orcid: '' }] }, today),
    ['Give it a title.', 'Zenodo needs a description.', 'List at least one author.'])
})

test('an embargo needs a date after today', () => {
  assert.deepEqual(problems({ ...ready, access: 'embargoed' }, today), ['Choose the date the files become public.'])
  assert.deepEqual(problems({ ...ready, access: 'embargoed', embargo_date: '2026-10-26' }, today), ['The embargo has to end after today.'])
  assert.deepEqual(problems({ ...ready, access: 'embargoed', embargo_date: '2026-10-27' }, today), [])
  assert.equal(firstEmbargoDay(new Date(2026, 11, 31)), '2027-01-01')
})

test('a sandbox DOI links to the record, a real one to doi.org', () => {
  assert.equal(doiHref({ doi_url: '', url: 'https://sandbox.zenodo.org/records/7001' }), 'https://sandbox.zenodo.org/records/7001')
  assert.equal(doiHref({ doi_url: 'https://doi.org/10.5281/zenodo.7001', url: 'https://zenodo.org/records/7001' }), 'https://doi.org/10.5281/zenodo.7001')
})

test('file sizes read naturally', () => {
  assert.equal(fileSize(512), '512 bytes')
  assert.equal(fileSize(48_200), '48 kB')
  assert.equal(fileSize(3_400_000), '3.4 MB')
  assert.equal(fileSize(2_100_000_000), '2.1 GB')
})
