import { useEffect, type MouseEvent, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useApp } from '../App'

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

/** The parts of the guide, in order: the contents list and the page are both made from this. */
export const GUIDE_SECTIONS = [
  { id: 'notebooks', title: 'Notebooks and experiments' },
  { id: 'reactions', title: 'Reactions and amounts' },
  { id: 'writing', title: 'Writing up' },
  { id: 'instruments', title: 'Sample codes and instrument data' },
  { id: 'spectra', title: 'Spectra' },
  { id: 'safety', title: 'Safety' },
  { id: 'signing', title: 'Signing and witnessing' },
  { id: 'finding', title: 'Finding things' },
  { id: 'sharing', title: 'Sharing and groups' },
  { id: 'account', title: 'Your account' },
  { id: 'problems', title: 'If something goes wrong' },
] as const
type Id = typeof GUIDE_SECTIONS[number]['id']

/** Go to a part of the page without a new history entry (Back still leaves the guide), and keep the address linkable. */
function jump(id: string, e?: MouseEvent) {
  e?.preventDefault()
  const el = document.getElementById(id)
  if (!el) return
  el.scrollIntoView({ block: 'start' })
  try { history.replaceState(history.state, '', `#${id}`) } catch { /* ok */ }
}

/** Getting started: how to use Benchtop, for someone who has just signed in. Every claim here is about what the app does now. */
export default function Guide() {
  const { notebooks, canWrite, user } = useApp()
  const K = isMac ? '⌘K' : 'Ctrl K'
  const pref = user.preferences?.default_notebook_id
  const nb = notebooks.find(n => n.id === pref) || notebooks[0]
  useEffect(() => {  // opened at /guide#signing: go to that part
    const id = decodeURIComponent(location.hash.slice(1))
    if (id && GUIDE_SECTIONS.some(s => s.id === id)) window.setTimeout(() => jump(id), 0)
  }, [])

  const body: Record<Id, ReactNode> = {
    notebooks: <>
      <p>A notebook holds experiments, usually one per project. The notebooks you can open are listed in the sidebar (under Menu on a phone)
        {canWrite ? <>; <b>+ New</b> beside the heading makes another</> : null}.{nb && <> <Link to={`/notebooks/${nb.id}`}>Open {nb.name}</Link></>}</p>
      <p>Each experiment gets a number, such as EXP-00042, that stays with it for good. Press <kbd>N</kbd> anywhere, or
        click <b>New experiment</b>, to start one: give it a title, choose the notebook and, if you like,
        a <Link to="/templates">template</Link> to start from.</p>
      <ul>
        <li>A new experiment has the usual sections ready: Aim, Reaction, Safety &amp; risk assessment, Procedure,
          Work-up &amp; purification, Characterisation, and Observations &amp; results.</li>
        <li><b>Add section</b> (at the top, between two sections, or at the end) adds text, a data table, files,
          references, a screening grid and more. Remove the sections you don't need; removing one offers <b>Undo</b>.</li>
        <li>Sections save themselves a moment after you stop typing. Each save is a new version (v1, v2…),
          and <b>History</b> on a section shows the earlier ones.</li>
        <li><b>Duplicate</b>, <b>Repeat at another scale…</b>, <b>Save as template</b> and <b>Download PDF</b> are
          under <b>Record</b>, beside the experiment (below it on a phone).</li>
      </ul>
    </>,
    reactions: <>
      <p>In the <b>Reaction</b> section, click <b>Draw reaction</b> and draw the scheme. Benchtop fills the table from
        the structures, with formulas and molecular weights. <b>Add from registry</b> adds a reagent or solvent from
        the lab's <Link to="/materials">materials</Link>, or finds one on PubChem.</p>
      <ul>
        <li>Choose the limiting reagent with the dot in the <b>Lim.</b> column, then type one amount in any row: mass,
          volume, mmol or equivalents. The rest is calculated, allowing for purity, density and concentration.</li>
        <li>The cell you typed is highlighted, so you can tell what you entered from what was worked out. Amounts
          take units: <code>1.2 g</code> in a mg cell, <code>250 µL</code> in a mL cell.</li>
        <li>Above the table, fill in the temperature (or rt, reflux), time, atmosphere and target concentration.
          Tick <b>fill to target M</b> on a solvent and its volume is worked out for you.</li>
        <li>Type the isolated mass under the product to get the yield.</li>
        <li>When the lab has run something similar, <b>Have we done this before?</b> under the table shows it, with
          the yield; <b>Use these conditions</b> copies its reagents and conditions into your table.</li>
      </ul>
    </>,
    writing: <>
      <p>Text sections work like a word processor: bold, sub- and superscript, tables and checklists. Paste or drop a
        picture (a screenshot, a photo of a TLC plate) straight into the text; it's saved with the record. Typing an
        experiment number such as EXP-00025 makes a link to it.</p>
      <p>At the bench, on an experiment's page:</p>
      <ul>
        <li><kbd>L</kbd> logs a line with the time (“14:32 added NaH portionwise”) to the experiment's Bench log
          section, without scrolling to it.</li>
        <li><kbd>T</kbd> starts a timer. It runs in the sidebar and rings when it's done, and the bench log records it.
          Typing “30 min stir” in the log starts one too.</li>
        <li><kbd>P</kbd> shows a QR code that opens the experiment on your phone, already signed in, to take photos, log
          notes and run timers. The phone has to be able to reach Benchtop (usually the same network); the
          dialog tells you if it can't.</li>
        <li><b>Draft write-up</b> turns the reaction table, conditions, work-up and characterisation into an
          experimental paragraph to copy or add as a section. It lists what's missing rather than guessing.</li>
      </ul>
    </>,
    instruments: <>
      <p>If your lab's instruments are connected to Benchtop (an admin sets this up), data finds its own way into your
        experiments:</p>
      <ol>
        <li>Choose your initials once, in <Link to="/account">Account settings → Sample codes</Link> (for example JL).</li>
        <li>Before you go to the instrument, click <b>New sample code</b> on the experiment. Write the code (JL-013) on
          the vial and type it as the sample name on the NMR or LC-MS. The experiment number (EXP-00042) works too.</li>
        <li>Once the instrument has saved the data, it's added to that experiment's Instrument data section and
          you get a notification.</li>
      </ol>
      <p>Use one code per sample (the crude and the pure product get one each), and keep the dash: JL-013 works,
        jl13 doesn't.</p>
      <p>Anything Benchtop can't place for certain (no code, a typo, two numbers) waits in
        the <Link to="/inbox">Inbox</Link> with a note saying why; it never guesses. Assign it to a draft from there.
        If someone else's file lands in your experiment, click <b>Not mine</b> on it and it goes back to the Inbox.</p>
      <p>Data only goes into drafts. Results that come back after you've signed (HRMS, elemental analysis) go into
        a <b>follow-up</b>: a new draft linked to the signed record. Make one with <b>Make a follow-up…</b> on the
        record's page, or from the Inbox.</p>
    </>,
    spectra: <>
      <p>Drag spectrum files onto an experiment's page: JCAMP-DX, a zipped Bruker experiment folder (processed, or just
        the raw FID straight off the spectrometer) or a two-column CSV. Each becomes an item in Characterisation with
        the technique, frequency and solvent filled in. Other files go into a Files section.</p>
      <ul>
        <li>¹H spectra open already analysed: signals found, integrated and scaled to the product's formula, with
          solvent, water and TMS lines left out. <b>Auto-analyse</b> runs it again. Check the result before you use it.</li>
        <li>Referencing: if the solvent residual is off, a note offers to reference to it in one click. Or choose
          <b> Reference</b> (<kbd>R</kbd>) and click a peak to set its shift.</li>
        <li>Integrating: choose <b>Integrate</b> (<kbd>I</kbd>) and drag across a signal. Type a proton count in the
          signals table to make that signal the reference. You can change any multiplicity or J there too.</li>
        <li>Scroll to zoom, or drag in <b>Zoom</b> mode (<kbd>Z</kbd>); <kbd>F</kbd> or a double-click shows the
          whole spectrum again.</li>
        <li>If the reaction has a drawn product, its predicted shifts are shown above the spectrum to compare with.</li>
        <li><b>Use as characterisation data</b> writes the signal list as text, <b>Export PNG</b> gives a picture for
          SI or slides, and <b>Compare…</b> overlays other spectra. Your changes are saved with the record.</li>
      </ul>
    </>,
    safety: <>
      <p>Each new experiment has a <b>Safety &amp; risk assessment</b> section. It reads the reaction table and shows
        every chemical's pictograms, signal word and hazard statements (from PubChem's public classifications), warns
        about known dangerous combinations such as NaH in DMF, and suggests controls.</p>
      <ul>
        <li>Fill in controls and PPE, waste disposal, and emergency and spills.</li>
        <li><b>Record hazards in notebook</b> saves what's known now into the record, so the signed record keeps it.
          If the reaction or a classification changes afterwards, a banner says so.</li>
        <li>The supplier's safety data sheet (SDS) takes precedence, and your lab's own safety rules still apply.</li>
      </ul>
    </>,
    signing: <>
      <p>When an experiment is finished, click <b>Sign as author</b> and enter your password. Signing locks the record:
        nobody can change it. You can ask a particular colleague to witness it, and they get a notification.</p>
      <ol>
        <li><b>Signed</b>: locked and waiting for a witness. Until it's witnessed, you can <b>Withdraw signature</b> to
          make changes; the withdrawal stays in the history.</li>
        <li>A colleague who can open the record (anyone but its author) reads it, then clicks <b>Witness</b> and
          enters their password, or clicks <b>Return to author</b> with a reason. A returned record is a draft again,
          to correct and sign again.</li>
        <li><b>Witnessed</b>: final. A witnessed record never changes. Late results go into a follow-up. A record
          that went wrong can be voided with a reason; it's kept, marked as voided, never deleted.</li>
      </ol>
      <p>Comments don't change a signed record: they aren't part of what was signed, so a record can be discussed
        during review or long after. Records waiting for your witness signature are listed first on
        the <Link to="/">dashboard</Link>.</p>
    </>,
    finding: <>
      <ul>
        <li><kbd>{K}</kbd> finds any experiment (by number or title), material (by name or CAS number), notebook or
          page, and runs actions such as New experiment. A few letters of each word are enough, in any order.</li>
        <li><Link to="/search">Search</Link> looks through everything: titles, tags, section text, reagents,
          conditions, tables and file names. Every word must match; put an exact phrase in “quotes”. It also searches
          by structure (substructure or similar structures).</li>
        <li><Link to="/reactions">Reactions</Link> puts every reaction you can open in one table, with conditions and
          yields, to filter and sort.</li>
        <li><kbd>?</kbd> lists every keyboard shortcut.{' '}
          <button className="btn ghost sm" onClick={() => window.dispatchEvent(new Event('benchtop:shortcuts'))}>Show the shortcuts</button></li>
      </ul>
    </>,
    sharing: <>
      <p>Each notebook is open to everyone with a Benchtop account, to one <Link to="/groups">group</Link>, or only to
        its owner. Group leads (a PI, say) see all of their group's notebooks. The owner changes this
        with <b>Change who can open it</b> on the notebook's page.</p>
      <ul>
        <li>The author can share a single experiment with anyone, to view or to edit, with <b>Share…</b> on it. Only
          the author signs it.</li>
        <li>Records you can't open don't appear anywhere for you: not in lists, search or suggestions.</li>
        <li>In <b>Discussion</b> beside an experiment, or on a single section, type @ and a name to bring a colleague
          in; they get a notification.</li>
      </ul>
    </>,
    account: <>
      <p>Open <Link to="/account">Account settings</Link> from your name at the bottom of the sidebar.</p>
      <ul>
        <li><b>Profile</b>: your full name is what your signatures show. Records you've already signed keep the name
          you had then.</li>
        <li><b>Sample codes</b>: your initials, for codes like JL-013.</li>
        <li><b>Sign-in</b>: if you sign in with single sign-on (your university account), set a signing password here;
          you need it to sign and witness records.</li>
        <li><b>Email</b>: which emails Benchtop sends you. You always get the same news as notifications, under the
          bell.</li>
        <li><b>Preferences</b>: light or dark, five colour themes, the notebook new experiments go into, and whether
          spectra open full-screen. They follow you to any computer.</li>
        <li><b>Customise</b> on the dashboard rearranges it, and <b>Customise menu</b> in the sidebar reorders or hides
          its links.</li>
      </ul>
    </>,
    problems: <>
      <ul>
        <li>If Benchtop can't save (it was stopped, the Wi-Fi went, your sign-in ran out), the section says <b>Not
          saved</b> and your typing is kept in this browser. Open the record again on the same computer and the section
          offers to put it back. Nothing is put back without asking.</li>
        <li>Removed a section by mistake? The message that appears offers <b>Undo</b>. <b>History</b> on a section keeps
          every saved version.</li>
        <li>A signed record is never edited or deleted. If one is wrong, correct it in a follow-up, or void it with
          a reason.</li>
        <li>For anything else (a forgotten password, a notebook you should be able to open, an instrument), ask your
          Benchtop admin.</li>
      </ul>
      <p className="faint">Admins have their own Admin tab for accounts, backups, instruments and email. Nothing in this
        guide needs it.</p>
    </>,
  }

  return (
    <div className="stack guide" style={{ gap: 22 }}>
      <div>
        <h1 className="page-title">Getting started</h1>
        <p className="lede">How to use Benchtop day to day. It takes about five minutes to read, and each part links to
          the page it describes.</p>
      </div>
      <nav className="panel guide-toc" aria-label="Contents">
        <h2>Contents</h2>
        <ol>{GUIDE_SECTIONS.map(s => <li key={s.id}><a href={`#${s.id}`} onClick={e => jump(s.id, e)}>{s.title}</a></li>)}</ol>
      </nav>
      {GUIDE_SECTIONS.map(s => (
        <section key={s.id} id={s.id} className="guide-sec" aria-labelledby={`${s.id}-h`}>
          <h2 id={`${s.id}-h`}>{s.title}</h2>
          {body[s.id]}
        </section>
      ))}
      <p className="faint guide-end"><a href="#top" onClick={e => { e.preventDefault(); window.scrollTo(0, 0); try { history.replaceState(history.state, '', location.pathname) } catch { /* ok */ } }}>Back to the contents</a></p>
    </div>
  )
}
