import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Modal from './Modal'

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)
const M = isMac ? '⌘' : 'Ctrl'

const GROUPS: [string, [string, string][]][] = [
  ['Anywhere', [[`${M} K`, 'Find or do anything'], ['N', 'New experiment'], ['?', 'This list'], ['Esc', 'Close a dialog or menu']]],
  ['On an experiment', [['L', 'Log a note to the bench log (with the time)'], ['T', 'Start a timer'], [`${M} S`, 'Save now (sections also save by themselves)'],
    ['P', 'Open on your phone'], ['30 min stir', 'Typed in the bench log: starts a timer']]],
  ['In a text section', [[`${M} B / I / U`, 'Bold, italic, underline'], [`${M} ,`, 'Subscript'], [`${M} .`, 'Superscript'], [`${M} ${isMac ? '⇧' : 'Shift'} H`, 'Highlight (yellow; other colours from the ▾ next to it)'],
    [`${M} ${isMac ? '⇧' : 'Shift'} > / <`, 'Larger or smaller text'],
    ['[ ] then space', 'Start a checklist'], ['EXP-00025', 'Typed anywhere: becomes a link with a preview'], [`${M} V`, 'Paste a picture (screenshot, photo)'],
    ['Click a picture', 'Place it, size it, or wrap text around it; drag a corner to resize'], ['Double-click a picture', 'See it full size']]],
  ['In the spectrum viewer', [['Z / I / R', 'Zoom, integrate or reference mode'], ['F', 'Fit to the window'], ['Double-click', 'Reset the zoom']]],
]

/** "?" anywhere (when not typing) shows every keyboard shortcut. */
export function Shortcuts() {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      const typing = /INPUT|TEXTAREA|SELECT/.test(el.tagName) || el.isContentEditable
      if (e.key === '?' && !typing && !e.metaKey && !e.ctrlKey && !document.querySelector('dialog[open]')) { e.preventDefault(); setOpen(true) }
    }
    const show = () => setOpen(true)
    window.addEventListener('keydown', on)
    window.addEventListener('benchtop:shortcuts', show)
    return () => { window.removeEventListener('keydown', on); window.removeEventListener('benchtop:shortcuts', show) }
  }, [])
  if (!open) return null
  return (
    <Modal wide title="Keyboard shortcuts" onClose={() => setOpen(false)} footer={<button className="btn" onClick={() => setOpen(false)}>Close</button>}>
      <div className="shortcut-grid">
        {GROUPS.map(([g, items]) => <section key={g}>
          <h3>{g}</h3>
          <dl>{items.map(([k, v]) => <div key={k}><dt>{k.split(' / ').map((p, i) => <span key={i}>{i > 0 && ' / '}<kbd>{p}</kbd></span>)}</dt><dd>{v}</dd></div>)}</dl>
        </section>)}
      </div>
      <p className="faint shortcuts-guide">New to Benchtop? <Link to="/guide" onClick={() => setOpen(false)}>Getting started</Link> explains the everyday tasks in five minutes.</p>
    </Modal>
  )
}
