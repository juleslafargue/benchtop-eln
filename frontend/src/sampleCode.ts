// Sample codes typed in the ⌘K box (see backend/app/samples.py for the codes themselves).

// hyphens as other programs write them (‐ ‑ ‒ – — ― − ﹣ －): the same as -
const DASH = /[\u2010-\u2015\u2212\ufe63\uff0d]/g

/** "jl-13", "JL_013", "JL 013" or "JL–013" written as a sample code is: JL-013. Anything else: null. */
export function sampleCode(q: string): string | null {
  const m = /^([a-z][a-z0-9]{1,5})[-_ ](\d{1,6})$/i.exec(q.trim().replace(DASH, '-'))
  return m ? `${m[1].toUpperCase()}-${m[2].replace(/^0+(?=\d)/, '').padStart(3, '0')}` : null
}

/** Could be a sample code, written any of those ways or with nothing between ("JL013"): worth asking Benchtop. */
export const looksLikeCode = (q: string) => /^[a-z][a-z0-9]{1,5}[-_ ]?\d{1,6}$/i.test(q.trim().replace(DASH, '-'))
