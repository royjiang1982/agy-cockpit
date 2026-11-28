(() => {
  'use strict';

  const PRIMARY_ENGINES = ['agy', 'codex', 'grok'];
  const ENG = {
    agy: { label: 'AGY', brand: 'agy' },
    codex: { label: 'Codex', brand: 'codex' },
    grok: { label: 'Grok', brand: 'grok' },
    droid: { label: 'Droid', brand: 'droid' },
    native: { label: '内置', brand: 'native' },
    swarm: { label: 'Swarm', brand: 'swarm' },
  };
  const EFF = { '': '', low: '低', medium: '中', high: '高', xhigh: '极高', max: 'Max', ultra: 'Ultra', same: '同档' };
  const SKIP_WHY = { quota_low: '配额低', cli_unavailable: '未装', external_disabled: 'CLI已关', swarm_disabled: 'Swarm已关' };
  const ROLES = [['scout', '快搜侦察'], ['standard', '视觉任务'], ['deep', '逻辑任务'], ['expert', '专家验收']];
  const E3 = ['low', 'medium', 'high'];
  const E4 = ['low', 'medium', 'high', 'xhigh'];
  const E4M = ['low', 'medium', 'high', 'max'];
  const E5 = ['low', 'medium', 'high', 'xhigh', 'max'];
  const E6 = [...E5, 'ultra'];

  const fam = (id, label, efforts, re, brand, group) => ({
    id, label, efforts, re, ...(brand ? { brand } : {}), ...(group ? { group } : {}),
  });
  const CATALOG = {
    agy: [
      fam('hydrogen', 'Hydrogen', E4, /^hydrogen$|^gemini-[\d.]+-hydrogen(?:-[a-z]+)?$/, 'agy', 'Gemini'),
      fam('neon', 'Neon', E4, /^neon$|^gemini-[\d.]+-neon(?:-[a-z]+)?$/, 'agy', 'Gemini'),
      fam('argon', 'Argon', E3, /^argon$|^gemini-[\d.]+-argon(?:-[a-z]+)?$/, 'agy', 'Gemini'),
      fam('carbon', 'Carbon', E3, /^carbon$|^gemini-[\d.]+-carbon(?:-[a-z]+)?$/, 'agy', 'Gemini'),
      fam('flash', 'Flash', E3, /^flash$|^gemini-[\d.]+-flash(?:-[a-z]+)?$/, 'agy', 'Gemini'),
      fam('opus', 'Opus', E4M, /^opus$|^claude-opus-[\d.-]+(?:-[a-z]+)?$/, 'claude', 'Claude'),
      fam('sonnet', 'Sonnet', E4M, /^sonnet$|^claude-sonnet-[\d.-]+(?:-[a-z]+)?$/, 'claude', 'Claude'),
    ],
    codex: [
      fam('sol', 'Sol', E6, /^sol$|^gpt-[\d.]+-sol$/),
      fam('astra', 'Astra', E6, /^astra$|^gpt-[\d.]+-astra$/),
      fam('terra', 'Terra', E6, /^terra$|^gpt-[\d.]+-terra$/),
      fam('luna', 'Luna', E5, /^luna$|^gpt-[\d.]+-luna$/),
    ],
    grok: [
      fam('grok', 'Grok', E4, /^grok$|^grok-[\d.]+$/),
      fam('grok-fast', 'Grok Fast', E4, /^grok-fast$|^grok-[\d.]+-(?:build-)?fast$/),
    ],
  };

  const t = (engine, model, effort = '', channel = 'cli', extra = {}) => ({ channel, engine, model, effort, ...extra });
  const DEFAULT_SEAT_POOL = [
    t('agy', 'neon', 'xhigh', 'cli', { enabled: true }),
    t('agy', 'carbon', 'high', 'cli', { enabled: true }),
    t('agy', 'opus', 'high', 'cli', { enabled: true }),
    t('codex', 'astra', 'xhigh', 'cli', { enabled: true }),
    t('grok', 'grok', 'xhigh', 'cli', { enabled: true }),
    t('codex', 'sol', 'xhigh', 'cli', { enabled: true }),
  ];

  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (typeof v === 'boolean' || k === 'value' || k === 'className') n[k] = v;
      else n.setAttribute(k, v);
    }
    n.append(...kids);
    return n;
  };
  const dot = b => el('span', { className: 'ds-dot', 'data-b': b, 'aria-hidden': 'true' });
  const engKeyOf = x => (x.channel === 'invoke_subagent' ? 'native' : x.channel === 'swarm' ? 'swarm' : x.engine);
  const modelsFor = ek => CATALOG[ek] || [];
  const findSpec = (ek, mod) => modelsFor(ek).find(s => s.re.test(String(mod || '').toLowerCase()));
  const effortOf = x => x.effort || (/(?:[-:])(low|medium|high|xhigh|max|ultra)$/i.exec(x.model || '')?.[1]?.toLowerCase() || '');
  const buildTarget = (ek, spec, effort, extra = {}) => {
    const eff = spec.efforts.includes(effort)
      ? effort
      : (spec.efforts.includes('high') ? 'high' : (spec.efforts[spec.efforts.length - 1] || ''));
    return t(ek, spec.id, eff, 'cli', extra);
  };
  const droidFallbackFor = primary => {
    const id = findSpec(primary.engine, primary.model)?.id || String(primary.model || '').toLowerCase();
    return t('droid', id, 'same', 'cli', { enabled: true });
  };
  function findFallbackEntry(p, primary) {
    const ek = primary.engine, spec = findSpec(ek, primary.model), fbs = p.fallbacks || {};
    for (const k of [primary.model, spec?.id].filter(Boolean).map(v => `${ek}:${v}`)) {
      if (k in fbs) return [k, fbs[k] || []];
    }
    if (spec) {
      const eff = effortOf(primary);
      let fallbackMatch = null;
      for (const [k, chain] of Object.entries(fbs)) {
        if (!k.startsWith(ek + ':')) continue;
        const mod = k.slice(ek.length + 1);
        if (!spec.re.test(mod.toLowerCase())) continue;
        const kEff = effortOf({ model: mod, effort: '' });
        if (!kEff || !eff || kEff === eff) return [k, chain || []];
        if (!fallbackMatch) fallbackMatch = [k, chain || []];
      }
      if (fallbackMatch) return fallbackMatch;
    }
    return [`${ek}:${spec?.id || primary.model}`, []];
  }
  function activeDroidFallback(p, target) {
    return findFallbackEntry(p, target)[1].find(c => c && c.enabled !== false && c.engine === 'droid') || null;
  }
  function syncFallback(p, target, enabled) {
    if (!target || target.engine === 'droid') return;
    const ek = target.engine, spec = findSpec(ek, target.model);
    const exactKey = `${ek}:${target.model}`, canonKey = `${ek}:${spec?.id || target.model}`;
    const fbs = p.fallbacks || (p.fallbacks = {});
    const key = (exactKey !== canonKey && exactKey in fbs) ? exactKey : canonKey;
    const base = (fbs[key] || []).filter(Boolean);
    const droidIdx = base.findIndex(c => c.engine === 'droid');
    if (enabled) {
      fbs[key] = droidIdx >= 0
        ? base.map((c, i) => (i === droidIdx ? { ...c, enabled: true } : { ...c }))
        : [...base.map(c => ({ ...c })), { ...droidFallbackFor(target), enabled: true }];
    } else if (droidIdx >= 0) {
      fbs[key] = base.map(c => (c.engine === 'droid' ? { ...c, enabled: false } : { ...c }));
    } else {
      fbs[key] = [...base.map(c => ({ ...c })), { ...droidFallbackFor(target), enabled: false }];
    }
    if (key === canonKey && enabled) {
      const targetEff = effortOf(target), defDroid = droidFallbackFor(target);
      const isDefaultEnabledDroid = c => c.engine === 'droid'
        && c.enabled !== false
        && (c.channel || 'cli') === defDroid.channel
        && (c.effort === 'same' || (!c.effort && !targetEff) || c.effort === targetEff)
        && (c.model === defDroid.model || Boolean(spec && spec.re.test(String(c.model || '').toLowerCase())));
      if (fbs[canonKey].length && fbs[canonKey].every(isDefaultEnabledDroid)) {
        for (const k of Object.keys(fbs)) {
          if (k === canonKey) continue;
          const matchesAlias = k === exactKey || (spec && k.startsWith(ek + ':') && spec.re.test(k.slice(ek.length + 1).toLowerCase()));
          if (!matchesAlias) continue;
          const kEff = effortOf({ model: k.slice(ek.length + 1), effort: '' });
          if (k !== exactKey && kEff && kEff !== targetEff) continue;
          const chain = (fbs[k] || []).filter(Boolean);
          if (chain.length && chain.every(isDefaultEnabledDroid)) delete fbs[k];
        }
      }
    }
  }
  function fmtTarget(x) {
    const ek = engKeyOf(x), em = ENG[ek] || { label: ek, brand: 'agy' };
    const spec = findSpec(ek, x.model)
      || (ek === 'droid' ? (findSpec('codex', x.model) || findSpec('agy', x.model) || findSpec('grok', x.model)) : null);
    const lbl = spec ? spec.label : String(x.model || '');
    const eff = EFF[effortOf(x)];
    const brand = ek === 'droid' ? 'droid' : (spec?.brand || (ek === 'agy' && /claude|opus|sonnet/i.test(x.model || '') ? 'claude' : em.brand));
    return { eng: em.label, short: lbl, text: eff && ek !== 'native' && ek !== 'swarm' ? `${lbl} · ${eff}` : lbl, brand };
  }

  function renderTargetEditor(p, target, { availableEngines, onChange, onOptCommit }) {
    const ek = engKeyOf(target), em = ENG[ek] || { label: ek, brand: 'agy' };
    const spec = findSpec(ek, target.model) || { id: target.model, label: fmtTarget(target).short, efforts: target.effort ? E4 : [] };
    const curEffRaw = effortOf(target), card = el('div', { className: 'ds-card' });

    const engKeys = PRIMARY_ENGINES.filter(e => availableEngines.includes(e) || e === ek);
    if (!engKeys.includes(ek)) engKeys.unshift(ek);
    if (!engKeys.length) engKeys.push(...PRIMARY_ENGINES);
    card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '引擎'),
      el('div', { className: 'ds-chips' }, ...engKeys.map(k => {
        const miss = PRIMARY_ENGINES.includes(k) && !availableEngines.includes(k);
        const btn = el('button', { type: 'button', className: k === ek ? 'ds-chip is-on' : 'ds-chip' },
          dot(ENG[k]?.brand || 'agy'), ENG[k]?.label || k, miss ? el('span', { className: 'ds-chip-meta' }, '未装') : '');
        btn.addEventListener('click', () => {
          const list = modelsFor(k);
          if (k !== ek && list.length) onChange(buildTarget(k, list[0], curEffRaw || 'high'));
        });
        return btn;
      }))));

    const modList = modelsFor(ek), modItems = modList.some(x => x.id === spec.id) ? modList : [spec, ...modList];
    const mkChip = x => {
      const btn = el('button', { type: 'button', className: x.id === spec.id ? 'ds-chip is-on' : 'ds-chip' },
        dot(x.brand || em.brand), x.label);
      btn.addEventListener('click', () => { if (x.id !== spec.id) onChange(buildTarget(ek, x, curEffRaw || 'high')); });
      return btn;
    };
    const groups = [...new Set(modItems.map(x => x.group).filter(Boolean))];
    const modBlock = el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '模型'));
    if (groups.length > 1) {
      for (const g of groups) {
        modBlock.append(el('div', { className: 'ds-mod-grp' },
          el('span', { className: 'ds-mod-sub' }, g),
          el('div', { className: 'ds-chips' }, ...modItems.filter(x => x.group === g).map(mkChip))));
      }
      const rest = modItems.filter(x => !x.group);
      if (rest.length) modBlock.append(el('div', { className: 'ds-chips' }, ...rest.map(mkChip)));
    } else {
      modBlock.append(el('div', { className: 'ds-chips' }, ...modItems.map(mkChip)));
    }
    card.append(modBlock);

    if (PRIMARY_ENGINES.includes(ek) && spec.efforts.length) {
      const efforts = curEffRaw && !spec.efforts.includes(curEffRaw) ? [...spec.efforts, curEffRaw] : spec.efforts;
      const curEff = efforts.includes(curEffRaw) ? curEffRaw : '';
      card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '推理档位'),
        el('div', { className: 'ds-seg' }, ...efforts.map(e => {
          const btn = el('button', { type: 'button', className: e === curEff ? 'is-on' : '' }, EFF[e] || e);
          btn.addEventListener('click', () => { if (e !== curEff) onChange(buildTarget(ek, spec, e)); });
          return btn;
        }))));
    }

    if (ek === 'codex') {
      const fastCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: p.codex_fast, 'aria-label': 'Codex Fast 模式' });
      fastCb.addEventListener('change', () => { p.codex_fast = fastCb.checked; onOptCommit(); });
      card.append(el('label', { className: 'ds-item ds-item-click' },
        el('span', { className: 'ds-lbl-sub' }, 'Codex Fast 模式'), fastCb));
    }
    return card;
  }

  function renderDroidCard(p, target, { enabled, activeEntry, onToggle, onOptCommit }) {
    const fbCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: enabled, 'aria-label': '回退到 Droid' });
    fbCb.addEventListener('change', () => onToggle(fbCb.checked));
    const baseFb = activeEntry || droidFallbackFor(target);
    const fbPreview = { ...baseFb, effort: baseFb.effort === 'same' ? effortOf(target) : effortOf(baseFb) };
    const card = el('div', { className: 'ds-card' },
      el('label', { className: 'ds-item ds-item-click' },
        el('div', { className: 'ds-row-col' },
          el('span', { className: 'ds-lbl' }, '回退到 Droid'),
          el('span', { className: 'ds-fb-sub' }, `→ Droid ${fmtTarget(fbPreview).text}`)),
        fbCb));
    if (enabled) {
      const qOn = p.quota_fallback.enabled, pct = p.quota_fallback.threshold_pct;
      const thrVal = el('span', {}, `≤${pct}%`);
      const minus = el('button', { type: 'button', disabled: !qOn || pct <= 0, 'aria-label': '降低阈值' }, '−');
      const plus = el('button', { type: 'button', disabled: !qOn || pct >= 50, 'aria-label': '提高阈值' }, '+');
      const stepBox = el('div', { className: qOn ? 'ds-step' : 'ds-step is-off' }, minus, thrVal, plus);
      const syncStep = () => {
        const on = p.quota_fallback.enabled, cur = p.quota_fallback.threshold_pct;
        thrVal.textContent = `≤${cur}%`;
        stepBox.className = on ? 'ds-step' : 'ds-step is-off';
        minus.disabled = !on || cur <= 0;
        plus.disabled = !on || cur >= 50;
      };
      const nudge = d => {
        const next = Math.min(50, Math.max(0, p.quota_fallback.threshold_pct + d));
        if (next === p.quota_fallback.threshold_pct) return;
        p.quota_fallback.threshold_pct = next; syncStep(); onOptCommit();
      };
      minus.addEventListener('click', () => nudge(-5));
      plus.addEventListener('click', () => nudge(5));
      const qCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: qOn, 'aria-label': '低配额自动回退到 Droid' });
      qCb.addEventListener('change', () => { p.quota_fallback.enabled = qCb.checked; syncStep(); onOptCommit(); });
      card.append(el('div', { className: 'ds-item' },
        el('span', { className: 'ds-lbl-sub' }, '低配额自动回退'),
        el('div', { className: 'ds-thr-wrap' }, stepBox, qCb)));
    }
    return card;
  }

  window.__COCKPIT_DISPATCH_CATALOG__ = {
    SKIP_WHY, ROLES, DEFAULT_SEAT_POOL, el, dot, findSpec,
    activeDroidFallback, syncFallback, fmtTarget, renderTargetEditor, renderDroidCard,
  };
})();
