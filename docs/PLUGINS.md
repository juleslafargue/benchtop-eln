# Plugins

A Benchtop plugin is one small text file, written in [TOML](https://toml.io), that adds:

- **Section types**: forms with your own fields, units, dropdowns, checklists and tables, for example a glovebox log, a cell-passage record or a hydrogenation safety checklist.
- **Instrument-file readers**: a description of a CSV or text export, so Benchtop can open that file as a chart in a Characterisation section, for example an HPLC chromatogram or a UV-vis spectrum.

Plugins contain no code. Benchtop draws the form itself, checks every value against the definition, and stores plugin sections like any other section. That means they're versioned, audited, searchable and signed, and they appear in exports.

## Installing a plugin

An admin opens **Admin → Integrations** (Plugins) and clicks **Add plugin file…**. Benchtop then:

1. Shows what the file adds, or what's wrong with it.
2. Copies the file into `<data folder>/plugins/` and switches it on.

To update a plugin, add the new version of the file the same way. You can also copy `.toml` files straight into that folder.

Benchtop ships three examples in `plugins/` (a glovebox log, a hydrogenation checklist and chromatogram/UV-vis readers). They're **off** until an admin switches them on, and they're a good starting point to copy.

**Removing a plugin is safe.** Each section made from a plugin keeps a copy of the definition it was made with. Existing records stay readable, verifiable and printable; only new sections of that type can no longer be added. Changing a plugin later doesn't alter sections already made with the old version.

## The file

```toml
[plugin]
id = "glovebox-log"          # lowercase letters, digits and dashes; unique
name = "Glovebox log"
version = "1.0.0"
author = "Ruiz group"
description = "Atmosphere readings and transfers for air-sensitive work."
api = 1                      # the plugin format version; this Benchtop understands 1
```

A file has one `[plugin]` table, then any number of `[[sections]]` and `[[readers]]`.

## Section types

```toml
[[sections]]
id = "glovebox"                  # unique within the plugin
name = "Glovebox log"            # shown in the "Add section" menu
title = "Glovebox log"           # default title of a new section
description = "O₂ and H₂O levels and items transferred"

  [[sections.fields]]
  id = "o2"
  label = "O₂"
  type = "number"
  unit = "ppm"
  min = 0
  warn_above = 5
  help = "Above 5 ppm: regenerate before handling pyrophorics."
  required = true
  width = "third"

  [[sections.tables]]
  id = "transfers"
  label = "Items in and out"
  min_rows = 0

    [[sections.tables.columns]]
    id = "item"
    label = "Item"
    type = "text"
```

### Field options

| Option | Applies to | Meaning |
|---|---|---|
| `id` | all | Lowercase letters, digits, `_` or `-`; unique within the section |
| `label` | all | What people see |
| `type` | all | `text`, `longtext`, `number`, `select`, `checkbox`, `date`, `datetime` |
| `required` | all | Must be filled in (a checkbox must be ticked) **before the author can sign** |
| `help` | all | A short hint under the field |
| `placeholder` | text, longtext, number | Grey example text |
| `default` | all but longtext | Starting value |
| `width` | fields | `third`, `half` (the default) or `full` (the default for `longtext`) |
| `unit` | number | Shown after the box and in exports |
| `min`, `max` | number | Out-of-range values are flagged |
| `warn_below`, `warn_above` | number | A warning (with your `help` text) when crossed |
| `options` | select | The list of choices |

Checkbox fields are drawn as a checklist and count towards "*n* of *m* ticked".

### Tables

A table has an `id`, a `label`, optional `help` and `min_rows`, and a list of `[[sections.tables.columns]]`. Columns take the same options as fields, but only the types `text`, `number`, `select`, `checkbox` and `date`. A required column must be filled in on every row that has anything in it. `min_rows` sets how many rows are needed before signing.

## Instrument-file readers

```toml
[[readers]]
id = "chromatogram-csv"
name = "Chromatogram (time, signal)"
technique = "HPLC"                 # filled into the Characterisation row
conditions = ""                    # optional default conditions
kind = "other"                     # other | uv | ir | ms (ms draws sticks)
extensions = [".csv", ".txt"]
filename = "(?i)(hplc|uplc|chrom)" # used when the file name matches…
detect = ""                        # …and/or when the first 4 KB of the file match
data_after = ""                    # data starts after the first line matching this
header = false                     # the first data line names the columns
delimiter = "auto"                 # auto, ",", ";", "tab", "|" or "whitespace"
decimal = "."                      # "," for European exports (1,25)
x = 1                              # column number (from 1) or header name
y = 2
x_label = "Retention time (min)"
y_label = "Signal (mAU)"
x_scale = 1.0                      # multiply values, e.g. 0.001 for µL → mL
y_scale = 1.0
reversed = false                   # draw the x axis high → low (IR, NMR)
dips = false                       # peaks point down (transmittance)
```

A reader needs a `filename` pattern, a `detect` pattern or both, so it isn't tried on every file. When both are given, both must match. Patterns are regular expressions; `(?i)` at the start makes them ignore case. Readers are only tried on files that aren't JCAMP-DX or Bruker zips, which Benchtop already understands. If a reader fails on a file, the message names the reader and what it couldn't find.

## Good to know

- **Only admins** can add, switch on or remove plugins. Everyone can use the section types of switched-on plugins.
- Adding, enabling, disabling and removing a plugin are recorded in the audit trail, with the file's SHA-256.
- Files are limited to 200 KB; definitions are checked thoroughly, and a file with a mistake is listed with the reason instead of being loaded.
- The plugin format is versioned (`api = 1`). If a future Benchtop changes it, it will say which files need updating rather than misreading them.
