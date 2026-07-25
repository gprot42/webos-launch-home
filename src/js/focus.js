import {REMOTE_KEY} from './remote.js';
import {
  isSettingsTextField,
  isEditingSettingsText,
  beginSettingsTextEdit,
  endSettingsTextEdit
} from './settings.js';

const ROW_TOLERANCE = 72;
const COL_TOLERANCE = 88;
const MIN_PRIMARY_DELTA = 18;
const POINTER_AXIS_THRESHOLD = 28;
const POINTER_AXIS_RATIO = 1.6;

function focusRow(el) {
  if (!el) return '';
  if (el.closest('#input-row')) return 'inputs';
  if (el.closest('#app-grid')) return 'apps';
  if (el.closest('#music-bar')) return 'music';
  if (el.closest('#settings-panel')) return 'settings';
  return 'other';
}

export function createFocusManager(root, handlers) {
  let items = [];
  let pointerAxis = null;
  let pointerAccumDx = 0;
  let pointerAccumDy = 0;
  let lastPointerX = null;
  let lastPointerY = null;
  // Last *physical* cursor position, never cleared by key navigation. Used to
  // reject phantom pointermove events that webOS fires when the app row scrolls
  // under a stationary Magic-Remote cursor after a remote key press.
  let lastRealPointerX = null;
  let lastRealPointerY = null;
  let lastKeyNavAt = 0;
  const KEY_NAV_POINTER_GUARD_MS = 700;

  function collect() {
    items = Array.from(root.querySelectorAll('.focusable:not([disabled])'));
    items.sort(function (a, b) {
      return Number(a.dataset.focusIndex || 0) - Number(b.dataset.focusIndex || 0);
    });
  }

  function scrollableAncestor(el, axis) {
    let node = el.parentElement;
    while (node && node !== document.body) {
      const style = window.getComputedStyle(node);
      if (axis === 'y') {
        const overflowY = style.overflowY;
        if ((overflowY === 'auto' || overflowY === 'scroll') &&
            node.scrollHeight > node.clientHeight + 1) {
          return node;
        }
      } else {
        const overflowX = style.overflowX;
        if ((overflowX === 'auto' || overflowX === 'scroll') &&
            node.scrollWidth > node.clientWidth + 1) {
          return node;
        }
      }
      node = node.parentElement;
    }
    return null;
  }

  // Older webOS Chromium builds ignore scrollIntoView({inline}), so the app
  // row never scrolls horizontally. Manually keep the focused tile inside the
  // scroll container's viewport by adjusting scrollLeft.
  function ensureHorizontallyVisible(el) {
    const container = scrollableAncestor(el, 'x');
    if (!container) return;
    const cRect = container.getBoundingClientRect();
    const eRect = el.getBoundingClientRect();
    const margin = 24;
    if (eRect.left < cRect.left + margin) {
      container.scrollLeft -= (cRect.left + margin) - eRect.left;
    } else if (eRect.right > cRect.right - margin) {
      container.scrollLeft += eRect.right - (cRect.right - margin);
    }
  }

  // Instant vertical keep-in-view (no smooth scroll). scrollIntoView on webOS
  // can lag a frame or two while the compositor repaints a large settings
  // panel; direct scrollTop writes redraw immediately. Walks every nested
  // overflow container (e.g. settings-apps inside settings-body).
  function ensureVerticallyVisible(el) {
    let node = el;
    const margin = 28;
    while (node && node !== document.body) {
      const container = scrollableAncestor(node, 'y');
      if (!container) break;
      const cRect = container.getBoundingClientRect();
      const eRect = el.getBoundingClientRect();
      if (eRect.top < cRect.top + margin) {
        container.scrollTop -= (cRect.top + margin) - eRect.top;
      } else if (eRect.bottom > cRect.bottom - margin) {
        container.scrollTop += eRect.bottom - (cRect.bottom - margin);
      }
      node = container;
    }
  }

  function clearFocusChrome(except) {
    root.querySelectorAll('.focused').forEach(function (item) {
      if (item !== except) item.classList.remove('focused');
    });
    // Full settings rows (label + control) get a highlight while scrolling.
    // Never strip photo-picker is-selected — that marks the chosen wallpaper.
    root.querySelectorAll('.settings-row-highlight').forEach(function (item) {
      if (item !== except && !(except && item.contains && item.contains(except))) {
        item.classList.remove('settings-row-highlight');
      }
    });
  }

  function highlightSettingsContext(el) {
    root.querySelectorAll('.settings-row-highlight').forEach(function (item) {
      item.classList.remove('settings-row-highlight');
    });
    if (!el || focusRow(el) !== 'settings') return;
    // Prefer the whole labeled row / track row so the eye tracks while scrolling.
    // Photo tiles: only the focused tile gets row-highlight; is-selected stays
    // on the chosen wallpaper separately.
    const row = el.closest && el.closest(
      '.settings-row, .settings-track-row, .settings-app-row, ' +
      '.settings-pinned-row, .settings-input-row, .photo-picker-tile'
    );
    if (row) row.classList.add('settings-row-highlight');
  }

  function focusItem(el) {
    if (!el) return false;
    // webOS/Chromium silently ignores .focus() on elements that are not
    // actually focusable at that moment (visibility:hidden, detached, an
    // ancestor with visibility:hidden, tabindex removed, etc). When that
    // happens document.activeElement does NOT change, so spatial nav keeps
    // re-selecting the same unfocusable neighbour and left/right freezes.
    try {
      if (typeof el.tabIndex === 'number' && el.tabIndex < 0) el.tabIndex = 0;
    } catch (err) { /* ignore */ }
    try { el.focus(); } catch (err2) { /* ignore */ }

    const landed = document.activeElement === el ||
      (el.contains && el.contains(document.activeElement));
    const inSettings = focusRow(el) === 'settings';

    // Outside settings: require real focus. Inside settings: still paint the
    // highlight so wheel/D-pad scrolling always shows which row is active
    // (checkboxes/ranges sometimes refuse activeElement on webOS).
    if (!landed && !inSettings) return false;

    clearFocusChrome(el);
    el.classList.add('focused');
    highlightSettingsContext(el);
    ensureHorizontallyVisible(el);
    ensureVerticallyVisible(el);
    return true;
  }

  // True when an element can actually take keyboard focus right now. Filters
  // out zero-size / hidden tiles that still report a layout rect (which fools
  // spatial navigation into repeatedly targeting them).
  function isFocusable(el) {
    if (!el) return false;
    if (el.disabled) return false;
    if (el.tabIndex < 0) return false;
    if (typeof el.offsetParent !== 'undefined' && el.offsetParent === null) {
      // display:none (offsetParent null) — but position:fixed also reports
      // null, so double-check via getClientRects for those.
      if (!el.getClientRects || el.getClientRects().length === 0) return false;
    }
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return false;
    return true;
  }

  function resetPointerAxis() {
    pointerAxis = null;
    pointerAccumDx = 0;
    pointerAccumDy = 0;
    lastPointerX = null;
    lastPointerY = null;
  }

  function updatePointerAxis(dx, dy) {
    pointerAccumDx += dx;
    pointerAccumDy += dy;

    const total = Math.abs(pointerAccumDx) + Math.abs(pointerAccumDy);
    if (total < POINTER_AXIS_THRESHOLD) return;

    if (Math.abs(pointerAccumDx) > Math.abs(pointerAccumDy) * POINTER_AXIS_RATIO) {
      pointerAxis = 'horizontal';
    } else if (Math.abs(pointerAccumDy) > Math.abs(pointerAccumDx) * POINTER_AXIS_RATIO) {
      pointerAxis = 'vertical';
    } else {
      pointerAxis = null;
    }

    pointerAccumDx = 0;
    pointerAccumDy = 0;
  }

  function shouldIgnorePointerTarget(target) {
    const active = document.activeElement;
    if (!active || !target || active === target) return false;
    if (!active.classList.contains('focusable')) return false;
    if (pointerAxis !== 'horizontal') return false;
    return focusRow(active) !== focusRow(target);
  }

  function onPointerMove(event) {
    if (event.clientX == null || event.clientY == null) return;

    // Reject phantom pointermove events. After a remote key press moves focus,
    // the app row scrolls under a stationary cursor; webOS then emits a
    // pointermove with the SAME screen coordinates as the last physical cursor
    // position. Left unchecked it re-focuses whatever tile is now under the
    // cursor, snapping focus back and freezing left/right navigation. Only
    // suppress it briefly and only when the cursor has not actually moved.
    if (Date.now() - lastKeyNavAt < KEY_NAV_POINTER_GUARD_MS
      && event.clientX === lastRealPointerX && event.clientY === lastRealPointerY) {
      return;
    }
    lastRealPointerX = event.clientX;
    lastRealPointerY = event.clientY;

    if (lastPointerX != null) {
      updatePointerAxis(event.clientX - lastPointerX, event.clientY - lastPointerY);
    }
    lastPointerX = event.clientX;
    lastPointerY = event.clientY;

    const target = event.target && event.target.closest
      ? event.target.closest('.focusable:not([disabled])')
      : null;
    if (!target) return;
    if (target.dataset && target.dataset.pointerFocus === 'off') return;
    if (shouldIgnorePointerTarget(target)) return;
    focusItem(target);
  }

  function settingsFocusables() {
    return items.filter(function (item) {
      return focusRow(item) === 'settings' && isFocusable(item);
    });
  }

  function moveSequential(active, delta) {
    const scoped = settingsFocusables();
    let idx = scoped.indexOf(active);
    if (idx < 0) {
      // Focus is outside the panel — enter at the first/last control.
      if (!scoped.length) return false;
      const edge = delta > 0 ? scoped[0] : scoped[scoped.length - 1];
      return focusItem(edge);
    }
    // Skip tiles that refuse focus (webOS sometimes ignores .focus() on a node).
    for (let i = idx + delta; i >= 0 && i < scoped.length; i += delta) {
      if (focusItem(scoped[i])) return true;
    }
    return true; // at edge; swallow so spatial nav doesn't escape
  }

  function photoGridTiles(tile) {
    if (!tile || !tile.closest) return [];
    const grid = tile.closest('.photo-picker-grid');
    if (!grid) return [];
    return Array.prototype.filter.call(
      grid.querySelectorAll('.photo-picker-tile.focusable'),
      isFocusable
    );
  }

  /** First settings control after the photo gallery (Ken Burns, Music, …). */
  function focusAfterPhotoGrid(tile) {
    const grid = tile && tile.closest ? tile.closest('.photo-picker-grid') : null;
    if (!grid) return false;
    const scoped = settingsFocusables();
    let lastInGrid = -1;
    for (let i = 0; i < scoped.length; i += 1) {
      if (grid.contains(scoped[i])) lastInGrid = i;
    }
    for (let j = lastInGrid + 1; j < scoped.length; j += 1) {
      if (focusItem(scoped[j])) return true;
    }
    return false;
  }

  function focusBeforePhotoGrid(tile) {
    const grid = tile && tile.closest ? tile.closest('.photo-picker-grid') : null;
    if (!grid) return false;
    const scoped = settingsFocusables();
    let firstInGrid = -1;
    for (let i = 0; i < scoped.length; i += 1) {
      if (grid.contains(scoped[i])) {
        firstInGrid = i;
        break;
      }
    }
    for (let j = firstInGrid - 1; j >= 0; j -= 1) {
      if (focusItem(scoped[j])) return true;
    }
    return false;
  }

  /**
   * Navigate the settings photo-picker grid by layout + index.
   * Spatial-only nav was unreliable on TV; index order matches reading order.
   */
  function movePhotoPicker(active, keyCode) {
    if (!active || !active.classList || !active.classList.contains('photo-picker-tile')) {
      return false;
    }
    const tiles = photoGridTiles(active);
    if (!tiles.length) return false;

    const idx = tiles.indexOf(active);
    if (idx < 0) return false;

    const isHorizontal = keyCode === REMOTE_KEY.LEFT || keyCode === REMOTE_KEY.RIGHT;
    const isVertical = keyCode === REMOTE_KEY.UP || keyCode === REMOTE_KEY.DOWN;
    if (!isHorizontal && !isVertical) return false;

    // How many tiles share the first visual row → column count for up/down.
    const firstTop = tiles[0].getBoundingClientRect().top;
    let cols = 0;
    for (let i = 0; i < tiles.length; i += 1) {
      if (Math.abs(tiles[i].getBoundingClientRect().top - firstTop) < 28) {
        cols += 1;
      } else {
        break;
      }
    }
    if (cols < 1) cols = 1;

    let nextIdx = -1;
    if (keyCode === REMOTE_KEY.RIGHT) nextIdx = idx + 1;
    else if (keyCode === REMOTE_KEY.LEFT) nextIdx = idx - 1;
    else if (keyCode === REMOTE_KEY.DOWN) nextIdx = idx + cols;
    else if (keyCode === REMOTE_KEY.UP) nextIdx = idx - cols;

    if (nextIdx >= 0 && nextIdx < tiles.length) {
      if (focusItem(tiles[nextIdx])) return true;
    }

    // Past the last / before the first tile → leave the gallery.
    if (isVertical) {
      if (keyCode === REMOTE_KEY.DOWN) {
        if (focusAfterPhotoGrid(active)) return true;
      } else if (focusBeforePhotoGrid(active)) {
        return true;
      }
      return moveSequential(active, keyCode === REMOTE_KEY.DOWN ? 1 : -1);
    }

    // Horizontal wrap within the grid only (stay put at ends).
    const step = keyCode === REMOTE_KEY.RIGHT ? 1 : -1;
    const fallback = idx + step;
    if (fallback >= 0 && fallback < tiles.length && focusItem(tiles[fallback])) {
      return true;
    }
    return true;
  }

  /**
   * Select/OK on a photo tile: choose it, then leave the gallery so the user
   * can keep scrolling Settings (Ken Burns, Music, Save, …).
   */
  function activatePhotoPickerTile(tile) {
    if (!tile || !tile.classList.contains('photo-picker-tile')) return false;
    try {
      // Prefer explicit handler if present (div tiles); also works for <button>.
      if (typeof tile.click === 'function') tile.click();
    } catch (err) { /* ignore */ }
    // Re-collect: click may toggle classes but not rebuild the panel.
    collect();
    // Advance focus out of the grid so "Select then continue" works on TV.
    if (!focusAfterPhotoGrid(tile)) {
      try { tile.focus(); } catch (err2) { /* ignore */ }
      focusItem(tile);
    }
    return true;
  }

  // Magic Remote scroll wheel → previous/next settings option.
  // webOS fires standard wheel events; without a handler the panel either
  // does nothing useful or only pixel-scrolls without moving focus.
  let wheelAccum = 0;
  let wheelLockUntil = 0;
  const WHEEL_STEP_PX = 48;
  const WHEEL_STEP_MS = 110;

  function onWheel(event) {
    const settingsOpen = document.body.classList.contains('settings-open');
    if (!settingsOpen) return;

    // Don't steal the wheel while the virtual keyboard is open for editing.
    if (isEditingSettingsText(document.activeElement)) return;

    // Always own the wheel inside settings so we can step focus instead of
    // leaving a laggy native pixel scroll with no selection movement.
    event.preventDefault();
    event.stopPropagation();

    const dy = event.deltaY || 0;
    // deltaMode: 0=pixel, 1=line, 2=page. Magic Remote is usually pixels;
    // normalize line/page into a comparable scale.
    const scale = event.deltaMode === 1 ? 24 : event.deltaMode === 2 ? 240 : 1;
    wheelAccum += dy * scale;

    const now = Date.now();
    if (now < wheelLockUntil) return;
    if (Math.abs(wheelAccum) < WHEEL_STEP_PX) return;

    const dir = wheelAccum > 0 ? 1 : -1;
    wheelAccum = 0;
    wheelLockUntil = now + WHEEL_STEP_MS;
    lastKeyNavAt = now;

    collect();
    const current = document.activeElement && focusRow(document.activeElement) === 'settings'
      ? document.activeElement
      : null;
    moveSequential(current, dir);
  }

  function adjustValueControl(el, dir) {
    if (!el) return false;
    const tag = el.tagName;

    if (el.classList && el.classList.contains('option-stepper') && typeof el.__step === 'function') {
      el.__step(dir);
      return true;
    }

    if (tag === 'SELECT') {
      const count = el.options.length;
      if (!count) return false;
      let next = el.selectedIndex + dir;
      if (next < 0) next = 0;
      if (next > count - 1) next = count - 1;
      if (next !== el.selectedIndex) {
        el.selectedIndex = next;
        el.dispatchEvent(new Event('change', {bubbles: true}));
      }
      return true;
    }

    if (tag === 'INPUT' && el.type === 'range') {
      const step = Number(el.step) || 1;
      const min = el.min !== '' ? Number(el.min) : 0;
      const max = el.max !== '' ? Number(el.max) : 100;
      let next = Number(el.value) + dir * step;
      if (next < min) next = min;
      if (next > max) next = max;
      if (next !== Number(el.value)) {
        el.value = String(next);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      }
      return true;
    }

    return false;
  }

  // Move to the previous/next focusable in the same row by index order.
  // Spatial navigation fails when tiles overlap or share an x-position (e.g. a
  // wrapped/stacked dock), so horizontal dock movement falls back to this.
  // Returns false when there is no same-row neighbour (row edge) so the caller
  // can continue across rows via moveByGlobalIndex.
  function moveByIndexInRow(active, delta) {
    const row = focusRow(active);
    const scoped = items.filter(function (item) {
      return focusRow(item) === row;
    });
    const idx = scoped.indexOf(active);
    if (idx < 0) return false;
    // Walk outward skipping any neighbour that refuses focus, so a single
    // hidden/unfocusable tile can't wedge left/right navigation.
    for (let i = idx + delta; i >= 0 && i < scoped.length; i += delta) {
      const next = scoped[i];
      if (!isFocusable(next)) continue;
      if (focusItem(next)) return true;
    }
    return false; // at the row edge (or no focusable neighbour); cross rows
  }

  // Final horizontal fallback: walk every focusable (except the settings
  // overlay) in focus-index order. This guarantees left/right always advances
  // through the launcher -- app grid, inputs, top-bar and music controls -- so
  // focus can never dead-end at a row boundary (the "can't scroll after
  // launching an app" freeze).
  function moveByGlobalIndex(active, delta) {
    const scoped = items.filter(function (item) {
      return focusRow(item) !== 'settings';
    });
    const idx = scoped.indexOf(active);
    if (idx < 0) return false;
    for (let i = idx + delta; i >= 0 && i < scoped.length; i += delta) {
      const next = scoped[i];
      if (!isFocusable(next)) continue;
      if (focusItem(next)) return true;
    }
    return false; // true first/last focusable item
  }

  function isTopChrome(el) {
    return !!(el && el.closest && el.closest('.top-bar'));
  }

  function focusSettingsButton() {
    const gear = root.querySelector('#app-settings-btn');
    if (!gear || !isFocusable(gear)) return false;
    return focusItem(gear);
  }

  // First focusable in the main dock (inputs → apps → music).
  function focusMainDockFirst() {
    const selectors = [
      '#input-row .focusable',
      '#app-grid .focusable',
      '#music-bar .focusable'
    ];
    for (let s = 0; s < selectors.length; s += 1) {
      const list = root.querySelectorAll(selectors[s]);
      for (let i = 0; i < list.length; i += 1) {
        if (isFocusable(list[i]) && focusItem(list[i])) return true;
      }
    }
    return false;
  }

  function moveDirection(keyCode) {
    collect();
    if (!items.length) return;

    resetPointerAxis();

    const active = document.activeElement;
    const fromIdx = items.indexOf(active);
    const settingsOpen = document.body.classList.contains('settings-open');
    const isHorizontal = keyCode === REMOTE_KEY.LEFT || keyCode === REMOTE_KEY.RIGHT;
    const isVertical = keyCode === REMOTE_KEY.UP || keyCode === REMOTE_KEY.DOWN;

    // Focus lost / not on a known tile: stay inside Settings when the panel is open.
    if (!active || fromIdx < 0) {
      if (settingsOpen) {
        const scoped = settingsFocusables();
        if (scoped.length) {
          focusItem(isVertical && keyCode === REMOTE_KEY.UP
            ? scoped[scoped.length - 1]
            : scoped[0]);
          return;
        }
      }
      focusItem(items[0]);
      return;
    }

    // Entire Settings panel: sequential focus order (up/down and non-stepper left/right).
    // Photo grid gets special 2D handling first.
    if (focusRow(active) === 'settings') {
      if (movePhotoPicker(active, keyCode)) return;

      if (isHorizontal && adjustValueControl(active, keyCode === REMOTE_KEY.RIGHT ? 1 : -1)) {
        return;
      }

      // Up/down always steps the next settings control; left/right does the same
      // when the focused control is not a stepper/range.
      if (isVertical || isHorizontal) {
        moveSequential(active, (keyCode === REMOTE_KEY.DOWN || keyCode === REMOTE_KEY.RIGHT) ? 1 : -1);
        return;
      }
    }

    const rect = active.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;

    let best = null;
    let bestScore = Infinity;

    items.forEach(function (item) {
      if (item === active) return;
      if (!isFocusable(item)) return; // skip hidden/zero-size tiles that still report a rect
      // Don't spatial-nav into the settings overlay panel from the home dock.
      if (focusRow(item) === 'settings') return;

      const r = item.getBoundingClientRect();
      const ix = r.left + r.width / 2;
      const iy = r.top + r.height / 2;
      const dx = ix - cx;
      const dy = iy - cy;

      if (keyCode === REMOTE_KEY.LEFT && dx >= -10) return;
      if (keyCode === REMOTE_KEY.RIGHT && dx <= 10) return;
      if (keyCode === REMOTE_KEY.UP && dy >= -10) return;
      if (keyCode === REMOTE_KEY.DOWN && dy <= 10) return;

      if (isHorizontal) {
        if (Math.abs(dy) > ROW_TOLERANCE) return;
        if (Math.abs(dx) < MIN_PRIMARY_DELTA) return;
      }

      if (isVertical) {
        // Gear sits top-right; allow a much wider column when aiming at top chrome
        // so UP from any dock tile can still see the settings button.
        const colLimit = isTopChrome(item) || isTopChrome(active)
          ? Math.max(COL_TOLERANCE, 900)
          : COL_TOLERANCE;
        if (Math.abs(dx) > colLimit) return;
        if (Math.abs(dy) < MIN_PRIMARY_DELTA) return;
      }

      const primary = isHorizontal ? Math.abs(dx) : Math.abs(dy);
      const secondary = isHorizontal ? Math.abs(dy) : Math.abs(dx);
      // Prefer top-chrome when moving up so we hit Settings over a distant input.
      const chromeBias = (keyCode === REMOTE_KEY.UP && isTopChrome(item)) ? -5000 : 0;
      const score = primary * 100 + secondary + chromeBias;

      if (score < bestScore) {
        bestScore = score;
        best = item;
      }
    });

    if (best && focusItem(best)) return;

    // Spatial search found nothing. Horizontal: index-order through the dock.
    if (isHorizontal) {
      const row = focusRow(active);
      if (row !== 'settings') {
        const delta = keyCode === REMOTE_KEY.RIGHT ? 1 : -1;
        if (moveByIndexInRow(active, delta)) return;
        moveByGlobalIndex(active, delta);
      }
      return;
    }

    // Vertical edges: UP always reaches Settings; DOWN from Settings returns
    // to the dock (inputs / apps). This is the main 10-foot path to the gear.
    if (keyCode === REMOTE_KEY.UP) {
      if (focusRow(active) !== 'settings' && !isTopChrome(active)) {
        if (focusSettingsButton()) return;
      }
    }
    if (keyCode === REMOTE_KEY.DOWN) {
      if (isTopChrome(active)) {
        if (focusMainDockFirst()) return;
      }
    }
  }

  function onKeyDown(event) {
    let code = event.keyCode;
    resetPointerAxis();
    // Open the phantom-pointermove guard window (see onPointerMove). Any remote
    // key that can move/scroll focus must suppress the scroll-induced pointer
    // event that would otherwise snap focus back.
    lastKeyNavAt = Date.now();

    // Physical USB/Bluetooth keyboards can report different keyCodes than the
    // TV remote; normalize via event.key so keyboard navigation works too.
    if (event.key) {
      const active = document.activeElement;
      // Only treat as "typing" when a settings text field is unlocked for edit
      // (Select pressed). Readonly text fields still use D-pad for navigation.
      const typing = isEditingSettingsText(active);
      const keyMap = {
        ArrowLeft: REMOTE_KEY.LEFT,
        ArrowUp: REMOTE_KEY.UP,
        ArrowRight: REMOTE_KEY.RIGHT,
        ArrowDown: REMOTE_KEY.DOWN,
        Enter: REMOTE_KEY.ENTER,
        Escape: REMOTE_KEY.BACK,
        GoBack: REMOTE_KEY.BACK
      };
      if (!typing) {
        keyMap.Backspace = REMOTE_KEY.BACK;
      }
      if (keyMap[event.key] !== undefined) {
        code = keyMap[event.key];
      }
    }

    if (code === REMOTE_KEY.BACK) {
      const active = document.activeElement;
      // Exit text edit (close virtual keyboard) before leaving settings.
      if (isEditingSettingsText(active)) {
        event.preventDefault();
        endSettingsTextEdit(active);
        try { active.blur(); } catch (err) { /* ignore */ }
        // Re-focus readonly so D-pad continues from this field.
        try {
          active.readOnly = true;
          active.focus();
        } catch (err) { /* ignore */ }
        return;
      }
      if (handlers && handlers.onBack) {
        event.preventDefault();
        handlers.onBack();
      }
      return;
    }

    if (code === REMOTE_KEY.RED) {
      if (handlers && handlers.onRed) {
        event.preventDefault();
        handlers.onRed();
      }
      return;
    }

    if (code === REMOTE_KEY.GREEN) {
      if (handlers && handlers.onGreen) {
        event.preventDefault();
        handlers.onGreen();
      }
      return;
    }

    if (code === REMOTE_KEY.VOLUME_UP) {
      if (handlers && handlers.onVolumeUp) {
        event.preventDefault();
        handlers.onVolumeUp();
      }
      return;
    }

    if (code === REMOTE_KEY.VOLUME_DOWN) {
      if (handlers && handlers.onVolumeDown) {
        event.preventDefault();
        handlers.onVolumeDown();
      }
      return;
    }

    if (code === REMOTE_KEY.VOLUME_MUTE) {
      if (handlers && handlers.onVolumeMute) {
        event.preventDefault();
        handlers.onVolumeMute();
      }
      return;
    }

    if (code === REMOTE_KEY.LEFT || code === REMOTE_KEY.RIGHT
      || code === REMOTE_KEY.UP || code === REMOTE_KEY.DOWN) {
      // While the virtual keyboard is up, don't steal arrow keys for spatial nav.
      if (isEditingSettingsText(document.activeElement)) {
        return;
      }
      event.preventDefault();
      moveDirection(code);
      return;
    }
    if (code === REMOTE_KEY.ENTER) {
      const active = document.activeElement;
      // Text fields: Select unlocks edit mode and opens the virtual keyboard.
      // Merely focusing via D-pad keeps them readonly so the keyboard stays down.
      if (isSettingsTextField(active) && active.readOnly) {
        event.preventDefault();
        beginSettingsTextEdit(active);
        return;
      }
      if (isEditingSettingsText(active)) {
        // Second Select finishes editing (dismiss keyboard / lock field).
        event.preventDefault();
        endSettingsTextEdit(active);
        try { active.blur(); } catch (err) { /* ignore */ }
        try {
          active.readOnly = true;
          active.focus();
        } catch (err) { /* ignore */ }
        return;
      }
      // Photo gallery: Select chooses the photo and moves on so you can keep
      // scrolling Settings (instead of trapping focus in the thumbnail grid).
      if (active && active.classList && active.classList.contains('photo-picker-tile')) {
        event.preventDefault();
        if (typeof event.stopPropagation === 'function') event.stopPropagation();
        activatePhotoPickerTile(active);
        return;
      }
      if (active && active.classList.contains('focusable')) {
        event.preventDefault();
        active.click();
      }
    }
  }

  document.addEventListener('keydown', onKeyDown);
  // Capture phase so we see the wheel even if a child stops bubbling.
  document.addEventListener('wheel', onWheel, {passive: false, capture: true});
  root.addEventListener('mousemove', onPointerMove);

  return {
    refresh: function () {
      collect();
      // Prefer a live, on-screen focusable. Focusing a hidden settings control
      // (still .focusable in the DOM) fails silently and leaves the dock dead.
      for (let i = 0; i < items.length; i += 1) {
        if (!isFocusable(items[i])) continue;
        // Prefer app tiles / inputs over top-bar chrome when reclaiming.
        if (focusItem(items[i])) return;
      }
    },
    /** Prefer the first focusable app tile (home dock) after a resume. */
    focusHomeDock: function () {
      collect();
      const tiles = items.filter(function (item) {
        return isFocusable(item) && item.classList && item.classList.contains('app-tile');
      });
      if (tiles.length && focusItem(tiles[0])) return true;
      for (let i = 0; i < items.length; i += 1) {
        if (isFocusable(items[i]) && focusItem(items[i])) return true;
      }
      return false;
    },
    focusWithin: function (selector) {
      collect();
      const scoped = items.filter(function (item) {
        return item.closest && item.closest(selector) && isFocusable(item);
      });
      if (scoped.length) focusItem(scoped[0]);
    },
    destroy: function () {
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('wheel', onWheel, {capture: true});
      root.removeEventListener('mousemove', onPointerMove);
    }
  };
}