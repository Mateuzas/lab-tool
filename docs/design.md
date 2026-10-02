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
