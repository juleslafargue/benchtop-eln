// @mention suggestions (src/mentions.ts, used by the comment box in src/components/Discussion.tsx): the person whose
// username or name starts with what was typed comes first, because Enter picks the first one.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { mentionRank, rankMentions } from '../src/mentions.ts'

// in username order, as /api/users lists them
const lab = [
  { username: 'dcho', full_name: 'Daniel Cho' },
  { username: 'hweiss', full_name: 'Hannah Weiss' },
  { username: 'j.annand', full_name: 'Jo Annand' },
  { username: 'kwatanabe', full_name: 'Kenji Watanabe' },
  { username: 'mruiz', full_name: 'María Ruiz' },
  { username: 'nnewbie', full_name: 'Nadia Newbie' },
  { username: 'pnair', full_name: 'Priya Nair' },
  { username: 'slaurent', full_name: 'Sophie Laurent' },
]
const first = q => rankMentions(lab, q)[0]?.username

test('the username, first or last name that starts with the letters wins over names that merely contain them', () => {
  assert.equal(first('nn'), 'nnewbie')  // not Hannah (Ha-nn-ah) or Jo Annand
  assert.equal(first('nad'), 'nnewbie')
  assert.equal(first('new'), 'nnewbie')
  assert.equal(first('nai'), 'pnair')  // last name, ahead of nothing else
  assert.equal(first('cho'), 'dcho')
  assert.equal(first('w'), 'hweiss')  // Weiss and Watanabe both start with w: list order between equals
  assert.deepEqual(rankMentions(lab, 'w').slice(0, 2).map(u => u.username), ['hweiss', 'kwatanabe'])
})

test('then a word inside the name or username, then anywhere inside', () => {
  assert.deepEqual(rankMentions(lab, 'ann').map(u => u.username), ['j.annand', 'hweiss'])  // j.annand: a word of the username
  assert.equal(mentionRank({ username: 'hweiss', full_name: 'Hannah Weiss' }, 'nna'), 2)
  assert.equal(mentionRank({ username: 'hweiss', full_name: 'Hannah Weiss' }, 'zz'), null)
})

test('an exact username comes first; accents and capitals do not matter; at most six', () => {
  const two = [{ username: 'annand', full_name: 'Ann Andrews' }, { username: 'ann', full_name: 'Ann Lee' }]
  assert.equal(rankMentions(two, 'ann')[0].username, 'ann')
  assert.equal(first('MAR'), 'mruiz')
  assert.equal(first('maria'), 'mruiz')
  assert.equal(rankMentions(lab, '').length, 6)
})

test('the comment box uses this ranking', () => {
  const src = readFileSync(new URL('../src/components/Discussion.tsx', import.meta.url), 'utf8')
  assert.match(src, /rankMentions\(users, menu\.q\)/)
})
