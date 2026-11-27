(() => {
  'use strict';

  const trigger = document.getElementById('dispatchSettingsBtn');
  if (!trigger) return;
  const endpoint = '/api/dispatch-policy';
  const sidecarToken = (new URLSearchParams(window.location.search).get('token') || '').trim();
  const withToken = url => !sidecarToken ? url : `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(sidecarToken)}`;
  let csrf = document.querySelector('meta[name="cockpit-csrf"]')?.content || '';

  const PRIMARY_ENGINES = ['agy', 'codex', 'grok'];
  const ENG = {
    agy: { label: 'AGY', brand: 'agy' }, codex: { label: 'Codex', brand: 'codex' },
    grok: { label: 'Grok', brand: 'grok' }, droid: { label: 'Droid', brand: 'droid' },
    native: { label: '内置', brand: 'native' }, swarm: { label: 'Swarm', brand: 'swarm' },
  };
  const EFF = { '': '', low: '低', medium: '中', high: '高', xhigh: '极高', max: 'Max', ultra: 'Ultra', same: '同档' };
  const SKIP_WHY = { quota_low: '配额低', cli_unavailable: '未装', external_disabled: 'CLI已关', swarm_disabled: 'Swarm已关' };
  const ROLES = [['scout', '快搜侦察'], ['standard', '视觉任务'], ['deep', '逻辑任务'], ['expert', '专家验收']];
  const E3 = ['low', 'medium', 'high'], E4 = ['low', 'medium', 'high', 'xhigh'];
  const E4M = ['low', 'medium', 'high', 'max'], E5 = ['low', 'medium', 'high', 'xhigh', 'max'], E6 = [...E5, 'ultra'];

  const fam = (id, label, efforts, re, brand) => ({ id, label, efforts, re, ...(brand ? { brand } : {}) });
  const CATALOG = {
    agy: [
      fam('hydrogen', 'Hydrogen', E4, /^hydrogen$|^gemini-[\d.]+-hydrogen(?:-[a-z]+)?$/),
      fam('neon', 'Neon', E4, /^neon$|^gemini-[\d.]+-neon(?:-[a-z]+)?$/),
      fam('argon', 'Argon', E3, /^argon$|^gemini-[\d.]+-argon(?:-[a-z]+)?$/),
      fam('carbon', 'Carbon', E3, /^carbon$|^gemini-[\d.]+-carbon(?:-[a-z]+)?$/),
      fam('flash', 'Flash', E3, /^flash$|^gemini-[\d.]+-flash(?:-[a-z]+)?$/),
      fam('opus', 'Opus', E4M, /^opus$|^claude-opus-[\d.-]+(?:-[a-z]+)?$/, 'claude'),
      fam('sonnet', 'Sonnet', E4M, /^sonnet$|^claude-sonnet-[\d.-]+(?:-[a-z]+)?$/, 'claude'),
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
  const DROID_LABELS = { flash: 'Flash', argon: 'Argon', opus: 'Opus', sonnet: 'Sonnet', sol: 'Sol', astra: 'Astra', terra: 'Terra', luna: 'Luna' };
  const t = (engine, model, effort = '', channel = 'cli', extra = {}) => ({ channel, engine, model, effort, ...extra });
  const DEFAULT_SEAT_POOL = [
    t('codex', 'astra', 'xhigh', 'cli', { enabled: true }), t('agy', 'neon', 'xhigh', 'cli', { enabled: true }),
    t('agy', 'opus', 'high', 'cli', { enabled: true }), t('grok', 'grok', 'xhigh', 'cli', { enabled: true }),
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
  const engKeyOf = x => x.channel === 'invoke_subagent' ? 'native' : x.channel === 'swarm' ? 'swarm' : x.engine;
  const modelsFor = ek => CATALOG[ek] || [];
  const findSpec = (ek, mod) => modelsFor(ek).find(s => s.re.test(String(mod || '').toLowerCase()));
  const effortOf = x => x.effort || (/-?(low|medium|high|xhigh|max|ultra)$/i.exec(x.model || '')?.[1]?.toLowerCase() || '');
  const buildTarget = (ek, spec, effort, extra = {}) => {
    const eff = spec.efforts.includes(effort) ? effort : (spec.efforts.includes('high') ? 'high' : (spec.efforts[spec.efforts.length - 1] || ''));
    return t(ek, spec.id, eff, 'cli', extra);
  };
  function droidFallbackFor(primary, role) {
    const ek = primary.engine, spec = findSpec(ek, primary.model), id = spec?.id || String(primary.model || '').toLowerCase();
    if (ek === 'codex') return t('droid', id, id === 'astra' && primary.effort === 'xhigh' ? 'xhigh' : 'same', 'cli', { enabled: true });
    if (ek === 'grok') return t('droid', 'astra', 'xhigh', 'cli', { enabled: true });
    if (id === 'opus' || id === 'sonnet') return t('droid', id, primary.effort || 'high', 'cli', { enabled: true });
    if (id === 'flash' || (id === 'hydrogen' && role === 'scout')) return t('droid', 'flash', 'high', 'cli', { enabled: true });
    return t('droid', 'argon', 'medium', 'cli', { enabled: true });
  }
  function findFallbackEntry(p, primary) {
    const ek = primary.engine, spec = findSpec(ek, primary.model);
    for (const k of [primary.model, spec?.id].filter(Boolean).map(v => `${ek}:${v}`)) {
      if (k in (p.fallbacks || {})) return [k, p.fallbacks[k] || []];
    }
    if (spec) {
      for (const [k, chain] of Object.entries(p.fallbacks || {})) {
        if (k.startsWith(ek + ':') && spec.re.test(k.slice(ek.length + 1).toLowerCase())) return [k, chain || []];
      }
    }
    return [`${ek}:${spec?.id || primary.model}`, []];
  }
  const fmtTarget = x => {
    const ek = engKeyOf(x), em = ENG[ek] || { label: ek, brand: 'agy' };
    const spec = findSpec(ek, x.model) || (ek === 'droid' ? (findSpec('codex', x.model) || findSpec('agy', x.model)) : null);
    const lbl = spec ? spec.label : (DROID_LABELS[String(x.model || '').toLowerCase()] || String(x.model || ''));
    const eff = EFF[effortOf(x)];
    const brand = ek === 'droid' ? 'droid' : (spec?.brand || (ek === 'agy' && /claude|opus|sonnet/i.test(x.model || '') ? 'claude' : em.brand));
    return { eng: em.label, short: lbl, text: eff && ek !== 'native' && ek !== 'swarm' ? `${lbl} · ${eff}` : lbl, brand };
  };

  let saved = null, draft = null, loading = false, saving = false, pendingSave = false, conflicted = false, msgTimer = null;
  let view = { type: 'home' };

  const drawer = el('dialog', { id: 'dispatchDrawer', 'aria-labelledby': 'dsTitle' });
  const backBtn = el('button', { type: 'button', className: 'ds-back', hidden: true }, '‹ 返回');
  const titleEl = el('h2', { id: 'dsTitle' }, '子代理设置');
  const statusEl = el('span', { className: 'ds-status', role: 'status', 'aria-live': 'polite' });
  const closeBtn = el('button', { type: 'button', className: 'ds-close', 'aria-label': '关闭' }, '✕');
  const warnText = el('span', {});
  const reloadBtn = el('button', { type: 'button', className: 'ds-warn-act', hidden: true }, '重载');
  const warnEl = el('div', { className: 'ds-warn', role: 'alert', hidden: true }, warnText, reloadBtn);
  const fields = el('fieldset', { className: 'ds-fields', 'aria-label': '子代理设置' });
  const bodyEl = el('div', { className: 'ds-body' }, warnEl, fields);
  drawer.append(
    el('div', { className: 'ds-head' }, el('div', { className: 'ds-head-left' }, backBtn, titleEl), el('div', { className: 'ds-head-right' }, statusEl, closeBtn)),
    bodyEl);
  document.body.append(drawer);

  const avail = () => saved?.capabilities?.available_engines || [];
  const hasDroid = () => avail().includes('droid');
  const enabledSeats = p => (p.roles.panel.seats || []).filter(s => s && s.enabled !== false);
  function syncSwitches(p) {
    const seats = enabledSeats(p);
    p.external_enabled = ROLES.some(([r]) => p.roles[r].primary.channel === 'cli') || seats.some(s => s.channel === 'cli');
    p.allow_swarm = ROLES.some(([r]) => p.roles[r].primary.channel === 'swarm') || seats.some(s => s.channel === 'swarm');
    if (!hasDroid()) p.quota_fallback.enabled = false;
  }
  function setMsg(text, tone = '', autoClear = false) {
    clearTimeout(msgTimer);
    if (tone === 'err') {
      warnEl.hidden = false; warnEl.dataset.tone = 'err'; warnText.textContent = text;
      statusEl.textContent = ''; statusEl.dataset.tone = '';
    } else {
      if (!saved?.read_only_warning) { warnEl.hidden = true; warnEl.dataset.tone = ''; reloadBtn.hidden = true; }
      statusEl.textContent = text; statusEl.dataset.tone = tone;
      if (autoClear && text) msgTimer = setTimeout(() => { if (statusEl.textContent === text) { statusEl.textContent = ''; statusEl.dataset.tone = ''; } }, 1600);
    }
  }
  function sync() {
    fields.disabled = loading || conflicted || !draft || Boolean(saved?.read_only_warning);
    reloadBtn.disabled = loading;
  }
  const go = nextView => { view = nextView; bodyEl.scrollTop = 0; render(); };

  function renderTargetEditor(target, { onChange }) {
    const has = avail(), ek = engKeyOf(target), em = ENG[ek] || { label: ek, brand: 'agy' };
    const spec = findSpec(ek, target.model) || { id: target.model, label: fmtTarget(target).short, efforts: target.effort ? E4 : [] };
    const curEffRaw = effortOf(target), card = el('div', { className: 'ds-card' });

    const engKeys = PRIMARY_ENGINES.filter(e => has.includes(e) || e === ek);
    if (!engKeys.includes(ek)) engKeys.unshift(ek);
    if (!engKeys.length) engKeys.push(...PRIMARY_ENGINES);
    card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '引擎'),
      el('div', { className: 'ds-chips' }, ...engKeys.map(k => {
        const miss = PRIMARY_ENGINES.includes(k) && !has.includes(k);
        const btn = el('button', { type: 'button', className: k === ek ? 'ds-chip is-on' : 'ds-chip' },
          dot(ENG[k]?.brand || 'agy'), ENG[k]?.label || k, miss ? el('span', { className: 'ds-chip-meta' }, '未装') : '');
        btn.addEventListener('click', () => { const list = modelsFor(k); if (k !== ek && list.length) onChange(buildTarget(k, list[0], curEffRaw || 'high')); });
        return btn;
      }))));

    const modList = modelsFor(ek), modItems = modList.some(x => x.id === spec.id) ? modList : [spec, ...modList];
    card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '模型'),
      el('div', { className: 'ds-chips' }, ...modItems.map(x => {
        const btn = el('button', { type: 'button', className: x.id === spec.id ? 'ds-chip is-on' : 'ds-chip' }, dot(x.brand || em.brand), x.label);
        btn.addEventListener('click', () => { if (x.id !== spec.id) onChange(buildTarget(ek, x, curEffRaw || 'high')); });
        return btn;
      }))));

    if (spec.efforts.length) {
      const curEff = spec.efforts.includes(curEffRaw) ? curEffRaw : spec.efforts[0];
      card.append(el('div', { className: 'ds-block' }, el('div', { className: 'ds-block-lbl' }, '推理档位'),
        el('div', { className: 'ds-seg' }, ...spec.efforts.map(e => {
          const btn = el('button', { type: 'button', className: e === curEff ? 'is-on' : '' }, EFF[e] || e);
          btn.addEventListener('click', () => { if (e !== curEff) onChange(buildTarget(ek, spec, e)); });
          return btn;
        }))));
    }
    return card;
  }

  function renderHome(p) {
    const masterCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: p.enabled, 'aria-label': '开启子代理编排' });
    masterCb.addEventListener('change', () => { p.enabled = masterCb.checked; commit(); });
    const masterCard = el('div', { className: 'ds-card' },
      el('label', { className: 'ds-item ds-item-click' }, el('span', { className: 'ds-lbl' }, '开启子代理编排'), masterCb));

    const roleCard = el('div', { className: p.enabled ? 'ds-card' : 'ds-card ds-dimmed' });
    for (const [role, name] of ROLES) {
      const f = fmtTarget(p.roles[role].primary), res = saved.resolved?.[role];
      let note = '';
      if (!pendingSave && !saving && p.enabled && res && (res.reason === 'fallback' || res.reason === 'builtin_fallback')) {
        const rf = fmtTarget(res), why = SKIP_WHY[res.skipped?.[0]?.reason];
        note = el('span', { className: 'ds-hint' }, `${why ? why + ' → ' : '↪ '}${rf.eng} ${rf.text}`);
      }
      const row = el('button', { type: 'button', className: 'ds-row' },
        el('span', { className: 'ds-lbl' }, name),
        el('span', { className: 'ds-eng-col' }, dot(f.brand), el('span', { className: 'ds-val-eng' }, f.eng)),
        el('span', { className: 'ds-val-txt' }, f.text), el('span', { className: 'ds-arrow' }, '›'), note);
      row.addEventListener('click', () => go({ type: 'role', role }));
      roleCard.append(row);
    }

    const seats = enabledSeats(p);
    const preview = seats.slice(0, 2).map(s => fmtTarget(s).short).join(' · ') + (seats.length > 2 ? ` +${seats.length - 2}` : '');
    const pRow = el('button', { type: 'button', className: 'ds-row' },
      el('span', { className: 'ds-lbl' }, '多方会审'),
      el('span', { className: 'ds-eng-col' }, seats[0] ? dot(fmtTarget(seats[0]).brand) : '', el('span', { className: 'ds-val-eng' }, `${seats.length}席`)),
      el('span', { className: 'ds-val-txt' }, preview || '未配置'), el('span', { className: 'ds-arrow' }, '›'));
    pRow.addEventListener('click', () => go({ type: 'panel' }));
    roleCard.append(pRow);

    const optCard = el('div', { className: p.enabled ? 'ds-card' : 'ds-card ds-dimmed' });
    const fastCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: p.codex_fast, 'aria-label': 'Codex Fast 模式' });
    fastCb.addEventListener('change', () => { p.codex_fast = fastCb.checked; commit(false); });
    optCard.append(el('label', { className: 'ds-item ds-item-click' }, el('span', { className: 'ds-lbl-sub' }, 'Codex Fast 模式'), fastCb));

    const droidOk = hasDroid(), thrVal = el('span', {}, `≤${p.quota_fallback.threshold_pct}%`);
    const minus = el('button', { type: 'button', disabled: !droidOk || p.quota_fallback.threshold_pct <= 0, 'aria-label': '降低阈值' }, '−');
    const plus = el('button', { type: 'button', disabled: !droidOk || p.quota_fallback.threshold_pct >= 50, 'aria-label': '提高阈值' }, '+');
    const stepBox = el('div', { className: droidOk && p.quota_fallback.enabled ? 'ds-step' : 'ds-step is-off' }, minus, thrVal, plus);
    const nudge = d => {
      const next = Math.min(50, Math.max(0, p.quota_fallback.threshold_pct + d));
      if (next === p.quota_fallback.threshold_pct) return;
      p.quota_fallback.threshold_pct = next; thrVal.textContent = `≤${next}%`;
      minus.disabled = next <= 0; plus.disabled = next >= 50; commit(false);
    };
    minus.addEventListener('click', () => nudge(-5));
    plus.addEventListener('click', () => nudge(5));
    const qCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: droidOk && p.quota_fallback.enabled, disabled: !droidOk, 'aria-label': '低配额自动回退到 Droid' });
    qCb.addEventListener('change', () => { p.quota_fallback.enabled = qCb.checked; stepBox.className = qCb.checked ? 'ds-step' : 'ds-step is-off'; commit(false); });
    optCard.append(el('div', { className: droidOk ? 'ds-item' : 'ds-item is-dis' },
      el('div', { className: 'ds-row-col' }, el('span', { className: 'ds-lbl-sub' }, '低配额自动回退'), droidOk ? '' : el('span', { className: 'ds-fb-sub' }, '未检测到 Droid，已停用')),
      el('div', { className: 'ds-thr-wrap' }, droidOk ? stepBox : '', qCb)));

    return [masterCard, roleCard, optCard];
  }

  function renderRoleDetail(p, role) {
    const cur = p.roles[role].primary, droidOk = hasDroid();
    const kids = [renderTargetEditor(cur, { onChange: next => { p.roles[role].primary = next; syncSwitches(p); commit(); } })];
    const [fbKey, chain] = findFallbackEntry(p, cur);
    const activeFb = chain.find(c => c && c.enabled !== false && c.engine === 'droid') || null;
    const mapped = activeFb || droidFallbackFor(cur, role);
    const fbCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: droidOk && Boolean(activeFb), disabled: !droidOk, 'aria-label': '回退到 Droid' });
    fbCb.addEventListener('change', () => {
      if (!droidOk) return;
      const spec = findSpec(cur.engine, cur.model), canonKey = `${cur.engine}:${spec?.id || cur.model}`;
      if (fbKey !== canonKey) delete p.fallbacks[fbKey];
      p.fallbacks[canonKey] = [{ ...droidFallbackFor(cur, role), enabled: fbCb.checked }];
      commit();
    });
    const sub = droidOk ? `→ Droid ${fmtTarget(mapped).text}` : '未检测到 Droid 引擎，不可开启';
    kids.push(el('div', { className: 'ds-card' },
      el('label', { className: droidOk ? 'ds-item ds-item-click' : 'ds-item is-dis' },
        el('div', { className: 'ds-row-col' }, el('span', { className: 'ds-lbl' }, '回退到 Droid'), el('span', { className: 'ds-fb-sub' }, sub)), fbCb)));
    return kids;
  }

  function renderPanelList(p) {
    const seats = enabledSeats(p), listCard = el('div', { className: 'ds-card' });
    seats.forEach((seat, idx) => {
      const f = fmtTarget(seat);
      const main = el('button', { type: 'button', className: 'ds-seat-main' },
        el('span', { className: 'ds-eng-col' }, dot(f.brand), el('span', { className: 'ds-val-eng' }, f.eng)), el('span', { className: 'ds-val-txt' }, f.text));
      main.addEventListener('click', () => go({ type: 'seat', index: idx }));
      const move = dir => { const copy = seats.map(s => ({ ...s, enabled: true })); [copy[idx], copy[idx + dir]] = [copy[idx + dir], copy[idx]]; p.roles.panel.seats = copy; commit(); };
      const up = el('button', { type: 'button', className: 'ds-icon-btn', disabled: idx === 0, 'aria-label': '上移' }, '↑');
      const down = el('button', { type: 'button', className: 'ds-icon-btn', disabled: idx === seats.length - 1, 'aria-label': '下移' }, '↓');
      const del = el('button', { type: 'button', className: 'ds-icon-btn is-del', disabled: seats.length <= 1, 'aria-label': '删除席位' }, '×');
      up.addEventListener('click', () => move(-1)); down.addEventListener('click', () => move(1));
      del.addEventListener('click', () => { p.roles.panel.seats = seats.filter((_, i) => i !== idx).map(s => ({ ...s, enabled: true })); syncSwitches(p); commit(); });
      listCard.append(el('div', { className: 'ds-seat-row' }, el('span', { className: 'ds-rank' }, String(idx + 1)), main, el('div', { className: 'ds-seat-acts' }, up, down, del)));
    });
    const addBtn = el('button', { type: 'button', className: 'ds-add-btn' }, '+ 添加席位');
    addBtn.addEventListener('click', () => {
      const has = avail(), sameSeat = (a, b) => a.engine === b.engine && (findSpec(a.engine, a.model)?.id || a.model) === (findSpec(b.engine, b.model)?.id || b.model);
      const nextSeat = DEFAULT_SEAT_POOL.find(c => has.includes(c.engine) && !seats.some(s => sameSeat(s, c)))
        || DEFAULT_SEAT_POOL.find(c => has.includes(c.engine)) || DEFAULT_SEAT_POOL[0];
      const nextList = [...seats.map(s => ({ ...s, enabled: true })), { ...nextSeat, enabled: true }];
      p.roles.panel.seats = nextList; syncSwitches(p); view = { type: 'seat', index: nextList.length - 1 }; bodyEl.scrollTop = 0; commit();
    });
    return [el('div', { className: 'ds-caption' }, '按顺序选用：单方取 #1、两方取前 2 个，不可用自动顺延下一席位。'), listCard, addBtn];
  }

  function renderSeatDetail(p, index) {
    const seats = enabledSeats(p);
    if (!seats[index]) { view = { type: 'panel' }; return renderPanelList(p); }
    const kids = [renderTargetEditor(seats[index], {
      onChange: next => { const copy = seats.map(s => ({ ...s, enabled: true })); copy[index] = { ...next, enabled: true }; p.roles.panel.seats = copy; syncSwitches(p); commit(); },
    })];
    if (seats.length > 1) {
      const delBtn = el('button', { type: 'button', className: 'ds-danger-btn' }, '移除此席位');
      delBtn.addEventListener('click', () => { p.roles.panel.seats = seats.filter((_, i) => i !== index).map(s => ({ ...s, enabled: true })); syncSwitches(p); view = { type: 'panel' }; bodyEl.scrollTop = 0; commit(); });
      kids.push(delBtn);
    }
    return kids;
  }

  function render() {
    if (!draft) return;
    const prevScroll = bodyEl.scrollTop, p = draft, isHome = view.type === 'home';
    if (saved.read_only_warning) { warnEl.hidden = false; warnEl.dataset.tone = ''; warnText.textContent = '配置文件异常或来自更高版本，当前只读展示默认值。'; reloadBtn.hidden = true; }
    backBtn.hidden = isHome;
    backBtn.textContent = view.type === 'seat' ? '‹ 多方会审' : '‹ 返回';
    titleEl.textContent = isHome ? '子代理设置' : view.type === 'role' ? (ROLES.find(r => r[0] === view.role)?.[1] || view.role) : view.type === 'panel' ? '多方会审' : `席位 #${view.index + 1}`;
    fields.className = isHome ? 'ds-fields' : 'ds-fields is-sub';
    const kids = isHome ? renderHome(p) : view.type === 'role' ? renderRoleDetail(p, view.role) : view.type === 'panel' ? renderPanelList(p) : renderSeatDetail(p, view.index);
    fields.replaceChildren(...kids);
    bodyEl.scrollTop = prevScroll;
    sync();
  }

  async function api(url, opts) {
    const res = await fetch(withToken(url), opts), data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) {
      const err = data?.error || `请求失败 (${res.status})`;
      const msg = res.status === 403 && err === 'invalid CSRF token' ? '面板已重载，请刷新后重试' : res.status === 403 ? `保存被拒绝 (${err})` : err;
      throw Object.assign(new Error(msg), { status: res.status, code: err });
    }
    return data;
  }
  async function refreshCsrf() {
    const res = await fetch(withToken('/'), { cache: 'no-store' });
    const hit = res.ok && (await res.text()).match(/<meta\s+name="cockpit-csrf"\s+content="([^"]+)"/i);
    if (!hit?.[1]) return false;
    csrf = hit[1];
    const tag = document.querySelector('meta[name="cockpit-csrf"]');
    if (tag) tag.content = csrf;
    return true;
  }
  async function load() {
    loading = true; sync(); setMsg('读取中…');
    try {
      saved = await api(endpoint + '?resolve=1', { cache: 'no-store' });
      draft = structuredClone(saved.policy); conflicted = false; pendingSave = false; reloadBtn.hidden = true;
      render(); setMsg('');
    } catch (e) { setMsg(e.message, 'err'); }
    finally { loading = false; sync(); }
  }
  function commit(shouldRender = true) {
    if (!draft || !saved || conflicted) return;
    if (!hasDroid()) {
      draft.quota_fallback.enabled = false;
      for (const k of Object.keys(draft.fallbacks || {})) draft.fallbacks[k] = (draft.fallbacks[k] || []).map(c => (c.engine === 'droid' ? { ...c, enabled: false } : c));
    }
    if (shouldRender) render();
    pendingSave = true;
    if (!saving) flushSave();
  }
  async function flushSave() {
    if (saving || !saved || !draft) return;
    saving = true; setMsg('保存中…');
    try {
      while (pendingSave && !conflicted) {
        pendingSave = false;
        const snapshot = structuredClone(draft);
        snapshot.revision = saved.policy.revision;
        const post = () => api(endpoint, { method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json', 'X-Cockpit-CSRF': csrf }, body: JSON.stringify(snapshot) });
        let out;
        try { out = await post(); }
        catch (e) { if (e.status === 403 && e.code === 'invalid CSRF token' && await refreshCsrf()) out = await post(); else throw e; }
        saved.policy = out.policy; saved.exists = true; draft.revision = out.policy.revision;
      }
      if (!conflicted) {
        setMsg('已保存', 'ok', true);
        const fresh = await api(endpoint + '?resolve=1', { cache: 'no-store' }).catch(() => null);
        if (fresh && !pendingSave && fresh.policy.revision === saved.policy.revision) {
          saved = fresh;
          if (view.type === 'home') render();
        }
      }
    } catch (e) {
      pendingSave = false;
      if (e.status === 409) { conflicted = true; reloadBtn.hidden = false; setMsg('配置已在别处变更，请重载', 'err'); sync(); }
      else setMsg(e.message, 'err');
    } finally { saving = false; }
  }

  const stepBackOrClose = () => { if (view.type === 'seat') go({ type: 'panel' }); else if (view.type !== 'home') go({ type: 'home' }); else drawer.close(); };
  backBtn.addEventListener('click', stepBackOrClose);
  closeBtn.addEventListener('click', stepBackOrClose);
  reloadBtn.addEventListener('click', () => load());
  trigger.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); });
  trigger.addEventListener('click', e => {
    e.stopPropagation(); if (drawer.open) return;
    if (document.getElementById('quotaDrawer')?.classList.contains('open')) document.getElementById('qdClose')?.click();
    view = { type: 'home' }; drawer.showModal(); trigger.setAttribute('aria-expanded', 'true');
    if (!saving && !pendingSave && !conflicted) load(); else render();
  });
  drawer.addEventListener('click', e => { if (e.target !== drawer) return; const r = drawer.getBoundingClientRect(); if (e.clientX < r.left || e.clientX >= r.right || e.clientY < r.top || e.clientY >= r.bottom) stepBackOrClose(); });
  drawer.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); stepBackOrClose(); } });
  drawer.addEventListener('close', () => { view = { type: 'home' }; trigger.setAttribute('aria-expanded', 'false'); trigger.focus(); });
  sync();
})();
