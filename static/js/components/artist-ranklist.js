// Ranked list with inline bars (Last.fm library style) where every row can carry its own
// action: rank · lead (play button or small art) · title + detail · bar with the value at its
// tip · optional trailing action. Used by the artist page for "Your top tracks" / "Your top albums".
//
// Why not charts.barList(): a barList row is one link or one button, and track rows need a play
// button next to the name. Values are printed next to every bar, so nothing is tooltip-only and
// the list reads as plain text for screen readers (bars are aria-hidden).
//
//   rankList(items, { max, valueLabel: 'plays', label: 'Your top tracks by …' })
//   items: [{ key, rank, title, sub, value, lead (html``), trailing (html``), playing }]
import { html, fmt } from '../core/ui.js';

export function rankList(items = [], { max, valueLabel = 'plays', valueOne, label, className = '' } = {}) {
  const top = Number(max) > 0 ? Number(max) : Math.max(0, ...items.map((it) => Number(it.value) || 0));
  const one = valueOne || valueLabel.replace(/s$/, '');
  return html`<ol class="rk${className ? ' ' + className : ''}" ${label ? html`aria-label="${label}"` : ''}>
    ${items.map((it, i) => {
      const v = Number(it.value) || 0;
      const pct = top > 0 ? Math.max(0, Math.min(1, v / top)) : 0;
      return html`<li class="rk-row${it.playing ? ' is-playing' : ''}" data-key="${it.key ?? i}">
        <span class="rk-rank" aria-hidden="true">${it.rank ?? i + 1}</span>
        <span class="rk-lead">${it.lead || ''}</span>
        <span class="rk-main">
          <span class="rk-text">
            <span class="rk-title" title="${it.title}"><span class="sr-only">${it.rank ?? i + 1}. </span>${it.title}</span>
            ${it.sub ? html`<span class="rk-sub">${it.sub}</span>` : ''}
          </span>
          <span class="rk-barline">
            <span class="rk-bar" style="--pct:${pct.toFixed(4)}" aria-hidden="true"></span>
            <span class="rk-val">${fmt.num(v)}<span class="sr-only"> ${v === 1 ? one : valueLabel}</span></span>
          </span>
        </span>
        ${it.trailing ? html`<span class="rk-trail">${it.trailing}</span>` : ''}
      </li>`;
    })}
  </ol>`;
}

export default rankList;
