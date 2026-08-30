# Mobile Sidebar Left-Edge Gesture Fix

## Problem

On mobile, the sidebar/menu opens too easily while the user is scrolling or swiping. The menu can be triggered from both the left and right side of the screen, which causes accidental popups while reading, typing, or interacting with the chat/agent composer.

Observed behavior:

- Swiping/scrolling from the right side can open the menu.
- Normal vertical scrolling can accidentally trigger the menu.
- Interactions inside the composer or scrollable content can trigger the menu.
- The hamburger/menu button is useful and should remain unchanged.

## Goal

Restrict the menu open gesture to a deliberate swipe that starts near the left edge of the screen. Preserve existing button-based menu behavior and desktop behavior.

## Required behavior

1. The menu should only open from a deliberate swipe starting near the LEFT edge of the screen.
2. Swipes starting from the right side must not open the menu.
3. Normal vertical scrolling must not open the menu.
4. Horizontal gestures inside inputs, textareas, code blocks, scrollable panes, or the message composer must not open the menu.
5. Existing hamburger/menu button behavior must remain unchanged.
6. Desktop behavior must remain unchanged.

## Gesture rules

Recommended implementation rules:

- Record the initial touch/pointer position.
- Only allow an open gesture if `touchStartX <= LEFT_EDGE_THRESHOLD`.
- Suggested threshold: `24–32px` from the left edge.
- Require meaningful rightward movement, for example `deltaX >= 50`.
- Require horizontal intent, for example `abs(deltaX) > abs(deltaY) * 1.5`.
- Ignore gestures that start from the right half or right edge of the viewport.
- Ignore gestures that start inside interactive targets.

Suggested constants/helpers:

```js
const LEFT_EDGE_SWIPE_ZONE_PX = 32;
const MIN_OPEN_SWIPE_DX = 50;
const HORIZONTAL_INTENT_RATIO = 1.5;
```

```js
function isInteractiveTarget(target) {
  return Boolean(target?.closest?.(
    'input, textarea, select, button, a, [contenteditable="true"], pre, code, .composer, .message-composer, .scrollable'
  ));
}
```

## Files to inspect

- `static/js/sidebar-layout.js`
- `static/js/app.js`
- `static/js/chat.js`
- `static/style.css`
- Any touch/pointer gesture handlers for sidebar/menu/drawer

## Implementation guidance

- Prefer narrowing the existing gesture handler rather than rewriting the drawer.
- Add a named constant for the left-edge swipe zone.
- Add a helper to detect interactive targets.
- Keep any existing close gesture if it works, but do not allow right-edge opening.
- Add comments explaining why the left-edge threshold exists.
- Avoid backend changes.
- Avoid changing agent execution, model routing, harness code, or chat streaming behavior.

## Validation

At minimum:

1. Run JS syntax checks or existing frontend tests if available.
2. Add or update a focused test if the repo has JS test coverage for gestures.
3. If no automated coverage exists, add a documented manual validation checklist.

Manual validation checklist:

- Swipe from the right side does not open the menu.
- Vertical scrolling does not open the menu.
- Deliberate swipe from the left edge opens the menu.
- Hamburger button still opens and closes the menu.
- Composer typing, text selection, and scrolling do not trigger the menu.
- Code block scrolling does not trigger the menu.
- Desktop layout and interactions are unchanged.

## Acceptance criteria

- Right-side swipe/scroll never opens the menu.
- Vertical scroll does not open the menu.
- Left-edge swipe opens the menu intentionally.
- Hamburger button still works.
- Composer and scrollable content gestures do not trigger the menu.
- Desktop behavior is unchanged.

## Non-goals

- Do not redesign the entire sidebar.
- Do not remove the hamburger/menu button.
- Do not change agent execution or model routing behavior.
- Do not change the mobile composer layout in this work item; that is tracked separately.
