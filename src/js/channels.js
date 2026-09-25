/**
 * Home -> Channels chip: Live TV's channels in a strip above the inputs row.
 * The first chip shows what is on now and opens Live TV; the others switch
 * Live TV to that channel. The chip only appears when the TV has channels
 * tuned (luna.js getTvChannels): a TV used only with a set-top box has none.
 *
 * Setting launcher.channels: 'favourites' (LG's favourites, or every channel
 * when there are none), 'all', or 'off'.
 */

import {getTvChannelNow, getTvChannels, launchApp, launchAppViaRoot, openTvChannel} from './luna.js';

const LIVE_TV_ID = 'com.webos.app.livetv';
// Channel lists change rarely (a re-tune); read again after this long.
const LIST_MAX_AGE_MS = 30 * 60 * 1000;

export function createChannelStrip(strip, getConfig, options) {
  const opts = options || {};
  let channels = [];
  let loadedAt = 0;
  let loading = null;
  let open = false;

  function mode() {
    const launcher = (getConfig() || {}).launcher || {};
    const value = launcher.channels;
    return value === 'all' || value === 'off' ? value : 'favourites';
  }

  // TV channels to offer: never hidden/skipped ones or radio; LG's favourites
  // when asked for and there are some.
  function shown() {
    if (mode() === 'off') return [];
    const tv = channels.filter(function (c) { return !c.hidden && !c.radio; });
    if (mode() === 'favourites') {
      const favourites = tv.filter(function (c) { return c.favourite; });
      if (favourites.length) return favourites;
    }
    return tv;
  }

  // Any watchable channel at all, whatever the setting (Live TV input).
  function hasChannels() {
    return channels.some(function (c) { return !c.hidden && !c.radio; });
  }

  function load(force) {
    if (loading) return loading;
    if (!force && loadedAt && Date.now() - loadedAt < LIST_MAX_AGE_MS) {
      return Promise.resolve(channels);
    }
    loading = getTvChannels().catch(function () {
      return [];
    }).then(function (list) {
      const had = shown().length > 0;
      const hadAny = hasChannels();
      channels = list;
      loadedAt = Date.now();
      loading = null;
      if ((had !== shown().length > 0 || hadAny !== hasChannels()) && opts.onAvailabilityChange) {
        opts.onAvailabilityChange();
      }
      return channels;
    });
    return loading;
  }

  function chip(number, name, extraClass) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'channel-chip focusable' + (extraClass ? ' ' + extraClass : '');
    button.dataset.focusIndex = String(150 + strip.children.length);
    const num = document.createElement('span');
    num.className = 'channel-number';
    num.textContent = number;
    const label = document.createElement('span');
    label.className = 'channel-name';
    label.textContent = name;
    button.appendChild(num);
    button.appendChild(label);
    strip.appendChild(button);
    return {button: button, number: num, label: label};
  }

  function openLiveTv() {
    if (opts.onBeforeLaunch) opts.onBeforeLaunch();
    close();
    return launchApp(LIVE_TV_ID).catch(function () {
      return launchAppViaRoot(LIVE_TV_ID);
    }).catch(function () {
      if (opts.onToast) opts.onToast('Could not open Live TV');
    });
  }

  function select(channel) {
    if (opts.onBeforeLaunch) opts.onBeforeLaunch();
    close();
    openTvChannel(channel).catch(function () {
      if (opts.onToast) opts.onToast('Could not switch to ' + channel.name);
    });
  }

  function render() {
    strip.innerHTML = '';
    // What's on now: a text preview (a web app can't show the tuner's picture).
    const now = chip('Live TV', 'What’s on now…', 'channel-chip-now');
    now.button.addEventListener('click', openLiveTv);
    getTvChannelNow().then(function (info) {
      if (!open) return;
      const channel = [info.number, info.name].filter(Boolean).join(' ');
      now.label.textContent = channel
        ? channel + (info.programme ? ' · ' + info.programme : '')
        : 'Watch Live TV';
    }).catch(function () {
      now.label.textContent = 'Watch Live TV';
    });
    shown().forEach(function (channel) {
      const c = chip(channel.number, channel.name);
      c.button.addEventListener('click', function () { select(channel); });
    });
    return now.button;
  }

  function show() {
    open = true;
    strip.hidden = false;
    if (opts.onToggle) opts.onToggle(true);
    const first = render();
    if (opts.focusControl) opts.focusControl(first);
    // Pick up a re-tune while the strip is open.
    load().then(function () {
      if (open && strip.children.length - 1 !== shown().length) render();
    });
  }

  function close() {
    if (!open) return;
    open = false;
    strip.hidden = true;
    strip.innerHTML = '';
    if (opts.onToggle) opts.onToggle(false);
  }

  return {
    // Read the channel list if it is older than LIST_MAX_AGE_MS (at start and
    // on each return to Launch Home).
    refresh: function () { return load(false); },
    available: function () { return shown().length > 0; },
    hasChannels: hasChannels,
    isOpen: function () { return open; },
    toggle: function () {
      if (open) close();
      else show();
    },
    close: close
  };
}
