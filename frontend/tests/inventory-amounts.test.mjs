// 0.36.5: a lot's usage list shows a small withdrawal from a big lot in a unit it can be read in (0.4 mg taken from a
// 1 kg drum read as "0 kg"; the server also recorded it as 0 until 0.36.5, see backend tests/test_v36_5_numbers.py).
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let d
before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-display-'))
  await build({ stdin: { contents: "export * from './display'", resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile: join(dir, 'd.mjs'), logLevel: 'error' })
  d = await import(pathToFileURL(join(dir, 'd.mjs')).href)
  rmSync(dir, { recursive: true, force: true })
})

test('a small change to a big lot is shown in a smaller unit, not as 0', () => {
  assert.equal(d.amountText(-4e-7, 'kg'), '-0.4 mg')
  assert.equal(d.amountText(-6e-4, 'kg'), '-0.6 g')
  assert.equal(d.amountText(-2.5e-5, 'L'), '-25 µL')
  assert.equal(d.amountText(-0.25, 'g'), '-0.25 g')     // the usual case: as before
  assert.equal(d.amountText(5, 'mL'), '5 mL')
  assert.equal(d.amountText(0, 'kg'), '0 kg')
})
