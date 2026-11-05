import { cutShortText } from '../listNotice'

/** Under a list that came in pages: how many of how many are shown, and Show more (0.31). Nothing when all are shown. */
export default function CutShort({ shown, total, things, busy, onMore }: {
  shown: number; total: number; things: string; busy: boolean; onMore: () => void
}) {
  const text = cutShortText(shown, total, things)
  if (!text) return null
  return (
    <div className="notice cut-short">
      <span role="status">{text}</span>
      <button type="button" className="btn sm" disabled={busy} onClick={onMore}>{busy ? 'Loading…' : 'Show more'}</button>
    </div>
  )
}
