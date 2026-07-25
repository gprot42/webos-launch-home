import {normalizeMusicConfig, resolveBuiltinPlaylist} from './builtin-music.js';
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
    return {url: entry, title: '', artist: ''};
  }
  return {
    url: entry.url || '',
    title: entry.title || '',
    artist: entry.artist || ''
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
  let targetVolume = 0.15;
  let userPaused = false;

  function trackLabel(track) {
    if (!track) return '';

    if (track.artist && track.title) {
      return track.artist + ' — ' + track.title;
    }
    if (track.title) return track.title;

    const parts = track.url.split('/');
    const name = parts[parts.length - 1] || 'Unknown track';
    return name.replace(/\.[^.]+$/, '');
  }

  function currentTrack() {
    return queue[queueIndex] || null;
  }

  function updateNowPlaying() {
    titleEl.textContent = trackLabel(currentTrack());
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

  function playCurrent() {
    if (!queue.length) {
      updateNowPlaying();
      return;
    }

    const track = queue[queueIndex];
    audio.src = track.url;
    audio.load();
    audio.volume = muted ? 0 : targetVolume;
    userPaused = false;
    audio.play().catch(function () {
      skipUnsupported();
    });
    updateNowPlaying();
  }

  function skipUnsupported() {
    showToast('Unsupported or missing track — skipping');
    nextTrack(true);
  }

  function nextTrack(fromError) {
    if (!queue.length) return;

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
        updateNowPlaying();
        return;
      }
    }

    playCurrent();
  }

  function showToast(message) {
    if (elements.onToast) elements.onToast(message);
  }

  async function loadTracks() {
    const config = getConfig();
    const music = normalizeMusicConfig(config.music || {});

    if (!music.enabled) {
      tracks = [];
      queue = [];
      audio.pause();
      updateNowPlaying();
      return;
    }

    let discovered = [];

    if (music.source === 'builtin') {
      discovered = await resolveBuiltinPlaylist(music.builtin, music.builtinPlaylist);
    } else {
      discovered = await discoverMusicTracks(music.path || '');
      // Optional subset chosen in Settings (urls). Empty = play all found.
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
    }

    tracks = discovered.map(normalizeTrackEntry).filter(function (track) {
      return track && track.url && isAudioFile(track.url);
    });
    queue = music.shuffle ? shuffleArray(tracks) : tracks.slice();
    queueIndex = 0;

    if (queue.length) {
      playCurrent();
    } else {
      updateNowPlaying();
    }
  }

  function fadeOutAndPause() {
    const config = getConfig();
    const fadeSec = (config.music && config.music.fadeSec) || 2;
    fadeTo(0, fadeSec, function () {
      audio.pause();
    });
  }

  function fadeInAndResume() {
    const config = getConfig();
    if (!config.music || !config.music.enabled) return;
    if (userPaused) return;

    const fadeSec = (config.music && config.music.fadeSec) || 2;
    if (audio.src) {
      audio.play().catch(function () {});
      fadeTo(muted ? 0 : targetVolume, fadeSec);
    } else if (queue.length) {
      playCurrent();
    }
  }

  function togglePause() {
    if (!queue.length) return;

    if (audio.paused) {
      userPaused = false;
      fadeInAndResume();
      return;
    }

    userPaused = true;
    fadeOutAndPause();
  }

  function stop() {
    clearFade();
    userPaused = false;
    audio.pause();
    audio.removeAttribute('src');
    updateNowPlaying();
  }

  audio.addEventListener('ended', function () {
    nextTrack(false);
  });

  audio.addEventListener('error', function () {
    skipUnsupported();
  });

  muteBtn.addEventListener('click', function () {
    muted = !muted;
    muteBtn.setAttribute('aria-pressed', muted ? 'true' : 'false');
    muteBtn.textContent = muted ? '🔇' : '🔉';
    audio.volume = muted ? 0 : targetVolume;
  });

  volumeSlider.addEventListener('input', function () {
    const value = Number(volumeSlider.value) / 100;
    setVolume(value, true);
    updateSliderFill();
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

    volumeSlider.classList.add('pulse');
    controlsWrap.classList.add('pulsing');
    clearTimeout(nudgeVolume.pulseTimer);
    nudgeVolume.pulseTimer = setTimeout(function () {
      volumeSlider.classList.remove('pulse');
      controlsWrap.classList.remove('pulsing');
    }, 500);

    return next;
  }

  function reflectSystemVolume(volume, systemMuted) {
    const min = Number(volumeSlider.min) || 0;
    const max = Number(volumeSlider.max) || 100;
    const clamped = Math.max(min, Math.min(max, Math.round(volume)));

    volumeSlider.value = String(clamped);
    setVolume(clamped / 100, true);
    updateSliderFill();

    muted = !!systemMuted || clamped <= min;
    muteBtn.setAttribute('aria-pressed', muted ? 'true' : 'false');
    muteBtn.textContent = muted ? '🔇' : '🔉';

    volumeSlider.classList.add('pulse');
    controlsWrap.classList.add('pulsing');
    clearTimeout(reflectSystemVolume.pulseTimer);
    reflectSystemVolume.pulseTimer = setTimeout(function () {
      volumeSlider.classList.remove('pulse');
      controlsWrap.classList.remove('pulsing');
    }, 500);
  }

  subscribeVolume(reflectSystemVolume);

  return {
    loadTracks: loadTracks,
    fadeOutAndPause: fadeOutAndPause,
    fadeInAndResume: fadeInAndResume,
    togglePause: togglePause,
    stop: stop,
    nextTrack: function () { nextTrack(false); },
    nudgeVolume: nudgeVolume,
    get enabled() {
      const config = getConfig();
      return config.music && config.music.enabled;
    },
    applyConfig: function () {
      const config = getConfig();
      const volume = (config.music && config.music.volume) || 0.15;
      setVolume(volume, true);
      volumeSlider.value = String(Math.round(volume * 100));
      updateSliderFill();
      muteBtn.textContent = muted ? '🔇' : '🔉';
    }
  };
}