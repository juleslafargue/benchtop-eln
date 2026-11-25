import { useEffect, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { cleanCode } from '../scan'
import { HOLD_MS, ScanBurst, type Key } from '../scanBurst'

/** The page event for a code a barcode scanner typed while no box had the focus (0.36.6). A page that handles it
 *  (Inventory, the bench view) calls preventDefault(); otherwise the Inventory page opens and looks the code up. */
export const SCANNED = 'benchtop:scanned'

/** Keys at scanner speed, with no text box focused and no dialog open, are a scan, not the page's shortcuts. */
export function useScanCatcher() {
  const nav = useNavigate()
  const navRef = useRef(nav)
  navRef.current = nav
  useEffect(() => {
    const burst = new ScanBurst()
    const ours = new WeakSet<Event>()
    let timer: ReturnType<typeof setTimeout> | undefined
    const typing = (el: EventTarget | null) => el instanceof HTMLElement && (/INPUT|TEXTAREA|SELECT/.test(el.tagName) || el.isContentEditable)
    const replay = (keys: Key[]) => {
      for (const k of keys) {
        const ev = new KeyboardEvent('keydown', { key: k.key, bubbles: true, cancelable: true })
        ours.add(ev)
        ;(document.activeElement && document.activeElement !== document.body ? document.activeElement : document.body).dispatchEvent(ev)
      }
    }
    const scanned = (raw: string) => {
      const code = cleanCode(raw)
      if (!code) return
      const ev = new CustomEvent(SCANNED, { detail: code, cancelable: true })
      window.dispatchEvent(ev)
      if (!ev.defaultPrevented) navRef.current(`/inventory?scan=${encodeURIComponent(code)}`)
    }
    const settle = () => {
      clearTimeout(timer)
      if (burst.holding) timer = setTimeout(() => { const out = burst.flush(); if (out.kind === 'scan') scanned(out.code); else if (out.kind === 'keys') replay(out.keys) }, HOLD_MS)
    }
    const onKey = (e: KeyboardEvent) => {
      if (ours.has(e)) return
      if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing || typing(e.target) || document.querySelector('dialog[open]')) {
        if (burst.holding) { clearTimeout(timer); const out = burst.flush(); if (out.kind === 'keys') replay(out.keys) }
        return
      }
      const out = burst.key({ key: e.key, time: e.timeStamp })
      if (out.kind === 'keys') {
        const last = out.keys[out.keys.length - 1]
        const mine = last && last.key === e.key && last.time === e.timeStamp
        replay(mine ? out.keys.slice(0, -1) : out.keys)
        if (!mine) { e.preventDefault(); e.stopImmediatePropagation() }  // this key starts a new hold
      } else {
        e.preventDefault(); e.stopImmediatePropagation()
        if (out.kind === 'scan') scanned(out.code)
      }
      settle()
    }
    window.addEventListener('keydown', onKey, true)
    return () => { window.removeEventListener('keydown', onKey, true); clearTimeout(timer) }
  }, [])
}

/** For a page that handles scans itself: `onCode` gets each code scanned while no box has the focus. */
export function useScanned(onCode: (code: string) => void) {
  const cb = useRef(onCode)
  cb.current = onCode
  useEffect(() => {
    const on = (e: Event) => { e.preventDefault(); cb.current((e as CustomEvent<string>).detail) }
    window.addEventListener(SCANNED, on)
    return () => window.removeEventListener(SCANNED, on)
  }, [])
}

/** Put once in the signed-in app. */
export default function ScanCatcher() {
  useScanCatcher()
  return null
}
