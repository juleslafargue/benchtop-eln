import { useCallback, useEffect, useRef, useState } from 'react'
import Modal from './Modal'
import { canUseLiveCamera, cleanCode, readCodes, type Decoded } from '../scan'
import { loadReader } from '../scanReader'

/** A photo as pixels, at most `max` pixels on its longest side (a phone's 12-megapixel photo reads as well, faster).
 *  The browser decodes it, so any photo it can show works (an iPhone's HEIC too) and it's turned the right way up. */
async function photoPixels(file: Blob, max = 2400): Promise<ImageData | null> {
  let src: CanvasImageSource & { width: number; height: number }
  let done = () => {}
  try {
    const bmp = await createImageBitmap(file)
    src = bmp; done = () => bmp.close()
  } catch {
    try {
      const url = URL.createObjectURL(file)
      const img = new Image()
      img.src = url
      await img.decode()
      src = img; done = () => URL.revokeObjectURL(url)
    } catch { return null }
  }
  try {
    const s = Math.min(1, max / Math.max(src.width, src.height))
    const c = document.createElement('canvas')
    c.width = Math.max(1, Math.round(src.width * s)); c.height = Math.max(1, Math.round(src.height * s))
    const ctx = c.getContext('2d', { willReadFrequently: true })
    if (!ctx) return null
    ctx.drawImage(src, 0, 0, c.width, c.height)
    return ctx.getImageData(0, 0, c.width, c.height)
  } catch { return null } finally { done() }
}

type Cam = 'off' | 'starting' | 'on' | 'paused' | 'error'
type Photo = { state: 'idle' | 'reading' | 'none' | 'error' } | { state: 'choose'; codes: Decoded[] }

/** Scan a barcode, three ways: the live camera (on a secure page), a photo of it (plain http, where browsers don't
 *  allow a live camera), or typing it, which is also how a USB or Bluetooth barcode scanner sends it (the code, then
 *  Enter). The camera is stopped as soon as a code is read or the dialog closes. */
export default function Scanner({ title = 'Scan a barcode', intro, onCode, onClose }: {
  title?: string; intro?: string; onCode: (code: string) => void; onClose: () => void
}) {
  const live = canUseLiveCamera(window)
  const touch = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches
  const [cam, setCam] = useState<Cam>('off')
  const [camErr, setCamErr] = useState('')
  const [photo, setPhoto] = useState<Photo>({ state: 'idle' })
  const [typed, setTyped] = useState('')
  const video = useRef<HTMLVideoElement>(null)
  const stream = useRef<MediaStream | null>(null)
  const run = useRef(0)  // the camera loop that may go on; anything else stops
  const alive = useRef(true)
  const fileInput = useRef<HTMLInputElement>(null)

  const stop = useCallback((next: Cam = 'off') => {
    run.current++
    stream.current?.getTracks().forEach(t => t.stop())
    stream.current = null
    if (video.current) video.current.srcObject = null
    if (alive.current) setCam(next)
  }, [])

  const answer = useRef(onCode)
  answer.current = onCode
  const found = useCallback((code: string) => { stop(); answer.current(code) }, [stop])

  const loop = useCallback(async (my: number) => {
    let read
    try { read = await loadReader() } catch { if (my === run.current) { stop('error'); setCamErr("The barcode reader couldn't be loaded. Check the connection to Benchtop, then try again.") } return }
    const canvas = document.createElement('canvas')
    const ctx = canvas.getContext('2d', { willReadFrequently: true })
    while (my === run.current && ctx) {
      const v = video.current
      if (v && v.readyState >= 2 && v.videoWidth) {
        const s = Math.min(1, 1280 / Math.max(v.videoWidth, v.videoHeight))
        canvas.width = Math.round(v.videoWidth * s); canvas.height = Math.round(v.videoHeight * s)
        ctx.drawImage(v, 0, 0, canvas.width, canvas.height)
        let codes: Decoded[] = []
        try { codes = await readCodes(read, ctx.getImageData(0, 0, canvas.width, canvas.height), false) } catch { /* the next frame */ }
        if (my !== run.current) return
        if (codes.length) { found(codes[0].text); return }
      }
      await new Promise(r => setTimeout(r, 120))
    }
  }, [found, stop])

  const start = useCallback(async () => {
    stop('starting')
    setCamErr('')
    const my = run.current
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } } })
      if (my !== run.current || !alive.current) { s.getTracks().forEach(t => t.stop()); return }  // closed while asking
      stream.current = s
      const v = video.current!
      v.srcObject = s
      await v.play().catch(() => {})
      setCam('on')
      loop(my)
    } catch (e: any) {
      if (my !== run.current) return
      stop('error')
      setCamErr(e?.name === 'NotAllowedError' ? "Benchtop isn't allowed to use the camera. Allow it in the browser's settings, or take a photo instead."
        : e?.name === 'NotFoundError' ? 'No camera was found. Take a photo instead, or type the code.' : "The camera couldn't be started. Take a photo instead, or type the code.")
    }
  }, [loop, stop])

  useEffect(() => {
    alive.current = true
    loadReader().catch(() => {})  // fetch the reader now, so the first frame or photo is read at once
    if (live && touch) start()
    // a phone put away with the dialog open: the camera goes off, and comes back with a tap
    const away = () => { if (document.visibilityState === 'hidden' && stream.current) stop('paused') }
    document.addEventListener('visibilitychange', away)
    return () => { document.removeEventListener('visibilitychange', away); alive.current = false; stop() }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const fromPhoto = async (file: File) => {
    stop()
    setPhoto({ state: 'reading' })
    try {
      const read = await loadReader()
      const pixels = await photoPixels(file)
      let codes = pixels ? await readCodes(read, pixels, true) : []
      if (!codes.length) codes = await readCodes(read, file, true).catch(() => [])  // the reader's own decoder: another chance
      if (!alive.current) return
      if (codes.length === 1) found(codes[0].text)
      else setPhoto(codes.length ? { state: 'choose', codes } : { state: 'none' })
    } catch { if (alive.current) setPhoto({ state: 'error' }) }
  }
  const takePhoto = () => { setPhoto({ state: 'idle' }); fileInput.current?.click() }
  const submitTyped = () => { const c = cleanCode(typed); if (c) found(c) }

  const showVideo = cam === 'starting' || cam === 'on'
  return (
    <Modal title={title} onClose={onClose} footer={<button className="btn" onClick={onClose}>Cancel</button>}>
      <div className="scanner stack" style={{ gap: 12 }}>
        {intro && <p className="muted" style={{ margin: 0 }}>{intro}</p>}
        <div className="scan-view" hidden={!showVideo}>
          <video ref={video} muted playsInline aria-label="Camera view: hold the barcode inside the frame" />
          <div className="scan-frame" aria-hidden="true" />
        </div>
        <div role="status" className="scan-status">
          {cam === 'starting' && 'Starting the camera…'}
          {cam === 'on' && 'Hold the barcode inside the frame.'}
          {cam === 'paused' && 'The camera was turned off while Benchtop was in the background.'}
          {cam === 'error' && <span className="error">{camErr}</span>}
          {photo.state === 'reading' && 'Reading the photo…'}
          {photo.state === 'none' && <span className="sp-note warn"><span>No barcode found in this photo. Try again with the code flat, in focus and filling more of the picture, or type it below.</span></span>}
          {photo.state === 'error' && <span className="error">The photo couldn't be read. Try again, or type the code below.</span>}
          {photo.state === 'choose' && <span>This photo has {photo.codes.length} codes. Which one?</span>}
        </div>
        {photo.state === 'choose' && <div className="stack" style={{ gap: 6 }}>
          {photo.codes.map(c => <button key={c.text} className="btn scan-choice" onClick={() => found(c.text)}><b>{c.text}</b><span className="faint">{c.format}</span></button>)}
        </div>}

        {live && cam !== 'on' && cam !== 'starting' && <button className={`btn${touch ? ' primary bench-big' : ''}`} onClick={start}>
          {cam === 'paused' || cam === 'error' ? 'Start the camera again' : 'Use the camera'}</button>}
        <input ref={fileInput} type="file" accept="image/*" capture="environment" hidden aria-label="Photo of the barcode"
          data-testid="scan-photo" onChange={e => { const f = e.target.files?.[0]; e.target.value = ''; if (f) fromPhoto(f) }} />
        {(!live || cam === 'error') && <>
          <button className={`btn${touch || !live ? ' primary bench-big' : ''}`} onClick={takePhoto} disabled={photo.state === 'reading'}>
            {photo.state === 'none' || photo.state === 'error' ? 'Take another photo' : 'Take a photo of the barcode'}</button>
          {!live && <p className="faint scan-why">Live scanning needs a secure (https) address; take a photo of the barcode instead.</p>}
        </>}

        <form className="scan-type" onSubmit={e => { e.preventDefault(); submitTyped() }}>
          <label className="field" htmlFor="scan-typed">Or type the code (a USB or Bluetooth barcode scanner types it here too)</label>
          <div className="row" style={{ flexWrap: 'nowrap' }}>
            <input id="scan-typed" className="input grow" value={typed} onChange={e => setTyped(e.target.value)} autoFocus={!touch}
              autoComplete="off" autoCapitalize="characters" spellCheck={false} enterKeyHint="search" placeholder="e.g. a VIM label or supplier code" />
            <button className="btn" type="submit" disabled={!cleanCode(typed)}>Look up</button>
          </div>
        </form>
      </div>
    </Modal>
  )
}
