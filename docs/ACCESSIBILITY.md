# Section 508 conformance notes and test plan

Target: **Revised Section 508 (36 CFR 1194, which incorporates WCAG 2.0 AA)**, designed to **WCAG 2.2 AA**.

## Built-in techniques

| WCAG SC | Implementation |
|---|---|
| 1.1.1 Non-text content | No informative images. Decorative brand mark is `aria-hidden`. |
| 1.3.1 Info & relationships | Semantic landmarks; `<fieldset>/<legend>` for groups; tables with `<caption>`, `th scope`; labels with `for`; hints and errors via `aria-describedby`. |
| 1.3.5 Input purpose | `autocomplete` on name, email, phone, username and password fields. |
| 1.4.1 Use of color | Status tags, alerts and errors always include text ("Error:", "Approved"). |
| 1.4.3 / 1.4.11 Contrast | Palette in `app.css` checked: text ≥ 4.5:1, borders/focus ≥ 3:1. |
| 1.4.4 / 1.4.10 / 1.4.12 Resize, reflow, text spacing | rem units; single-column layout under 40rem; no fixed heights on text containers. |
| 2.1.1 Keyboard | Native controls only; no custom widgets except native `<dialog>`. |
| 2.2.1 Timing adjustable | Session-timeout warning 2 minutes before sign-out, with "Stay signed in". |
| 2.4.1 Bypass blocks | "Skip to main content" link. |
| 2.4.2 Page titled | Unique `<title>` per page; "(has errors)" added when validation fails. |
| 2.4.3 Focus order / 2.4.7 Focus visible | DOM order = visual order; 3px high-contrast outline. |
| 2.4.8 Location | `aria-current="page"` in nav, `aria-current="step"` in the document wizard, breadcrumbs in admin. |
| 2.5.8 Target size | Buttons min 44px tall (small buttons 36px, still above 24px). |
| 3.3.1 / 3.3.3 Error identification & suggestion | Error summary (focused on load) linking to each field; inline message says how to fix. |
| 3.3.4 Error prevention (legal/financial) | Review step before signing; explicit certification checkbox; confirmation for destructive actions. |
| 4.1.2 Name, role, value | Every input labelled; icon-free buttons with visible text; `sr-only` context such as "Remove *San Diego*". |
| 4.1.3 Status messages | Flash messages use `role="status"` / `role="alert"`. |

## Automated checks

`pytest tests/test_accessibility.py` renders every page (including error states) and fails on: missing `lang`,
missing title, missing landmarks, not exactly one `h1`, skipped heading levels, duplicate ids, unlabelled
controls, broken `aria-describedby`, images without alt, tables without headers, nameless buttons, and missing skip
link.

Recommended additions in CI: **axe-core** (via Playwright or `pa11y-ci`) and **Lighthouse** against a running
instance.

## Manual test plan (run before each release)

1. **Keyboard only**: complete an authorization, approve it as `budget` and `ao`, create a voucher, then return and
   re-sign it. Check you never lose focus and never get trapped.
2. **Screen readers**: NVDA + Firefox, JAWS + Chrome, VoiceOver + Safari. Check that headings and landmarks list
   correctly, that form hints and errors are read with each field, and that success messages are announced.
3. **Zoom**: 200% and 400% (equivalent to a 320px width). No horizontal scrolling except inside data tables, which are
   keyboard-scrollable regions.
4. **Text spacing bookmarklet** (WCAG 1.4.12): no clipped text.
5. **Windows High Contrast / forced colors**: tags, buttons and focus remain visible.
6. **Session timeout**: set `session_timeout_minutes` to 3, wait for the dialog, and extend using the keyboard.
7. **Trusted Tester v5** process (DHS) for formal 508 conformance reporting, and record results in a VPAT/ACR.
