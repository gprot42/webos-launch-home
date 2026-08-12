/**
 * In-app screensaver for Launch Home.
 *
 * After idle timeout while Launch Home is foreground, shows a full-screen
 * photo slideshow (from the same background sources as the home wallpaper)
 * with an optional large clock. Any remote key / pointer dismisses it.
 *
 * Runs only inside this app — does not replace the system-wide LG saver when
 * other apps are open. When active, motion (slideshow + slow Ken Burns) helps
 * OLED burn-in protection and makes the system saver less likely to steal focus.
 */

import {normalizeBackgroundConfig, resolveBackgroundImages} from './backgrounds.js';
import {resolveLoungePaths} from './usb.js';

const PRESETS = {
  'warm-gradient': 'linear-gradient(135deg, #1a1028 0%, #3d1f2e 40%, #7a3b2e 100%)',
  'cool-gradient': 'linear-gradient(145deg, #0b1220 0%, #162447 50%, #1f4068 100%)',
  'midnight': 'linear-gradient(180deg, #050508 0%, #12121a 60%, #1c1c28 100%)',
  'ember': 'radial-gradient(ellipse at 30% 20%, #4a1942 0%, #1a0a14 50%, #080408 100%)'
};

function formatTime(date, timezone) {
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
    } catch (err) { /* fall through */ }
  }
  return date.getHours() + ':' + String(date.getMinutes()).padStart(2, '0');
}

function formatDate(date, timezone) {
  const options = {weekday: 'long', day: 'numeric', month: 'long'};
  if (timezone) options.timeZone = timezone;
  try {
    return new Intl.DateTimeFormat('en-GB', options).format(date);
  } catch (err) {
    delete options.timeZone;
    return new Intl.DateTimeFormat('en-GB', options).format(date);
  }
}

/**
 * @param {object} options
 * @param {HTMLElement} options.rootEl
 * @param {() => object} options.getConfig
 * @param {() => boolean} [options.isBlocked]  true while settings open / not foreground
 * @param {() => void} [options.onShow]
 * @param {() => void} [options.onHide]
 */
export function createCustomScreensaver(options) {
  const rootEl = options.rootEl;
  const getConfig = options.getConfig;
  const isBlocked = typeof options.isBlocked === 'function'
    ? options.isBlocked
    : function () { return false; };
  const onShow = options.onShow || function () {};
  const onHide = options.onHide || function () {};

  if (!rootEl) {
    return {
      start: function () {},
      stop: function () {},
      resetIdle: function () {},
      isActive: function () { return false; },
      applyConfig: function () {}
    };
  }

  const bgA = rootEl.querySelector('.custom-screensaver-bg-a');
  const bgB = rootEl.querySelector('.custom-screensaver-bg-b');
  const timeEl = rootEl.querySelector('.custom-screensaver-time');
  const dateEl = rootEl.querySelector('.custom-screensaver-date');

  let active = false;
  let stopped = true;
  let idleTimer = null;
  let slideTimer = null;
  let clockTimer = null;
  let images = [];
  let slideIndex = 0;
  let useA = true;
  let dismissArmed = false;
  let loadGen = 0;
  let usbBackgroundPath = '';

  function cfg() {
    const launcher = (getConfig().launcher) || {};
    return {
      enabled: launcher.customScreensaver !== false,
      idleMinutes: typeof launcher.customScreensaverMinutes === 'number'
        ? launcher.customScreensaverMinutes
        : 5,
      slideSec: typeof launcher.customScreensaverSlideSec === 'number'
        ? launcher.customScreensaverSlideSec
        : 20,
      showClock: launcher.customScreensaverShowClock !== false,
      showDate: launcher.customScreensaverShowDate !== false,
      timezone: launcher.timezone || ''
    };
  }

  function clearIdleTimer() {
    if (idleTimer) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
  }

  function clearSlideTimer() {
    if (slideTimer) {
      clearInterval(slideTimer);
      slideTimer = null;
    }
  }

  function clearClockTimer() {
    if (clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  }

  function paintClock() {
    const c = cfg();
    const now = new Date();
    if (timeEl) {
      timeEl.textContent = c.showClock ? formatTime(now, c.timezone) : '';
      timeEl.hidden = !c.showClock;
    }
    if (dateEl) {
      dateEl.textContent = c.showDate ? formatDate(now, c.timezone) : '';
      dateEl.hidden = !c.showDate;
    }
  }

  function applyLayer(el, urlOrGradient, isGradient) {
    if (!el) return;
    if (isGradient) {
      el.style.backgroundImage = urlOrGradient;
      el.classList.remove('is-photo');
    } else {
      const safe = String(urlOrGradient).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      el.style.backgroundImage = 'url("' + safe + '")';
      el.classList.add('is-photo');
    }
  }

  function showSlide(index) {
    if (!images.length) return;
    const item = images[index % images.length];
    const front = useA ? bgA : bgB;
    const back = useA ? bgB : bgA;
    applyLayer(back, item.url, item.gradient);
    // Cross-fade: back becomes visible, front fades out.
    if (back) {
      back.classList.add('is-front');
      back.classList.remove('is-back');
    }
    if (front) {
      front.classList.remove('is-front');
      front.classList.add('is-back');
    }
    useA = !useA;
  }

  async function loadImages() {
    const gen = ++loadGen;
    const config = getConfig();
    const bg = normalizeBackgroundConfig(config.background);
    const out = [];

    if (bg.source === 'preset' || bg.source === 'animated-gradient') {
      const grad = PRESETS[bg.preset] || PRESETS['warm-gradient'];
      out.push({url: grad, gradient: true});
      return out;
    }

    if (!usbBackgroundPath) {
      try {
        const paths = await resolveLoungePaths(config);
        if (gen !== loadGen) return null;
        usbBackgroundPath = paths.backgroundPath || '';
      } catch (err) {
        usbBackgroundPath = '';
      }
    }

    // Prefer a multi-image set for motion (OLED-friendly). Temporarily force
    // slideshow mode when resolving so builtin/url catalogs return the full list.
    const probe = JSON.parse(JSON.stringify(config));
    if (!probe.background) probe.background = {};
    if (probe.background.source === 'builtin' || probe.background.source === 'url') {
      probe.background.mode = 'slideshow';
    }

    let list = [];
    try {
      list = await resolveBackgroundImages(probe, usbBackgroundPath);
    } catch (err) {
      list = [];
    }
    if (gen !== loadGen) return null;

    if (!list.length) {
      // Fall back to whatever the home wallpaper would use (single image).
      try {
        list = await resolveBackgroundImages(config, usbBackgroundPath);
      } catch (err2) {
        list = [];
      }
    }
    if (gen !== loadGen) return null;

    // Deduplicate while preserving order.
    const seen = {};
    for (let i = 0; i < list.length; i += 1) {
      const u = list[i];
      if (!u || seen[u]) continue;
      seen[u] = true;
      out.push({url: u, gradient: false});
    }

    if (!out.length) {
      out.push({url: PRESETS.midnight, gradient: true});
    }
    return out;
  }

  function startSlideshow() {
    clearSlideTimer();
    if (images.length <= 1) {
      showSlide(0);
      return;
    }
    showSlide(slideIndex);
    const sec = Math.max(8, cfg().slideSec || 20);
    slideTimer = setInterval(function () {
      slideIndex += 1;
      showSlide(slideIndex);
    }, sec * 1000);
  }

  function show(opts) {
    const force = !!(opts && opts.force);
    if (active) return;
    if (stopped) {
      if (!force) return;
      // Force preview: start listeners first.
      stopped = false;
      document.addEventListener('keydown', onActivity, true);
      document.addEventListener('keyup', onActivity, true);
      document.addEventListener('pointerdown', onActivity, true);
      document.addEventListener('mousedown', onActivity, true);
      document.addEventListener('click', onActivity, true);
      document.addEventListener('wheel', onActivity, true);
      document.addEventListener('mousemove', onActivity, true);
      document.addEventListener('touchstart', onActivity, true);
    }
    if (!force) {
      if (isBlocked()) return;
      if (!cfg().enabled) return;
    }

    active = true;
    dismissArmed = false;
    clearIdleTimer();
    rootEl.hidden = false;
    rootEl.removeAttribute('hidden');
    rootEl.setAttribute('aria-hidden', 'false');
    rootEl.classList.add('visible');
    // Instant paint — no wait for CSS transition opacity 0→1 on first frame.
    rootEl.style.opacity = '1';
    document.body.classList.add('custom-screensaver-active');

    paintClock();
    clearClockTimer();
    clockTimer = setInterval(paintClock, 15000);

    loadImages().then(function (list) {
      if (!active || !list) return;
      images = list;
      slideIndex = 0;
      useA = true;
      if (bgA) {
        bgA.classList.add('is-front');
        bgA.classList.remove('is-back');
        // Paint first frame immediately (crossfade starts empty otherwise).
        if (list[0]) applyLayer(bgA, list[0].url, list[0].gradient);
      }
      if (bgB) {
        bgB.classList.add('is-back');
        bgB.classList.remove('is-front');
      }
      startSlideshow();
    });

    // Ignore the key/click that might still be settling; arm dismiss shortly.
    // Force previews stay up longer before dismiss is armed (remote bounce).
    setTimeout(function () {
      if (active) dismissArmed = true;
    }, force ? 1200 : 400);

    try { onShow(); } catch (err) { /* ignore */ }
  }

  function hide() {
    if (!active) return;
    active = false;
    dismissArmed = false;
    clearSlideTimer();
    clearClockTimer();
    rootEl.classList.remove('visible');
    rootEl.style.opacity = '';
    rootEl.setAttribute('aria-hidden', 'true');
    rootEl.hidden = true;
    rootEl.setAttribute('hidden', '');
    document.body.classList.remove('custom-screensaver-active');
    try { onHide(); } catch (err) { /* ignore */ }
    scheduleIdle();
  }

  function scheduleIdle() {
    clearIdleTimer();
    if (stopped || active) return;
    if (!cfg().enabled) return;
    if (isBlocked()) return;
    if (typeof document !== 'undefined' && document.hidden) return;

    let mins = cfg().idleMinutes;
    if (isNaN(mins) || mins < 1) mins = 5;
    if (mins > 180) mins = 180;
    idleTimer = setTimeout(function () {
      idleTimer = null;
      show();
    }, mins * 60 * 1000);
  }

  function resetIdle() {
    if (active) {
      if (dismissArmed) hide();
      return;
    }
    scheduleIdle();
  }

  function isWakeEvent(event) {
    if (!event) return true;
    const t = event.type || '';
    // Magic Remote pointer drift should not dismiss; real presses / keys do.
    return t === 'keydown' || t === 'keyup' || t === 'pointerdown' ||
      t === 'mousedown' || t === 'click' || t === 'wheel' || t === 'touchstart';
  }

  function onActivity(event) {
    if (stopped) return;
    if (active) {
      if (!dismissArmed || !isWakeEvent(event)) return;
      // Swallow the wake input so it does not click a tile under the overlay.
      if (event) {
        try {
          event.preventDefault();
          event.stopPropagation();
        } catch (err) { /* ignore */ }
      }
      hide();
      return;
    }
    scheduleIdle();
  }

  function start() {
    if (!stopped) {
      scheduleIdle();
      return;
    }
    stopped = false;
    document.addEventListener('keydown', onActivity, true);
    document.addEventListener('keyup', onActivity, true);
    document.addEventListener('pointerdown', onActivity, true);
    document.addEventListener('mousedown', onActivity, true);
    document.addEventListener('click', onActivity, true);
    document.addEventListener('wheel', onActivity, true);
    document.addEventListener('mousemove', onActivity, true);
    document.addEventListener('touchstart', onActivity, true);
    scheduleIdle();
  }

  function stop() {
    stopped = true;
    clearIdleTimer();
    hide();
    document.removeEventListener('keydown', onActivity, true);
    document.removeEventListener('keyup', onActivity, true);
    document.removeEventListener('pointerdown', onActivity, true);
    document.removeEventListener('mousedown', onActivity, true);
    document.removeEventListener('click', onActivity, true);
    document.removeEventListener('wheel', onActivity, true);
    document.removeEventListener('mousemove', onActivity, true);
    document.removeEventListener('touchstart', onActivity, true);
  }

  function applyConfig() {
    if (stopped) return;
    if (!cfg().enabled) {
      clearIdleTimer();
      if (active) hide();
      return;
    }
    if (active) {
      paintClock();
      startSlideshow();
    } else {
      scheduleIdle();
    }
  }

  return {
    start: start,
    stop: stop,
    resetIdle: resetIdle,
    isActive: function () { return active; },
    applyConfig: applyConfig,
    /**
     * Force-show (settings Preview / SSH flag / launch params).
     * Ignores enabled toggle and isBlocked so demos always appear.
     */
    preview: function () {
      if (stopped) start();
      // Hide any half-state then force show.
      if (active) {
        active = false;
        clearSlideTimer();
        clearClockTimer();
      }
      show({force: true});
    },
    hide: hide
  };
}
