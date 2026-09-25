/**
 * Settings -> Backup & restore: the backup file's contents, and reading back
 * the newest one found. luna.js writes and reads the files (as root).
 */

import {APP_VERSION} from './version.js';

// When and where the last backup went, shown under the buttons (a small note
// of its own, not part of the settings).
export const LAST_BACKUP_KEY = 'lounge.backup.last';

export function backupWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return 'an unknown date';
  return d.toLocaleString([], {day: 'numeric', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit'});
}

export function lastBackupNote() {
  try {
    const last = JSON.parse(localStorage.getItem(LAST_BACKUP_KEY) || 'null');
    if (last && last.savedAt) return 'Last backup: ' + backupWhen(last.savedAt) + ' (' + last.where + ').';
  } catch (err) {
    // No note yet.
  }
  return 'No backup made from this TV yet.';
}

/**
 * The backup file: the saved settings plus who wrote them and when. ASCII only
 * (other characters as \u escapes) so it can go through the shell in pieces.
 */
export function settingsBackupText(config) {
  const text = JSON.stringify({
    app: 'org.webosbrew.lounge.launcher',
    kind: 'launch-home-settings',
    version: APP_VERSION,
    savedAt: new Date().toISOString(),
    config: config
  }, null, 1);
  return text.replace(/[\u007f-\uffff]/g, function (c) {
    return '\\u' + ('000' + c.charCodeAt(0).toString(16)).slice(-4);
  });
}

/** The newest valid backup among those found, as {data, where, time}, or null. */
export function newestBackup(found) {
  let best = null;
  (found || []).forEach(function (entry) {
    let data = null;
    try {
      data = JSON.parse(entry.text);
    } catch (err) {
      return;
    }
    if (!data || data.app !== 'org.webosbrew.lounge.launcher' || !data.config) return;
    const time = Date.parse(data.savedAt) || 0;
    if (!best || time > best.time) best = {data: data, where: entry.where, time: time};
  });
  return best;
}
