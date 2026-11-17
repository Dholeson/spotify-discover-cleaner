// Settings (#/settings, #/settings?section=backlog|accounts|history|connection|advanced|extras).
//
// One scrolling page of sections with a sub-nav (left on desktop, chips on narrow screens) that
// follows the scroll position. Each section lives in components/settings-<name>.js and exposes
// {id, label, icon, title, desc, mount(el), onStatus?(status, changed), destroy()}; this file
// lays them out, deep-links to them, forwards status updates and keeps relative times fresh.
import { html, mount, icon, pageHeader, sectionHeader, reportError } from '../core/ui.js';
import { refreshRelTimes } from '../components/settings-ui.js';
import { createBacklogSection } from '../components/settings-backlog.js';
import { createAccountsSection } from '../components/settings-accounts.js';
import { createDataSection } from '../components/settings-data.js';
import { createConnectionSection } from '../components/settings-connection.js';
import { createAdvancedSection } from '../components/settings-advanced.js';
import { createExtrasSection } from '../components/settings-extras.js';

// Friendly aliases so other pages can link to what they mean (e.g. "Settings → History").
const ALIASES = {
  clearing: 'backlog', playlist: 'backlog', 'auto-clear': 'backlog',
  spotify: 'accounts', lastfm: 'accounts', account: 'accounts',
  data: 'history', sync: 'history', import: 'history',
  status: 'connection', diagnostics: 'connection', health: 'connection',
  env: 'advanced',
};

let root = null;
let sections = [];
let router = null;
let relTimer = null;
let spyRaf = 0;
let userMoved = false;
let settleTimers = [];
let cleanup = [];

const viewEl = () => document.getElementById('view');
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function resolveSection(name) {
  const n = String(name || '').toLowerCase();
  const id = ALIASES[n] || n;
  return sections.some((s) => s.id === id) ? id : null;
}

function setCurrent(id) {
  root?.querySelectorAll('.st-nav-link').forEach((a) => {
    if (a.dataset.section === id) a.setAttribute('aria-current', 'true');
    else a.removeAttribute('aria-current');
  });
}

/** Highlight the section being read: the last one whose top passed the upper third. */
function spy() {
  spyRaf = 0;
  const v = viewEl();
  if (!root || !v) return;
  const top = v.getBoundingClientRect().top;
  const els = [...root.querySelectorAll('.st-section')];
  let current = els[0]?.dataset.section;
  if (v.scrollTop + v.clientHeight >= v.scrollHeight - 4) current = els[els.length - 1]?.dataset.section;
  else {
    for (const s of els) {
      if (s.getBoundingClientRect().top - top <= v.clientHeight * 0.35) current = s.dataset.section;
    }
  }
  setCurrent(current);
}

function scrollToSection(id, { smooth = true, focus = true } = {}) {
  const v = viewEl();
  const sec = root?.querySelector(`#st-${id}`);
  if (!v || !sec) return;
  const y = v.scrollTop + sec.getBoundingClientRect().top - v.getBoundingClientRect().top - 16;
  v.scrollTo({ top: Math.max(0, y), behavior: smooth && !reducedMotion() ? 'smooth' : 'auto' });
  setCurrent(id);
  if (focus) sec.querySelector('h2')?.focus({ preventScroll: true });
}

/** Deep link on first load: data above the target loads async and pushes it down, so re-align a
 *  couple of times unless the user has started scrolling themselves. */
function settleOn(id, section) {
  settleTimers.forEach(clearTimeout);
  userMoved = false;
  const go = (focus) => { if (!userMoved && root) scrollToSection(id, { smooth: false, focus }); };
  requestAnimationFrame(() => go(true));
  settleTimers = [350, 900, 1800].map((ms) => setTimeout(() => go(false), ms));
  if (id === 'connection' && section === 'diagnostics') sections.find((s) => s.id === 'connection')?.openDiagnostics?.();
}

function setDirty(id, on) {
  const dot = root?.querySelector(`.st-nav-link[data-section="${id}"] .st-nav-dot`);
  if (dot) dot.hidden = !on;
}

export default {
  id: 'settings',
  title: 'Settings',

  mount(el, params, ctx) {
    root = el;
    router = ctx.router;
    const env = { ctx, setDirty };
    sections = [
      createBacklogSection(env),
      createAccountsSection(env),
      createDataSection(env),
      createConnectionSection(env),
      createAdvancedSection(env),
      createExtrasSection(env),
    ];
    mount(root, html`
      ${pageHeader({ title: 'Settings', desc: 'Your backlog playlist and how clearing works, your connected accounts, and your listening data.' })}
      <div class="st-layout">
        <nav class="st-nav" aria-label="Settings sections">
          <ul class="st-nav-list" role="list">
            ${sections.map((s) => html`<li><a class="st-nav-link" href="#/settings?section=${s.id}" data-section="${s.id}">
              ${icon(s.icon)}<span class="st-nav-label">${s.label}</span><span class="st-nav-dot" hidden title="Unsaved changes"><span class="sr-only">(unsaved changes)</span></span></a></li>`)}
          </ul>
        </nav>
        <div class="st-content">
          ${sections.map((s) => html`<section class="st-section" id="st-${s.id}" data-section="${s.id}" aria-labelledby="st-${s.id}-h">
            ${sectionHeader({ title: s.title, desc: s.desc, id: `st-${s.id}-h` })}
            <div class="st-section-body" data-body="${s.id}"></div>
          </section>`)}
        </div>
      </div>`);
    root.querySelectorAll('.st-section h2').forEach((h) => h.setAttribute('tabindex', '-1'));

    for (const s of sections) {
      try { s.mount(root.querySelector(`[data-body="${s.id}"]`)); } catch (err) { reportError(err); }
    }

    // Sub-nav: plain links (middle-click and copy-link work); a normal click scrolls smoothly and
    // records the section in the URL without adding history entries.
    root.querySelector('.st-nav').addEventListener('click', (ev) => {
      const a = ev.target instanceof Element ? ev.target.closest('.st-nav-link') : null;
      if (!a || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button !== 0) return;
      ev.preventDefault();
      userMoved = true;
      scrollToSection(a.dataset.section);
      router.setQuery({ section: a.dataset.section }, { replace: true });
    });

    const v = viewEl();
    const onScroll = () => { if (!spyRaf) spyRaf = requestAnimationFrame(spy); };
    const onUser = () => { userMoved = true; };
    v?.addEventListener('scroll', onScroll, { passive: true });
    ['wheel', 'touchstart', 'keydown', 'pointerdown'].forEach((t) => v?.addEventListener(t, onUser, { passive: true }));
    cleanup.push(() => {
      v?.removeEventListener('scroll', onScroll);
      ['wheel', 'touchstart', 'keydown', 'pointerdown'].forEach((t) => v?.removeEventListener(t, onUser));
    });

    relTimer = setInterval(() => refreshRelTimes(root), 30000);

    const target = resolveSection(params.section);
    if (target) settleOn(target, String(params.section || '').toLowerCase());
    else requestAnimationFrame(spy);
  },

  update(params) {
    const target = resolveSection(params.section);
    if (!target) return;
    const link = root?.querySelector(`.st-nav-link[aria-current="true"]`);
    // The sub-nav already scrolled there; only react to links from elsewhere.
    if (link?.dataset.section === target && userMoved) return;
    scrollToSection(target);
    if (String(params.section).toLowerCase() === 'diagnostics') sections.find((s) => s.id === 'connection')?.openDiagnostics?.();
  },

  onStatus(status, info = {}) {
    for (const s of sections) {
      if (typeof s.onStatus !== 'function') continue;
      try { s.onStatus(status, info.initial ? null : info.changed); } catch (err) { reportError(err); }
    }
  },

  unmount() {
    settleTimers.forEach(clearTimeout);
    settleTimers = [];
    clearInterval(relTimer);
    if (spyRaf) cancelAnimationFrame(spyRaf);
    spyRaf = 0;
    cleanup.forEach((f) => { try { f(); } catch (err) { reportError(err); } });
    cleanup = [];
    for (const s of sections) {
      try { s.destroy?.(); } catch (err) { reportError(err); }
    }
    sections = [];
    root = null;
  },
};
