import { useEffect, useState } from 'react'
import { api } from '../api'
import { NumInput } from './bits'

interface Ion { adduct: string; formula: string; charge: number; mz: number }

/** Calculated m/z for the product's common adducts; type the found value and get the SI line. */
export default function HrmsHelper({ smiles, current, onUse }: { smiles: string; current: string; onUse: (data: string) => void }) {
  const [ions, setIons] = useState<Ion[]>([])
  const [err, setErr] = useState('')
  const [adduct, setAdduct] = useState('[M + H]+')
  const [found, setFound] = useState<number | null>(() => {
    const m = current.match(/found\s+([\d.]+)/i)
    return m ? Number(m[1]) : null
  })
  useEffect(() => {
    api.get<{ ions: Ion[] }>(`/api/chem/hrms?smiles=${encodeURIComponent(smiles)}`).then(r => { setIons(r.ions); setErr('') }).catch(e => setErr(e.message))
  }, [smiles])
  useEffect(() => { const m = current.match(/(\[M[^\]]*\][+−-])/); if (m) setAdduct(m[1].replace('-', '−')) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (err) return <p className="faint" style={{ margin: '4px 0 0' }}>HRMS helper unavailable: {err}</p>
  const ion = ions.find(i => i.adduct === adduct) || ions[0]
  if (!ion) return null
  const ppm = found != null ? (found - ion.mz) / ion.mz * 1e6 : null
  const text = `m/z: ${ion.adduct} calcd for ${ion.formula} ${ion.mz.toFixed(4)}; found ${found != null ? found.toFixed(4) : '—'}`
  return (
    <div className="hrms">
      <select className="select" value={ion.adduct} onChange={e => setAdduct(e.target.value)} aria-label="Ion">
        {ions.map(i => <option key={i.adduct} value={i.adduct}>{i.adduct}</option>)}
      </select>
      <span>calcd for <b>{ion.formula}</b> <b>{ion.mz.toFixed(4)}</b></span>
      <label className="row" style={{ gap: 6 }}>found <NumInput className="input" label="Found m/z" value={found} onCommit={setFound} nd={4} live={0} /></label>
      {/* the room for the ppm is kept while there's none, so Use doesn't move under the mouse as it appears (0.30.1) */}
      <span className={ppm == null ? 'ppm' : Math.abs(ppm) <= 5 ? 'ppm ok' : 'ppm bad'} title={ppm == null ? undefined : 'Most journals accept within ±5 ppm'}>
        {ppm != null && `${ppm >= 0 ? '+' : ''}${ppm.toFixed(1)} ppm`}</span>
      <button className="btn sm primary" disabled={found == null} onClick={() => onUse(text)}>Use</button>
    </div>
  )
}
