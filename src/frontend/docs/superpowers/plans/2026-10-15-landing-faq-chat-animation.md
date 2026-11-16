# Landing FAQ Chat Animation Implementation Plan

> **For agentic workers:** Implement inline with `superpowers:executing-plans`; preserve the approved three-scene storyboard and the verified chatbot transcript summarized below.

**Goal:** Replace the static FAQ example on the landing page with a looping GSAP walkthrough of three real chatbot flows.

**Architecture:** Add a client component for the chat window, scene content, code block, and chart. Use a scoped GSAP timeline for typing, response reveal, link emphasis, chart drawing, and scene transitions; leave reduced-motion users with a readable static scene. The existing section copy stays in its current parent component.

**Tech Stack:** Next.js, React, TypeScript, CSS Modules, GSAP, `@gsap/react`, existing support-chat renderers.

**Spec:** Approved conversation request in this session; this plan records its constraints and the real responses used.

## Global Constraints

- Keep `landing.faqKicker`, `landing.faqTitle`, and `landing.faqDescription` unchanged.
- Base each scene on verified responses from `/api/v1/support/chat` on the user's `8002` backend.
- Do not put account-specific application names, IDs, API keys, or raw usage counts in the public landing visual.
- Keep the FAQ feature copy localized for Vietnamese and English.
- Use `useGSAP` with a component scope; honor `prefers-reduced-motion`; add no dependencies.
- Do not add or run tests in this task.

## Review Focus

- Scene sequencing and cleanup after component unmount.
- Reduced-motion and no-animation rendering.
- Long code content and narrow viewport overflow.
- Internal action links and public-safe transcript content.
- Vietnamese and English labels while preserving the section heading and description.

### Task 1: Replace the static FAQ visual with the real-response walkthrough

**Files:**
- Create: `src/components/landing-faq-walkthrough.tsx`
- Create: `src/components/landing-faq-walkthrough.module.css`
- Modify: `src/components/landing-product-features.tsx`

**Verified response inputs:**
- Application list: the assistant returned a private list plus the `Mở Applications` action at `/applications`; display only an anonymized summary and that action.
- FAQ application setup: the assistant recommended development first, an available model or small pool, a routing target, budget, output limit, and fallback; it returned `/applications/new`, `/available-models`, `/usage`, and documentation actions.
- SDK follow-up: the assistant returned a fenced Python OpenAI SDK example using `TASKWAY_API_KEY`, `https://taskway-ai-be.online/v1`, `model="auto"`, and an `Idempotency-Key`; use a shortened form of this real block.
- Usage chart: the assistant returned a `requests` timeseries with `day` dimension, line chart, and `/usage` action; use a normalized anonymous shape without raw counts.

- [ ] Build a localized chat-window component with the three scenes, actual internal action links, existing `SupportMessageContent` code rendering, and an SVG timeseries chart.
- [ ] Sequence question reveal, typing indicator, assistant response, code/chart reveal, and transitions with one scoped, looping GSAP timeline.
- [ ] Replace only the static FAQ visual subtree; preserve section title, description, and capability chips.
- [ ] Run lint and TypeScript checks, inspect the hosted preview at desktop and narrow viewport widths, and confirm reduced-motion state remains readable.
