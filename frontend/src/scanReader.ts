// The barcode decoder (zxing-wasm, MIT), fetched only when a scanner first opens so the app's main script doesn't
// grow. Its .wasm file is one of Benchtop's own built files (in dist/assets), never a CDN: it works offline on a lab
// network. Without the locateFile below the library would fetch it from jsDelivr.
import type { Reader } from './scan'

let ready: Promise<Reader> | null = null

export function loadReader(): Promise<Reader> {
  if (!ready) {
    ready = (async () => {
      const [zx, wasm] = await Promise.all([import('zxing-wasm/reader'), import('zxing-wasm/reader/zxing_reader.wasm?url')])
      zx.prepareZXingModule({ overrides: { locateFile: (path: string, prefix: string) => (path.endsWith('.wasm') ? wasm.default : prefix + path) } })
      return zx.readBarcodes as unknown as Reader
    })()
    ready.catch(() => { ready = null })  // a network hiccup: try again next time
  }
  return ready
}
