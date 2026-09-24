export const PROFILE_OPTIONS = [
  {id: 'default', label: 'Default'},
  {id: 'night', label: 'Night'},
  {id: 'cinema', label: 'Cinema'}
];

export const PROFILE_PRESETS = {
  night: {
    // Keeps your wallpaper, just under a darker scrim. It used to force the
    // midnight gradient, so photos picked in Settings "reset" to Gradient.
    background: {
      overlayOpacity: 0.6
    },
    music: {
      volume: 0.08
    }
  },
  cinema: {
    background: {
      source: 'preset',
      mode: 'static',
      preset: 'midnight',
      overlayOpacity: 0.55
    },
    music: {
      enabled: false
    }
  }
};

function deepMerge(target, source) {
  const out = Object.assign({}, target);
  if (!source || typeof source !== 'object') return out;

  for (const key of Object.keys(source)) {
    const value = source[key];
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      out[key] = deepMerge(out[key] || {}, value);
    } else {
      out[key] = value;
    }
  }
  return out;
}

export function getProfileOverrides(config) {
  const profileId = (config && config.profile) || 'default';
  if (profileId === 'default') return {};

  const custom = (config && config.profiles && config.profiles[profileId]) || {};
  const preset = PROFILE_PRESETS[profileId] || {};
  return deepMerge(preset, custom);
}

export function applyActiveProfile(config) {
  const base = deepMerge({}, config);
  const overrides = getProfileOverrides(config);
  return deepMerge(base, overrides);
}