import {normalizeBackgroundConfig, resolveBackgroundImages} from './backgrounds.js';
import {resolveLoungePaths} from './usb.js';

const PRESETS = {
  'warm-gradient': 'linear-gradient(135deg, #1a1028 0%, #3d1f2e 40%, #7a3b2e 100%)',
  'cool-gradient': 'linear-gradient(145deg, #0b1220 0%, #162447 50%, #1f4068 100%)',
  'midnight': 'linear-gradient(180deg, #050508 0%, #12121a 60%, #1c1c28 100%)',
  'ember': 'radial-gradient(ellipse at 30% 20%, #4a1942 0%, #1a0a14 50%, #080408 100%)'
};

function preloadImage(url) {
  return new Promise(function (resolve) {
    if (!url) {
      resolve(false);
      return;
    }
    const img = new Image();
    let settled = false;
    function done(ok) {
      if (settled) return;
      settled = true;
      resolve(!!ok);
    }
    img.onload = function () {
      done(true);
    };
    img.onerror = function () {
      done(false);
    };
    // Older webOS can hang forever on a bad URL — bound the wait.
    setTimeout(function () {
      // naturalWidth > 0 means decode progressed even if onload is flaky.
      done(!!(img.complete && img.naturalWidth > 0));
    }, 8000);
    try {
      img.src = url;
    } catch (err) {
      done(false);
    }
  });
}

export function createBackgroundController(elements, getConfig) {
  const layer = elements.backgroundLayer;
  const scrim = elements.scrim;
  let slideshowTimer = null;
  let slideshowIndex = 0;
  let slideshowImages = [];
  let usbBackgroundPath = '';
  let refreshGen = 0;

  function applyPreset(name, animated) {
    const gradient = PRESETS[name] || PRESETS['warm-gradient'];
    layer.style.backgroundImage = gradient;
    layer.style.backgroundColor = '#0a0a0f';
    layer.style.backgroundSize = 'cover';
    layer.style.backgroundPosition = 'center center';
    layer.style.backgroundRepeat = 'no-repeat';
    layer.classList.remove('has-image');
    layer.classList.remove('ken-burns');
    layer.classList.toggle('gradient-animated', !!animated);
    syncMovingBackground();
  }

  // Frosted glass over a moving wallpaper is re-blurred every frame (13 fps on
  // a webOS 25 OLED); body.bg-moving swaps the blur for a tint (main.css).
  function syncMovingBackground() {
    document.body.classList.toggle('bg-moving',
      layer.classList.contains('ken-burns') || layer.classList.contains('gradient-animated'));
  }

  function applyImage(url) {
    const config = getConfig();
    const bg = normalizeBackgroundConfig(config.background);
    // Quote-safe CSS url(); escape backslashes and quotes for file paths.
    const safe = String(url).replace(/\\/g, '\\\\').replace(/"/g, '\\"');

    layer.style.backgroundColor = '#0a0a0f';
    layer.style.backgroundImage = 'url("' + safe + '")';
    layer.style.backgroundSize = 'cover';
    layer.style.backgroundPosition = 'center center';
    layer.style.backgroundRepeat = 'no-repeat';
    layer.classList.add('has-image');
    layer.classList.remove('gradient-animated');

    if (bg.kenBurns) {
      layer.classList.add('ken-burns');
    } else {
      layer.classList.remove('ken-burns');
    }
    syncMovingBackground();
  }

  function applyScrim() {
    const config = getConfig();
    const opacity = (config.background && config.background.overlayOpacity) || 0.45;
    scrim.style.opacity = String(opacity);
  }

  function clearSlideshow() {
    if (slideshowTimer) {
      clearInterval(slideshowTimer);
      slideshowTimer = null;
    }
  }

  function showSlideshowImage() {
    if (!slideshowImages.length) return;
    applyImage(slideshowImages[slideshowIndex % slideshowImages.length]);
    slideshowIndex += 1;
  }

  function startSlideshow(images, intervalSec) {
    slideshowImages = images;
    slideshowIndex = 0;
    showSlideshowImage();
    slideshowTimer = setInterval(showSlideshowImage, intervalSec * 1000);
  }

  /**
   * Try each candidate URL until one decodes. webOS 4 may reject one form of
   * path but accept another (relative vs absolute file://).
   */
  async function pickLoadableImage(candidates) {
    for (let i = 0; i < candidates.length; i += 1) {
      const url = candidates[i];
      if (!url) continue;
      // Apply optimistically so the user sees something while we verify.
      applyImage(url);
      const ok = await preloadImage(url);
      if (ok) return url;
    }
    return '';
  }

  async function refresh() {
    const gen = ++refreshGen;
    clearSlideshow();
    applyScrim();

    const config = getConfig();
    const bg = normalizeBackgroundConfig(config.background);

    if (bg.source === 'preset' || bg.source === 'animated-gradient') {
      applyPreset(bg.preset || 'warm-gradient', bg.source === 'animated-gradient');
      return;
    }

    if (!usbBackgroundPath) {
      const paths = await resolveLoungePaths(config);
      if (gen !== refreshGen) return;
      usbBackgroundPath = paths.backgroundPath || '';
    }

    const images = await resolveBackgroundImages(config, usbBackgroundPath);
    if (gen !== refreshGen) return;

    if (!images.length) {
      applyPreset(bg.preset || 'warm-gradient', false);
      return;
    }

    if (bg.mode === 'slideshow' && images.length > 1) {
      // Verify at least the first image loads; still run the full list.
      const first = await pickLoadableImage(images);
      if (gen !== refreshGen) return;
      if (!first) {
        applyPreset(bg.preset || 'warm-gradient', false);
        return;
      }
      startSlideshow(images, bg.slideshowIntervalSec || 300);
      return;
    }

    const loaded = await pickLoadableImage(images);
    if (gen !== refreshGen) return;
    if (!loaded) {
      applyPreset(bg.preset || 'warm-gradient', false);
    }
  }

  return {
    refresh: refresh,
    destroy: clearSlideshow
  };
}
