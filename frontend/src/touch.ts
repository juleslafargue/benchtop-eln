// A touch screen with no mouse or trackpad (a phone, a tablet on its own): keyboard hints mean nothing there (0.35).
export const TOUCH_ONLY = '(hover: none), (pointer: coarse)'
export const touchOnly = () => typeof matchMedia === 'function' && matchMedia(TOUCH_ONLY).matches
