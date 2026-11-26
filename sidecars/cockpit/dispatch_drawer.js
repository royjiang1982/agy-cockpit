(() => {
  'use strict';

  const trigger = document.getElementById('dispatchSettingsBtn');
  if (!trigger) return;
  const endpoint = '/api/dispatch-policy';
  const csrf = document.querySelector('meta[name="cockpit-csrf"]')?.content;
  const engines = { agy: 'AGY', codex: 'Codex', droid: 'Droid', grok: 'Grok', pi: 'Pi' };
  const channels = { cli: '外部 CLI', invoke_subagent: '内置子代理', swarm: 'Swarm' };
  const efforts = { '': '默认', low: '低', medium: '中', high: '高', xhigh: '极高', same: '沿用主力' };
  const roles = {
    scout: ['快搜侦察', '查资料、找素材、扫竞品'],
    standard: ['常规制作', '小修、出分镜、写商品文案'],
    deep: ['深度执行', '跨模块研发、剪辑流程、批量分析'],
    expert: ['专家验收', '验收交付、成片质检、核对上架材料'],
    panel: ['多方会审', '独立评估方案、创意或选品'],
  };
  const reasons = {
    quota_low: '配额回退', cli_unavailable: 'CLI 未检测到', external_disabled: '外部 CLI 已关闭',
    swarm_disabled: 'Swarm 已关闭', role_permission_mismatch: '角色权限不符', candidate_disabled: '候选已停用',
  };
  const builtin = (role, enabled) => ({
    channel: 'invoke_subagent', engine: 'agy', model: ({ scout: 'flash', standard: 'inherit', panel: 'flash' })[role] || 'pro',
    effort: '', ...(enabled === undefined ? {} : { enabled }),
  });
  const isOn = target => target.enabled !== false;
  const node = (tag, attrs = {}, ...children) => {
    const el = document.createElement(tag);
    for (const [key, val] of Object.entries(attrs)) {
      if (typeof val === 'boolean' || key === 'value' || key === 'className') el[key] = val;
      else el.setAttribute(key, val);
    }
    el.append(...children);
    return el;
  };
  const block = (cls, ...children) => node('div', { className: cls }, ...children);
  const button = (label, run, attrs = {}) => {
    const el = node('button', { type: 'button', ...attrs }, label);
    if (run) el.addEventListener('click', run);
    return el;
  };

  let saved, draft, busy = false, dirty = false, conflicted = false, panelOpen = false;
  let listId = 0, quotaTimer;
  const drawer = node('dialog', { id: 'dispatchDrawer', 'aria-labelledby': 'dsTitle' });
  const env = block('ds-env', '正在读取环境…');
  const close = button('✕', () => drawer.close(), { className: 'ds-close', 'aria-label': '关闭派发设置', autofocus: true });
  const warning = node('div', { className: 'ds-warning', role: 'alert', hidden: true });
  const fields = node('fieldset', { className: 'ds-fields', 'aria-label': '派发选项' });
  const body = block('ds-body', warning, fields);
  const status = node('div', { className: 'ds-status', role: 'status', 'aria-live': 'polite' });
  const reload = button('重新加载', () => {
    if (!dirty || window.confirm('重新加载会丢弃未保存的修改。')) load();
  }, { hidden: true });
  const reset = button('恢复默认', () => {
    if (window.confirm('恢复默认设置并立即生效？未保存的修改将被丢弃。')) write(true);
  });
  const save = button('保存生效', null, { type: 'submit', className: 'ds-save' });
  const form = node('form', {}, body, block('ds-footer', status, block('ds-actions', reload, reset, save)));
  drawer.append(block('ds-header', block('ds-heading', node('h2', { id: 'dsTitle' }, '派发设置'), env), close), form);
  document.body.append(drawer);

  function sync() {
    const readOnly = !draft || Boolean(saved.read_only_warning);
    fields.disabled = busy || readOnly;
    save.disabled = fields.disabled || conflicted || !dirty;
    reset.disabled = fields.disabled || conflicted;
    reload.disabled = busy;
  }
  function message(text, tone = '') {
    status.textContent = text;
    status.dataset.tone = tone;
  }
  function touched() {
    dirty = true;
    if (!conflicted) message('有未保存的修改', 'warn');
    sync();
  }
  function toggle(label, checked, set, note = '', cls = '', name = label) {
    const input = node('input', { type: 'checkbox', checked, 'aria-label': name });
    input.addEventListener('change', () => { set(input.checked); touched(); });
    const hint = note && node('small', { id: 'ds-hint-' + ++listId }, note);
    if (hint) input.setAttribute('aria-describedby', hint.id);
    return node('label', { className: 'ds-toggle ' + cls }, input,
      node('span', {}, label, ...(hint ? [hint] : [])));
  }
  function select(label, name, entries, value, set) {
    const input = node('select', { 'aria-label': name }, ...Object.entries(entries).map(
      ([key, title]) => node('option', { value: key }, title)));
    input.value = value;
    input.addEventListener('change', () => { set(input.value); touched(); });
    return node('label', { className: 'ds-field' }, label, input);
  }
  function modelField(target, name, readOnly, set) {
    if (target.channel === 'invoke_subagent') {
      const native = readOnly ? { flash: 'Flash' } : { inherit: '继承', pro: 'Pro' };
      if (!(target.model in native)) native[target.model] = target.model;
      return select('模型', name, native, target.model, set);
    }
    const id = 'ds-model-' + ++listId;
    const input = node('input', { value: target.model, required: true, maxLength: 81, list: id, 'aria-label': name });
    input.addEventListener('input', () => { set(input.value); touched(); });
    const models = saved.capabilities?.models?.[target.engine] || [];
    return node('label', { className: 'ds-field ds-model' }, '模型', input,
      node('datalist', { id }, ...models.map(value => node('option', { value }))));
  }
  function targetFields(target, name, role, onChange = () => {}) {
    const grid = block('ds-target');
    const readOnly = draft.roles[role].read_only;
    const access = () => Object.fromEntries(Object.entries(engines).map(([key, title]) =>
      [key, available().includes(key) ? title : title + '（未检测）']));
    const set = (key, val) => { target[key] = val; onChange(target); };
    function draw(focus) {
      const channel = select('通道', name + ' 通道', channels, target.channel, value => {
        const wasNative = target.channel === 'invoke_subagent';
        set('channel', value);
        if (value !== 'cli') set('engine', 'agy');
        if (value === 'invoke_subagent') {
          set('model', builtin(role).model);
          set('effort', '');
        } else if (wasNative) {
          set('model', saved.capabilities?.models?.[target.engine]?.[0] || '');
        }
        draw('channel');
      });
      const engine = select('引擎', name + ' 引擎', target.channel === 'cli' ? access() : { agy: 'AGY' },
        target.engine, value => { set('engine', value); draw('engine'); });
      engine.lastChild.disabled = target.channel !== 'cli';
      grid.replaceChildren(channel, engine, modelField(target, name + ' 模型', readOnly, value => set('model', value)),
        select('档位', name + ' 档位', efforts, target.effort, value => set('effort', value)));
      if (focus) (focus === 'channel' ? channel : engine).lastChild.focus();
    }
    draw();
    return grid;
  }
  const available = () => saved?.capabilities?.available_engines || [];
  function actual(target) {
    if (!target) return '预览待刷新';
    if (target.mode === 'disabled') return '自定义派发已关闭';
    const why = target.reason === 'builtin_fallback' ? '内置兜底' : target.reason === 'fallback' ? '模型回退' : '';
    const skipped = why ? reasons[target.skipped?.[0]?.reason] : '';
    return [engines[target.engine], channels[target.channel], target.model || (target.requires_model_check ? '待核验模型' : ''),
      target.effort && efforts[target.effort], target.codex_fast ? 'Fast' : '', why, skipped].filter(Boolean).join(' · ');
  }
  const preview = text => block('ds-preview', '当前生效：' + text);

  function panelFields(resolved) {
    const current = saved.policy.roles.panel.seats;
    const seats = draft.roles.panel.seats;
    const details = node('details', { className: 'ds-panel', open: panelOpen },
      node('summary', {}, '会审席位'));
    details.addEventListener('toggle', () => { panelOpen = details.open; });
    for (let i = 0; i < Math.max(4, seats.length); i++) {
      const seat = seats[i] || builtin('panel', false), name = '席位 ' + (i + 1);
      const put = () => {
        while (seats.length < i) seats.push(builtin('panel', false));
        seats[i] = seat;
      };
      const effective = current[i] && isOn(current[i]) ? resolved?.seats?.[current.slice(0, i).filter(isOn).length] : null;
      const isolation = effective?.blind_isolated ? '盲审隔离' : '共享公开板 · 非盲审';
      const text = effective ? actual(effective) + ' · ' + isolation
        : resolved?.mode === 'disabled' ? actual(resolved) : resolved ? '席位未启用' : '预览待刷新';
      details.append(block('ds-seat', toggle(name, isOn(seat), enabled => { seat.enabled = enabled; put(); }, '', '', '启用' + name),
        targetFields(seat, name, 'panel', put), preview(text)));
    }
    return details;
  }
  function roleFields(role, [name, hint]) {
    const cfg = draft.roles[role], resolved = saved.resolved?.[role];
    const head = block('ds-role-head', node('h4', {}, name), node('span', { className: 'ds-badge' }, cfg.read_only ? '只读' : '可写'));
    const box = block('ds-role', head, block('ds-hint', hint));
    if (role === 'panel') {
      const text = resolved?.mode !== 'panel' ? actual(resolved) : !resolved.seats.length ? '没有启用席位'
        : resolved.seats.length + ' 席 · ' + (resolved.blind_isolated ? '盲审隔离' : '含公开板 · 非盲审');
      box.append(preview(text), panelFields(resolved));
    } else box.append(targetFields(cfg.primary, name, role), preview(actual(resolved)));
    return box;
  }
  function quota(engine) {
    const chip = node('span', { className: 'ds-quota', role: 'img', 'data-engine': engine });
    updateQuota(chip);
    return chip;
  }
  function updateQuota(chip) {
    const engine = chip.dataset.engine, now = Date.now() / 1000;
    const q = saved.capabilities?.quota?.[engine]; // API exposes the per-engine map as "quota".
    const age = now - q?.observed_at;
    const fresh = q?.fresh && Number.isFinite(q.pct) && age >= 0 && age < 300 && (!q.reset_ts || now < q.reset_ts);
    chip.dataset.level = !fresh ? 'unknown' : q.pct <= 5 ? 'low' : q.pct <= 20 ? 'mid' : 'ok';
    chip.textContent = fresh ? q.pct.toFixed(1).replace(/\.0$/, '') + '%' : '—';
    chip.title = engines[engine] + ' 剩余配额：' + (fresh ? chip.textContent : '过期或未知');
    chip.setAttribute('aria-label', chip.title);
  }
  function fallbackGroup(key, chain) {
    const split = key.indexOf(':'), engine = key.slice(0, split), model = key.slice(split + 1);
    const order = node('ol', { className: 'ds-chain' });
    function draw(focus, direction) {
      order.replaceChildren(...chain.map((target, i) => {
        const name = engines[target.engine] + ' · ' + target.model;
        const arrows = [-1, 1].map(step => button(step < 0 ? '↑' : '↓', () => {
          [chain[i], chain[i + step]] = [chain[i + step], chain[i]];
          touched(); draw(i + step, step);
        }, { disabled: !chain[i + step], 'aria-label': (step < 0 ? '上移 ' : '下移 ') + model + '：' + name + '（' + (i + 1) + '）' }));
        const row = node('li', {}, block('ds-chain-row',
          block('ds-candidate', node('b', {}, name), quota(target.engine),
            block('ds-hint', [channels[target.channel], efforts[target.effort]].join(' · '))),
          block('ds-order', toggle('启用', isOn(target), value => { target.enabled = value; }, '', '',
            '启用 ' + model + ' 的回退项 ' + (i + 1) + '：' + name), ...arrows)));
        if (i === focus) queueMicrotask(() => (arrows.find((btn, j) => !btn.disabled && j === (direction < 0 ? 0 : 1))
          || arrows.find(btn => !btn.disabled))?.focus());
        return row;
      }));
    }
    draw();
    return block('ds-fallback', block('ds-role-head', node('h4', {}, engines[engine] + ' · ' + model), quota(engine)),
      chain.length ? order : block('ds-hint', '无回退项'));
  }

  function preset(kind) {
    const has = available(), useCli = kind !== 'native' && has.length > 0;
    function recommend(role, effort, preferred) {
      const engine = preferred.find(key => has.includes(key) && saved.capabilities?.models?.[key]?.length);
      if (!useCli || !engine) return builtin(role);
      const catalog = saved.capabilities.models[engine];
      const hint = { scout: /hydrogen|flash/, standard: /neon-medium|argon/, deep: /-sol|neon-(?:x?high|medium)/,
        expert: /-astra|xhigh/, panel: /-astra|xhigh/ }[role];
      return { channel: 'cli', engine, model: catalog.find(item => hint.test(item)) || catalog[0], effort };
    }
    const max = kind === 'max', eco = kind === 'eco';
    Object.assign(draft, { enabled: true, external_enabled: useCli, allow_swarm: max,
      codex_fast: useCli && has.includes('codex') && !eco, quota_fallback: { enabled: true, threshold_pct: eco ? 20 : max ? 0 : 5 } });
    for (const [role, effort, engines] of [
      ['scout', 'high', ['agy', 'droid', 'codex']], ['standard', 'medium', ['agy', 'droid', 'codex']],
      ['deep', 'high', ['codex', 'agy', 'droid']], ['expert', 'xhigh', ['codex', 'agy', 'droid']],
    ]) {
      draft.roles[role].primary = eco && (role === 'scout' || role === 'standard') ? builtin(role)
        : recommend(role, effort, [...engines, 'grok', 'pi']);
    }
    const order = ['codex', 'agy', 'droid', 'grok', 'pi'], detected = order.filter(key => has.includes(key));
    draft.roles.panel.seats = Array.from({ length: Math.max(4, draft.roles.panel.seats.length) }, (_, i) => {
      const pool = max && useCli ? [detected[i % detected.length]] : [detected[i] || (i > 1 ? order[i] : undefined)];
      return { ...recommend('panel', eco ? 'medium' : 'xhigh', pool), enabled: i < (max && useCli ? 4 : 2) };
    });
    touched(); render();
  }
  const section = (title, ...rows) => node('section', { className: 'ds-section' }, node('h3', {}, title), ...rows);
  function render() {
    const has = available(), p = draft;
    warning.hidden = !saved.read_only_warning;
    warning.textContent = warning.hidden ? '' : '配置损坏、无法读取或来自更高版本。当前仅展示默认设置，不能保存或覆盖原配置。';
    env.textContent = !saved.capabilities ? '环境待刷新' : has.length
      ? '已检测：' + has.map(key => engines[key] || key).join(' · ') : '纯 AGY 内置模式';
    const presetRow = block('ds-presets', ...[['自动推荐', 'auto'], ['纯内置', 'native'], ['省配额', 'eco'], ['全力多引擎', 'max']]
      .map(([label, kind], i) => button(label, () => { preset(kind); fields.querySelectorAll('.ds-presets button')[i].focus(); })));
    const threshold = node('input', { type: 'number', min: 0, max: 50, step: 1, required: true,
      value: p.quota_fallback.threshold_pct, 'aria-label': '配额回退阈值（百分比）' });
    threshold.addEventListener('input', () => { p.quota_fallback.threshold_pct = threshold.valueAsNumber; touched(); });
    const groups = Object.entries(p.fallbacks).map(([key, chain]) => fallbackGroup(key, chain));
    fields.replaceChildren(presetRow, section('全局开关', block('ds-switches',
      toggle('启用自定义派发', p.enabled, val => { p.enabled = val; }),
      toggle('启用外部 CLI 引擎', p.external_enabled, val => { p.external_enabled = val; },
        has.length ? '' : '本机未检测到外部 CLI', has.length ? '' : 'ds-undetected'),
      toggle('允许内置使用 Swarm', p.allow_swarm, val => { p.allow_swarm = val; }, '精确选模型／协作，公开板非盲审'),
      toggle('Codex Fast 档', p.codex_fast, val => { p.codex_fast = val; }, '', has.includes('codex') ? 'ds-codex' : ''),
      block('ds-threshold', toggle('配额自动回退', p.quota_fallback.enabled, val => { p.quota_fallback.enabled = val; }),
        node('label', {}, '阈值', threshold, '%')))),
    section('通用角色路由', ...Object.entries(roles).map(([role, meta]) => roleFields(role, meta))),
    section('模型 Fallback 链', ...(groups.length ? groups : [block('ds-hint', '未配置回退链')])));
    sync();
  }

  async function api(path, payload) {
    const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 6000);
    try {
      const opts = { signal: ctl.signal, cache: 'no-store' };
      if (payload !== undefined) {
        if (!csrf || csrf === '__CSRF_TOKEN__') throw new Error('页面缺少安全令牌，请刷新驾驶舱');
        Object.assign(opts, { method: 'POST', body: JSON.stringify(payload),
          headers: { 'Content-Type': 'application/json', 'X-Cockpit-CSRF': csrf } });
      }
      const res = await fetch(path, opts), data = await res.json().catch(() => null);
      if (!res.ok || !data || data.ok === false) throw Object.assign(
        new Error(res.status === 403 ? '服务已重启或来源校验未通过，请刷新面板后重试' :
          data?.error || '请求失败（HTTP ' + res.status + '）'), { status: res.status });
      return data;
    } finally { clearTimeout(timer); }
  }
  function apply(data) {
    saved = data;
    draft = JSON.parse(JSON.stringify(data.policy));
    dirty = conflicted = false;
    reload.hidden = !saved.read_only_warning;
    render();
  }
  async function load() {
    if (busy) return;
    busy = true; sync(); message('正在加载…');
    try { apply(await api(endpoint + '?resolve=1')); message(''); }
    catch (error) { reload.hidden = false; message('加载失败：' + error.message, 'error'); }
    finally { busy = false; sync(); }
  }
  async function write(restore) {
    if (busy || !draft || saved.read_only_warning || conflicted || (!restore && !dirty)) return;
    busy = true; sync(); message(restore ? '正在恢复…' : '正在保存…');
    try {
      const data = await api(endpoint + (restore ? '/reset' : ''), restore ? { revision: draft.revision } : draft);
      const done = restore ? '已恢复默认并生效' : '已保存并生效';
      try { apply(await api(endpoint + '?resolve=1')); message(done, 'ok'); }
      catch (_) {
        apply({ ...data, capabilities: saved.capabilities });
        reload.hidden = false; message(done + '；生效预览需重新加载', 'warn');
      }
    } catch (error) {
      if (error.status === 409) {
        conflicted = true; reload.hidden = false;
        message('配置已在别处更新，请重新加载', 'error');
      } else message('未能' + (restore ? '恢复' : '保存') + '：' + error.message, 'error');
    } finally { busy = false; sync(); }
  }

  form.addEventListener('submit', ev => { ev.preventDefault(); write(false); });
  trigger.addEventListener('keydown', ev => {
    if (ev.key === 'Enter' || ev.key === ' ') ev.stopPropagation();
  });
  trigger.addEventListener('click', ev => {
    ev.stopPropagation();
    if (drawer.open) return;
    if (document.getElementById('quotaDrawer')?.classList.contains('open')) document.getElementById('qdClose')?.click();
    drawer.showModal();
    trigger.setAttribute('aria-expanded', 'true');
    if (!dirty && !conflicted) load();
    quotaTimer = setInterval(() => drawer.querySelectorAll('.ds-quota').forEach(updateQuota), 15000);
  });
  drawer.addEventListener('click', ev => {
    if (ev.target !== drawer) return;
    const r = drawer.getBoundingClientRect();
    if (ev.clientX < r.left || ev.clientX >= r.right || ev.clientY < r.top || ev.clientY >= r.bottom) drawer.close();
  });
  drawer.addEventListener('keydown', ev => {
    if (ev.key === 'Escape') { ev.stopPropagation(); ev.preventDefault(); drawer.close(); }
  });
  drawer.addEventListener('close', () => {
    clearInterval(quotaTimer);
    trigger.setAttribute('aria-expanded', 'false');
    trigger.focus();
  });
  sync();
})();
