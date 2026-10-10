# Lab Tool UI design

Living record of design decisions, so later passes stay consistent (and Matas can overrule them).

## Brief

- **Subject:** a desktop console that connects a complete beginner to their HackTheBox /
  TryHackMe lab safely: VPN, kill switch, target scope, tools.
- **Audience:** people who have never used a VPN file, a terminal or Kali, and are nervous
  about "doing hacking wrong".
- **Primary job of the main screen:** get connected in one click, and always know, in plain
  words, what is connected to what and whether it is safe.

## Pass 1 (2026-10-02): status + connect + VPN picker + target

### Palette (light / dark)

| Token | Light | Dark | Role |
|---|---|---|---|
| `--paper` | `#EEF1F4` | `#151B25` | window background (cool slate, not cream, not black) |
| `--ink` | `#1B2433` | `#E4E9F0` | text, the route line when idle |
| `--ink-soft` | `#5B6678` | `#98A3B5` | secondary text |
| `--rule` | `#C9D0DA` | `#2E3747` | idle route, field borders |
| `--go` | `#17845F` | `#3FB68B` | connected / protected |
| `--wait` | `#B7791F` | `#E0A743` | connecting / reconnecting |
| `--stop` | `#B3362B` | `#E5685C` | problem / blocked |

Signal colours are only used on the route and the status headline, never as decoration.

### Type

**Atkinson Hyperlegible Next** for everything; **Atkinson Hyperlegible Mono** only for IP
addresses and server names (digits line up, 0/O and 1/l are unambiguous, which matters when a
beginner copies an IP). Both are from the Braille Institute, designed for legibility: the type
choice *is* the brief (ease of use for beginners). Bundled locally (offline app, strict CSP).
Scale: 13 / 15 / 18 / 34 px. Status headline 34 px, weight 700, tight tracking.

### Layout

```
 Connected to HTB                                    <- status headline, the loudest text
 Connected. Target: 10.129.15.9                      <- supervisor's plain message

 This Mac ━━━ Kill switch ━━━ VPN ━━━ HTB lab ━━━ Target      <- THE ROUTE (hero)
                                       89 ms        10.129.15.9

 Lab network  [ Starting Point (EU 2)        v ]   ( Disconnect )
 Target       [ 10.129.15.9                    ]   ( Set target )

 Your lab IP 10.10.15.248
```

Left-aligned, one column, max ~640 px, no cards and no shadows: structure comes from spacing
and the route line. Below 600 px the route turns vertical.

### Principles

1. **The route is the memorable thing.** It teaches what the app does (your Mac reaches the lab
   only through the kill switch and VPN) and shows *where* a problem is: a broken segment sits
   exactly where the connection failed. Everything else stays quiet.
2. **One motion only:** the VPN segment flows while connecting/reconnecting. Nothing else
   animates on its own. Reduced motion respected.
3. **Words a beginner understands.** "Lab network", not "VPN profile"; "Target", not "scope".
   Errors say what happened and what to do.
4. **One action at a time.** A single primary button whose label always says what it will do:
   Connect / Disconnect / Switch network.

### Review against generic defaults

- Generic for this brief would be the "hacker" look: near-black, neon green, monospace
  everywhere, terminal chrome. Rejected: it intimidates the exact audience, and it's HTB's
  own branding. Chose cool slate paper + ink with signal colours.
- Generic layout would be a SaaS card grid of stats (IP / latency / server). Rejected: replaced
  by the route diagram, which carries the same data in a form that explains itself.
- Mono for small labels is a default tell; here mono is restricted to IPs/hosts, where it does
  a real job.

### Self-critique after building (screenshots of all 10 states, light + dark)

Fixed in pass 1:
- The VPN label ran into the next hop's "89 ms" (read as one string): added a column gap.
- Notes shifted when one wrapped: `align-content: start` on each hop.
- "(EU 2)" broke inside the bracket: label keeps "(EU 2)" together.
- Mono was used for words ("Not set", "blocked", "89 ms"): mono now only for real IPs.
- "Connected" said twice (headline + message): headline "Connected to the lab"; the message
  now teaches scope: "You can reach X. Everything else is blocked, so a typo can't hit a real
  computer."
- Docker off showed "No lab network yet, download the file", which was wrong advice: controls
  are hidden whenever the lab is unreachable; the headline is the only instruction.

Tools: `make app-shots` renders every state from the dev preview mock
(`app/src/dev/preview-mock.ts`, `?s=<state>` in the browser) with Playwright.

Open for Matas's pass: window chrome/title bar, the empty space below the controls (where the
terminal will go), an app icon, the VPN-file import flow (a file dialog instead of "put it in
the vpn folder").

## Pass 2 (2026-10-02): client palette (dark only) + built-in terminal

### Palette (client-specified; light theme comes later)

| Token | Value | Share | Role |
|---|---|---|---|
| `--bg` | `#0B090C` Deep Black | 60% | window |
| `--surface` | `#161219` Surface Dark | 60% | terminal panel, fields |
| `--text` | `#F8F9FA` Soft White | 30% | text |
| `--text-soft` | Soft White at 62% | 30% | secondary text |
| `--line` | Soft White at 14% | 30% | borders, idle route |
| `--accent` | `#8A2BE2` Vibrant Purple | 10% | connected/working: route, headline, focus |
| `--alert` | `#FF2A85` Neon Pink | 10% | the focal button (Connect) and alerts/broken hops |

Contrast on `--bg`: purple ~3.3:1 (OK for large text and graphics only, so never small text);
pink ~5.6:1 (OK for text). With two accents, "connecting" is told apart by pattern, not
colour: solid purple line = connected, flowing purple dashes = connecting.

### Layout with the terminal

```
 Connected to the lab
 You can reach 10.129.15.9. Everything else is blocked, so a typo can't hit a real computer.
 ●━━━━●━━━━●━━━━●━━━━●   This Mac / Kill switch / VPN / HTB lab / Target
 Lab network [Starting Point (EU 2) v] (Disconnect)    Target [ip        ] (Set target)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ ┌──(root㉿kali)-[/lab]                                                     │  <- terminal fills
 │ └─# _                                                                      │     the rest
 └──────────────────────────────────────────────────────────────────────────┘
```

The terminal is the workspace, so it takes all remaining height. Controls sit on one row at
normal widths. The route stays the memorable element; the terminal is quiet (Surface Dark,
no border glow).

### Security trade-off (CSP), to apply when the terminal UI lands

Not applied yet: the CSP stays strict until the terminal frontend exists. When it lands:
xterm.js 6 creates `<style>` tags at runtime and has no nonce option. `style-src` now allows
`'unsafe-inline'`, and Tauri's style nonce injection is disabled
(`dangerousDisableAssetCspModification: ["style-src"]`), because a nonce would make browsers
ignore `'unsafe-inline'`. Scripts stay locked to the app's own nonce'd files; the residual risk
is CSS injection (can restyle, cannot run code). Revisit if xterm adds nonce support.

### Pass 2 status

- Palette applied (dark only). Self-critique: idle route and field borders were ~1.4:1 on Deep
  Black, too faint for meaningful graphics; raised `--rule` to `#606064` (3:1).
- Terminal: backend done (`app/src-tauri/src/term.rs`: `term_open(cols, rows, onEvent)`,
  `term_write(id, data)`, `term_resize(id, cols, rows)`, `term_close(id)`; events
  `{event:"data", data:[bytes]}` / `{event:"exit"}`). Frontend: see Pass 4.

## Pass 3 (2026-10-02): responsive layout

Problem: at full screen everything sat in the top-left corner (fixed 680 px column, no centring).

- Content column centred: `width: min(1120px, 100% - 2 * gutter)`, gutter `clamp(20px, 5vw, 72px)`.
  Text stays left-aligned inside the column (easier to read than centred text).
- Fluid scale: headline `clamp(30px, 1.6vw + 18px, 50px)`, lead text and route spacing scale
  similarly; top padding scales with window height (`clamp(28px, 7vh, 88px)`).
- Wide windows (>= 1000 px): the two control rows sit side by side.
- Top-anchored on purpose: the space below the controls is reserved for the terminal.
- Breakpoints kept: controls stack < 720 px, route turns vertical < 600 px.
- Self-critique after building (screenshots at 1920, 1470, 900, 680 px): only the headline
  scaled at first, so body text and controls looked tiny at full screen; body, small text,
  route pins and control height (`--control-h`) now scale with `clamp()` too. Row labels
  drifted when a row grew taller (multi-line hint); labels and hint now align to the first
  control line instead of the row's centre.

## Pass 4 (2026-10-02): terminal frontend

- `app/src/terminal.ts` (xterm.js + fit addon over the `term_*` commands). Opens a shell as soon
  as the lab container is reachable (no extra click); when the lab goes away the panel is covered
  with "The terminal opens when the lab is running."; when the shell exits it says "This terminal
  has ended." with an "Open a new terminal" button.
- Panel is `.term-panel` (not `.terminal`: xterm adds that class to its own element and the
  styles doubled up). Surface Dark, hairline edge, border turns purple while focused (the only
  focus indicator; xterm's inner outline is removed so it isn't doubled).
- Terminal colours: Soft White on Surface Dark, Neon Pink cursor, purple selection; ANSI blues
  lifted so the Kali prompt is readable.
- CSP changed as planned above (`style-src 'unsafe-inline'`, `dangerousDisableAssetCspModification`).
- Preview mock has a fake shell (echo, `ls`, `exit`) so the UI can be designed in a browser.
- Not yet verified: inside the real Tauri window against the live container (CSP, Docker exec
  resize, paste). Run `make app-dev` with the lab up.

## Pass 5 (2026-10-02): tabs + black/white terminal

- Tabs **Lab** (route + controls) and **Terminal**, under a status headline that is always
  visible so "is it safe?" is answered on both. Plain-word tabs on a hairline, purple underline
  on the active one; arrow keys / Home / End switch tabs. The terminal stays mounted while
  hidden, so the shell keeps running when you switch to Lab.
- Terminal is plain black on white text, white cursor and selection (client request, overrides
  the Surface Dark panel from Pass 4). ANSI colours stay (lifted) so the Kali prompt reads.

## Pass 4 (2026-10-02): Argo CD palette

Client direction: Argo CD's colours. Source: `argoproj/argo-ui` `src/styles/config.scss`.
Argo CD's signature is light grey content with teal actions; its status colours (Healthy green,
Progressing blue, Degraded red, Missing grey) map directly onto the route's hop states.

| Token | Argo variable | Value | Role |
|---|---|---|---|
| `--paper` | `$argo-color-gray-2` | `#EFF3F5` | window |
| `--field` | `$white-color` | `#FFFFFF` | fields |
| `--ink` | `$argo-color-gray-8` | `#363C4A` | text |
| `--ink-soft` | `$argo-color-gray-6` | `#6D7F8B` | secondary text |
| `--rule` | `$argo-color-gray-5` | `#8FA4B1` | borders, idle route (Missing/Unknown) |
| `--go` | `$argo-success-color-dark` | `#3F946D` | connected (Healthy) |
| `--wait` | `$argo-running-color-dark` | `#378398` | connecting (Progressing), flowing |
| `--stop` | `$argo-failed-color-dark` | `#C04B4F` | broken (Degraded), alerts |
| `--cta` | `$argo-color-teal-7` | `#006F8A` | main button (white text) |

Deviation: Argo's bright status colours (#18BE94, #0DADEA, #E96D76) are ~2-2.5:1 on the light
background, below 3:1 for meaningful graphics; Argo's own `-dark` variants are used instead.
Teal-7 rather than teal-6 for the button so white text passes 4.5:1.

**Terminal (when built): plain black and white**, background `#000000` (`$argo-color-gray-9`),
text `#FFFFFF`, not themed with the app palette (client decision).

## Pass 5 (2026-10-02): minimal home + Burp and Wireshark tabs

Problem (client): the home screen felt all over the place: headline, tabs, route, two labelled
forms and an IP line stacked with no single focal point; the tab hairline still used a
dark-palette colour (invisible on Argo's light background).

```
 Lab   Terminal   Burp   Wireshark                         ● Connected
 ───────────────────────────────────────────────────────────────────────
        Connected to the lab
        You can reach 10.129.15.9. Everything else is blocked.
        ●━━━━━━●━━━━━━●━━━━━━●━━━━━━●
        [ Starting Point (EU 2) v ][ Disconnect ]  [ 10.129.15.9 ][ Set ]
```

- One top bar on every tab: tabs left, a status dot + one word right (visible from any tab).
- Lab tab = one centred block (vertically too): status, route, one control strip. Visible
  field labels removed (fields keep aria-labels + placeholders); lab IP moves onto the route.
- Burp / Wireshark tabs share one pattern: what the tool is for (one sentence), its state, one
  button. Not installed -> "Get Burp/Wireshark" opens the official download page.
- Terminal stays plain black and white (pass 4 decision).
- Built (2026-10-02): top bar with tabs + status chip; Lab tab as one centred block with a
  label-free control strip; lab IP shown on the route under "This Mac"; Burp and Wireshark tabs
  (`app/src/tools.ts`, backend `app/src-tauri/src/tools.rs`). Self-critique: a disabled
  "Watching" button looked broken, so the button is hidden while Wireshark runs and the green
  state line carries the message. Checked at 1470 and 640 px, all tool states (missing, ready,
  running).

## Pass 6 (2026-10-02): Claude tab

Same pattern as the Burp/Wireshark tabs: heading, one sentence, state, one button, tip.
"Open tutor" opens a macOS Terminal window running Claude Code in `tutor/` (the Task 5
sandbox: reads the command log, lab status and Burp history; cannot run commands or edit
files). Model = whatever the user's Claude Code is set to; the app does not pin one.
Not installed -> "Get Claude Code" (fixed official URL). Dev-only for now: the tutor folder
path is the repo's; a packaged app will need its own copy of the tutor config.

## Pass 6 (2026-10-02): several terminals + pop-out

- Terminal tab has its own tab strip ("Terminal 1", "Terminal 2", each with a close ×) and two
  plain-word buttons: **New terminal**, **Pop out**. Active tab is filled black so it reads as
  part of the black panel. Switching tabs keeps each shell and its history.
- **Pop out** opens that shell in its own OS window (`term-<id>`, loads `index.html?popout=<id>`),
  so it can be dragged anywhere. The window shows only the terminal and **Move back to the app**;
  closing the window with the X does the same. The tab returns to the app with its history.
- Backend (`term.rs`): a shell no longer belongs to one window. Its output channel is swappable
  (`term_attach`) and the last 256 KB is kept and replayed into whichever window attaches, so
  moving a terminal doesn't blank it. Popped-out shells survive the main window's tab; quitting
  the app (closing the main window) closes pop-outs and ends the shells.
- An ended shell says "This terminal has ended." with Close this tab / Close window.
- Verified in the browser preview (mock shells; the mock's pop-out is a fresh shell, since the
  real hand-over happens in Rust). `cargo check` passes. Not yet run in the real app.

## Pass 7 (2026-10-02): add a VPN file from the app

- No files: hint "Download your VPN file from HTB (Connect to HTB, then OpenVPN), then add it
  here." and the main button becomes "Add VPN file" (the only next step).
- Files exist: strip unchanged; a small text button "Add a VPN file" under the list.
- After adding: new file selected; a short confirmation "Added <label>." (role=status).
- Plain-word errors from the backend. The file is read in the webview (file picker) and sent as
  text; no filesystem permission is granted to the frontend.
- Built: "Add VPN file" (empty state main button) and "Add a VPN file" text button; confirmation
  and errors reuse the strip's status line. Self-critique: the text button inherited the strip's
  button min-width (looked indented); selection now only changes to a file that is listed.

## Pass 7 (2026-10-02): rename, tab overflow, button roles

- Rename a terminal: double-click its tab or press F2; Enter keeps, Escape cancels, max 24
  characters. A pop-out window keeps the name it had when popped out.
- The tab strip is one line that scrolls sideways when full (active tab kept in view); the two
  buttons never move. Before, tabs wrapped and pushed the buttons around.
- **New terminal** is the filled primary button with a plus; **Pop out** is an outline button
  with a leave-the-box arrow, so the two read as different kinds of action.
- Each terminal is its own `lab-shell` process (own transcript and own `LAB_SESSION`). Fixed:
  the transcript name was timestamp-to-the-second only, so two terminals opened in the same
  second wrote to the same file; it now ends in the shell's pid. Needs an image rebuild
  (`make build`) to take effect.

## Pass 8 (2026-10-10): two modes, Lab and Bug bounty

Idea (client): like a trading platform's training vs real account. Two separate "apps" in one
window. Only one runs at a time; launching one fully stops the other, so containers, proxies and
VPNs can never collide. Each has a Launch button and its own accent colour.

```
 ▪ Lab  Running   ▪ Bug bounty  Stopped          <- dark mode strip (always visible)
 ───────────────────────────────────────────
 Lab  Terminal  Burp  Wireshark  Claude    ● Connected      <- the running mode's own tabs
```
Viewing is not running: clicking a mode shows it; its launch screen (heading, one sentence,
state, one button, one tip: the Burp/Wireshark pattern) appears until that mode is the one
running. A stopped lab shows the Lab launch screen; Bug bounty has no app yet, so it is always
a launch screen ("work in progress").

- **Identity by colour:** Lab accent stays Argo teal `#006f8a`; Bug bounty is violet `#6b46c1`
  (white text 6.4:1). The accent token `--cta` is swapped per mode, so buttons, the active tab,
  focus rings and the terminal focus border all change together. Status colours (green/blue/red)
  keep their meaning in both modes.
- **Strip colours:** dark slate `#363c4a` (Argo gray-8) with lighter swatches (teal `#1fbdd0`,
  violet `#a995f0`) so both stay ≥4:1 on it. Active mode: white text + 3px underline in its colour.
- **Stopping a running lab asks first** (inline, not a pop-up): "This stops the lab: its VPN
  connection, its proxy and any open lab terminals." Stopping Bug bounty (nothing to lose yet) doesn't.
- Launching Lab restarts the container; the supervisor reconnects the last VPN and target.
- Review against defaults: a tab strip of identical pills would have been the default;
  the dark strip + coloured swatch reads as "which environment am I in", which is the job.
- Built (2026-10-10): mode strip + launch screens (`app/src/modes.ts`), `start_lab` / `stop_lab`
  commands (lab.rs). Self-critique from screenshots of every step (running lab -> view bug
  bounty -> confirm -> stopping -> running -> stopped lab -> relaunch, plus 560 px): flow correct;
  the preview mock defined its "stopped" scenario too late (fixed). A test keeps the UI's
  "Lab stopped" match in step with the Rust status title (negative control verified).
- Known limits: Bug bounty has no backend (its "running" is a UI flag); Burp stays open when the
  lab stops (it's the user's own project, we don't close it) but its proxy now points at nothing;
  the mode on screen isn't remembered across app restarts (always opens on Lab).
