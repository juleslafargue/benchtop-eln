// A USB or Bluetooth barcode scanner "types" the code, a character every few milliseconds, then presses Enter (or Tab).
// 0.36.6: a scan while no box had the focus went to the page's one-key shortcuts instead: scanning PTL-0042 on a record
// opened the phone QR code (P), then the timer (T) and the bench log (L). Now a burst of keys that fast, with no box
// focused, is taken as a scan (and looked up); a key pressed by a person still does what it always did, about 60 ms
// later. Nothing here touches the page, so node's tests run it as the app does (tests/v0366-scanner.test.mjs).

/** Keys closer together than this are a scanner, not a person (people type 80–250 ms apart; scanners 1–20 ms). */
export const SCANNER_GAP_MS = 35
/** How long a key waits to see whether more follow at scanner speed. */
export const HOLD_MS = 60
/** The shortest code taken as a scan: shorter bursts are given back as the keys they were. */
export const MIN_CODE = 4
/** A burst this long with no Enter or Tab after it is a scanner set up to send nothing after the code. */
export const MIN_CODE_WITHOUT_ENTER = 6

export interface Key { key: string; time: number }
export type Outcome = { kind: 'wait' } | { kind: 'scan'; code: string } | { kind: 'keys'; keys: Key[] }

/** The decisions, without timers or events: feed it each key (outside a text box) and call `flush` when HOLD_MS has
 *  passed with no new key. */
export class ScanBurst {
  private held: Key[] = []

  get holding(): boolean { return this.held.length > 0 }

  /** A key pressed while no box has the focus: "wait" (held back for now), "scan", or "keys" to give back now. */
  key(k: Key): Outcome {
    const last = this.held[this.held.length - 1]
    const fast = last !== undefined && k.time - last.time <= SCANNER_GAP_MS
    if (k.key === 'Enter' || k.key === 'Tab') {
      if (last && k.time - last.time <= HOLD_MS && this.held.length >= MIN_CODE && this.burst()) return this.take()
      return { kind: 'keys', keys: [...this.drop(), k] }
    }
    if (k.key.length !== 1) return { kind: 'keys', keys: [...this.drop(), k] }  // arrows, Escape…: never held
    if (last && !fast) {
      const before = this.drop()
      this.held = [k]
      return before.length ? { kind: 'keys', keys: before } : { kind: 'wait' }
    }
    this.held.push(k)
    return { kind: 'wait' }
  }

  /** HOLD_MS passed with no new key. */
  flush(): Outcome {
    if (this.held.length >= MIN_CODE_WITHOUT_ENTER && this.burst()) return this.take()
    const keys = this.drop()
    return keys.length ? { kind: 'keys', keys } : { kind: 'wait' }
  }

  private burst(): boolean {
    return this.held.every((k, i) => i === 0 || k.time - this.held[i - 1].time <= SCANNER_GAP_MS)
  }

  private take(): Outcome {
    const code = this.held.map(k => k.key).join('').trim()
    this.held = []
    return { kind: 'scan', code }
  }

  private drop(): Key[] {
    const keys = this.held
    this.held = []
    return keys
  }
}

/** For a box a scanner types into (the Scan dialog): did this Enter or Tab come at scanner speed after the code, so
 *  it ends the scan (Tab included, rather than moving the focus to the next button)? */
export function endsScan(key: string, gapMs: number, typedLength: number): boolean {
  return (key === 'Enter' || key === 'Tab') && typedLength >= MIN_CODE && gapMs <= HOLD_MS
}
