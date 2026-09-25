/**
 * Settings -> Add an app -> Add all apps from LG's home screen: which of
 * LG's launch points (luna.js listLaunchPoints, in LG's order) to add to the
 * home row. Everything else stays as it is: nothing is removed or reordered.
 */

import {getAppIdCandidates} from './app-icons.js';

// Not for the dock: Launch Home itself (and its voice card), inputs and Live TV
// (Launch Home's inputs row has those), and TV Settings (it has its own tile).
const NOT_FOR_DOCK = /^(org\.webosbrew\.lounge\.|com\.webos\.app\.(hdmi|externalinput|livetv$|inputcommon$|component|scart|av\d*$|rgb)|com\.palm\.app\.settings$|com\.webos\.app\.settings$)/;

/**
 * App ids from `points` to add, in LG's order: shown apps (not hidden, and not
 * bookmarks such as a named HDMI input) that aren't pinned yet (`pinned`, any
 * alias id counts), aren't a custom app's launch id (`customApps`) and belong
 * in the dock.
 */
export function lgHomeAppsToAdd(points, pinned, customApps) {
  const taken = {};
  (pinned || []).forEach(function (id) {
    getAppIdCandidates(id).forEach(function (alias) { taken[alias] = true; });
  });
  (customApps || []).forEach(function (entry) {
    if (entry && entry.launchId) taken[entry.launchId] = true;
    if (entry && entry.id) taken[entry.id] = true;
  });
  const out = [];
  (points || []).forEach(function (point) {
    const id = point && typeof point.id === 'string' ? point.id : '';
    if (!id || point.hidden === true || point.visible === false || point.lptype === 'bookmark') return;
    if (NOT_FOR_DOCK.test(id)) return;
    if (getAppIdCandidates(id).some(function (alias) { return taken[alias]; })) return;
    getAppIdCandidates(id).forEach(function (alias) { taken[alias] = true; });
    out.push(id);
  });
  return out;
}
