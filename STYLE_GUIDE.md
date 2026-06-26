# BTQ Style Guide

House style for every user-facing surface we ship — PWAs, dashboards, reporting apps,
demos, one-off HTML. The goal is that a screen built today looks and behaves like one
built last month, and that none of them fight the user's environment.

This guide is normative. If you deviate, say why in the PR/commit. If a rule here is
wrong, change the rule here — don't quietly ignore it in one surface.

---

## 1. First principle: respect the user, don't blind them

- **Never force a theme.** Every HTML surface honors the OS light/dark setting via
  `prefers-color-scheme`. A light-only page served to a dark-mode user (or vice versa)
  is a defect, not a style choice — it ignores their preference and is jarring when they
  move between linked pages.
- **Linked surfaces share one palette** so they match in both modes (e.g. a report and
  the demo it links to should not flip appearance).
- Accessibility is part of "done," not a later pass (see §5).

## 2. Theming: how we do light/dark

Use CSS custom properties with a **light default** and a **dark override** behind a media
query. Declare `color-scheme: light dark` so native controls, form fields, and scrollbars
adapt too. No hardcoded hex on backgrounds, text, borders, or inputs — route every color
through a token.

```css
:root{
  color-scheme: light dark;
  --bg:#ffffff;        /* page background        */
  --panel:#ffffff;     /* cards, tables, nav     */
  --soft:#f1f4f8;      /* table headers, wells   */
  --input:#ffffff;     /* inputs / selects       */
  --line:#d8dee6;      /* borders / dividers     */
  --ink:#17202a;       /* primary text           */
  --mut:#5d6875;       /* secondary text         */
  --accent:#2563eb;    /* primary action / links */
  --on-accent:#ffffff; /* text on --accent       */
  --warn:#b45309;      /* attention (e.g. To-Order) */
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f1216; --panel:#171c23; --soft:#10151c; --input:#0d1219;
    --line:#2a323d; --ink:#e8edf2; --mut:#94a3b8;
    --accent:#3b82f6; --on-accent:#ffffff; --warn:#f59e0b;
  }
}
```

This token set is the **canonical palette** — copy it; don't invent a parallel one.
Surface-specific tokens (status chips, notices, header gradients) follow the same
light-default + dark-override pattern; see the reference implementations below. Semantic
status colors keep their hue across modes (green = standardized/good, amber = attention,
red = error) and only shift lightness.

**Reference implementations** (keep these in sync if you change the palette):
- `project/admin_reporting/server.py` → `render_page()` stylesheet
- `project/admin_reporting/demo/order_sheet_demo.html` → `<style>` block

## 3. Typography & layout

- System font stack: `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
  No web-font downloads for internal tools.
- Base size 14px / line-height ~1.45. Tabular numerals (`font-variant-numeric: tabular-nums`)
  for any column of numbers (prices, quantities, spend).
- Constrain main content width (~1080–1180px) and center it; don't run tables full-bleed
  on wide monitors.
- Spacing/radius: consistent 8–24px spacing scale; 6–12px border radius. Pick from what
  the reference surfaces already use rather than introducing new values.

## 4. Components baseline

- **Tables**: 1px `--line` borders, `--soft` header row, uppercase 11–12px header labels,
  right-align numeric columns. Section header rows use `--soft` + muted ink.
- **Buttons / actions**: `--accent` background, `--on-accent` text; secondary actions use
  `--panel` background with an `--accent`/link-colored label.
- **Status chips**: small, rounded, semantic color (standard/attention/error). Keep the
  same vocabulary across surfaces.
- **Notices / callouts**: tinted `--accent` left-border card; never a bare colored block.

## 5. Accessibility (required, not optional)

- Maintain WCAG AA contrast (≥4.5:1 body text, ≥3:1 large text) in **both** themes — the
  dark override must be checked independently, not assumed.
- Interactive targets ≥ ~34px tall.
- Don't encode meaning in color alone (pair the chip color with a label/icon).
- Keyboard reachable; visible focus; real `<label>`s tied to inputs.

## 6. Responsive

- Single breakpoint around 760px is enough for internal tools: stack the page header,
  make form controls full-width, allow tables to scroll horizontally.
- Mobile-first capture surfaces (field capture) stay fast and non-blocking — visual polish
  never adds upload latency.

## 7. Before you ship a UI — checklist

- [ ] Loads correctly in **both** light and dark (toggle the OS setting and look).
- [ ] No hardcoded color hex outside the `:root` / media-query token blocks.
- [ ] Numeric columns are tabular and right-aligned.
- [ ] Contrast passes AA in both themes.
- [ ] Matches any sibling surface it links to.
- [ ] Token palette matches the canonical set (didn't fork it).

---

*Scope:* applies to all BTQ user-facing HTML. The same principles (especially §1–2) carry
to sibling repos (gregstoltz-com sites, etc.) — when in doubt, respect the system setting.
*Future:* extract the canonical tokens into a single shared `theme.css` so surfaces import
rather than copy. Until then, this file is the source of truth.
