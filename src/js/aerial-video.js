import {
  normalizeVideoConfig,
  resolveAerialSelection,
  resolveAerialSourceUrl,
  detectAerialCapabilities
} from './aerial.js';
import {getHdrSupport} from './platform.js';

/**
 * Aerial-video playback layer (Settings -> Background -> Aerial videos).
 *
 * Owns one muted, no-controls <video> element painted between the background
 * layer and the scrim. It shows a clip's poster while it buffers, plays the
 * resolved quality variant, rotates on the clip's `ended` event (a single pick
 * loops), skips a clip that fails to load, and — when every clip is dead —
 * hands control back to the caller for the gradient fallback.
 *
 * Playback pauses whenever nobody is looking at the wallpaper: the in-app
 * Settings panel, an overlay/other app in front (body.app-inactive), the All
 * apps grid, or a backgrounded document. Reduced-motion devices get the still
 * poster instead of motion.
 */

// Body state classes that mean the wallpaper is not being watched.
const PAUSING_CLASSES = ['settings-open', 'app-inactive', 'all-apps-open'];

// Cap how long a clip may buffer before it is treated as dead.
const LOAD_TIMEOUT_MS = 12000;

function prefersReducedMotion() {
  try {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (err) {
    return false;
  }
}

export function createAerialVideoLayer(video, opts) {
  const options = opts || {};
  const probeHdr = options.probeHdr || getHdrSupport;

  let clips = [];
  let index = 0;
  let failed = {}; // clip index → true once it refused to play this session
  let quality = 'auto';
  let perfMode = false;
  let capabilities = {canPlayHevc: false, hdrCapable: false};
  let active = false;
  let posterOnly = false;
  let loading = false;
  let cancelPending = null; // finishes an in-flight load as a failure
  let gen = 0;
  let observer = null;
  let bound = false;

  function capsOpts() {
    return {
      quality: quality,
      perfMode: perfMode,
      canPlayHevc: capabilities.canPlayHevc,
      hdrCapable: capabilities.hdrCapable
    };
  }

  function isPlaying() {
    return active && !posterOnly && !video.paused && !video.ended;
  }

  function notify() {
    if (typeof options.onPlayStateChange === 'function') options.onPlayStateChange();
  }

  function shouldPause() {
    if (document.hidden) return true;
    for (let i = 0; i < PAUSING_CLASSES.length; i += 1) {
      if (document.body.classList.contains(PAUSING_CLASSES[i])) return true;
    }
    return false;
  }

  /** Pause or resume to match the current visible/active state. */
  function syncPlayback() {
    if (!active || posterOnly) return;
    if (shouldPause()) {
      video.pause();
      notify();
      return;
    }
    if (video.src) {
      const promise = video.play();
      if (promise && promise.catch) promise.catch(function () { /* autoplay blocked */ });
    }
  }

  function retryOnGesture() {
    function retry() {
      document.removeEventListener('keydown', retry, true);
      document.removeEventListener('click', retry, true);
      syncPlayback();
    }
    document.addEventListener('keydown', retry, true);
    document.addEventListener('click', retry, true);
  }

  /**
   * Load one clip and resolve true once it can play (or false on failure).
   * The listeners are transient: while a load is in flight the persistent
   * `error` handler stands down so the two can't both react to one failure.
   */
  function playClip(i) {
    const clip = clips[i];
    const url = clip ? resolveAerialSourceUrl(clip.sources, capsOpts()) : '';
    if (!url) return Promise.resolve(false);

    return new Promise(function (resolve) {
      let settled = false;

      function cleanup() {
        video.removeEventListener('canplay', onReady);
        video.removeEventListener('error', onFail);
        loading = false;
        cancelPending = null;
      }
      function finish(ok) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        cleanup();
        resolve(ok);
      }
      function onReady() {
        finish(true);
        video.classList.add('is-active');
        const promise = video.play();
        if (promise && promise.catch) {
          promise.catch(function () { retryOnGesture(); });
        }
      }
      function onFail() {
        failed[i] = true;
        finish(false);
      }

      const timer = setTimeout(function () {
        failed[i] = true;
        finish(false);
      }, LOAD_TIMEOUT_MS);

      loading = true;
      cancelPending = function () { onFail(); };
      try {
        video.poster = clip.poster || '';
        video.src = url;
        // Reveal the layer now: its `poster` shows while the clip buffers.
        video.classList.add('is-active');
        video.load();
      } catch (err) {
        onFail();
      }
    });
  }

  /** Show a stale poster without loading any video bytes (reduced motion). */
  function showPoster(clip) {
    try {
      video.removeAttribute('src');
      video.load();
    } catch (err) { /* ignore */ }
    if (clip) video.poster = clip.poster || '';
    video.classList.add('is-active');
  }

  /** How many selected clips have not been ruled out this session. */
  function playableCount() {
    let n = 0;
    for (let i = 0; i < clips.length; i += 1) {
      if (!failed[i]) n += 1;
    }
    return n;
  }

  /** Loop when only one clip can still play; otherwise rotate on `ended`. */
  function applyLoop() {
    video.loop = playableCount() <= 1;
  }

  /**
   * Move to the next clip that still plays. A dead clip (or one that fails
   * mid-rotation) is skipped; when nothing is left, hand off to the fallback.
   */
  async function advance() {
    const myGen = gen;
    const total = clips.length;
    for (let step = 1; step < total; step += 1) {
      const next = (index + step) % total;
      if (failed[next]) continue;
      const ok = await playClip(next);
      if (myGen !== gen) return;
      if (ok) {
        index = next;
        applyLoop();
        video.classList.add('is-active');
        syncPlayback();
        return;
      }
    }
    // Nothing else plays: if the clip that just ended is still healthy, loop it
    // rather than dropping a working clip to the gradient fallback.
    if (!failed[index] && clips[index]) {
      video.loop = true;
      video.classList.add('is-active');
      syncPlayback();
      return;
    }
    active = false;
    video.classList.remove('is-active');
    notify();
    if (typeof options.onExhausted === 'function') options.onExhausted();
  }

  function onRuntimeError() {
    if (loading) return; // the in-flight load owns this failure
    failed[index] = true;
    advance();
  }

  function onEnded() {
    if (clips.length > 1) advance();
  }

  function bindEvents() {
    if (bound) return;
    bound = true;
    video.addEventListener('error', onRuntimeError);
    video.addEventListener('ended', onEnded);
    video.addEventListener('playing', notify);
    video.addEventListener('pause', notify);
    document.addEventListener('visibilitychange', syncPlayback);
    if (typeof MutationObserver === 'function') {
      observer = new MutationObserver(syncPlayback);
      observer.observe(document.body, {attributes: true, attributeFilter: ['class']});
    }
  }

  function unbindEvents() {
    if (!bound) return;
    bound = false;
    video.removeEventListener('error', onRuntimeError);
    video.removeEventListener('ended', onEnded);
    video.removeEventListener('playing', notify);
    video.removeEventListener('pause', notify);
    document.removeEventListener('visibilitychange', syncPlayback);
    if (observer) {
      observer.disconnect();
      observer = null;
    }
  }

  /**
   * Put the aerial layer up for `bg`. Resolves true once a clip is playing
   * (or a reduced-motion poster is showing), false when every clip is dead —
   * the caller then falls back to a gradient.
   *
   * @param {object} bg normalized background config (videoId/videoIds/videoQuality)
   * @param {{perfMode?: boolean}} [runOpts]
   */
  async function start(bg, runOpts) {
    const myGen = gen + 1;
    gen = myGen;
    if (cancelPending) cancelPending();

    const cfg = normalizeVideoConfig(bg);
    quality = cfg.videoQuality || 'auto';
    perfMode = !!(runOpts && runOpts.perfMode);
    clips = resolveAerialSelection(bg);
    failed = {};
    index = 0;
    active = false;
    posterOnly = prefersReducedMotion();
    video.classList.remove('is-active');

    if (!clips.length) return false;

    if (posterOnly) {
      showPoster(clips[0]);
      bindEvents();
      active = true;
      notify();
      return true;
    }

    capabilities = await detectAerialCapabilities(probeHdr);
    if (myGen !== gen) return false;

    // First playable clip wins; every dead one is skipped.
    let played = false;
    for (let i = 0; i < clips.length && !played; i += 1) {
      played = await playClip(i);
      if (myGen !== gen) return false;
      if (played) index = i;
      else failed[i] = true;
    }
    if (!played) {
      // Nothing playable: hide the layer so the caller's gradient shows.
      video.classList.remove('is-active');
      return false;
    }

    applyLoop();
    bindEvents();
    active = true;
    video.classList.add('is-active');
    syncPlayback();
    notify();
    return true;
  }

  function stop() {
    gen += 1;
    if (cancelPending) cancelPending();
    active = false;
    posterOnly = false;
    clips = [];
    index = 0;
    unbindEvents();
    try {
      video.pause();
      video.removeAttribute('src');
      video.load();
    } catch (err) { /* ignore */ }
    video.classList.remove('is-active');
    notify();
  }

  function destroy() {
    stop();
    video.removeAttribute('poster');
  }

  return {
    start: start,
    stop: stop,
    destroy: destroy,
    isPlaying: isPlaying
  };
}
