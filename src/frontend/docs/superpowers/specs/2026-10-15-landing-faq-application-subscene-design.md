# Landing FAQ Application Detail Subscene

## Goal

Extend the landing page chatbot walkthrough so the Applications prompt demonstrates opening the **FAQ Assistant** application. Keep the existing three primary chatbot scenes and their navigation. Show the application detail page as a nested scene inside the video frame, using fictional data and the visual structure of the real Application Detail page.

## Approved Experience

1. The Applications prompt and its response play as they do now.
2. The **FAQ Assistant** action remains visible with the other application actions.
3. During autoplay, a pointer appears, moves to **FAQ Assistant**, and clicks it.
4. After the click, the video frame shows the Applications loading skeleton from the workspace UI, then transitions to the static Application Detail preview.
5. During autoplay, the preview closes directly into the Setup scene, without showing the Applications chat again. A manual back action still returns to the Applications chat.
6. The three primary scene controls remain Applications, Setup, and Usage. The application preview is not a fourth primary scene.
7. A visitor can click **FAQ Assistant** directly to open the preview and use a back control to return to the chat. Other sample application actions keep their current Applications route behavior.

## Application Preview

Use a static preview rather than navigating to `/applications/[id]`. `FAQ Assistant` is sample content and has no real application ID or backend record. The preview must not load account data or make Gateway API requests.

The preview follows the real `ApplicationDetailScene` structure:

- Compact Taskway workspace shell with Applications selected.
- Heading: `FAQ Assistant`, `production application`, slug `faq-assistant`, configuration version `12`.
- Application tabs: Overview selected, Playground, and Settings.
- Four overview cards:
  - Routing objective: Balanced.
  - Available models: two enabled, `GPT-4.1` and `Claude Sonnet`.
  - Budget guardrail: `250.00`.
  - Requests: `12,840`.
- A recent requests panel with three fictional completed rows:
  - `req_faq_7A31`, Playground, Oct 14 at 10:42, `184 ms / 0.0012`.
  - `req_faq_4D08`, API key, Oct 14 at 10:38, `236 ms / 0.0015`.
  - `req_faq_9C52`, Playground, Oct 14 at 10:24, `192 ms / 0.0011`.

Use the workspace's existing colors, borders, typography, status treatments, and card hierarchy. Scale the shell and content to the current walkthrough card, with the four cards arranged in two columns at that size. Keep the preview readable in both light and dark themes.

## Interaction and Animation

- The walkthrough remains the owner of the primary timeline and nested preview state.
- Give the FAQ Assistant action a stable identifier so the timeline can find its rendered link without relying on visible text.
- Measure the target link inside the walkthrough root and animate a pointer overlay toward it. Show a brief click press before transitioning views.
- Show a compact Applications workspace skeleton for about 0.7 seconds after manual or automatic activation, matching the loading state in the supplied screenshot.
- Keep the pointer decorative and `aria-hidden`; the link remains a real keyboard-operable control.
- During autoplay, show the Application Detail preview for about 1.4 seconds after loading, then transition directly to Setup as the preview closes. Do not briefly show the Applications chat between the preview and Setup.
- A manual activation opens the preview and pauses autoplay. The back control returns to chat and resumes the sequence.
- Scene selection clears the nested preview and selects the requested primary scene.
- Under `prefers-reduced-motion`, omit pointer movement and automatic nested transitions while leaving the action and back control usable.

## Scope and Files

- Add a small presentation component and CSS module for the fictional Application Detail preview.
- Update `landing-faq-walkthrough.tsx` to manage the nested view, pointer animation, and transition timing.
- Extend `support-chat-references.tsx` only as needed to expose a stable action identifier and optional activation handler while preserving existing action behavior.
- Do not change the real Application Detail route, Gateway API, or the three primary scene definitions.

## Verification

- Check TypeScript and lint for modified files.
- Review the desktop and narrow layouts, autoplay pointer targeting, manual open/back behavior, primary scene navigation, and reduced-motion behavior.
- Keep the existing project instruction not to add or run tests for the landing walkthrough task.
