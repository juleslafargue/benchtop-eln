// 0.36.6: a USB barcode scanner types the code fast, then Enter or Tab. With no box focused, its keys went to the
// page's one-key shortcuts (P, T, L, N, ?); in the Scan box, a Tab after the code moved on instead of looking it up.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { ScanBurst, endsScan, HOLD_MS } from '../src/scanBurst.ts'

/** Feed keys `gap` ms apart; returns every outcome that isn't "wait", then what a flush gives. */
function feed(text, gap, end = 'Enter', start = 1000) {
  const b = new ScanBurst()
  const out = []
  let t = start
  for (const ch of text) { const o = b.key({ key: ch, time: t }); if (o.kind !== 'wait') out.push(o); t += gap }
  if (end) { const o = b.key({ key: end, time: t }); if (o.kind !== 'wait') out.push(o) }
  const f = b.flush()
  if (f.kind !== 'wait') out.push(f)
  return out
}

test('a scanner burst ending in Enter or Tab is one scan, and none of its keys reach the shortcuts', () => {
  for (const end of ['Enter', 'Tab']) {
    assert.deepEqual(feed('PTL-0042', 8, end), [{ kind: 'scan', code: 'PTL-0042' }])
    assert.deepEqual(feed('0123456789012', 2, end), [{ kind: 'scan', code: '0123456789012' }])
  }
  // a scanner that sends nothing after the code: taken as a scan once the burst stops
  assert.deepEqual(feed('VIM-000731', 5, null), [{ kind: 'scan', code: 'VIM-000731' }])
})

test('a person pressing a shortcut key still gets it (held a moment, then given back)', () => {
  assert.deepEqual(feed('p', 0, null), [{ kind: 'keys', keys: [{ key: 'p', time: 1000 }] }])
  // keys typed at a person's pace each come back, in order
  const keys = feed('ltp', 150, null).flatMap(o => o.keys.map(k => k.key))
  assert.deepEqual(keys, ['l', 't', 'p'])
  // a short fast pair is not a code
  assert.deepEqual(feed('ab', 10, 'Enter').flatMap(o => o.kind === 'keys' ? o.keys.map(k => k.key) : ['SCAN']), ['a', 'b', 'Enter'])
  // Escape and arrows are never held
  const b = new ScanBurst()
  assert.deepEqual(b.key({ key: 'Escape', time: 5 }), { kind: 'keys', keys: [{ key: 'Escape', time: 5 }] })
})

test('a slow Enter after fast keys is a person pressing Enter, not the end of a scan', () => {
  const out = feed('ABCD', 5, null)
  assert.equal(out[0].kind, 'keys')  // 4 keys without Enter: shorter than a scan without Enter
  const b = new ScanBurst()
  for (let i = 0; i < 6; i++) b.key({ key: 'X', time: i * 5 })
  assert.equal(b.key({ key: 'Enter', time: 25 + HOLD_MS + 50 }).kind, 'keys')
})

test('in the Scan box, Tab right after the code ends the scan; a Tab pressed later moves on as usual', () => {
  assert.equal(endsScan('Tab', 6, 8), true)
  assert.equal(endsScan('Enter', 3, 13), true)
  assert.equal(endsScan('Tab', 900, 8), false)
  assert.equal(endsScan('Tab', 5, 2), false)
  assert.equal(endsScan('a', 5, 8), false)
})
