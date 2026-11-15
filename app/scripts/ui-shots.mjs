// Screenshots of every UI scenario from the dev preview mock (src/dev/preview-mock.ts).
// Needs `npm run dev` running. Usage: node scripts/ui-shots.mjs [outDir] [light|dark] [width] [height] [scenario,...]
// Lab scenarios open in Lab mode; `programs-*` scenarios are Bug bounty's Programs screen (FE-2).
import { chromium } from "playwright";

const out = process.argv[2] ?? "ui-shots";
const scheme = process.argv[3] ?? "light";
const width = Number(process.argv[4] ?? 900);
const height = Number(process.argv[5] ?? 620);
const only = process.argv[6]?.split(",");
const lab = ["connected", "no-target", "connecting", "reconnecting", "vpn-error", "blocked",
  "disconnected", "no-files", "stale", "docker-off"];

// Programs: [mock ?p= state, what to do after it loads, what to wait for]
const programs = {
  "programs-loaded": ["loaded", null, "synced"],
  "programs-loading": ["loading", null, null],
  "programs-refreshing": ["refreshing", null, "refreshing"],
  "programs-error": ["error", null, "error"],
  "programs-changes": ["changes", async (p) => { await p.keyboard.press("c"); await p.keyboard.press("j"); await p.keyboard.press("j"); }, "synced"],
  "programs-filter": ["loaded", async (p) => { await p.keyboard.press("/"); await p.keyboard.type("financial"); }, "synced"],
  "programs-excluded": ["loaded", async (p) => { await p.keyboard.press("x"); await p.keyboard.press("G"); }, "synced"],
  "programs-mixed": ["loaded", async (p) => { await p.keyboard.press("J"); }, "synced"],
  "programs-empty": ["empty", null, "refreshing"],
  "programs-empty-error": ["empty-error", null, "error"],
  // Background sync (FE-5): 5th element = extra query
  "programs-next-sync": ["loaded", null, "synced", null, ""],
  "programs-bg-sync": ["loaded", async (p) => { await p.waitForFunction(() => !document.getElementById("pg-changes")?.hidden, null, { timeout: 8000 }); await p.waitForTimeout(200); await p.keyboard.press("c"); }, "synced", null, "&sync=1"],
  // Scope guard (FE-4): 4th element is the mock ?g= state
  "guard-off": ["loaded", null, "synced", "off"],
  "guard-starting": ["loaded", null, "synced", "starting"],
  "guard-active": ["loaded", null, "synced", "active"],
  "guard-error": ["loaded", null, "synced", "error"],
  "guard-refusals": ["loaded", async (p) => { await p.keyboard.press("R"); }, "synced", "refusals"],
  "guard-confirm": ["loaded", async (p) => { await p.keyboard.press("j"); await p.keyboard.press("j"); await p.keyboard.press("u"); }, "synced", "active"],
  // Private programs and manual targets (FE-7)
  "programs-applications": ["loaded", async (p) => { await p.keyboard.press("End"); await p.keyboard.press("k"); await p.keyboard.press("a"); }, "synced"],
  "manual-active": ["loaded", async (p) => { await p.keyboard.press("End"); await p.keyboard.press("u"); await p.waitForFunction(() => document.querySelector("tr.manual.active")); }, "synced", "off"],
  "shortcuts": ["loaded", async (p) => { await p.keyboard.press("?"); }, "synced"],
  "guard-lost": ["loaded", async (p) => { await p.waitForFunction(() => /press u/.test(document.querySelector("#pg-guard .g-text")?.textContent ?? ""), null, { timeout: 8000 }); }, "synced", "lost"],
};
const scenarios = only ?? [...lab, ...Object.keys(programs)];

const browser = await chromium.launch();
for (const s of scenarios) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 2, colorScheme: scheme });
  const page = await ctx.newPage();
  const p = programs[s];
  // The app remembers the last mode; lab shots start in Lab mode.
  await page.addInitScript((mode) => { try { localStorage.setItem("kali-lab.mode", mode); } catch {} }, p ? "bounty" : "lab");
  if (p) {
    const [state, act, wait, g, extra] = p;
    await page.goto(`http://localhost:1420/?p=${state}${g ? `&g=${g}` : ""}${extra ?? ""}`);
    if (wait === "synced") await page.waitForFunction(() => /^Synced just now/.test(document.getElementById("pg-status")?.title ?? ""));
    if (wait === "refreshing") await page.waitForFunction(() => /Refreshing 12\//.test(document.getElementById("pg-status")?.textContent ?? ""));
    if (wait === "error") await page.waitForFunction(() => !document.getElementById("pg-error")?.hidden);
    if (act) await act(page);
  } else {
    await page.goto(`http://localhost:1420/?s=${s}`);
    await page.waitForFunction(() => document.getElementById("title")?.textContent !== "Checking the lab…");
  }
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${out}/${scheme}-${width}-${s}.png` });
  await ctx.close();
}
await browser.close();
console.log(`${scenarios.length} screenshots -> ${out}/`);
