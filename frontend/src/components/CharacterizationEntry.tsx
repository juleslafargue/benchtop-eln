import { useState } from 'react'
import { api, type Attachment } from '../api'
import { hydrogensInFormula } from '../spectra'
import { canViewSpectrum } from '../viewable'
import SpectrumViewer, { type SavedAnalysis } from './SpectrumViewer'
import HrmsHelper from './HrmsHelper'
import type { EntryEditorProps } from './EntryCard'

interface Item { technique: string; conditions: string; data: string; attachment_id: number | null; analysis?: SavedAnalysis | null }

const TECHNIQUES: [string, string][] = [
  ['1H NMR', '400 MHz, CDCl3'], ['13C NMR', '101 MHz, CDCl3'], ['19F NMR', '376 MHz, CDCl3'], ['HRMS', 'ESI+'],
  ['IR', 'ATR'], ['mp', ''], ['Rf', ''], ['HPLC purity', ''], ['[α]D', 'c 1.0, CHCl3'], ['UV-vis', ''], ['Elemental analysis', ''],
]
const PLACEHOLDER: Record<string, string> = {
  '1H NMR': 'δ 7.54 (d, J = 8.8 Hz, 2H), 3.86 (s, 3H)…', '13C NMR': 'δ 159.2, 140.9, 133.8…',
  HRMS: 'm/z [M+H]+ calcd for C13H13O 185.0961, found 185.0959', IR: 'ν 2955, 1605, 1520, 1245 cm⁻¹',
  mp: '86–88 °C', Rf: '0.45 (10% EtOAc/hexanes)',
}

export default function CharacterizationEntry({ content, onChange, readOnly, experimentId, attachments, onAttachmentsChanged, context }: EntryEditorProps) {
  const items: Item[] = content.items || []
  // a file just dropped onto the page opens straight away
  const [open, setOpen] = useState<number | null>(() => {
    const i = (content.items || []).findIndex((it: Item) => it.attachment_id && it.attachment_id === context.openAttachmentId)
    return i >= 0 ? i : null
  })
  const [uploading, setUploading] = useState<number | null>(null)
  const [uploadErr, setUploadErr] = useState('')
  const set = (next: Item[]) => onChange({ ...content, items: next })
  const patch = (i: number, p: Partial<Item>) => set(items.map((it, j) => (j === i ? { ...it, ...p } : it)))
  const add = (t: string, c: string) => set([...items, { technique: t, conditions: c, data: '', attachment_id: null }])

  const upload = async (i: number, f: File | undefined) => {
    if (!f) return
    setUploading(i); setUploadErr('')
    const fd = new FormData()
    fd.append('file', f)
    try {
      const a = await api.post<Attachment>(`/api/experiments/${experimentId}/attachments`, fd)
      onAttachmentsChanged()
      patch(i, { attachment_id: a.id })
      if (canViewSpectrum(a)) setOpen(i)
    } catch (e: any) { setUploadErr(`${f.name}: ${e.message}`) } finally { setUploading(null) }
  }

  const rowsFor = (t: string) => Math.min(8, Math.max(2, t.split('\n').reduce((n, l) => n + Math.ceil((l.length || 1) / 80), 0)))

  return (
    <div className="char-list">
      {items.map((it, i) => {
        const att = attachments.find(a => a.id === it.attachment_id)
        const viewable = att && canViewSpectrum(att)
        const defaultCond = TECHNIQUES.find(t => t[0] === it.technique)?.[1]
        return (
          <div key={i} className={`char-card${open === i ? ' open' : ''}`}>
            <div className="char-head-box"><div className="char-head">
              <label className="fld"><span className="fld-cap">Technique</span>
                <input className="input char-tech" list="char-techniques" disabled={readOnly} value={it.technique}
                  onChange={e => patch(i, { technique: e.target.value })} /></label>
              <label className="fld"><span className="fld-cap">Instrument & solvent</span>
                <input className="input" placeholder={readOnly ? undefined : defaultCond || 'optional'} disabled={readOnly} value={it.conditions}
                  onChange={e => patch(i, { conditions: e.target.value })} /></label>
              <div className="fld char-file-wrap"><span className="fld-cap">Data file</span>
                <div className="char-file">
                  {readOnly ? <span className="faint char-fname">{att ? att.filename : 'No file'}</span> : <>
                    <select className="select" aria-label="Linked file" value={it.attachment_id ?? ''} title={att?.filename}
                      onChange={e => patch(i, { attachment_id: e.target.value ? Number(e.target.value) : null })}>
                      <option value="">No file linked</option>
                      {attachments.map(a => <option key={a.id} value={a.id}>{a.filename}</option>)}
                    </select>
                    <label className="btn sm" title="Upload a spectrum or data file">
                      {uploading === i ? 'Uploading…' : 'Upload'}
                      <input type="file" hidden onChange={e => upload(i, e.target.files?.[0])} />
                    </label>
                  </>}
                  {viewable && <button className={`btn sm${open === i ? ' primary' : ''}`} onClick={() => setOpen(open === i ? null : i)}
                    title={open === i ? 'Close the spectrum viewer' : 'Open the spectrum viewer'}>
                    {open === i ? 'Hide spectrum' : 'View spectrum'}</button>}
                </div>
              </div>
              {!readOnly && <button className="btn ghost sm char-remove" aria-label={`Remove ${it.technique}`} title={`Remove this ${it.technique || 'item'}`}
                onClick={() => set(items.filter((_, j) => j !== i))}>×</button>}
            </div></div>
            <label className="fld"><span className="fld-cap" title="The line that goes into your write-up. From a spectrum, click 'Use as characterisation data' to fill it.">Reported data</span>
              <textarea className="input char-data" rows={rowsFor(it.data)}
                placeholder={readOnly ? undefined : PLACEHOLDER[it.technique] || 'Result as it should appear in the write-up'} disabled={readOnly} value={it.data}
                onChange={e => patch(i, { data: e.target.value })} /></label>
            {/HRMS|^MS\b|ESI|APCI/i.test(it.technique) && context.productSmiles && !readOnly && (
              <HrmsHelper smiles={context.productSmiles} current={it.data} onUse={d => patch(i, { data: d, conditions: it.conditions || 'ESI' })} />
            )}
            {open === i && att && (
              <SpectrumViewer attachmentId={att.id} filename={att.filename} saved={it.analysis}
                expectedH={/^1H/i.test(it.technique) ? hydrogensInFormula(context.productFormula) : null}
                productSmiles={context.productSmiles} productMW={context.productMW}
                onSave={readOnly ? undefined : a => patch(i, { analysis: a })}
                onInsert={readOnly ? undefined : text => patch(i, { data: text.replace(/^.*?(?=δ )|^[^:]+:\s*/, '') })} />
            )}
          </div>
        )
      })}
      {uploadErr && <p className="error" role="alert" style={{ margin: 0 }}>Upload failed: {uploadErr}</p>}
      <datalist id="char-techniques">{TECHNIQUES.map(([t]) => <option key={t} value={t} />)}</datalist>
      {!readOnly && (
        <div className="chips char-add">
          <span className="faint" style={{ marginRight: 4 }}>Add a measurement:</span>
          {TECHNIQUES.map(([t, c]) => <button key={t} className="chip" onClick={() => add(t, c)}>{t}</button>)}
        </div>
      )}
    </div>
  )
}
