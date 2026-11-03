// 0.30.1: the smaller things the 0.30 stress test found, in the browser's own code.
// - A number box says why it can't take what was typed ("Not a number", "mL can't be turned into mg") instead of
//   quietly putting the old value back; "25 °C", "1 000" and "5." are read (src/components/bits.tsx).
// - An @mention with an accented letter keeps the suggestions open (src/mentions.ts).
// - Sample codes written with other dashes (JL–013) are read, and JL013 is worth asking the server about
//   (src/sampleCode.ts).
// - Downloads can take the server's file name, which is right for non-Latin names (src/api.ts).
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { before, test } from 'node:test'

let m

before(async () => {
  const { build } = await import('esbuild')
  const dir = mkdtempSync(join(tmpdir(), 'benchtop-polish-'))
  const out = join(dir, 'polish.mjs')
  const stub = { name: 'editor-stub', setup(b) {  // the structure editor (Ketcher) isn't needed here
    b.onResolve({ filter: /\/StructureEditor$/ }, () => ({ path: 'editor', namespace: 'stub' }))
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: 'export default function StructureEditor() { return null }', loader: 'js' }))
  } }
  await build({
    stdin: { contents: `export { parseAmount, numberProblem } from './components/bits'; export { mentionTyped } from './mentions'
      export { sampleCode, looksLikeCode } from './sampleCode'; export { servedName } from './api'`,
      resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'ts' },
    bundle: true, platform: 'node', format: 'esm', outfile: out, logLevel: 'error', jsx: 'automatic', plugins: [stub],
    define: { 'process.env.NODE_ENV': '"production"' },
    banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
  })
  m = await import(pathToFileURL(out).href)
  rmSync(dir, { recursive: true, force: true })
})

test('what a number box can read', () => {
  assert.equal(m.parseAmount('25 °C', 'C'), 25)
  assert.equal(m.parseAmount('-78°C', 'C'), -78)
  assert.equal(m.parseAmount('25 ° C', 'C'), 25)
  assert.equal(m.parseAmount('1 000', 'mg'), 1000)  // spaces between groups of three are thousands
  assert.equal(m.parseAmount('12 500,5'), 12500.5)
  assert.equal(m.parseAmount('1 000 g', 'mg'), 1e6)
  assert.equal(m.parseAmount('5.'), 5)
  assert.equal(m.parseAmount('.5'), 0.5)
  for (const ok of ['25 °C', '1 000', '5.', '', '1.2 g', '0,5']) assert.equal(m.numberProblem(ok, ok.includes('°') ? 'C' : 'mg'), null, ok)
})

test("what it can't read is said, not thrown away", () => {
  assert.equal(m.numberProblem('abc'), 'Not a number.')
  assert.equal(m.numberProblem('1 2'), 'Not a number.')  // not groups of three: no guess
  assert.equal(m.numberProblem('1e999'), 'Not a number.')
  assert.equal(m.numberProblem('12 mL', 'mg'), "mL can't be turned into mg. Type e.g. 1.2 g.")
  assert.equal(m.numberProblem('2 weeks', 'h'), "weeks can't be turned into hours. Type e.g. 30 min.")
  assert.equal(m.numberProblem('warm', 'C'), 'Not a number. Type it in °C, e.g. -78.')
  assert.ok(m.numberProblem('1,000', 'mg').includes('1000 or 1'))  // 0.30.1's first fix still holds
  assert.equal(m.parseAmount('12 mL', 'mg'), undefined)
})

test('an @mention with accents keeps the suggestions open', () => {
  assert.equal(m.mentionTyped('Thanks @Marí'), 'Marí')  // 0.30: null, so the list closed
  assert.equal(m.mentionTyped('@Zoë'), 'Zoë')
  assert.equal(m.mentionTyped('cc @j.ann'), 'j.ann')
  assert.equal(m.mentionTyped('@'), '')
  assert.equal(m.mentionTyped('mail me at jo@example'), null)  // an email address isn't a mention
  assert.equal(m.mentionTyped('@hweiss done'), null)
})

test('sample codes written with other dashes, or none', () => {
  for (const typed of ['JL–013', 'jl—13', 'JL‐013', 'JL−013', 'jl-13', 'JL_013', 'JL 013']) assert.equal(m.sampleCode(typed), 'JL-013', typed)
  for (const typed of ['JL013', 'jl13', 'JL–013', 'JLA 7']) assert.ok(m.looksLikeCode(typed), typed)
  for (const typed of ['suzuki', 'EXP', '13', 'J-1', 'JL-0130000']) assert.ok(!m.looksLikeCode(typed), typed)
})

test("the server's file name, as Content-Disposition gives it", () => {
  assert.equal(m.servedName("attachment; filename*=utf-8''EXP-00012%20-%20%D0%A1%D1%83%D0%B7%D1%83%D0%BA%D0%B8.eln"), 'EXP-00012 - Сузуки.eln')
  assert.equal(m.servedName('attachment; filename="EXP-00038.eln"'), 'EXP-00038.eln')
  assert.equal(m.servedName(null), null)
  assert.equal(m.servedName("attachment; filename*=utf-8''%E0%A4%A"), null)
})
