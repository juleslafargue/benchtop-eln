// Thin typed client for the Benchtop REST API.
import type { PublicationInfo } from './publish'

export type Role = 'admin' | 'scientist' | 'viewer'
export type Status = 'open' | 'signed' | 'witnessed' | 'voided' | 'locked'
export type EntryKind = 'text' | 'reaction' | 'table' | 'file' | 'purification' | 'characterization' | 'safety' | 'screen' | 'references' | 'custom'

export interface Preferences { theme?: 'system' | 'light' | 'dark'; palette?: string; email_notify?: { witness?: boolean; results?: boolean; system?: boolean }; default_notebook_id?: number | null; spectrum_expanded?: boolean; guide_dismissed?: boolean; pinned?: number[]; nav?: string[]; nav_hidden?: string[]; dashboard?: { widgets: unknown[] } }
export interface User { id: number; username: string; full_name: string; email: string; role: Role; active: boolean; preferences: Preferences; has_password?: boolean; must_change_password?: boolean; sample_prefix?: string | null; orcid?: string | null }
export interface UserBrief { id: number; username: string; full_name: string }
export type Visibility = 'everyone' | 'group' | 'private'
export interface Notebook {
  id: number; name: string; description: string; archived: boolean; owner: UserBrief
  experiment_count: number; status_counts: Record<Status, number>; created_at: string
  visibility: Visibility; group: { id: number; name: string } | null
}
export interface Group {
  id: number; name: string; description: string; created_at: string; notebook_count: number
  members: (UserBrief & { role: 'lead' | 'member' })[]; my_role: 'lead' | 'member' | null; can_manage: boolean
}
export interface Audience { visibility: Visibility; group: { id: number; name: string } | null }
export interface Comment {
  id: number; experiment_id: number; entry_id: number | null; parent_id: number | null; user: UserBrief
  body: string; deleted: boolean; created_at: string; edited_at: string | null
  resolved_at: string | null; resolved_by: UserBrief | null; mine: boolean
  unseen_mentions?: UserBrief[]
}
export interface Notification {
  id: number; kind: string; text: string; actor: UserBrief | null; experiment: { id: number; eid: string; title: string } | null
  comment_id: number | null; entry_id: number | null; created_at: string; read: boolean
}
export interface ExperimentSummary {
  id: number; eid: string; title: string; description: string; status: Status
  notebook_id: number | null; notebook_name: string | null; is_template: boolean
  template_id: number | null; tags: string[]; owner: UserBrief; created_at: string; updated_at: string
  match?: string; snippet?: string; score?: number; smiles?: string
}
export interface Entry {
  id: number; experiment_id: number; kind: EntryKind; title: string; position: number
  content: any; version: number; updated_by: number; updated_at: string
}
export interface Attachment { id: number; filename: string; content_type: string; size: number; sha256: string; created_at: string; from_instrument?: boolean }
export interface TrustedTimestamp { status: 'ok' | 'pending' | 'failed'; tsa: string; gen_time: string | null; delay_seconds: number | null; error: string; attempts: number }
export interface Signature {
  id: number; meaning: string; comment: string; content_hash: string; signed_at: string; user: UserBrief
  signer_name?: string; signer_username?: string | null
  matches_current?: boolean
  timestamp?: TrustedTimestamp | null
}
export interface WitnessRequest {
  id: number; witness: UserBrief; requested_by: UserBrief; asked_at: string; due_at: string | null; overdue: boolean
  reminded_at: string | null; can_remind_now: boolean; email: boolean
}
export interface WitnessCandidate extends UserBrief { has_email: boolean }
export interface Experiment extends ExperimentSummary {
  entries: Entry[]; attachments: Attachment[]; signatures: Signature[]; witness_request?: WitnessRequest | null
  can_edit: boolean; shared_with_me: 'view' | 'edit' | null; audience: Audience; open_comments: number
  follows?: LinkedExperiment | { hidden: true } | null; follow_ups?: LinkedExperiment[]
  samples?: SampleCode[]
  origin?: ElnOrigin | null
  publication?: PublicationInfo | null  // on Zenodo (0.28): a draft, or the DOI once published
}
/** Where an imported record came from (0.20), shown as a banner on it. */
export interface ElnOrigin {
  source: string; source_url: string; file: string | null; exported: string | null
  original_id: string; original_url: string; original_title: string; original_created: string | null; original_modified: string | null
  authors: string[]; imported_at: string; imported_by: string; locked_reason: string; warnings: string[]
  signatures: { meaning: string; signer: string; signed_at: string; comment: string; matches: boolean; timestamp: boolean }[]
  files: { imported: number; matched: number; mismatched: number }
}
export interface ElnPreview {
  token: string; filename: string; size: number; source: string; source_url: string; exported: string | null
  items: { key: string; title: string; identifier: string; created: string | null; authors: string[]; files: number; bytes: number
    locked: boolean; lock_reason: string; from_benchtop: boolean; warnings: string[]; already: { id: number; eid: string } | null }[]
}
export interface ElnImportResult {
  created: { id: number; eid: string; title: string; locked: boolean; warnings: string[] }[]
  failed: { title: string; error: string }[]
}
export interface SampleCode { id: number; code: string; user: UserBrief | null; created_at: string }
export interface LinkedExperiment { id: number; eid: string; title: string; status?: Status; notebook_id?: number | null }
export interface InboxItem {
  id: number; path: string; revision: number; state: 'inbox' | 'ignored' | 'attached'; note: string
  instrument: { id: number; name: string; kind: string }
  files: { n: number; name: string; size: number; content_type: string }[]
  size: number; arrived_at: string; handled_at: string | null; handled_by: UserBrief | null
  matched: (LinkedExperiment & { status: Status })[]; experiment: LinkedExperiment | null
  follow_up: boolean; for_you: boolean
}
export interface Instrument {
  id: number; name: string; kind: string; kind_label: string; folder: string; audience: { id: number; name: string }[]
  settle_minutes: number; enabled: boolean; import_since: string | null; last_scan_at: string | null
  last_scan_ms: number | null; last_error: string; counts: Record<string, number>; created_at: string
}
export interface FolderPreview {
  folder: string; items: number; scan_seconds: number; hints: string[]
  latest: { path: string; action: 'attach' | 'inbox'; note: string; experiment: string | null; size: number; modified: string }[]
  since?: { items: number; attach: number; inbox: number }
}
export interface StoichRow {
  role: 'reactant' | 'reagent' | 'catalyst' | 'solvent' | 'product'
  name: string; smiles: string; formula: string; mw: number | null
  material_id: number | null; lot_id: number | null
  equiv: number | null; mmol: number | null; mass_mg: number | null; volume_ml: number | null
  density: number | null; conc_m: number | null; purity: number | null
  limiting: boolean; basis: 'mass' | 'volume' | 'equiv' | 'mmol' | 'fill'
  actual_mass_mg: number | null; theoretical_mass_mg: number | null; yield_pct: number | null; notes: string
}
export interface Lot {
  id: number; material_id: number; barcode: string; supplier: string; catalog_number: string
  purity: number | null; amount: number; unit: string; location: string; status: string
  received_at: string; expires_at: string | null
  material_name?: string; mid?: string; smiles?: string; mw?: number | null
}
export interface Material {
  id: number; mid: string; name: string; kind: string; smiles: string; cas: string; formula: string
  mw: number | null; inchikey: string; density: number | null; synonyms: string; hazards: string; notes: string
  lot_count: number; available_lots: number; lots?: Lot[]; safety?: any; pubchem_cid?: number | null; source_experiment?: { id: number | null; eid: string; title: string } | null
  used_in?: { id: number; eid: string; title: string; status: Status }[]; created_by_user?: UserBrief; created_at: string
  score?: number
}
export interface ReactionSummary {
  experiment_id: number; eid: string; title: string; status: Status; owner: UserBrief; tags: string[]; date: string
  entry_title: string; reaction_smiles: string; temperature_c: number | null; temperature_label: string | null
  time_h: number | null; atmosphere: string | null; concentration_m: number | null; limiting: string | null
  scale_mmol: number | null; reagents: string[]; solvents: string[]; product: string | null; yield_pct: number | null
  purification: string | null; characterised: string[]
  metrics?: GreenMetrics
}
export interface GreenMetrics { scope?: string; atom_economy_pct?: number; rme_pct?: number; e_factor?: number; pmi?: number; solvent_l_per_kg?: number; missing_mass?: string[] }
export const PURIFICATION_LABELS: Record<string, string> = {
  column: 'Flash column', prep_tlc: 'Prep TLC', prep_hplc: 'Prep HPLC', recrystallization: 'Recrystallisation',
  trituration: 'Trituration', distillation: 'Distillation', extraction: 'Extraction only', none: 'Used crude',
}
export interface AuditEvent { id: number; ts: string; username: string; action: string; object_type: string; object_id: number | null; details: any; hash: string }

const TOKEN_KEY = 'benchtop.token'
export const getToken = () => { try { return localStorage.getItem(TOKEN_KEY) } catch { return null } }
export const setToken = (t: string | null) => {
  try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) } catch { /* private mode */ }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

let onUnauthorized: () => void = () => {}
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn }

async function request<T>(method: string, path: string, body?: unknown, raw = false): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  let payload: BodyInit | undefined
  if (body instanceof FormData) payload = body
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body) }
  const res = await fetch(path, { method, headers, body: payload })
  if (res.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/logout' && !/\/(sign|witness|reject|withdraw|void)$/.test(path)) onUnauthorized()
  if (!res.ok) {
    let msg = res.statusText
    try {
      const j = await res.json()
      msg = typeof j.detail === 'string' ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: any) => d.msg).join('; ') : msg
    } catch { /* not json */ }
    throw new ApiError(res.status, msg)
  }
  if (raw) return res as unknown as T
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') || ''
  return (ct.includes('json') ? res.json() : res.text()) as Promise<T>
}

export const api = {
  get: <T>(p: string) => request<T>('GET', p),
  post: <T>(p: string, b?: unknown) => request<T>('POST', p, b ?? {}),
  put: <T>(p: string, b?: unknown) => request<T>('PUT', p, b),
  patch: <T>(p: string, b?: unknown) => request<T>('PATCH', p, b),
  del: <T>(p: string) => request<T>('DELETE', p),
  raw: (p: string) => request<Response>('GET', p, undefined, true),
}

export const depictUrl = (smiles: string, w = 260, h = 160, rxn = false, highlight = '') =>
  `/api/chem/depict?smiles=${encodeURIComponent(smiles)}&w=${w}&h=${h}${rxn ? '&rxn=true' : ''}${highlight ? `&highlight=${encodeURIComponent(highlight)}` : ''}`

export async function downloadAuthed(path: string, filename: string, open = false) {
  const res = await api.raw(path)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  if (open) window.open(url, '_blank')
  else {
    const a = document.createElement('a')
    a.href = url; a.download = filename; a.click()
  }
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

export const fmt = (v: number | null | undefined, nd = 3) =>
  v === null || v === undefined || Number.isNaN(v) ? '' : String(Number(v.toFixed(nd)))

export const when = (iso: string) => {
  const d = new Date(iso)
  return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export const displayName = (u?: UserBrief | null) => (u ? u.full_name || u.username : '—')

/** "just now", "5 min ago", "3 h ago", "yesterday", then a date */
export const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  if (s < 172800) return 'yesterday'
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: s > 300 * 86400 ? 'numeric' : undefined })
}

// ---------------------------------------------------------------- pictures inside text
// Pasted pictures are stored as <img data-attachment-id="12">: the file needs the sign-in token, so it is
// fetched here and shown from a blob URL (cached for the page's lifetime).
const blobUrls = new Map<number, Promise<string>>()
export function attachmentUrl(id: number): Promise<string> {
  let p = blobUrls.get(id)
  if (!p) {
    p = api.raw(`/api/attachments/${id}?inline=true`).then(r => r.blob()).then(b => URL.createObjectURL(b))
    p.catch(() => blobUrls.delete(id))
    blobUrls.set(id, p)
  }
  return p
}
/** Fill in every <img data-attachment-id> inside an element that hasn't got a picture yet. */
export function loadAttachmentImages(root: HTMLElement | null) {
  root?.querySelectorAll<HTMLImageElement>('img[data-attachment-id]').forEach(img => {
    if (img.getAttribute('src')) return
    const id = Number(img.dataset.attachmentId)
    // size and place it as chosen in the editor; click to see it full size
    const w = Math.round(Number(img.dataset.width)), wrapped = img.dataset.align === 'wrap-left' || img.dataset.align === 'wrap-right'
    if ((w >= 10 && w <= 100) || wrapped) { img.style.width = `${w >= 10 && w <= 100 ? w : 40}%`; img.classList.add('pic-sized') }
    img.style.cursor = 'zoom-in'
    img.addEventListener('click', () => window.dispatchEvent(new CustomEvent('benchtop:view-picture', { detail: { id, alt: img.alt } })))
    if (id) attachmentUrl(id).then(u => { img.src = u }).catch(() => { img.alt = `${img.alt || 'picture'} (couldn't load)` })
  })
}
