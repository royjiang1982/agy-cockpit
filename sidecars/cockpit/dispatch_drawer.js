(() => {
  'use strict';

  const trigger = document.getElementById('dispatchSettingsBtn');
  if (!trigger) return;
  const endpoint = '/api/dispatch-policy';
  const csrf = document.querySelector('meta[name="cockpit-csrf"]')?.content || '';
  const ENG = {
    agy: { label: 'AGY', brand: 'agy', channel: 'cli' }, codex: { label: 'Codex', brand: 'codex', channel: 'cli' },
    droid: { label: 'Droid', brand: 'droid', channel: 'cli' }, grok: { label: 'Grok', brand: 'grok', channel: 'cli' },
    native: { label: '内置', brand: 'native', channel: 'invoke_subagent' }, swarm: { label: 'Swarm', brand: 'swarm', channel: 'swarm' },
  };
  const EFF = { '': '', low: '低', medium: '中', high: '高', xhigh: '极高', max: 'Max', ultra: 'Ultra', same: '同档' };
  const SWARM_TIER = { low: 'Low', medium: 'Medium', high: 'High', xhigh: 'XHigh', max: 'Max' };
  const SKIP_WHY = { quota_low: '配额低', cli_unavailable: '未装', external_disabled: 'CLI已关', swarm_disabled: 'Swarm已关' };
  const ROLES = [['scout', '快搜侦察', true], ['standard', '常规制作', false], ['deep', '深度执行', false], ['expert', '专家验收', false]];
  const E3 = ['low', 'medium', 'high'], E4 = ['low', 'medium', 'high', 'xhigh'];
  const E4M = ['low', 'medium', 'high', 'max'], E5 = ['low', 'medium', 'high', 'xhigh', 'max'], E6 = [...E5, 'ultra'];

  const mkAgy = (id, label, prefix, efforts, brand = 'agy') => ({ id, label, brand, efforts, toModel: e => `${prefix}-${e || efforts[efforts.length - 1]}`, match: m => m === prefix || m.startsWith(prefix + '-') });
  const mkSwarm = (id, label, base, efforts, brand = 'swarm') => ({ id, label, brand, efforts, swarmBase: base, toModel: e => `${base} (${SWARM_TIER[e] || 'High'})`, match: m => m.toLowerCase().startsWith(base.toLowerCase()) });
  const m = (id, label, efforts, brand) => (brand ? { id, label, efforts, brand } : { id, label, efforts });
  const CATALOG = {
    agy: [mkAgy('hydrogen', 'Hydrogen', 'gemini-4-hydrogen', E4), mkAgy('neon', 'Neon', 'gemini-4-neon', E4), mkAgy('argon', 'Argon', 'gemini-4-argon', E3), mkAgy('carbon', 'Carbon', 'gemini-4-carbon', E3), mkAgy('flash38', 'Flash 3.8', 'gemini-3.8-flash', E3), mkAgy('opus55', 'Opus 5.5', 'claude-opus-5-5', E4M, 'claude'), mkAgy('sonnet55', 'Sonnet 5.5', 'claude-sonnet-5-5', E4M, 'claude')],
    codex: [m('gpt-6.2-sol', 'gpt-6.2-sol', E6), m('gpt-6.2-astra', 'gpt-6.2-astra', E6), m('gpt-6.1-terra', 'gpt-6.1-terra', E6), m('gpt-6-luna', 'gpt-6-luna', E5)],
    grok: [m('grok-4.7', 'grok-4.7', E4), m('grok-4.7-build-fast', 'grok-4.7-fast', E4), m('grok-4.6', 'grok-4.6', E4), m('grok-4.5', 'grok-4.5', E4)],
    // Droid mirrors only models present in other engines (AGY + Codex), no extras.
    droid: [m('gemini-3.8-flash', 'Flash 3.8', E3, 'agy'), m('gemini-4-argon', 'Argon', E3, 'agy'), m('claude-opus-5-5', 'Opus 5.5', E4M, 'claude'), m('claude-sonnet-5-5', 'Sonnet 5.5', E4M, 'claude'), m('gpt-6.2-sol', 'gpt-6.2-sol', E6, 'codex'), m('gpt-6.2-astra', 'gpt-6.2-astra', E6, 'codex'), m('gpt-6.1-terra', 'gpt-6.1-terra', E6, 'codex'), m('gpt-6-luna', 'gpt-6-luna', E5, 'codex')],
    native_ro: [m('flash', 'Flash', [])],
    native_rw: [m('inherit', '继承主模型', []), m('pro', 'Pro', [])],
    swarm: [mkSwarm('s_hydrogen', 'Hydrogen', 'Gemini 4 Hydrogen', E4, 'agy'), mkSwarm('s_neon', 'Neon', 'Gemini 4 Neon', E4, 'agy'), mkSwarm('s_argon', 'Argon', 'Gemini 4 Argon', E3, 'agy'), mkSwarm('s_carbon', 'Carbon', 'Gemini 4 Carbon', E3, 'agy'), mkSwarm('s_flash38', 'Flash 3.8', 'Gemini 3.8 Flash', E3, 'agy'), mkSwarm('s_opus55', 'Opus 5.5', 'Claude Opus 5.5', E4M, 'claude'), mkSwarm('s_sonnet55', 'Sonnet 5.5', 'Claude Sonnet 5.5', E4M, 'claude')],
  };
  const t = (engine, model, effort = '', channel = 'cli', extra = {}) => ({ channel, engine, model, effort, ...extra });
  const DEFAULT_SEAT_POOL = [
    t('codex', 'gpt-6.2-astra', 'xhigh', 'cli', { enabled: true }), t('agy', 'gemini-4-neon-xhigh', 'xhigh', 'cli', { enabled: true }),
    t('agy', 'claude-opus-5-5-high', 'high', 'cli', { enabled: true }), t('grok', 'grok-4.7', 'xhigh', 'cli', { enabled: true }),
    t('droid', 'gpt-6.2-astra', 'xhigh', 'cli', { enabled: true }), t('agy', 'flash', '', 'invoke_subagent', { enabled: true }),
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
  const engKeyOf = x => x.channel === 'invoke_subagent' ? 'native' : x.channel === 'swarm' ? 'swarm' : x.engine;
  const modelsFor = (ek, ro) => ek === 'native' ? (ro ? CATALOG.native_ro : CATALOG.native_rw) : (CATALOG[ek] || []);
  const findSpec = (ek, m, ro) => modelsFor(ek, ro).find(s => (s.match ? s.match(m) : s.id === m));
  function effortOf(x, spec) {
    if (x.channel === 'swarm') {
      const m = /\((Low|Medium|High|XHigh|Max)\)$/i.exec(x.model || '');
      return m ? m[1].toLowerCase() : (spec?.efforts?.[1] || 'high');
    }
    if (x.effort) return x.effort;
    const m = /-(low|medium|high|xhigh|max|ultra)$/i.exec(x.model || '');
    return m ? m[1].toLowerCase() : '';
  }
  const buildTarget = (ek, spec, effort, extra = {}) => {
    const meta = ENG[ek] || { channel: 'cli' };
    const eff = effort === 'same' ? 'same' : (spec.efforts.includes(effort) ? effort : (spec.efforts.includes('high') ? 'high' : (spec.efforts[spec.efforts.length - 1] || '')));
    if (meta.channel === 'swarm') return t('agy', spec.toModel ? spec.toModel(eff) : spec.id, '', 'swarm', extra);
    if (meta.channel === 'invoke_subagent') return t('agy', spec.id, '', 'invoke_subagent', extra);
    return t(ek, spec.toModel ? spec.toModel(eff === 'same' ? 'high' : eff) : spec.id, eff, 'cli', extra);
  };
  const fmtTarget = x => {
    const ek = engKeyOf(x), em = ENG[ek] || { label: ek, brand: 'agy' };
    const spec = findSpec(ek, x.model, false) || findSpec(ek, x.model, true);
    const lbl = spec ? spec.label : String(x.model || '').replace(/^(?:gemini-4-|gemini-|claude-)/, '').replace(/-(?:low|medium|high|xhigh|max|ultra)$/, '');
    const eff = EFF[effortOf(x, spec)];
    const brand = ek === 'droid' ? 'droid' : (spec?.brand || (ek === 'agy' && /claude|opus|sonnet/i.test(x.model || '') ? 'claude' : em.brand));
    return { eng: em.label, short: lbl, text: eff && ek !== 'native' ? `${lbl} · ${eff}` : lbl, brand };
  };

  let saved = null, draft = null, busy = false, dirty = false, conflicted = false;
  let view = { type: 'home' };

  const drawer = el('dialog', { id: 'dispatchDrawer', 'aria-labelledby': 'dsTitle' });
  const backBtn = el('button', { type: 'button', className: 'ds-back', hidden: true }, '‹ 返回');
  const titleEl = el('h2', { id: 'dsTitle' }, '派发设置');
  const masterCb = el('input', { type: 'checkbox', className: 'ds-switch', 'aria-label': '启用子代理派发', title: '启用或暂停派发' });
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
      el('div', { className: 'ds-head-left' }, backBtn, titleEl),
      el('div', { className: 'ds-head-right' }, masterCb, closeBtn)),
    form);
  document.body.append(drawer);

  const avail = () => saved?.capabilities?.available_engines || [];
  const enabledSeats = p => (p.roles.panel.seats || []).filter(s => s && s.enabled !== false);
  function syncSwitches(p) {
    const seats = enabledSeats(p);
    p.external_enabled = ROLES.some(([r]) => p.roles[r].primary.channel === 'cli') || seats.some(s => s.channel === 'cli');
    if (ROLES.some(([r]) => p.roles[r].primary.channel === 'swarm') || seats.some(s => s.channel === 'swarm')) p.allow_swarm = true;
  }
  function setMsg(text, tone = '') {
    if (tone === 'err') { warnEl.hidden = false; warnEl.dataset.tone = 'err'; warnEl.textContent = text; statusEl.textContent = ''; statusEl.dataset.tone = ''; }
    else { if (!saved?.read_only_warning) { warnEl.hidden = true; warnEl.dataset.tone = ''; } statusEl.textContent = text; statusEl.dataset.tone = tone; }
  }
  function sync() {
    const ro = !draft || Boolean(saved?.read_only_warning);
    fields.disabled = busy || ro;
    masterCb.disabled = fields.disabled;
    masterCb.checked = Boolean(draft?.enabled);
    saveBtn.disabled = fields.disabled || conflicted || !dirty;
    resetBtn.disabled = fields.disabled || conflicted || (!saved?.exists && !dirty);
    reloadBtn.disabled = busy;
  }
  const markDirty = () => { dirty = true; if (!conflicted) setMsg('未保存', 'warn'); sync(); };
  const go = nextView => { view = nextView; bodyEl.scrollTop = 0; render(); };

  function renderTargetEditor(target, { readOnly, allowNative = true, allowSwarm = true, allowSame = false, onChange }) {
    const has = avail(), ek = engKeyOf(target), em = ENG[ek] || { label: ek, brand: 'agy' };
    const spec = findSpec(ek, target.model, readOnly) || { id: target.model, label: fmtTarget(target).short, efforts: target.effort ? E4 : [] };
    const curEffRaw = effortOf(target, spec);
    const card = el('div', { className: 'ds-card' });

    const engKeys = [...['agy', 'codex', 'droid', 'grok'].filter(e => has.includes(e) || e === ek), ...(allowNative ? ['native'] : []), ...(allowSwarm ? ['swarm'] : [])];
    card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '引擎'),
      el('div', { className: 'ds-chips' }, ...engKeys.map(k => {
        const miss = ENG[k].channel === 'cli' && !has.includes(k);
        const btn = el('button', { type: 'button', className: k === ek ? 'ds-chip is-on' : 'ds-chip' },
          dot(ENG[k].brand), ENG[k].label, miss ? el('span', { className: 'ds-chip-meta' }, '未装') : '');
        btn.addEventListener('click', () => {
          if (k === ek) return;
          const list = modelsFor(k, readOnly);
          if (list.length) onChange(buildTarget(k, list[0], curEffRaw || 'high'));
        });
        return btn;
      }))));

    const modList = modelsFor(ek, readOnly);
    const modItems = modList.some(m => m.id === spec.id) ? modList : [spec, ...modList];
    card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '模型'),
      el('div', { className: 'ds-chips' }, ...modItems.map(m => {
        const btn = el('button', { type: 'button', className: m.id === spec.id ? 'ds-chip is-on' : 'ds-chip' }, dot(m.brand || em.brand), m.label);
        btn.addEventListener('click', () => { if (m.id !== spec.id) onChange(buildTarget(ek, m, curEffRaw || 'high')); });
        return btn;
      }))));

    if (spec.efforts.length) {
      const effs = allowSame ? ['same', ...spec.efforts] : spec.efforts;
      const curEff = effs.includes(curEffRaw) ? curEffRaw : effs[0];
      card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '推理档位'),
        el('div', { className: 'ds-seg' }, ...effs.map(e => {
          const btn = el('button', { type: 'button', className: e === curEff ? 'is-on' : '' }, EFF[e] || e);
          btn.addEventListener('click', () => { if (e !== curEff) onChange(buildTarget(ek, spec, e)); });
          return btn;
        }))));
    }
    return card;
  }

  function renderHome(p) {
    const roleCard = el('div', { className: p.enabled ? 'ds-card' : 'ds-card ds-dimmed' });
    for (const [role, name] of ROLES) {
      const f = fmtTarget(p.roles[role].primary), res = saved.resolved?.[role];
      let note = '';
      if (!dirty && p.enabled && res && (res.reason === 'fallback' || res.reason === 'builtin_fallback')) {
        const rf = fmtTarget(res), why = SKIP_WHY[res.skipped?.[0]?.reason];
        note = el('span', { className: 'ds-hint' }, `${why ? why + ' → ' : '↪ '}${rf.eng} ${rf.text}`);
      }
      const row = el('button', { type: 'button', className: 'ds-row' },
        el('span', { className: 'ds-lbl' }, name),
        el('span', { className: 'ds-eng-col' }, dot(f.brand), el('span', { className: 'ds-val-eng' }, f.eng)),
        el('span', { className: 'ds-val-txt' }, f.text),
        el('span', { className: 'ds-arrow' }, '›'),
        note);
      row.addEventListener('click', () => go({ type: 'role', role }));
      roleCard.append(row);
    }

    const seats = enabledSeats(p);
    const preview = seats.slice(0, 2).map(s => fmtTarget(s).short).join(' · ') + (seats.length > 2 ? ` +${seats.length - 2}` : '');
    const pRow = el('button', { type: 'button', className: 'ds-row' },
      el('span', { className: 'ds-lbl' }, '多方会审'),
      el('span', { className: 'ds-eng-col' }, seats[0] ? dot(fmtTarget(seats[0]).brand) : '', el('span', { className: 'ds-val-eng' }, `${seats.length}席`)),
      el('span', { className: 'ds-val-txt' }, preview || '未配置'),
      el('span', { className: 'ds-arrow' }, '›'));
    pRow.addEventListener('click', () => go({ type: 'panel' }));
    roleCard.append(pRow);

    const optCard = el('div', { className: p.enabled ? 'ds-card' : 'ds-card ds-dimmed' });
    const fastCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: p.codex_fast, 'aria-label': 'Codex Fast 模式' });
    fastCb.addEventListener('change', () => { p.codex_fast = fastCb.checked; markDirty(); });
    optCard.append(el('label', { className: 'ds-item ds-item-click' }, el('span', { className: 'ds-lbl-sub' }, 'Codex Fast 模式'), fastCb));

    const thrVal = el('span', {}, `≤${p.quota_fallback.threshold_pct}%`);
    const minus = el('button', { type: 'button', disabled: p.quota_fallback.threshold_pct <= 0, 'aria-label': '降低阈值' }, '−');
    const plus = el('button', { type: 'button', disabled: p.quota_fallback.threshold_pct >= 50, 'aria-label': '提高阈值' }, '+');
    const stepBox = el('div', { className: p.quota_fallback.enabled ? 'ds-step' : 'ds-step is-off' }, minus, thrVal, plus);
    const nudge = d => {
      const next = Math.min(50, Math.max(0, p.quota_fallback.threshold_pct + d));
      if (next === p.quota_fallback.threshold_pct) return;
      p.quota_fallback.threshold_pct = next; thrVal.textContent = `≤${next}%`;
      minus.disabled = next <= 0; plus.disabled = next >= 50; markDirty();
    };
    minus.addEventListener('click', () => nudge(-5));
    plus.addEventListener('click', () => nudge(5));
    const qCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: p.quota_fallback.enabled, 'aria-label': '低配额自动回退' });
    qCb.addEventListener('change', () => { p.quota_fallback.enabled = qCb.checked; stepBox.className = qCb.checked ? 'ds-step' : 'ds-step is-off'; markDirty(); });
    optCard.append(el('div', { className: 'ds-item' }, el('span', { className: 'ds-lbl-sub' }, '低配额自动回退'), el('div', { className: 'ds-thr-wrap' }, stepBox, qCb)));

    return [...(!p.enabled ? [el('div', { className: 'ds-banner' }, '派发已暂停，任务将由主代理直接执行。')] : []), roleCard, optCard];
  }

  function renderRoleDetail(p, role) {
    const [, , readOnly] = ROLES.find(r => r[0] === role), cur = p.roles[role].primary;
    const kids = [renderTargetEditor(cur, {
      readOnly, allowNative: true, allowSwarm: !readOnly,
      onChange: next => { p.roles[role].primary = next; syncSwitches(p); markDirty(); render(); },
    })];
    if (cur.channel !== 'cli') return [...kids, el('div', { className: 'ds-caption' }, '当前使用内置通道，无需额外配置外部模型回退。')];

    const fbKey = `${cur.engine}:${cur.model}`, chain = p.fallbacks[fbKey] || [];
    const firstOn = chain.find(c => c && c.enabled !== false) || null;
    const fbCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: Boolean(firstOn), 'aria-label': '开启模型回退' });
    fbCb.addEventListener('change', () => {
      if (!fbCb.checked) p.fallbacks[fbKey] = chain.map(c => ({ ...c, enabled: false }));
      else if (chain.length) p.fallbacks[fbKey] = chain.map((c, i) => ({ ...c, enabled: i === 0 ? true : c.enabled }));
      else {
        const defEng = avail().includes('droid') ? 'droid' : (avail()[0] || 'droid');
        p.fallbacks[fbKey] = [buildTarget(defEng, modelsFor(defEng, false)[0], cur.effort || 'high', { enabled: true })];
      }
      markDirty(); render();
    });
    kids.push(el('div', { className: 'ds-card' }, el('label', { className: 'ds-item ds-item-click' }, el('span', { className: 'ds-lbl' }, '开启回退'), fbCb)));
    if (firstOn) {
      kids.push(renderTargetEditor(firstOn, {
        readOnly: false, allowNative: false, allowSwarm: false, allowSame: true,
        onChange: next => {
          const rest = chain.filter(c => !(c.channel === firstOn.channel && c.engine === firstOn.engine && c.model === firstOn.model));
          p.fallbacks[fbKey] = [{ ...next, enabled: true }, ...rest];
          markDirty(); render();
        },
      }));
    }
    return kids;
  }

  function renderPanelList(p) {
    const seats = enabledSeats(p), listCard = el('div', { className: 'ds-card' });
    seats.forEach((seat, idx) => {
      const f = fmtTarget(seat);
      const main = el('button', { type: 'button', className: 'ds-seat-main' },
        el('span', { className: 'ds-eng-col' }, dot(f.brand), el('span', { className: 'ds-val-eng' }, f.eng)),
        el('span', { className: 'ds-val-txt' }, f.text));
      main.addEventListener('click', () => go({ type: 'seat', index: idx }));
      const move = dir => {
        const copy = seats.map(s => ({ ...s, enabled: true }));
        [copy[idx], copy[idx + dir]] = [copy[idx + dir], copy[idx]];
        p.roles.panel.seats = copy; markDirty(); render();
      };
      const up = el('button', { type: 'button', className: 'ds-icon-btn', disabled: idx === 0, 'aria-label': '上移' }, '↑');
      const down = el('button', { type: 'button', className: 'ds-icon-btn', disabled: idx === seats.length - 1, 'aria-label': '下移' }, '↓');
      const del = el('button', { type: 'button', className: 'ds-icon-btn is-del', disabled: seats.length <= 1, 'aria-label': '删除席位' }, '×');
      up.addEventListener('click', () => move(-1));
      down.addEventListener('click', () => move(1));
      del.addEventListener('click', () => { p.roles.panel.seats = seats.filter((_, i) => i !== idx).map(s => ({ ...s, enabled: true })); syncSwitches(p); markDirty(); render(); });
      listCard.append(el('div', { className: 'ds-seat-row' }, el('span', { className: 'ds-rank' }, String(idx + 1)), main, el('div', { className: 'ds-seat-acts' }, up, down, del)));
    });
    const addBtn = el('button', { type: 'button', className: 'ds-add-btn' }, '+ 添加席位');
    addBtn.addEventListener('click', () => {
      const has = avail();
      const nextSeat = DEFAULT_SEAT_POOL.find(c => (c.channel !== 'cli' || has.includes(c.engine))
        && !seats.some(s => s.channel === c.channel && s.engine === c.engine && s.model === c.model))
        || t('agy', 'flash', '', 'invoke_subagent', { enabled: true });
      const nextList = [...seats.map(s => ({ ...s, enabled: true })), { ...nextSeat, enabled: true }];
      p.roles.panel.seats = nextList; syncSwitches(p); markDirty(); go({ type: 'seat', index: nextList.length - 1 });
    });
    return [el('div', { className: 'ds-caption' }, '按顺序选用：单方取 #1、两方取前 2 个，不可用自动顺延下一席位。'), listCard, addBtn];
  }

  function renderSeatDetail(p, index) {
    const seats = enabledSeats(p);
    if (!seats[index]) { view = { type: 'panel' }; return renderPanelList(p); }
    const kids = [renderTargetEditor(seats[index], {
      readOnly: true, allowNative: true, allowSwarm: true,
      onChange: next => {
        const copy = seats.map(s => ({ ...s, enabled: true }));
        copy[index] = { ...next, enabled: true };
        p.roles.panel.seats = copy; syncSwitches(p); markDirty(); render();
      },
    })];
    if (seats.length > 1) {
      const delBtn = el('button', { type: 'button', className: 'ds-danger-btn' }, '移除此席位');
      delBtn.addEventListener('click', () => { p.roles.panel.seats = seats.filter((_, i) => i !== index).map(s => ({ ...s, enabled: true })); syncSwitches(p); markDirty(); go({ type: 'panel' }); });
      kids.push(delBtn);
    }
    return kids;
  }

  function render() {
    if (!draft) return;
    const prevScroll = bodyEl.scrollTop, p = draft, isHome = view.type === 'home';
    if (saved.read_only_warning) { warnEl.hidden = false; warnEl.dataset.tone = ''; warnEl.textContent = '配置文件异常或来自更高版本，当前只读展示默认值。'; }
    backBtn.hidden = isHome;
    masterCb.hidden = !isHome;
    backBtn.textContent = view.type === 'seat' ? '‹ 多方会审' : '‹ 返回';
    titleEl.textContent = isHome ? '派发设置' : view.type === 'role' ? (ROLES.find(r => r[0] === view.role)?.[1] || view.role) : view.type === 'panel' ? '多方会审' : `席位 #${view.index + 1}`;
    fields.className = isHome ? 'ds-fields' : 'ds-fields is-sub';
    const kids = isHome ? renderHome(p) : view.type === 'role' ? renderRoleDetail(p, view.role) : view.type === 'panel' ? renderPanelList(p) : renderSeatDetail(p, view.index);
    fields.replaceChildren(...kids);
    bodyEl.scrollTop = prevScroll;
    sync();
  }

  async function api(url, opts) {
    const res = await fetch(url, opts), data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) throw Object.assign(new Error(res.status === 403 ? '面板已重载，请刷新后重试' : (data?.error || `请求失败 (${res.status})`)), { status: res.status });
    return data;
  }
  async function load() {
    busy = true; sync(); setMsg('读取中…');
    try {
      saved = await api(endpoint + '?resolve=1', { cache: 'no-store' });
      draft = structuredClone(saved.policy);
      dirty = false; conflicted = false; reloadBtn.hidden = true;
      render(); setMsg(''); return true;
    } catch (e) { setMsg(e.message, 'err'); return false; }
    finally { busy = false; sync(); }
  }
  async function write(isReset) {
    if (!saved) return;
    if (isReset && !saved.exists) { draft = structuredClone(saved.policy); dirty = false; view = { type: 'home' }; render(); setMsg('已还原', 'ok'); return; }
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

  backBtn.addEventListener('click', () => go(view.type === 'seat' ? { type: 'panel' } : { type: 'home' }));
  masterCb.addEventListener('change', () => { if (!draft) return; draft.enabled = masterCb.checked; markDirty(); render(); });
  closeBtn.addEventListener('click', () => drawer.close());
  reloadBtn.addEventListener('click', () => load());
  resetBtn.addEventListener('click', () => write(true));
  form.addEventListener('submit', e => { e.preventDefault(); write(false); });
  trigger.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); });
  trigger.addEventListener('click', e => {
    e.stopPropagation();
    if (drawer.open) return;
    if (document.getElementById('quotaDrawer')?.classList.contains('open')) document.getElementById('qdClose')?.click();
    view = { type: 'home' };
    drawer.showModal();
    trigger.setAttribute('aria-expanded', 'true');
    if (!dirty && !conflicted) load(); else render();
  });
  drawer.addEventListener('click', e => {
    if (e.target !== drawer) return;
    const r = drawer.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX >= r.right || e.clientY < r.top || e.clientY >= r.bottom) drawer.close();
  });
  drawer.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    e.stopPropagation(); e.preventDefault();
    if (view.type === 'seat') go({ type: 'panel' });
    else if (view.type !== 'home') go({ type: 'home' });
    else drawer.close();
  });
  drawer.addEventListener('close', () => { view = { type: 'home' }; trigger.setAttribute('aria-expanded', 'false'); trigger.focus(); });
  sync();
})();
