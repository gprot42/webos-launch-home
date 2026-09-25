/**
 * What Launch Home did in the background, for Settings -> TV check: main.js
 * counts, the check reads how much changed while TV Settings was open.
 */
export const activity = {
  // Something launched us again (webOSRelaunch), e.g. the Home watcher.
  relaunches: 0,
  // We brought ourselves back because the foreground check saw the stock home.
  returns: 0,
  // Stuck-app recovery closed another app.
  closes: 0,
  // We took the remote's focus back.
  reclaims: 0,
  // Latest app ids the foreground check saw, oldest first, and how many were
  // ever added (so a reader can tell which are new).
  foreground: [],
  foregroundSeq: 0
};

export function noteForeground(appId) {
  const id = appId || '(none)';
  const list = activity.foreground;
  if (list[list.length - 1] === id) return;
  list.push(id);
  activity.foregroundSeq += 1;
  if (list.length > 12) list.shift();
}
