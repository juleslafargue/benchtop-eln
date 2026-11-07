// 0.33: the Supporting Information builder. The .docx is built here from fixture data with the code the app uses
// (siModel, then buildSiDocx); only the pictures, which the browser draws, are fixtures too.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { unzipSync, strFromU8 } from 'fflate'
import { captionRuns, checkLines, htmlRuns, paragraphs, siFileName, siModel, textRuns } from '../src/si.ts'
import { buildSiDocx } from '../src/siDocx.ts'

const show = rs => rs.map(r => (r.sub ? `_{${r.text}}` : r.sup ? `^{${r.text}}` : r.italics ? `*${r.text}*` : r.bold ? `**${r.text}**` : r.text)).join('')

// a real PNG (1 × 1, white), and its size as the builder is told it
const PNG = Uint8Array.from(Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=', 'base64'))
// each picture different (bytes after the PNG's end): Word files keep one copy of identical pictures
let made = 0
const img = (w, h) => ({ data: Uint8Array.from([...PNG, ++made]), width: w, height: h })

const compound = (over = {}) => ({
  index: 0, experiment_id: 4, number: '3a', name: '4-Methoxybiphenyl', visible: true, eid: 'EXP-00004', status: 'signed',
  product: { name: '4-Methoxybiphenyl', smiles: 'COc1ccc(-c2ccccc2)cc1', formula: 'C13H12O', mw: 184.24 },
  experimental_html: '4-Bromoanisole (187 mg, 1.00 mmol, 1.0 equiv), phenylboronic acid (146 mg, 1.20 mmol, 1.2 equiv), '
    + 'tetrakis(triphenylphosphine)palladium(0) (34.7 mg, 0.0300 mmol, 3 mol%) and K2CO3 in 1,4-dioxane were stirred at 90 °C under N₂ for 16 h. '
    + 'Purification by flash column chromatography (silica gel, 0→10% EtOAc/hexanes) afforded 4-methoxybiphenyl as a white solid (155 mg, 84%).',
  characterisation_html: 'R<sub>f</sub> = 0.45 (10% EtOAc/hexanes); ¹H NMR (400 MHz, CDCl3) δ 7.54 (d, <i>J</i> = 8.8 Hz, 2H), 3.86 (s, 3H); '
    + '¹³C NMR (101 MHz, CDCl3) δ 159.2, 140.9; HRMS (ESI-TOF) m/z calcd for C13H13O [M + H]+ 185.0961; found 185.0965; mp 86–88 °C.',
  spectra: [
    { attachment_id: 11, filename: '4-Methoxybiphenyl_1H.jdx', technique: '1H NMR', conditions: '400 MHz, CDCl3', analysis: null, included: true },
    { attachment_id: 12, filename: '4-Methoxybiphenyl_13C.jdx', technique: '13C NMR', conditions: '101 MHz, CDCl3', analysis: null, included: true },
    { attachment_id: 13, filename: 'notes.pdf', technique: 'IR', conditions: '', analysis: null, included: false },
  ],
  problems: [],
  ...over,
})

const doc = (pageSize = 'letter') => ({
  title: 'Suzuki couplings of aryl bromides', page_size: pageSize,
  general: 'All reactions were run under N2 in oven-dried glassware.\n\nNMR spectra were recorded at 298 K.',
  compounds: [
    compound(),
    compound({ index: 1, experiment_id: 7, number: '2a', name: '3-Phenylpyridine', eid: 'EXP-00007',
      spectra: [{ attachment_id: 21, filename: 'odd.dx', technique: '1H NMR', conditions: '', analysis: null, included: true }] }),
    { index: 2, experiment_id: 9, number: '9', name: 'EXP-00009', visible: false, eid: 'EXP-00009', problems: ['EXP-00009 is no longer one you can open, so it\'s left out'] },
  ],
})
const pictures = () => ({
  structures: new Map([[0, img(300, 180)], [1, null]]),
  spectra: new Map([[11, { image: img(2000, 840), header: '¹H NMR (400 MHz, CDCl3)' }], [12, { image: img(2000, 840) }],
    [21, { error: "this file isn't a spectrum Benchtop can read" }]]),
})

async function build(pageSize) {
  const bytes = await buildSiDocx(siModel(doc(pageSize), pictures()))
  const files = unzipSync(bytes)
  return { files, xml: strFromU8(files['word/document.xml']), bytes }
}

test('the Word file has the sections, the compounds, their data and the pictures', async () => {
  const { files, xml, bytes } = await build('letter')
  assert.equal(strFromU8(bytes.slice(0, 2)), 'PK')
  const text = xml.replace(/<[^>]+>/g, '').replace(/&apos;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
  for (const h of ['Supporting Information', 'Suzuki couplings of aryl bromides', 'Table of contents', 'General information',
    'Experimental procedures and characterisation data', 'NMR spectra']) assert.ok(text.includes(h), h)
  // headings are Word's own heading styles, so the Navigation pane lists them
  assert.match(xml, /<w:pStyle w:val="Heading1"\/>/)
  assert.match(xml, /<w:pStyle w:val="Heading2"\/>/)
  assert.match(strFromU8(files['word/styles.xml']), /w:styleId="Heading1"[\s\S]*?<w:outlineLvl w:val="0"\/>/)
  // compound numbers in bold, in the headings and the captions
  const boldRuns = [...xml.matchAll(/<w:r>(?:(?!<\/w:r>).)*?<w:b\/>(?:(?!<\/w:r>).)*?<w:t[^>]*>([^<]*)<\/w:t>/gs)].map(m => m[1])
  assert.ok(boldRuns.filter(t => t === '3a').length >= 3, boldRuns.join('|'))  // heading, spectra heading, captions
  assert.ok(boldRuns.includes('2a'))
  assert.ok(text.includes('4-Methoxybiphenyl (3a)'))
  // the person's general text, paragraph by paragraph
  assert.ok(text.includes('All reactions were run under N2 in oven-dried glassware.') && text.includes('NMR spectra were recorded at 298 K.'))
  // the experimental and characterisation text, with sub- and superscripts and italic J as Word formatting, not tags
  assert.ok(text.includes('were stirred at 90 °C under N'))
  assert.ok(!/&lt;(sub|i)&gt;/.test(xml) && !text.includes('<sub>'))
  const runWith = (prop, t) => new RegExp(`<w:r><w:rPr>(?:(?!</w:rPr>).)*<w:vertAlign w:val="${prop}"/>(?:(?!</w:rPr>).)*</w:rPr><w:t[^>]*>${t}</w:t>`, 's')
  assert.match(xml, runWith('subscript', 'f'))   // Rf
  assert.match(xml, runWith('subscript', '3'))   // CDCl3
  assert.match(xml, runWith('superscript', '13')) // 13C
  assert.match(xml, runWith('superscript', '1'))  // 1H
  assert.match(xml, /<w:i\/>(?:(?!<\/w:r>).)*<w:t[^>]*>J<\/w:t>/s)
  // a structure picture and the two spectra in word/media
  const media = Object.keys(files).filter(f => /^word\/media\/.+\.png$/.test(f))
  assert.equal(media.length, 3, media.join(', '))
  assert.equal((xml.match(/<w:drawing>/g) || []).length, 3)
  // the file that couldn't be drawn is named, not dropped silently
  assert.ok(text.includes("(spectrum file odd.dx not shown: this file isn't a spectrum Benchtop can read)"))
  // the one no longer visible is left out
  assert.ok(!text.includes('EXP-00009'))
  // captions, numbered in order
  assert.ok(text.includes('Figure S1. 1H NMR (400 MHz, CDCl3) of 3a.') && text.includes('Figure S2. 13C NMR (101 MHz, CDCl3) of 3a.'))
  // US Letter, 1 inch margins; page numbers in the footer; nothing Word asks to update on opening
  assert.match(xml, /<w:pgSz w:w="12240" w:h="15840"/)
  const footer = Object.keys(files).find(f => /^word\/footer\d*\.xml$/.test(f))
  assert.ok(footer && /PAGE/.test(strFromU8(files[footer])))
  assert.ok(!/TOC \\|updateFields/.test(xml + strFromU8(files['word/settings.xml'] || new Uint8Array())))
})

test('A4 when chosen', async () => {
  const { xml } = await build('a4')
  assert.match(xml, /<w:pgSz w:w="11906" w:h="16838"/)
})

test('formula counts, charges and units become sub- and superscripts; other words stay as written', () => {
  assert.equal(show(textRuns('m/z [M+H]+ calcd for C13H13O; Pd(PPh3)4, K2CO3, (CDCl3), DMSO-d6, cm-1')),
    '*m/z* [M+H]^{+} calcd for C_{13}H_{13}O; Pd(PPh_{3})_{4}, K_{2}CO_{3}, (CDCl_{3}), DMSO-d_{6}, cm^{−1}')
  assert.equal(show(textRuns('C18 silica, ESI-TOF, B3LYP, N3, 2-Bromotoluene, 1,4-dioxane')), 'C18 silica, ESI-TOF, B3LYP, N3, 2-Bromotoluene, 1,4-dioxane')
  assert.equal(show(textRuns('¹H and ¹³C under N₂')), '^{1}H and ^{13}C under N_{2}')
  assert.equal(show(htmlRuns('R<sub>f</sub> = 0.3; (d, <i>J</i> = 8.8 Hz) &amp; 4&#x27;-Br')), 'R_{f} = 0.3; (d, *J* = 8.8 Hz) & 4\'-Br')
})

test('captions name the technique, conditions and the compound number', () => {
  assert.equal(show(captionRuns(2, '13C NMR', '101 MHz, CDCl3', '3a')), '**Figure S2. **^{13}C NMR (101 MHz, CDCl_{3}) of **3a**.')
  // no conditions in the record: the spectrum's own header
  assert.equal(show(captionRuns(1, '1H NMR', '', 'S3', '¹H NMR (500 MHz, DMSO-d6)')), '**Figure S1. **^{1}H NMR (500 MHz, DMSO-d_{6}) of **S3**.')
})

test('the check list says what is missing, per compound', () => {
  assert.deepEqual(checkLines([
    { number: '1', name: 'A', eid: 'EXP-00001', problems: [] },
    { number: '2a', name: 'B', eid: 'EXP-00002', problems: ['no ¹³C NMR data', 'no HRMS (or elemental analysis)'] },
    { number: '', name: 'C', eid: 'EXP-00003', problems: ['not signed yet'] },
  ]), ['2a: no ¹³C NMR data; no HRMS (or elemental analysis)', 'C: not signed yet'])
})

test('file names come from the title, with safe characters', () => {
  assert.equal(siFileName('Suzuki couplings'), 'SI – Suzuki couplings.docx')
  assert.equal(siFileName('Scope: aryl/heteroaryl bromides?'), 'SI – Scope aryl heteroaryl bromides.docx')
  assert.equal(siFileName(' .. '), 'SI – Supporting information.docx')
  assert.ok(siFileName('x'.repeat(500)).length < 140)
})

test('general text: a blank line starts a new paragraph', () => {
  assert.deepEqual(paragraphs('One.\nStill one.\n\n\nTwo.\r\n\r\nThree.'), ['One.\nStill one.', 'Two.', 'Three.'])
})
