// Screenshots of every UI scenario from the dev preview mock (src/dev/preview-mock.ts).
// Needs `npm run dev` running. Usage: node scripts/ui-shots.mjs [outDir] [light|dark]
import { chromium } from "playwright";

const out = process.argv[2] ?? "ui-shots";
const scheme = process.argv[3] ?? "light";
const scenarios = ["connected", "no-target", "connecting", "reconnecting", "vpn-error", "blocked",
  "disconnected", "no-files", "stale", "docker-off"];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 900, height: 620 }, deviceScaleFactor: 2, colorScheme: scheme });
for (const s of scenarios) {
  await page.goto(`http://localhost:1420/?s=${s}`);
  await page.waitForFunction(() => document.getElementById("title")?.textContent !== "Checking the lab…");
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${out}/${scheme}-${s}.png` });
}
await browser.close();
console.log(`${scenarios.length} screenshots -> ${out}/`);
