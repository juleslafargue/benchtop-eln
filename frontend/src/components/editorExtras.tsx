// Editor additions: pictures stored as attachments, and EXP-/MAT- numbers that become links with a preview.
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { Extension, Node, mergeAttributes } from '@tiptap/core'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import { day, api, attachmentUrl, type Attachment } from '../api'
import { Mol } from './bits'

/** How a picture sits in the text, as in Word or Google Docs. Stored on the <img> as data-align and data-width. */
export type PictureLayout = null | 'center' | 'right' | 'wrap-left' | 'wrap-right'
const LAYOUTS: PictureLayout[] = [null, 'center', 'right', 'wrap-left', 'wrap-right']
const clampWidth = (w: unknown) => { const n = Math.round(Number(w)); return Number.isFinite(n) && n >= 10 && n <= 100 ? n : null }
const WRAP_DEFAULT = 40  // % of the text width when text first wraps around a picture

const svg = (d: string) => `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="${d}"/></svg>`
const PLACE: [PictureLayout, string, string][] = [
  [null, 'On its own line, left', 'M2 3.5h6v6H2zM2 12.5h12'],
  ['center', 'On its own line, centred', 'M5 3.5h6v6H5zM2 12.5h12'],
  ['right', 'On its own line, right', 'M8 3.5h6v6H8zM2 12.5h12'],
  ['wrap-left', 'Wrap text: picture on the left', 'M2 3.5h6v6H2zM10 4h4M10 6.5h4M10 9h4M2 12.5h12'],
  ['wrap-right', 'Wrap text: picture on the right', 'M8 3.5h6v6H8zM2 4h4M2 6.5h4M2 9h4M2 12.5h12'],
]
const SIZES: [number | null, string, string][] = [[33, 'S', 'Small (a third of the text width)'], [50, 'M', 'Medium (half the text width)'],
  [75, 'L', 'Large (three quarters of the text width)'], [100, 'Full', 'Full text width']]

/** A picture that lives in the experiment's files: <img data-attachment-id="12">. Click it to place and size it;
 *  drag a corner to resize. */
export const AttachmentImage = Node.create({
  name: 'attachmentImage',
  group: 'block',
  atom: true,
  draggable: true,
  selectable: true,
  addAttributes() {
    return {
      id: { default: null, parseHTML: el => Number(el.getAttribute('data-attachment-id')) || null, renderHTML: a => ({ 'data-attachment-id': a.id }) },
      alt: { default: '', parseHTML: el => el.getAttribute('alt') || '', renderHTML: a => (a.alt ? { alt: a.alt } : {}) },
      align: { default: null, parseHTML: el => (LAYOUTS.includes(el.getAttribute('data-align') as PictureLayout) ? el.getAttribute('data-align') : null),
        renderHTML: a => (a.align ? { 'data-align': a.align } : {}) },
      width: { default: null, parseHTML: el => clampWidth(el.getAttribute('data-width')), renderHTML: a => (a.width ? { 'data-width': String(a.width) } : {}) },
    }
  },
  parseHTML() { return [{ tag: 'img[data-attachment-id]' }] },
  renderHTML({ HTMLAttributes }) { return ['img', mergeAttributes(HTMLAttributes)] },
  addNodeView() {
    return ({ node, editor, getPos }) => {
      let cur = node
      const wrap = document.createElement('figure')
      const frame = document.createElement('span')  // hugs the picture: handles and the bar sit on it
      frame.className = 'att-img-frame'
      const img = document.createElement('img')
      img.alt = node.attrs.alt || 'picture'
      img.draggable = false
      const label = document.createElement('span')
      label.className = 'att-img-size'
      label.setAttribute('aria-hidden', 'true')

      const set = (attrs: Record<string, unknown>) => {
        const pos = typeof getPos === 'function' ? getPos() : null
        if (pos == null || !editor.isEditable) return
        editor.chain().focus().command(({ tr }) => { tr.setNodeMarkup(pos, undefined, { ...cur.attrs, ...attrs }); return true }).setNodeSelection(pos).run()
      }
      const button = (html: string, title: string, onClick: () => void, cls = '') => {
        const b = document.createElement('button')
        b.type = 'button'; b.title = title; b.setAttribute('aria-label', title); b.innerHTML = html
        if (cls) b.className = cls
        b.addEventListener('mousedown', e => e.preventDefault())
        b.addEventListener('click', onClick)
        return b
      }
      const sep = () => { const s = document.createElement('span'); s.className = 'att-img-sep'; return s }

      // the picture toolbar
      const tools = document.createElement('div')
      tools.className = 'att-img-tools'
      tools.setAttribute('role', 'toolbar')
      tools.setAttribute('aria-label', 'Picture')
      const placeBtns = PLACE.map(([value, title, d]) => ({ value, b: button(svg(d), title, () => set({ align: value, width: (value === 'wrap-left' || value === 'wrap-right') && !cur.attrs.width ? WRAP_DEFAULT : cur.attrs.width })) }))
      const sizeBtns = SIZES.map(([w, text, title]) => ({ w, b: button(text, title, () => set({ width: w }), 'txt') }))
      const original = button(svg('M3 8a5 5 0 1 0 1.5-3.5M3 2.5v3h3'), 'Original size', () => set({ width: null }))
      const view = button(svg('M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9'), 'View full size', () => openPicture(cur.attrs.id, cur.attrs.alt))
      tools.append(...placeBtns.map(x => x.b), sep(), ...sizeBtns.map(x => x.b), original, sep(), view)

      // corner handles: drag to resize, keeping the proportions (like Word / Docs)
      const handles = (['nw', 'ne', 'sw', 'se'] as const).map(corner => {
        const h = document.createElement('span')
        h.className = `att-img-h ${corner}`
        h.setAttribute('aria-hidden', 'true')
        h.addEventListener('pointerdown', e => {
          if (!editor.isEditable) return
          e.preventDefault(); e.stopPropagation()
          const column = (wrap.parentElement as HTMLElement).clientWidth || 1
          const startX = e.clientX, startW = img.getBoundingClientRect().width
          const leftSide = corner === 'nw' || corner === 'sw'
          const k = cur.attrs.align === 'center' ? 2 : 1  // a centred picture grows on both sides
          let pct = clampWidth((startW / column) * 100) || 100
          h.setPointerCapture(e.pointerId)
          wrap.classList.add('resizing')
          const move = (ev: PointerEvent) => {
            const dx = (ev.clientX - startX) * (leftSide ? -1 : 1) * k
            pct = Math.max(10, Math.min(100, Math.round(((startW + dx) / column) * 100)))
            paint(pct)
            label.textContent = `${pct}%`
          }
          const up = () => {
            h.removeEventListener('pointermove', move); h.removeEventListener('pointerup', up); h.removeEventListener('pointercancel', up)
            wrap.classList.remove('resizing')
            set({ width: pct })
          }
          h.addEventListener('pointermove', move); h.addEventListener('pointerup', up); h.addEventListener('pointercancel', up)
        })
        return h
      })

      frame.append(img, label, ...handles, tools)
      wrap.append(frame)

      const paint = (dragWidth?: number) => {
        const align = cur.attrs.align as PictureLayout
        const width = dragWidth ?? cur.attrs.width as number | null
        const floating = align === 'wrap-left' || align === 'wrap-right'
        wrap.classList.add('att-img')  // classList, not className: keep the editor's "selected" class
        for (const a of LAYOUTS) if (a) wrap.classList.toggle(`align-${a}`, align === a)
        wrap.classList.toggle('sized', !!width || floating)
        // floating pictures size the whole figure (text flows beside it); others size the frame within the line
        wrap.style.width = floating ? `${width || WRAP_DEFAULT}%` : ''
        frame.style.width = floating ? '100%' : width ? `${width}%` : ''
        placeBtns.forEach(({ value, b }) => b.setAttribute('aria-pressed', String(align === value)))
        sizeBtns.forEach(({ w, b }) => b.setAttribute('aria-pressed', String(cur.attrs.width === w)))
        original.disabled = !cur.attrs.width
      }
      paint()
      img.addEventListener('dblclick', () => openPicture(cur.attrs.id, cur.attrs.alt))
      img.addEventListener('click', () => { if (!editor.isEditable) openPicture(cur.attrs.id, cur.attrs.alt) })
      if (node.attrs.id) attachmentUrl(node.attrs.id).then(u => { img.src = u }).catch(() => { wrap.classList.add('missing'); img.alt = `${img.alt} (couldn't load)` })
      return {
        dom: wrap,
        update: (n: any) => { if (n.type !== cur.type || n.attrs.id !== cur.attrs.id) return false; cur = n; paint(); return true },
        stopEvent: (e: Event) => !!(e.target as HTMLElement).closest?.('.att-img-tools, .att-img-h'),
        ignoreMutation: () => true,
      }
    }
  },
})

/** Open a picture full size (the viewer is <PictureViewer/>, mounted once in the app). */
export function openPicture(id: number | null, alt = '') {
  if (id) window.dispatchEvent(new CustomEvent('benchtop:view-picture', { detail: { id, alt } }))
}

export function PictureViewer() {
  const [pic, setPic] = useState<{ id: number; alt: string; url?: string } | null>(null)
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent).detail
      setPic({ id: d.id, alt: d.alt })
      attachmentUrl(d.id).then(url => setPic(p => (p && p.id === d.id ? { ...p, url } : p))).catch(() => setPic(null))
    }
    window.addEventListener('benchtop:view-picture', on)
    return () => window.removeEventListener('benchtop:view-picture', on)
  }, [])
  useEffect(() => {
    if (!pic) return
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') setPic(null) }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [pic])
  if (!pic) return null
  return createPortal(
    <div className="pic-viewer" role="dialog" aria-modal="true" aria-label={pic.alt || 'Picture'} onClick={() => setPic(null)}>
      {pic.url ? <img src={pic.url} alt={pic.alt} onClick={e => e.stopPropagation()} /> : <span className="faint">Loading…</span>}
      <div className="pic-viewer-bar" onClick={e => e.stopPropagation()}>
        <span>{pic.alt}</span>
        <button className="btn sm" onClick={() => setPic(null)}>Close</button>
      </div>
    </div>, document.body)
}

/** Upload pictures pasted or dropped into a text section, then put them where the cursor is. */
export async function uploadPictures(view: any, files: File[], experimentId: number, onUploaded: () => void, onError: (m: string) => void, pos?: number) {
  {
    for (const f of files) {
      try {
        const fd = new FormData()
        const name = f.name && f.name !== 'image.png' ? f.name : `pasted_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.${(f.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`
        fd.append('file', f, name)
        const att = await api.post<Attachment>(`/api/experiments/${experimentId}/attachments`, fd)
        const node = view.state.schema.nodes.attachmentImage.create({ id: att.id, alt: att.filename })
        const tr = pos != null ? view.state.tr.insert(Math.min(pos, view.state.doc.content.size), node) : view.state.tr.replaceSelectionWith(node)
        view.dispatch(tr)
        onUploaded()
      } catch (e: any) { onError(e.message) }
    }
  }
}

export function PastePictures(experimentId: number, onUploaded: () => void, onError: (m: string) => void) {
  const upload = (view: any, files: File[], pos?: number) => uploadPictures(view, files, experimentId, onUploaded, onError, pos)
  const images = (list: FileList | null | undefined) => Array.from(list || []).filter(f => f.type.startsWith('image/'))
  return Extension.create({
    name: 'pastePictures',
    addProseMirrorPlugins() {
      return [new Plugin({
        key: new PluginKey('pastePictures'),
        props: {
          handlePaste: (view, event) => {
            const files = images(event.clipboardData?.files)
            if (!files.length || !view.editable) return false
            upload(view, files); return true
          },
          handleDrop: (view, event) => {
            const files = images((event as DragEvent).dataTransfer?.files)
            if (!files.length || !view.editable) return false
            event.preventDefault()
            const at = view.posAtCoords({ left: (event as DragEvent).clientX, top: (event as DragEvent).clientY })
            upload(view, files, at?.pos); return true
          },
        },
      })]
    },
  })
}

const REF = /\b(EXP|MAT)-\d{5}\b/g
/** Highlights EXP-00025 / MAT-00007 wherever they're typed; the hover card does the rest. */
export const RecordLinks = Extension.create({
  name: 'recordLinks',
  addProseMirrorPlugins() {
    const deco = (doc: any) => {
      const out: Decoration[] = []
      doc.descendants((n: any, pos: number) => {
        if (!n.isText || !n.text) return
        for (const m of n.text.matchAll(REF)) out.push(Decoration.inline(pos + m.index!, pos + m.index! + m[0].length, { class: 'ref-link', 'data-ref': m[0] }))
      })
      return DecorationSet.create(doc, out)
    }
    return [new Plugin({
      key: new PluginKey('recordLinks'),
      state: { init: (_, { doc }) => deco(doc), apply: (tr, old) => (tr.docChanged ? deco(tr.doc) : old) },
      props: { decorations(state) { return this.getState(state) } },
    })]
  },
})

interface RefInfo { kind: 'experiment' | 'material'; id: number; code: string; title: string; status?: string; author?: string; date?: string; smiles?: string; formula?: string; mw?: number; cas?: string }
const cache = new Map<string, Promise<RefInfo | null>>()
const lookup = (code: string) => { let p = cache.get(code); if (!p) { p = api.get<RefInfo>(`/api/lookup/${code}`).catch(() => null); cache.set(code, p) } return p }

/** Hover (or tap) an EXP-/MAT- number inside `root` for a preview; click "Open", or ⌘-click the number. */
export function RefPreview({ root }: { root: React.RefObject<HTMLElement> }) {
  const nav = useNavigate()
  const [at, setAt] = useState<{ code: string; x: number; y: number } | null>(null)
  const [info, setInfo] = useState<RefInfo | null | undefined>(undefined)
  const hide = useRef<number>()
  useEffect(() => {
    const el = root.current
    if (!el) return
    const over = (e: MouseEvent) => {
      const t = (e.target as HTMLElement).closest('.ref-link') as HTMLElement | null
      if (!t) return
      clearTimeout(hide.current)
      const r = t.getBoundingClientRect()
      setAt(a => (a?.code === t.dataset.ref ? a : { code: t.dataset.ref!, x: r.left, y: r.bottom }))
    }
    const out = (e: MouseEvent) => { if ((e.target as HTMLElement).closest('.ref-link')) hide.current = window.setTimeout(() => setAt(null), 250) }
    const click = (e: MouseEvent) => {
      const t = (e.target as HTMLElement).closest('.ref-link') as HTMLElement | null
      if (t && (e.metaKey || e.ctrlKey || !el.querySelector('[contenteditable=true]'))) {
        e.preventDefault()
        lookup(t.dataset.ref!).then(i => i && nav(i.kind === 'experiment' ? `/experiments/${i.id}` : `/materials/${i.id}`))
      }
    }
    el.addEventListener('mouseover', over); el.addEventListener('mouseout', out); el.addEventListener('click', click)
    return () => { el.removeEventListener('mouseover', over); el.removeEventListener('mouseout', out); el.removeEventListener('click', click) }
  }, [root, nav])
  useEffect(() => { if (at) { setInfo(undefined); lookup(at.code).then(setInfo) } }, [at?.code]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!at) return null
  const left = Math.max(8, Math.min(at.x, window.innerWidth - 300)), top = at.y + 6 + 220 > window.innerHeight ? at.y - 230 : at.y + 6
  return createPortal(
    <div className="ref-card" style={{ left, top }} onMouseEnter={() => clearTimeout(hide.current)} onMouseLeave={() => setAt(null)} role="tooltip">
      {info === undefined ? <span className="faint">Looking up {at.code}…</span>
        : info === null ? <span className="faint">{at.code} doesn't exist or isn't shared with you.</span>
          : <>
            <div className="faint" style={{ fontSize: 12 }}>{info.code}{info.status ? <> · <span className={`status ${info.status}`}>{info.status === 'open' ? 'draft' : info.status}</span></> : ''}</div>
            <b className="ref-title">{info.title}</b>
            {info.smiles && <span className="ref-mol"><Mol smiles={info.smiles} rxn={info.kind === 'experiment' && info.smiles.includes('>')} w={540} h={150} /></span>}
            <div className="faint" style={{ fontSize: 12 }}>{info.kind === 'experiment' ? `${info.author || ''}${info.date ? ` · ${day(info.date)}` : ''}`
              : [info.formula, info.mw ? `${info.mw.toFixed(2)} g/mol` : '', info.cas].filter(Boolean).join(' · ')}</div>
            <button className="btn sm" onClick={() => nav(info.kind === 'experiment' ? `/experiments/${info.id}` : `/materials/${info.id}`)}>Open {info.code}</button>
          </>}
    </div>, document.body)
}
