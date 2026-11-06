// Barcode scanning (0.32): reading a code from a camera frame or a photo, and what a scanned bottle means for a reaction.
// Nothing here touches the page, so node's tests run it as the app does (tests/scan.test.mjs). The decoder itself,
// zxing-wasm, is loaded only when a scanner opens (scanReader.ts).
import { day, fmt, type Lot, type StoichRow } from './api'

/** The barcode types read: everything on a chemical's bottle or a lab label (Code 128 and 39, EAN/UPC, ITF, GS1
 *  DataBar, QR, Data Matrix, PDF417). Fewer types read faster and misread less. */
export const READ_FORMATS = ['Code128', 'Code39', 'Code93', 'Codabar', 'EAN13', 'EAN8', 'UPCA', 'UPCE', 'ITF', 'DataBar',
  'DataBarExp', 'QRCode', 'MicroQRCode', 'DataMatrix', 'PDF417'] as const

export interface Decoded { text: string; format: string }
/** zxing-wasm's readBarcodes, or anything that answers the same way. */
export type Reader = (input: Blob | ImageData, options: Record<string, unknown>) => Promise<{ text: string; format: string; isValid?: boolean }[]>

// a scanner sends Enter (or a tab) after the code; a photo's reading can end in a control character
const EDGES = /^[\s\x00-\x1f\x7f]+|[\s\x00-\x1f\x7f]+$/g

/** A code without the spaces and control characters around it. Inside it nothing changes: a GS1 code's group
 *  separators stay. The server cleans codes the same way (clean_code in routers/materials.py). */
export function cleanCode(raw: string | null | undefined): string {
  return (raw || '').replace(EDGES, '').slice(0, 200)
}

/** The codes in one image, each once. A camera frame stops at the first code; a photo may hold several. */
export async function readCodes(read: Reader, image: Blob | ImageData, still: boolean): Promise<Decoded[]> {
  const found = await read(image, {
    formats: [...READ_FORMATS], tryHarder: true, tryRotate: true, tryInvert: true, tryDownscale: true,
    maxNumberOfSymbols: still ? 8 : 1,
  })
  const out: Decoded[] = []
  for (const r of found) {
    const text = cleanCode(r.text)
    if (text && r.isValid !== false && !out.some(o => o.text === text)) out.push({ text, format: r.format })
  }
  return out
}

/** The live camera works only on a secure page (https, or this computer); on plain http a photo is taken instead. */
export function canUseLiveCamera(w: { isSecureContext?: boolean; navigator?: { mediaDevices?: { getUserMedia?: unknown } } }): boolean {
  return !!w.isSecureContext && typeof w.navigator?.mediaDevices?.getUserMedia === 'function'
}

// ------------------------------------------------------------------ a scanned lot

export interface ScannedLot extends Lot {
  material: { id: number; mid: string; name: string; kind: string; formula: string; smiles: string; mw: number | null; density: number | null; cas: string }
  safety: { status: string; signal?: string; pictograms?: string[]; flags?: string[]; change_unacknowledged?: boolean } | null
}
export interface ScanRecord { kind: 'experiment' | 'material' | 'sample'; id: number; code: string; title: string; experiment?: string }
export interface ScanAnswer { code: string; lots: ScannedLot[]; record: ScanRecord | null }

/** Why this bottle shouldn't be used, said plainly; null when nothing is wrong with it. */
export function lotProblem(lot: Pick<Lot, 'status' | 'expires_at'>, now: Date = new Date()): string | null {
  if (lot.status === 'empty') return 'This bottle is marked empty.'
  if (lot.status === 'disposed') return 'This bottle was disposed of.'
  if (lot.status === 'quarantined') return "This bottle is quarantined: don't use it until someone clears it."
  if (lot.expires_at && new Date(lot.expires_at).getTime() < now.getTime()) return `This bottle expired on ${day(lot.expires_at)}.`
  return null
}

// as the bench view's reaction table writes them
export const mass = (mg: number) => (mg >= 1000 ? `${fmt(mg / 1000, 2)} g` : `${fmt(mg, 1)} mg`)
export const vol = (ml: number) => (ml >= 1000 ? `${fmt(ml / 1000, 2)} L` : `${fmt(ml, ml < 1 ? 3 : 1)} mL`)

/** What to weigh or measure for a row, as the bench view's table shows it: "weigh 205 mg", "measure 1.2 mL", or "".
 *  A liquid (a density), a solvent or a solution is measured by volume; a solid is weighed. */
export function toMeasure(r: Pick<StoichRow, 'role' | 'mass_mg' | 'volume_ml' | 'density' | 'conc_m'>): string {
  if (r.volume_ml && (r.density || r.role === 'solvent' || r.conc_m || !r.mass_mg)) return `measure ${vol(r.volume_ml)}`
  if (r.mass_mg) return `weigh ${mass(r.mass_mg)}`
  return ''
}

/** One line for a row: "4-Bromotoluene, reagent, 1.20 eq: weigh 205 mg". */
export function rowLine(r: StoichRow): string {
  const name = r.name?.trim() || r.formula?.trim() || r.role
  const eq = r.equiv != null && r.role !== 'solvent' && r.role !== 'product' ? `, ${fmt(r.equiv, 2)} eq` : ''
  const what = r.role === 'product' ? '' : toMeasure(r)
  return `${name}, ${r.role}${eq}${what ? `: ${what}` : ''}`
}

/** The rows of a reaction table this bottle is for (its material), with their places in the table. */
export function rowsForLot(rows: StoichRow[], lot: Pick<Lot, 'material_id'>): { row: StoichRow; index: number }[] {
  return rows.map((row, index) => ({ row, index })).filter(x => x.row.material_id === lot.material_id)
}

/** The reaction rows with this bottle as row `index`'s lot, and its purity, as the reaction table does when a lot is
 *  chosen. The server works the amounts out again when the section is saved. null when the row can't take it: it isn't
 *  this bottle's material any more, or its amount was already taken from another bottle (undo that first). */
export function withBottle(rows: StoichRow[], index: number, lot: Pick<Lot, 'id' | 'material_id' | 'purity'>): StoichRow[] | null {
  const r = rows[index]
  if (!r || r.material_id !== lot.material_id || (r as { deducted?: unknown }).deducted) return null
  return rows.map((x, i) => (i === index ? { ...x, lot_id: lot.id, purity: lot.purity ?? 100 } : x))
}
