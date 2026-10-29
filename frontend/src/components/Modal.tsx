import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

// While any dialog is open the page behind it must not scroll (a wheel or trackpad gesture inside the
// structure editor used to move the experiment page underneath). Counted, so nested dialogs work.
let locks = 0
let savedY = 0
function lockPage() {
  if (locks++ === 0) {
    // pin the page where it is (overflow:hidden alone jumps back to the top in some browsers)
    savedY = window.scrollY
    const w = window.innerWidth - document.documentElement.clientWidth // keep the layout from jumping when the scrollbar goes
    const b = document.body.style
    document.documentElement.style.setProperty('--scrollbar-gap', `${w}px`)
    b.position = 'fixed'; b.top = `-${savedY}px`; b.left = '0'; b.right = '0'
    document.documentElement.classList.add('modal-open')
  }
  return () => {
    if (--locks === 0) {
      const b = document.body.style
      b.position = ''; b.top = ''; b.left = ''; b.right = ''
      document.documentElement.classList.remove('modal-open')
      window.scrollTo(0, savedY)
    }
  }
}

/** A dialog. Keyboard focus: it goes to the field marked autoFocus (the sign dialog's password), else the first
 *  thing that takes focus; when the dialog closes it goes back to whatever had it before (the button that opened
 *  it), instead of the top of the page. */
export default function Modal({ title, children, footer, onClose, wide }: {
  title: ReactNode; children: ReactNode; footer?: ReactNode; onClose: () => void; wide?: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  // what had the focus as the dialog opened (read while drawing it, before anything inside could take the focus)
  const [opener] = useState(() => (typeof document === 'undefined' ? null : document.activeElement))
  useEffect(lockPage, [])
  // The dialog is first drawn open (not yet modal), so a field marked autoFocus can take the focus as it appears;
  // making it modal then moves the focus to its first field or button, so the autoFocus one is given it back.
  useLayoutEffect(() => {
    const d = ref.current!
    const wanted = document.activeElement
    if (d.open) d.close()
    d.showModal()
    if (wanted instanceof HTMLElement && wanted !== d && d.contains(wanted)) wanted.focus()
    return () => {
      if (d.open) d.close()  // while it's open, everything outside it is inert and can't take the focus
      if (opener instanceof HTMLElement && opener !== document.body && opener.isConnected) {
        try { opener.focus({ preventScroll: true }) } catch { /* gone */ }
      }
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const d = ref.current!
    const cancel = (e: Event) => { e.preventDefault(); onClose() }
    d.addEventListener('cancel', cancel)
    return () => d.removeEventListener('cancel', cancel)
  }, [onClose])
  return (
    <dialog ref={ref} open className={`modal${wide ? ' wide' : ''}`} aria-labelledby={titleId}>
      <div className="modal-head"><h2 id={titleId} style={{ fontSize: 19 }}>{title}</h2></div>
      <div className="modal-body">{children}</div>
      {footer && <div className="modal-foot">{footer}</div>}
    </dialog>
  )
}
