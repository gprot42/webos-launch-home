import {
  normalizeMusicConfig,
  resolveBuiltinPlaylist,
  BUILTIN_MUSIC_FALLBACK,
  builtinTrackCandidates
} from './builtin-music.js';
import {discoverMusicTracks, isAudioFile} from './usb.js';
import {subscribeVolume} from './luna.js';

function shuffleArray(items) {
  const arr = items.slice();
  for (let i = arr.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    const tmp = arr[i];
    arr[i] = arr[j];
    arr[j] = tmp;
  }
  return arr;
}

function normalizeTrackEntry(entry) {
  if (!entry) return null;
  if (typeof entry === 'string') {
    return {url: entry, urls: [entry], title: '', artist: ''};
  }
  const url = entry.url || '';
  let urls = Array.isArray(entry.urls) ? entry.urls.slice() : [];
  if (url && urls.indexOf(url) < 0) urls.unshift(url);
  if (!urls.length && url) urls = [url];
  return {
    url: url || (urls[0] || ''),
    urls: urls,
    title: entry.title || '',
    artist: entry.artist || '',
    id: entry.id || '',
    file: entry.file || ''
  };
}

export function createMusicPlayer(getConfig, elements) {
  const audio = elements.audio;
  const titleEl = elements.trackTitle;
  const muteBtn = elements.muteBtn;
  const volumeSlider = elements.volumeSlider;
  const controlsWrap = volumeSlider.closest('.music-controls') || volumeSlider.parentElement;

  let tracks = [];
  let queue = [];
  let queueIndex = 0;
  let muted = false;
  let fadeTimer = null;
  let targetVolume = 0.25;
  let userPaused = false;
  // True when browser/TV blocked autoplay — retry on first remote/key gesture.
  let pendingAutoplay = false;
  let urlCandidateIndex = 0;
  let autoplayUnlockBound = false;
  let playGeneration = 0;
  let canPlayListener = null;
  let skipCount = 0;

  function trackLabel(track) {
    if (!track) return '';

    if (track.artist && track.title) {
      return track.artist + ' — ' + track.title;
    }
    if (track.title) return track.title;

    const parts = (track.url || '').split('/');
    const name = parts[parts.length - 1] || 'Unknown track';
    return name.replace(/\.[^.]+$/, '');
  }

  function currentTrack() {
    return queue[queueIndex] || null;
  }

  function musicBarEl() {
    return elements.musicBar || (titleEl && titleEl.closest && titleEl.closest('.music-bar'));
  }

  function showBarEnabled() {
    const config = getConfig();
    return !!(config.music && config.music.showBar);
  }

  function applyMusicBarVisibility() {
    const bar = musicBarEl();
    if (!bar) return;
    const config = getConfig();
    const musicOn = !!(config.music && config.music.enabled);
    bar.classList.toggle('music-hidden', !musicOn);
    bar.classList.toggle('show-bar', musicOn && showBarEnabled());
  }

  function updateNowPlaying() {
    const bar = musicBarEl();
    const track = currentTrack();
    let text = '';
    if (track) {
      text = pendingAutoplay
        ? trackLabel(track) + ' — press OK / any key to play'
        : trackLabel(track);
    }
    titleEl.textContent = text;
    if (bar) {
      if (text) {
        bar.classList.add('has-track');
      } else {
        bar.classList.remove('has-track');
      }
    }
    applyMusicBarVisibility();
  }

  function setVolume(value, immediate) {
    targetVolume = value;
    if (immediate) {
      audio.volume = muted ? 0 : value;
    }
  }

  function updateSliderFill() {
    const min = Number(volumeSlider.min) || 0;
    const max = Number(volumeSlider.max) || 100;
    const pct = ((Number(volumeSlider.value) - min) / (max - min)) * 100;
    volumeSlider.style.setProperty('--fill', pct + '%');
  }

  function clearFade() {
    if (fadeTimer) {
      clearInterval(fadeTimer);
      fadeTimer = null;
    }
  }

  function clearCanPlayWait() {
    if (canPlayListener) {
      try {
        audio.removeEventListener('canplay', canPlayListener);
        audio.removeEventListener('loadeddata', canPlayListener);
      } catch (err) { /* ignore */ }
      canPlayListener = null;
    }
  }

  function fadeTo(volume, durationSec, onDone) {
    clearFade();
    const start = audio.volume;
    const delta = volume - start;
    if (!durationSec || Math.abs(delta) < 0.01) {
      audio.volume = volume;
      if (onDone) onDone();
      return;
    }

    const steps = Math.max(10, Math.floor(durationSec * 20));
    let step = 0;
    fadeTimer = setInterval(function () {
      step += 1;
      const progress = step / steps;
      audio.volume = start + delta * progress;
      if (step >= steps) {
        clearFade();
        audio.volume = volume;
        if (onDone) onDone();
      }
    }, (durationSec * 1000) / steps);
  }

  function candidateUrls(track) {
    if (!track) return [];
    if (Array.isArray(track.urls) && track.urls.length) return track.urls;
    if (track.url) return [track.url];
    return [];
  }

  /**
   * Start playback. Older webOS WebKits return void from audio.play() (not a
   * Promise) — calling .then on that crashed the player and left silence.
   * Some TVs only allow autoplay when muted; we try muted first then unmute.
   */
  function tryPlay() {
    return new Promise(function (resolve) {
      function afterPlayAttempt(ok) {
        if (ok) {
          pendingAutoplay = false;
          // Restore real volume after a muted autoplay unlock.
          try {
            audio.muted = false;
          } catch (err) { /* ignore */ }
          audio.volume = muted ? 0 : targetVolume;
          updateNowPlaying();
        }
        resolve(!!ok);
      }

      function invokePlay(useMuted) {
        let result;
        const prevMuted = audio.muted;
        try {
          if (useMuted) audio.muted = true;
          result = audio.play();
        } catch (err) {
          try { audio.muted = prevMuted; } catch (e2) { /* ignore */ }
          const name = err && err.name ? String(err.name) : '';
          if (name === 'NotAllowedError' || /user|gesture|autoplay/i.test(String(err && err.message || ''))) {
            pendingAutoplay = true;
            updateNowPlaying();
            afterPlayAttempt(false);
            return;
          }
          afterPlayAttempt(false);
          return;
        }

        // Legacy: play() returns undefined — check paused after a tick.
        if (result == null || typeof result.then !== 'function') {
          setTimeout(function () {
            if (!audio.paused) {
              afterPlayAttempt(true);
              return;
            }
            if (useMuted) {
              try { audio.muted = prevMuted; } catch (e2) { /* ignore */ }
              // Try again unmuted in case muted path failed silently.
              invokePlay(false);
              return;
            }
            pendingAutoplay = true;
            updateNowPlaying();
            afterPlayAttempt(false);
          }, 100);
          return;
        }

        result.then(function () {
          afterPlayAttempt(true);
        }, function (err) {
          try { audio.muted = prevMuted; } catch (e2) { /* ignore */ }
          const name = err && err.name ? String(err.name) : '';
          if (useMuted) {
            // Muted autoplay failed — try unmuted (may still need gesture).
            invokePlay(false);
            return;
          }
          if (
            name === 'NotAllowedError' ||
            name === 'AbortError' ||
            /user|gesture|autoplay/i.test(String(err && err.message || ''))
          ) {
            pendingAutoplay = true;
            updateNowPlaying();
            afterPlayAttempt(false);
            return;
          }
          if (!audio.error) {
            pendingAutoplay = true;
            updateNowPlaying();
            afterPlayAttempt(false);
            return;
          }
          afterPlayAttempt(false);
        });
      }

      audio.volume = muted ? 0 : targetVolume;
      // Prefer muted autoplay (allowed on more WebKits), then unmute.
      invokePlay(true);
    });
  }

  function unlockAutoplay() {
    if (userPaused) return;
    const config = getConfig();
    if (!config.music || !config.music.enabled) return;
    if (!queue.length) return;

    // Always attempt play on user gesture (even if pendingAutoplay was false).
    if (audio.src) {
      try {
        audio.muted = false;
      } catch (err) { /* ignore */ }
      audio.volume = muted ? 0 : targetVolume;
      tryPlay().then(function (ok) {
        if (ok) {
          pendingAutoplay = false;
          updateNowPlaying();
        }
      });
      return;
    }

    if (queue.length) {
      playCurrent();
    }
  }

  function bindAutoplayUnlock() {
    if (autoplayUnlockBound) return;
    autoplayUnlockBound = true;
    const unlock = function () {
      unlockAutoplay();
    };
    document.addEventListener('keydown', unlock, true);
    document.addEventListener('keyup', unlock, true);
    document.addEventListener('pointerdown', unlock, true);
    document.addEventListener('click', unlock, true);
    // webOS remote often surfaces as keydown on body after focus.
    if (document.body) {
      document.body.addEventListener('keydown', unlock, true);
    }
  }

  function playCurrent() {
    if (!queue.length) {
      pendingAutoplay = false;
      updateNowPlaying();
      return;
    }

    const track = queue[queueIndex];
    const urls = candidateUrls(track);
    if (!urls.length) {
      skipUnsupported();
      return;
    }

    if (urlCandidateIndex >= urls.length) {
      urlCandidateIndex = 0;
      skipUnsupported();
      return;
    }

    const src = urls[urlCandidateIndex];
    track.url = src;
    playGeneration += 1;
    const gen = playGeneration;
    clearCanPlayWait();

    try {
      audio.pause();
    } catch (err) { /* ignore */ }

    audio.src = src;
    audio.volume = muted ? 0 : targetVolume;
    userPaused = false;
    updateNowPlaying();

    function startWhenReady() {
      if (gen !== playGeneration) return;
      clearCanPlayWait();
      tryPlay().then(function (ok) {
        if (gen !== playGeneration) return;
        if (ok) {
          skipCount = 0;
          return;
        }
        // Not playing: media error → next URL; autoplay → wait for key.
        if (audio.error) {
          urlCandidateIndex += 1;
          if (urlCandidateIndex < urls.length) {
            playCurrent();
          } else {
            urlCandidateIndex = 0;
            skipUnsupported();
          }
        }
      });
    }

    // Wait briefly for data; also try immediately (some WebKits need both).
    canPlayListener = startWhenReady;
    audio.addEventListener('canplay', canPlayListener);
    audio.addEventListener('loadeddata', canPlayListener);

    try {
      audio.load();
    } catch (err) { /* ignore */ }

    // Fallback if canplay never fires (common with file:// on some builds).
    setTimeout(function () {
      if (gen !== playGeneration) return;
      if (!audio.paused) return;
      startWhenReady();
    }, 400);
  }

  function skipUnsupported() {
    skipCount += 1;
    if (skipCount >= Math.max(queue.length, 1) * 3) {
      // All candidates failed repeatedly — stop the skip storm.
      showToast('Could not play ambient music');
      pendingAutoplay = false;
      updateNowPlaying();
      return;
    }
    showToast('Unsupported or missing track — skipping');
    nextTrack(true);
  }

  function nextTrack(fromError) {
    if (!queue.length) return;

    urlCandidateIndex = 0;
    queueIndex += 1;
    const config = getConfig();
    const repeat = (config.music && config.music.repeat) || 'all';

    if (queueIndex >= queue.length) {
      if (repeat === 'all') {
        queueIndex = 0;
        if (config.music && config.music.shuffle) {
          queue = shuffleArray(tracks);
        }
      } else if (repeat === 'one' && !fromError) {
        queueIndex -= 1;
      } else {
        audio.pause();
        pendingAutoplay = false;
        updateNowPlaying();
        return;
      }
    }

    playCurrent();
  }

  function showToast(message) {
    if (elements.onToast) elements.onToast(message);
  }

  function fallbackBuiltinTracks() {
    return BUILTIN_MUSIC_FALLBACK.map(function (entry) {
      return {
        url: 'assets/music/' + entry.file,
        urls: builtinTrackCandidates(entry.file),
        title: entry.title || entry.id,
        artist: entry.artist || '',
        id: entry.id,
        file: entry.file
      };
    });
  }

  function acceptTrack(track) {
    if (!track) return false;
    const urls = candidateUrls(track);
    if (!urls.length) return false;
    // Always accept packaged music paths; isAudioFile strips ?v= query strings.
    return urls.some(function (u) {
      return (
        isAudioFile(u) ||
        /\.mp3/i.test(String(u)) ||
        /\/music\//i.test(String(u)) ||
        /assets\/music\//i.test(String(u))
      );
    });
  }

  async function loadTracks() {
    const config = getConfig();
    const music = normalizeMusicConfig(config.music || {});

    if (!music.enabled) {
      tracks = [];
      queue = [];
      pendingAutoplay = false;
      try { audio.pause(); } catch (err) { /* ignore */ }
      titleEl.textContent = '';
      updateNowPlaying();
      return;
    }

    let discovered = [];

    try {
      if (music.source === 'builtin') {
        discovered = await resolveBuiltinPlaylist(music.builtin, music.builtinPlaylist);
      } else {
        discovered = await discoverMusicTracks(music.path || '');
        if (music.usbPlaylist && music.usbPlaylist.length) {
          const allow = {};
          music.usbPlaylist.forEach(function (url) {
            if (url) allow[String(url)] = true;
          });
          discovered = discovered.filter(function (entry) {
            const url = typeof entry === 'string' ? entry : entry && entry.url;
            return url && allow[String(url)];
          });
        }
        // USB empty → fall back to built-ins so the user still hears music.
        if (!discovered || !discovered.length) {
          discovered = await resolveBuiltinPlaylist(music.builtin, music.builtinPlaylist);
        }
      }
    } catch (err) {
      discovered = [];
    }

    if (!discovered || !discovered.length) {
      discovered = fallbackBuiltinTracks();
    }

    tracks = discovered.map(normalizeTrackEntry).filter(acceptTrack);

    // Last resort: hard-coded relative paths only (never show empty playlist).
    if (!tracks.length) {
      tracks = fallbackBuiltinTracks().map(normalizeTrackEntry).filter(acceptTrack);
    }

    queue = music.shuffle ? shuffleArray(tracks) : tracks.slice();
    queueIndex = 0;
    urlCandidateIndex = 0;
    skipCount = 0;

    if (queue.length) {
      bindAutoplayUnlock();
      playCurrent();
    } else {
      pendingAutoplay = false;
      updateNowPlaying();
      // Should be unreachable with hard-coded fallback; keep quiet if it isn't.
      console.error('Launch Home: music playlist empty after fallbacks');
    }
  }

  function fadeOutAndPause() {
    const config = getConfig();
    const fadeSec = (config.music && config.music.fadeSec) || 2;
    fadeTo(0, fadeSec, function () {
      try { audio.pause(); } catch (err) { /* ignore */ }
    });
  }

  function fadeInAndResume() {
    const config = getConfig();
    if (!config.music || !config.music.enabled) return;
    if (userPaused) return;

    const fadeSec = (config.music && config.music.fadeSec) || 2;
    if (audio.src) {
      audio.volume = muted ? 0 : 0;
      tryPlay().then(function (ok) {
        if (ok) fadeTo(muted ? 0 : targetVolume, fadeSec);
      });
    } else if (queue.length) {
      playCurrent();
    }
  }

  function togglePause() {
    if (!queue.length) {
      // Try reloading tracks if empty (e.g. previous load failed).
      loadTracks();
      return;
    }

    if (audio.paused) {
      userPaused = false;
      pendingAutoplay = false;
      fadeInAndResume();
      return;
    }

    userPaused = true;
    pendingAutoplay = false;
    fadeOutAndPause();
  }

  function stop() {
    clearFade();
    clearCanPlayWait();
    playGeneration += 1;
    userPaused = false;
    pendingAutoplay = false;
    try { audio.pause(); } catch (err) { /* ignore */ }
    try { audio.removeAttribute('src'); } catch (err) { /* ignore */ }
    updateNowPlaying();
  }

  audio.addEventListener('ended', function () {
    nextTrack(false);
  });

  audio.addEventListener('error', function () {
    if (!queue.length) return;
    // Ignore stale errors after a new src was chosen.
    const track = currentTrack();
    const urls = candidateUrls(track);
    urlCandidateIndex += 1;
    if (urlCandidateIndex < urls.length) {
      playCurrent();
      return;
    }
    urlCandidateIndex = 0;
    skipUnsupported();
  });

  muteBtn.addEventListener('click', function () {
    muted = !muted;
    muteBtn.setAttribute('aria-pressed', muted ? 'true' : 'false');
    muteBtn.textContent = muted ? '🔇' : '🔉';
    audio.volume = muted ? 0 : targetVolume;
    unlockAutoplay();
  });

  volumeSlider.addEventListener('input', function () {
    const value = Number(volumeSlider.value) / 100;
    setVolume(value, true);
    updateSliderFill();
    unlockAutoplay();
  });

  function nudgeVolume(deltaPercent) {
    const min = Number(volumeSlider.min) || 0;
    const max = Number(volumeSlider.max) || 100;
    const next = Math.max(min, Math.min(max, Number(volumeSlider.value) + deltaPercent));
    volumeSlider.value = String(next);

    if (muted && next > min) {
      muted = false;
      muteBtn.setAttribute('aria-pressed', 'false');
      muteBtn.textContent = '🔉';
    }

    setVolume(next / 100, true);
    updateSliderFill();
    unlockAutoplay();

    volumeSlider.classList.add('pulse');
    controlsWrap.classList.add('pulsing');
    clearTimeout(nudgeVolume.pulseTimer);
    nudgeVolume.pulseTimer = setTimeout(function () {
      volumeSlider.classList.remove('pulse');
      controlsWrap.classList.remove('pulsing');
    }, 500);

    return next;
  }

  /**
   * System mute only. Do not map TV volume 0–100 onto ambient 0–1 (that made
   * ambient inaudible whenever the TV volume was low).
   */
  function reflectSystemVolume(_volume, systemMuted) {
    if (systemMuted) {
      audio.volume = 0;
      return;
    }
    if (!muted) {
      audio.volume = targetVolume;
    }
  }

  subscribeVolume(reflectSystemVolume);
  bindAutoplayUnlock();

  return {
    loadTracks: loadTracks,
    fadeOutAndPause: fadeOutAndPause,
    fadeInAndResume: fadeInAndResume,
    togglePause: togglePause,
    stop: stop,
    nextTrack: function () { nextTrack(false); },
    nudgeVolume: nudgeVolume,
    unlockAutoplay: unlockAutoplay,
    get enabled() {
      const config = getConfig();
      return config.music && config.music.enabled;
    },
    applyConfig: function () {
      const config = getConfig();
      const volume = (config.music && typeof config.music.volume === 'number')
        ? config.music.volume
        : 0.35;
      setVolume(volume, true);
      volumeSlider.value = String(Math.round(volume * 100));
      updateSliderFill();
      muteBtn.textContent = muted ? '🔇' : '🔉';
      applyMusicBarVisibility();
    }
  };
}
