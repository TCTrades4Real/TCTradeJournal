(function () {
  'use strict';

  const COLLAPSED_W = 56;
  const EXPANDED_W  = 200;

  const page = (location.pathname.split('/').pop() || 'index.html').split('?')[0];
  let open = localStorage.getItem('tc_nav_open') !== 'false';

  const w = () => open ? EXPANDED_W : COLLAPSED_W;

  // Center content within the viewport space to the right of the nav.
  // Uses the most common page max-width (1200px) as the reference.
  const MAX_W = 1200;
  function bodyMargins(navW) {
    return {
      left:  `max(${navW}px, calc((100vw + ${navW}px - ${MAX_W}px) / 2))`,
      right: `max(0px, calc((100vw - ${navW}px - ${MAX_W}px) / 2))`,
    };
  }

  /* ── Icons ─────────────────────────────────────────── */
  const I = {
    home: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9.5L12 3l9 6.5V20a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9.5z"/><path d="M9 21V12h6v9"/></svg>`,
    month: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>`,
    day:   `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/></svg>`,
    candle:`<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="2" x2="18" y2="6"/><line x1="18" y1="14" x2="18" y2="22"/><rect x="15" y="6" width="6" height="8" rx="1"/><line x1="6" y1="2" x2="6" y2="8"/><rect x="3" y="8" width="6" height="8" rx="1"/><line x1="6" y1="16" x2="6" y2="22"/></svg>`,
    report:`<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="2" y1="20" x2="22" y2="20"/></svg>`,
    left:  `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>`,
    right: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>`,
  };

  const ITEMS = [
    { href: 'index.html',       label: 'Dashboard',   icon: I.home   },
    { href: 'month.html',       label: 'Month',        icon: I.month  },
    { href: 'day.html',         label: 'Day',           icon: I.day    },
    { href: 'reports.html',     label: 'Reports',       icon: I.report },
  ];

  /* ── Styles ─────────────────────────────────────────── */
  const style = document.createElement('style');
  style.textContent = `
    body {
      margin-left:  ${bodyMargins(w()).left}  !important;
      margin-right: ${bodyMargins(w()).right} !important;
      transition: margin-left 0.2s ease, margin-right 0.2s ease !important;
    }

    #_nav {
      position: fixed;
      top: 0; left: 0; bottom: 0;
      width: ${EXPANDED_W}px;
      background: #0d0d0d;
      border-right: 1px solid #1c1c1c;
      z-index: 9000;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      transition: width 0.2s ease;
    }
    #_nav._c { width: ${COLLAPSED_W}px; }

    #_nav-top {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      padding: 12px 10px;
      flex-shrink: 0;
      min-height: 52px;
    }
    #_nav._c #_nav-top { justify-content: center; }

    #_nav-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 32px; height: 32px;
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 8px;
      cursor: pointer;
      color: #6b7280;
      flex-shrink: 0;
      transition: background 0.15s, color 0.15s, border-color 0.15s;
    }
    #_nav-btn:hover { background: #222; color: #e0e0e0; border-color: #3a3a3a; }

    #_nav-list {
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 4px 8px;
      overflow: hidden;
    }

    ._nl {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 9px 12px;
      border-radius: 8px;
      color: #6b7280;
      text-decoration: none;
      white-space: nowrap;
      overflow: hidden;
      font-size: 13.5px;
      font-weight: 400;
      transition: background 0.15s, color 0.15s;
    }
    ._nl:hover { background: #161616; color: #d1d5db; }
    ._nl._a  { background: #161616; color: #f9fafb; }
    ._nl._a ._ni svg { stroke: #22c55e; }

    ._ni { flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 18px; }
    ._nt { overflow: hidden; text-overflow: ellipsis; opacity: 1; transition: opacity 0.15s; }

    #_nav._c ._nl { padding: 9px; justify-content: center; gap: 0; }
    #_nav._c ._nt { opacity: 0; width: 0; pointer-events: none; }
  `;
  document.head.appendChild(style);

  /* ── Build nav ──────────────────────────────────────── */
  const nav = document.createElement('nav');
  nav.id = '_nav';
  if (!open) nav.classList.add('_c');

  const items = ITEMS.map(item => {
    const active = page === item.href;
    return `<a href="${item.href}" class="_nl${active ? ' _a' : ''}">
      <span class="_ni">${item.icon}</span>
      <span class="_nt">${item.label}</span>
    </a>`;
  }).join('');

  nav.innerHTML = `
    <div id="_nav-top">
      <button id="_nav-btn" title="Toggle navigation">${open ? I.left : I.right}</button>
    </div>
    <div id="_nav-list">${items}</div>
  `;

  document.body.insertBefore(nav, document.body.firstChild);

  /* ── Toggle ─────────────────────────────────────────── */
  document.getElementById('_nav-btn').addEventListener('click', () => {
    open = !open;
    localStorage.setItem('tc_nav_open', open);
    nav.classList.toggle('_c', !open);
    const m = bodyMargins(w());
    document.body.style.marginLeft  = m.left;
    document.body.style.marginRight = m.right;
    document.getElementById('_nav-btn').innerHTML = open ? I.left : I.right;
  });
})();
