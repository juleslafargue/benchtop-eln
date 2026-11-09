// How a few things are shown (0.35): display only. Nothing here changes what's stored or what a signature covers.
import { displayName, when, type Signature } from './api'

const SIG_VERB: Record<string, string> = { author: 'Signed', witness: 'Witnessed', rejected: 'Returned to the author', withdrawn: 'Signature withdrawn', void: 'Voided' }

/** A signature in one plain line: "Signed by Sam Patel, 9 Nov 2026, 14:42 · time certified". "time certified" only when an
 *  independent timestamp service certified it; the fingerprint and the service's name are shown under Details. */
export function sigLine(s: Pick<Signature, 'meaning' | 'signer_name' | 'user' | 'signed_at' | 'timestamp'>): string {
  const verb = SIG_VERB[s.meaning] || s.meaning
  const certified = s.timestamp?.status === 'ok' ? ' · time certified' : ''
  return `${verb} by ${s.signer_name || (s.user ? displayName(s.user) : 'someone')}, ${when(s.signed_at)}${certified}`
}

/** A calculated amount to 4 significant figures (whole numbers stay whole), for reading at a glance. What's kept is
 *  unchanged; `fullValue` gives it in full for the tooltip. */
export const sig4 = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(v) ? '' : Math.abs(v) >= 1000 ? String(Math.round(v)) : String(Number(v.toPrecision(4)))
export const fullValue = (v: number | null | undefined, unit: string) => v == null ? undefined : `${v} ${unit}`
