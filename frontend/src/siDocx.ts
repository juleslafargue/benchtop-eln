/** The Supporting Information as a Word file (0.33), from siModel's plain data. Built with the `docx` package, which
 *  is loaded only when someone builds a document (the page imports this file with import()), so the app itself stays
 *  the same size. Nothing here uses Word fields Word asks to update on opening: the table of contents is a plain list
 *  with links, and only the page numbers in the footer are fields (Word fills those in by itself). */
import {
  AlignmentType, Bookmark, Document, Footer, HeadingLevel, ImageRun, InternalHyperlink, Packer, PageNumber, Paragraph,
  TextRun, type ParagraphChild,
} from 'docx'
import type { Run, SiImage, SiModel } from './si'

const FONT = 'Times New Roman'
const PAGE = {  // twentieths of a point; 1 inch margins
  letter: { width: 12240, height: 15840 },
  a4: { width: 11906, height: 16838 },
}
const MARGIN = 1440
const PX_PER_TWIP = 96 / 1440  // docx sizes pictures in pixels at 96 per inch

function runs(rs: Run[], extra: { bold?: boolean } = {}): TextRun[] {
  return rs.map(r => new TextRun({
    text: r.text, bold: r.bold || extra.bold || undefined, italics: r.italics || undefined,
    subScript: r.sub || undefined, superScript: r.sup || undefined,
  }))
}

/** "4-Methoxybiphenyl (3a)" with the number in bold. */
function nameAndNumber(name: string, number: string, bold = false): TextRun[] {
  return number ? [new TextRun({ text: `${name} (`, bold: bold || undefined }), new TextRun({ text: number, bold: true }), new TextRun({ text: ')', bold: bold || undefined })]
    : [new TextRun({ text: name, bold: bold || undefined })]
}

function picture(img: SiImage, widthPx: number): ImageRun {
  const w = Math.round(widthPx), h = Math.round(widthPx * img.height / img.width)
  return new ImageRun({ type: 'png', data: img.data, transformation: { width: w, height: h } })
}

function heading(level: 1 | 2, children: ParagraphChild[], anchor?: string, pageBreakBefore = false): Paragraph {
  return new Paragraph({
    heading: level === 1 ? HeadingLevel.HEADING_1 : HeadingLevel.HEADING_2, pageBreakBefore,
    keepNext: true, children: anchor ? [new Bookmark({ id: anchor, children })] : children,
  })
}

const tocLine = (anchor: string, children: TextRun[], indent = 0) => new Paragraph({
  indent: indent ? { left: indent } : undefined, spacing: { after: 60 },
  children: [new InternalHyperlink({ anchor, children })],
})

export async function buildSiDocx(m: SiModel): Promise<Uint8Array> {
  const page = PAGE[m.pageSize] || PAGE.letter
  const textWidth = (page.width - 2 * MARGIN) * PX_PER_TWIP  // 6.5 in on Letter, 6.27 in on A4
  const body: Paragraph[] = []
  const hasGeneral = m.general.length > 0
  const withFigures = m.parts.filter(p => p.figures.length)

  // title
  body.push(new Paragraph({ heading: HeadingLevel.TITLE, alignment: AlignmentType.CENTER, children: [new TextRun('Supporting Information')] }))
  if (m.title) body.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 360 }, children: [new TextRun({ text: m.title, size: 28 })] }))

  // table of contents: a list with links (no TOC field, so Word never asks to update it)
  body.push(heading(1, [new TextRun('Table of contents')]))
  if (hasGeneral) body.push(tocLine('general', [new TextRun({ text: 'General information', style: 'Hyperlink' })]))
  body.push(tocLine('experimental', [new TextRun({ text: 'Experimental procedures and characterisation data', style: 'Hyperlink' })]))
  for (const p of m.parts) body.push(tocLine(p.anchor, nameAndNumber(p.name, p.number).map(r => r), 360))
  if (withFigures.length) body.push(tocLine('spectra', [new TextRun({ text: m.spectraHeading, style: 'Hyperlink' })]))

  // general information: the person's own text
  if (hasGeneral) {
    body.push(heading(1, [new TextRun('General information')], 'general', true))
    for (const para of m.general) {
      const lines = para.split('\n')
      body.push(new Paragraph({ alignment: AlignmentType.LEFT, children: lines.map((l, i) => new TextRun({ text: l, break: i ? 1 : undefined })) }))
    }
  }

  // each compound: heading, structure, procedure, data
  body.push(heading(1, [new TextRun('Experimental procedures and characterisation data')], 'experimental', true))
  for (const p of m.parts) {
    body.push(heading(2, nameAndNumber(p.name, p.number, true), p.anchor))
    if (p.structure) body.push(new Paragraph({ alignment: AlignmentType.CENTER, keepNext: true, children: [picture(p.structure, Math.min(textWidth * 0.36, 220))] }))
    if (p.experimental.length) body.push(new Paragraph({ alignment: AlignmentType.LEFT, children: runs(p.experimental) }))
    if (p.characterisation.length) body.push(new Paragraph({ alignment: AlignmentType.LEFT, children: runs(p.characterisation) }))
  }

  // spectra, drawn as the spectrum viewer draws them
  if (withFigures.length) {
    body.push(heading(1, [new TextRun(m.spectraHeading)], 'spectra', true))
    for (const p of withFigures) {
      body.push(heading(2, nameAndNumber(p.name, p.number, true), `${p.anchor}-spectra`))
      for (const f of p.figures) {
        if (!f.image) { body.push(new Paragraph({ children: [new TextRun({ text: f.note || `(spectrum file ${f.label} not shown)`, italics: true })] })); continue }
        body.push(new Paragraph({ alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 120, after: 60 }, children: [picture(f.image, textWidth)] }))
        body.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 240 }, children: runs(f.caption) }))
      }
    }
  }

  const doc = new Document({
    creator: 'Benchtop', title: m.title ? `Supporting Information: ${m.title}` : 'Supporting Information',
    styles: {
      default: {
        document: { run: { font: FONT, size: 22 }, paragraph: { spacing: { after: 120, line: 264 } } },
        title: { run: { font: FONT, size: 36, bold: true, color: '000000' }, paragraph: { spacing: { after: 120 }, alignment: AlignmentType.CENTER } },
        heading1: { run: { font: FONT, size: 28, bold: true, color: '000000' }, paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 0 } },
        heading2: { run: { font: FONT, size: 22, bold: true, color: '000000' }, paragraph: { spacing: { before: 240, after: 80 }, outlineLevel: 1 } },
        hyperlink: { run: { color: '1F4E79' } },
      },
    },
    sections: [{
      properties: { page: { size: page, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
      footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ children: ['S', PageNumber.CURRENT] })] })] }) },
      children: body,
    }],
  })
  return new Uint8Array(await Packer.toArrayBuffer(doc))
}
