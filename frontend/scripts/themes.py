"""Benchtop colour themes: one place to define them, check their contrast, and write src/themes.css.

    python3 scripts/themes.py          # check contrast and regenerate src/themes.css

Every theme has a light and a dark version. "benchtop" is the default and lives in styles.css.
"""
from pathlib import Path

KEYS = ["ground", "paper", "paper-2", "ink", "ink-2", "ink-3", "rule", "rule-strong", "accent", "accent-ink",
        "accent-soft", "open", "signed", "witnessed", "voided", "danger", "focus"]

THEMES = {
    "benchtop": {  # the default, as in styles.css (here only for the contrast check)
        "light": dict(ground="#e6ecf1", paper="#fbfcfd", **{"paper-2": "#f2f5f8"}, ink="#17212c", **{"ink-2": "#4b5b6b", "ink-3": "#5d6a77"},
                      rule="#cbd5de", **{"rule-strong": "#9fb0bf"}, accent="#b8336a", **{"accent-ink": "#ffffff", "accent-soft": "#f6e3eb"},
                      open="#5b6b7b", signed="#9a6410", witnessed="#1d7466", voided="#8b4a4a", danger="#b3261e", focus="#2f6fdf"),
        "dark": dict(ground="#10171e", paper="#172029", **{"paper-2": "#1d2833"}, ink="#e4ebf1", **{"ink-2": "#a7b5c2", "ink-3": "#8293a2"},
                     rule="#2c3a47", **{"rule-strong": "#46586a"}, accent="#e0699a", **{"accent-ink": "#1a0d13", "accent-soft": "#3a1f2b"},
                     open="#9aaaba", signed="#e0a64a", witnessed="#4fc2ae", voided="#d58c8c", danger="#ff8a80", focus="#7aa7ff"),
    },
    # a bound lab notebook: pale grid paper, blue-black ink, the red margin rule (dark: the navy cover)
    "notebook": {
        "light": dict(ground="#e7ece6", paper="#fdfdfb", **{"paper-2": "#f1f4ef"}, ink="#1a2240", **{"ink-2": "#434e6c", "ink-3": "#5f6883"},
                      rule="#cdd5d8", **{"rule-strong": "#98a5b3"}, accent="#24479e", **{"accent-ink": "#ffffff", "accent-soft": "#e0e7f7"},
                      open="#5a6480", signed="#935f0f", witnessed="#1f6f5e", voided="#8b4a4a", danger="#b3261e", focus="#24479e",
                      grid="rgba(84, 128, 176, .16)", margin="#d9534f"),
        "dark": dict(ground="#121828", paper="#1a2134", **{"paper-2": "#20283d"}, ink="#e7eaf3", **{"ink-2": "#aab3c8", "ink-3": "#8590a8"},
                     rule="#2d3750", **{"rule-strong": "#48546f"}, accent="#91aaff", **{"accent-ink": "#0d1430", "accent-soft": "#232f55"},
                     open="#a3adc2", signed="#e2a852", witnessed="#55c3ad", voided="#dd9292", danger="#ff8a80", focus="#91aaff",
                     grid="rgba(140, 160, 210, .07)", margin="#c9615c"),
    },
    # a fume hood: brushed steel, graphite, and the yellow of the sash label
    "steel": {
        "light": dict(ground="#dee1e4", paper="#fafbfb", **{"paper-2": "#eef0f2"}, ink="#1b1f23", **{"ink-2": "#484f57", "ink-3": "#5b636b"},
                      rule="#c6cbd0", **{"rule-strong": "#959da6"}, accent="#22282e", **{"accent-ink": "#ffd23f", "accent-soft": "#f6edc4"},
                      open="#5c656e", signed="#9a5a0c", witnessed="#1d6b5f", voided="#8b4a4a", danger="#b3261e", focus="#1f6fd1"),
        "dark": dict(ground="#101214", paper="#171a1d", **{"paper-2": "#1e2226"}, ink="#e6e8ea", **{"ink-2": "#a9b0b7", "ink-3": "#848c94"},
                     rule="#2a2f34", **{"rule-strong": "#464d55"}, accent="#ffd23f", **{"accent-ink": "#1a1600", "accent-soft": "#39321a"},
                     open="#a2abb4", signed="#f29a52", witnessed="#52c1ab", voided="#dd9292", danger="#ff8a80", focus="#7aa7ff"),
    },
    # litmus: violet paper (dark: the same violet, a shade from black)
    "litmus": {
        "light": dict(ground="#ebe7f1", paper="#fdfcfe", **{"paper-2": "#f4f1f8"}, ink="#221a33", **{"ink-2": "#524868", "ink-3": "#6c6380"},
                      rule="#d6cfe2", **{"rule-strong": "#a89cbd"}, accent="#6a3aa8", **{"accent-ink": "#ffffff", "accent-soft": "#ece2f8"},
                      open="#635a76", signed="#935f0f", witnessed="#1d6f63", voided="#9a3f4f", danger="#b3261e", focus="#6a3aa8"),
        "dark": dict(ground="#16121f", paper="#1d1829", **{"paper-2": "#251f34"}, ink="#ece8f4", **{"ink-2": "#b3aac6", "ink-3": "#8f86a4"},
                     rule="#322a44", **{"rule-strong": "#5b5075"}, accent="#c3a3ff", **{"accent-ink": "#1c0f33", "accent-soft": "#33264d"},
                     open="#aea5c0", signed="#e2a852", witnessed="#55c3ad", voided="#e398a6", danger="#ff8a80", focus="#c3a3ff"),
    },
    # high contrast, with a typeface designed for low vision (Atkinson Hyperlegible)
    "contrast": {
        "light": dict(ground="#ffffff", paper="#ffffff", **{"paper-2": "#f0f0f0"}, ink="#000000", **{"ink-2": "#1f1f1f", "ink-3": "#454545"},
                      rule="#767676", **{"rule-strong": "#000000"}, accent="#0039b3", **{"accent-ink": "#ffffff", "accent-soft": "#dce6ff"},
                      open="#333333", signed="#7a4a00", witnessed="#005c4b", voided="#7a1f1f", danger="#a00000", focus="#0039b3"),
        "dark": dict(ground="#000000", paper="#000000", **{"paper-2": "#161616"}, ink="#ffffff", **{"ink-2": "#ebebeb", "ink-3": "#c4c4c4"},
                     rule="#8a8a8a", **{"rule-strong": "#ffffff"}, accent="#9cc0ff", **{"accent-ink": "#000000", "accent-soft": "#10264d"},
                     open="#d9d9d9", signed="#ffc266", witnessed="#5eead4", voided="#ffadad", danger="#ff9e9e", focus="#ffdd00"),
    },
}
LABELS = {  # what the picker in Account settings shows
    "benchtop": ("Benchtop", "Cool grey-blue with a magenta accent. The original."),
    "notebook": ("Lab notebook", "Grid paper, blue-black ink and a red margin rule; a navy cover in dark mode."),
    "steel": ("Fume hood", "Brushed steel and graphite, with sash-label yellow on the buttons."),
    "litmus": ("Litmus", "Soft violet throughout."),
    "contrast": ("High contrast", "Black on white (or white on black), heavier outlines, and a typeface designed for low vision."),
}
EXTRA = {  # type changes per theme
    "steel": {"serif": "var(--sans)"},
    "contrast": {"sans": "'Atkinson Hyperlegible', system-ui, sans-serif", "serif": "'Atkinson Hyperlegible', system-ui, sans-serif"},
}


def lum(h: str) -> float:
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (foreground, background, minimum): body text 7 (contrast theme) / 4.5, secondary text 4.5 (on the page, panels and
# the sidebar alike: axe-core checks every one), statuses and accents 3+
CHECKS = [("ink", "paper", 7), ("ink-2", "paper", 4.5), ("ink-3", "paper", 4.5), ("ink-3", "ground", 4.5), ("ink-3", "paper-2", 4.5), ("ink", "ground", 7),
          ("accent", "paper", 4.5), ("accent-ink", "accent", 4.5), ("ink", "accent-soft", 7), ("open", "paper", 4.5),
          ("signed", "paper", 4.5), ("signed", "paper-2", 4.5), ("witnessed", "paper", 4.5), ("voided", "paper", 4.5), ("danger", "paper", 4.5),
          ("focus", "paper", 3), ("rule-strong", "paper", 2)]


def check() -> bool:
    ok = True
    for name, modes in THEMES.items():
        for mode, v in modes.items():
            bad = [f"{f} on {b} {ratio(v[f], v[b]):.1f} < {m}" for f, b, m in CHECKS if ratio(v[f], v[b]) < m]
            if bad:
                ok = False
            print(f"{name:9s} {mode:5s} {'ok' if not bad else 'LOW: ' + '; '.join(bad)}")
    return ok


def block(v: dict, extra: dict, mode: str) -> str:
    items = [f"--{k}: {v[k]};" for k in KEYS] + [f"--{k}: {v[k]};" for k in ("grid", "margin") if k in v]
    items += [f"--{k}: {x};" for k, x in extra.items()] + [f"color-scheme: {mode};"]
    return " ".join(items)


def css() -> str:
    out = ["/* Generated by scripts/themes.py — edit there, then run: python3 scripts/themes.py */",
           "/* Picked in Account settings (data-palette on <html>); light/dark still follows data-theme or the system. */"]
    for name, modes in THEMES.items():
        if name == "benchtop":
            continue
        sel = f":root[data-palette='{name}']"
        ex = EXTRA.get(name, {})
        out.append(f"{sel} {{ {block(modes['light'], ex, 'light')} }}")
        out.append(f"@media (prefers-color-scheme: dark) {{ {sel}:not([data-theme='light']) {{ {block(modes['dark'], ex, 'dark')} }} }}")
        out.append(f"{sel}[data-theme='dark'] {{ {block(modes['dark'], ex, 'dark')} }}")
    out.append("""
/* Lab notebook: grid paper behind everything, and the red margin rule down the sidebar */
:root[data-palette='notebook'] body {
  background-image: linear-gradient(var(--grid) 1px, transparent 1px), linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 22px 22px; background-attachment: fixed; }
:root[data-palette='notebook'] .side { box-shadow: inset -2px 0 0 var(--margin); }

/* Fume hood: graphite buttons with sash-label yellow lettering; squarer corners */
:root[data-palette='steel'] .btn.primary { letter-spacing: .01em; }
:root[data-palette='steel'] .btn, :root[data-palette='steel'] .input, :root[data-palette='steel'] .select { border-radius: 4px; }

/* High contrast: heavier outlines and underlined links */
:root[data-palette='contrast'] :focus-visible { outline-width: 3px; }
:root[data-palette='contrast'] .panel, :root[data-palette='contrast'] .entry { border-color: var(--rule-strong); }
:root[data-palette='contrast'] a:not(.btn):not(.list-row):not(.side a) { text-decoration: underline; }
""")
    return "\n".join(out) + "\n"


def ts() -> str:
    import json
    items = []
    for name, modes in THEMES.items():
        label, desc = LABELS[name]
        prev = {m: {k: v[k] for k in ("ground", "paper", "ink", "ink-3", "rule", "accent", "accent-ink", "signed", "witnessed")} for m, v in modes.items()}
        items.append({"id": name, "label": label, "description": desc, "preview": prev})
    return ("// Generated by scripts/themes.py — edit there, then run: python3 scripts/themes.py\n"
            "export interface ThemeInfo { id: string; label: string; description: string; preview: Record<'light' | 'dark', Record<string, string>> }\n"
            f"export const THEMES: ThemeInfo[] = {json.dumps(items, indent=2, ensure_ascii=False)}\n")


if __name__ == "__main__":
    good = check()
    root = Path(__file__).resolve().parents[1]
    root.joinpath("src", "themeList.ts").write_text(ts())
    root.joinpath("src", "themes.css").write_text(css())
    print("wrote src/themes.css" + ("" if good else " (fix the LOW contrasts above)"))
