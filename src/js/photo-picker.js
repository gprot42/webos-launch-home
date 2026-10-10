/**
 * Poster-tile picker used by the Background settings galleries.
 *
 * Renders a grid of focusable tiles — `.photo-picker-grid` / `.photo-picker-tile`,
 * the classes the settings focus manager navigates by — each with a thumbnail
 * that walks a fallback chain, a caption and an optional marker. Selection is
 * single (radio-like) or multi (checkbox-like); picks are reported through
 * `onChange`. The built-in photo gallery, the online photo gallery and the
 * aerial-video gallery all build on it.
 *
 * @param {{
 *   tiles: Array<{
 *     id: string,
 *     title?: string,
 *     thumb?: string|string[],
 *     marker?: string,
 *     custom?: boolean,
 *     hint?: string,
 *     onActivate?: (event?: Event) => void
 *   }>,
 *   mode?: 'single'|'multi',
 *   selected?: string[],
 *   focusIndexBase?: number,
 *   onChange?: (ids: string[], id: string, event?: Event) => void
 * }} opts
 */
export function createPosterTilePicker(opts) {
  const options = opts || {};
  const multi = options.mode === 'multi';
  const grid = document.createElement('div');
  grid.className = 'photo-picker-grid';

  const tiles = [];
  let selected = (options.selected || []).map(String);

  function isSelected(id) {
    return selected.indexOf(String(id)) >= 0;
  }

  function applySelection() {
    tiles.forEach(function (tile) {
      const on = isSelected(tile.id);
      tile.el.classList.toggle('is-selected', on);
      tile.el.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  function pick(id, event) {
    const key = String(id);
    if (multi) {
      const at = selected.indexOf(key);
      if (at >= 0) selected.splice(at, 1);
      else selected.push(key);
    } else {
      selected = [key];
    }
    applySelection();
    if (typeof options.onChange === 'function') {
      options.onChange(selected.slice(), key, event);
    }
  }

  function buildThumb(entry, tile) {
    const img = document.createElement('img');
    img.className = 'photo-picker-thumb';
    img.alt = '';
    img.loading = 'lazy';
    img.decoding = 'async';
    img.draggable = false;
    img.style.pointerEvents = 'none';
    const sources = (Array.isArray(entry.thumb) ? entry.thumb : [entry.thumb]).filter(Boolean);
    let at = 0;
    if (sources.length) img.src = sources[0];
    img.addEventListener('error', function () {
      at += 1;
      if (at >= sources.length) {
        tile.classList.add('photo-picker-thumb-failed');
        return;
      }
      img.src = sources[at];
    });
    return img;
  }

  function buildCaption(text) {
    const caption = document.createElement('span');
    caption.className = 'photo-picker-caption';
    caption.textContent = text || '';
    caption.style.pointerEvents = 'none';
    return caption;
  }

  (options.tiles || []).forEach(function (entry, index) {
    // div+role=button: native <button> on webOS often eats Select/OK and
    // leaves focus stuck so you cannot scroll to the next settings rows.
    const el = document.createElement('div');
    el.className = 'photo-picker-tile focusable';
    if (entry.custom) el.classList.add('photo-picker-custom');
    el.setAttribute('role', 'button');
    // 905… after Display (904), before USB filename (930); settings focus does
    // not sort by this, but keep it distinct from the home dock indices.
    el.dataset.focusIndex = String((options.focusIndexBase || 905) + index);
    el.setAttribute('aria-label', entry.title || entry.id || 'Custom URL');
    el.tabIndex = 0;

    if (entry.custom) {
      el.appendChild(buildCaption(entry.title || 'Custom URL'));
      if (entry.hint) {
        const hint = document.createElement('span');
        hint.className = 'photo-picker-custom-hint';
        hint.textContent = entry.hint;
        hint.style.pointerEvents = 'none';
        el.appendChild(hint);
      }
    } else {
      el.appendChild(buildThumb(entry, el));
      el.appendChild(buildCaption(entry.title || entry.id));
    }

    if (entry.marker) {
      const marker = document.createElement('span');
      marker.className = 'photo-picker-marker';
      marker.textContent = entry.marker;
      marker.style.pointerEvents = 'none';
      el.appendChild(marker);
    }

    // Do NOT select on focus — arrows only browse. Select/OK/click chooses so
    // the gold “is-selected” state stays when the remote scrolls away.
    el.addEventListener('click', function (event) {
      if (event && event.preventDefault) event.preventDefault();
      pick(entry.id, event);
      if (typeof entry.onActivate === 'function') entry.onActivate(event);
    });

    grid.appendChild(el);
    tiles.push({id: entry.id, el: el});
  });

  applySelection();

  return {
    el: grid,
    /** Replace the selection (used when another control changes it). */
    mark: function (ids) {
      selected = (ids || []).map(String);
      applySelection();
    },
    getSelected: function () {
      return selected.slice();
    }
  };
}
