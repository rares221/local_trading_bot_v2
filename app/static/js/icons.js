/**
 * Lucide icon subset — trading bot UI
 * Stroke-based SVGs, 24×24 viewBox. No external dependency.
 *
 * Usage (static HTML):  <span data-icon="zap"></span>
 * Usage (JS strings):   icon('zap')  or  icon('zap', 14)
 */
(function () {
  var P = {
    /* ── Navigation ── */
    'layout-dashboard':
      '<rect width="7" height="9" x="3" y="3" rx="1"/>' +
      '<rect width="7" height="5" x="3" y="15" rx="1"/>' +
      '<rect width="7" height="5" x="14" y="3" rx="1"/>' +
      '<rect width="7" height="9" x="14" y="13" rx="1"/>',

    'list-checks':
      '<path d="m3 17 2 2 4-4"/>' +
      '<path d="m3 7 2 2 4-4"/>' +
      '<path d="M13 6h8"/>' +
      '<path d="M13 12h8"/>' +
      '<path d="M13 18h8"/>',

    'search':
      '<circle cx="11" cy="11" r="8"/>' +
      '<path d="m21 21-4.3-4.3"/>',

    'bar-chart-2':
      '<path d="M18 20V10"/>' +
      '<path d="M12 20V4"/>' +
      '<path d="M6 20v-6"/>',

    'activity':
      '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',

    'book-open':
      '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>' +
      '<path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',

    'newspaper':
      '<path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/>' +
      '<path d="M18 14h-8"/>' +
      '<path d="M15 18h-5"/>' +
      '<path d="M10 6h8v4h-8V6Z"/>',

    /* ── Pipeline / actions ── */
    'zap':
      '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',

    'refresh-cw':
      '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>' +
      '<path d="M21 3v5h-5"/>' +
      '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>' +
      '<path d="M8 16H3v5"/>',

    'eraser':
      '<path d="m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21"/>' +
      '<path d="M22 21H7"/>' +
      '<path d="m5 11 9 9"/>',

    'trash-2':
      '<path d="M3 6h18"/>' +
      '<path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>' +
      '<path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>' +
      '<line x1="10" x2="10" y1="11" y2="17"/>' +
      '<line x1="14" x2="14" y1="11" y2="17"/>',

    'save':
      '<path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/>' +
      '<path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/>' +
      '<path d="M7 3v4a1 1 0 0 0 1 1h7"/>',

    'sliders-horizontal':
      '<line x1="21" x2="14" y1="4" y2="4"/>' +
      '<line x1="10" x2="3" y1="4" y2="4"/>' +
      '<line x1="21" x2="12" y1="12" y2="12"/>' +
      '<line x1="8" x2="3" y1="12" y2="12"/>' +
      '<line x1="21" x2="16" y1="20" y2="20"/>' +
      '<line x1="12" x2="3" y1="20" y2="20"/>' +
      '<line x1="14" x2="14" y1="2" y2="6"/>' +
      '<line x1="8" x2="8" y1="10" y2="14"/>' +
      '<line x1="16" x2="16" y1="18" y2="22"/>',

    /* ── Row / table actions ── */
    'check':
      '<path d="M20 6 9 17l-5-5"/>',

    'x':
      '<path d="M18 6 6 18"/>' +
      '<path d="m6 6 12 12"/>',

    'square-x':
      '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/>' +
      '<path d="m15 9-6 6"/>' +
      '<path d="m9 9 6 6"/>',

    'history':
      '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>' +
      '<path d="M3 3v5h5"/>' +
      '<path d="M12 7v5l4 2"/>',

    /* ── Research ── */
    'sparkles':
      '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>' +
      '<path d="M20 3v4"/>' +
      '<path d="M22 5h-4"/>' +
      '<path d="M4 17v2"/>' +
      '<path d="M5 18H3"/>',

    /* ── Settings ── */
    'gauge':
      '<path d="m12 14 4-4"/>' +
      '<path d="M3.34 19a10 10 0 1 1 17.32 0"/>',

    'shield':
      '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',

    'server':
      '<rect width="20" height="8" x="2" y="2" rx="2" ry="2"/>' +
      '<rect width="20" height="8" x="2" y="14" rx="2" ry="2"/>' +
      '<line x1="6" x2="6.01" y1="6" y2="6"/>' +
      '<line x1="6" x2="6.01" y1="18" y2="18"/>',

    'bot':
      '<path d="M12 8V4H8"/>' +
      '<rect width="16" height="12" x="4" y="8" rx="2"/>' +
      '<path d="M2 14h2"/>' +
      '<path d="M20 14h2"/>' +
      '<path d="M15 13v2"/>' +
      '<path d="M9 13v2"/>',

    'cpu':
      '<rect width="16" height="16" x="4" y="4" rx="2"/>' +
      '<rect width="6" height="6" x="9" y="9" rx="1"/>' +
      '<path d="M15 2v2"/><path d="M15 20v2"/>' +
      '<path d="M2 15h2"/><path d="M2 9h2"/>' +
      '<path d="M20 15h2"/><path d="M20 9h2"/>' +
      '<path d="M9 2v2"/><path d="M9 20v2"/>',

    /* ── Misc ── */
    'play-circle':
      '<circle cx="12" cy="12" r="10"/>' +
      '<polygon points="10 8 16 12 10 16 10 8"/>',

    'filter-x':
      '<path d="M13.013 3H2l8 9.46V19l4 2v-8.54l.9-1.055"/>' +
      '<path d="m22 3-5 5"/>' +
      '<path d="m17 3 5 5"/>',

    'file-text':
      '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/>' +
      '<path d="M14 2v4a2 2 0 0 0 2 2h4"/>' +
      '<path d="M10 9H8"/>' +
      '<path d="M16 13H8"/>' +
      '<path d="M16 17H8"/>',

    'info':
      '<circle cx="12" cy="12" r="10"/>' +
      '<path d="M12 16v-4"/>' +
      '<path d="M12 8h.01"/>',

    'map':
      '<path d="M14.106 5.553a2 2 0 0 0 1.788 0l3.659-1.83A1 1 0 0 1 21 4.619v12.764a1 1 0 0 1-.553.894l-4.553 2.277a2 2 0 0 1-1.788 0l-4.212-2.106a2 2 0 0 0-1.788 0l-3.659 1.83A1 1 0 0 1 3 19.381V6.618a1 1 0 0 1 .553-.894l4.553-2.277a2 2 0 0 1 1.788 0z"/>' +
      '<path d="M15 5.764v15"/>' +
      '<path d="M9 3.236v15"/>',

    'rss':
      '<path d="M4 11a9 9 0 0 1 9 9"/>' +
      '<path d="M4 4a16 16 0 0 1 16 16"/>' +
      '<circle cx="5" cy="19" r="1"/>',

    'calendar':
      '<path d="M8 2v4"/>' +
      '<path d="M16 2v4"/>' +
      '<rect width="18" height="18" x="3" y="4" rx="2"/>' +
      '<path d="M3 10h18"/>',

    'alert-triangle':
      '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/>' +
      '<path d="M12 9v4"/>' +
      '<path d="M12 17h.01"/>',
  };

  /** Return an SVG string for the given icon name. */
  function icon(name, size) {
    size = size || 15;
    var paths = P[name] || '';
    return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" ' +
           'fill="none" stroke="currentColor" stroke-width="2" ' +
           'stroke-linecap="round" stroke-linejoin="round" ' +
           'class="btn-icon" aria-hidden="true">' + paths + '</svg>';
  }

  /** Hydrate all <span data-icon="name"> placeholders in the document. */
  function hydrateIcons() {
    document.querySelectorAll('[data-icon]').forEach(function (el) {
      var name = el.getAttribute('data-icon');
      var size = parseInt(el.getAttribute('data-icon-size') || '15', 10);
      el.innerHTML = icon(name, size);
    });
  }

  window.icon = icon;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', hydrateIcons);
  } else {
    hydrateIcons();
  }
})();
