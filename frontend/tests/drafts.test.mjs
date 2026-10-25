// 0.27: unsaved typing is kept in the browser until the server has it (src/drafts.ts).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { KEEP_DAYS, clearDraft, draftKey, draftSituation, draftText, loadDraft, saveDraft, sweepDrafts } from '../src/drafts.ts'

/** localStorage as a browser has it, with an optional size limit (a full storage throws, like the real one). */
function store(limit = Infinity) {
  const m = new Map()
  return {
    getItem: k => (m.has(k) ? m.get(k) : null),
    setItem(k, v) {
      const used = [...m].filter(([key]) => key !== k).reduce((n, [key, val]) => n + key.length + val.length, 0)
      if (used + k.length + v.length > limit) throw new Error('QuotaExceededError')
      m.set(k, String(v))
    },
    removeItem: k => { m.delete(k) },
    get length() { return m.size },
    key: i => [...m.keys()][i] ?? null,
  }
}

const draft = (over = {}) => ({ entryId: 7, experimentId: 3, baseVersion: 4, title: 'Notes', content: { html: '<p>Yield 87%</p>' },
  at: Date.now(), tab: 'a', ...over })

test('a kept copy comes back for the same person and section, and not for anyone else', () => {
  const s = store()
  assert.ok(saveDraft(s, 2, draft()))
  assert.deepEqual(loadDraft(s, 2, 7), draft({ at: loadDraft(s, 2, 7).at }))
  assert.equal(loadDraft(s, 5, 7), null, 'another person signed in on the same computer')
  assert.equal(loadDraft(s, 2, 8), null, 'another section')
  clearDraft(s, 2, 7)
  assert.equal(loadDraft(s, 2, 7), null)
})

test('a full browser storage makes room by dropping the oldest copies, never the one being kept', () => {
  const s = store(900)
  saveDraft(s, 2, draft({ entryId: 1, at: 1000, content: { html: 'x'.repeat(200) } }))
  saveDraft(s, 2, draft({ entryId: 2, at: 2000, content: { html: 'y'.repeat(200) } }))
  assert.ok(saveDraft(s, 2, draft({ entryId: 3, at: 3000, content: { html: 'z'.repeat(300) } })))
  assert.equal(loadDraft(s, 2, 1), null, 'the oldest made room')
  assert.ok(loadDraft(s, 2, 2) && loadDraft(s, 2, 3))
  assert.equal(saveDraft(s, 2, draft({ entryId: 4, content: { html: 'w'.repeat(5000) } })), false, 'too big on its own: not kept')
})

test('blocked or broken storage never throws', () => {
  const broken = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('blocked') }, removeItem() { throw new Error('blocked') }, length: 0, key: () => null }
  assert.equal(loadDraft(broken, 2, 7), null)
  assert.equal(saveDraft(broken, 2, draft()), false)
  clearDraft(broken, 2, 7)
  assert.equal(loadDraft(null, 2, 7), null)
  const s = store()
  s.setItem(draftKey(2, 7), '{not json')
  assert.equal(loadDraft(s, 2, 7), null)
})

test(`copies older than ${KEEP_DAYS} days are cleared, newer ones and other settings are left`, () => {
  const s = store()
  const now = Date.UTC(2030, 0, 31)
  saveDraft(s, 2, draft({ entryId: 1, at: now - (KEEP_DAYS + 1) * 86400e3 }))
  saveDraft(s, 2, draft({ entryId: 2, at: now - 2 * 86400e3 }))
  s.setItem('benchtop.token', 'abc')
  assert.equal(sweepDrafts(s, now), 1)
  assert.equal(loadDraft(s, 2, 1), null)
  assert.ok(loadDraft(s, 2, 2))
  assert.equal(s.getItem('benchtop.token'), 'abc')
})

test('what the offer says: the same version, changed since, or the record can no longer be edited', () => {
  assert.equal(draftSituation(draft({ baseVersion: 4 }), 4, false), 'same')
  assert.equal(draftSituation(draft({ baseVersion: 4 }), 6, false), 'changed')
  assert.equal(draftSituation(draft({ baseVersion: 4 }), 4, true), 'readonly')
})

test('kept changes copy as plain text', () => {
  assert.equal(draftText(draft({ content: { html: '<p>Yield 87%&nbsp;&amp; mp 135&ndash;136</p><p>White solid</p>' } })),
    'Notes\n\nYield 87% & mp 135&ndash;136\nWhite solid')
  assert.match(draftText(draft({ title: 'Work-up', content: { method: 'column', eluent: 'EtOAc/hexane' } })), /^Work-up\n\n\{\n {2}"method": "column"/)
})
