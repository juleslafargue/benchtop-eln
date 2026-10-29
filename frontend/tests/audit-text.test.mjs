// The admin's audit trail in plain sentences (src/auditText.ts). The stored event is shown untouched under each one.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let m

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-audit-'))
  const out = join(dir, 'audit.mjs')
  await build({ entryPoints: [new URL('../src/auditText.ts', import.meta.url).pathname], bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error' })
  m = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

const names = { people: { nnewbie: 'Nadia Newbie', dcho: 'Daniel Cho', hweiss: 'Hannah Weiss' }, userIds: { 7: 'Nadia Newbie', 4: 'Daniel Cho' }, groups: { 2: 'Ruiz group' } }
const ev = (action, object_type, details = {}, username = 'nnewbie', object_id = 34) => ({ action, object_type, object_id, username, details })

test('the common events read as sentences, with people by name', () => {
  const cases = [
    [ev('sign:author', 'experiment', { eid: 'EXP-00034', content_hash: 'ab' }), 'Nadia Newbie signed EXP-00034'],
    [ev('sign:witness', 'experiment', { eid: 'EXP-00034' }, 'dcho'), 'Daniel Cho witnessed EXP-00034'],
    [ev('sign:rejected', 'experiment', { eid: 'EXP-00034', comment: 'add the 13C' }, 'dcho'), 'Daniel Cho returned EXP-00034 to its author'],
    [ev('login', 'user', { ip: '127.0.0.1' }, 'dcho', 4), 'Daniel Cho signed in'],
    [ev('login_failed', 'user', { username: 'nnewbe' }, 'system', null), 'Someone failed to sign in as nnewbe'],
    [ev('create', 'experiment', { eid: 'EXP-00034', title: 'Amide coupling' }), 'Nadia Newbie started EXP-00034 “Amide coupling”'],
    [ev('update', 'entry', { experiment: 'EXP-00034', version: 3 }), 'Nadia Newbie edited a section of EXP-00034 (now version 3)'],
    [ev('upload', 'attachment', { experiment: 'EXP-00034', filename: 'H1.jdx' }), 'Nadia Newbie added the file H1.jdx to EXP-00034'],
    [ev('update', 'user', { changes: { active: false } }, 'hweiss', 7), 'Hannah Weiss disabled Nadia Newbie'],
    [ev('update', 'user', { changes: { role: 'admin' } }, 'hweiss', 4), 'Hannah Weiss made Daniel Cho an admin'],
    [ev('group_member', 'group', { member: 'nnewbie', role: 'member' }, 'hweiss', 2), 'Hannah Weiss added Nadia Newbie to Ruiz group'],
    [ev('witness_request', 'experiment', { eid: 'EXP-00034', witness_id: 4 }), 'Nadia Newbie asked Daniel Cho to witness EXP-00034'],
    [ev('inventory', 'lot', { barcode: 'MAT-00022-L0001', delta: -250, unit: 'uL' }), 'Nadia Newbie took 250 µL from MAT-00022-L0001'],
  ]
  for (const [e, want] of cases) assert.equal(m.auditSentence(e, names), want)
})

test('anything else is left as stored (the page shows its action and details)', () => {
  assert.equal(m.auditSentence(ev('backup_test', 'system', {}), names), null)
  assert.equal(m.auditSentence(ev('settings_email', 'system', {}), names), null)
})

test('the hash chain check and the raw events are still there', () => {
  const admin = readFileSync(new URL('../src/pages/Admin.tsx', import.meta.url), 'utf8')
  assert.match(admin, /api\.get\('\/api\/audit\/verify'\)/)
  assert.match(admin, /<details className="audit-raw">/)
  assert.match(admin, /JSON\.stringify\(e\.details\)/)
  assert.match(admin, /#\{e\.id\} · \{e\.hash\}/)
})
