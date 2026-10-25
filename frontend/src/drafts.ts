/**
 * Unsaved typing, kept in this browser until the server has it (0.27).
 *
 * A section saves itself a moment after each change. If the server can't be reached (Benchtop stopped, the Mac
 * asleep, Wi-Fi gone, the sign-in expired), the section says "Not saved" and warns before the tab closes, but before
 * 0.27 the typing lived only in that tab: closing it anyway, or a browser crash, lost it.
 *
 * Now, while a section has changes the server hasn't confirmed, a copy is kept here (localStorage), per person and
 * per section, and removed as soon as the save is confirmed. When the record is opened again, the section offers to
 * put it back. Nothing is put back without asking. Copies older than KEEP_DAYS are cleared.
 */

export interface Draft {
  entryId: number
  experimentId: number
  /** the section's version these changes were made on */
  baseVersion: number
  title: string
  content: unknown
  /** when the copy was last updated (ms since 1970) */
  at: number
  /** the browser tab that made it (sessionStorage survives a reload of the same tab) */
  tab: string
}

export const KEEP_DAYS = 30
const PREFIX = 'benchtop.draft.'

/** Where the copies go: localStorage in the app, a stand-in in the tests. */
export interface Store {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
  readonly length: number
  key(i: number): string | null
}

export const draftKey = (userId: number, entryId: number) => `${PREFIX}${userId}.${entryId}`

export function loadDraft(store: Store | null, userId: number, entryId: number): Draft | null {
  if (!store) return null
  try {
    const raw = store.getItem(draftKey(userId, entryId))
    if (!raw) return null
    const d = JSON.parse(raw)
    return d && typeof d === 'object' && d.entryId === entryId ? d as Draft : null
  } catch {
    return null
  }
}

/** Keep a copy. If the browser's storage is full, the oldest copies make room; returns whether it was kept. */
export function saveDraft(store: Store | null, userId: number, d: Draft): boolean {
  if (!store) return false
  const value = JSON.stringify(d)
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      store.setItem(draftKey(userId, d.entryId), value)
      return true
    } catch {
      const others = allDrafts(store).filter(o => o.key !== draftKey(userId, d.entryId))
      if (!others.length) return false
      others.sort((a, b) => a.at - b.at)
      store.removeItem(others[0].key)
    }
  }
  return false
}

export function clearDraft(store: Store | null, userId: number, entryId: number): void {
  try { store?.removeItem(draftKey(userId, entryId)) } catch { /* storage blocked: nothing was kept anyway */ }
}

function allDrafts(store: Store): { key: string; at: number }[] {
  const out: { key: string; at: number }[] = []
  for (let i = 0; i < store.length; i++) {
    const k = store.key(i)
    if (!k || !k.startsWith(PREFIX)) continue
    let at = 0
    try { at = Number(JSON.parse(store.getItem(k) || '{}').at) || 0 } catch { /* unreadable: oldest */ }
    out.push({ key: k, at })
  }
  return out
}

/** Clear copies older than KEEP_DAYS (a section deleted since, or changes someone chose not to put back). */
export function sweepDrafts(store: Store | null, now: number = Date.now()): number {
  if (!store) return 0
  let n = 0
  try {
    for (const d of allDrafts(store)) {
      if (now - d.at > KEEP_DAYS * 86400e3) { store.removeItem(d.key); n++ }
    }
  } catch { /* storage blocked */ }
  return n
}

/** What the offer says about the server's copy: the same version the changes were made on, or a newer one. */
export function draftSituation(d: Draft, serverVersion: number, readOnly: boolean): 'readonly' | 'changed' | 'same' {
  if (readOnly) return 'readonly'
  return serverVersion > d.baseVersion ? 'changed' : 'same'
}

/** The kept changes as plain text, for copying when they can't be put back (the record was signed since). */
export function draftText(d: Draft): string {
  const c = d.content as any
  const body = typeof c?.html === 'string'
    ? c.html.replace(/<br\s*\/?>/gi, '\n').replace(/<\/(p|h[1-6]|li|div|tr)>/gi, '\n').replace(/<[^>]+>/g, '')
      .replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&')
      .replace(/\n{3,}/g, '\n\n').trim()
    : JSON.stringify(c, null, 2)
  return `${d.title}\n\n${body}`
}

/** The browser's storage, or null where it's blocked (some private windows). */
export function browserStore(): Store | null {
  try {
    const s = window.localStorage
    const k = `${PREFIX}probe`
    s.setItem(k, '1')
    s.removeItem(k)
    return s
  } catch {
    return null
  }
}

/** This tab's name, kept through a reload of the same tab. */
export function tabId(): string {
  try {
    let id = sessionStorage.getItem('benchtop.tab')
    if (!id) { id = Math.random().toString(36).slice(2, 10); sessionStorage.setItem('benchtop.tab', id) }
    return id
  } catch {
    return 'tab'
  }
}
