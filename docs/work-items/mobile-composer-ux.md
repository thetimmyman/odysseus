# Mobile Chat/Agent Composer UX Fix

## Problem

On mobile, especially iOS, tapping the chat/agent message box causes the page to zoom/focus in. The composer then consumes too much vertical space, and the send button becomes hard to find while the keyboard is open.

Observed behavior:

- Tapping the message input zooms the viewport.
- The textarea expands too tall after focus.
- The send button is pushed away or becomes difficult to locate.
- The keyboard/focused layout makes the message area dominate the screen.
- The issue affects Agent mode and likely Chat mode because they share the composer.

## Goal

Make the mobile composer compact, stable, and usable while preserving desktop behavior.

## Required behavior

1. Prevent iOS focus zoom on the chat/agent input.
   - Ensure focused inputs/textareas use at least `16px` font size on mobile.
   - Do not disable pinch zoom globally unless there is no accessible alternative.
2. Keep the send button visible when the keyboard is open.
3. Limit the message box height on mobile.
   - Compact initial/single-line state.
   - Expand only up to a reasonable max height, approximately `25–30vh`.
   - After max height, scroll inside the textarea instead of expanding the entire composer.
4. Keep the composer sticky to the bottom with safe-area padding.
5. Handle mobile keyboard/viewport resize cleanly.
   - Prefer CSS dynamic viewport units where appropriate.
   - Use `window.visualViewport` only if CSS alone is insufficient.
6. Preserve desktop behavior.
7. Preserve Agent/Chat mode toggle behavior.
8. Preserve voice, search, and terminal controls.
9. Preserve message sending, Enter/Shift+Enter behavior, and streaming status display.

## Files to inspect

- `static/index.html`
- `static/style.css`
- `static/js/chat.js`
- `static/js/chatStream.js`
- `static/app.js`
- Any composer/input-related JS or CSS modules

## Implementation guidance

- Add mobile-specific CSS with media queries.
- Use `font-size: 16px` or larger on mobile text inputs/textareas.
- Add max-height and `overflow-y: auto` to the composer textarea.
- Ensure the send button remains visible and aligned in the composer row.
- Avoid full-page layout jumps when the keyboard opens.
- Keep tap targets accessible.
- Avoid broad changes to agent execution, model routing, harness code, or backend logic.

## Validation

At minimum:

1. Run syntax/compile checks for changed JS/CSS where available.
2. Run any existing frontend/unit tests relevant to chat/composer.
3. If no automated tests exist, add a documented manual validation checklist.

Manual validation checklist:

- iPhone/mobile viewport: tapping the message box does not zoom the page.
- Keyboard open: send button remains visible and reachable.
- Long message: textarea scrolls internally after max height.
- Agent mode: composer remains usable.
- Chat mode: composer remains usable.
- Desktop viewport: layout is unchanged.
- Streaming status remains visible enough to understand whether work is running.

## Acceptance criteria

- Tapping the message box on iPhone/mobile does not zoom the page.
- Keyboard opening does not hide the send button.
- The message box no longer consumes most of the screen.
- Composer remains usable in both Agent and Chat mode.
- Desktop layout remains unchanged.
- The implementation is verified with tests or a clear manual checklist.

## Non-goals

- Do not redesign the entire chat UI.
- Do not change agent execution behavior.
- Do not modify model routing or harness behavior.
- Do not introduce global accessibility regressions by disabling user zoom unnecessarily.
