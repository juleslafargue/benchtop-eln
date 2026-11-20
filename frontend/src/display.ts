// How a few things are shown (0.35): display only. Nothing here changes what's stored or what a signature covers.
import { displayName, fmt, unitLabel, when, type Signature } from './api'

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

/** An inventory amount for reading: in its lot's unit, or a smaller one when it would read as 0 there (0.4 mg taken from
 *  a 1 kg bottle is "−0.4 mg", not "0 kg"). What's stored is unchanged. */
const SMALLER: Record<string, [string, number][]> = { kg: [['g', 1e3], ['mg', 1e6]], g: [['mg', 1e3]], L: [['mL', 1e3], ['uL', 1e6]], mL: [['uL', 1e3]] }
export function amountText(v: number | null | undefined, unit: string): string {
  if (v == null || !Number.isFinite(v)) return ''
  if (v === 0 || Math.abs(v) >= 0.001) return `${fmt(v, 4)} ${unitLabel(unit)}`
  for (const [u, f] of SMALLER[unit] || []) if (Math.abs(v * f) >= 0.1 || u === SMALLER[unit][SMALLER[unit].length - 1][0]) return `${fmt(v * f, 4)} ${unitLabel(u)}`
  return `${Number(v.toPrecision(3))} ${unitLabel(unit)}`
}
/** A lot's balance in the usage list: to 4 decimals, or as many significant figures as it takes to show it isn't the
 *  round number next to it (1 kg less 0.4 mg is 0.9999996, not "1"). */
export const balanceText = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(v) ? '' : Number(v.toFixed(4)) === v ? String(v) : String(Number(v.toPrecision(10)))
