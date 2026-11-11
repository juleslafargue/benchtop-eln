import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { api, type EntryKind } from '../api'

export interface PluginSection { key: string; name: string; description: string; plugin: string }
let pluginCache: Promise<PluginSection[]> | null = null
/** Section types from switched-on plugins (fetched once per page load). */
export const pluginSections = () => (pluginCache ??= api.get<PluginSection[]>('/api/plugins/sections').catch(() => []))
export const resetPluginSections = () => { pluginCache = null }


/** Every section kind, grouped, with a one-line description so people can tell them apart. */
export const SECTION_KINDS: { kind: EntryKind; label: string; hint: string; group: string; icon: ReactNode; plugin?: string }[] = [
  { kind: 'reaction', label: 'Reaction', hint: 'Scheme, conditions and stoichiometry table', group: 'Chemistry',
    icon: <path d="M2 8h9M8.5 5.5 11 8l-2.5 2.5M13 4.5v7" /> },
  { kind: 'purification', label: 'Work-up & purification', hint: 'Method, eluent, Rf, isolated mass', group: 'Chemistry',
    icon: <path d="M3 3h10l-4 5v4.5L7 14V8z" /> },
  { kind: 'characterization', label: 'Characterisation', hint: 'NMR, HRMS, IR, mp; drop spectra here', group: 'Chemistry',
    icon: <path d="M1.5 12.5h2l1.5-3 1.5 3h1L9.5 3l1.5 9.5h3" /> },
  { kind: 'screen', label: 'Screening grid', hint: 'Two-factor screen with a yield heatmap', group: 'Chemistry',
    icon: <><rect x="2.5" y="2.5" width="11" height="11" rx="1.5" /><path d="M2.5 6.2h11M2.5 9.8h11M6.2 2.5v11M9.8 2.5v11" /></> },
  { kind: 'safety', label: 'Risk assessment', hint: 'Hazards for every chemical in the reaction', group: 'Chemistry',
    icon: <><path d="M8 2 14.5 13.5h-13z" /><path d="M8 6.5v3.2M8 11.6v.1" /></> },
  { kind: 'text', label: 'Text', hint: 'Procedure, observations, notes', group: 'Record',
    icon: <path d="M3 4h10M3 7h10M3 10h10M3 13h6" /> },
  { kind: 'table', label: 'Data table', hint: 'Free-form table of values', group: 'Record',
    icon: <><rect x="2.5" y="3" width="11" height="10" rx="1.5" /><path d="M2.5 6.5h11M7 6.5V13" /></> },
  { kind: 'references', label: 'References', hint: 'Papers by DOI: title, authors, journal', group: 'Record',
    icon: <path d="M8 4.6C6.8 3.7 4.8 3.4 2.5 3.6v8.9c2.3-.2 4.3.1 5.5 1 1.2-.9 3.2-1.2 5.5-1V3.6c-2.3-.2-4.3.1-5.5 1zM8 4.6v8.9" /> },
  { kind: 'file', label: 'Files', hint: 'Attach any file to the record', group: 'Record',
    icon: <path d="M10.5 5.5 6 10a1.4 1.4 0 0 0 2 2l5-5a2.8 2.8 0 0 0-4-4L4 8a4.2 4.2 0 0 0 6 6l3.5-3.5" /> },
]
/** The section types that match what was typed in the menu's box, best first: those whose name matches (starting with
 *  it before containing it) come before those that only match by their description, each in the menu's order.
 *  (0.36: "table" and Enter added a Reaction, whose description says "stoichiometry table", not a Data table.) */
export function matchSections<T extends { label: string; hint: string }>(all: T[], q: string): T[] {
  const t = q.trim().toLowerCase()
  if (!t) return all
  const rank = (k: T) => {
    const label = k.label.toLowerCase()
    const hint = k.hint.toLowerCase()
    return label.startsWith(t) ? 0 : label.includes(t) ? 1 : hint.includes(t) ? 2 : `${label} ${hint}`.includes(t) ? 3 : -1
  }
  return all.map((k, i) => ({ k, i, r: rank(k) })).filter(x => x.r >= 0).sort((a, b) => a.r - b.r || a.i - b.i).map(x => x.k)
}

const PUZZLE = <path d="M6 2.5h2.5a1.25 1.25 0 1 1 2.5 0H13.5V5a1.25 1.25 0 1 1 0 2.5v6H10a1.25 1.25 0 1 0-2.5 0H2.5V10a1.25 1.25 0 1 0 0-2.5V2.5z" />

export function KindIcon({ kind }: { kind: EntryKind }) {
  const k = SECTION_KINDS.find(x => x.kind === kind)
  return <svg className="kind-icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor"
    strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">{kind === 'custom' ? PUZZLE : k?.icon}</svg>
}

/** The menu itself: positioned under (or above) its anchor, keyboard navigable, type to filter. */
function Menu({ anchor, existing, onPick, onClose, align }: {
  anchor: HTMLElement; existing: EntryKind[]; onPick: (k: EntryKind, plugin?: string) => void; onClose: () => void; align: 'start' | 'center' | 'end'
}) {
  const [extra, setExtra] = useState<PluginSection[]>([])
  useEffect(() => { pluginSections().then(setExtra) }, [])
  const all = [...SECTION_KINDS, ...extra.map(p => ({ kind: 'custom' as EntryKind, label: p.name, hint: p.description || 'From a plugin', group: 'From plugins', icon: PUZZLE, plugin: p.key }))]
  const ref = useRef<HTMLDivElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const [pos, setPos] = useState<{ top: number; left: number; up: boolean } | null>(null)
  const items = matchSections(all, q)

  const place = () => {
    const r = anchor.getBoundingClientRect(), m = ref.current
    const w = m?.offsetWidth || 340, h = m?.offsetHeight || 420
    const up = r.bottom + h + 12 > innerHeight && r.top - h - 12 > 0
    let left = align === 'end' ? r.right - w : align === 'center' ? r.left + r.width / 2 - w / 2 : r.left
    left = Math.max(8, Math.min(left, innerWidth - w - 8))
    setPos({ top: up ? r.top - h - 6 : r.bottom + 6, left, up })
  }
  useLayoutEffect(place, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const down = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node) && !anchor.contains(e.target as Node)) onClose() }
    addEventListener('mousedown', down); addEventListener('scroll', place, true); addEventListener('resize', place)
    return () => { removeEventListener('mousedown', down); removeEventListener('scroll', place, true); removeEventListener('resize', place) }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { setActive(0) }, [q])
  // focus once placed (a hidden element can't take focus, so autoFocus would miss)
  const placed = pos != null
  useEffect(() => { if (placed) input.current?.focus({ preventScroll: true }) }, [placed])

  const key = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(a => (a + 1) % Math.max(1, items.length)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(a => (a - 1 + items.length) % Math.max(1, items.length)) }
    else if (e.key === 'Enter' && items[active]) { e.preventDefault(); onPick(items[active].kind, items[active].plugin) }
    else if (e.key === 'Escape') { e.preventDefault(); onClose(); anchor.focus() }
    else if (e.key === 'Tab') onClose()
  }

  let lastGroup = ''
  return (
    <div ref={ref} className={`add-menu${pos?.up ? ' up' : ''}`} role="dialog" aria-label="Add a section"
      style={pos ? { top: pos.top, left: pos.left } : { visibility: 'hidden', top: 0, left: 0 }} onKeyDown={key}>
      <input ref={input} className="add-menu-filter" placeholder="Add a section…" value={q} onChange={e => setQ(e.target.value)}
        aria-label="Filter section types" aria-controls="add-menu-list" aria-activedescendant={items[active] ? `add-${items[active].plugin || items[active].kind}` : undefined} />
      <ul id="add-menu-list" role="listbox">
        {items.map((k, i) => {
          const head = !q && k.group !== lastGroup ? <li className="add-menu-group" role="presentation">{(lastGroup = k.group)}</li> : null
          const has = !k.plugin && existing.includes(k.kind)
          return [head,
            <li key={k.plugin || k.kind} id={`add-${k.plugin || k.kind}`} role="option" aria-selected={i === active} className={i === active ? 'on' : ''}
              onMouseEnter={() => setActive(i)} onMouseDown={e => e.preventDefault()} onClick={() => onPick(k.kind, k.plugin)}>
              <KindIcon kind={k.kind} />
              <span className="add-menu-text"><b>{k.label}</b><span>{k.hint}</span></span>
              {has && <span className="add-menu-has" title="This experiment already has one">added</span>}
            </li>]
        })}
        {!items.length && <li className="add-menu-empty">No section type matches “{q}”</li>}
      </ul>
      <div className="add-menu-foot">↑↓ to choose · Enter to add · Esc to close</div>
    </div>
  )
}

/** A trigger plus its menu. `variant` picks the look: the toolbar button, the between-sections line, or the end tile. */
export function AddSection({ existing, onAdd, variant, label = 'Add section' }: {
  existing: EntryKind[]; onAdd: (k: EntryKind, plugin?: string) => Promise<void> | void; variant: 'button' | 'between' | 'end'; label?: string
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const btn = useRef<HTMLButtonElement>(null)
  const pick = async (k: EntryKind, plugin?: string) => {
    setOpen(false); setBusy(true)
    try { await onAdd(k, plugin) } finally { setBusy(false) }
  }
  const trigger = variant === 'between'
    ? <button ref={btn} className="insert-btn" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(o => !o)} disabled={busy}>
        <span className="plus">+</span><span className="insert-label">{busy ? 'Adding…' : label}</span></button>
    : variant === 'end'
      ? <button ref={btn} className="add-tile" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(o => !o)} disabled={busy}>
          <span className="plus">+</span>{busy ? 'Adding…' : label}
          <span className="add-tile-kinds">{SECTION_KINDS.slice(0, 6).map(k => <KindIcon key={k.kind} kind={k.kind} />)}</span></button>
      : <button ref={btn} className="btn primary sm add-btn" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(o => !o)} disabled={busy}>
          <span className="plus">+</span>{busy ? 'Adding…' : label}<span className="caret" aria-hidden="true">▾</span></button>
  return (
    <div className={`add-wrap add-${variant}${open ? ' open' : ''}`}>
      {variant === 'between' && <span className="insert-line" aria-hidden="true" />}
      {trigger}
      {open && btn.current && <Menu anchor={btn.current} existing={existing} onPick={pick} onClose={() => setOpen(false)}
        align={variant === 'button' ? 'end' : variant === 'between' ? 'center' : 'start'} />}
    </div>
  )
}
