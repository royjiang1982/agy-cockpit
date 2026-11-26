(() => {
  'use strict';

  const trigger = document.getElementById('dispatchSettingsBtn');
  if (!trigger) return;
  const endpoint = '/api/dispatch-policy';
  const csrf = document.querySelector('meta[name="cockpit-csrf"]')?.content || '';
  const ENG = { agy: 'AGY', codex: 'Codex', droid: 'Droid', grok: 'Grok', pi: 'Pi' };
  const EFF = { '': '', low: '低', medium: '中', high: '高', xhigh: '极高', same: '同档' };
  const ROLES = [
    ['scout', '快搜侦察'],
    ['standard', '常规制作'],
    ['deep', '深度执行'],
    ['expert', '专家验收'],
  ];
  const FALLBACK_LABELS = {
    'agy:gemini-4-hydrogen-high': 'AGY Hydrogen',
    'agy:gemini-4-neon-medium': 'AGY Neon (中)',
    'agy:gemini-4-neon-high': 'AGY Neon (高)',
    'codex:gpt-6.2-sol': 'Codex sol',
    'codex:gpt-6.2-astra': 'Codex astra',
    'agy:claude-opus-5-5-high': 'AGY Opus 5.5',
  };
  const ROLE_CHOICES = {
    scout: [
      { channel: 'cli', engine: 'agy', model: 'gemini-4-hydrogen-high', effort: 'high' },
      { channel: 'cli', engine: 'droid', model: 'gemini-3.8-flash', effort: 'high' },
      { channel: 'invoke_subagent', engine: 'agy', model: 'flash', effort: '' },
    ],
    standard: [
      { channel: 'cli', engine: 'agy', model: 'gemini-4-neon-medium', effort: 'medium' },
      { channel: 'cli', engine: 'agy', model: 'gemini-4-neon-high', effort: 'high' },
      { channel: 'cli', engine: 'droid', model: 'gemini-4-argon', effort: 'medium' },
      { channel: 'invoke_subagent', engine: 'agy', model: 'inherit', effort: '' },
      { channel: 'swarm', engine: 'agy', model: 'Gemini 4 Neon (Medium)', effort: '' },
    ],
    deep: [
      { channel: 'cli', engine: 'codex', model: 'gpt-6.2-sol', effort: 'high' },
      { channel: 'cli', engine: 'agy', model: 'gemini-4-neon-high', effort: 'high' },
      { channel: 'cli', engine: 'droid', model: 'gpt-6.2-sol', effort: 'high' },
      { channel: 'invoke_subagent', engine: 'agy', model: 'pro', effort: '' },
      { channel: 'swarm', engine: 'agy', model: 'Gemini 4 Neon (Medium)', effort: '' },
    ],
    expert: [
      { channel: 'cli', engine: 'codex', model: 'gpt-6.2-astra', effort: 'xhigh' },
      { channel: 'cli', engine: 'codex', model: 'gpt-6.2-sol', effort: 'xhigh' },
      { channel: 'cli', engine: 'agy', model: 'gemini-4-neon-xhigh', effort: 'xhigh' },
      { channel: 'cli', engine: 'droid', model: 'gpt-6.2-astra', effort: 'xhigh' },
      { channel: 'invoke_subagent', engine: 'agy', model: 'pro', effort: '' },
      { channel: 'swarm', engine: 'agy', model: 'Gemini 4 Neon (High)', effort: '' },
    ],
  };
  const FALLBACK_CHOICES = [
    { channel: 'cli', engine: 'droid', model: 'gemini-3.8-flash', effort: 'high' },
    { channel: 'cli', engine: 'droid', model: 'gemini-4-argon', effort: 'medium' },
    { channel: 'cli', engine: 'droid', model: 'gpt-6.2-sol', effort: 'same' },
    { channel: 'cli', engine: 'droid', model: 'gpt-6.2-astra', effort: 'xhigh' },
    { channel: 'cli', engine: 'droid', model: 'claude-opus-5-5', effort: 'high' },
    { channel: 'cli', engine: 'agy', model: 'gemini-4-neon-medium', effort: 'medium' },
    { channel: 'cli', engine: 'codex', model: 'gpt-6.2-sol', effort: 'high' },
    { channel: 'swarm', engine: 'agy', model: 'Gemini 4 Neon (Medium)', effort: '' },
    { channel: 'swarm', engine: 'agy', model: 'Gemini 4 Neon (High)', effort: '' },
  ];
  const PANEL_MODES = {
    dual: {
      label: '两方 · Astra + Neon',
      seats: [
        { channel: 'cli', engine: 'codex', model: 'gpt-6.2-astra', effort: 'xhigh', enabled: true },
        { channel: 'cli', engine: 'agy', model: 'gemini-4-neon-xhigh', effort: 'xhigh', enabled: true },
        { channel: 'cli', engine: 'agy', model: 'claude-opus-5-5-high', effort: 'high', enabled: false },
        { channel: 'cli', engine: 'grok', model: 'grok-4.7', effort: 'xhigh', enabled: false },
      ],
    },
    quad: {
      label: '四方 · 全引擎会审',
      seats: [
        { channel: 'cli', engine: 'codex', model: 'gpt-6.2-astra', effort: 'xhigh', enabled: true },
        { channel: 'cli', engine: 'agy', model: 'gemini-4-neon-xhigh', effort: 'xhigh', enabled: true },
        { channel: 'cli', engine: 'agy', model: 'claude-opus-5-5-high', effort: 'high', enabled: true },
        { channel: 'cli', engine: 'grok', model: 'grok-4.7', effort: 'xhigh', enabled: true },
      ],
    },
    builtin: {
      label: '内置 · 独立盲审',
      seats: [
        { channel: 'invoke_subagent', engine: 'agy', model: 'flash', effort: '', enabled: true },
        { channel: 'invoke_subagent', engine: 'agy', model: 'flash', effort: '', enabled: true },
      ],
    },
    swarm: {
      label: 'Swarm · 公开多视角',
      seats: [
        { channel: 'swarm', engine: 'agy', model: 'Gemini 4 Neon (High)', effort: '', enabled: true },
        { channel: 'swarm', engine: 'agy', model: 'Gemini 4 Argon (Medium)', effort: '', enabled: true },
      ],
    },
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
  const tKey = t => `${t.channel}|${t.engine}|${t.model}|${t.effort || ''}`;
  const parseKey = k => {
    const [channel, engine, model, effort] = k.split('|');
    return { channel, engine, model, effort: effort || '' };
  };
  function fmtTarget(t, installed) {
    if (t.channel === 'invoke_subagent') {
      const m = { flash: 'Flash', inherit: '继承主模型', pro: 'Pro' }[t.model] || t.model;
      return `内置 · ${m}`;
    }
    if (t.channel === 'swarm') return `Swarm · ${t.model}`;
    const shortM = String(t.model || '').replace(/^gemini-4-/, '').replace(/^gemini-/, '').replace(/^claude-/, '');
    const eff = EFF[t.effort || ''];
    const suffix = eff && !shortM.endsWith('-' + (t.effort || '')) ? ` (${eff})` : '';
    const miss = installed && !installed.includes(t.engine) ? ' · 未装' : '';
    return `${ENG[t.engine] || t.engine} · ${shortM}${suffix}${miss}`;
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
  const drawer = el('dialog', { id: 'dispatchDrawer', 'aria-labelledby': 'dsTitle' });
  const closeBtn = el('button', { type: 'button', className: 'ds-close', 'aria-label': '关闭' }, '✕');
  const warnEl = el('div', { className: 'ds-warn', role: 'alert', hidden: true });
  const fields = el('fieldset', { className: 'ds-fields', 'aria-label': '派发设置' });
  const statusEl = el('div', { className: 'ds-status', role: 'status', 'aria-live': 'polite' });
  const reloadBtn = el('button', { type: 'button', hidden: true }, '重载');
  const resetBtn = el('button', { type: 'button' }, '恢复默认');
  const saveBtn = el('button', { type: 'submit', className: 'ds-save' }, '保存');
  const form = el('form', {},
    el('div', { className: 'ds-body' }, warnEl, fields),
    el('div', { className: 'ds-foot' }, statusEl, el('div', { className: 'ds-acts' }, reloadBtn, resetBtn, saveBtn)));
  drawer.append(el('div', { className: 'ds-head' }, el('h2', { id: 'dsTitle' }, '派发设置'), closeBtn), form);
  document.body.append(drawer);

  const avail = () => saved?.capabilities?.available_engines || [];
  function setMsg(text, tone = '') { statusEl.textContent = text; statusEl.dataset.tone = tone; }
  function sync() {
    const ro = !draft || Boolean(saved?.read_only_warning);
    fields.disabled = busy || ro;
    saveBtn.disabled = fields.disabled || conflicted || !dirty;
    resetBtn.disabled = fields.disabled || conflicted;
    reloadBtn.disabled = busy;
  }
  function markDirty() {
    dirty = true;
    if (!conflicted) setMsg('有未保存修改', 'warn');
    sync();
  }
  function activeMode() {
    if (!draft) return '';
    if (!draft.external_enabled) return 'native';
    if (!draft.codex_fast && draft.quota_fallback?.threshold_pct >= 15) return 'eco';
    return 'auto';
  }
  function applyPreset(kind) {
    const has = avail(), useCli = kind !== 'native' && has.length > 0, eco = kind === 'eco';
    draft.enabled = true;
    draft.external_enabled = useCli;
    draft.codex_fast = useCli && has.includes('codex') && !eco;
    draft.quota_fallback = { enabled: true, threshold_pct: eco ? 20 : 5 };
    const pick = (role, preferNative) => {
      if (!useCli || preferNative) {
        return ROLE_CHOICES[role].find(c => c.channel === 'invoke_subagent') || ROLE_CHOICES[role][0];
      }
      return ROLE_CHOICES[role].find(c => c.channel === 'cli' && has.includes(c.engine))
        || ROLE_CHOICES[role].find(c => c.channel === 'invoke_subagent');
    };
    draft.roles.scout.primary = { ...pick('scout', eco) };
    draft.roles.standard.primary = { ...pick('standard', eco) };
    draft.roles.deep.primary = { ...pick('deep', false) };
    draft.roles.expert.primary = { ...pick('expert', false) };
    draft.roles.panel.seats = structuredClone((useCli ? PANEL_MODES.dual : PANEL_MODES.builtin).seats);
    markDirty(); render();
  }

  function buildTargetSelect(id, label, current, choices, onSelect) {
    const has = avail();
    const list = choices.filter(c => c.channel !== 'cli' || has.includes(c.engine));
    const curKey = current ? tKey(current) : '__off__';
    const sel = el('select', { id, 'aria-label': label });
    const seen = new Set();
    if (choices === FALLBACK_CHOICES) {
      sel.append(el('option', { value: '__off__' }, '停用（不回退）'));
      seen.add('__off__');
    }
    for (const c of list) {
      const k = tKey(c);
      if (seen.has(k)) continue;
      seen.add(k);
      sel.append(el('option', { value: k }, fmtTarget(c, has)));
    }
    if (current && !seen.has(curKey)) {
      sel.prepend(el('option', { value: curKey }, fmtTarget(current, has)));
    }
    sel.value = curKey;
    sel.addEventListener('change', () => { onSelect(sel.value); markDirty(); });
    return sel;
  }

  function render() {
    if (!draft) return;
    const p = draft, has = avail(), mode = activeMode();
    warnEl.hidden = !saved.read_only_warning;
    warnEl.textContent = warnEl.hidden ? '' : '配置文件异常或来自更高版本，当前只读展示默认值。';

    const modeBar = el('div', { className: 'ds-modes', role: 'group', 'aria-label': '快捷模式' },
      ...[['auto', has.length ? '多引擎' : '自动'], ['native', '纯内置'], ['eco', '省配额']].map(([k, txt]) => {
        const b = el('button', { type: 'button', className: mode === k ? 'is-on' : '' }, txt);
        b.addEventListener('click', () => applyPreset(k));
        return b;
      }));

    const roleList = el('div', { className: 'ds-list' });
    for (const [role, name] of ROLES) {
      const sid = 'ds-role-' + role;
      const sel = buildTargetSelect(sid, name, p.roles[role].primary, ROLE_CHOICES[role], v => {
        p.roles[role].primary = parseKey(v);
      });
      const row = el('div', { className: 'ds-row' }, el('label', { for: sid }, name), sel);
      const res = saved.resolved?.[role];
      if (!dirty && res && (res.reason === 'fallback' || res.reason === 'builtin_fallback')) {
        row.append(el('div', { className: 'ds-note' }, `↪ 生效：${fmtTarget(res)}`));
      }
      roleList.append(row);
    }

    const pMode = detectPanelMode(p.roles.panel.seats);
    const pSel = el('select', { id: 'ds-role-panel', 'aria-label': '多方会审' });
    for (const [k, m] of Object.entries(PANEL_MODES)) {
      if ((k === 'dual' || k === 'quad') && !has.length && pMode !== k) continue;
      pSel.append(el('option', { value: k }, m.label));
    }
    if (pMode === 'custom') pSel.prepend(el('option', { value: 'custom' }, '自定义席位'));
    pSel.value = pMode;
    pSel.addEventListener('change', () => {
      if (PANEL_MODES[pSel.value]) {
        p.roles.panel.seats = structuredClone(PANEL_MODES[pSel.value].seats);
        if (pSel.value === 'swarm') p.allow_swarm = true;
        markDirty(); render();
      }
    });
    roleList.append(el('div', { className: 'ds-row' }, el('label', { for: 'ds-role-panel' }, '多方会审'), pSel));

    const advSum = [
      p.quota_fallback.enabled ? `配额 ≤${p.quota_fallback.threshold_pct}%` : '不自动回退',
      p.codex_fast ? 'Fast' : '',
      p.allow_swarm ? 'Swarm' : '',
    ].filter(Boolean).join(' · ');

    const thrInput = el('input', {
      type: 'number', min: 0, max: 50, step: 1, value: p.quota_fallback.threshold_pct, 'aria-label': '配额回退阈值百分比',
    });
    thrInput.addEventListener('input', () => {
      const n = Number(thrInput.value);
      if (Number.isInteger(n) && n >= 0 && n <= 50) { p.quota_fallback.threshold_pct = n; markDirty(); }
    });
    const mkSw = (label, checked, onSet) => {
      const cb = el('input', { type: 'checkbox', checked });
      cb.addEventListener('change', () => { onSet(cb.checked); markDirty(); });
      return el('label', { className: 'ds-sw' }, cb, label);
    };
    const swGrid = el('div', { className: 'ds-sw-grid' },
      mkSw('启用派发', p.enabled, v => { p.enabled = v; }),
      mkSw('外部 CLI', p.external_enabled, v => { p.external_enabled = v; }),
      mkSw('Codex Fast', p.codex_fast, v => { p.codex_fast = v; }),
      mkSw('允许 Swarm', p.allow_swarm, v => { p.allow_swarm = v; }),
      el('div', { className: 'ds-sw-thr' },
        mkSw('配额自动回退', p.quota_fallback.enabled, v => { p.quota_fallback.enabled = v; }),
        el('span', { className: 'ds-thr-box' }, '≤', thrInput, '%')));

    const fbList = el('div', { className: 'ds-list' }, el('div', { className: 'ds-subhdr' }, '模型回退（主力 → 备用）'));
    for (const [key, chain] of Object.entries(p.fallbacks || {})) {
      const firstOn = (chain || []).find(c => c && c.enabled !== false) || null;
      const lbl = FALLBACK_LABELS[key] || key.replace(':', ' · ');
      const fid = 'ds-fb-' + key.replace(/[^a-z0-9]/gi, '_');
      const sel = buildTargetSelect(fid, lbl + ' 回退模型', firstOn, FALLBACK_CHOICES, v => {
        if (v === '__off__') {
          p.fallbacks[key] = (chain || []).map(c => ({ ...c, enabled: false }));
        } else {
          const picked = { ...parseKey(v), enabled: true };
          const rest = (chain || []).filter(c => tKey(c) !== v);
          p.fallbacks[key] = [picked, ...rest];
        }
      });
      fbList.append(el('div', { className: 'ds-row ds-fb-row' }, el('label', { for: fid, title: key }, lbl), sel));
    }

    const adv = el('details', { className: 'ds-adv', open: advOpen },
      el('summary', {}, el('span', {}, '回退与开关'), el('span', { className: 'ds-adv-sum' }, advSum)),
      el('div', { className: 'ds-adv-body' }, swGrid, fbList));
    adv.addEventListener('toggle', () => { advOpen = adv.open; });

    fields.replaceChildren(modeBar, roleList, adv);
    sync();
  }

  async function api(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) {
      const msg = res.status === 403 ? '面板已重载，请刷新后重试' : (data?.error || `请求失败 (${res.status})`);
      throw Object.assign(new Error(msg), { status: res.status });
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
    } catch (e) { setMsg(e.message, 'err'); }
    finally { busy = false; sync(); }
  }
  async function write(isReset) {
    busy = true; sync(); setMsg(isReset ? '恢复中…' : '保存中…');
    try {
      const body = isReset ? { revision: saved.policy.revision } : draft;
      await api(isReset ? endpoint + '/reset' : endpoint, {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'X-Cockpit-CSRF': csrf },
        body: JSON.stringify(body),
      });
      await load();
      setMsg(isReset ? '已恢复默认' : '已保存', 'ok');
    } catch (e) {
      if (e.status === 409) {
        conflicted = true; reloadBtn.hidden = false;
        setMsg('配置已变更，请先重载', 'err');
      } else setMsg(e.message, 'err');
    } finally { busy = false; sync(); }
  }

  closeBtn.addEventListener('click', () => drawer.close());
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
    if (e.target !== drawer) return;
    const r = drawer.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX >= r.right || e.clientY < r.top || e.clientY >= r.bottom) drawer.close();
  });
  drawer.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); drawer.close(); } });
  drawer.addEventListener('close', () => { trigger.setAttribute('aria-expanded', 'false'); trigger.focus(); });
  sync();
})();
