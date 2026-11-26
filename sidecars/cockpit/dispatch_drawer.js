(() => {
  'use strict';

  const trigger = document.getElementById('dispatchSettingsBtn');
  if (!trigger) return;
  const endpoint = '/api/dispatch-policy';
  const csrf = document.querySelector('meta[name="cockpit-csrf"]')?.content || '';
  const ENG = { agy: 'AGY', codex: 'Codex', droid: 'Droid', grok: 'Grok', pi: 'Pi' };
  const EFF = { '': '', low: '低', medium: '中', high: '高', xhigh: '极高', same: '同档' };
  const SKIP_WHY = { quota_low: '配额低', cli_unavailable: '未装', external_disabled: 'CLI已关', swarm_disabled: 'Swarm已关' };
  const MODEL_NAMES = {
    'gemini-4-hydrogen-high': 'Hydrogen', 'gemini-4-neon-medium': 'Neon',
    'gemini-4-neon-high': 'Neon', 'gemini-4-neon-xhigh': 'Neon',
    'gemini-4-argon': 'Argon', 'gemini-3.8-flash': 'Flash 3.8',
    'claude-opus-5-5-high': 'Opus 5.5', 'claude-opus-5-5': 'Opus 5.5',
    'gpt-6.2-sol': 'sol', 'gpt-6.2-astra': 'astra', 'grok-4.7': 'Grok 4.7',
    'Gemini 4 Neon (Medium)': 'Neon · 中', 'Gemini 4 Neon (High)': 'Neon · 高',
    'Gemini 4 Argon (Medium)': 'Argon · 中',
  };
  const ROLES = [['scout', '快搜侦察'], ['standard', '常规制作'], ['deep', '深度执行'], ['expert', '专家验收']];
  const FALLBACK_LABELS = {
    'agy:gemini-4-hydrogen-high': 'AGY Hydrogen', 'agy:gemini-4-neon-medium': 'AGY Neon · 中',
    'agy:gemini-4-neon-high': 'AGY Neon · 高', 'codex:gpt-6.2-sol': 'Codex sol',
    'codex:gpt-6.2-astra': 'Codex astra', 'agy:claude-opus-5-5-high': 'AGY Opus 5.5',
  };
  const t = (engine, model, effort = '', channel = 'cli', extra = {}) => ({ channel, engine, model, effort, ...extra });
  const ROLE_CHOICES = {
    scout: [t('agy', 'gemini-4-hydrogen-high', 'high'), t('droid', 'gemini-3.8-flash', 'high'), t('agy', 'flash', '', 'invoke_subagent')],
    standard: [
      t('agy', 'gemini-4-neon-medium', 'medium'), t('agy', 'gemini-4-neon-high', 'high'),
      t('droid', 'gemini-4-argon', 'medium'), t('agy', 'inherit', '', 'invoke_subagent'),
      t('agy', 'Gemini 4 Neon (Medium)', '', 'swarm'),
    ],
    deep: [
      t('codex', 'gpt-6.2-sol', 'high'), t('agy', 'gemini-4-neon-high', 'high'),
      t('droid', 'gpt-6.2-sol', 'high'), t('agy', 'pro', '', 'invoke_subagent'),
      t('agy', 'Gemini 4 Neon (Medium)', '', 'swarm'),
    ],
    expert: [
      t('codex', 'gpt-6.2-astra', 'xhigh'), t('codex', 'gpt-6.2-sol', 'xhigh'),
      t('agy', 'gemini-4-neon-xhigh', 'xhigh'), t('droid', 'gpt-6.2-astra', 'xhigh'),
      t('agy', 'pro', '', 'invoke_subagent'), t('agy', 'Gemini 4 Neon (High)', '', 'swarm'),
    ],
  };
  const FALLBACK_CHOICES = [
    t('droid', 'gemini-3.8-flash', 'high'), t('droid', 'gemini-4-argon', 'medium'),
    t('droid', 'gpt-6.2-sol', 'same'), t('droid', 'gpt-6.2-astra', 'xhigh'),
    t('droid', 'claude-opus-5-5', 'high'), t('agy', 'gemini-4-neon-medium', 'medium'),
    t('codex', 'gpt-6.2-sol', 'high'),
  ];
  const PANEL_SEATS = (on3, on4) => [
    t('codex', 'gpt-6.2-astra', 'xhigh', 'cli', { enabled: true }),
    t('agy', 'gemini-4-neon-xhigh', 'xhigh', 'cli', { enabled: true }),
    t('agy', 'claude-opus-5-5-high', 'high', 'cli', { enabled: on3 }),
    t('grok', 'grok-4.7', 'xhigh', 'cli', { enabled: on4 }),
  ];
  const PANEL_MODES = {
    dual: { label: 'Astra + Neon', eng: '两方', meta: '两方盲审', brand: 'codex', seats: PANEL_SEATS(false, false) },
    quad: { label: '全引擎四方', eng: '四方', meta: '四方盲审', brand: 'grok', seats: PANEL_SEATS(true, true) },
    builtin: { label: '独立盲审', eng: '内置', meta: '内置', brand: 'native', seats: [t('agy', 'flash', '', 'invoke_subagent', { enabled: true }), t('agy', 'flash', '', 'invoke_subagent', { enabled: true })] },
    swarm: { label: '公开多视角', eng: 'Swarm', meta: 'Swarm', brand: 'swarm', seats: [t('agy', 'Gemini 4 Neon (High)', '', 'swarm', { enabled: true }), t('agy', 'Gemini 4 Argon (Medium)', '', 'swarm', { enabled: true })] },
  };

  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (typeof v === 'boolean' || k === 'value' || k === 'className') n[k] = v;
      else n.setAttribute(k, v);
    }
    n.append(...kids);
    return n;
  };
  const chevSvg = () => {
    const ns = 'http://www.w3.org/2000/svg', s = document.createElementNS(ns, 'svg'), p = document.createElementNS(ns, 'path');
    for (const [k, v] of [['viewBox', '0 0 10 6'], ['width', '9'], ['height', '5'], ['class', 'ds-chev'], ['aria-hidden', 'true']]) s.setAttribute(k, v);
    for (const [k, v] of [['d', 'M1 1l4 4 4-4'], ['fill', 'none'], ['stroke', 'currentColor'], ['stroke-width', '1.5'], ['stroke-linecap', 'round'], ['stroke-linejoin', 'round']]) p.setAttribute(k, v);
    s.append(p);
    return s;
  };
  const dot = b => el('span', { className: 'ds-dot', 'data-b': b, 'aria-hidden': 'true' });
  const tKey = x => `${x.channel}|${x.engine}|${x.model}|${x.effort || ''}`;
  const parseKey = k => { const [channel, engine, model, effort] = k.split('|'); return { channel, engine, model, effort: effort || '' }; };
  const brandOf = x => !x ? 'off' : x.channel === 'invoke_subagent' ? 'native' : x.channel === 'swarm' ? 'swarm'
    : (x.engine === 'agy' && /claude|opus|sonnet/i.test(x.model || '') ? 'claude' : (x.engine || 'agy'));
  function fmtOption(x, installed) {
    if (x.channel === 'invoke_subagent') {
      return { text: { flash: 'Flash', inherit: '继承主模型', pro: 'Pro' }[x.model] || x.model, eng: '内置', meta: '内置', brand: 'native' };
    }
    const name = MODEL_NAMES[x.model] || String(x.model || '').replace(/^(?:gemini-4-|gemini-|claude-)/, '').replace(/-(?:low|medium|high|xhigh)$/, '');
    if (x.channel === 'swarm') return { text: name, eng: 'Swarm', meta: 'Swarm', brand: 'swarm' };
    const eff = EFF[x.effort || ''], eng = ENG[x.engine] || x.engine;
    const miss = installed && !installed.includes(x.engine) ? ' · 未装' : '';
    return { text: eff ? `${name} · ${eff}` : name, eng, meta: eng + miss, brand: brandOf(x) };
  }
  function detectPanelMode(seats) {
    const on = (seats || []).filter(s => s && s.enabled !== false);
    if (!on.length) return 'custom';
    if (on.every(s => s.channel === 'invoke_subagent')) return 'builtin';
    if (on.every(s => s.channel === 'swarm')) return 'swarm';
    if (on.length >= 4 && on.every(s => s.channel === 'cli')) return 'quad';
    if (on.length === 2 && on.every(s => s.channel === 'cli')) return 'dual';
    return 'custom';
  }

  let saved = null, draft = null, busy = false, dirty = false, conflicted = false, advOpen = false;
  let activeMenu = null, activeAnchor = null;

  const drawer = el('dialog', { id: 'dispatchDrawer', 'aria-labelledby': 'dsTitle' });
  const masterTxt = el('span', {}, '已启用');
  const masterCb = el('input', { type: 'checkbox', className: 'ds-switch', 'aria-label': '启用子代理派发' });
  const closeBtn = el('button', { type: 'button', className: 'ds-close', 'aria-label': '关闭' }, '✕');
  const warnEl = el('div', { className: 'ds-warn', role: 'alert', hidden: true });
  const fields = el('fieldset', { className: 'ds-fields', 'aria-label': '派发设置' });
  const bodyEl = el('div', { className: 'ds-body' }, warnEl, fields);
  const statusEl = el('span', { className: 'ds-status', role: 'status', 'aria-live': 'polite' });
  const reloadBtn = el('button', { type: 'button', className: 'ds-ghost', hidden: true }, '重载');
  const resetBtn = el('button', { type: 'button', className: 'ds-ghost' }, '恢复默认');
  const saveBtn = el('button', { type: 'submit', className: 'ds-save' }, '保存');
  const form = el('form', {}, bodyEl,
    el('div', { className: 'ds-foot' }, resetBtn, el('div', { className: 'ds-acts' }, statusEl, reloadBtn, saveBtn)));
  drawer.append(
    el('div', { className: 'ds-head' },
      el('h2', { id: 'dsTitle' }, '派发设置'),
      el('div', { className: 'ds-head-right' }, el('label', { className: 'ds-master' }, masterTxt, masterCb), closeBtn)),
    form);
  document.body.append(drawer);

  function closeMenu(restoreFocus = false) {
    const ret = activeAnchor;
    if (activeMenu) { activeMenu.remove(); activeMenu = null; }
    if (activeAnchor) { activeAnchor.setAttribute('aria-expanded', 'false'); activeAnchor = null; }
    if (restoreFocus) ret?.focus();
  }
  function openMenu(anchor, items, curVal, onPick) {
    if (activeAnchor === anchor) { closeMenu(); return; }
    closeMenu();
    const menu = el('div', { className: 'ds-menu', role: 'listbox' });
    const opts = items.map(it => {
      const isCur = it.value === curVal;
      const opt = el('button', { type: 'button', className: isCur ? 'ds-opt is-cur' : 'ds-opt', role: 'option', 'aria-selected': String(isCur) },
        el('span', { className: 'ds-opt-chk', 'aria-hidden': 'true' }, isCur ? '✓' : ''),
        dot(it.brand), el('span', { className: 'ds-opt-main' }, it.text),
        it.meta ? el('span', { className: 'ds-opt-meta' }, it.meta) : '');
      opt.addEventListener('click', e => { e.stopPropagation(); closeMenu(); if (it.value !== curVal) onPick(it.value); });
      menu.append(opt);
      return opt;
    });
    menu.addEventListener('keydown', e => {
      const idx = opts.indexOf(document.activeElement);
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        opts[(idx + (e.key === 'ArrowDown' ? 1 : -1) + opts.length) % opts.length]?.focus();
      } else if (e.key === 'Home') { e.preventDefault(); opts[0]?.focus(); }
      else if (e.key === 'End') { e.preventDefault(); opts[opts.length - 1]?.focus(); }
    });
    drawer.append(menu);
    const r = anchor.getBoundingClientRect(), dr = drawer.getBoundingClientRect();
    const availBelow = window.innerHeight - r.bottom - 34, availAbove = r.top - 10;
    const rawH = menu.scrollHeight || 160, mw = menu.offsetWidth || 204;
    const goDown = availBelow >= Math.min(rawH, 168) || availBelow >= availAbove;
    const maxH = Math.max(96, Math.min(240, goDown ? availBelow : availAbove));
    menu.style.maxHeight = `${maxH}px`;
    const mh = Math.min(rawH, maxH);
    menu.style.left = `${Math.max(dr.left + 8, Math.min(r.right - mw, dr.right - mw - 8))}px`;
    menu.style.top = `${goDown ? r.bottom + 4 : Math.max(8, r.top - mh - 4)}px`;
    activeMenu = menu; activeAnchor = anchor;
    anchor.setAttribute('aria-expanded', 'true');
    (opts.find(o => o.classList.contains('is-cur')) || opts[0])?.focus();
  }
  function makePicker(label, curItem, items, curVal, onPick) {
    const btn = el('button', {
      type: 'button', className: curVal === '__off__' ? 'ds-pick is-off' : 'ds-pick',
      'aria-haspopup': 'listbox', 'aria-expanded': 'false', 'aria-label': label,
    }, dot(curItem.brand), curItem.eng ? el('span', { className: 'ds-pick-eng' }, curItem.eng) : '',
    el('span', { className: 'ds-pick-txt' }, curItem.text), chevSvg());
    btn.addEventListener('click', e => { e.stopPropagation(); openMenu(btn, items, curVal, onPick); });
    return btn;
  }

  const avail = () => saved?.capabilities?.available_engines || [];
  function setMsg(text, tone = '') {
    if (tone === 'err') {
      warnEl.hidden = false; warnEl.dataset.tone = 'err'; warnEl.textContent = text;
      statusEl.textContent = ''; statusEl.dataset.tone = '';
    } else {
      if (!saved?.read_only_warning) { warnEl.hidden = true; warnEl.dataset.tone = ''; }
      statusEl.textContent = text; statusEl.dataset.tone = tone;
    }
  }
  function sync() {
    const ro = !draft || Boolean(saved?.read_only_warning);
    fields.disabled = busy || ro;
    masterCb.disabled = fields.disabled;
    masterCb.checked = Boolean(draft?.enabled);
    masterTxt.textContent = draft ? (draft.enabled ? '已启用' : '已暂停') : '';
    saveBtn.disabled = fields.disabled || conflicted || !dirty;
    resetBtn.disabled = fields.disabled || conflicted || (!saved?.exists && !dirty);
    reloadBtn.disabled = busy;
  }
  function markDirty() { dirty = true; if (!conflicted) setMsg('未保存', 'warn'); sync(); }
  function hasCliRole(p) {
    return ROLES.some(([r]) => p.roles[r].primary.channel === 'cli')
      || (p.roles.panel.seats || []).some(s => s.enabled !== false && s.channel === 'cli');
  }
  function activeMode() {
    if (!draft || !draft.enabled) return '';
    if (!draft.external_enabled) return 'native';
    if (!hasCliRole(draft)) return '';
    if (!draft.codex_fast && draft.quota_fallback?.enabled && draft.quota_fallback.threshold_pct >= 15
      && draft.roles.scout.primary.channel === 'invoke_subagent') return 'eco';
    return 'auto';
  }
  function advSummaryText(p) {
    if (!p.external_enabled) return p.allow_swarm ? '仅内置 · Swarm' : '仅内置';
    return [p.quota_fallback.enabled ? `配额 ≤${p.quota_fallback.threshold_pct}%` : '不回退',
      p.codex_fast ? 'Fast' : '', p.allow_swarm ? 'Swarm' : ''].filter(Boolean).join(' · ');
  }
  function applyPreset(kind) {
    const has = avail(), useCli = kind !== 'native' && has.length > 0, eco = kind === 'eco';
    draft.enabled = true; draft.external_enabled = useCli;
    draft.codex_fast = useCli && has.includes('codex') && !eco;
    draft.quota_fallback = { enabled: true, threshold_pct: eco ? 20 : 5 };
    const pick = (role, native) => (!useCli || native
      ? ROLE_CHOICES[role].find(c => c.channel === 'invoke_subagent')
      : ROLE_CHOICES[role].find(c => c.channel === 'cli' && has.includes(c.engine))
        || ROLE_CHOICES[role].find(c => c.channel === 'invoke_subagent'));
    for (const [role, native] of [['scout', eco], ['standard', eco], ['deep', false], ['expert', false]]) {
      draft.roles[role].primary = { ...pick(role, native) };
    }
    draft.roles.panel.seats = structuredClone((useCli ? PANEL_MODES.dual : PANEL_MODES.builtin).seats);
    markDirty(); render();
  }
  function buildMenuItems(current, choices, allowOff = false) {
    const has = avail(), seen = new Set(), items = [];
    if (allowOff) { items.push({ value: '__off__', text: '停用', eng: '', meta: '不回退', brand: 'off' }); seen.add('__off__'); }
    for (const c of choices.filter(x => x.channel !== 'cli' || has.includes(x.engine))) {
      const k = tKey(c);
      if (!seen.has(k)) { seen.add(k); items.push({ value: k, ...fmtOption(c, has) }); }
    }
    if (current && !seen.has(tKey(current))) items.unshift({ value: tKey(current), ...fmtOption(current, has) });
    return items;
  }

  function render() {
    if (!draft) return;
    const prevScroll = bodyEl.scrollTop;
    closeMenu();
    const p = draft, has = avail(), mode = activeMode();
    if (saved.read_only_warning) {
      warnEl.hidden = false; warnEl.dataset.tone = '';
      warnEl.textContent = '配置文件异常或来自更高版本，当前只读展示默认值。';
    }
    const seg = el('div', { className: 'ds-seg', role: 'group', 'aria-label': '快捷模式' },
      ...[['auto', '多引擎', !has.length], ['native', '纯内置', false], ['eco', '省配额', !has.length]].map(([k, txt, off]) => {
        const b = el('button', { type: 'button', className: mode === k ? 'is-on' : '', disabled: off, title: off ? '未检测到外部 CLI 引擎' : '' }, txt);
        b.addEventListener('click', () => applyPreset(k));
        return b;
      }));

    const roleCard = el('div', { className: p.enabled ? 'ds-card' : 'ds-card ds-dimmed' });
    for (const [role, name] of ROLES) {
      const cur = p.roles[role].primary, res = saved.resolved?.[role];
      let note = '';
      if (!dirty && p.enabled && res && (res.reason === 'fallback' || res.reason === 'builtin_fallback')) {
        const f = fmtOption(res), why = SKIP_WHY[res.skipped?.[0]?.reason];
        note = el('span', { className: 'ds-hint' }, `${why ? why + ' → ' : '↪ 生效：'}${f.eng} ${f.text}`);
      }
      roleCard.append(el('div', { className: 'ds-item' },
        el('div', { className: 'ds-item-col' }, el('span', { className: 'ds-lbl' }, name), note),
        makePicker(name, fmtOption(cur, has), buildMenuItems(cur, ROLE_CHOICES[role]), tKey(cur), v => {
          const next = parseKey(v);
          p.roles[role].primary = next;
          if (next.channel === 'cli' && !p.external_enabled) p.external_enabled = true;
          if (next.channel === 'swarm' && !p.allow_swarm) p.allow_swarm = true;
          markDirty(); render();
        })));
    }
    const pMode = detectPanelMode(p.roles.panel.seats);
    const pItems = Object.entries(PANEL_MODES)
      .filter(([k]) => !((k === 'dual' || k === 'quad') && !has.length && pMode !== k))
      .map(([k, m]) => ({ value: k, text: m.label, eng: m.eng, meta: m.meta, brand: m.brand }));
    if (pMode === 'custom') pItems.unshift({ value: 'custom', text: '自定义席位', eng: '', meta: '', brand: 'agy' });
    roleCard.append(el('div', { className: 'ds-item' },
      el('span', { className: 'ds-lbl' }, '多方会审'),
      makePicker('多方会审', pItems.find(x => x.value === pMode) || pItems[0], pItems, pMode, v => {
        if (!PANEL_MODES[v]) return;
        p.roles.panel.seats = structuredClone(PANEL_MODES[v].seats);
        if ((v === 'dual' || v === 'quad') && !p.external_enabled) p.external_enabled = true;
        if (v === 'swarm') p.allow_swarm = true;
        markDirty(); render();
      })));

    const advSumEl = el('span', { className: 'ds-adv-sum' }, advSummaryText(p));
    const mkToggleRow = (label, checked, onSet, extra = null) => {
      const cb = el('input', { type: 'checkbox', className: 'ds-switch', checked, 'aria-label': label });
      cb.addEventListener('change', () => { onSet(cb.checked); markDirty(); render(); });
      if (!extra) return el('label', { className: 'ds-item ds-item-click' }, el('span', { className: 'ds-lbl-sub' }, label), cb);
      return el('div', { className: 'ds-item' }, el('span', { className: 'ds-lbl-sub' }, label), el('div', { className: 'ds-thr-wrap' }, extra, cb));
    };
    const swCard = el('div', { className: 'ds-card' }, mkToggleRow('外部 CLI 引擎', p.external_enabled, v => { p.external_enabled = v; }));
    if (p.external_enabled) swCard.append(mkToggleRow('Codex Fast 模式', p.codex_fast, v => { p.codex_fast = v; }));
    swCard.append(mkToggleRow('允许 Swarm 协作', p.allow_swarm, v => { p.allow_swarm = v; }));
    if (p.external_enabled) {
      const thrVal = el('span', {}, `≤${p.quota_fallback.threshold_pct}%`);
      const minus = el('button', { type: 'button', disabled: p.quota_fallback.threshold_pct <= 0, 'aria-label': '降低阈值' }, '−');
      const plus = el('button', { type: 'button', disabled: p.quota_fallback.threshold_pct >= 50, 'aria-label': '提高阈值' }, '+');
      const nudge = delta => {
        const next = Math.min(50, Math.max(0, p.quota_fallback.threshold_pct + delta));
        if (next === p.quota_fallback.threshold_pct) return;
        p.quota_fallback.threshold_pct = next;
        thrVal.textContent = `≤${next}%`; minus.disabled = next <= 0; plus.disabled = next >= 50;
        advSumEl.textContent = advSummaryText(p); markDirty();
      };
      minus.addEventListener('click', () => nudge(-5));
      plus.addEventListener('click', () => nudge(5));
      swCard.append(mkToggleRow('低配额自动回退', p.quota_fallback.enabled, v => { p.quota_fallback.enabled = v; },
        el('div', { className: p.quota_fallback.enabled ? 'ds-step' : 'ds-step is-off' }, minus, thrVal, plus)));
    }

    const advKids = [swCard];
    if (p.external_enabled) {
      const fbCard = el('div', { className: 'ds-card' }, el('div', { className: 'ds-card-hdr' }, '模型回退'));
      for (const [key, chain] of Object.entries(p.fallbacks || {})) {
        const firstOn = (chain || []).find(c => c && c.enabled !== false) || null;
        const lbl = FALLBACK_LABELS[key] || key.replace(':', ' · ');
        fbCard.append(el('div', { className: 'ds-item' },
          el('span', { className: 'ds-lbl-sub', title: key }, lbl),
          makePicker(lbl + ' 回退模型', firstOn ? fmtOption(firstOn, has) : { text: '停用', eng: '', brand: 'off' },
            buildMenuItems(firstOn, FALLBACK_CHOICES, true), firstOn ? tKey(firstOn) : '__off__', v => {
              const cur = p.fallbacks[key] || [];
              p.fallbacks[key] = v === '__off__' ? cur.map(c => ({ ...c, enabled: false }))
                : [{ ...parseKey(v), enabled: true }, ...cur.filter(c => tKey(c) !== v)];
              markDirty(); render();
            })));
      }
      advKids.push(fbCard);
    }

    const adv = el('details', { className: p.enabled ? 'ds-adv' : 'ds-adv ds-dimmed', open: advOpen },
      el('summary', {}, el('span', {}, '回退与开关'), advSumEl),
      el('div', { className: 'ds-adv-body' }, ...advKids));
    adv.addEventListener('toggle', () => { advOpen = adv.open; });

    const kids = [seg];
    if (!p.enabled) kids.push(el('div', { className: 'ds-banner' }, '派发已暂停，任务将由主代理直接执行。'));
    else if (!p.external_enabled && hasCliRole(p)) kids.push(el('div', { className: 'ds-banner' }, '外部 CLI 已关闭，下方 CLI 角色将自动回退至内置通道。'));
    kids.push(roleCard, adv);
    fields.replaceChildren(...kids);
    bodyEl.scrollTop = prevScroll;
    sync();
  }

  async function api(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) {
      throw Object.assign(new Error(res.status === 403 ? '面板已重载，请刷新后重试' : (data?.error || `请求失败 (${res.status})`)), { status: res.status });
    }
    return data;
  }
  async function load() {
    busy = true; sync(); setMsg('读取中…');
    try {
      saved = await api(endpoint + '?resolve=1', { cache: 'no-store' });
      draft = structuredClone(saved.policy);
      dirty = false; conflicted = false; reloadBtn.hidden = true;
      render(); setMsg('');
      return true;
    } catch (e) { setMsg(e.message, 'err'); return false; }
    finally { busy = false; sync(); }
  }
  async function write(isReset) {
    if (!saved) return;
    if (isReset && !saved.exists) { draft = structuredClone(saved.policy); dirty = false; render(); setMsg('已还原', 'ok'); return; }
    busy = true; sync(); setMsg(isReset ? '恢复中…' : '保存中…');
    try {
      await api(isReset ? endpoint + '/reset' : endpoint, {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-Cockpit-CSRF': csrf },
        body: JSON.stringify(isReset ? { revision: saved.policy.revision } : draft),
      });
      if (await load()) setMsg(isReset ? '已恢复默认' : '已保存', 'ok');
    } catch (e) {
      if (e.status === 409) { conflicted = true; reloadBtn.hidden = false; setMsg('配置已在别处变更，请点「重载」获取最新版本', 'err'); }
      else setMsg(e.message, 'err');
    } finally { busy = false; sync(); }
  }

  masterCb.addEventListener('change', () => { if (!draft) return; draft.enabled = masterCb.checked; markDirty(); render(); });
  bodyEl.addEventListener('scroll', () => closeMenu(), { passive: true });
  window.addEventListener('resize', () => closeMenu());
  closeBtn.addEventListener('click', () => { closeMenu(); drawer.close(); });
  reloadBtn.addEventListener('click', () => load());
  resetBtn.addEventListener('click', () => write(true));
  form.addEventListener('submit', e => { e.preventDefault(); write(false); });
  trigger.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); });
  trigger.addEventListener('click', e => {
    e.stopPropagation();
    if (drawer.open) return;
    if (document.getElementById('quotaDrawer')?.classList.contains('open')) document.getElementById('qdClose')?.click();
    drawer.showModal();
    trigger.setAttribute('aria-expanded', 'true');
    if (!dirty && !conflicted) load();
  });
  drawer.addEventListener('click', e => {
    if (activeMenu && !activeMenu.contains(e.target) && !activeAnchor?.contains(e.target)) { closeMenu(); return; }
    if (e.target !== drawer) return;
    const r = drawer.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX >= r.right || e.clientY < r.top || e.clientY >= r.bottom) drawer.close();
  });
  drawer.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    e.stopPropagation(); e.preventDefault();
    if (activeMenu) closeMenu(true); else drawer.close();
  });
  drawer.addEventListener('close', () => { closeMenu(); trigger.setAttribute('aria-expanded', 'false'); trigger.focus(); });
  sync();
})();
