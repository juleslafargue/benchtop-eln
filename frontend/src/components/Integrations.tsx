import { useEffect, useRef, useState } from 'react'
import { useApp } from '../App'
import { day, ago, api, when } from '../api'
import Modal from './Modal'
import { resetPluginSections } from './AddSection'
import { resetFileReaders } from '../spectra'

/** Copy-to-clipboard with a fallback for browsers that refuse it. */
function CopyField({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false)
  const ref = useRef<HTMLInputElement>(null)
  const copy = async () => {
    try { await navigator.clipboard.writeText(value) } catch { ref.current?.select(); document.execCommand('copy') }
    setDone(true); setTimeout(() => setDone(false), 1500)
  }
  return (
    <div className="copy-field">
      <input ref={ref} className="input mono" readOnly value={value} aria-label={label} onFocus={e => e.target.select()} />
      <button className="btn sm" onClick={copy}>{done ? 'Copied' : 'Copy'}</button>
    </div>
  )
}

// ====================================================================== API tokens (Account page)

interface Token { id: number; name: string; prefix: string; scope: 'read' | 'write'; created_at: string; expires_at: string | null; last_used_at: string | null; last_used_ip: string; expired: boolean; token?: string }

export function ApiTokens() {
  const { user, toast } = useApp()
  const [rows, setRows] = useState<Token[] | null>(null)
  const [making, setMaking] = useState(false)
  const [made, setMade] = useState<Token | null>(null)
  const load = () => api.get<Token[]>('/api/auth/tokens').then(setRows).catch(() => setRows([]))
  useEffect(() => { load() }, [])
  const revoke = async (t: Token) => {
    if (!confirm(`Revoke “${t.name}”? Anything using it stops working straight away.`)) return
    try { await api.del(`/api/auth/tokens/${t.id}`); toast('Token revoked'); load() } catch (e: any) { toast(e.message) }
  }
  return (
    <div className="panel">
      {rows?.map(t => (
        <div key={t.id} className="section spread session-row">
          <div>
            <b style={{ fontWeight: 600 }}>{t.name}</b>
            <span className={`tag${t.scope === 'write' ? ' lead' : ''}`} style={{ marginLeft: 8 }}>{t.scope === 'write' ? 'read & write' : 'read only'}</span>
            {t.expired && <span className="tag warn-tag" style={{ marginLeft: 6 }}>expired</span>}
            <div className="faint"><code>{t.prefix}…</code> · made {when(t.created_at)} · {t.expires_at ? `${t.expired ? 'expired' : 'expires'} ${day(t.expires_at)}` : 'never expires'}
              {' · '}{t.last_used_at ? `last used ${ago(t.last_used_at)}${t.last_used_ip ? ` from ${t.last_used_ip}` : ''}` : 'never used'}</div>
          </div>
          <button className="btn sm" onClick={() => revoke(t)}>Revoke</button>
        </div>
      ))}
      {rows && !rows.length && <div className="section faint">No tokens. Make one when a script or another program needs to read from or write to your notebooks.</div>}
      <div className="section"><button className="btn sm" onClick={() => setMaking(true)}>New token…</button></div>
      {making && <NewToken viewer={user.role === 'viewer'} onClose={() => setMaking(false)} onMade={t => { setMaking(false); setMade(t); load() }} />}
      {made && <Modal title="Your new token" onClose={() => setMade(null)} footer={<button className="btn primary" onClick={() => setMade(null)}>I've saved it</button>}>
        <p>Copy it now: <b>Benchtop won't show it again.</b> Keep it somewhere safe, like a password manager; anyone who has it can act as you{made.scope === 'write' ? ' (except signing)' : ' (read only)'}.</p>
        <CopyField value={made.token!} label="API token" />
        <p className="faint" style={{ marginTop: 12 }}>Use it as a bearer token, for example:</p>
        <pre className="code-block">{`curl -H "Authorization: Bearer ${made.token}" \\\n  ${location.origin}/api/experiments`}</pre>
        <p className="faint">The full list of API endpoints is at <a href="/docs" target="_blank" rel="noreferrer">/docs</a>.</p>
      </Modal>}
    </div>
  )
}

function NewToken({ viewer, onClose, onMade }: { viewer: boolean; onClose: () => void; onMade: (t: Token) => void }) {
  const [name, setName] = useState('')
  const [scope, setScope] = useState<'read' | 'write'>('read')
  const [days, setDays] = useState<string>('90')
  const [err, setErr] = useState('')
  const make = async () => {
    try { onMade(await api.post<Token>('/api/auth/tokens', { name, scope, days: days === 'never' ? null : Number(days) })) } catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title="New API token" onClose={onClose} footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!name.trim()} onClick={make}>Make token</button></>}>
      <div className="stack" style={{ gap: 14 }}>
        <label className="field">What it's for<input className="input" autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Plate reader upload script" /></label>
        <div className="field"><span>Access</span>
          <div className="choice" role="group" aria-label="Access">
            <button type="button" aria-pressed={scope === 'read'} onClick={() => setScope('read')}>Read only</button>
            <button type="button" aria-pressed={scope === 'write'} disabled={viewer} onClick={() => setScope('write')}>Read &amp; write</button>
          </div>
          <span className="help">Tokens can never sign, witness or void records, or change accounts and settings. Those always need you.</span>
        </div>
        <label className="field" style={{ maxWidth: 240 }}>Expires
          <select className="select" value={days} onChange={e => setDays(e.target.value)}>
            <option value="7">in 7 days</option><option value="30">in 30 days</option><option value="90">in 90 days</option>
            <option value="365">in a year</option><option value="never">never</option>
          </select></label>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      </div>
    </Modal>
  )
}

// ====================================================================== webhooks (Admin page)

interface Delivery { id: number; event: string; created_at: string; status_code: number | null; ok: boolean; error: string; attempts: number; duration_ms: number | null }
interface Hook { id: number; name: string; url: string; events: string[]; format: 'json' | 'slack' | 'teams'; include_private: boolean; active: boolean; has_secret: boolean; created_at: string; last: Delivery | null; secret?: string }

export function Webhooks() {
  const { toast } = useApp()
  const [data, setData] = useState<{ events: Record<string, string>; webhooks: Hook[] } | null>(null)
  const [editing, setEditing] = useState<Hook | 'new' | null>(null)
  const [secret, setSecret] = useState<{ name: string; secret: string } | null>(null)
  const [log, setLog] = useState<{ hook: Hook; rows: Delivery[] } | null>(null)
  const load = () => api.get<typeof data>('/api/webhooks').then(setData).catch(() => {})
  useEffect(() => { load() }, [])
  const test = async (h: Hook) => {
    const d = await api.post<Delivery>(`/api/webhooks/${h.id}/test`).catch(e => { toast(e.message); return null })
    if (d) toast(d.ok ? `Test delivered to ${h.name} (HTTP ${d.status_code})` : `Test failed: ${d.error}`)
    load()
  }
  const toggle = async (h: Hook) => { await api.patch(`/api/webhooks/${h.id}`, { active: !h.active }); load() }
  const remove = async (h: Hook) => { if (confirm(`Delete the webhook “${h.name}”?`)) { await api.del(`/api/webhooks/${h.id}`); load() } }
  const rotate = async (h: Hook) => {
    if (!confirm('Make a new signing secret? The receiver must be updated with it, or it will reject deliveries.')) return
    const r = await api.post<{ secret: string }>(`/api/webhooks/${h.id}/secret`); setSecret({ name: h.name, secret: r.secret })
  }
  const showLog = async (h: Hook) => setLog({ hook: h, rows: await api.get<Delivery[]>(`/api/webhooks/${h.id}/deliveries`) })
  if (!data) return null
  return (
    <section className="stack" style={{ gap: 8 }}>
      <div className="spread"><h2 style={{ fontSize: 17 }}>Webhooks</h2><button className="btn sm" onClick={() => setEditing('new')}>Add webhook</button></div>
      <p className="faint" style={{ margin: 0 }}>Benchtop calls these addresses when something happens, for example to post in a Slack or Teams channel when an experiment
        needs witnessing. They receive who did what to which experiment, never its contents.</p>
      <div className="panel integ">
        {data.webhooks.map(h => (
          <div key={h.id} className="section hook-row">
            <div style={{ minWidth: 0 }}>
              <b style={{ fontWeight: 600 }}>{h.name}</b>
              <span className="tag" style={{ marginLeft: 8 }}>{{ json: 'JSON', slack: 'Slack', teams: 'Teams' }[h.format]}</span>
              {!h.active && <span className="tag" style={{ marginLeft: 6 }}>paused</span>}
              <div className="faint hook-url" title={h.url}>{h.url}</div>
              <div className="faint">{h.events.map(e => e.replace('experiment.', '').replace('.created', ' created')).join(', ')}{h.include_private ? ' · includes private notebooks' : ''}</div>
              {h.last && <div className={h.last.ok ? 'ok-text' : 'warn-text'} style={{ fontSize: 13 }}>
                {h.last.ok ? '✓' : '✗'} Last delivery {ago(h.last.created_at)}: {h.last.event}, {h.last.ok ? `HTTP ${h.last.status_code}` : h.last.error}</div>}
            </div>
            <div className="hook-actions">
              <button className="btn ghost sm" onClick={() => test(h)}>Send test</button>
              <button className="btn ghost sm" onClick={() => showLog(h)}>History</button>
              <button className="btn ghost sm" onClick={() => setEditing(h)}>Edit</button>
              <button className="btn ghost sm" onClick={() => toggle(h)}>{h.active ? 'Pause' : 'Resume'}</button>
              {h.format === 'json' && <button className="btn ghost sm" onClick={() => rotate(h)}>New secret</button>}
              <button className="btn ghost sm" onClick={() => remove(h)}>Delete</button>
            </div>
          </div>
        ))}
        {!data.webhooks.length && <div className="section faint">No webhooks yet.</div>}
      </div>
      {editing && <HookForm hook={editing === 'new' ? null : editing} events={data.events} onClose={() => setEditing(null)}
        onSaved={h => { setEditing(null); load(); if (h.secret && h.format === 'json') setSecret({ name: h.name, secret: h.secret }) }} />}
      {secret && <Modal title={`Signing secret for ${secret.name}`} onClose={() => setSecret(null)} footer={<button className="btn primary" onClick={() => setSecret(null)}>Done</button>}>
        <p>Each delivery carries <code>X-Benchtop-Signature: sha256=…</code>, an HMAC-SHA256 of the request body made with this secret. Give it to the receiving service so it can check requests really came from Benchtop. It won't be shown again.</p>
        <CopyField value={secret.secret} label="Signing secret" />
      </Modal>}
      {log && <Modal wide title={`Deliveries to ${log.hook.name}`} onClose={() => setLog(null)} footer={<button className="btn" onClick={() => setLog(null)}>Close</button>}>
        {log.rows.length ? <table className="plain"><thead><tr><th>When</th><th>Event</th><th>Result</th><th>Tries</th><th>Time</th></tr></thead>
          <tbody>{log.rows.map(d => <tr key={d.id}><td>{when(d.created_at)}</td><td>{d.event}</td>
            <td className={d.ok ? 'ok-text' : 'warn-text'}>{d.ok ? `✓ HTTP ${d.status_code}` : `✗ ${d.error}`}</td><td>{d.attempts}</td><td>{d.duration_ms != null ? `${d.duration_ms} ms` : ''}</td></tr>)}</tbody></table>
          : <p className="faint">Nothing sent yet.</p>}
      </Modal>}
    </section>
  )
}

function HookForm({ hook, events, onClose, onSaved }: { hook: Hook | null; events: Record<string, string>; onClose: () => void; onSaved: (h: Hook) => void }) {
  const [f, setF] = useState({ name: hook?.name || '', url: hook?.url || '', events: hook?.events || ['experiment.signed'], format: hook?.format || 'json' as Hook['format'], include_private: hook?.include_private || false })
  const [err, setErr] = useState('')
  const guess = (url: string) => /hooks\.slack\.com/.test(url) ? 'slack' : /webhook\.office\.com|logic\.azure\.com|powerplatform/.test(url) ? 'teams' : f.format
  const save = async () => {
    try { onSaved(hook ? await api.patch<Hook>(`/api/webhooks/${hook.id}`, f) : await api.post<Hook>('/api/webhooks', f)) } catch (e: any) { setErr(e.message) }
  }
  return (
    <Modal title={hook ? `Edit ${hook.name}` : 'Add a webhook'} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={!f.url || !f.events.length} onClick={save}>{hook ? 'Save' : 'Add webhook'}</button></>}>
      <div className="stack" style={{ gap: 14 }}>
        <label className="field">Address<input className="input" autoFocus value={f.url} placeholder="https://hooks.slack.com/services/…"
          onChange={e => setF({ ...f, url: e.target.value, format: guess(e.target.value) })} /></label>
        <label className="field">Name<input className="input" value={f.name} placeholder="e.g. #ruiz-lab channel" onChange={e => setF({ ...f, name: e.target.value })} /></label>
        <div className="field"><span>Send as</span>
          <div className="choice" role="group" aria-label="Format">
            {(['slack', 'teams', 'json'] as const).map(x => <button key={x} type="button" aria-pressed={f.format === x} onClick={() => setF({ ...f, format: x })}>
              {{ slack: 'Slack message', teams: 'Teams message', json: 'JSON (for your own code)' }[x]}</button>)}
          </div>
          <span className="help">{f.format === 'json' ? 'The full event as JSON, signed with a secret so your code can check it.' : 'A one-line message with a link, e.g. “Priya Nair signed EXP-00025: KX-107 reductive amination”.'}</span>
        </div>
        <fieldset className="field hook-events"><legend>When</legend>
          {Object.entries(events).map(([k, label]) => <label key={k} className="row" style={{ gap: 8 }}>
            <input type="checkbox" checked={f.events.includes(k)} onChange={e => setF({ ...f, events: e.target.checked ? [...f.events, k] : f.events.filter(x => x !== k) })} />{label}</label>)}
        </fieldset>
        <label className="row" style={{ gap: 8 }}><input type="checkbox" checked={f.include_private} onChange={e => setF({ ...f, include_private: e.target.checked })} />
          Include experiments in private notebooks (their titles would be sent too)</label>
        {err && <p className="error" style={{ margin: 0 }}>{err}</p>}
      </div>
    </Modal>
  )
}

// ====================================================================== plugins (Admin page)

interface PluginInfo {
  id: string; name: string; version: string; author: string; description: string; homepage: string; file: string; bundled: boolean; enabled: boolean
  errors: string[]; sections: { id: string; name: string; description: string }[]; readers: { id: string; name: string; technique: string }[]
}

export function Plugins() {
  const { toast } = useApp()
  const [data, setData] = useState<{ api: number; folder: string; plugins: PluginInfo[] } | null>(null)
  const [preview, setPreview] = useState<{ file: File; plugin?: PluginInfo; error?: string } | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const load = () => api.get<typeof data>('/api/plugins').then(setData).catch(() => {})
  useEffect(() => { load() }, [])
  const changed = () => { resetPluginSections(); resetFileReaders(); load() }
  const toggle = async (p: PluginInfo) => {
    try { await api.post(`/api/plugins/${p.id}/enabled`, { enabled: !p.enabled }); toast(`${p.name} ${p.enabled ? 'switched off' : 'switched on'}`); changed() } catch (e: any) { toast(e.message) }
  }
  const remove = async (p: PluginInfo) => {
    if (!confirm(`Remove ${p.name}? Sections already made with it stay readable; nobody can add new ones.`)) return
    await api.del(`/api/plugins/${p.id}`); changed()
  }
  const pick = async (file: File) => {
    const fd = new FormData(); fd.append('file', file)
    const r = await api.post<{ ok: boolean; plugin?: PluginInfo; error?: string }>('/api/plugins/check', fd).catch((e: any) => ({ ok: false, plugin: undefined, error: e.message as string }))
    setPreview({ file, plugin: r.plugin, error: r.error })
  }
  const install = async () => {
    if (!preview) return
    const fd = new FormData(); fd.append('file', preview.file)
    try { const r = await api.post<{ replaced: boolean; plugin: PluginInfo }>('/api/plugins', fd); toast(`${r.plugin.name} ${r.replaced ? 'updated' : 'added and switched on'}`); setPreview(null); changed() }
    catch (e: any) { setPreview({ ...preview, error: e.message }) }
  }
  if (!data) return null
  return (
    <section className="stack" style={{ gap: 8 }}>
      <div className="spread"><h2 style={{ fontSize: 17 }}>Plugins</h2>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn sm" onClick={() => input.current?.click()}>Add plugin file…</button>
          <input ref={input} type="file" accept=".toml,text/plain" hidden onChange={e => { const f = e.target.files?.[0]; if (f) pick(f); e.target.value = '' }} />
        </div></div>
      <p className="faint" style={{ margin: 0 }}>Plugins are small text files (TOML) that add section types and instrument-file readers, with no code. Sections made from a plugin keep a copy of
        its layout, so they stay readable if the plugin changes or is removed. How to write one: <code>docs/PLUGINS.md</code>.</p>
      <div className="panel integ">
        {data.plugins.map(p => (
          <div key={p.id} className={`section plugin-row${p.enabled ? ' on' : ''}`}>
            <div style={{ minWidth: 0 }}>
              <b style={{ fontWeight: 600 }}>{p.name}</b>{p.version && <span className="faint"> v{p.version}</span>}
              <span className="tag" style={{ marginLeft: 8 }}>{p.bundled ? 'example' : 'added by your lab'}</span>
              {p.description && <div className="faint">{p.description}</div>}
              {p.errors.length > 0 && <div className="error" style={{ margin: '4px 0 0' }}>{p.file}: {p.errors[0]}</div>}
              <div className="plugin-adds">
                {p.sections.map(s => <span key={s.id} className="tag" title={s.description}>Section · {s.name}</span>)}
                {p.readers.map(r => <span key={r.id} className="tag" title={r.technique}>Reader · {r.name}</span>)}
              </div>
            </div>
            <div className="hook-actions">
              <label className="switch"><input type="checkbox" role="switch" checked={p.enabled} disabled={p.errors.length > 0} onChange={() => toggle(p)} aria-label={`${p.name} enabled`} /><span>{p.enabled ? 'On' : 'Off'}</span></label>
              {!p.bundled && <button className="btn ghost sm" onClick={() => remove(p)}>Remove</button>}
            </div>
          </div>
        ))}
        {!data.plugins.length && <div className="section faint">No plugins found.</div>}
      </div>
      <p className="faint" style={{ margin: 0, fontSize: 12.5 }}>Your lab's plugin files live in <code>{data.folder}</code>; you can also copy files there directly.</p>
      {preview && <Modal title={preview.plugin ? `Add ${preview.plugin.name}?` : 'This plugin file has a problem'} onClose={() => setPreview(null)}
        footer={<><button className="btn" onClick={() => setPreview(null)}>Cancel</button>{preview.plugin && !preview.error && <button className="btn primary" onClick={install}>
          {data.plugins.some(p => p.id === preview.plugin!.id && !p.bundled) ? 'Update plugin' : 'Add and switch on'}</button>}</>}>
        {preview.plugin && <>
          <p>{preview.plugin.description || <span className="faint">No description.</span>}</p>
          <dl className="kv"><dt>File</dt><dd>{preview.file.name}</dd><dt>Version</dt><dd>{preview.plugin.version || '—'}</dd><dt>Author</dt><dd>{preview.plugin.author || '—'}</dd>
            <dt>Adds</dt><dd>{[...preview.plugin.sections.map(s => `section “${s.name}”`), ...preview.plugin.readers.map(r => `reader “${r.name}”`)].join(', ')}</dd></dl>
        </>}
        {preview.error && <p className="error">{preview.error}</p>}
      </Modal>}
    </section>
  )
}
