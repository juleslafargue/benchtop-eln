import { useEffect, useRef, useState, type ReactNode } from 'react'
import { EditorContent, useEditor, type Editor } from '@tiptap/react'
import { Extension } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import Subscript from '@tiptap/extension-subscript'
import Superscript from '@tiptap/extension-superscript'
import Underline from '@tiptap/extension-underline'
import Highlight from '@tiptap/extension-highlight'
import TextAlign from '@tiptap/extension-text-align'
import TextStyle from '@tiptap/extension-text-style'
import Color from '@tiptap/extension-color'
import Link from '@tiptap/extension-link'
import TaskList from '@tiptap/extension-task-list'
import TaskItem from '@tiptap/extension-task-item'
import Table from '@tiptap/extension-table'
import TableRow from '@tiptap/extension-table-row'
import TableCell from '@tiptap/extension-table-cell'
import TableHeader from '@tiptap/extension-table-header'
import Placeholder from './placeholder'
import { AttachmentImage, PastePictures, RecordLinks, RefPreview, uploadPictures } from './editorExtras'
import { useApp } from '../App'
import type { EntryEditorProps } from './EntryCard'

// Colours that stay readable on paper and in dark mode; text keeps the theme's ink by default.
// highlighter colours, like Word / Docs; stored by name (data-hl) so each theme can show them readably in light and dark
const HIGHLIGHTS: [string, string][] = [['yellow', 'Yellow'], ['green', 'Green'], ['blue', 'Blue'], ['pink', 'Pink'], ['orange', 'Orange'], ['purple', 'Purple']]
// Word and Google Docs mark highlights as a background colour on the text; map it to the nearest highlighter
const NAMED: Record<string, string> = { yellow: '#ffff00', lime: '#00ff00', green: '#00ff00', brightgreen: '#00ff00', aqua: '#00ffff', cyan: '#00ffff',
  turquoise: '#00ffff', teal: '#008080', blue: '#0000ff', navy: '#000080', darkblue: '#000080', magenta: '#ff00ff', fuchsia: '#ff00ff', pink: '#ff00ff',
  red: '#ff0000', darkred: '#800000', maroon: '#800000', orange: '#ffa500', purple: '#800080', violet: '#8000ff', olive: '#808000', darkyellow: '#808000' }
export function nearestHighlight(css: string): string | null | false {
  let v = (css || '').trim().toLowerCase()
  if (!v || v === 'transparent' || v === 'none' || v === 'inherit' || v === 'initial') return false
  v = NAMED[v.replace(/[\s-]/g, '')] || v
  let r: number, g: number, b: number
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/.exec(v), rgb = /^rgba?\(([^)]+)\)$/.exec(v)
  if (hex) { const h = hex[1].length === 3 ? hex[1].split('').map(x => x + x).join('') : hex[1]; [r, g, b] = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)) }
  else if (rgb) { const parts = rgb[1].split(/[\s,/]+/).map(Number); [r, g, b] = parts; if (parts.length > 3 && parts[3] === 0) return false }
  else return false
  const mx = Math.max(r, g, b) / 255, mn = Math.min(r, g, b) / 255, l = (mx + mn) / 2, d = mx - mn
  if (d < 0.12 || l > 0.96) return false  // white, grey or black backgrounds aren't highlights
  let h = mx === r / 255 ? ((g - b) / 255 / d) % 6 : mx === g / 255 ? (b - r) / 255 / d + 2 : (r - g) / 255 / d + 4
  h = (h * 60 + 360) % 360
  const name = h < 15 || h >= 320 ? 'pink' : h < 42 ? 'orange' : h < 72 ? 'yellow' : h < 165 ? 'green' : h < 255 ? 'blue' : 'purple'
  return name === 'yellow' ? null : name
}

const Highlighter = Highlight.extend({
  parseHTML() {
    const fromStyle = (value: string) => { const n = nearestHighlight(value); return n === false ? false : { color: n } }
    return [{ tag: 'mark' }, { style: 'background-color', getAttrs: fromStyle as any }, { style: 'background', getAttrs: fromStyle as any }]
  },
  addAttributes() {
    return {
      color: {
        default: null,  // null = yellow, the classic highlighter
        parseHTML: (el: HTMLElement) => { const v = el.getAttribute('data-hl'); return v && v !== 'yellow' && HIGHLIGHTS.some(([k]) => k === v) ? v : null },
        renderHTML: (a: { color?: string | null }) => (a.color ? { 'data-hl': a.color } : {}),
      },
    }
  },
}).configure({ multicolor: true })

// font sizes in points, as in Word / Docs; "Default" follows the section's normal text size
export const FONT_SIZES = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 36]
const toPt = (v: string) => {
  const m = /^([\d.]+)(pt|px)$/.exec((v || '').trim())
  if (!m) return null
  const pt = m[2] === 'px' ? Number(m[1]) * 0.75 : Number(m[1])
  return pt >= 6 && pt <= 72 ? Math.round(pt) : null
}
const FontSize = Extension.create({
  name: 'fontSize',
  addGlobalAttributes() {
    return [{ types: ['textStyle'], attributes: { fontSize: {
      default: null,
      parseHTML: (el: HTMLElement) => toPt(el.style.fontSize),
      renderHTML: (a: { fontSize?: number | null }) => (a.fontSize ? { style: `font-size: ${a.fontSize}pt` } : {}),
    } } }]
  },
  addKeyboardShortcuts() {
    // Word's grow / shrink font (⌘⇧> and ⌘⇧<)
    const step = (dir: 1 | -1) => () => { setFontSize(this.editor, nextSize(this.editor.getAttributes('textStyle').fontSize ?? null, dir)); return true }
    return { 'Mod-Shift-.': step(1), 'Mod-Shift->': step(1), 'Mod-Shift-,': step(-1), 'Mod-Shift-<': step(-1) }
  },
})
const BODY_PT = 13  // a text section's normal size (17 px), for stepping up or down from "Default"
function nextSize(cur: number | null, dir: 1 | -1) {
  const from = cur ?? BODY_PT
  const pick = dir > 0 ? FONT_SIZES.find(s => s > from) : [...FONT_SIZES].reverse().find(s => s < from)
  return pick ?? (dir > 0 ? FONT_SIZES[FONT_SIZES.length - 1] : FONT_SIZES[0])
}
function setFontSize(editor: Editor, pt: number | null) {
  const ch = editor.chain().focus().setMark('textStyle', { fontSize: pt })
  ;(pt ? ch : ch.removeEmptyTextStyle()).run()
}

const COLOURS: [string, string | null][] = [['Default', null], ['Red', '#c0392b'], ['Orange', '#c46a12'], ['Green', '#2e8540'], ['Blue', '#2f6fb5'], ['Purple', '#7d4cad']]
const SYMBOLS: [string, string[]][] = [
  ['Common', ['°C', '±', '×', '÷', '≈', '≠', '≤', '≥', '~', '‰', '½', '¼', '·', '…', '§', '‡']],
  ['Arrows', ['→', '←', '⇌', '⇄', '↑', '↓', '⟶', '⇒', '↻']],
  ['Greek', ['α', 'β', 'γ', 'δ', 'ε', 'ζ', 'η', 'θ', 'κ', 'λ', 'µ', 'ν', 'π', 'ρ', 'σ', 'τ', 'φ', 'χ', 'ψ', 'ω', 'Δ', 'Σ', 'Φ', 'Ω']],
  ['Units', ['µL', 'µmol', 'mmol', 'mg', 'mL', 'M', 'equiv', 'h', 'min', 'rt', 'Å', 'ppm', 'Hz', 'nm', 'cm⁻¹', 'm/z']],
]

/** Subscript the numbers in chemical formulas within the selection (or the word at the caret):
 *  CH2Cl2 → CH₂Cl₂, Ca(OH)2 → Ca(OH)₂. Leaves numbers that don't follow an element or bracket alone. */
function formatFormula(editor: Editor) {
  const { state } = editor
  let { from, to } = state.selection
  if (from === to) { // expand to the word under the caret
    const $p = state.selection.$from, text = $p.parent.textContent, off = $p.parentOffset
    let a = off, b = off
    while (a > 0 && /\S/.test(text[a - 1])) a--
    while (b < text.length && /\S/.test(text[b])) b++
    from = $p.start() + a; to = $p.start() + b
  }
  const ranges: [number, number][] = []
  state.doc.nodesBetween(from, to, (node, pos) => {
    if (!node.isText || !node.text) return
    const re = /(?<=[A-Za-z)\]])\d+/g
    let m: RegExpExecArray | null
    while ((m = re.exec(node.text))) {
      const s = pos + m.index, e = s + m[0].length
      if (e > from && s < to) ranges.push([Math.max(s, from), Math.min(e, to)])
    }
  })
  if (!ranges.length) return
  const sub = state.schema.marks.subscript
  let tr = state.tr
  for (const [s, e] of ranges) tr = tr.addMark(s, e, sub.create())
  editor.view.dispatch(tr)
  editor.commands.focus()
}

function stamp() {
  return new Date().toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/** A toolbar dropdown that closes on outside click or Escape. */
function Menu({ label, title, children, wide, on, text }: { label: ReactNode; title: string; children: (close: () => void) => ReactNode; wide?: boolean; on?: boolean; text?: string }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const out = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', out); document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', out); document.removeEventListener('keydown', esc) }
  }, [open])
  return (
    <div className="rt-menu" ref={ref}>
      <button type="button" className={`rt-drop${on ? ' on' : ''}`} title={title} aria-label={text ? `${title}: ${text}` : title} aria-haspopup="true" aria-expanded={open}
        onMouseDown={e => e.preventDefault()} onClick={() => setOpen(o => !o)}>{label}<span className="rt-caret" aria-hidden>▾</span></button>
      {open && <div className={`rt-pop${wide ? ' wide' : ''}`} role="menu">{children(() => setOpen(false))}</div>}
    </div>
  )
}

const Icon = ({ d, fill }: { d: string; fill?: boolean }) => (
  <svg width="15" height="15" viewBox="0 0 16 16" fill={fill ? 'currentColor' : 'none'} stroke={fill ? 'none' : 'currentColor'} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={d} /></svg>
)
const ICONS = {
  table: 'M2.5 3.5h11v9h-11zM2.5 6.5h11M2.5 9.5h11M6.2 3.5v9M9.8 3.5v9',
  picture: 'M2.5 3.5h11v9h-11zM2.5 10.5l3-3 2.5 2.5 2-2 3.5 3.5M10.5 6.2h.01',
  undo: 'M5.5 4L2.5 7l3 3M2.5 7h7a4 4 0 010 8h-2',
  redo: 'M10.5 4l3 3-3 3M13.5 7h-7a4 4 0 000 8h2',
  bullet: 'M6 4h8M6 8h8M6 12h8M2.5 4h.01M2.5 8h.01M2.5 12h.01',
  check: 'M2 4.5l1.3 1.3L5.5 3.5M2 10.5l1.3 1.3 2.2-2.3M8 4.5h6M8 10.5h6',
  link: 'M6.5 9.5a3 3 0 004.2 0l2.1-2.1a3 3 0 00-4.2-4.2l-.7.7M9.5 6.5a3 3 0 00-4.2 0L3.2 8.6a3 3 0 004.2 4.2l.7-.7',
  quote: 'M3 4h10M3 4v8M6 7h7M6 10h5',
  rule: 'M2 8h12',
  clock: 'M8 2.5a5.5 5.5 0 110 11 5.5 5.5 0 010-11zM8 5v3l2 1.5',
  clear: 'M3 3l10 10M6 3h7M9.5 3L8 7.5M4 13h5',
  alignL: 'M2 4h12M2 7h8M2 10h12M2 13h8',
  alignC: 'M2 4h12M4 7h8M2 10h12M4 13h8',
  alignR: 'M2 4h12M6 7h8M2 10h12M6 13h8',
}

// shortcut hints as this computer writes them: ⌘B on a Mac, Ctrl+B elsewhere (the editor takes either)
const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
export const keys = (mac: string) => IS_MAC ? mac : mac.replace('⌘⇧', 'Ctrl+Shift+').replace('⌘', 'Ctrl+')

export default function TextEntry({ content, onChange, readOnly, experimentId, onAttachmentsChanged, label }: EntryEditorProps) {
  const { toast } = useApp()
  const wrap = useRef<HTMLDivElement>(null)
  const picker = useRef<HTMLInputElement>(null)
  const [lastHl, setLastHl] = useState<string>(() => { try { return localStorage.getItem('benchtop.highlight') || 'yellow' } catch { return 'yellow' } })
  const editor = useEditor({
    extensions: [
      StarterKit, Subscript, Superscript, Underline, Highlighter, TextStyle, Color, FontSize,
      TextAlign.configure({ types: ['heading', 'paragraph'] }),
      Link.configure({ openOnClick: false, autolink: true, HTMLAttributes: { rel: 'noopener noreferrer', target: '_blank' } }),
      TaskList, TaskItem.configure({ nested: true }),
      Table.configure({ resizable: false }), TableRow, TableCell, TableHeader,
      AttachmentImage, RecordLinks, PastePictures(experimentId, () => onAttachmentsChanged(), m => toast(m)),
      Placeholder(`Write here. ${keys('⌘,')} for subscript, ${keys('⌘.')} for superscript, “[ ] ” for a checklist.`),
    ],
    content: content.html || '',
    editable: !readOnly,
    editorProps: { attributes: { role: 'textbox', 'aria-label': label || 'Text', 'aria-multiline': 'true' } },
    onUpdate: ({ editor }) => onChange({ ...content, html: editor.getHTML() }),
  }, [readOnly])
  // the section can be renamed while the editor stays: keep its name for screen readers in step
  useEffect(() => {
    if (editor && !editor.isDestroyed) editor.setOptions({ editorProps: { attributes: { role: 'textbox', 'aria-label': label || 'Text', 'aria-multiline': 'true' } } })
  }, [editor, label])

  // pick up server-normalised HTML without clobbering the caret during typing
  useEffect(() => {
    if (editor && !editor.isFocused && content.html !== editor.getHTML()) editor.commands.setContent(content.html || '', false)
  }, [content.html, editor])

  if (!editor) return null
  const c = () => editor.chain().focus()
  // a button that shows text (x₂, H₂O) is named starting with that text, so it can be called by what it shows
  const B = ({ cmd, on, label, title, disabled, text }: { cmd: () => void; on?: boolean; label: ReactNode; title: string; disabled?: boolean; text?: string }) => (
    <button type="button" className={on ? 'on' : ''} title={title} aria-label={text ? `${text}: ${title}` : title} aria-pressed={on === undefined ? undefined : !!on} disabled={disabled}
      onMouseDown={e => e.preventDefault()} onClick={cmd}>{label}</button>
  )
  const Sep = () => <span className="rt-sep" aria-hidden />
  const style = editor.isActive('heading', { level: 2 }) ? 'Heading' : editor.isActive('heading', { level: 3 }) ? 'Subheading' : editor.isActive('codeBlock') ? 'Code' : 'Text'
  const inTable = editor.isActive('table')
  const colour = editor.getAttributes('textStyle').color as string | undefined
  const size = (editor.getAttributes('textStyle').fontSize as number | null | undefined) ?? null
  const hlOn = editor.isActive('highlight')
  const hlNow = hlOn ? ((editor.getAttributes('highlight').color as string | null) || 'yellow') : null
  const applyHl = (name: string) => {
    setLastHl(name)
    try { localStorage.setItem('benchtop.highlight', name) } catch { /* private mode */ }
    if (hlNow === name) c().unsetHighlight().run()
    else c().setHighlight({ color: name === 'yellow' ? null : name } as any).run()
  }
  const picture = editor.isActive('attachmentImage')  // a selected picture: the alignment menu lines it up instead of text
  const pa = picture ? editor.getAttributes('attachmentImage').align : null
  const align = picture ? (pa === 'center' || pa === 'right' ? pa : 'left')
    : (['center', 'right'] as const).find(a => editor.isActive({ textAlign: a })) || 'left'

  return (
    <div>
      {!readOnly && (
        <div className="rt-toolbar" role="toolbar" aria-label="Formatting">
          <B label={<Icon d={ICONS.undo} />} title={keys('Undo (⌘Z)')} cmd={() => c().undo().run()} disabled={!editor.can().undo()} />
          <B label={<Icon d={ICONS.redo} />} title={keys('Redo (⌘⇧Z)')} cmd={() => c().redo().run()} disabled={!editor.can().redo()} />
          <Sep />
          <Menu label={<span className="rt-style">{style}</span>} title="Paragraph style" text={style}>
            {close => <>
              {([['Text', () => c().setParagraph().run(), 'p'], ['Heading', () => c().setHeading({ level: 2 }).run(), 'h2'], ['Subheading', () => c().setHeading({ level: 3 }).run(), 'h3'], ['Code', () => c().setCodeBlock().run(), 'code']] as const)
                .map(([l, fn, cls]) => <button key={l} role="menuitem" className={`rt-item st-${cls}${style === l ? ' on' : ''}`} onMouseDown={e => e.preventDefault()} onClick={() => { fn(); close() }}>{l}</button>)}
            </>}
          </Menu>
          <Sep />
          <B label={<b>B</b>} title={keys('Bold (⌘B)')} on={editor.isActive('bold')} cmd={() => c().toggleBold().run()} />
          <B label={<i>I</i>} title={keys('Italic (⌘I)')} on={editor.isActive('italic')} cmd={() => c().toggleItalic().run()} />
          <B label={<u>U</u>} title={keys('Underline (⌘U)')} on={editor.isActive('underline')} cmd={() => c().toggleUnderline().run()} />
          <B label={<s>S</s>} title={keys('Strikethrough (⌘⇧S)')} on={editor.isActive('strike')} cmd={() => c().toggleStrike().run()} />
          <B label="x₂" text="x₂" title={keys('Subscript (⌘,)')} on={editor.isActive('subscript')} cmd={() => c().toggleSubscript().run()} />
          <B label="x²" text="x²" title={keys('Superscript (⌘.)')} on={editor.isActive('superscript')} cmd={() => c().toggleSuperscript().run()} />
          <B label={<span className="rt-formula">H₂O</span>} text="H₂O" title="Format as chemical formula: subscripts the numbers in the selection or word (CH2Cl2 → CH₂Cl₂)" cmd={() => formatFormula(editor)} />
          <span className="rt-split">
            <B label={<span className="rt-hl" data-hl={lastHl}>ab</span>} text="ab" title={`Highlight in ${lastHl} (${keys('⌘⇧H')} for yellow)`} on={hlOn} cmd={() => applyHl(lastHl)} />
            <Menu title="Highlight colour" on={false} label={null}>
              {close => <div className="stack" style={{ gap: 4, padding: 2 }}>
                <div className="rt-swatches">
                  {HIGHLIGHTS.map(([k, n]) => <button key={k} role="menuitem" title={n} aria-label={`Highlight ${n.toLowerCase()}`} className={`rt-swatch rt-hl-swatch${hlNow === k ? ' on' : ''}`}
                    onMouseDown={e => e.preventDefault()} onClick={() => { applyHl(k); close() }}><span className="rt-hl" data-hl={k}>ab</span></button>)}
                </div>
                <button role="menuitem" className="rt-item" disabled={!hlOn} onMouseDown={e => e.preventDefault()} onClick={() => { c().unsetHighlight().run(); close() }}>No highlight</button>
              </div>}
            </Menu>
          </span>
          <span className="rt-split rt-size" role="group" aria-label="Font size">
            <B label="−" title={keys('Smaller text (⌘⇧<)')} cmd={() => setFontSize(editor, nextSize(size, -1))} disabled={size === FONT_SIZES[0]} />
            <Menu title="Font size" on={size !== null} label={<span className="rt-size-n">{size ?? 'Size'}</span>}>
              {close => <div className="rt-size-list">
                <button role="menuitem" className={`rt-item${size === null ? ' on' : ''}`} onMouseDown={e => e.preventDefault()} onClick={() => { setFontSize(editor, null); close() }}>Default</button>
                {FONT_SIZES.map(pt => <button key={pt} role="menuitem" className={`rt-item${size === pt ? ' on' : ''}`} onMouseDown={e => e.preventDefault()}
                  onClick={() => { setFontSize(editor, pt); close() }}><span style={{ fontSize: `${Math.min(pt, 20)}pt`, lineHeight: 1.1 }}>{pt}</span></button>)}
              </div>}
            </Menu>
            <B label="+" title={keys('Larger text (⌘⇧>)')} cmd={() => setFontSize(editor, nextSize(size, 1))} disabled={size === FONT_SIZES[FONT_SIZES.length - 1]} />
          </span>
          <Menu title="Text colour" text="A" on={!!colour} label={<span className="rt-colour" style={{ borderBottomColor: colour || 'currentColor' }}>A</span>}>
            {close => <div className="rt-swatches">
              {COLOURS.map(([n, v]) => <button key={n} role="menuitem" title={n} aria-label={n} className={`rt-swatch${(colour || null) === v ? ' on' : ''}`}
                style={{ color: v || 'var(--ink)' }} onMouseDown={e => e.preventDefault()}
                onClick={() => { v ? c().setColor(v).run() : c().unsetColor().run(); close() }}>A</button>)}
            </div>}
          </Menu>
          <Sep />
          <B label={<Icon d={ICONS.bullet} />} title="Bulleted list" on={editor.isActive('bulletList')} cmd={() => c().toggleBulletList().run()} />
          <B label="1." text="1." title="Numbered steps" on={editor.isActive('orderedList')} cmd={() => c().toggleOrderedList().run()} />
          <B label={<Icon d={ICONS.check} />} title="Checklist" on={editor.isActive('taskList')} cmd={() => c().toggleTaskList().run()} />
          <Menu title={picture ? 'Picture alignment' : 'Alignment'} label={<Icon d={align === 'center' ? ICONS.alignC : align === 'right' ? ICONS.alignR : ICONS.alignL} />}>
            {close => <>{([['left', 'Left', ICONS.alignL], ['center', 'Centre', ICONS.alignC], ['right', 'Right', ICONS.alignR]] as const).map(([a, l, d]) =>
              <button key={a} role="menuitem" className={`rt-item${align === a ? ' on' : ''}`} onMouseDown={e => e.preventDefault()}
                onClick={() => { (picture ? c().updateAttributes('attachmentImage', { align: a === 'left' ? null : a }) : c().setTextAlign(a)).run(); close() }}><Icon d={d} /> {l}</button>)}</>}
          </Menu>
          <Sep />
          <Menu title="Insert" label="Insert">
            {close => <>
              <button role="menuitem" className="rt-item" onMouseDown={e => e.preventDefault()} onClick={() => { c().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run(); close() }}><Icon d={ICONS.table} /> Table</button>
              <button role="menuitem" className="rt-item" onMouseDown={e => e.preventDefault()} onClick={() => { c().toggleBlockquote().run(); close() }}><Icon d={ICONS.quote} /> Note / quote</button>
              <button role="menuitem" className="rt-item" onMouseDown={e => e.preventDefault()} onClick={() => { c().setHorizontalRule().run(); close() }}><Icon d={ICONS.rule} /> Divider</button>
              <button role="menuitem" className="rt-item" onMouseDown={e => e.preventDefault()} onClick={() => { picker.current?.click(); close() }} title="You can also paste or drop a picture straight into the text"><Icon d={ICONS.picture} /> Picture…</button>
              <button role="menuitem" className="rt-item" onMouseDown={e => e.preventDefault()} onClick={() => { c().insertContent(`<p><strong>${stamp()}</strong> </p>`).run(); close() }}><Icon d={ICONS.clock} /> Date &amp; time stamp</button>
            </>}
          </Menu>
          <Menu title="Link" on={editor.isActive('link')} label={<Icon d={ICONS.link} />}>
            {close => <form className="rt-link" onSubmit={e => {
              e.preventDefault()
              const url = String(new FormData(e.currentTarget).get('url') || '').trim()
              if (!url) c().extendMarkRange('link').unsetLink().run()
              else {
                const href = /^[a-z]+:/i.test(url) ? url : `https://${url}`
                if (/^(javascript|data|vbscript):/i.test(href)) return
                if (editor.state.selection.empty && !editor.isActive('link')) c().insertContent(`<a href="${href.replace(/"/g, '&quot;')}">${url.replace(/</g, '&lt;')}</a> `).run()
                else c().extendMarkRange('link').setLink({ href }).run()
              }
              close()
            }}>
              <input className="input" autoFocus placeholder="Paste a link (DOI, paper, SI…)" defaultValue={editor.getAttributes('link').href || ''} name="url" />
              <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                {editor.isActive('link') && <button type="button" className="btn ghost sm" onClick={() => { c().extendMarkRange('link').unsetLink().run(); close() }}>Remove</button>}
                <button className="btn primary sm">Apply</button>
              </div>
            </form>}
          </Menu>
          <Menu title="Insert symbol" text="Ω" label="Ω" wide>
            {close => <>{SYMBOLS.map(([group, list]) => <div key={group} className="rt-symgroup">
              <div className="rt-symhead">{group}</div>
              <div className="rt-syms">{list.map(s => <button key={s} role="menuitem" title={`Insert ${s}`} onMouseDown={e => e.preventDefault()}
                onClick={() => { c().insertContent(s).run(); close() }}>{s}</button>)}</div>
            </div>)}</>}
          </Menu>
          {['°C', '±', 'µ', '→', 'Δ'].map(s => <B key={s} label={s} title={`Insert ${s}`} cmd={() => c().insertContent(s).run()} />)}
          <Sep />
          <B label={<Icon d={ICONS.clear} />} title="Clear formatting" cmd={() => c().unsetAllMarks().clearNodes().run()} />
          {inTable && <div className="rt-table-tools" role="group" aria-label="Table">
            <span className="faint">Table:</span>
            <B label="+ Row" title="Add a row below" cmd={() => c().addRowAfter().run()} />
            <B label="+ Column" title="Add a column to the right" cmd={() => c().addColumnAfter().run()} />
            <B label="− Row" title="Delete this row" cmd={() => c().deleteRow().run()} />
            <B label="− Column" title="Delete this column" cmd={() => c().deleteColumn().run()} />
            <B label="Header" title="Toggle header row" cmd={() => c().toggleHeaderRow().run()} />
            <B label="Delete table" title="Delete the whole table" cmd={() => c().deleteTable().run()} />
          </div>}
        </div>
      )}
      <div className="prose" ref={wrap}><EditorContent editor={editor} /></div>
      <RefPreview root={wrap} />
      {!readOnly && <input ref={picker} type="file" accept="image/*" multiple hidden onChange={e => {
        const files = Array.from(e.target.files || []); e.target.value = ''
        editor.commands.focus()
        uploadPictures(editor.view, files, experimentId, () => onAttachmentsChanged(), m => toast(m))
      }} />}
    </div>
  )
}
