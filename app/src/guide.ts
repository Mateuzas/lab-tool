/**
 * The Guide screen (FE-12): the Bug bounty workflow as static HTML in index.html. A screen, not a
 * mode: it starts and stops nothing. `w` on Programs opens it; `w` or Esc goes back to where you were.
 * Keys here: j/k scroll, 0–7 jump to a stage. Commands copy with the clipboard, like `y` in look here.
 */
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

const STEP = 64; // px per j/k
const stageEl = (n: number) => $(n === 8 ? "gd-later" : `gd-${n}`);

export function setupGuide(opts: { visible: () => boolean; back: () => void }) {
  const scroll = $("gd-scroll");
  const links = [...document.querySelectorAll<HTMLAnchorElement>(".gd-index a[data-stage]")];
  const stages = links.map((a) => stageEl(Number(a.dataset.stage)));
  const smooth = () => (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

  let jumped: number | null = null; // the stage last jumped to, marked while it is on screen

  function jump(n: number) {
    const el = stageEl(n);
    if (!el) return;
    jumped = stages.indexOf(el);
    scroll.scrollTo({ top: el.offsetTop, behavior: smooth() });
    markCurrent();
  }

  /** The index marks the stage at the top of the view. At the bottom, where the last stages can't
   *  reach the top, it marks the one you jumped to, or else the first that starts in view. */
  function markCurrent() {
    const top = scroll.scrollTop, bottom = top + scroll.clientHeight;
    const inView = (i: number) => stages[i].offsetTop >= top - 2 && stages[i].offsetTop < bottom;
    let cur = 0;
    stages.forEach((s, i) => { if (s.offsetTop <= top + 24) cur = i; });
    if (jumped !== null && inView(jumped)) cur = jumped;
    else if (top > 0 && bottom >= scroll.scrollHeight - 2) {
      const first = stages.findIndex((_, i) => inView(i));
      if (first > cur) cur = first;
    }
    links.forEach((a, i) => (i === cur ? a.setAttribute("aria-current", "step") : a.removeAttribute("aria-current")));
  }

  let ticking = false;
  scroll.addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; markCurrent(); });
  });

  for (const a of links) {
    a.addEventListener("click", (e) => {
      e.preventDefault(); // scroll the panel, not the page or the URL
      jump(Number(a.dataset.stage));
      scroll.focus({ preventScroll: true });
    });
  }

  for (const b of document.querySelectorAll<HTMLButtonElement>(".gd-copy")) {
    const code = b.parentElement!.querySelector("code")!;
    b.setAttribute("aria-label", `Copy ${code.textContent}`);
    let timer = 0;
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent ?? "");
        b.textContent = "Copied";
        b.dataset.done = "";
      } catch {
        b.textContent = "Couldn't copy";
      }
      clearTimeout(timer);
      timer = window.setTimeout(() => { b.textContent = "Copy"; delete b.dataset.done; }, 1600);
    });
  }

  $("gd-back").addEventListener("click", () => opts.back());

  document.addEventListener("keydown", (e) => {
    // defaultPrevented: the `w` that just opened the guide from Programs must not close it again.
    if (!opts.visible() || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target as HTMLElement;
    if (t.closest("input, textarea, select, [contenteditable]")) return;
    let handled = true;
    switch (e.key) {
      case "w": case "Escape": opts.back(); break;
      case "j": jumped = null; scroll.scrollBy({ top: STEP }); break;
      case "k": jumped = null; scroll.scrollBy({ top: -STEP }); break;
      default:
        if (/^[0-7]$/.test(e.key)) jump(Number(e.key));
        else handled = false;
    }
    if (handled) e.preventDefault();
  });

  markCurrent();
  return {
    /** The guide is on screen: take the keyboard so arrows, Space and PgDn scroll it too. */
    focus() {
      scroll.focus({ preventScroll: true });
      markCurrent();
    },
  };
}
