import './compat.js';
import {loadConfig, saveConfig, applyUsbConfig} from './config.js';
import {applyActiveProfile} from './profiles.js';
import {resolveLoungePaths} from './usb.js';
import {createBackgroundController} from './background.js';
import {createMusicPlayer} from './music.js';
import {createAppGrid} from './apps.js';
import {createInputRow} from './inputs.js';
import {createFocusManager} from './focus.js';
import {createSettingsPanel} from './settings.js';
import {
  getForegroundApp,
  launchApp,
  launchAppViaRoot,
  listApps,
  closeApp,
  enableHomeWatcher,
  disableHomeWatcher,
  enableBootLaunch,
  disableBootLaunch,
  setSystemVolume,
  setScreensaverTimeout,
  execRoot
} from './luna.js';
import {isHomeApp} from './remote.js';
import {isTerminalAppId, getAppIdCandidates, isCompanionVoiceApp} from './app-icons.js';
import {createVoiceIndicator} from './voice-indicator.js';
import {createCustomScreensaver} from './screensaver.js';
import {addVoxrelayListener} from './voxrelay-ws.js';
import {resolveVoiceLaunch} from './voice-launch.js';

const APP_ID = 'org.webosbrew.lounge.launcher';

let baseConfig = loadConfig();
let visible = true;
let foregroundTimer = null;
let lastForegroundAppId = APP_ID;
let returningToLounge = false;
let launchPending = false;
let wentHiddenSinceLaunch = false;
let launchAt = 0;
let lastHomeLaunchAt = 0;
let ghostReasserted = false;
let lastVoiceLaunchKey = '';
let lastVoiceLaunchAt = 0;

const elements = {
  backgroundLayer: document.getElementById('background-layer'),
  scrim: document.getElementById('scrim'),
  clock: document.getElementById('clock'),
  clockDate: document.getElementById('clock-date'),
  appSettingsBtn: document.getElementById('app-settings-btn'),
  inputRow: document.getElementById('input-row'),
  launcher: document.querySelector('.launcher'),
  appGridShell: document.getElementById('app-grid-shell'),
  appGrid: document.getElementById('app-grid'),
  musicBar: document.getElementById('music-bar'),
  trackTitle: document.getElementById('track-title'),
  muteBtn: document.getElementById('mute-btn'),
  volumeSlider: document.getElementById('volume-slider'),
  audio: document.getElementById('ambient-audio'),
  settingsPanel: document.getElementById('settings-panel'),
  toast: document.getElementById('toast'),
  voiceIndicator: document.getElementById('voice-indicator'),
  customScreensaver: document.getElementById('custom-screensaver')
};

const voiceIndicator = createVoiceIndicator(elements.voiceIndicator);

function getBaseConfig() {
  return baseConfig;
}

function getConfig() {
  return applyActiveProfile(baseConfig);
}

function setConfig(nextConfig) {
  baseConfig = nextConfig;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add('visible');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(function () {
    elements.toast.classList.remove('visible');
  }, 2800);
}

elements.onToast = showToast;

const background = createBackgroundController(elements, getConfig);
const music = createMusicPlayer(getConfig, Object.assign({}, elements, {onToast: showToast}));
const inputs = createInputRow(elements.inputRow, getConfig, {
  onToast: showToast,
  onBeforeLaunch: function () {
    // HDMI / input switch — raise TV volume like launching an app.
    applyAppLaunchVolume();
    const config = getConfig();
    if (config.music && config.music.pauseOnLaunch) {
      music.fadeOutAndPause();
    }
  }
});
function syncHomeWatcher(config, opts) {
  const quiet = opts && opts.quiet;
  const on = !!(config && config.launcher &&
    (config.launcher.launchOnHome || config.launcher.returnOnAppExit));
  const apply = on ? enableHomeWatcher : disableHomeWatcher;
  return apply().then(function () {
    if (on && !quiet) showToast('Home button → Launch Home is active');
  }).catch(function (err) {
    console.error(err);
    if (on) {
      const detail = err && err.message ? String(err.message) : '';
      const short = detail.replace(/\s+/g, ' ').slice(0, 90);
      showToast(short
        ? 'Home watcher failed: ' + short
        : 'Home watcher failed — check root / Homebrew Channel');
    }
  });
}

function syncBootLaunch(config, opts) {
  const quiet = opts && opts.quiet;
  const on = !!(config && config.launcher && config.launcher.bootOnStart);
  const apply = on ? enableBootLaunch : disableBootLaunch;
  return apply().then(function () {
    if (on && !quiet) showToast('Boot on TV start is active');
  }).catch(function (err) {
    console.error(err);
    if (on) {
      const detail = err && err.message ? String(err.message) : '';
      const short = detail.replace(/\s+/g, ' ').slice(0, 90);
      showToast(short
        ? 'Boot on start failed: ' + short
        : 'Boot on start failed — check root / Homebrew Channel');
    }
  });
}

function syncRootHooks(config, opts) {
  return Promise.all([
    syncHomeWatcher(config, opts),
    syncBootLaunch(config, opts)
  ]);
}

const settings = createSettingsPanel(elements.settingsPanel, getBaseConfig, {
  onOpen: function () {
    music.fadeInAndResume();
    if (customScreensaver && typeof customScreensaver.hide === 'function') {
      customScreensaver.hide();
    }
  },
  onRendered: function () {
    // Land focus inside the panel so wheel / arrows navigate options immediately.
    if (focus && typeof focus.focusWithin === 'function') {
      focus.focusWithin('#settings-panel');
    }
  },
  onSave: function (savedConfig) {
    setConfig(savedConfig);
    applyHomeVolume();
    applyScreensaverSetting();
    if (customScreensaver && typeof customScreensaver.applyConfig === 'function') {
      customScreensaver.applyConfig();
    }
    refreshAll();
    syncRootHooks(savedConfig);
  },
  onClose: function () {
    focus.refresh();
    if (customScreensaver && typeof customScreensaver.resetIdle === 'function') {
      customScreensaver.resetIdle();
    }
  },
  onPreviewScreensaver: function () {
    // Several attempts — settings close / focus reclaim can race the first show.
    function once() {
      if (customScreensaver && typeof customScreensaver.preview === 'function') {
        customScreensaver.preview();
      }
    }
    once();
    setTimeout(once, 400);
    setTimeout(once, 1200);
  },
  onToast: showToast
});
function applyHomeVolume() {
  const config = getConfig();
  const level = config.launcher && typeof config.launcher.volumeAtHome === 'number'
    ? config.launcher.volumeAtHome
    : 6;
  return setSystemVolume(level).catch(function () { /* best-effort */ });
}

function applyAppLaunchVolume() {
  const config = getConfig();
  const level = config.launcher && typeof config.launcher.volumeOnAppLaunch === 'number'
    ? config.launcher.volumeOnAppLaunch
    : 13;
  return setSystemVolume(level).catch(function () { /* best-effort */ });
}

function applyScreensaverSetting() {
  const config = getConfig();
  let mins = config.launcher && typeof config.launcher.screensaverMinutes === 'number'
    ? config.launcher.screensaverMinutes
    : 30;
  // When the in-app screensaver is on, push the LG system timer to max so it
  // does not launch com.webos.app.screensaver over our slideshow.
  if (config.launcher && config.launcher.customScreensaver !== false && mins > 0 && mins < 30) {
    mins = 30;
  }
  // Guard against legacy invalid values (5/15/60) still in memory.
  if (mins !== 0 && mins !== 3 && mins !== 10 && mins !== 20 && mins !== 30) {
    if (mins === 5) mins = 3;
    else if (mins === 15) mins = 10;
    else if (mins >= 60) mins = 30;
    else mins = 30;
  }
  return setScreensaverTimeout(mins).catch(function () { /* best-effort */ });
}

const customScreensaver = createCustomScreensaver({
  rootEl: elements.customScreensaver,
  getConfig: getConfig,
  isBlocked: function () {
    if (!visible) return true;
    if (settings && typeof settings.isVisible === 'function' && settings.isVisible()) {
      return true;
    }
    if (launchPending) return true;
    return false;
  },
  onShow: function () {
    // Ambient music keeps playing under the saver.
  },
  onHide: function () {
    reclaimInput();
  }
});

const apps = createAppGrid(elements.appGrid, getConfig, {
  onBeforeLaunch: function () {
    launchPending = true;
    wentHiddenSinceLaunch = false;
    ghostReasserted = false;
    launchAt = Date.now();
    if (customScreensaver && typeof customScreensaver.hide === 'function') {
      customScreensaver.hide();
    }
    const config = getConfig();
    if (config.music && config.music.pauseOnLaunch) {
      music.fadeOutAndPause();
    }
    applyAppLaunchVolume();
  },
  onOpenSettings: function () {
    settings.show();
  },
  onOpenTvSettings: function () {
    openTvSettings();
  },
  onToast: showToast
});
const focus = createFocusManager(document.getElementById('app'), {
  onBack: function () {
    if (settings.isVisible()) {
      if (typeof settings.handleBack === 'function' && settings.handleBack()) {
        return;
      }
      settings.hide();
      return;
    }
  },
  onRed: function () {
    if (settings.isVisible()) return;
    // Red also unlocks autoplay if the TV blocked silent start.
    if (music && typeof music.unlockAutoplay === 'function') {
      music.unlockAutoplay();
    }
    music.togglePause();
  },
  onGreen: function () {
    if (settings.isVisible()) return;
    music.nextTrack();
  },
  onVolumeUp: function () {
    if (settings.isVisible()) return;
    music.nudgeVolume(5);
  },
  onVolumeDown: function () {
    if (settings.isVisible()) return;
    music.nudgeVolume(-5);
  },
  onVolumeMute: function () {
    if (settings.isVisible()) return;
    elements.muteBtn.click();
  }
});

async function openTvSettings() {
  const config = getConfig();
  if (config.music && config.music.pauseOnLaunch) {
    music.fadeOutAndPause();
  }
  applyAppLaunchVolume();
  const ids = getAppIdCandidates('com.webos.app.settings');
  for (let i = 0; i < ids.length; i += 1) {
    try {
      await launchApp(ids[i]);
      return;
    } catch (err) {
      // Try the next candidate id.
    }
  }
  showToast('Could not open TV Settings');
}

if (elements.appSettingsBtn) {
  // Use click only (not pointerup+click). Dual handlers raced with Settings
  // Close, which sits in the same top-right corner as the gear on Magic Remote.
  elements.appSettingsBtn.addEventListener('click', function (event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    settings.show();
  });
}

function formatClockTime(date, timezone) {
  if (timezone && typeof Intl !== 'undefined' && Intl.DateTimeFormat) {
    try {
      const parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: timezone,
        hour: 'numeric',
        minute: '2-digit',
        hour12: false
      }).formatToParts(date);
      let hour = '';
      let minute = '';
      for (let i = 0; i < parts.length; i += 1) {
        if (parts[i].type === 'hour') hour = parts[i].value;
        if (parts[i].type === 'minute') minute = parts[i].value;
      }
      if (hour && minute) return hour + ':' + minute;
    } catch (err) {
      // Invalid timezone — fall back to local time.
    }
  }

  const hours = date.getHours();
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return hours + ':' + minutes;
}

function formatClockDate(date, timezone) {
  const options = {
    weekday: 'long',
    day: 'numeric',
    month: 'long'
  };
  if (timezone) {
    options.timeZone = timezone;
  }
  try {
    return new Intl.DateTimeFormat('en-GB', options).format(date);
  } catch (err) {
    // Invalid timezone — fall back to local date.
    delete options.timeZone;
    return new Intl.DateTimeFormat('en-GB', options).format(date);
  }
}

function applyClockStyle() {
  const launcher = (getConfig().launcher) || {};
  const rawAlign = launcher.clockAlign || 'center';
  // left | center (centre top) | center-middle | right
  const align =
    rawAlign === 'left' ||
    rawAlign === 'right' ||
    rawAlign === 'center-middle'
      ? rawAlign
      : 'center';
  const sizeAllowed = {
    small: 1,
    medium: 1,
    large: 1,
    'x-large': 1,
    'xx-large': 1
  };
  const size = sizeAllowed[launcher.clockSize] ? launcher.clockSize : 'large';

  document.body.classList.remove(
    'clock-left',
    'clock-center',
    'clock-center-middle',
    'clock-right'
  );
  // CSS class: center-middle → clock-center-middle; others → clock-{align}
  document.body.classList.add('clock-' + align);

  document.body.classList.remove(
    'clock-size-small',
    'clock-size-medium',
    'clock-size-large',
    'clock-size-x-large',
    'clock-size-xx-large'
  );
  document.body.classList.add('clock-size-' + size);
}

function updateClock() {
  const config = getConfig();
  const launcher = config.launcher || {};
  const now = new Date();

  applyClockStyle();

  if (launcher.showClock) {
    elements.clock.textContent = formatClockTime(now, launcher.timezone || '');
  } else {
    elements.clock.textContent = '';
  }

  if (elements.clockDate) {
    if (launcher.showDate) {
      elements.clockDate.textContent = formatClockDate(now, launcher.timezone || '');
    } else {
      elements.clockDate.textContent = '';
    }
  }
}

async function applyUsbOverrides() {
  const config = getBaseConfig();
  const paths = await resolveLoungePaths(config);

  if (paths.usbConfig) {
    setConfig(applyUsbConfig(config, paths.usbConfig));
    saveConfig(baseConfig);
  }

  if (!baseConfig.music.path && paths.musicPath) {
    baseConfig.music.path = paths.musicPath;
  }

  if (!baseConfig.background.path && paths.backgroundPath) {
    baseConfig.background.path = paths.backgroundPath;
  }
}

// Icon row layout: 'scroll' keeps a single horizontal row capped to N icons
// (the rest reachable by scrolling left/right), 'wrap' lets icons stack onto
// multiple rows. Scroll is the default.
function applyIconLayout() {
  const grid = elements.appGrid;
  const shell = elements.appGridShell;
  const launcher = elements.launcher;
  if (!grid || !launcher) return;
  const config = getConfig();
  const layout = (config.launcher && config.launcher.iconLayout) || 'scroll';
  launcher.classList.toggle('layout-scroll', layout === 'scroll');
  launcher.classList.toggle('layout-wrap', layout !== 'scroll');
  if (layout === 'scroll') {
    const scaleBySize = {small: 0.78, medium: 1, large: 1.28};
    const iconSize = (config.launcher && config.launcher.iconSize) || 'medium';
    const scale = scaleBySize[iconSize] || 1;
    let perRow = parseInt(config.launcher && config.launcher.iconsPerRow, 10) || 7;
    perRow = Math.min(Math.max(perRow, 3), 12);
    const tileFootprint = (152 * scale) + 28; // tile width + 14px*2 margins
    const maxW = Math.round(perRow * tileFootprint) + 'px';
    // Cap the shell (edges sit on the shell); grid fills the shell width.
    if (shell) {
      shell.style.maxWidth = maxW;
      shell.style.setProperty('--tile-scale', String(scale));
    }
    grid.style.maxWidth = '';
  } else {
    if (shell) {
      shell.style.maxWidth = '';
      shell.style.setProperty('--tile-scale', '1');
    }
    grid.style.maxWidth = '';
  }
  scheduleAppScrollHints();
}

/**
 * Show left/right chevron fades when more app icons exist off-screen.
 * Only applies in scroll-one-row layout.
 */
function updateAppScrollHints() {
  const grid = elements.appGrid;
  const shell = elements.appGridShell;
  const launcher = elements.launcher;
  if (!grid || !shell) return;

  const scrollMode = launcher && launcher.classList.contains('layout-scroll');
  if (!scrollMode) {
    shell.classList.remove('can-scroll-left', 'can-scroll-right');
    return;
  }

  // scrollWidth can lag a frame after tile rebuild; remeasure if needed.
  const maxScroll = grid.scrollWidth - grid.clientWidth;
  const hasOverflow = maxScroll > 8;
  const atLeft = grid.scrollLeft <= 6;
  const atRight = grid.scrollLeft >= maxScroll - 6;

  shell.classList.toggle('can-scroll-left', hasOverflow && !atLeft);
  shell.classList.toggle('can-scroll-right', hasOverflow && !atRight);
}

/** Remeasure after layout/images settle (scrollWidth can lag one frame). */
function scheduleAppScrollHints() {
  updateAppScrollHints();
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(function () {
      updateAppScrollHints();
      setTimeout(updateAppScrollHints, 80);
    });
  } else {
    setTimeout(updateAppScrollHints, 80);
  }
}

function bindAppScrollHints() {
  const grid = elements.appGrid;
  if (!grid || grid.dataset.scrollHintsBound === '1') return;
  grid.dataset.scrollHintsBound = '1';

  grid.addEventListener('scroll', function () {
    updateAppScrollHints();
  }, {passive: true});

  window.addEventListener('resize', function () {
    scheduleAppScrollHints();
  });
}

function applyIconAlign() {
  if (!elements.launcher) return;
  const config = getConfig();
  const align = (config.launcher && config.launcher.iconAlign) || 'center';
  elements.launcher.classList.remove('icons-left', 'icons-center', 'icons-right');
  const cls = align === 'left' ? 'icons-left' : align === 'right' ? 'icons-right' : 'icons-center';
  elements.launcher.classList.add(cls);
}

// When a user on a weaker TV enables Performance mode, the glassmorphic blur and
// animated background are disabled for smoother rendering, while the layout stays
// intact (see the `.perf-mode` rules in styles/main.css).
function applyPerfMode() {
  const config = getConfig();
  const on = !!(config.launcher && config.launcher.perfMode);
  document.body.classList.toggle('perf-mode', on);
}

async function refreshAll() {
  updateClock();
  applyIconAlign();
  applyPerfMode();
  music.applyConfig();
  await background.refresh();
  await inputs.refresh();
  await apps.refresh();
  applyIconLayout();
  scheduleAppScrollHints();
  await music.loadTracks();
  focus.refresh();
}

// Reclaim system keyboard/pointer focus and re-select a dock tile.
// After Home-button intercept, webOS often leaves INPUT routed to stock Home
// even though Launch Home is painted on top — remote OK does nothing until we
// re-assert window focus and a real focusable tile.
function reclaimInput() {
  // Clear any stuck settings-open dim/hide that would block the dock.
  if (settings && !settings.isVisible()) {
    document.body.classList.remove('settings-open');
    if (elements.settingsPanel) {
      elements.settingsPanel.hidden = true;
    }
  }
  try { window.focus(); } catch (err) { /* ignore */ }
  try { document.body && document.body.focus && document.body.focus(); } catch (err) { /* ignore */ }
  try {
    if (document.activeElement && document.activeElement.blur) {
      document.activeElement.blur();
    }
  } catch (err) { /* ignore */ }
  if (focus && typeof focus.focusHomeDock === 'function') {
    focus.focusHomeDock();
  } else {
    focus.refresh();
  }
}

let resumeTimer = null;
function scheduleReclaimBursts() {
  // webOS may deliver input ownership a few hundred ms after the surface paints.
  reclaimInput();
  clearTimeout(scheduleReclaimBursts.t1);
  clearTimeout(scheduleReclaimBursts.t2);
  clearTimeout(scheduleReclaimBursts.t3);
  scheduleReclaimBursts.t1 = setTimeout(reclaimInput, 250);
  scheduleReclaimBursts.t2 = setTimeout(reclaimInput, 800);
  scheduleReclaimBursts.t3 = setTimeout(reclaimInput, 1600);
}

function handleResume() {
  visible = true;
  launchPending = false;
  returningToLounge = false;
  if (voiceIndicator && typeof voiceIndicator.start === 'function') {
    voiceIndicator.start();
  }
  // Debounce stacked resume events (visibility + webOSRelaunch + focus).
  clearTimeout(resumeTimer);
  resumeTimer = setTimeout(function () {
    const config = getConfig();
    applyHomeVolume();
    if (config.music && config.music.resumeOnReturn) {
      music.fadeInAndResume();
    }
    // Do not kill an intentional screensaver preview on resume noise.
    // (Idle auto-show still ends on real key press via the saver itself.)
    if (customScreensaver && typeof customScreensaver.isActive === 'function' &&
        customScreensaver.isActive()) {
      /* leave preview up */
    } else if (customScreensaver && typeof customScreensaver.resetIdle === 'function') {
      customScreensaver.resetIdle();
    }
    refreshAll().then(function () {
      scheduleReclaimBursts();
    }, function (err) {
      console.error(err);
      scheduleReclaimBursts();
    });
  }, 50);
}

function handleVisibilityChange() {
  if (document.hidden) {
    visible = false;
    // Our surface was backgrounded, so any app we launched took the foreground
    // normally. This disarms the ghost-focus recovery for that launch.
    wentHiddenSinceLaunch = true;
    if (voiceIndicator && typeof voiceIndicator.hide === 'function') {
      voiceIndicator.hide();
    }
    if (customScreensaver && typeof customScreensaver.hide === 'function') {
      customScreensaver.hide();
    }
    return;
  }
  handleResume();
}

function shouldInterceptHome(launcher) {
  if (!launcher) return false;
  // launchOnHome (preferred) or legacy returnOnAppExit: when the stock home
  // comes to the foreground after another app (Home button or app exit),
  // relaunch Launch Home so it acts as the home screen.
  return !!(launcher.launchOnHome || launcher.returnOnAppExit);
}

async function launchLoungeBestEffort() {
  try {
    await launchApp(APP_ID);
    return;
  } catch (err) {
    // Sandboxed launch often fails while we are backgrounded.
  }
  try {
    await launchAppViaRoot(APP_ID);
  } catch (err) {
    // Best-effort.
  }
}

async function maybeReturnToLounge(appId) {
  const config = getConfig();
  if (!shouldInterceptHome(config.launcher)) return;
  if (returningToLounge) return;
  if (appId === APP_ID) return;
  if (!isHomeApp(appId)) return;

  // Rate-limit: root watcher + in-app poll can both fire.
  const now = Date.now();
  if (now - lastHomeLaunchAt < 1500) return;
  lastHomeLaunchAt = now;

  // In-app backup for the root home-watcher: whenever stock Home is foreground,
  // bring Launch Home forward. (Root watcher is the reliable path when suspended.)
  returningToLounge = true;
  try {
    await launchLoungeBestEffort();
  } finally {
    returningToLounge = false;
  }
}

function startForegroundWatcher() {
  if (!window.webOS || !window.webOS.service) return;

  const pollMs = shouldInterceptHome(getBaseConfig().launcher) ? 800 : 2000;

  foregroundTimer = setInterval(async function () {
    // Keep polling while backgrounded only when we may need to intercept Home.
    if (!visible && !shouldInterceptHome(getBaseConfig().launcher)) return;

    try {
      const res = await getForegroundApp();
      const appId = res.appId || res.id || '';
      const config = getConfig();

      // Only pause ambient when we actually left Launch Home (or launched an app).
      // Pausing whenever getForegroundApp() != us kills music on cold start
      // (poll often returns Home / empty for a few seconds after launch).
      if (
        appId &&
        appId !== APP_ID &&
        config.music &&
        config.music.pauseOnLaunch &&
        (!visible || launchPending || wentHiddenSinceLaunch)
      ) {
        music.fadeOutAndPause();
      }

      // Ghost-focus recovery.
      //
      // Symptom (confirmed on-device): the user launches an app from the dock
      // (e.g. a "viewer"/media app) that FAILS to fully launch -- it grabs the
      // REMOTE INPUT but never takes the graphics foreground -- so our launcher
      // stays fully visible on top yet receives no usable remote navigation and
      // feels frozen. The pointer still works because pointer events route by
      // screen position, but arrow keys go to the dead app surface.
      //
      // We detect this precisely:
      //   - launchPending          : the user launched something from our dock
      //   - !wentHiddenSinceLaunch : our surface was never backgrounded, i.e.
      //                              we are still the top surface on screen
      //   - appId && appId !== us  : yet the system foreground app is not us
      //
      // This excludes apps opened normally (they fire visibilitychange->hidden,
      // setting wentHiddenSinceLaunch) and benign failed launches (nothing
      // actually launched, so the foreground app stays us). In those cases we
      // must NOT act, or we'd yank the user out of their app.
      //
      // Recovery: CLOSE the stuck app first -- relaunching ourselves alone is
      // not enough because webOS keeps input routed to that app's surface until
      // it is torn down -- then bring ourselves back to the foreground and
      // reclaim DOM focus. We deliberately do NOT clear launchPending here: if
      // the close/relaunch didn't take, the next poll retries until the
      // foreground is us again (self-healing) or the 15s launch window expires.
      if (launchPending && !wentHiddenSinceLaunch && visible &&
          appId && appId !== APP_ID && !returningToLounge &&
          (Date.now() - launchAt) > 2500) {
        // Native streaming apps (Prime Video = amazon) often grab input
        // before their card paints. Closing them looks like "launch did
        // nothing". Re-assert once via root instead.
        if (/amazon|netflix|youtube|disney|iplayer|apple\.appletv/i.test(appId)) {
          if (!ghostReasserted) {
            ghostReasserted = true;
            try {
              await launchAppViaRoot(appId);
            } catch (errAssert) {
              /* keep waiting for the native card */
            }
          }
          return;
        }
        returningToLounge = true;
        try {
          await closeApp(appId);
        } catch (err) {
          // The stuck app may already be gone; keep going.
        }
        try {
          await launchApp(APP_ID);
        } catch (err) {
          // Best-effort reclaim.
        } finally {
          returningToLounge = false;
        }
        reclaimInput();
        lastForegroundAppId = APP_ID;
        return;
      }

      // Stop watching a launch once it clearly resolved one way or another.
      if (launchPending && (wentHiddenSinceLaunch || (Date.now() - launchAt) > 15000)) {
        launchPending = false;
      }

      // We just became the system foreground again (e.g. after Home intercept).
      // Force input reclaim even if visibilitychange was flaky.
      if (appId === APP_ID && lastForegroundAppId && lastForegroundAppId !== APP_ID) {
        scheduleReclaimBursts();
      }

      await maybeReturnToLounge(appId);
      lastForegroundAppId = appId || lastForegroundAppId;
    } catch (err) {
      // Foreground polling is best-effort.
    }
  }, pollMs);
}

function handlePowerOff() {
  music.stop();
}

async function autoEnableTerminal() {
  if (baseConfig.launcher && baseConfig.launcher.terminalChecked) return;

  let installed = [];
  try {
    const res = await listApps();
    installed = (res && res.apps) || [];
  } catch (err) {
    return; // Could not list apps; try again on the next launch.
  }

  const terminal = installed.find(function (app) {
    return app && isTerminalAppId(app.id);
  });

  const pinned = (baseConfig.launcher.pinnedApps || []);
  if (terminal && pinned.indexOf(terminal.id) < 0) {
    pinned.push(terminal.id);
    baseConfig.launcher.pinnedApps = pinned;
  }

  baseConfig.launcher.terminalChecked = true;
  saveConfig(baseConfig);
}

async function init() {
  await applyUsbOverrides();
  await autoEnableTerminal();
  updateClock();
  setInterval(updateClock, 30000);
  bindAppScrollHints();

  applyHomeVolume();
  applyScreensaverSetting();
  await refreshAll();

  // In-app photo/clock screensaver after idle.
  if (customScreensaver && typeof customScreensaver.start === 'function') {
    customScreensaver.start();
  }

  // One-shot force preview: URL, PalmSystem launch params, webOSRelaunch,
  // or root flag file /tmp/launch-home-preview-ss (written by install/SSH).
  function parseLaunchParamsObject(raw) {
    if (!raw) return null;
    if (typeof raw === 'object') return raw;
    try { return JSON.parse(String(raw)); } catch (err) { return null; }
  }
  function launchParamsWantPreview() {
    try {
      // Injected by install/SSH: <script>window.__LH_PREVIEW_SS=1</script>
      if (window.__LH_PREVIEW_SS === 1 || window.__LH_PREVIEW_SS === true ||
          window.__LH_PREVIEW_SS === '1') {
        return true;
      }
    } catch (err0) { /* ignore */ }
    try {
      if (typeof location !== 'undefined' &&
          /(?:\?|&)previewSs=1(?:&|$)/.test(location.search || '')) {
        return true;
      }
    } catch (err) { /* ignore */ }
    const candidates = [];
    try {
      if (window.PalmSystem && window.PalmSystem.launchParams) {
        candidates.push(window.PalmSystem.launchParams);
      }
    } catch (err2) { /* ignore */ }
    try {
      if (window.webOSSystem && window.webOSSystem.launchParams) {
        candidates.push(window.webOSSystem.launchParams);
      }
    } catch (err3) { /* ignore */ }
    for (let i = 0; i < candidates.length; i += 1) {
      const p = parseLaunchParamsObject(candidates[i]);
      if (p && (p.previewSs === 1 || p.previewSs === true || p.previewSs === '1')) {
        return true;
      }
    }
    return false;
  }
  function triggerScreensaverPreview(reason) {
    console.log('[launch-home] screensaver preview:', reason || '');
    if (customScreensaver && typeof customScreensaver.preview === 'function') {
      customScreensaver.preview();
    }
  }
  if (launchParamsWantPreview()) {
    setTimeout(function () { triggerScreensaverPreview('launchParams'); }, 900);
    setTimeout(function () { triggerScreensaverPreview('launchParams-retry'); }, 2500);
  }
  // Root-written flag: reliable on TV even when launch params are dropped.
  try {
    execRoot(
      'if [ -f /tmp/launch-home-preview-ss ]; then cat /tmp/launch-home-preview-ss; rm -f /tmp/launch-home-preview-ss; fi'
    ).then(function (res) {
      let text = (res && res.stdoutString ? String(res.stdoutString) : '').trim();
      if (!text && res && res.stdoutBytes) {
        try { text = String(atob(res.stdoutBytes)).trim(); } catch (e) { /* ignore */ }
      }
      if (text === '1' || text === 'preview' || text.indexOf('1') === 0) {
        setTimeout(function () { triggerScreensaverPreview('flag-file'); }, 600);
        setTimeout(function () { triggerScreensaverPreview('flag-file-retry'); }, 2200);
      }
    }).catch(function () { /* ignore */ });
  } catch (errFlag) { /* ignore */ }
  document.addEventListener('webOSRelaunch', function (ev) {
    try {
      const d = (ev && ev.detail) || {};
      const p = d.returnValue != null ? d : (d.params || d);
      if (p && (p.previewSs === 1 || p.previewSs === true || p.previewSs === '1')) {
        setTimeout(function () { triggerScreensaverPreview('webOSRelaunch'); }, 400);
      }
    } catch (errR) { /* ignore */ }
  });

  // Mic + AI badge while VoxRelay is listening (top-right).
  if (voiceIndicator && typeof voiceIndicator.start === 'function') {
    voiceIndicator.start();
  }

  // Voice app launch: VoxRelay parses "launch Prime Video" but the native
  // amazon card often fails to replace Launch Home unless we launch from
  // this foreground web app (sandbox + root, every known id).
  function voiceLaunchDeduped(spec) {
    if (!spec || !apps || typeof apps.launchApp !== 'function') return;
    const key = String((spec.ids && spec.ids[0]) || spec.id || spec.title || '');
    const now = Date.now();
    if (key && key === lastVoiceLaunchKey && now - lastVoiceLaunchAt < 4000) {
      return;
    }
    lastVoiceLaunchKey = key;
    lastVoiceLaunchAt = now;
    apps.launchApp(spec).catch(function (err) {
      console.error(err);
    });
  }

  addVoxrelayListener(function (eventName, payload) {
    if (eventName === 'appLaunch') {
      const ids = [];
      if (payload && payload.id) ids.push(payload.id);
      if (payload && Array.isArray(payload.ids)) {
        payload.ids.forEach(function (id) {
          if (id && ids.indexOf(id) < 0) ids.push(id);
        });
      }
      if (!ids.length) return;
      if (ids.some(isCompanionVoiceApp)) return;
      voiceLaunchDeduped({
        id: ids[0],
        launchId: ids[0],
        ids: ids,
        title: (payload && payload.spoken) || ids[0]
      });
      return;
    }
    if (eventName === 'transcriptFinal') {
      const text = payload && (payload.text || payload.transcript);
      const spec = resolveVoiceLaunch(text, getConfig());
      if (spec) voiceLaunchDeduped(spec);
    }
  });

  // Retry ambient autoplay shortly after startup (webOS often allows play once
  // the app surface is fully focused, even if the first play() was blocked).
  setTimeout(function () {
    if (music && typeof music.unlockAutoplay === 'function') {
      music.unlockAutoplay();
    } else if (music && typeof music.fadeInAndResume === 'function') {
      music.fadeInAndResume();
    }
  }, 600);
  setTimeout(function () {
    if (music && typeof music.unlockAutoplay === 'function') {
      music.unlockAutoplay();
    }
  }, 2000);

  document.addEventListener('visibilitychange', handleVisibilityChange);
  // webOS fires `webOSRelaunch` on the document when the user returns to an
  // already-running app (e.g. after another app closes or fails to launch).
  // Treat it as a resume so a suspended launcher wakes up and regains input.
  document.addEventListener('webOSRelaunch', handleResume);
  window.addEventListener('focus', function () {
    reclaimInput();
    if (music && typeof music.unlockAutoplay === 'function') {
      music.unlockAutoplay();
    }
  });
  window.addEventListener('pagehide', handlePowerOff);
  startForegroundWatcher();

  // Re-assert root hooks if the user previously enabled them. The home watcher
  // and boot-launch init.d must be re-written after every install (ipk replace
  // drops scripts; hbchannel may kill orphan processes).
  const cfg = getBaseConfig();
  if (shouldInterceptHome(cfg.launcher) || (cfg.launcher && cfg.launcher.bootOnStart)) {
    syncRootHooks(cfg, {quiet: true}).catch(function () { /* ignore */ });
  }
}

init().catch(function (err) {
  showToast('Startup error — check USB paths');
  console.error(err);
});