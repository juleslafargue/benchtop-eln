/** Which attached files the spectrum viewer opens. No imports, so `npm test` can check it directly. */

export const SPECTRUM_EXT = /\.(jdx|dx|jcamp|jcm|csv|tsv|txt|xy|zip)$/i
/** Raw vendor data zipped by instrument folders (LC-MS .D, .raw, Sciex .wiff, Agilent .fid): not spectra it can read. */
const VENDOR_ZIP = /\.(d|raw|wiff2?|fid)\.zip$/i
/** Opening means downloading the whole file into the browser: above this, offer Download only. */
export const VIEW_MAX_BYTES = 100_000_000

export function canViewSpectrum(a: { filename: string; size?: number }): boolean {
  return SPECTRUM_EXT.test(a.filename) && !VENDOR_ZIP.test(a.filename) && (a.size ?? 0) <= VIEW_MAX_BYTES
}

/** .txt, .csv and .tsv may be spectra (two columns of numbers, JCAMP, an instrument's export) or anything else (notes, a
 *  results table). Up to this size the file is read first, and "View spectrum" is offered only if it can be shown. */
const TEXT_DATA = /\.(txt|csv|tsv)$/i
export const CHECK_MAX_BYTES = 5_000_000
export function needsReadCheck(a: { filename: string; size?: number }): boolean {
  return canViewSpectrum(a) && TEXT_DATA.test(a.filename) && (a.size ?? 0) <= CHECK_MAX_BYTES
}

/** How an attached picture is shown (0.36: a TIFF was a broken image). 'img': as it is; 'preview': a PNG the server
 *  makes from it, for viewing only (TIFF, from microscopes, gel imagers and scanners); 'none': no picture, just a line
 *  saying so (a format browsers can't show, such as HEIC or a camera's raw file); null: not a picture. */
const BROWSER_IMAGE = /^image\/(png|jpeg|pjpeg|gif|webp|avif|apng|bmp|svg\+xml|x-icon|vnd\.microsoft\.icon)$/i
export const isTiff = (a: { filename: string; content_type: string }) =>
  /^image\/(x-)?tiff?$/i.test(a.content_type) || /\.tiff?$/i.test(a.filename)
export function pictureKind(a: { filename: string; content_type: string }): 'img' | 'preview' | 'none' | null {
  if (isTiff(a)) return 'preview'
  if (!a.content_type.toLowerCase().startsWith('image/')) return null
  return BROWSER_IMAGE.test(a.content_type) ? 'img' : 'none'
}
/** The format, for "No preview for TIFF": TIFF, HEIC, CR2… */
export function formatName(a: { filename: string; content_type: string }): string {
  if (isTiff(a)) return 'TIFF'
  const ext = /\.([a-z0-9]{1,6})$/i.exec(a.filename)?.[1]
  return (ext || a.content_type.split('/')[1]?.replace(/^x-/, '') || 'this file').toUpperCase()
}
