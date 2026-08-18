import {saveConfig, TIMEZONE_OPTIONS, coerceScreensaverMinutes} from './config.js';
import {listInstalledApps} from './apps.js';
import {loadAppCatalog, resolvePinnedApp, setIconSrc, lazyLoadIcon} from './app-catalog.js';
import {KNOWN_BUILTIN_APPS, getBuiltinAppIcon, getBuiltinAppTitle, BUILTIN_ICON_CHOICES} from './app-icons.js';
import {
  loadBuiltinManifest,
  normalizeBackgroundConfig,
  parseUrlList,
  REMOTE_BACKGROUNDS,
  findRemoteBackgroundById,
  findRemoteBackgroundByUrl,
  remoteThumbUrl,
  builtinImageUrl
} from './backgrounds.js';
import {loadBuiltinMusicManifest, normalizeMusicConfig} from './builtin-music.js';
import {applyActiveProfile, PROFILE_OPTIONS} from './profiles.js';
import {fetchInputDevices} from './inputs.js';
import {findLoungeRoots, joinPath, discoverMusicTracks} from './usb.js';
import {whoAmI} from './luna.js';
import {APP_VERSION} from './version.js';
import {
  getVoxrelayConfig,
  getVoxrelayStatus,
  setVoxrelayConfig,
  startSuperGrokLogin,
  cancelSuperGrokLogin,
  signOutSuperGrok,
  importSuperGrokAuth,
  CHAT_MODELS,
  VOICE_MODELS,
  STT_LANGUAGES
} from './voxrelay-config.js';
import {qrSvgMarkup} from './qr-svg.js';

const DEFAULT_INPUTS = ['HDMI_1', 'HDMI_2', 'HDMI_3', 'TV'];
const KEYBOARD_SCROLL_RESERVE = 420;

/**
 * Gate virtual keyboard on TV text fields.
 *
 * webOS opens the on-screen keyboard as soon as a text input is focused, which
 * is painful when D-pad scrolling through Settings. Keep fields read-only until
 * the user presses OK/Select (or clicks), then unlock for editing. On blur,
 * re-lock so the next focus via arrows won't pop the keyboard again.
 */
export function isSettingsTextField(el) {
  if (!el || !el.tagName) return false;
  const tag = el.tagName;
  if (tag === 'TEXTAREA') return true;
  if (tag !== 'INPUT') return false;
  const t = String(el.type || 'text').toLowerCase();
  return t === 'text' || t === 'number' || t === 'search' || t === 'url' || t === 'password';
}

export function isEditingSettingsText(el) {
  return isSettingsTextField(el) && el.readOnly === false && el.dataset.tvEdit === '1';
}

export function beginSettingsTextEdit(el) {
  if (!isSettingsTextField(el)) return false;
  el.readOnly = false;
  el.dataset.tvEdit = '1';
  try { el.focus(); } catch (err) { /* ignore */ }
  // Some webOS builds only raise the keyboard after a click once editable.
  try { el.click(); } catch (err) { /* ignore */ }
  return true;
}

export function endSettingsTextEdit(el) {
  if (!isSettingsTextField(el)) return;
  el.readOnly = true;
  delete el.dataset.tvEdit;
}

function gateTextField(input) {
  if (!input || input.dataset.tvKeyboardGated === '1') return;
  input.dataset.tvKeyboardGated = '1';
  input.readOnly = true;

  input.addEventListener('blur', function () {
    endSettingsTextEdit(input);
  });

  // Magic Remote pointer: click should enter edit mode (same as OK).
  input.addEventListener('click', function (event) {
    if (input.readOnly) {
      event.preventDefault();
      beginSettingsTextEdit(input);
    }
  });
}

function attachInputScrollHelpers(scrollContainer) {
  if (!scrollContainer) return;

  scrollContainer.querySelectorAll('input[type="text"], input[type="number"], textarea').forEach(function (input) {
    gateTextField(input);

    input.addEventListener('focus', function () {
      // Only scroll for keyboard when actually editing (Select pressed).
      if (input.readOnly) return;
      window.setTimeout(function () {
        const containerRect = scrollContainer.getBoundingClientRect();
        const inputRect = input.getBoundingClientRect();
        const visibleBottom = containerRect.bottom - KEYBOARD_SCROLL_RESERVE;

        if (inputRect.bottom > visibleBottom) {
          scrollContainer.scrollTop += inputRect.bottom - visibleBottom + 32;
        } else if (inputRect.top < containerRect.top + 16) {
          scrollContainer.scrollTop -= containerRect.top + 16 - inputRect.top;
        }
      }, 320);
    });
  });
}

function createOptionStepper(className, focusIndex, optionList, currentValue, onChange) {
  const el = document.createElement('div');
  el.className = 'option-stepper focusable' + (className ? ' ' + className : '');
  el.dataset.focusIndex = String(focusIndex);
  el.tabIndex = 0;

  const prev = document.createElement('span');
  prev.className = 'stepper-arrow';
  prev.textContent = '\u2039';

  const labelEl = document.createElement('span');
  labelEl.className = 'stepper-label';

  const next = document.createElement('span');
  next.className = 'stepper-arrow';
  next.textContent = '\u203A';

  el.appendChild(prev);
  el.appendChild(labelEl);
  el.appendChild(next);

  let options = optionList.slice();
  let index = 0;

  function indexOfValue(value) {
    for (let i = 0; i < options.length; i += 1) {
      if (options[i].value === value) return i;
    }
    return -1;
  }

  function render() {
    const opt = options[index] || {value: '', label: ''};
    labelEl.textContent = opt.label;
    el.value = opt.value;
    el.dataset.value = opt.value;
    prev.classList.toggle('is-disabled', index <= 0);
    next.classList.toggle('is-disabled', index >= options.length - 1);
  }

  el.__step = function (dir) {
    if (!options.length) return;
    let n = index + dir;
    if (n < 0) n = 0;
    if (n > options.length - 1) n = options.length - 1;
    if (n !== index) {
      index = n;
      render();
      if (onChange) onChange(el.value);
    }
  };

  el.setOptions = function (newOptions, newValue) {
    options = (newOptions || []).slice();
    const found = indexOfValue(newValue);
    index = found >= 0 ? found : 0;
    render();
  };

  el.setValue = function (value) {
    const found = indexOfValue(value);
    if (found >= 0 && found !== index) {
      index = found;
      render();
    }
  };

  prev.addEventListener('click', function (event) {
    event.stopPropagation();
    el.__step(-1);
  });

  next.addEventListener('click', function (event) {
    event.stopPropagation();
    el.__step(1);
  });

  el.addEventListener('click', function (event) {
    if (event.target === prev || event.target === next) return;
    el.__step(1);
  });

  const start = indexOfValue(currentValue);
  index = start >= 0 ? start : 0;
  render();
  return el;
}

export function createSettingsPanel(panel, getConfig, options) {
  let visible = false;
  let opening = false;
  let renderGen = 0;
  let builtinManifest = [];
  let pinnedOrder = [];
  let pinnedContainer = null;
  let appsByIdMap = {};
  let customApps = [];
  // After open, ignore pointer/click for a short window so the Magic Remote
  // "click" that opened Settings (gear is top-right, Close is also top-right)
  // does not immediately hit Close and dismiss the panel.
  let openGuardUntil = 0;
  let guardHandler = null;
  let aiOauthPollTimer = null;

  function findCustomApp(id) {
    for (let i = 0; i < customApps.length; i += 1) {
      if (customApps[i].id === id) return customApps[i];
    }
    return null;
  }

  function clearOpenGuard() {
    openGuardUntil = 0;
    if (guardHandler) {
      document.removeEventListener('click', guardHandler, true);
      document.removeEventListener('pointerup', guardHandler, true);
      document.removeEventListener('pointerdown', guardHandler, true);
      guardHandler = null;
    }
  }

  function armOpenGuard(ms) {
    clearOpenGuard();
    openGuardUntil = Date.now() + (ms || 600);
    guardHandler = function (event) {
      if (Date.now() >= openGuardUntil) {
        clearOpenGuard();
        return;
      }
      // Swallow the trailing OK/click from the same press that opened us.
      event.preventDefault();
      event.stopPropagation();
      if (typeof event.stopImmediatePropagation === 'function') {
        event.stopImmediatePropagation();
      }
    };
    document.addEventListener('click', guardHandler, true);
    document.addEventListener('pointerup', guardHandler, true);
    document.addEventListener('pointerdown', guardHandler, true);
  }

  function requestHide() {
    if (Date.now() < openGuardUntil) return; // ignore Close during open guard
    hide();
  }

  function hide() {
    visible = false;
    opening = false;
    renderGen += 1; // invalidate any in-flight render
    clearOpenGuard();
    if (aiOauthPollTimer) {
      clearInterval(aiOauthPollTimer);
      aiOauthPollTimer = null;
    }
    panel.hidden = true;
    document.body.classList.remove('settings-open');
    if (options.onClose) options.onClose();
  }

  /** Immediate shell so the panel is visible even while async lists load. */
  function paintLoadingShell() {
    panel.innerHTML = '';

    const header = document.createElement('div');
    header.className = 'settings-header';

    const headerTitleWrap = document.createElement('div');
    headerTitleWrap.className = 'settings-header-title';
    const headerTitle = document.createElement('h2');
    headerTitle.textContent = 'Launch Home - "Come home to what you love"';
    headerTitleWrap.appendChild(headerTitle);

    const headerActions = document.createElement('div');
    headerActions.className = 'settings-header-actions';
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'settings-close focusable';
    closeBtn.dataset.focusIndex = '900';
    closeBtn.textContent = 'Close';
    closeBtn.addEventListener('click', requestHide);
    headerActions.appendChild(closeBtn);

    header.appendChild(headerTitleWrap);
    header.appendChild(headerActions);

    const body = document.createElement('div');
    body.className = 'settings-body';
    const loading = document.createElement('p');
    loading.className = 'settings-hint settings-loading';
    loading.textContent = 'Loading settings…';
    body.appendChild(loading);

    panel.appendChild(header);
    panel.appendChild(body);
  }

  function showErrorState(message) {
    // Never leave a blank black panel.
    if (!panel.querySelector('.settings-header')) {
      paintLoadingShell();
    }
    const body = panel.querySelector('.settings-body');
    if (body) {
      body.innerHTML = '';
      const msg = document.createElement('p');
      msg.className = 'settings-hint';
      msg.textContent = message || 'Could not load settings. Press Close and try again.';
      body.appendChild(msg);
    }
  }

  function show() {
    // Already open — just re-focus (do not rebuild).
    if (visible && !opening && panel && !panel.hidden &&
        (panel.querySelector('.settings-section') || panel.querySelector('.settings-tabs'))) {
      if (options.onRendered) options.onRendered();
      return;
    }
    // Open already in progress.
    if (opening && visible && !panel.hidden) return;

    opening = true;
    visible = true;
    const gen = (renderGen += 1);

    // Paint shell BEFORE hiding underlay so we never flash empty black.
    panel.hidden = false;
    panel.removeAttribute('hidden');
    paintLoadingShell();
    document.body.classList.add('settings-open');
    armOpenGuard(700);

    if (options.onOpen) options.onOpen();
    // Delay focus until after the opening click sequence finishes, otherwise
    // focus lands on Close under the cursor and the same OK closes us.
    setTimeout(function () {
      if (!visible || gen !== renderGen) return;
      if (options.onRendered) options.onRendered();
    }, 350);

    Promise.resolve(render(gen)).then(function () {
      if (!visible || gen !== renderGen) return;
      opening = false;
      // If render bailed early without sections, keep a usable shell.
      if (!panel.querySelector('.settings-section') && !panel.querySelector('.settings-tabs')) {
        showErrorState('Settings did not finish loading. Close and open again.');
      }
      if (options.onRendered) options.onRendered();
    }).catch(function (err) {
      console.error(err);
      if (!visible || gen !== renderGen) return;
      opening = false;
      showErrorState('Could not load settings. Close and try again.');
      if (options.onRendered) options.onRendered();
    });
  }

  function labeledControl(label, control) {
    const wrap = document.createElement('div');
    wrap.className = 'settings-row';
    const span = document.createElement('span');
    span.textContent = label;
    wrap.appendChild(span);
    wrap.appendChild(control);
    return wrap;
  }

  function labeledBlock(label, control) {
    const wrap = document.createElement('div');
    wrap.className = 'settings-block';
    const heading = document.createElement('span');
    heading.className = 'settings-block-label';
    heading.textContent = label;
    wrap.appendChild(heading);
    wrap.appendChild(control);
    return wrap;
  }

  function focusPhotoGallery(galleryEl) {
    if (!galleryEl || galleryEl.hidden) return;
    window.setTimeout(function () {
      try {
        galleryEl.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      } catch (err) {
        try { galleryEl.scrollIntoView(true); } catch (err2) { /* ignore */ }
      }
      const firstTile = galleryEl.querySelector('.photo-picker-tile.focusable');
      if (firstTile && typeof firstTile.focus === 'function') {
        try { firstTile.focus(); } catch (err3) { /* ignore */ }
        firstTile.classList.add('focused');
        const siblings = galleryEl.ownerDocument.querySelectorAll(
          '#settings-panel .focusable.focused'
        );
        for (let i = 0; i < siblings.length; i += 1) {
          if (siblings[i] !== firstTile) siblings[i].classList.remove('focused');
        }
      }
    }, 80);
  }

  /**
   * After choosing a wallpaper, jump focus to Save so the user can confirm
   * and exit without scrolling the whole settings panel.
   */
  function focusSaveButton() {
    window.setTimeout(function () {
      const save = panel.querySelector('.settings-save.focusable');
      if (!save) return;
      try {
        save.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      } catch (err) {
        try { save.scrollIntoView(true); } catch (err2) { /* ignore */ }
      }
      try {
        if (typeof save.tabIndex === 'number' && save.tabIndex < 0) save.tabIndex = 0;
      } catch (err3) { /* ignore */ }
      try { save.focus(); } catch (err4) { /* ignore */ }

      const focused = panel.querySelectorAll('.focusable.focused');
      for (let i = 0; i < focused.length; i += 1) {
        if (focused[i] !== save) focused[i].classList.remove('focused');
      }
      save.classList.add('focused');

      // Match focus manager row highlight for consistency.
      const highlights = panel.querySelectorAll('.settings-row-highlight');
      for (let j = 0; j < highlights.length; j += 1) {
        highlights[j].classList.remove('settings-row-highlight');
      }
      save.classList.add('settings-row-highlight');
    }, 60);
  }

  function syncBackgroundFields(source, refs, opts) {
    const isImage = source !== 'preset' && source !== 'animated-gradient';
    const showBuiltin = source === 'builtin';
    const showUsb = source === 'usb';
    const showUrl = source === 'url';
    const isSlideshow = refs.displaySelect.value === 'slideshow';
    const revealGallery = !!(opts && opts.revealGallery);

    refs.displayRow.hidden = !isImage;
    // Built-in / online galleries are mutually exclusive by source.
    refs.builtinRow.hidden = !showBuiltin || isSlideshow;
    refs.usbHint.hidden = !showUsb;
    refs.usbFileRow.hidden = !showUsb || isSlideshow;
    refs.remoteRow.hidden = !showUrl || isSlideshow;
    refs.urlHint.hidden = !showUrl;
    refs.urlRow.hidden = !showUrl || isSlideshow;
    refs.urlsRow.hidden = !showUrl || !isSlideshow;
    refs.intervalRow.hidden = !isImage || !isSlideshow;
    refs.kenBurnsRow.hidden = !isImage;

    // When the user switches Source to a photo catalog, scroll that gallery into view.
    if (revealGallery && !isSlideshow) {
      if (showBuiltin && refs.builtinRow && !refs.builtinRow.hidden) {
        focusPhotoGallery(refs.builtinRow);
      } else if (showUrl && refs.remoteRow && !refs.remoteRow.hidden) {
        focusPhotoGallery(refs.remoteRow);
      }
    }
  }

  function movePinned(index, delta) {
    const next = index + delta;
    if (next < 0 || next >= pinnedOrder.length) return;
    const tmp = pinnedOrder[index];
    pinnedOrder[index] = pinnedOrder[next];
    pinnedOrder[next] = tmp;
    if (pinnedContainer) renderPinnedList(pinnedContainer);
  }

  function renderPinnedList(container) {
    container.innerHTML = '';

    pinnedOrder.forEach(function (appId, index) {
      const app = appsByIdMap[appId] || {id: appId, title: appId, icon: ''};
      const row = document.createElement('div');
      row.className = 'settings-pinned-row';

      if (app.icon) {
        const icon = document.createElement('img');
        icon.className = 'settings-app-icon';
        icon.src = app.icon;
        icon.alt = '';
        row.appendChild(icon);
      }

      const title = document.createElement('span');
      title.className = 'settings-pinned-title';
      title.textContent = app.title || app.id;

      const upBtn = document.createElement('button');
      upBtn.type = 'button';
      upBtn.className = 'settings-mini-btn focusable';
      upBtn.dataset.focusIndex = String(1200 + index * 3);
      upBtn.textContent = '↑';
      upBtn.disabled = index === 0;
      upBtn.addEventListener('click', function () { movePinned(index, -1); });

      const downBtn = document.createElement('button');
      downBtn.type = 'button';
      downBtn.className = 'settings-mini-btn focusable';
      downBtn.dataset.focusIndex = String(1201 + index * 3);
      downBtn.textContent = '↓';
      downBtn.disabled = index === pinnedOrder.length - 1;
      downBtn.addEventListener('click', function () { movePinned(index, 1); });

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'settings-mini-btn focusable';
      removeBtn.dataset.focusIndex = String(1202 + index * 3);
      removeBtn.textContent = '✕';
      removeBtn.addEventListener('click', function () {
        const removedId = pinnedOrder[index];
        pinnedOrder.splice(index, 1);
        for (let i = customApps.length - 1; i >= 0; i -= 1) {
          if (customApps[i].id === removedId) customApps.splice(i, 1);
        }
        renderPinnedList(container);
      });

      row.appendChild(title);
      row.appendChild(upBtn);
      row.appendChild(downBtn);
      row.appendChild(removeBtn);
      container.appendChild(row);
    });
  }

  function withDeadline(promise, ms) {
    return new Promise(function (resolve) {
      let done = false;
      const timer = setTimeout(function () {
        if (!done) { done = true; resolve(null); }
      }, ms);
      Promise.resolve(promise).then(function (value) {
        if (!done) { done = true; clearTimeout(timer); resolve(value); }
      }, function () {
        if (!done) { done = true; clearTimeout(timer); resolve(null); }
      });
    });
  }

  async function render(gen) {
    const config = getConfig();
    const effective = applyActiveProfile(config);
    const bg = normalizeBackgroundConfig(effective.background);
    // Cap waits so the panel never sits empty after we clear the loading shell.
    const loaded = await withDeadline(loadBuiltinManifest(), 4000);
    if (gen !== renderGen || !visible) return;
    builtinManifest = loaded || builtinManifest || [];

    pinnedOrder = (config.launcher.pinnedApps || []).slice();
    customApps = (config.launcher.customApps || []).map(function (entry) {
      return Object.assign({}, entry);
    });

    // Keep the loading shell until we have a header ready, then replace in one go
    // via a document fragment so the panel is never empty/black.
    const next = document.createDocumentFragment();

    const header = document.createElement('div');
    header.className = 'settings-header';

    const headerTitleWrap = document.createElement('div');
    headerTitleWrap.className = 'settings-header-title';

    const headerTitle = document.createElement('h2');
    headerTitle.textContent = 'Launch Home - "Come home to what you love"';

    const versionLabel = document.createElement('p');
    versionLabel.className = 'settings-version';
    versionLabel.textContent = 'Settings · Version ' + APP_VERSION;

    // Effective user the launcher runs privileged calls as (root when the
    // Homebrew Channel service is elevated). Tells the user whether app scanning,
    // which needs root, will work. Populated asynchronously via the Luna bus.
    const runUserLabel = document.createElement('p');
    runUserLabel.className = 'settings-run-user run-user-checking';
    runUserLabel.textContent = 'Running as: checking…';
    whoAmI().then(function (user) {
      if (!user) {
        runUserLabel.textContent = 'Running as: unknown (not rooted / no Homebrew Channel)';
        runUserLabel.className = 'settings-run-user run-user-limited';
        return;
      }
      const isRoot = user === 'root';
      runUserLabel.textContent = 'Running as: ' + user +
        (isRoot ? ' — app scanning available' : ' — not root, app scanning may be limited');
      runUserLabel.className = 'settings-run-user ' + (isRoot ? 'run-user-root' : 'run-user-limited');
    }).catch(function () {
      runUserLabel.textContent = 'Running as: unknown';
      runUserLabel.className = 'settings-run-user run-user-limited';
    });

    headerTitleWrap.appendChild(headerTitle);
    headerTitleWrap.appendChild(versionLabel);
    headerTitleWrap.appendChild(runUserLabel);

    const headerActions = document.createElement('div');
    headerActions.className = 'settings-header-actions';

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'settings-close focusable';
    closeBtn.dataset.focusIndex = '900';
    closeBtn.textContent = 'Close';
    closeBtn.addEventListener('click', requestHide);
    headerActions.appendChild(closeBtn);

    header.appendChild(headerTitleWrap);
    header.appendChild(headerActions);
    next.appendChild(header);

    const body = document.createElement('div');
    body.className = 'settings-body';
    next.appendChild(body);

    // Two tabs: Home (launcher) and AI Voice (VoxRelay).
    let activeSettingsTab = 'home';
    const tabsBar = document.createElement('div');
    tabsBar.className = 'settings-tabs';
    tabsBar.setAttribute('role', 'tablist');

    const homeTabBtn = document.createElement('button');
    homeTabBtn.type = 'button';
    homeTabBtn.className = 'settings-tab focusable active';
    homeTabBtn.dataset.focusIndex = '890';
    homeTabBtn.setAttribute('role', 'tab');
    homeTabBtn.setAttribute('aria-selected', 'true');
    homeTabBtn.dataset.tab = 'home';
    homeTabBtn.textContent = 'Home';

    const aiTabBtn = document.createElement('button');
    aiTabBtn.type = 'button';
    aiTabBtn.className = 'settings-tab focusable';
    aiTabBtn.dataset.focusIndex = '891';
    aiTabBtn.setAttribute('role', 'tab');
    aiTabBtn.setAttribute('aria-selected', 'false');
    aiTabBtn.dataset.tab = 'ai';
    aiTabBtn.textContent = 'AI Voice';

    tabsBar.appendChild(homeTabBtn);
    tabsBar.appendChild(aiTabBtn);
    body.appendChild(tabsBar);

    // Stack both panes (CSS) so tab switches only flip visibility — no full
    // reflow of the heavy Home tree (photos, app lists) on every Left/Right.
    const panesWrap = document.createElement('div');
    panesWrap.className = 'settings-tab-panes';

    const homePane = document.createElement('div');
    homePane.className = 'settings-tab-pane is-active';
    homePane.dataset.tab = 'home';
    homePane.setAttribute('role', 'tabpanel');

    const aiPane = document.createElement('div');
    aiPane.className = 'settings-tab-pane';
    aiPane.dataset.tab = 'ai';
    aiPane.setAttribute('role', 'tabpanel');
    aiPane.setAttribute('aria-hidden', 'true');

    panesWrap.appendChild(homePane);
    panesWrap.appendChild(aiPane);
    body.appendChild(panesWrap);

    // Declared before setSettingsTab so the early Home default cannot hit a TDZ.
    let saveBtn = null;
    let aiStatusLabel = null;
    let aiApiKeyInput = null;
    let aiApiKeyHint = null;
    let aiApiKeyShowBtn = null;
    let aiGeminiKeyInput = null;
    let aiGeminiKeyHint = null;
    let aiGeminiKeyShowBtn = null;
    let aiSttSelect = null;
    let aiChatSelect = null;
    let aiVoiceModelSelect = null;
    let aiDismissSelect = null;
    let aiAuthModeSelect = null;
    let aiCreditBanner = null;
    let aiOauthStatus = null;
    let aiOauthCode = null;
    let aiOauthHint = null;
    let aiOauthQrWrap = null;
    let aiOauthSignInBtn = null;
    let aiOauthImportBtn = null;
    let aiOauthSignOutBtn = null;
    let aiLoadedConfig = null;
    let aiXaiKeyRevealed = false;
    let aiGeminiKeyRevealed = false;

    function setSettingsTab(tabId, opts) {
      const tabOpts = opts || {};
      const next = tabId === 'ai' ? 'ai' : 'home';
      // No-op if already on this tab (avoids thrashing on repeated focus events).
      if (next === activeSettingsTab && !tabOpts.focusContent && !tabOpts.force) {
        return;
      }
      activeSettingsTab = next;
      const isHome = activeSettingsTab === 'home';
      // Class toggles only — CSS keeps inactive pane out of interaction without
      // display:none reflow of the entire Home settings tree.
      homePane.classList.toggle('is-active', isHome);
      aiPane.classList.toggle('is-active', !isHome);
      homePane.setAttribute('aria-hidden', isHome ? 'false' : 'true');
      aiPane.setAttribute('aria-hidden', isHome ? 'true' : 'false');
      homeTabBtn.classList.toggle('active', isHome);
      aiTabBtn.classList.toggle('active', !isHome);
      homeTabBtn.setAttribute('aria-selected', isHome ? 'true' : 'false');
      aiTabBtn.setAttribute('aria-selected', isHome ? 'false' : 'true');
      if (saveBtn && saveBtn.textContent !== 'Save' && saveBtn.textContent !== 'Saving…') {
        saveBtn.textContent = 'Save';
      }
      // Only dive into pane content when the user activates the tab (click/OK),
      // not when D-pad Right merely highlights the other tab.
      if (tabOpts.focusContent) {
        const pane = isHome ? homePane : aiPane;
        let first = pane.querySelector('.photo-picker-tile.focusable');
        if (!first) first = pane.querySelector('.focusable');
        if (first && typeof first.focus === 'function') {
          try { first.focus(); } catch (err) { /* ignore */ }
          first.classList.add('focused');
        }
      }
    }
    // Pane flips only on click / explicit Left-Right (settings-activate-tab).
    // A bare focus on Home (cursor or wheel) must not leave AI Voice.
    homeTabBtn.addEventListener('settings-activate-tab', function () {
      setSettingsTab('home');
    });
    aiTabBtn.addEventListener('settings-activate-tab', function () {
      setSettingsTab('ai');
    });
    homeTabBtn.addEventListener('click', function () {
      setSettingsTab('home', {focusContent: true, force: true});
    });
    aiTabBtn.addEventListener('click', function () {
      setSettingsTab('ai', {focusContent: true, force: true});
    });
    // Ensure Home is the selected tab when the panel finishes building.
    setSettingsTab('home', {force: true});

    const aiStatusSection = document.createElement('section');
    aiStatusSection.className = 'settings-section';
    aiStatusSection.innerHTML = '<h3>VoxRelay status</h3>';
    aiStatusLabel = document.createElement('p');
    aiStatusLabel.className = 'settings-hint ai-status-line';
    aiStatusLabel.textContent = 'Checking VoxRelay…';
    aiStatusSection.appendChild(aiStatusLabel);
    aiCreditBanner = document.createElement('p');
    aiCreditBanner.className = 'ai-credit-banner';
    aiCreditBanner.hidden = true;
    aiStatusSection.appendChild(aiCreditBanner);
    const aiStatusHint = document.createElement('p');
    aiStatusHint.className = 'settings-hint';
    aiStatusHint.textContent =
      'Voice is optional and uses a separate service on this TV. Launch Home does not include that service.';
    aiStatusSection.appendChild(aiStatusHint);
    aiPane.appendChild(aiStatusSection);

    function makeKeyRow(label, focusIndex, placeholder) {
      const wrap = document.createElement('div');
      wrap.className = 'settings-row settings-key-row';
      const span = document.createElement('span');
      span.textContent = label;
      const controls = document.createElement('div');
      controls.className = 'settings-key-controls';
      const input = document.createElement('input');
      input.type = 'password';
      input.className = 'settings-text focusable';
      input.dataset.focusIndex = String(focusIndex);
      input.placeholder = placeholder || '';
      input.spellcheck = false;
      input.autocomplete = 'off';
      const showBtn = document.createElement('button');
      showBtn.type = 'button';
      showBtn.className = 'settings-mini-btn focusable';
      showBtn.dataset.focusIndex = String(focusIndex + 1);
      showBtn.textContent = 'Show';
      controls.appendChild(input);
      controls.appendChild(showBtn);
      wrap.appendChild(span);
      wrap.appendChild(controls);
      return {row: wrap, input: input, showBtn: showBtn};
    }

    const aiKeySection = document.createElement('section');
    aiKeySection.className = 'settings-section';
    aiKeySection.innerHTML = '<h3>API keys</h3>';
    aiAuthModeSelect = createOptionStepper('', 1990, [
      {value: 'API_KEY', label: 'xAI API key'},
      {value: 'SUPERGROK_OAUTH', label: 'SuperGrok Heavy'}
    ], 'API_KEY', function () {
      syncAuthModeUi();
      if (isSuperGrokMode()) paintOauthFromStatus({}, aiLoadedConfig);
    });
    aiKeySection.appendChild(labeledControl('Authentication', aiAuthModeSelect));
    const authHint = document.createElement('p');
    authHint.className = 'settings-hint';
    authHint.textContent = 'Get a key at console.x.ai — stored only on this TV.';
    aiKeySection.appendChild(authHint);

    const oauthBox = document.createElement('div');
    oauthBox.hidden = true;
    aiOauthStatus = document.createElement('p');
    aiOauthStatus.className = 'settings-hint';
    aiOauthStatus.textContent = 'SuperGrok: not signed in';
    oauthBox.appendChild(aiOauthStatus);
    const oauthPending = document.createElement('div');
    oauthPending.className = 'ai-oauth-pending';
    aiOauthQrWrap = document.createElement('div');
    aiOauthQrWrap.className = 'ai-oauth-qr-wrap';
    aiOauthQrWrap.hidden = true;
    aiOauthQrWrap.setAttribute('aria-hidden', 'true');
    oauthPending.appendChild(aiOauthQrWrap);
    const oauthPendingText = document.createElement('div');
    oauthPendingText.className = 'ai-oauth-pending-text';
    aiOauthCode = document.createElement('p');
    aiOauthCode.className = 'ai-oauth-code';
    aiOauthCode.hidden = true;
    oauthPendingText.appendChild(aiOauthCode);
    aiOauthHint = document.createElement('p');
    aiOauthHint.className = 'settings-hint';
    aiOauthHint.hidden = true;
    oauthPendingText.appendChild(aiOauthHint);
    oauthPending.appendChild(oauthPendingText);
    oauthBox.appendChild(oauthPending);

    function hideOauthQr() {
      if (!aiOauthQrWrap) return;
      aiOauthQrWrap.hidden = true;
      aiOauthQrWrap.setAttribute('aria-hidden', 'true');
      aiOauthQrWrap.innerHTML = '';
    }

    function showOauthQr(uri) {
      if (!aiOauthQrWrap) return;
      const svg = qrSvgMarkup(uri);
      if (!svg) {
        hideOauthQr();
        return;
      }
      aiOauthQrWrap.innerHTML = svg;
      aiOauthQrWrap.hidden = false;
      aiOauthQrWrap.setAttribute('aria-hidden', 'false');
    }

    function isSuperGrokMode() {
      return !!(aiAuthModeSelect && aiAuthModeSelect.value === 'SUPERGROK_OAUTH');
    }

    function showOauthPending(code, uri) {
      if (!isSuperGrokMode()) {
        hideOauthQr();
        if (aiOauthCode) aiOauthCode.hidden = true;
        if (aiOauthHint) aiOauthHint.hidden = true;
        return;
      }
      const url = uri || 'https://accounts.x.ai/oauth2/device';
      aiOauthStatus.textContent = 'Scan the QR code, then approve this code on your phone';
      aiOauthStatus.className = 'settings-hint';
      aiOauthCode.hidden = false;
      aiOauthCode.textContent = code || '';
      aiOauthHint.hidden = false;
      aiOauthHint.textContent = 'Or open ' + url + ' and enter the code. Waiting for approval…';
      showOauthQr(url);
    }

    function syncAuthModeUi() {
      const heavy = isSuperGrokMode();
      if (oauthBox) oauthBox.hidden = !heavy;
      if (authHint) {
        authHint.textContent = heavy
          ? 'SuperGrok Heavy uses the same sign-in as Grok Build / `grok login`. TV voice still calls api.x.ai and can fail if that team is out of API credits.'
          : 'Get a key at console.x.ai — stored only on this TV. Leave blank to keep the current key.';
      }
      if (!heavy) {
        hideOauthQr();
        if (aiOauthCode) aiOauthCode.hidden = true;
        if (aiOauthHint) aiOauthHint.hidden = true;
        stopOauthPoll();
      }
    }
    const oauthRow = document.createElement('div');
    oauthRow.className = 'ai-oauth-row';
    aiOauthSignInBtn = document.createElement('button');
    aiOauthSignInBtn.type = 'button';
    aiOauthSignInBtn.className = 'settings-mini-btn focusable';
    aiOauthSignInBtn.dataset.focusIndex = '1991';
    aiOauthSignInBtn.textContent = 'Sign in with SuperGrok';
    aiOauthImportBtn = document.createElement('button');
    aiOauthImportBtn.type = 'button';
    aiOauthImportBtn.className = 'settings-mini-btn focusable';
    aiOauthImportBtn.dataset.focusIndex = '1992';
    aiOauthImportBtn.textContent = 'Import grok login file';
    aiOauthSignOutBtn = document.createElement('button');
    aiOauthSignOutBtn.type = 'button';
    aiOauthSignOutBtn.className = 'settings-mini-btn focusable';
    aiOauthSignOutBtn.dataset.focusIndex = '1993';
    aiOauthSignOutBtn.textContent = 'Sign out';
    oauthRow.appendChild(aiOauthSignInBtn);
    oauthRow.appendChild(aiOauthImportBtn);
    oauthRow.appendChild(aiOauthSignOutBtn);
    oauthBox.appendChild(oauthRow);
    aiKeySection.appendChild(oauthBox);
    syncAuthModeUi();

    function stopOauthPoll() {
      if (aiOauthPollTimer) {
        clearInterval(aiOauthPollTimer);
        aiOauthPollTimer = null;
      }
    }

    function paintOauthFromStatus(status, cfg) {
      syncAuthModeUi();
      if (!isSuperGrokMode()) return;
      const signedIn = !!(status && status.oauthSignedIn) ||
        !!(cfg && cfg.oauth_signed_in);
      const email = (status && status.oauthEmail) || (cfg && cfg.oauth_email) || '';
      const pending = (status && status.oauthPending) || (cfg && cfg.oauth_pending);
      const err = (status && status.oauthError) || (cfg && cfg.oauth_error) || '';
      if (signedIn) {
        if (aiAuthModeSelect && aiAuthModeSelect.value !== 'SUPERGROK_OAUTH') {
          aiAuthModeSelect.value = 'SUPERGROK_OAUTH';
          if (typeof aiAuthModeSelect.setValue === 'function') {
            aiAuthModeSelect.setValue('SUPERGROK_OAUTH');
          }
          syncAuthModeUi();
        }
        aiOauthStatus.textContent = email
          ? ('SuperGrok signed in as ' + email)
          : 'SuperGrok signed in';
        aiOauthStatus.className = 'settings-hint ai-status-ok';
        aiOauthCode.hidden = true;
        aiOauthHint.hidden = true;
        hideOauthQr();
        aiOauthSignOutBtn.hidden = false;
        aiOauthSignInBtn.textContent = 'Sign in again';
        stopOauthPoll();
      } else if (pending && pending.userCode) {
        showOauthPending(
          pending.userCode,
          pending.verificationUri || 'https://accounts.x.ai/oauth2/device'
        );
        aiOauthSignOutBtn.hidden = true;
        aiOauthSignInBtn.textContent = 'Cancel sign-in';
      } else {
        aiOauthStatus.textContent = err
          ? ('SuperGrok: ' + err)
          : 'SuperGrok: not signed in';
        aiOauthStatus.className = err ? 'settings-hint ai-status-warn' : 'settings-hint';
        aiOauthCode.hidden = true;
        aiOauthHint.hidden = true;
        hideOauthQr();
        aiOauthSignOutBtn.hidden = true;
        aiOauthSignInBtn.textContent = 'Sign in with SuperGrok';
        stopOauthPoll();
      }
    }

    function pollOauthUntilDone() {
      stopOauthPoll();
      aiOauthPollTimer = setInterval(function () {
        if (!visible) {
          stopOauthPoll();
          return;
        }
        getVoxrelayStatus().then(function (status) {
          paintOauthFromStatus(status || {}, aiLoadedConfig);
          if (status && status.oauthSignedIn) {
            if (options.onToast) options.onToast('SuperGrok signed in');
            loadAiTab();
          }
        }).catch(function () { /* ignore */ });
      }, 2000);
    }

    aiOauthSignInBtn.addEventListener('click', function () {
      const pending = aiLoadedConfig && aiLoadedConfig.oauth_pending;
      if (aiOauthSignInBtn.textContent.indexOf('Cancel') === 0 ||
          (pending && pending.userCode)) {
        cancelSuperGrokLogin().then(function () {
          loadAiTab();
        }).catch(function (err) {
          if (options.onToast) options.onToast((err && err.message) || 'Cancel failed');
        });
        return;
      }
      aiOauthSignInBtn.disabled = true;
      startSuperGrokLogin().then(function (res) {
        aiOauthSignInBtn.disabled = false;
        const code = (res && res.userCode) || '';
        const uri = (res && res.verificationUri) || 'https://accounts.x.ai/oauth2/device';
        showOauthPending(code, uri);
        aiOauthSignInBtn.textContent = 'Cancel sign-in';
        if (options.onToast) options.onToast('SuperGrok code: ' + code);
        pollOauthUntilDone();
      }).catch(function (err) {
        aiOauthSignInBtn.disabled = false;
        if (options.onToast) {
          options.onToast((err && err.message) || 'Could not start SuperGrok sign-in');
        }
      });
    });
    aiOauthImportBtn.addEventListener('click', function () {
      aiOauthImportBtn.disabled = true;
      importSuperGrokAuth().then(function (res) {
        aiOauthImportBtn.disabled = false;
        if (options.onToast) {
          options.onToast(res && res.email
            ? ('Imported SuperGrok for ' + res.email)
            : 'Imported SuperGrok session');
        }
        loadAiTab();
      }).catch(function (err) {
        aiOauthImportBtn.disabled = false;
        if (options.onToast) {
          options.onToast((err && err.message) ||
            'Copy ~/.grok/auth.json to /home/root/.grok/auth.json on the TV');
        }
      });
    });
    aiOauthSignOutBtn.addEventListener('click', function () {
      signOutSuperGrok().then(function () {
        if (aiAuthModeSelect) {
          aiAuthModeSelect.value = 'API_KEY';
          if (typeof aiAuthModeSelect.setValue === 'function') {
            aiAuthModeSelect.setValue('API_KEY');
          }
        }
        if (options.onToast) options.onToast('Signed out of SuperGrok');
        loadAiTab();
      }).catch(function (err) {
        if (options.onToast) options.onToast((err && err.message) || 'Sign out failed');
      });
    });

    const xaiRow = makeKeyRow('Grok (xAI) key', 2001, 'xai-…');
    aiApiKeyInput = xaiRow.input;
    aiApiKeyShowBtn = xaiRow.showBtn;
    aiKeySection.appendChild(xaiRow.row);
    aiApiKeyHint = document.createElement('p');
    aiApiKeyHint.className = 'settings-hint';
    aiApiKeyHint.textContent = 'Get a key at console.x.ai — stored only on this TV. Leave blank to keep the current key.';
    aiKeySection.appendChild(aiApiKeyHint);

    const gemRow = makeKeyRow('Gemini key', 2003, 'AIza…');
    aiGeminiKeyInput = gemRow.input;
    aiGeminiKeyShowBtn = gemRow.showBtn;
    aiKeySection.appendChild(gemRow.row);
    aiGeminiKeyHint = document.createElement('p');
    aiGeminiKeyHint.className = 'settings-hint';
    aiGeminiKeyHint.textContent = 'Optional. Google AI Studio key (usually starts with AIza). Leave blank to keep current.';
    aiKeySection.appendChild(aiGeminiKeyHint);

    aiApiKeyShowBtn.addEventListener('click', function () {
      aiXaiKeyRevealed = !aiXaiKeyRevealed;
      const full = (aiLoadedConfig && aiLoadedConfig.xai_api_key_full) || '';
      const typed = (aiApiKeyInput.value || '').trim();
      if (aiXaiKeyRevealed) {
        aiApiKeyInput.type = 'text';
        if (!typed && full) aiApiKeyInput.value = full;
        aiApiKeyShowBtn.textContent = 'Hide';
      } else {
        aiApiKeyInput.type = 'password';
        if (full && aiApiKeyInput.value === full) aiApiKeyInput.value = '';
        aiApiKeyShowBtn.textContent = 'Show';
      }
    });
    aiGeminiKeyShowBtn.addEventListener('click', function () {
      aiGeminiKeyRevealed = !aiGeminiKeyRevealed;
      const full = (aiLoadedConfig && aiLoadedConfig.gemini_api_key_full) || '';
      const typed = (aiGeminiKeyInput.value || '').trim();
      if (aiGeminiKeyRevealed) {
        aiGeminiKeyInput.type = 'text';
        if (!typed && full) aiGeminiKeyInput.value = full;
        aiGeminiKeyShowBtn.textContent = 'Hide';
      } else {
        aiGeminiKeyInput.type = 'password';
        if (full && aiGeminiKeyInput.value === full) aiGeminiKeyInput.value = '';
        aiGeminiKeyShowBtn.textContent = 'Show';
      }
    });

    const lunaHowto = document.createElement('div');
    lunaHowto.className = 'settings-hint settings-howto';
    lunaHowto.innerHTML =
      '<strong>Set a key from a PC (SSH as root)</strong><br>' +
      'Grok / xAI:<br>' +
      '<code>luna-send -n 1 -f luna://com.webos.service.voxrelay/setConfig \'{"xai_api_key":"xai-YOUR_KEY"}\'</code>' +
      'Gemini:<br>' +
      '<code>luna-send -n 1 -f luna://com.webos.service.voxrelay/setConfig \'{"gemini_api_key":"AIza-YOUR_KEY"}\'</code>';
    aiKeySection.appendChild(lunaHowto);
    aiPane.appendChild(aiKeySection);

    const aiVoiceSection = document.createElement('section');
    aiVoiceSection.className = 'settings-section';
    aiVoiceSection.innerHTML = '<h3>Voice & chat</h3>';
    aiSttSelect = createOptionStepper('', 2010,
      STT_LANGUAGES.map(function (e) { return {value: e.value, label: e.label}; }),
      'en');
    aiVoiceSection.appendChild(labeledControl('Speech language', aiSttSelect));
    aiVoiceModelSelect = createOptionStepper('', 2011,
      VOICE_MODELS.map(function (e) { return {value: e.value, label: e.label}; }),
      'grok-voice-think-fast-2.0');
    aiVoiceSection.appendChild(labeledControl('Voice model', aiVoiceModelSelect));
    aiChatSelect = createOptionStepper('', 2012,
      CHAT_MODELS.map(function (e) { return {value: e.value, label: e.label}; }),
      'grok-4.6');
    aiVoiceSection.appendChild(labeledControl('Chat model', aiChatSelect));
    aiDismissSelect = createOptionStepper('', 2013, [
      {value: '6', label: '6 seconds'},
      {value: '8', label: '8 seconds'},
      {value: '10', label: '10 seconds'},
      {value: '12', label: '12 seconds'},
      {value: '20', label: '20 seconds'},
      {value: '30', label: '30 seconds'}
    ], '8');
    aiVoiceSection.appendChild(labeledControl('Answer card auto-dismiss', aiDismissSelect));
    const aiSaveHint = document.createElement('p');
    aiSaveHint.className = 'settings-hint';
    aiSaveHint.textContent = 'Press Save after changes. Key changes restart the voice daemon.';
    aiVoiceSection.appendChild(aiSaveHint);
    aiPane.appendChild(aiVoiceSection);

    function applyAiConfigToForm(cfg) {
      aiLoadedConfig = cfg || {};
      aiXaiKeyRevealed = false;
      aiGeminiKeyRevealed = false;
      const xaiConfigured = !!(aiLoadedConfig.xai_api_key_configured ||
        (aiLoadedConfig.xai_api_key_masked && aiLoadedConfig.api_key_configured));
      const gemConfigured = !!aiLoadedConfig.gemini_api_key_configured ||
        !!(aiLoadedConfig.gemini_api_key_masked);
      const xaiMasked = aiLoadedConfig.xai_api_key_masked || '';
      const gemMasked = aiLoadedConfig.gemini_api_key_masked || '';
      aiApiKeyInput.type = 'password';
      aiApiKeyInput.value = '';
      aiApiKeyInput.placeholder = xaiConfigured ? (xaiMasked || '••••••••') : 'xai-…';
      aiApiKeyShowBtn.textContent = 'Show';
      aiApiKeyHint.textContent = xaiConfigured
        ? ('Current Grok key: ' + xaiMasked + ' — leave blank to keep it')
        : 'Optional if SuperGrok is signed in. Get a key at console.x.ai';
      const authMode = (aiLoadedConfig.oauth_signed_in && 'SUPERGROK_OAUTH') ||
        aiLoadedConfig.auth_mode || 'API_KEY';
      if (aiAuthModeSelect) {
        aiAuthModeSelect.value = authMode;
        if (typeof aiAuthModeSelect.setValue === 'function') {
          aiAuthModeSelect.setValue(authMode);
        }
      }
      syncAuthModeUi();
      paintOauthFromStatus({}, aiLoadedConfig);
      aiGeminiKeyInput.type = 'password';
      aiGeminiKeyInput.value = '';
      aiGeminiKeyInput.placeholder = gemConfigured ? (gemMasked || '••••••••') : 'AIza…';
      aiGeminiKeyShowBtn.textContent = 'Show';
      aiGeminiKeyHint.textContent = gemConfigured
        ? ('Current Gemini key: ' + gemMasked + ' — leave blank to keep it')
        : 'Optional Gemini key (Google AI Studio).';
      aiSttSelect.value = aiLoadedConfig.stt_language || 'en';
      if (typeof aiSttSelect.setValue === 'function') aiSttSelect.setValue(aiSttSelect.value);
      // Map removed model ids to the remaining choices.
      let chatModel = aiLoadedConfig.chat_model || 'grok-4.6';
      if (chatModel !== 'grok-4.6' && chatModel !== 'grok-4.5') {
        chatModel = 'grok-4.6';
      }
      aiChatSelect.value = chatModel;
      if (typeof aiChatSelect.setValue === 'function') aiChatSelect.setValue(chatModel);
      let voiceModel = aiLoadedConfig.voice_model || 'grok-voice-think-fast-2.0';
      if (voiceModel === 'grok-voice-latest') voiceModel = 'grok-voice-think-fast-2.0';
      aiVoiceModelSelect.value = voiceModel;
      if (typeof aiVoiceModelSelect.setValue === 'function') {
        aiVoiceModelSelect.setValue(voiceModel);
      }
      let dismissN = Math.round(Number(aiLoadedConfig.overlay_auto_dismiss_sec));
      if (!isFinite(dismissN) || dismissN < 3) dismissN = 8;
      const dismiss = String(dismissN);
      aiDismissSelect.value = dismiss;
      if (typeof aiDismissSelect.setValue === 'function') aiDismissSelect.setValue(dismiss);
    }

    function loadAiTab() {
      Promise.all([
        getVoxrelayConfig().catch(function (err) { return {__error: err}; }),
        getVoxrelayStatus().catch(function () { return {}; })
      ]).then(function (results) {
        if (gen !== renderGen || !visible) return;
        const cfg = results[0] || {};
        const status = results[1] || {};
        if (cfg.__error) {
          aiStatusLabel.textContent = 'VoxRelay not reachable — is it installed?';
          aiStatusLabel.className = 'settings-hint ai-status-line ai-status-warn';
          return;
        }
        applyAiConfigToForm(cfg);
        paintOauthFromStatus(status, cfg);
        const lastErr = status.lastXaiError || cfg.last_xai_error;
        if (aiCreditBanner) {
          if (lastErr && lastErr.message) {
            aiCreditBanner.hidden = false;
            aiCreditBanner.textContent = lastErr.message;
          } else {
            aiCreditBanner.hidden = true;
            aiCreditBanner.textContent = '';
          }
        }
        const ready = !!(cfg.api_key_configured || status.oauthSignedIn ||
          cfg.oauth_signed_in);
        if (lastErr && lastErr.message) {
          aiStatusLabel.textContent = lastErr.kind === 'credits'
            ? 'Voice blocked — xAI API credits / spend limit'
            : 'Voice blocked — xAI API error';
          aiStatusLabel.className = 'settings-hint ai-status-line ai-status-err';
        } else if (status.daemonActive) {
          aiStatusLabel.textContent = ready
            ? 'Voice daemon running — ready'
            : 'Daemon running — API key or SuperGrok sign-in still needed';
          aiStatusLabel.className = 'settings-hint ai-status-line ' +
            (ready ? 'ai-status-ok' : 'ai-status-warn');
        } else {
          aiStatusLabel.textContent = ready
            ? 'Signed in — daemon not running (Save AI to restart)'
            : 'VoxRelay idle — add API key or SuperGrok, then Save AI';
          aiStatusLabel.className = 'settings-hint ai-status-line ai-status-warn';
        }
      });
    }

    const profileSection = document.createElement('section');
    profileSection.className = 'settings-section';
    profileSection.innerHTML = '<h3>Profile</h3>';

    const profileSelect = createOptionStepper('', 901,
      PROFILE_OPTIONS.map(function (entry) {
        return {value: entry.id, label: entry.label};
      }),
      config.profile || 'default');
    profileSection.appendChild(labeledControl('Active profile', profileSelect));

    const profileHint = document.createElement('p');
    profileHint.className = 'settings-hint';
    profileHint.textContent = 'Night lowers ambient volume and darkens the scrim. Cinema disables music and uses a dark gradient.';
    profileSection.appendChild(profileHint);
    homePane.appendChild(profileSection);

    const section = document.createElement('section');
    section.className = 'settings-section';
    section.innerHTML = '<h3>Background</h3>';

    const sourceSelect = createOptionStepper('', 902, [
      {value: 'preset', label: 'Gradient'},
      {value: 'animated-gradient', label: 'Animated Gradient'},
      {value: 'builtin', label: 'Built-in photos'},
      {value: 'usb', label: 'USB folder'},
      {value: 'url', label: 'Online URL (nature + anime)'}
    ], bg.source, function (value) {
      syncFields({revealGallery: value === 'url' || value === 'builtin'});
    });
    section.appendChild(labeledControl('Source', sourceSelect));

    const presetSelect = createOptionStepper('', 903, [
      {value: 'warm-gradient', label: 'Warm gradient'},
      {value: 'cool-gradient', label: 'Cool gradient'},
      {value: 'midnight', label: 'Midnight'},
      {value: 'ember', label: 'Ember'}
    ], bg.preset);
    // Only shown when Source is Gradient / Animated Gradient (not when Photo is set).
    const presetRow = labeledControl('Gradient style', presetSelect);
    section.appendChild(presetRow);

    const displaySelect = createOptionStepper('', 904, [
      {value: 'static', label: 'Single image'},
      {value: 'slideshow', label: 'Slideshow'}
    ], bg.mode, function () {
      syncFields();
    });
    const displayRow = labeledControl('Display', displaySelect);
    section.appendChild(displayRow);

    // Built-in photo gallery (thumbnails) — only visible when Source is Built-in photos.
    let selectedBuiltinId = bg.builtin || (builtinManifest[0] && builtinManifest[0].id) || '';
    const builtinRow = document.createElement('div');
    builtinRow.className = 'settings-block photo-picker-block';
    const builtinHeading = document.createElement('span');
    builtinHeading.className = 'settings-block-label';
    builtinHeading.textContent = 'Choose a built-in photo (~3840px, sharp on 4K)';
    builtinRow.appendChild(builtinHeading);

    const builtinGrid = document.createElement('div');
    builtinGrid.className = 'photo-picker-grid';
    builtinRow.appendChild(builtinGrid);

    function markBuiltinSelection() {
      const tiles = builtinGrid.querySelectorAll('.photo-picker-tile');
      for (let i = 0; i < tiles.length; i += 1) {
        const tile = tiles[i];
        const id = tile.dataset.builtinId || '';
        const isSel = id === selectedBuiltinId;
        tile.classList.toggle('is-selected', isSel);
        tile.setAttribute('aria-pressed', isSel ? 'true' : 'false');
      }
    }

    function selectBuiltin(id) {
      selectedBuiltinId = id || '';
      markBuiltinSelection();
      focusSaveButton();
    }

    (builtinManifest || []).forEach(function (entry, index) {
      // div+role=button: native <button> on webOS often eats Select/OK and
      // leaves focus stuck so you cannot scroll to the next settings rows.
      const tile = document.createElement('div');
      tile.className = 'photo-picker-tile focusable';
      tile.setAttribute('role', 'button');
      // 905… after Display (904), before USB filename (930).
      tile.dataset.focusIndex = String(905 + index);
      tile.dataset.builtinId = entry.id;
      tile.setAttribute('aria-label', entry.title || entry.id);
      tile.tabIndex = 0;

      const img = document.createElement('img');
      img.className = 'photo-picker-thumb';
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.draggable = false;
      img.style.pointerEvents = 'none';
      img.src = builtinImageUrl(entry.file);
      img.addEventListener('error', function () {
        if (img.dataset.fallbackTried === '1') {
          tile.classList.add('photo-picker-thumb-failed');
          return;
        }
        img.dataset.fallbackTried = '1';
        img.src = 'assets/backgrounds/' + entry.file;
      });

      const caption = document.createElement('span');
      caption.className = 'photo-picker-caption';
      caption.textContent = entry.title || entry.id;
      caption.style.pointerEvents = 'none';

      tile.appendChild(img);
      tile.appendChild(caption);
      // Do NOT select on focus — arrows only browse. Select/OK/click chooses
      // so the gold “is-selected” state stays when you scroll away.
      tile.addEventListener('click', function (event) {
        if (event && event.preventDefault) event.preventDefault();
        selectBuiltin(entry.id);
      });
      builtinGrid.appendChild(tile);
    });
    markBuiltinSelection();

    const builtinPickHint = document.createElement('p');
    builtinPickHint.className = 'settings-hint';
    builtinPickHint.textContent =
      'Packaged at ~3840px for 4K TVs. Arrows browse · Select chooses · focus jumps to Save.';
    builtinRow.appendChild(builtinPickHint);
    section.appendChild(builtinRow);

    const usbHint = document.createElement('p');
    usbHint.className = 'settings-hint';
    usbHint.textContent = 'USB path: lounge/backgrounds/ with images.json. For a single file, enter the filename below.';
    section.appendChild(usbHint);

    const usbFileInput = document.createElement('input');
    usbFileInput.type = 'text';
    usbFileInput.className = 'settings-text focusable';
    usbFileInput.dataset.focusIndex = '930';
    usbFileInput.placeholder = 'living-room.jpg';
    usbFileInput.value = config.background.file || '';
    const usbFileRow = labeledControl('USB filename', usbFileInput);
    section.appendChild(usbFileRow);

    // Match curated remote by saved id, else by URL, else default first entry.
    let initialRemote = bg.remote || '';
    if (!findRemoteBackgroundById(initialRemote)) {
      const byUrl = findRemoteBackgroundByUrl(bg.url);
      initialRemote = byUrl ? byUrl.id : (REMOTE_BACKGROUNDS[0] && REMOTE_BACKGROUNDS[0].id) || '';
    }
    // Empty string = custom typed URL (not a catalog entry).
    if (bg.url && !findRemoteBackgroundByUrl(bg.url) && !findRemoteBackgroundById(bg.remote)) {
      initialRemote = '';
    }

    const urlHint = document.createElement('p');
    urlHint.className = 'settings-hint';
    urlHint.textContent = 'Loads over the network (not stored in the app package). Choose a photo below (nature or anime), or pick Custom URL and paste any direct https image link (Unsplash, Pexels, Wallhaven, your own host). TV must be online. Slideshow with no URLs uses the curated set.';
    section.appendChild(urlHint);

    const urlInput = document.createElement('input');
    urlInput.type = 'text';
    urlInput.className = 'settings-text focusable';
    urlInput.dataset.focusIndex = '931';
    urlInput.placeholder = 'https://images.unsplash.com/photo-…';
    // Prefer saved URL; if empty and a catalog pick is selected, prefill that.
    const remotePrefill = findRemoteBackgroundById(initialRemote);
    urlInput.value = bg.url || (remotePrefill && remotePrefill.url) || '';

    // Visual photo gallery (replaces text-only stepper) — D-pad pickable tiles.
    let selectedRemoteId = initialRemote;
    const remoteRow = document.createElement('div');
    remoteRow.className = 'settings-block photo-picker-block';
    const remoteHeading = document.createElement('span');
    remoteHeading.className = 'settings-block-label';
    remoteHeading.textContent =
      'Choose an online photo (' + REMOTE_BACKGROUNDS.length + ' network images · nature + anime)';
    remoteRow.appendChild(remoteHeading);

    const remoteGrid = document.createElement('div');
    remoteGrid.className = 'photo-picker-grid';
    remoteRow.appendChild(remoteGrid);

    function markRemoteSelection() {
      const tiles = remoteGrid.querySelectorAll('.photo-picker-tile');
      for (let i = 0; i < tiles.length; i += 1) {
        const tile = tiles[i];
        const id = tile.dataset.remoteId || '';
        const isSel = id === selectedRemoteId;
        tile.classList.toggle('is-selected', isSel);
        tile.setAttribute('aria-pressed', isSel ? 'true' : 'false');
      }
    }

    function selectRemote(id) {
      selectedRemoteId = id || '';
      const picked = findRemoteBackgroundById(selectedRemoteId);
      if (picked) {
        urlInput.value = picked.url;
      }
      markRemoteSelection();
      // Custom URL tile: leave focus for pasting; catalog pick → jump to Save.
      if (id) focusSaveButton();
    }

    REMOTE_BACKGROUNDS.forEach(function (entry, index) {
      const tile = document.createElement('div');
      tile.className = 'photo-picker-tile focusable';
      tile.setAttribute('role', 'button');
      tile.dataset.focusIndex = String(905 + index);
      tile.dataset.remoteId = entry.id;
      tile.setAttribute('aria-label', entry.title || entry.id);
      tile.tabIndex = 0;

      const img = document.createElement('img');
      img.className = 'photo-picker-thumb';
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.draggable = false;
      img.style.pointerEvents = 'none';
      img.src = remoteThumbUrl(entry.url);
      img.addEventListener('error', function () {
        tile.classList.add('photo-picker-thumb-failed');
      });

      const caption = document.createElement('span');
      caption.className = 'photo-picker-caption';
      caption.textContent = entry.title || entry.id;
      caption.style.pointerEvents = 'none';

      tile.appendChild(img);
      tile.appendChild(caption);
      // Browse with arrows only; Select/click keeps is-selected when focus leaves.
      tile.addEventListener('click', function (event) {
        if (event && event.preventDefault) event.preventDefault();
        selectRemote(entry.id);
      });
      remoteGrid.appendChild(tile);
    });

    const customTile = document.createElement('div');
    customTile.className = 'photo-picker-tile photo-picker-custom focusable';
    customTile.setAttribute('role', 'button');
    customTile.dataset.focusIndex = String(905 + REMOTE_BACKGROUNDS.length);
    customTile.dataset.remoteId = '';
    customTile.setAttribute('aria-label', 'Custom URL');
    customTile.tabIndex = 0;
    const customCaption = document.createElement('span');
    customCaption.className = 'photo-picker-caption';
    customCaption.textContent = 'Custom URL';
    customCaption.style.pointerEvents = 'none';
    const customHint = document.createElement('span');
    customHint.className = 'photo-picker-custom-hint';
    customHint.textContent = 'Paste link below';
    customHint.style.pointerEvents = 'none';
    customTile.appendChild(customCaption);
    customTile.appendChild(customHint);
    customTile.addEventListener('click', function (event) {
      if (event && event.preventDefault) event.preventDefault();
      selectRemote('');
      try {
        urlInput.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      } catch (err) {
        try { urlInput.scrollIntoView(true); } catch (err2) { /* ignore */ }
      }
      window.setTimeout(function () {
        beginSettingsTextEdit(urlInput);
      }, 120);
    });
    remoteGrid.appendChild(customTile);
    markRemoteSelection();
    section.appendChild(remoteRow);

    // Keep remote id aligned when the user edits the URL by hand.
    urlInput.addEventListener('input', function () {
      const match = findRemoteBackgroundByUrl(urlInput.value.trim());
      selectedRemoteId = match ? match.id : '';
      markRemoteSelection();
    });

    const urlRow = labeledControl('Image URL', urlInput);
    section.appendChild(urlRow);

    const urlsInput = document.createElement('textarea');
    urlsInput.className = 'settings-textarea focusable';
    urlsInput.dataset.focusIndex = '932';
    urlsInput.rows = 4;
    urlsInput.placeholder = 'One https image URL per line (leave empty to use curated online set)';
    urlsInput.value = (bg.urls || []).join('\n');
    const urlsRow = labeledBlock('Slideshow URLs', urlsInput);
    section.appendChild(urlsRow);

    const intervalInput = document.createElement('input');
    intervalInput.type = 'number';
    intervalInput.min = '30';
    intervalInput.max = '3600';
    intervalInput.step = '30';
    intervalInput.className = 'settings-text focusable';
    intervalInput.dataset.focusIndex = '933';
    intervalInput.value = String(bg.slideshowIntervalSec || 300);
    const intervalRow = labeledControl('Seconds per slide', intervalInput);
    section.appendChild(intervalRow);

    const kenBurnsToggle = document.createElement('input');
    kenBurnsToggle.type = 'checkbox';
    kenBurnsToggle.checked = !!bg.kenBurns;
    kenBurnsToggle.className = 'focusable';
    kenBurnsToggle.dataset.focusIndex = '934';
    const kenBurnsRow = labeledControl('Slow zoom background', kenBurnsToggle);
    section.appendChild(kenBurnsRow);
    const kenBurnsHint = document.createElement('p');
    kenBurnsHint.className = 'settings-hint';
    kenBurnsHint.textContent =
      'When on, the wallpaper slowly zooms in and drifts a little (like a calm slideshow). When off, the picture stays still. Photos only — not gradients.';
    section.appendChild(kenBurnsHint);

    const overlayRange = document.createElement('input');
    overlayRange.type = 'range';
    overlayRange.min = '20';
    overlayRange.max = '60';
    overlayRange.value = String(Math.round((bg.overlayOpacity || 0.45) * 100));
    overlayRange.className = 'focusable';
    overlayRange.dataset.focusIndex = '935';
    section.appendChild(labeledControl('Overlay opacity', overlayRange));

    const oledNote = document.createElement('p');
    oledNote.className = 'oled-note';
    oledNote.textContent = 'On OLED TVs, prefer slideshow or gradients over a single static photo left on screen for long periods.';
    section.appendChild(oledNote);
    homePane.appendChild(section);

    const refs = {
      displayRow: displayRow,
      builtinRow: builtinRow,
      usbHint: usbHint,
      usbFileRow: usbFileRow,
      remoteRow: remoteRow,
      urlHint: urlHint,
      urlRow: urlRow,
      urlsRow: urlsRow,
      intervalRow: intervalRow,
      kenBurnsRow: kenBurnsRow,
      displaySelect: displaySelect
    };

    function syncFields(opts) {
      // Gradient style only applies to gradient sources — hide entirely for
      // photos (builtin/USB/URL) so a leftover "Warm gradient" is not confusing.
      const isGradientSource =
        sourceSelect.value === 'preset' || sourceSelect.value === 'animated-gradient';
      presetRow.hidden = !isGradientSource;
      syncBackgroundFields(sourceSelect.value, refs, opts);
    }

    syncFields();

    // Swap loading shell → full chrome now (profile + background already in body).
    // Remaining sections append to the live body so the user always sees content.
    panel.innerHTML = '';
    panel.appendChild(next);

    const music = normalizeMusicConfig(config.music);
    const builtinTracks = (await withDeadline(loadBuiltinMusicManifest(), 4000)) || [];

    if (gen !== renderGen || !visible) return;

    const musicSection = document.createElement('section');
    musicSection.className = 'settings-section';
    musicSection.innerHTML = '<h3>Music</h3>';

    const musicEnabled = document.createElement('input');
    musicEnabled.type = 'checkbox';
    musicEnabled.checked = !!effective.music.enabled;
    musicEnabled.className = 'focusable';
    musicEnabled.dataset.focusIndex = '940';
    musicSection.appendChild(labeledControl('Ambient music', musicEnabled));

    const showMusicBar = document.createElement('input');
    showMusicBar.type = 'checkbox';
    showMusicBar.checked = !!music.showBar;
    showMusicBar.className = 'focusable';
    showMusicBar.dataset.focusIndex = '9405';
    musicSection.appendChild(labeledControl('Show music bar', showMusicBar));

    const musicBarHint = document.createElement('p');
    musicBarHint.className = 'settings-hint';
    musicBarHint.textContent = 'When on, shows the track name next to the volume control. Off by default (volume only).';
    musicSection.appendChild(musicBarHint);

    const musicSourceSelect = createOptionStepper('', 941, [
      {value: 'builtin', label: 'Built-in ambient (6 tracks)'},
      {value: 'usb', label: 'My tracks (USB)'}
    ], music.source, function () {
      syncMusicFields();
    });
    const musicSourceRow = labeledControl('Source', musicSourceSelect);
    musicSection.appendChild(musicSourceRow);

    // --- Built-in: multi-select which of the 8 packaged tracks to rotate ---
    const savedBuiltinPlaylist = Array.isArray(music.builtinPlaylist)
      ? music.builtinPlaylist.slice()
      : [];
    // Empty playlist in config means “all packaged tracks”.
    const builtinSelected = {};
    builtinTracks.forEach(function (entry) {
      builtinSelected[entry.id] = !savedBuiltinPlaylist.length
        || savedBuiltinPlaylist.indexOf(entry.id) >= 0;
    });

    const builtinPlaylistBlock = document.createElement('div');
    builtinPlaylistBlock.className = 'settings-block';
    const builtinPlaylistLabel = document.createElement('span');
    builtinPlaylistLabel.className = 'settings-block-label';
    builtinPlaylistLabel.textContent = 'Built-in tracks (tick to include)';
    builtinPlaylistBlock.appendChild(builtinPlaylistLabel);

    const builtinPlaylistList = document.createElement('div');
    builtinPlaylistList.className = 'settings-track-list';
    builtinPlaylistBlock.appendChild(builtinPlaylistList);

    builtinTracks.forEach(function (entry, index) {
      const row = document.createElement('div');
      row.className = 'settings-track-row';

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'focusable';
      checkbox.dataset.focusIndex = String(942 + index);
      checkbox.checked = !!builtinSelected[entry.id];
      checkbox.addEventListener('change', function () {
        builtinSelected[entry.id] = checkbox.checked;
        // Keep “Start with” options in sync with enabled set.
        refreshBuiltinStartOptions();
      });

      const title = document.createElement('span');
      title.className = 'settings-track-title';
      const detail = entry.description ? ' — ' + entry.description : '';
      title.textContent = (entry.title || entry.id) + detail;

      row.appendChild(checkbox);
      row.appendChild(title);
      builtinPlaylistList.appendChild(row);
    });
    musicSection.appendChild(builtinPlaylistBlock);

    function enabledBuiltinIds() {
      return builtinTracks
        .map(function (entry) { return entry.id; })
        .filter(function (id) { return builtinSelected[id]; });
    }

    function builtinStartOptions() {
      const enabled = enabledBuiltinIds();
      const source = enabled.length
        ? builtinTracks.filter(function (e) { return enabled.indexOf(e.id) >= 0; })
        : builtinTracks;
      return source.map(function (entry) {
        return {value: entry.id, label: entry.title || entry.id};
      });
    }

    const builtinTrackSelect = createOptionStepper('', 950,
      builtinStartOptions(),
      music.builtin);
    const builtinTrackRow = labeledControl('Start with', builtinTrackSelect);
    musicSection.appendChild(builtinTrackRow);

    function refreshBuiltinStartOptions() {
      const opts = builtinStartOptions();
      let cur = builtinTrackSelect.value;
      const still = opts.some(function (o) { return o.value === cur; });
      if (!still && opts.length) cur = opts[0].value;
      builtinTrackSelect.setOptions(opts, cur || '');
    }

    // --- USB: folder + multi-select discovered tracks ---
    const musicFolderPicker = createOptionStepper('', 951, [
      {value: '', label: 'Custom path (type below)…'}
    ], '', function (value) {
      if (value) {
        musicPathInput.value = value;
        scanUsbTracks(value);
      }
    });
    const musicFolderRow = labeledControl('Browse folders', musicFolderPicker);
    musicSection.appendChild(musicFolderRow);

    findLoungeRoots().then(function (roots) {
      const opts = [{value: '', label: 'Custom path (type below)…'}];
      roots.forEach(function (root) {
        opts.push({value: joinPath(root, 'music'), label: joinPath(root, 'music')});
        opts.push({value: joinPath(root, 'music', 'ambient'), label: joinPath(root, 'music', 'ambient')});
        opts.push({value: joinPath(root, 'music', 'jazz'), label: joinPath(root, 'music', 'jazz')});
      });

      if (opts.length === 1) {
        opts[0].label = 'No USB drives detected — type a path below';
      }
      musicFolderPicker.setOptions(opts, config.music.path || '');
    }).catch(function () {
      musicFolderPicker.setOptions(
        [{value: '', label: 'Could not scan USB drives — type a path below'}], '');
    });

    const musicPathInput = document.createElement('input');
    musicPathInput.type = 'text';
    musicPathInput.className = 'settings-text focusable';
    musicPathInput.dataset.focusIndex = '952';
    musicPathInput.placeholder = 'e.g. /media/usb1/lounge/music';
    musicPathInput.value = config.music.path || '';
    const musicPathRow = labeledControl('Music folder', musicPathInput);
    musicSection.appendChild(musicPathRow);

    musicPathInput.addEventListener('change', function () {
      musicFolderPicker.setValue(musicPathInput.value);
      scanUsbTracks(musicPathInput.value.trim());
    });
    musicPathInput.addEventListener('blur', function () {
      scanUsbTracks(musicPathInput.value.trim());
    });

    const usbPlaylistBlock = document.createElement('div');
    usbPlaylistBlock.className = 'settings-block';
    const usbPlaylistLabel = document.createElement('span');
    usbPlaylistLabel.className = 'settings-block-label';
    usbPlaylistLabel.textContent = 'USB tracks (tick to include)';
    usbPlaylistBlock.appendChild(usbPlaylistLabel);

    const usbScanStatus = document.createElement('p');
    usbScanStatus.className = 'settings-hint';
    usbScanStatus.textContent = 'Set a folder with playlist.m3u or tracks.json, then scan.';
    usbPlaylistBlock.appendChild(usbScanStatus);

    const usbPlaylistList = document.createElement('div');
    usbPlaylistList.className = 'settings-track-list';
    usbPlaylistBlock.appendChild(usbPlaylistList);
    musicSection.appendChild(usbPlaylistBlock);

    // url -> checked
    const usbSelected = {};
    const savedUsb = Array.isArray(music.usbPlaylist) ? music.usbPlaylist : [];
    let usbTrackEntries = [];

    function renderUsbTrackList(entries) {
      usbTrackEntries = entries || [];
      usbPlaylistList.innerHTML = '';
      if (!usbTrackEntries.length) {
        usbScanStatus.textContent = musicPathInput.value.trim()
          ? 'No tracks found. Add playlist.m3u or tracks.json in that folder.'
          : 'Set a USB music folder to load your tracks.';
        return;
      }
      usbScanStatus.textContent = usbTrackEntries.length + ' track(s) found — tick the ones to play.';
      const hadSaved = savedUsb.length > 0;
      usbTrackEntries.forEach(function (entry, index) {
        const url = entry.url || '';
        if (usbSelected[url] === undefined) {
          usbSelected[url] = !hadSaved || savedUsb.indexOf(url) >= 0;
        }
        const row = document.createElement('div');
        row.className = 'settings-track-row';

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'focusable';
        checkbox.dataset.focusIndex = String(960 + index);
        checkbox.checked = !!usbSelected[url];
        checkbox.addEventListener('change', function () {
          usbSelected[url] = checkbox.checked;
        });

        const title = document.createElement('span');
        title.className = 'settings-track-title';
        if (entry.artist && entry.title) {
          title.textContent = entry.artist + ' — ' + entry.title;
        } else if (entry.title) {
          title.textContent = entry.title;
        } else {
          const parts = String(url).split('/');
          title.textContent = (parts[parts.length - 1] || url).replace(/\.[^.]+$/, '');
        }

        row.appendChild(checkbox);
        row.appendChild(title);
        usbPlaylistList.appendChild(row);
      });
      if (options.onRendered) options.onRendered();
    }

    let usbScanGen = 0;
    function scanUsbTracks(folder) {
      const gen = (usbScanGen += 1);
      if (!folder) {
        renderUsbTrackList([]);
        return;
      }
      usbScanStatus.textContent = 'Scanning USB music…';
      withDeadline(discoverMusicTracks(folder), 5000).then(function (list) {
        if (gen !== usbScanGen || !visible) return;
        renderUsbTrackList(list || []);
      });
    }

    if (music.path) {
      scanUsbTracks(music.path);
    }

    const shuffleToggle = document.createElement('input');
    shuffleToggle.type = 'checkbox';
    shuffleToggle.checked = config.music.shuffle !== false;
    shuffleToggle.className = 'focusable';
    shuffleToggle.dataset.focusIndex = '990';
    const shuffleRow = labeledControl('Shuffle', shuffleToggle);
    musicSection.appendChild(shuffleRow);

    const repeatSelect = createOptionStepper('', 991, [
      {value: 'all', label: 'Repeat all'},
      {value: 'one', label: 'Repeat one'},
      {value: 'off', label: 'Play once'}
    ], config.music.repeat || 'one');
    const repeatRow = labeledControl('Repeat', repeatSelect);
    musicSection.appendChild(repeatRow);

    const musicVolume = document.createElement('input');
    musicVolume.type = 'range';
    musicVolume.min = '0';
    musicVolume.max = '50';
    musicVolume.value = String(Math.round((effective.music.volume || 0.15) * 100));
    musicVolume.className = 'focusable';
    musicVolume.dataset.focusIndex = '992';
    musicSection.appendChild(labeledControl('Ambient volume', musicVolume));

    const musicHint = document.createElement('p');
    musicHint.className = 'settings-hint';
    musicHint.textContent = 'Six built-in tracks ship in the app (48 kbps ambient, CC0 / CC BY). If music is silent at launch, press any remote key once (autoplay unlock). Red = pause, Green = skip. USB: Source → My tracks for more.';
    musicSection.appendChild(musicHint);

    function syncMusicFields() {
      const isBuiltin = musicSourceSelect.value === 'builtin';
      builtinPlaylistBlock.hidden = !isBuiltin;
      builtinTrackRow.hidden = !isBuiltin;
      musicFolderRow.hidden = isBuiltin;
      musicPathRow.hidden = isBuiltin;
      usbPlaylistBlock.hidden = isBuiltin;
      // Shuffle/repeat apply to multi-track playlists (built-in or USB).
      shuffleRow.hidden = false;
      repeatRow.hidden = false;
    }

    syncMusicFields();
    homePane.appendChild(musicSection);

    const launcherSection = document.createElement('section');
    launcherSection.className = 'settings-section';
    launcherSection.innerHTML = '<h3>Launcher</h3>';

    // TV system volume levels (0–100), separate from ambient music slider.
    function volumeLevelOptions(selected) {
      const opts = [];
      for (let v = 0; v <= 30; v += 1) {
        opts.push({value: String(v), label: String(v)});
      }
      // Also offer a few higher steps for loud setups.
      [35, 40, 45, 50, 60, 70, 80, 90, 100].forEach(function (v) {
        opts.push({value: String(v), label: String(v)});
      });
      const sel = String(selected);
      if (!opts.some(function (o) { return o.value === sel; })) {
        opts.unshift({value: sel, label: sel});
      }
      return opts;
    }

    const volumeAtHomeSelect = createOptionStepper(
      '',
      993,
      volumeLevelOptions(
        typeof config.launcher.volumeAtHome === 'number' ? config.launcher.volumeAtHome : 6
      ),
      String(typeof config.launcher.volumeAtHome === 'number' ? config.launcher.volumeAtHome : 6)
    );
    launcherSection.appendChild(labeledControl('Volume in Launch Home', volumeAtHomeSelect));

    const volumeOnAppSelect = createOptionStepper(
      '',
      994,
      volumeLevelOptions(
        typeof config.launcher.volumeOnAppLaunch === 'number' ? config.launcher.volumeOnAppLaunch : 13
      ),
      String(typeof config.launcher.volumeOnAppLaunch === 'number' ? config.launcher.volumeOnAppLaunch : 13)
    );
    launcherSection.appendChild(labeledControl('Volume when apps launch', volumeOnAppSelect));

    const volumeLevelsHint = document.createElement('p');
    volumeLevelsHint.className = 'settings-hint';
    volumeLevelsHint.textContent = 'TV system volume (0–100). Launch Home uses the first level; Netflix / HDMI / other apps use the second.';
    launcherSection.appendChild(volumeLevelsHint);

    // ── Launch Home (in-app) screensaver ──────────────────────────────
    const customSsToggle = document.createElement('input');
    customSsToggle.type = 'checkbox';
    customSsToggle.checked = config.launcher.customScreensaver !== false;
    customSsToggle.className = 'focusable';
    customSsToggle.dataset.focusIndex = '990';
    launcherSection.appendChild(labeledControl('Launch Home screensaver', customSsToggle));

    const customSsIdle = createOptionStepper('', 991, [
      {value: '2', label: '2 minutes'},
      {value: '5', label: '5 minutes'},
      {value: '10', label: '10 minutes'},
      {value: '15', label: '15 minutes'},
      {value: '20', label: '20 minutes'},
      {value: '30', label: '30 minutes'},
      {value: '45', label: '45 minutes'},
      {value: '60', label: '60 minutes'}
    ], String(
      typeof config.launcher.customScreensaverMinutes === 'number'
        ? config.launcher.customScreensaverMinutes
        : 5
    ));
    launcherSection.appendChild(labeledControl('Start after (idle)', customSsIdle));

    const customSsSlide = createOptionStepper('', 992, [
      {value: '12', label: '12 seconds'},
      {value: '20', label: '20 seconds'},
      {value: '30', label: '30 seconds'},
      {value: '45', label: '45 seconds'},
      {value: '60', label: '1 minute'},
      {value: '120', label: '2 minutes'}
    ], String(
      typeof config.launcher.customScreensaverSlideSec === 'number'
        ? config.launcher.customScreensaverSlideSec
        : 20
    ));
    launcherSection.appendChild(labeledControl('Photo change every', customSsSlide));

    const customSsClock = document.createElement('input');
    customSsClock.type = 'checkbox';
    customSsClock.checked = config.launcher.customScreensaverShowClock !== false;
    customSsClock.className = 'focusable';
    customSsClock.dataset.focusIndex = '993';
    launcherSection.appendChild(labeledControl('Screensaver clock', customSsClock));

    const customSsDate = document.createElement('input');
    customSsDate.type = 'checkbox';
    customSsDate.checked = config.launcher.customScreensaverShowDate !== false;
    customSsDate.className = 'focusable';
    customSsDate.dataset.focusIndex = '994';
    launcherSection.appendChild(labeledControl('Screensaver date', customSsDate));

    const customSsHint = document.createElement('p');
    customSsHint.className = 'settings-hint';
    customSsHint.textContent =
      'Full-screen photo slideshow from your Launch Home wallpapers while this app is open. Any remote button wakes. Uses Ken Burns motion for OLED care.';
    launcherSection.appendChild(customSsHint);

    const previewSsBtn = document.createElement('button');
    previewSsBtn.type = 'button';
    previewSsBtn.className = 'settings-preview-ss-btn focusable';
    previewSsBtn.dataset.focusIndex = '996';
    previewSsBtn.textContent = 'Preview screensaver now';
    previewSsBtn.addEventListener('click', function () {
      hide();
      if (options.onPreviewScreensaver) {
        setTimeout(function () { options.onPreviewScreensaver(); }, 350);
      } else if (options.onToast) {
        options.onToast('Save & reopen Launch Home to preview');
      }
    });
    launcherSection.appendChild(previewSsBtn);

    // System LG gallery saver (rooted write). Pushed to 30 min when custom is on.
    const ssMins = coerceScreensaverMinutes(
      typeof config.launcher.screensaverMinutes === 'number'
        ? config.launcher.screensaverMinutes
        : 30
    );
    const screensaverSelect = createOptionStepper('', 995, [
      {value: '0', label: 'Off (never)'},
      {value: '3', label: '3 minutes'},
      {value: '10', label: '10 minutes'},
      {value: '20', label: '20 minutes'},
      {value: '30', label: '30 minutes'}
    ], String(ssMins));
    launcherSection.appendChild(labeledControl('TV system screensaver', screensaverSelect));

    const screensaverHint = document.createElement('p');
    screensaverHint.className = 'settings-hint';
    screensaverHint.textContent =
      'LG system gallery timeout (3/10/20/30 min only). When Launch Home screensaver is on, leave this at 30 so the TV does not interrupt first. Requires rooted TV.';
    launcherSection.appendChild(screensaverHint);

    const showClockToggle = document.createElement('input');
    showClockToggle.type = 'checkbox';
    showClockToggle.checked = config.launcher.showClock !== false;
    showClockToggle.className = 'focusable';
    showClockToggle.dataset.focusIndex = '1000';
    launcherSection.appendChild(labeledControl('Show clock', showClockToggle));

    const showDateToggle = document.createElement('input');
    showDateToggle.type = 'checkbox';
    showDateToggle.checked = config.launcher.showDate !== false;
    showDateToggle.className = 'focusable';
    showDateToggle.dataset.focusIndex = '1001';
    launcherSection.appendChild(labeledControl('Show date', showDateToggle));

    const clockAlignSelect = createOptionStepper('', 1002, [
      {value: 'left', label: 'Top left'},
      {value: 'center', label: 'Centre top'},
      {value: 'center-middle', label: 'Centre middle'},
      {value: 'right', label: 'Top right'}
    ], config.launcher.clockAlign || 'center');
    launcherSection.appendChild(labeledControl('Clock position', clockAlignSelect));

    const clockSizeSelect = createOptionStepper('', 1003, [
      {value: 'small', label: 'Small'},
      {value: 'medium', label: 'Medium'},
      {value: 'large', label: 'Large'},
      {value: 'x-large', label: 'X-Large'},
      {value: 'xx-large', label: 'XX-Large'}
    ], config.launcher.clockSize || 'large');
    launcherSection.appendChild(labeledControl('Clock size', clockSizeSelect));

    const timezoneSelect = createOptionStepper('', 1004,
      TIMEZONE_OPTIONS.map(function (option) {
        return {value: option.value, label: option.label};
      }),
      config.launcher.timezone || '');
    launcherSection.appendChild(labeledControl('Timezone', timezoneSelect));

    const iconSizeSelect = createOptionStepper('', 1005, [
      {value: 'small', label: 'Small'},
      {value: 'medium', label: 'Medium'},
      {value: 'large', label: 'Large'}
    ], config.launcher.iconSize || 'medium');
    launcherSection.appendChild(labeledControl('Icon size', iconSizeSelect));

    const iconAlignSelect = createOptionStepper('', 1006, [
      {value: 'left', label: 'Left'},
      {value: 'center', label: 'Centre'},
      {value: 'right', label: 'Right'}
    ], config.launcher.iconAlign || 'center');
    launcherSection.appendChild(labeledControl('Icon alignment', iconAlignSelect));

    const iconLayoutSelect = createOptionStepper('', 1007, [
      {value: 'scroll', label: 'Scroll one row'},
      {value: 'wrap', label: 'Stacked rows'}
    ], config.launcher.iconLayout || 'scroll');
    launcherSection.appendChild(labeledControl('Icon layout', iconLayoutSelect));

    const perRowValue = config.launcher.iconsPerRow || 7;
    const iconsPerRowSelect = createOptionStepper('', 1008, [
      {value: '4', label: '4'}, {value: '5', label: '5'}, {value: '6', label: '6'},
      {value: '7', label: '7'}, {value: '8', label: '8'}, {value: '9', label: '9'},
      {value: '10', label: '10'}, {value: '11', label: '11'}, {value: '12', label: '12'}
    ], String(perRowValue));
    launcherSection.appendChild(labeledControl('Icons per row (scroll mode)', iconsPerRowSelect));

    const perfModeToggle = document.createElement('input');
    perfModeToggle.type = 'checkbox';
    perfModeToggle.checked = !!config.launcher.perfMode;
    perfModeToggle.className = 'focusable';
    perfModeToggle.dataset.focusIndex = '1009';
    launcherSection.appendChild(labeledControl('Performance mode (low-spec TVs)', perfModeToggle));

    // Off by default: when enabled, stock Home coming to the foreground
    // (Home button press after another app, or an app exiting to home)
    // relaunches Launch Home.
    const launchOnHomeToggle = document.createElement('input');
    launchOnHomeToggle.type = 'checkbox';
    launchOnHomeToggle.checked = !!(config.launcher.launchOnHome || config.launcher.returnOnAppExit);
    launchOnHomeToggle.className = 'focusable';
    launchOnHomeToggle.dataset.focusIndex = '1010';
    launcherSection.appendChild(labeledControl('Launch on Home button', launchOnHomeToggle));

    const launchOnHomeHint = document.createElement('p');
    launchOnHomeHint.className = 'settings-hint';
    launchOnHomeHint.textContent = 'When enabled, a root service opens Launch Home whenever stock Home appears. A brief flash of the LG home screen is normal (the TV opens Home first; we cannot block the key without a separate input-hook app). Requires rooted TV + Homebrew Channel. Toggle off/on and Save after updates if it stops working.';
    launcherSection.appendChild(launchOnHomeHint);

    const bootToggle = document.createElement('input');
    bootToggle.type = 'checkbox';
    bootToggle.checked = !!config.launcher.bootOnStart;
    bootToggle.className = 'focusable';
    bootToggle.dataset.focusIndex = '1011';
    launcherSection.appendChild(labeledControl('Boot on TV start', bootToggle));

    const bootHint = document.createElement('p');
    bootHint.className = 'settings-hint';
    bootHint.textContent = 'When enabled, a root init.d script launches Launch Home after the TV powers on. Requires rooted TV + Homebrew Channel (same as Home button). A short delay on boot is normal while webOS starts.';
    launcherSection.appendChild(bootHint);
    homePane.appendChild(launcherSection);

    const inputsSection = document.createElement('section');
    inputsSection.className = 'settings-section';
    inputsSection.innerHTML = '<h3>Inputs</h3><p class="settings-hint">Choose which inputs appear and set custom labels. Uncheck all to hide the input row entirely.</p>';

    const inputsList = document.createElement('div');
    inputsList.className = 'settings-inputs';
    inputsSection.appendChild(inputsList);
    homePane.appendChild(inputsSection);

    const appsSection = document.createElement('section');
    appsSection.className = 'settings-section';
    appsSection.innerHTML = '<h3>Pinned apps</h3><p class="settings-hint">Reorder or remove apps pinned to the home row.</p>';

    const pinnedList = document.createElement('div');
    pinnedList.className = 'settings-pinned-list';
    appsSection.appendChild(pinnedList);

    const addHeading = document.createElement('h4');
    addHeading.className = 'settings-subheading';
    addHeading.textContent = 'Add an app';
    appsSection.appendChild(addHeading);

    const addHint = document.createElement('p');
    addHint.className = 'settings-hint';
    addHint.textContent = 'Tap + next to any installed app below to pin it to the home row.';
    appsSection.appendChild(addHint);

    const addAppsList = document.createElement('div');
    addAppsList.className = 'settings-apps';
    appsSection.appendChild(addAppsList);
    homePane.appendChild(appsSection);

    const customSection = document.createElement('section');
    customSection.className = 'settings-section';
    customSection.innerHTML = '<h3>Custom app</h3><p class="settings-hint">Pin any installed app by its App ID and choose a bundled icon. Find the App ID on your TV or in the Homebrew app list.</p>';

    const customAppIdInput = document.createElement('input');
    customAppIdInput.type = 'text';
    customAppIdInput.className = 'settings-text focusable';
    customAppIdInput.dataset.focusIndex = '1400';
    customAppIdInput.placeholder = 'e.g. com.spotify.tv';
    customSection.appendChild(labeledControl('App ID', customAppIdInput));

    const customNameInput = document.createElement('input');
    customNameInput.type = 'text';
    customNameInput.className = 'settings-text focusable';
    customNameInput.dataset.focusIndex = '1401';
    customNameInput.placeholder = 'e.g. Spotify';
    customSection.appendChild(labeledControl('Name', customNameInput));

    const iconPreview = document.createElement('img');
    iconPreview.className = 'settings-app-icon';
    iconPreview.alt = '';
    iconPreview.src = BUILTIN_ICON_CHOICES[0].value;

    const iconSelect = createOptionStepper('', 1402, BUILTIN_ICON_CHOICES, BUILTIN_ICON_CHOICES[0].value, function (value) {
      iconPreview.src = value;
    });

    const iconRow = labeledControl('Icon', iconSelect);
    iconRow.insertBefore(iconPreview, iconRow.lastChild);
    customSection.appendChild(iconRow);

    const addCustomBtn = document.createElement('button');
    addCustomBtn.type = 'button';
    addCustomBtn.className = 'settings-mini-btn settings-add-custom focusable';
    addCustomBtn.dataset.focusIndex = '1403';
    addCustomBtn.textContent = 'Add custom app';
    addCustomBtn.addEventListener('click', function () {
      const launchId = customAppIdInput.value.trim();
      if (!launchId) {
        if (options.onToast) options.onToast('Enter an App ID first');
        return;
      }

      const id = 'custom:' + Date.now() + '-' + Math.floor(Math.random() * 1000);
      const title = customNameInput.value.trim() || getBuiltinAppTitle(launchId) || launchId;

      customApps.push({id: id, launchId: launchId, title: title, icon: iconSelect.value});
      pinnedOrder.push(id);

      customAppIdInput.value = '';
      customNameInput.value = '';

      loadAppsLists(pinnedList, addAppsList, config);
    });
    customSection.appendChild(addCustomBtn);
    homePane.appendChild(customSection);

    const discoverSection = document.createElement('section');
    discoverSection.className = 'settings-section';
    discoverSection.innerHTML = '<h3>Discover apps</h3><p class="settings-hint">Scan this TV for installed apps, then add any of them to the dock. Discovered apps keep their own icon unless you pick a bundled one below.</p>';

    const scanBtn = document.createElement('button');
    scanBtn.type = 'button';
    scanBtn.className = 'settings-mini-btn settings-scan-btn focusable';
    scanBtn.dataset.focusIndex = '1500';
    scanBtn.textContent = 'Scan for apps';
    discoverSection.appendChild(scanBtn);

    const discoverIconSelect = createOptionStepper(
      '',
      1501,
      [{value: '', label: 'Use native icon'}].concat(BUILTIN_ICON_CHOICES),
      '',
      null
    );
    discoverSection.appendChild(labeledControl('Icon override', discoverIconSelect));

    const discoverStatus = document.createElement('p');
    discoverStatus.className = 'settings-hint';
    discoverSection.appendChild(discoverStatus);

    const discoverList = document.createElement('div');
    discoverList.className = 'settings-apps';
    discoverSection.appendChild(discoverList);
    homePane.appendChild(discoverSection);

    let discovered = [];

    function inDock(appId) {
      if (pinnedOrder.indexOf(appId) >= 0) return true;
      for (let i = 0; i < customApps.length; i += 1) {
        if (customApps[i].launchId === appId) return true;
      }
      return false;
    }

    function renderDiscoverList() {
      discoverList.innerHTML = '';
      if (!discovered.length) return;

      const available = discovered.filter(function (app) {
        return app && app.id && !inDock(app.id);
      });

      discoverStatus.textContent = available.length
        ? 'Found ' + discovered.length + ' apps.'
        : 'All discovered apps are already in your dock.';

      available.forEach(function (app, index) {
        const row = document.createElement('div');
        row.className = 'settings-app-row';

        if (app.icon) {
          const icon = document.createElement('img');
          icon.className = 'settings-app-icon';
          icon.alt = '';
          icon.addEventListener('error', function () { icon.remove(); });
          lazyLoadIcon(icon, app.icon, discoverList);
          row.appendChild(icon);
        }

        const title = document.createElement('span');
        title.textContent = app.title || app.id;

        const addBtn = document.createElement('button');
        addBtn.type = 'button';
        addBtn.className = 'settings-mini-btn focusable';
        addBtn.dataset.focusIndex = String(1510 + index);
        addBtn.dataset.pointerFocus = 'off';
        addBtn.textContent = '+';
        addBtn.addEventListener('click', function () {
          const override = discoverIconSelect.value;
          if (override) {
            const id = 'custom:' + Date.now() + '-' + Math.floor(Math.random() * 1000);
            customApps.push({id: id, launchId: app.id, title: app.title || app.id, icon: override});
            pinnedOrder.push(id);
          } else {
            pinnedOrder.push(app.id);
          }
          loadAppsLists(pinnedList, addAppsList, config);
          renderDiscoverList();
        });

        row.appendChild(title);
        row.appendChild(addBtn);
        discoverList.appendChild(row);
      });
    }

    let scanning = false;
    scanBtn.addEventListener('click', async function () {
      if (scanning) return;
      scanning = true;
      scanBtn.disabled = true;
      discoverStatus.textContent = 'Scanning\u2026';
      try {
        discovered = await listInstalledApps({includeHidden: true});
        discovered.sort(function (a, b) {
          return (a.title || a.id).localeCompare(b.title || b.id);
        });
        if (!discovered.length) {
          discoverStatus.textContent = 'No apps found on this TV.';
        } else {
          renderDiscoverList();
        }
      } catch (err) {
        discoverStatus.textContent = 'Could not scan for apps.';
      } finally {
        scanning = false;
        scanBtn.disabled = false;
      }
    });

    saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'settings-save focusable';
    saveBtn.dataset.focusIndex = '899';
    saveBtn.textContent = 'Save';
    saveBtn.addEventListener('click', function () {
      // AI tab: persist VoxRelay config only.
      if (activeSettingsTab === 'ai') {
        const payload = {
          stt_language: aiSttSelect.value || 'en',
          chat_model: aiChatSelect.value || 'grok-4.6',
          voice_model: aiVoiceModelSelect.value || 'grok-voice-think-fast-2.0',
          overlay_auto_dismiss_sec: parseInt(aiDismissSelect.value, 10) || 8,
          auth_mode: (aiAuthModeSelect && aiAuthModeSelect.value) || 'API_KEY'
        };
        const key = (aiApiKeyInput.value || '').trim();
        if (key) {
          if (key.indexOf('xai-') !== 0 && key.indexOf('eyJ') !== 0) {
            if (options.onToast) {
              options.onToast('Grok key should start with xai- (or paste a SuperGrok token)');
            }
            return;
          }
          payload.xai_api_key = key;
        }
        const gemKey = (aiGeminiKeyInput.value || '').trim();
        if (gemKey) {
          payload.gemini_api_key = gemKey;
        }
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving…';
        setVoxrelayConfig(payload).then(function (res) {
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
          if (res && res.restarting) {
            if (options.onToast) options.onToast('AI settings saved — restarting voice…');
          } else if (options.onToast) {
            options.onToast('AI settings saved');
          }
          aiApiKeyInput.value = '';
          aiGeminiKeyInput.value = '';
          loadAiTab();
        }).catch(function (err) {
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
          if (options.onToast) {
            options.onToast((err && err.message) || 'Could not save AI settings');
          }
        });
        return;
      }

      config.profile = profileSelect.value;
      config.background.source = sourceSelect.value;
      config.background.mode = displaySelect.value;
      config.background.preset = presetSelect.value;
      config.background.builtin = selectedBuiltinId || '';
      config.background.file = usbFileInput.value.trim();
      // Online catalog is independent of built-in; only apply when source is url.
      config.background.remote = selectedRemoteId || '';
      config.background.url = urlInput.value.trim();
      // Keep remote id aligned when the URL still matches a catalog entry.
      if (config.background.url) {
        const match = findRemoteBackgroundByUrl(config.background.url);
        if (match) config.background.remote = match.id;
      } else if (selectedRemoteId) {
        const picked = findRemoteBackgroundById(selectedRemoteId);
        if (picked) {
          config.background.url = picked.url;
          config.background.remote = picked.id;
        }
      }
      config.background.urls = parseUrlList(urlsInput.value);
      config.background.slideshowIntervalSec = Number(intervalInput.value) || 300;
      config.background.kenBurns = kenBurnsToggle.checked;
      config.background.overlayOpacity = Number(overlayRange.value) / 100;

      config.music.enabled = musicEnabled.checked;
      config.music.showBar = showMusicBar.checked;
      config.music.source = musicSourceSelect.value;
      config.music.builtin = builtinTrackSelect.value;
      // Empty array = all built-ins; otherwise only ticked ids.
      let pickedBuiltin = enabledBuiltinIds();
      if (!pickedBuiltin.length && builtinTracks.length) {
        // Don't allow empty playlist — keep the start track (or first).
        pickedBuiltin = [builtinTrackSelect.value || builtinTracks[0].id];
      }
      config.music.builtinPlaylist =
        pickedBuiltin.length === builtinTracks.length ? [] : pickedBuiltin;
      config.music.path = musicPathInput.value.trim();
      // Empty = all USB tracks found; otherwise only ticked urls.
      let pickedUsb = [];
      usbTrackEntries.forEach(function (entry) {
        const url = entry && entry.url;
        if (url && usbSelected[url]) pickedUsb.push(url);
      });
      if (!pickedUsb.length && usbTrackEntries.length) {
        pickedUsb = [usbTrackEntries[0].url];
      }
      config.music.usbPlaylist =
        usbTrackEntries.length && pickedUsb.length === usbTrackEntries.length
          ? []
          : pickedUsb;
      config.music.shuffle = shuffleToggle.checked;
      config.music.repeat = repeatSelect.value;
      config.music.volume = Number(musicVolume.value) / 100;

      config.launcher.volumeAtHome = parseInt(volumeAtHomeSelect.value, 10);
      if (isNaN(config.launcher.volumeAtHome)) config.launcher.volumeAtHome = 6;
      config.launcher.volumeOnAppLaunch = parseInt(volumeOnAppSelect.value, 10);
      if (isNaN(config.launcher.volumeOnAppLaunch)) config.launcher.volumeOnAppLaunch = 13;
      config.launcher.customScreensaver = customSsToggle.checked;
      config.launcher.customScreensaverMinutes = parseInt(customSsIdle.value, 10);
      if (isNaN(config.launcher.customScreensaverMinutes) ||
          config.launcher.customScreensaverMinutes < 1) {
        config.launcher.customScreensaverMinutes = 5;
      }
      config.launcher.customScreensaverSlideSec = parseInt(customSsSlide.value, 10);
      if (isNaN(config.launcher.customScreensaverSlideSec) ||
          config.launcher.customScreensaverSlideSec < 8) {
        config.launcher.customScreensaverSlideSec = 20;
      }
      config.launcher.customScreensaverShowClock = customSsClock.checked;
      config.launcher.customScreensaverShowDate = customSsDate.checked;
      config.launcher.screensaverMinutes = coerceScreensaverMinutes(
        parseInt(screensaverSelect.value, 10)
      );
      // Keep system saver from firing before the in-app one.
      if (config.launcher.customScreensaver &&
          config.launcher.screensaverMinutes > 0 &&
          config.launcher.screensaverMinutes < 30) {
        config.launcher.screensaverMinutes = 30;
      }
      config.launcher.showClock = showClockToggle.checked;
      config.launcher.showDate = showDateToggle.checked;
      config.launcher.clockAlign = clockAlignSelect.value || 'center';
      // Normalise unknown legacy values to centre top.
      if (['left', 'center', 'center-middle', 'right'].indexOf(config.launcher.clockAlign) < 0) {
        config.launcher.clockAlign = 'center';
      }
      config.launcher.clockSize = clockSizeSelect.value || 'large';
      config.launcher.timezone = timezoneSelect.value;
      config.launcher.iconSize = iconSizeSelect.value;
      config.launcher.iconAlign = iconAlignSelect.value;
      config.launcher.iconLayout = iconLayoutSelect.value;
      config.launcher.iconsPerRow = parseInt(iconsPerRowSelect.value, 10) || 7;
      config.launcher.perfMode = perfModeToggle.checked;
      config.launcher.launchOnHome = launchOnHomeToggle.checked;
      // Keep legacy key in sync so older builds / USB config still work.
      config.launcher.returnOnAppExit = launchOnHomeToggle.checked;
      config.launcher.bootOnStart = bootToggle.checked;
      config.launcher.pinnedApps = pinnedOrder.slice();
      config.launcher.customApps = customApps.slice();

      saveInputSettings(inputsList, config);

      saveConfig(config);
      if (options.onSave) options.onSave(config);
      if (config.launcher.launchOnHome && options.onToast) {
        options.onToast('Enabling Home → Launch Home watcher…');
      } else if (config.launcher.bootOnStart && options.onToast) {
        options.onToast('Enabling boot on TV start…');
      }
      hide();
    });
    headerActions.insertBefore(saveBtn, closeBtn);

    await withDeadline(loadInputSettings(inputsList, config), 6000);
    if (gen !== renderGen || !visible) return;
    await withDeadline(loadAppsLists(pinnedList, addAppsList, config), 8000);
    if (gen !== renderGen || !visible) return;
    attachInputScrollHelpers(body);
    // Load AI tab in background so Home settings stay snappy.
    loadAiTab();
  }

  async function loadInputSettings(container, config) {
    const devices = await fetchInputDevices();
    const allowed = config.launcher.inputs || DEFAULT_INPUTS.slice();
    const labels = config.launcher.inputLabels || {};

    devices.forEach(function (device, index) {
      const row = document.createElement('div');
      row.className = 'settings-input-row';

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.className = 'focusable';
      checkbox.dataset.focusIndex = String(1100 + index * 2);
      checkbox.dataset.inputId = device.id;
      checkbox.checked = allowed.indexOf(device.id) >= 0;

      const labelInput = document.createElement('input');
      labelInput.type = 'text';
      labelInput.className = 'settings-text focusable';
      labelInput.dataset.focusIndex = String(1101 + index * 2);
      labelInput.dataset.inputId = device.id;
      labelInput.placeholder = device.label || device.id.replace(/_/g, ' ');
      labelInput.value = labels[device.id] || '';

      const name = document.createElement('span');
      name.className = 'settings-input-name';
      name.textContent = device.label || device.id;

      row.appendChild(checkbox);
      row.appendChild(name);
      row.appendChild(labelInput);
      container.appendChild(row);
    });
  }

  function saveInputSettings(container, config) {
    const allowed = [];
    const labels = {};

    container.querySelectorAll('.settings-input-row').forEach(function (row) {
      const checkbox = row.querySelector('input[type=checkbox]');
      const labelInput = row.querySelector('input[type=text]');
      const inputId = checkbox.dataset.inputId;

      if (checkbox.checked) allowed.push(inputId);
      if (labelInput.value.trim()) labels[inputId] = labelInput.value.trim();
    });

    // Empty array is intentional ("show no inputs"). Do not fall back to defaults.
    config.launcher.inputs = allowed;
    config.launcher.inputLabels = labels;
  }

  async function loadAppsLists(pinnedListEl, addContainer, config) {
    addContainer.innerHTML = '';
    const catalog = await loadAppCatalog();
    const apps = await listInstalledApps();
    appsByIdMap = Object.assign({}, catalog);
    apps.forEach(function (app) {
      appsByIdMap[app.id] = app;
    });

    customApps.forEach(function (entry) {
      if (!entry || !entry.id) return;
      appsByIdMap[entry.id] = {
        id: entry.id,
        launchId: entry.launchId,
        title: entry.title || entry.launchId || entry.id,
        icon: entry.icon || ''
      };
    });

    for (let i = 0; i < pinnedOrder.length; i += 1) {
      const appId = pinnedOrder[i];
      if (findCustomApp(appId)) continue;
      if (!appsByIdMap[appId] || !appsByIdMap[appId].title || !appsByIdMap[appId].icon) {
        appsByIdMap[appId] = await resolvePinnedApp(appId, appsByIdMap);
      }
    }

    KNOWN_BUILTIN_APPS.forEach(function (id) {
      if (appsByIdMap[id] && appsByIdMap[id].title) return;
      appsByIdMap[id] = {
        id: id,
        title: getBuiltinAppTitle(id) || id,
        icon: getBuiltinAppIcon(id) || ''
      };
    });

    pinnedContainer = pinnedListEl;
    renderPinnedList(pinnedListEl);

    const seen = {};
    const candidates = [];
    apps.forEach(function (app) {
      if (app && app.id && !seen[app.id]) {
        seen[app.id] = true;
        candidates.push(app);
      }
    });
    Object.keys(appsByIdMap).forEach(function (id) {
      const app = appsByIdMap[id];
      if (app && app.id && app.title && !seen[app.id]) {
        seen[app.id] = true;
        candidates.push(app);
      }
    });

    candidates.sort(function (a, b) {
      return (a.title || a.id).localeCompare(b.title || b.id);
    });

    const customLaunchIds = {};
    customApps.forEach(function (entry) {
      if (entry && entry.launchId) customLaunchIds[entry.launchId] = true;
    });

    const remaining = candidates.filter(function (app) {
      return pinnedOrder.indexOf(app.id) < 0 && !customLaunchIds[app.id];
    });

    if (!remaining.length) {
      const empty = document.createElement('p');
      empty.className = 'settings-hint';
      empty.textContent = candidates.length
        ? 'All available apps are already pinned.'
        : 'No other apps were found on this TV.';
      addContainer.appendChild(empty);
    }

    remaining.forEach(function (app, index) {
      const row = document.createElement('div');
      row.className = 'settings-app-row';

      if (app.icon) {
        const icon = document.createElement('img');
        icon.className = 'settings-app-icon';
        icon.alt = '';
        icon.addEventListener('error', function () { icon.remove(); });
        lazyLoadIcon(icon, app.icon, addContainer);
        row.appendChild(icon);
      }

      const title = document.createElement('span');
      title.textContent = app.title || app.id;

      const addBtn = document.createElement('button');
      addBtn.type = 'button';
      addBtn.className = 'settings-mini-btn focusable';
      addBtn.dataset.focusIndex = String(1300 + index);
      addBtn.dataset.pointerFocus = 'off';
      addBtn.textContent = '+';
      addBtn.addEventListener('click', function () {
        pinnedOrder.push(app.id);
        loadAppsLists(pinnedListEl, addContainer, config);
      });

      row.appendChild(title);
      row.appendChild(addBtn);
      addContainer.appendChild(row);
    });
  }

  return {
    show: show,
    hide: hide,
    isVisible: function () { return visible; }
  };
}