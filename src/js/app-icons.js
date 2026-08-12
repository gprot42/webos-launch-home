const BUILTIN_APP_ICONS = {
  'com.palm.app.settings': 'assets/app-icons/tv-settings.png',
  'com.webos.app.settings': 'assets/app-icons/tv-settings.png',
  'org.webosbrew.hbchannel': 'assets/app-icons/homebrew.png',
  'com.apple.appletv': 'assets/app-icons/apple-tv.png',
  'netflix': 'assets/app-icons/netflix.png',
  'com.netflix.ninja': 'assets/app-icons/netflix.png',
  'amazon.html': 'assets/app-icons/prime-video.png',
  'amazon': 'assets/app-icons/prime-video.png',
  'com.amazon.amazonvideo.livingroom': 'assets/app-icons/prime-video.png',
  'youtube.leanback.v4': 'assets/app-icons/youtube.png',
  'com.google.android.youtube.tv': 'assets/app-icons/youtube.png',
  'com.webos.app.lgchannels': 'assets/app-icons/lg-channels.png',
  'com.webos.app.livetv': 'assets/app-icons/lg-channels.png',
  'bbc.iplayer.lge': 'assets/app-icons/bbc-iplayer.png',
  'bbc.iplayer': 'assets/app-icons/bbc-iplayer.png',
  'tv.wuaki': 'assets/app-icons/rakuten.png',
  'com.webos.app.browser': 'assets/app-icons/browser.png',
  'com.webos.app.mediadiscovery': 'assets/app-icons/media-player.png',
  'com.zattoo.itvx': 'assets/app-icons/itvx.png',
  'itvx': 'assets/app-icons/itvx.png',
  'com.channel4.vod': 'assets/app-icons/channel4.png',
  'all4': 'assets/app-icons/channel4.png',
  'com.channel5.my5': 'assets/app-icons/channel5.png',
  'my5': 'assets/app-icons/channel5.png',
  'com.disney.disneyplus': 'assets/app-icons/disney-plus.png',
  'disneyplus': 'assets/app-icons/disney-plus.png',
  'com.webosbrew.terminal': 'assets/app-icons/terminal.png',
  'com.webos.app.terminal': 'assets/app-icons/terminal.png'
};

const BUILTIN_APP_TITLES = {
  'com.palm.app.settings': 'TV Settings',
  'com.webos.app.settings': 'TV Settings',
  'org.webosbrew.hbchannel': 'Homebrew',
  'com.apple.appletv': 'Apple TV',
  'netflix': 'Netflix',
  'com.netflix.ninja': 'Netflix',
  'amazon.html': 'Prime Video',
  'amazon': 'Prime Video',
  'com.amazon.amazonvideo.livingroom': 'Prime Video',
  'youtube.leanback.v4': 'YouTube',
  'com.google.android.youtube.tv': 'YouTube',
  'com.webos.app.lgchannels': 'LG Channels',
  'com.webos.app.livetv': 'LG Channels',
  'bbc.iplayer.lge': 'BBC iPlayer',
  'bbc.iplayer': 'BBC iPlayer',
  'tv.wuaki': 'Rakuten TV',
  'com.webos.app.browser': 'Web Browser',
  'com.webos.app.mediadiscovery': 'Media Player',
  'com.zattoo.itvx': 'ITVX',
  'itvx': 'ITVX',
  'com.channel4.vod': 'Channel 4',
  'all4': 'Channel 4',
  'com.channel5.my5': 'Channel 5',
  'my5': 'Channel 5',
  'com.disney.disneyplus': 'Disney+',
  'disneyplus': 'Disney+',
  'com.webosbrew.terminal': 'Terminal',
  'com.webos.app.terminal': 'Terminal'
};

/** Same app, different IDs across webOS 6 vs current TVs. */
export const APP_ID_ALIASES = {
  'com.palm.app.settings': ['com.webos.app.settings'],
  'com.webos.app.settings': ['com.palm.app.settings'],
  'youtube.leanback.v4': ['com.google.android.youtube.tv'],
  'netflix': ['com.netflix.ninja'],
  // Prime Video: older webOS used amazon.html; current OLEDs use native "amazon".
  'amazon.html': ['amazon', 'com.amazon.amazonvideo.livingroom'],
  'amazon': ['amazon.html', 'com.amazon.amazonvideo.livingroom'],
  'com.amazon.amazonvideo.livingroom': ['amazon', 'amazon.html'],
  'com.webos.app.lgchannels': ['com.webos.app.livetv'],
  'bbc.iplayer.lge': ['bbc.iplayer'],
  'bbc.iplayer': ['bbc.iplayer.lge'],
  'com.zattoo.itvx': ['itvx'],
  'com.channel4.vod': ['all4'],
  'com.channel5.my5': ['my5'],
  'com.disney.disneyplus': ['disneyplus']
};

export function getAppIdCandidates(id) {
  const seen = {};
  const out = [];

  function add(candidate) {
    if (!candidate || seen[candidate]) return;
    seen[candidate] = true;
    out.push(candidate);
  }

  // Current LG OLEDs ship Prime Video as native `amazon`. Trying amazon.html
  // first costs a multi-second Luna timeout on every launch / catalog lookup.
  if (id === 'amazon.html' || id === 'amazon' ||
      id === 'com.amazon.amazonvideo.livingroom') {
    add('amazon');
    add('amazon.html');
    add('com.amazon.amazonvideo.livingroom');
    return out;
  }

  add(id);
  (APP_ID_ALIASES[id] || []).forEach(add);
  return out;
}

export function getBuiltinAppIcon(id) {
  const candidates = getAppIdCandidates(id);
  for (let i = 0; i < candidates.length; i += 1) {
    if (BUILTIN_APP_ICONS[candidates[i]]) return BUILTIN_APP_ICONS[candidates[i]];
  }
  if (id && /terminal/i.test(id)) return 'assets/app-icons/terminal.png';
  return '';
}

export function getBuiltinAppTitle(id) {
  const candidates = getAppIdCandidates(id);
  for (let i = 0; i < candidates.length; i += 1) {
    if (BUILTIN_APP_TITLES[candidates[i]]) return BUILTIN_APP_TITLES[candidates[i]];
  }
  if (id && /terminal/i.test(id)) return 'Terminal';
  return '';
}

/** True when an app id looks like a terminal / shell app. */
export function isTerminalAppId(id) {
  return !!id && /terminal/i.test(id);
}

/** Curated apps we ship icons for, so they can always be added from settings. */
export const KNOWN_BUILTIN_APPS = [
  'com.apple.appletv',
  'netflix',
  'amazon.html',
  'youtube.leanback.v4',
  'com.webos.app.lgchannels',
  'bbc.iplayer.lge',
  'tv.wuaki',
  'com.zattoo.itvx',
  'com.channel4.vod',
  'com.channel5.my5',
  'com.disney.disneyplus',
  'com.webos.app.browser',
  'com.webos.app.mediadiscovery',
  'com.webos.app.settings'
];

/** Bundled icon assets a user can pick from when adding a custom app tile. */
export const BUILTIN_ICON_CHOICES = [
  {value: 'assets/app-icons/netflix.png', label: 'Netflix'},
  {value: 'assets/app-icons/prime-video.png', label: 'Prime Video'},
  {value: 'assets/app-icons/youtube.png', label: 'YouTube'},
  {value: 'assets/app-icons/apple-tv.png', label: 'Apple TV'},
  {value: 'assets/app-icons/disney-plus.png', label: 'Disney+'},
  {value: 'assets/app-icons/bbc-iplayer.png', label: 'BBC iPlayer'},
  {value: 'assets/app-icons/itvx.png', label: 'ITVX'},
  {value: 'assets/app-icons/channel4.png', label: 'Channel 4'},
  {value: 'assets/app-icons/channel5.png', label: 'Channel 5'},
  {value: 'assets/app-icons/rakuten.png', label: 'Rakuten TV'},
  {value: 'assets/app-icons/lg-channels.png', label: 'LG Channels'},
  {value: 'assets/app-icons/browser.png', label: 'Web Browser'},
  {value: 'assets/app-icons/media-player.png', label: 'Media Player'},
  {value: 'assets/app-icons/tv-settings.png', label: 'TV Settings'},
  {value: 'assets/app-icons/homebrew.png', label: 'Homebrew'},
  {value: 'assets/app-icons/terminal.png', label: 'Terminal'}
];