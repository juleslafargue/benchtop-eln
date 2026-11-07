/** Supporting Information (0.33): what goes in the Word file, as plain data. No imports, so `npm test` checks it
 *  directly; siDocx.ts turns the result into the .docx, and pages/SupportingInfo.tsx draws the pictures. */

/** A piece of text with its formatting, as a Word run carries it. */
export interface Run { text: string; bold?: boolean; italics?: boolean; sub?: boolean; sup?: boolean }

export interface SiSpectrum {
  attachment_id: number; filename: string; size?: number; technique: string; conditions: string
  analysis: unknown; included: boolean
}
/** One compound as GET /api/si-documents/{id} gives it. */
export interface SiCompound {
  index: number; experiment_id: number; number: string; name: string; name_set?: string; visible: boolean; eid: string
  title?: string; status?: string; name_default?: string
  product?: { name?: string | null; smiles?: string | null; formula?: string | null; mw?: number | null } | null
  heading?: string; experimental_html?: string; characterisation_html?: string; missing?: string[]
  spectra?: SiSpectrum[]; problems: string[]
}
export interface SiItem { experiment_id: number; number: string; name: string; spectra: number[] | null }
export interface SiDocument {
  id: number; title: string; general: string; page_size: 'letter' | 'a4'; version: number; items: SiItem[]
  compounds: SiCompound[]; updated_at: string
}

/** A picture for the document: PNG bytes and its size in pixels. */
export interface SiImage { data: Uint8Array; width: number; height: number }
export interface SiFigure { image: SiImage | null; caption: Run[]; label: string; note?: string }
export interface SiPart {
  number: string; name: string; anchor: string; structure: SiImage | null
  experimental: Run[]; characterisation: Run[]; figures: SiFigure[]
}
export interface SiModel { title: string; general: string[]; pageSize: 'letter' | 'a4'; parts: SiPart[]; spectraHeading: string }

// ------------------------------------------------------------------ text with sub- and superscripts

const SUPER_DIGITS = '⁰¹²³⁴⁵⁶⁷⁸⁹', SUB_DIGITS = '₀₁₂₃₄₅₆₇₈₉'
const ELEMENTS = new Set(('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr '
  + 'Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir '
  + 'Pt Au Hg Tl Pb Bi D').split(' '))
// groups chemists write inside formulae (PPh3, Et3N, Bu4NBr, CO2Me)
const GROUPS = new Set('Me Et Ph Bu Ac Bn Bz Ts Ms Tf Cy Pr Py'.split(' '))

/** Is this word a formula whose digits are subscripts (CDCl3, C13H13O, K2CO3, Pd(PPh3)4, CH2Cl2)? Two or more element
 *  symbols or groups and at least one count; a single symbol with a number (C18, N3, H2) is left as written. */
function formulaParts(word: string): { text: string; sub: boolean }[] | null {
  const out: { text: string; sub: boolean }[] = []
  let i = 0, symbols = 0, counts = 0, depth = 0
  while (i < word.length) {
    const ch = word[i]
    if (ch === '(' || ch === '[') { depth++; out.push({ text: ch, sub: false }); i++; continue }
    if (ch === ')' || ch === ']') {
      if (!depth--) return null
      out.push({ text: ch, sub: false }); i++
    } else {
      const two = word.slice(i, i + 2), one = word[i]
      const sym = /^[A-Z][a-z]$/.test(two) && (ELEMENTS.has(two) || GROUPS.has(two)) ? two : /^[A-Z]$/.test(one) && ELEMENTS.has(one) ? one : null
      if (!sym) return null
      out.push({ text: sym, sub: false }); i += sym.length; symbols++
    }
    const m = /^\d+/.exec(word.slice(i))
    if (m) { out.push({ text: m[0], sub: true }); i += m[0].length; counts++ }
  }
  return symbols >= 2 && counts >= 1 && depth === 0 ? out : null
}

/** Plain text as runs: formula counts (CDCl3), deuterated solvents (DMSO-d6), charges ([M+H]+) and cm-1 set properly,
 *  Unicode super- and subscript digits (¹H, N₂) turned into ordinary digits raised or lowered (every font has those),
 *  and m/z in italics. */
export function textRuns(text: string, base: Omit<Run, 'text'> = {}): Run[] {
  const out: Run[] = []
  const push = (t: string, f: Omit<Run, 'text'> = {}) => {
    if (!t) return
    const r = { ...base, ...f }
    const last = out[out.length - 1]
    if (last && !!last.sub === !!r.sub && !!last.sup === !!r.sup && !!last.italics === !!r.italics && !!last.bold === !!r.bold) last.text += t
    else out.push({ text: t, ...r })
  }
  const re = /(\[[^\]\n]{1,20}\])([+−-])(?![\w])|\b(cm)-1\b|-(d)(\d{1,2})\b|\b(m\/z)\b|([⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)|([₀₁₂₃₄₅₆₇₈₉]+)|(?<![\w-])([A-Z][A-Za-z0-9()[\]]*\d[A-Za-z0-9()[\]]*)(?![\w-])/g
  let at = 0
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m[1]) { push(text.slice(at, m.index)); push(m[1]); push(m[2] === '-' ? '−' : m[2], { sup: true }) }
    else if (m[3]) { push(text.slice(at, m.index)); push('cm'); push('−1', { sup: true }) }
    else if (m[4]) { push(text.slice(at, m.index)); push('-d'); push(m[5], { sub: true }) }
    else if (m[6]) { push(text.slice(at, m.index)); push('m/z', { italics: true }) }
    else if (m[7]) { push(text.slice(at, m.index)); push([...m[7]].map(c => (c === '⁻' ? '−' : c === '⁺' ? '+' : String(SUPER_DIGITS.indexOf(c)))).join(''), { sup: true }) }
    else if (m[8]) { push(text.slice(at, m.index)); push([...m[8]].map(c => String(SUB_DIGITS.indexOf(c))).join(''), { sub: true }) }
    else {
      // a bracket closing an aside ("(400 MHz, CDCl3)") isn't part of the formula
      let word = m[9]
      const open = () => (word.match(/[([]/g) || []).length, shut = () => (word.match(/[)\]]/g) || []).length
      while (/[)\]]$/.test(word) && shut() > open()) word = word.slice(0, -1)
      re.lastIndex = m.index + word.length
      const parts = formulaParts(word)
      if (!parts) continue  // not a formula: left in the text as it is
      push(text.slice(at, m.index))
      for (const p of parts) push(p.text, p.sub ? { sub: true } : {})
      at = m.index + word.length
      continue
    }
    at = m.index + m[0].length
  }
  push(text.slice(at))
  return out
}

const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }
const decode = (s: string) => s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (all, e: string) =>
  e[0] === '#' ? String.fromCodePoint(e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10)) : ENTITIES[e.toLowerCase()] ?? all)

/** The write-up's HTML (only <sub>, <sup>, <i>, <b> and their kin, as writeup.draft makes it) as runs. */
export function htmlRuns(html: string): Run[] {
  const out: Run[] = []
  const state = { bold: 0, italics: 0, sub: 0, sup: 0 }
  const re = /<(\/?)(sub|sup|i|em|b|strong)\s*>|<[^>]*>/gi
  let at = 0
  const text = (t: string) => {
    if (!t) return
    for (const r of textRuns(decode(t), { bold: !!state.bold || undefined, italics: !!state.italics || undefined, sub: !!state.sub || undefined, sup: !!state.sup || undefined })) {
      const clean = Object.fromEntries(Object.entries(r).filter(([, v]) => v !== undefined)) as unknown as Run
      out.push(clean)
    }
  }
  for (let m = re.exec(html); m; m = re.exec(html)) {
    text(html.slice(at, m.index))
    at = m.index + m[0].length
    const tag = (m[2] || '').toLowerCase()
    if (!tag) continue
    const key = tag === 'em' || tag === 'i' ? 'italics' : tag === 'strong' || tag === 'b' ? 'bold' : (tag as 'sub' | 'sup')
    state[key] = Math.max(0, state[key] + (m[1] ? -1 : 1))
  }
  text(html.slice(at))
  return out
}

export const plain = (runs: Run[]) => runs.map(r => r.text).join('')

// ------------------------------------------------------------------ names, captions, the check list

/** "SI – Suzuki couplings.docx": characters no file system minds, and never empty. */
export function siFileName(title: string): string {
  const t = title.normalize('NFC').replace(/[\\/:*?"<>|\u0000-\u001f\u007f]+/g, ' ').replace(/\s+/g, ' ').replace(/^[\s.]+|[\s.]+$/g, '').slice(0, 120).trim()
  return `SI – ${t || 'Supporting information'}.docx`
}

/** "1H NMR" → "¹H NMR", as writeup.py writes it. */
export function techniqueLabel(t: string): string {
  return t.trim().replace(/^(1H|13C|19F|31P|11B|15N)\b/, m => [...m].map(c => (/\d/.test(c) ? SUPER_DIGITS[+c] : c)).join(''))
}

const isNmr = (t: string) => /NMR/i.test(t)

/** "Figure S1. ¹H NMR (400 MHz, CDCl₃) of 3a." with the compound number in bold. With no instrument or solvent in the
 *  record, the spectrum file's own header (`fallback`, e.g. "¹H NMR (400 MHz, CDCl3)") is used. */
export function captionRuns(figure: number, technique: string, conditions: string, number: string, fallback = ''): Run[] {
  const label = techniqueLabel(technique), cond = conditions.trim()
  const head = cond ? `${label || 'Spectrum'} (${cond})` : fallback || label || 'Spectrum'
  return [{ text: `Figure S${figure}. `, bold: true }, ...textRuns(`${head} of `), ...(number ? [{ text: number, bold: true }] : textRuns('the title compound')), { text: '.' }]
}

/** The check list, one line per compound that needs something: "2a: no ¹³C NMR data; no HRMS". */
export function checkLines(compounds: Pick<SiCompound, 'number' | 'name' | 'eid' | 'problems'>[]): string[] {
  return compounds.filter(c => c.problems.length).map(c => `${c.number || c.name || c.eid}: ${c.problems.join('; ')}`)
}

/** The spectra a compound's figures are drawn from: those ticked, in the record's order. */
export const chosenSpectra = (c: SiCompound) => (c.spectra || []).filter(s => s.included)

/** The person's general text as paragraphs: a blank line starts a new one. */
export const paragraphs = (text: string) => text.replace(/\r\n?/g, '\n').split(/\n\s*\n/).map(p => p.trim()).filter(Boolean)

/** Everything the Word file holds, in order, from the document and the pictures drawn for it. Compounds that are no
 *  longer visible are left out. `pictures.spectra` has, per attachment, the drawn PNG and the spectrum's own header
 *  (used when the record gives no instrument or solvent), or why it couldn't be drawn. */
export function siModel(doc: Pick<SiDocument, 'title' | 'general' | 'page_size' | 'compounds'>, pictures: {
  structures: Map<number, SiImage | null>
  spectra: Map<number, { image?: SiImage; header?: string; error?: string }>
}): SiModel {
  let figure = 0
  let other = false
  const parts: SiPart[] = doc.compounds.filter(c => c.visible).map((c, k) => {
    const figures: SiFigure[] = chosenSpectra(c).map(s => {
      if (!isNmr(s.technique)) other = true
      const p = pictures.spectra.get(s.attachment_id)
      if (!p?.image) return { image: null, caption: [], label: s.filename, note: `(spectrum file ${s.filename} not shown${p?.error ? `: ${p.error}` : ''})` }
      figure++
      return { image: p.image, caption: captionRuns(figure, s.technique, s.conditions, c.number, p.header), label: s.filename }
    })
    return {
      number: c.number, name: c.name, anchor: `compound${k + 1}`, structure: pictures.structures.get(c.index) ?? null,
      experimental: htmlRuns(c.experimental_html || ''), characterisation: htmlRuns(c.characterisation_html || ''), figures,
    }
  })
  return { title: doc.title.trim(), general: paragraphs(doc.general), pageSize: doc.page_size, parts, spectraHeading: other ? 'NMR and other spectra' : 'NMR spectra' }
}
