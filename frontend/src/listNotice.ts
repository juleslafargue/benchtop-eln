// Lists that come in pages say so when they're cut short (0.31). Before, the Materials page stopped at 500, Search at
// 100 of each kind and the Inbox at 200, without a word.

const n = (x: number) => x.toLocaleString('en-CA')

/** "Showing the first 500 of 812 materials." when a list is cut short; '' when nothing is left out. */
export function cutShortText(shown: number, total: number, things: string): string {
  return total > shown ? `Showing the first ${n(shown)} of ${n(total)} ${things}.` : ''
}

/** The next page added to the rows already shown, none twice: a row added in between moves the others along a page. */
export function addPage<T extends { id: number }>(shown: T[], page: T[]): T[] {
  const have = new Set(shown.map(r => r.id))
  return [...shown, ...page.filter(r => !have.has(r.id))]
}
