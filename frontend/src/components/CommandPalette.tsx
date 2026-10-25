import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useApp } from '../App'
import { api, type ExperimentSummary, type Material } from '../api'

interface Item { key: string; group: string; label: string; hint?: string; run: () => void }

/** Subsequence match with a bonus for word starts and contiguous runs; 0 means no match. */
function score(q: string, text: string) {
  if (!q) return 1
  const t = text.toLowerCase(), s = q.toLowerCase()
  if (t.includes(s)) return 100 + (t.startsWith(s) ? 50 : 0) - t.indexOf(s)
  let ti = 0, sc = 0, run = 0
  for (const ch of s) {
    const i = t.indexOf(ch, ti)
    if (i < 0) return 0
    run = i === ti ? run + 1 : 0
    sc += 1 + run + (i === 0 || /[\s\-(]/.test(t[i - 1]) ? 3 : 0)
    ti = i + 1
  }
  return sc
}

export default function CommandPalette({ onClose, onNewExperiment, onNewNotebook }: {
  onClose: () => void; onNewExperiment: () => void; onNewNotebook: () => void
}) {
  const nav = useNavigate()
  const { notebooks, user, logout, canWrite } = useApp()
  const [q, setQ] = useState('')
  const [exps, setExps] = useState<ExperimentSummary[]>([])
  const [mats, setMats] = useState<Material[]>([])
  const [sel, setSel] = useState(0)
  const list = useRef<HTMLUListElement>(null)

  useEffect(() => {
    api.get<ExperimentSummary[]>('/api/experiments?limit=500').then(setExps).catch(() => {})
    api.get<Material[]>('/api/materials?limit=500').then(setMats).catch(() => {})
  }, [])

  const go = (path: string) => () => { onClose(); nav(path) }
  const items: Item[] = useMemo(() => [
    ...(canWrite ? [
      { key: 'a-newexp', group: 'Actions', label: 'New experiment', hint: 'N', run: () => { onClose(); onNewExperiment() } },
      { key: 'a-newnb', group: 'Actions', label: 'New notebook', run: () => { onClose(); onNewNotebook() } },
      { key: 'a-reg', group: 'Actions', label: 'Register a material', run: go('/materials?new=1') },
    ] : []),
    { key: 'p-home', group: 'Go to', label: 'Dashboard', run: go('/') },
    { key: 'p-compounds', group: 'Go to', label: 'Compounds', run: go('/compounds') },
    { key: 'p-rx', group: 'Go to', label: 'Reactions', run: go('/reactions') },
    { key: 'p-search', group: 'Go to', label: 'Structure search', run: go('/search') },
    { key: 'p-mat', group: 'Go to', label: 'Materials', run: go('/materials') },
    { key: 'p-inv', group: 'Go to', label: 'Inventory', run: go('/inventory') },
    { key: 'p-tpl', group: 'Go to', label: 'Templates', run: go('/templates') },
    { key: 'p-acc', group: 'Go to', label: 'Account settings', run: go('/account') },
    ...(user.role === 'admin' ? ([['', 'Users'], ['backups', 'Backups, data check & disk space'], ['instruments', 'Instruments'], ['email', 'Email'],
      ['signing', 'Signing: witnessing & trusted timestamps'], ['integrations', 'Integrations: plugins, webhooks & NMR data'], ['audit', 'Audit trail']] as const)
      .map(([k, label]) => ({ key: `p-admin-${k || 'users'}`, group: 'Go to', label: `Admin → ${label}`, run: go(k ? `/admin/${k}` : '/admin') })) : []),
    { key: 'a-out', group: 'Actions', label: 'Sign out', run: () => { onClose(); logout() } },
    ...notebooks.map(n => ({ key: `n${n.id}`, group: 'Notebooks', label: n.name, hint: `${n.experiment_count} experiments`, run: go(`/notebooks/${n.id}`) })),
    ...exps.map(e => ({ key: `e${e.id}`, group: 'Experiments', label: `${e.eid} ${e.title}`, hint: e.status, run: go(`/experiments/${e.id}`) })),
    ...mats.map(m => ({ key: `m${m.id}`, group: 'Materials', label: `${m.name}${m.cas ? ` (${m.cas})` : ''}`, hint: m.mid, run: go(`/materials/${m.id}`) })),
  ], [exps, mats, notebooks, user.role, canWrite]) // eslint-disable-line react-hooks/exhaustive-deps

  const results = useMemo(() => {
    // every word must match somewhere (any order): "bromo suzuki" finds "Suzuki coupling of 4-bromoanisole"
    const words = q.trim().split(/\s+/).filter(Boolean)
    const scoreAll = (text: string) => words.reduce((acc, w) => { const sc = score(w, text); return acc === 0 || sc === 0 ? 0 : acc + sc }, 1)
    const scored = items.map(it => ({ it, s: scoreAll(`${it.label} ${it.hint || ''}`) })).filter(x => x.s > 0)
    if (q) scored.sort((a, b) => b.s - a.s)
    else return scored.filter(x => x.it.group !== 'Materials').slice(0, 14).map(x => x.it) // recent work + actions when empty
    return scored.slice(0, 30).map(x => x.it)
  }, [items, q])

  useEffect(() => setSel(0), [q])
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [sel])

  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div className="palette" role="dialog" aria-label="Go to or do anything" onMouseDown={e => e.stopPropagation()}>
        <input className="palette-input" autoFocus placeholder="Find an experiment, material or page… (try “suzuki” or a CAS number)"
          value={q} onChange={e => setQ(e.target.value)} aria-controls="palette-list"
          onKeyDown={e => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel(i => Math.min(results.length - 1, i + 1)) }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setSel(i => Math.max(0, i - 1)) }
            else if (e.key === 'Enter') { e.preventDefault(); results[sel]?.run() }
            else if (e.key === 'Escape') onClose()
          }} />
        <ul className="palette-list" id="palette-list" role="listbox" ref={list}>
          {results.map((it, i) => (
            <li key={it.key} role="option" aria-selected={i === sel} onMouseEnter={() => setSel(i)} onClick={it.run}>
              <span className="palette-group">{it.group}</span>
              <span className="palette-label">{it.label}</span>
              {it.hint && <span className="faint">{it.hint}</span>}
            </li>
          ))}
          {!results.length && <li className="faint" style={{ padding: 14 }}>Nothing matches “{q}”.</li>}
        </ul>
        <div className="palette-foot faint">↑↓ to move · Enter to open · Esc to close</div>
      </div>
    </div>
  )
}
