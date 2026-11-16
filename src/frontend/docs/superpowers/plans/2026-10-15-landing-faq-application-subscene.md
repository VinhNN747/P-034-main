# Landing FAQ Application Detail Subscene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an animated, in-video Application Detail preview opened from the FAQ Assistant CTA while preserving the walkthrough's three primary chatbot scenes.

**Architecture:** Add a static presentation component for the fictional FAQ Assistant detail page, styled with the existing workspace design patterns. Give the matching chat action an activation callback and stable selector, then extend the walkthrough's GSAP timeline with a decorative cursor, nested preview state, timed return, and reduced-motion handling.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript, CSS Modules, GSAP 3, `@gsap/react`.

**Spec:** `docs/superpowers/specs/2026-10-15-landing-faq-application-subscene-design.md`

## Global Constraints

- Preserve exactly three primary scenes: Applications, Setup, and Usage.
- Keep the FAQ Assistant view inside the landing video; do not navigate to `/applications/[id]` or make Gateway API calls.
- Use fictional preview data: `faq-assistant`, production, config version `12`, Balanced, `GPT-4.1`, `Claude Sonnet`, budget `250.00`, and `12,840` requests.
- Show the pointer only as decoration; keep the **FAQ Assistant** action keyboard-operable.
- Honor `prefers-reduced-motion`: no automatic pointer or nested scene transitions, while manual open/back remain available.
- Add or run no automated tests for this landing walkthrough task.

## File Structure

- Create `src/components/landing-application-preview.tsx` for the static Application Detail mock and back control.
- Create `src/components/landing-application-preview.module.css` for its compact workspace shell and responsive layout.
- Modify `src/components/support-chat-references.tsx` to expose action IDs and optional activation callbacks.
- Modify `src/components/landing-faq-walkthrough.tsx` to own preview state and the pointer/timing sequence.

## Review Focus

- The pointer must target the FAQ Assistant action after the answer and actions are visible, including at narrow widths.
- Actions without an activation callback must preserve their current link navigation behavior.
- Manual open/back and scene selection must not leave the carousel paused or the preview stuck open.
- Reduced-motion mode must omit automated pointer and preview transitions while retaining functional controls.
- Preview shell, tabs, cards, and recent request rows must remain legible in light and dark themes.

---

### Task 1: Build the static Application Detail preview

**Files:**
- Create: `src/components/landing-application-preview.tsx`
- Create: `src/components/landing-application-preview.module.css`

**Interfaces:**
- Produces: `LandingApplicationPreview({ onBack }: { onBack: () => void })`.
- The component reads the selected language from `useLanguage()` and owns no application API state.

- [x] **Step 1: Create `LandingApplicationPreview`** with a compact Taskway workspace shell, Applications selected, an Overview heading, Overview/Playground/Settings tabs, four overview cards, a recent requests panel, and a back-to-chat button.
- [x] **Step 2: Add the approved sample content**: `FAQ Assistant`, `production application`, `faq-assistant · Configuration version 12`; Balanced; two enabled models (`GPT-4.1`, `Claude Sonnet`); budget `250.00`; `12,840` requests; and the three fictional rows `req_faq_7A31`, `req_faq_4D08`, and `req_faq_9C52` with values from the spec.
- [x] **Step 3: Style the preview** using the workspace's existing surface, text, border, status, and active-tab treatment; use a two-column card grid in the video frame and collapse it cleanly at narrow widths. Keep the existing light/dark theme variables effective.
- [x] **Step 4: Review the component output** against `ApplicationDetailScene` in `application-scenes.tsx`; confirm the same content hierarchy is visible without fetching live data.

### Task 2: Make chat actions selectively activatable in place

**Files:**
- Modify: `src/components/support-chat-references.tsx`
- Modify: `src/components/landing-faq-walkthrough.tsx`

**Interfaces:**
- Produces: `Action = { id?: string; label: string; url: string; onActivate?: () => void }` for `SupportChatReferences`.
- When `onActivate` exists, its link prevents route navigation, invokes the callback, and renders `data-chat-action={id}`. Actions without it keep current behavior.

- [x] **Step 1: Extend the reference action type and link rendering** with optional `id` and `onActivate`; allow distinct actions to share a URL by deduplicating on `id ?? url`, and use that value as the React key.
- [x] **Step 2: Mark the FAQ Assistant action** with stable ID `demo-faq-assistant`, exact label `FAQ Assistant`, URL `/applications`, and an activation callback that opens the nested preview.
- [x] **Step 3: Keep the other six demo application links unchanged** so they continue to navigate to `/applications`.
- [x] **Step 4: Review the rendered action list** and confirm the target selector appears only on the FAQ Assistant action, while existing actions still navigate normally.

### Task 3: Integrate the pointer and nested preview into the three-scene loop

**Files:**
- Modify: `src/components/landing-faq-walkthrough.tsx`
- Consume: `src/components/landing-application-preview.tsx`

**Interfaces:**
- Preview state is `"chat" | "loading" | "application"` and is owned by `LandingFaqWalkthrough`.
- A back callback returns to the chat view; the chat timeline remains the only owner of the three primary scene sequence.
- The pointer uses the `[data-chat-action="demo-faq-assistant"]` target and remains `aria-hidden`.
- Consumes: Task 1's `LandingApplicationPreview({ onBack })` and Task 2's activatable `SupportChatReferences` action.

- [x] **Step 1: Render the nested view** by switching between the chat UI, an Applications loading skeleton, and `LandingApplicationPreview` inside the existing video frame; update the browser-bar label for each state and retain the three primary scene controls only in chat view.
- [x] **Step 2: Add the decorative pointer overlay** to the walkthrough root and animate it from its initial point to the target action's measured position relative to `rootRef`; scale it briefly for the click press.
- [x] **Step 3: Sequence the automatic click** after the first answer and action links reveal: invoke the same FAQ Assistant activation path as a visitor click, show loading for 0.72 seconds and the application preview for 1.4 seconds, then transition directly to Setup as the preview closes. Keep manual open/back behavior returning to the chat.
- [x] **Step 4: Handle manual controls**: a visitor click opens the preview and pauses the timeline; the back control returns and resumes it; selecting a primary scene clears preview state and selects that scene.
- [x] **Step 5: Respect reduced motion** by omitting cursor movement and all automatic nested-view changes in that mode, while keeping the FAQ Assistant action and back control usable.
- [x] **Step 6: Review sequencing boundaries** at the end of the Applications scene and at the loop reset; confirm the preview cannot leak into another primary scene or reopen immediately after manual return.

### Task 4: Verify implementation

**Files:**
- Review: all four files listed above.

- [x] **Step 1: Run TypeScript** with `npx tsc --noEmit --incremental false` from `ai20k-chat`; expected result: exit code 0.
- [x] **Step 2: Run ESLint** with `npx eslint src/components/landing-application-preview.tsx src/components/landing-faq-walkthrough.tsx src/components/support-chat-references.tsx`; expected result: exit code 0.
- [x] **Step 3: Review the final diff** for exact sample data, three-scene navigation, keyboard-operable CTA/back behavior, reduced-motion handling, and no API imports or requests in the preview.
- [x] **Step 4: Inspect the walkthrough in a browser** at its standard and narrow widths; confirm pointer targeting, the four-card/tables layout, automatic return, manual open/back, primary scene selection, and reduced-motion behavior.
