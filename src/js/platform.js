import {lunaRequest} from './luna.js';

let cachedSdkVersion = null;

export function parseSdkVersion(raw) {
  if (!raw) return 0;
  const match = String(raw).match(/(\d+(?:\.\d+)?)/);
  return match ? parseFloat(match[1]) : 0;
}

export async function getSdkVersion() {
  if (cachedSdkVersion !== null) return cachedSdkVersion;

  if (window.webOS && typeof window.webOS.deviceInfo === 'function') {
    cachedSdkVersion = await new Promise(function (resolve) {
      window.webOS.deviceInfo(function (info) {
        resolve(parseSdkVersion(info && info.sdkVersion));
      });
    });
    return cachedSdkVersion;
  }

  try {
    const res = await lunaRequest('luna://com.webos.service.tv.systemproperty', {
      method: 'getSystemInfo',
      parameters: {keys: ['sdkVersion']}
    });
    cachedSdkVersion = parseSdkVersion(res.sdkVersion);
  } catch (err) {
    cachedSdkVersion = 0;
  }

  return cachedSdkVersion;
}

/** webOS 6.x TVs (sdk 6) and later year-branded releases (sdk 9+). */
export function isModernWebOS(sdkVersion) {
  return sdkVersion >= 6;
}

// Keys webOS has used to report panel HDR support; any truthy one counts.
const HDR_KEYS = ['hdr', 'hdr10', 'hdrMode', 'dolbyVision', 'supportsHDR'];

/**
 * Best-effort panel HDR check via the TV's system properties. This is the
 * fallback for `matchMedia('(dynamic-range: high)')`, which older webOS does
 * not implement. Kept conservative: no answer means "not HDR", so playback
 * falls back to 4K SDR / 1080p rather than forcing HDR onto an SDR panel.
 */
export async function getHdrSupport() {
  try {
    const res = await lunaRequest('luna://com.webos.service.tv.systemproperty', {
      method: 'getSystemInfo',
      parameters: {keys: HDR_KEYS}
    });
    if (!res) return false;
    return HDR_KEYS.some(function (key) {
      const value = res[key];
      return value === true || value === 'true' || value === '1' ||
        value === 'HDR' || value === 'hdr' || value === 'HDR10';
    });
  } catch (err) {
    return false;
  }
}