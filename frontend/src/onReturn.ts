// Coming back to a record's tab or window (0.36.1). A record's page picks up changes made elsewhere (the bench log, your
// phone, a colleague) when its tab is shown again or its window regains focus; everything on the page that is fetched
// separately (Mentioned in) refreshes at the same moments, through this one function. No imports, so `npm test` can
// check it directly.

/** Call `again` whenever the page is shown again (its tab made visible, its window focused). Returns the clean-up, so
 *  `useEffect(() => onReturn(load), [load])` keeps it for as long as the component is there. */
export function onReturn(again: () => void, doc: Pick<Document, 'addEventListener' | 'removeEventListener' | 'visibilityState'> = document,
  win: Pick<Window, 'addEventListener' | 'removeEventListener'> = window): () => void {
  const shown = () => { if (doc.visibilityState === 'visible') again() }
  doc.addEventListener('visibilitychange', shown)
  win.addEventListener('focus', shown)
  return () => { doc.removeEventListener('visibilitychange', shown); win.removeEventListener('focus', shown) }
}
