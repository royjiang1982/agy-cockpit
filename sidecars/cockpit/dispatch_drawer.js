(() => {
  'use strict';

  const trigger = document.getElementById('dispatchSettingsBtn');
  const C = window.__COCKPIT_DISPATCH_CATALOG__;
  if (!trigger || !C) return;

  const {
    SKIP_WHY, ROLES, DEFAULT_SEAT_POOL, el, dot, findSpec,
    activeDroidFallback, syncFallback, fmtTarget, renderTargetEditor, renderDroidCard,
  } = C;

  const endpoint = '/api/dispatch-policy';
  const sidecarToken = (new URLSearchParams(window.location.search).get('token') || '').trim();
  const withToken = url => (!sidecarToken ? url : `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(sidecarToken)}`);
  let csrf = document.querySelector('meta[name="cockpit-csrf"]')?.content || '';

  let saved = null, draft = null, loading = false, saving = false, pendingSave = false, conflicted = false, msgTimer = null;
  let view = { type: 'home' };

  const drawer = el('dialog', { id: 'dispatchDrawer', 'aria-labelledby': 'dsTitle' });
  const titleEl = el('h2', { id: 'dsTitle' }, '子代理设置');
  const statusEl = el('span', { className: 'ds-status', role: 'status', 'aria-live': 'polite' });
  const closeBtn = el('button', { type: 'button', className: 'ds-close', 'aria-label': '关闭', title: '关闭' }, '✕');
  const warnText = el('span', {});
  const reloadBtn = el('button', { type: 'button', className: 'ds-warn-act', hidden: true }, '重载');
  const warnEl = el('div', { className: 'ds-warn', role: 'alert', hidden: true }, warnText, reloadBtn);
  const fields = el('fieldset', { className: 'ds-fields', 'aria-label': '子代理设置' });
  const bodyEl = el('div', { className: 'ds-body' }, warnEl, fields);
  drawer.append(
    el('div', { className: 'ds-head' },
      el('div', { className: 'ds-head-left' }, titleEl),
      el('div', { className: 'ds-head-right' }, statusEl, closeBtn)),
    bodyEl);
  document.body.append(drawer);

  const avail = () => saved?.capabilities?.available_engines || [];
  const hasDroid = () => avail().includes('droid');
  const enabledSeats = p => (p.roles.panel.seats || []).filter(s => s && s.enabled !== false);

  function syncSwitches(p) {
    const seats = enabledSeats(p);
    p.external_enabled = ROLES.some(([r]) => p.roles[r].primary.channel === 'cli') || seats.some(s => s.channel === 'cli');
    if (ROLES.some(([r]) => p.roles[r].primary.channel === 'swarm') || seats.some(s => s.channel === 'swarm')) {
      p.allow_swarm = true;
    }
  }

  function setMsg(text, tone = '', autoClear = false) {
    clearTimeout(msgTimer);
    if (tone === 'err') {
      warnEl.hidden = false; warnEl.dataset.tone = 'err'; warnText.textContent = text;
      statusEl.textContent = ''; statusEl.dataset.tone = '';
    } else {
      if (!saved?.read_only_warning) { warnEl.hidden = true; warnEl.dataset.tone = ''; reloadBtn.hidden = true; }
      statusEl.textContent = text; statusEl.dataset.tone = tone;
      if (autoClear && text) {
        msgTimer = setTimeout(() => {
          if (statusEl.textContent === text) { statusEl.textContent = ''; statusEl.dataset.tone = ''; }
        }, 1600);
      }
    }
  }

  function sync() {
    fields.disabled = loading || conflicted || !draft || Boolean(saved?.read_only_warning);
    reloadBtn.disabled = loading;
  }

  function go(nextView) {
    view = nextView;
    bodyEl.scrollTop = 0;
    render();
    if (drawer.open) closeBtn.focus({ preventScroll: true });
  }

  function renderHome(p) {
    const masterCb = el('input', { type: 'checkbox', className: 'ds-switch', checked: p.enabled, 'aria-label': '接管子代理编排' });
    masterCb.addEventListener('change', () => { p.enabled = masterCb.checked; commit(); });
    const masterCard = el('div', { className: 'ds-card' },
      el('label', { className: 'ds-item ds-item-click' },
        el('div', { className: 'ds-row-col' },
          el('span', { className: 'ds-lbl' }, '接管子代理编排'),
          el('span', { className: 'ds-item-desc' }, '开启后接管编排策略，忽略用户或项目配置；关闭则切回原有设置')),
        masterCb));

    const roleCard = el('div', { className: p.enabled ? 'ds-card' : 'ds-card ds-dimmed', inert: !p.enabled });
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

    return [masterCard, roleCard];
  }

  function renderRoleDetail(p, role) {
    const cur = p.roles[role].primary, activeFb = activeDroidFallback(p, cur);
    const kids = [renderTargetEditor(p, cur, {
      availableEngines: avail(),
      onChange: next => { p.roles[role].primary = next; syncSwitches(p); commit(); },
      onOptCommit: () => commit(false),
    })];
    if (hasDroid()) {
      kids.push(renderDroidCard(p, cur, {
        enabled: Boolean(activeFb), activeEntry: activeFb,
        onToggle: on => { syncFallback(p, cur, on); commit(); },
        onOptCommit: () => commit(false),
      }));
    }
    return kids;
  }

  function renderPanelList(p) {
    const seats = enabledSeats(p), kids = [el('div', { className: 'ds-caption' }, '按顺序选用：单方取 #1、两方取前 2 个，不可用自动顺延下一席位。')];
    if (seats.length) {
      const listCard = el('div', { className: 'ds-card' });
      seats.forEach((seat, idx) => {
        const f = fmtTarget(seat);
        const main = el('button', { type: 'button', className: 'ds-seat-main' },
          el('span', { className: 'ds-eng-col' }, dot(f.brand), el('span', { className: 'ds-val-eng' }, f.eng)),
          el('span', { className: 'ds-val-txt' }, f.text));
        main.addEventListener('click', () => go({ type: 'seat', index: idx }));
        const move = dir => {
          const copy = seats.map(s => ({ ...s, enabled: true }));
          [copy[idx], copy[idx + dir]] = [copy[idx + dir], copy[idx]];
          p.roles.panel.seats = copy; commit();
        };
        const up = el('button', { type: 'button', className: 'ds-icon-btn', disabled: idx === 0, 'aria-label': '上移' }, '↑');
        const down = el('button', { type: 'button', className: 'ds-icon-btn', disabled: idx === seats.length - 1, 'aria-label': '下移' }, '↓');
        const del = el('button', { type: 'button', className: 'ds-icon-btn is-del', disabled: seats.length <= 1, 'aria-label': '删除席位' }, '×');
        up.addEventListener('click', () => move(-1));
        down.addEventListener('click', () => move(1));
        del.addEventListener('click', () => { p.roles.panel.seats = seats.filter((_, i) => i !== idx).map(s => ({ ...s, enabled: true })); syncSwitches(p); commit(); });
        listCard.append(el('div', { className: 'ds-seat-row' }, el('span', { className: 'ds-rank' }, String(idx + 1)), main, el('div', { className: 'ds-seat-acts' }, up, down, del)));
      });
      kids.push(listCard);
    }
    const addBtn = el('button', { type: 'button', className: 'ds-add-btn' }, '+ 添加席位');
    addBtn.addEventListener('click', () => {
      const has = avail();
      const sameSeat = (a, b) => a.engine === b.engine && (findSpec(a.engine, a.model)?.id || a.model) === (findSpec(b.engine, b.model)?.id || b.model);
      const nextSeat = DEFAULT_SEAT_POOL.find(c => has.includes(c.engine) && !seats.some(s => sameSeat(s, c)))
        || DEFAULT_SEAT_POOL.find(c => has.includes(c.engine)) || DEFAULT_SEAT_POOL[0];
      go({ type: 'new-seat', seat: { ...nextSeat, enabled: true }, fbEnabled: Boolean(activeDroidFallback(p, nextSeat)) });
    });
    kids.push(addBtn);
    return kids;
  }

  function renderSeatDetail(p, index, draftSeat = null) {
    const seats = enabledSeats(p), isNew = Boolean(draftSeat);
    if (!isNew && !seats[index]) { view = { type: 'panel' }; return renderPanelList(p); }
    const target = isNew ? draftSeat : seats[index];
    const activeFb = activeDroidFallback(p, target);
    const fbOn = isNew ? Boolean(view.fbEnabled) : Boolean(activeFb);
    const kids = [renderTargetEditor(p, target, {
      availableEngines: avail(),
      onChange: next => {
        if (isNew) { view = { type: 'new-seat', seat: { ...next, enabled: true }, fbEnabled: Boolean(activeDroidFallback(p, next)) }; render(); return; }
        const copy = seats.map(s => ({ ...s, enabled: true }));
        copy[index] = { ...next, enabled: true };
        p.roles.panel.seats = copy; syncSwitches(p); commit();
      },
      onOptCommit: () => commit(false),
    })];
    if (hasDroid()) {
      kids.push(renderDroidCard(p, target, {
        enabled: fbOn, activeEntry: activeFb,
        onToggle: on => { if (isNew) { view = { ...view, fbEnabled: on }; render(); } else { syncFallback(p, target, on); commit(); } },
        onOptCommit: () => commit(false),
      }));
    }
    if (isNew) {
      const saveBtn = el('button', { type: 'button', className: 'ds-primary-btn' }, '保存');
      saveBtn.addEventListener('click', () => {
        p.roles.panel.seats = [...seats.map(s => ({ ...s, enabled: true })), { ...draftSeat, enabled: true }];
        if (hasDroid()) syncFallback(p, draftSeat, fbOn);
        syncSwitches(p); go({ type: 'panel' }); commit(false);
      });
      kids.push(saveBtn);
    } else if (seats.length > 1) {
      const delBtn = el('button', { type: 'button', className: 'ds-danger-btn' }, '移除此席位');
      delBtn.addEventListener('click', () => {
        p.roles.panel.seats = seats.filter((_, i) => i !== index).map(s => ({ ...s, enabled: true }));
        syncSwitches(p); go({ type: 'panel' }); commit(false);
      });
      kids.push(delBtn);
    }
    return kids;
  }

  function render() {
    if (!draft) return;
    const prevScroll = bodyEl.scrollTop, p = draft, isHome = view.type === 'home';
    if (saved.read_only_warning) {
      warnEl.hidden = false; warnEl.dataset.tone = ''; warnText.textContent = '配置文件异常或来自更高版本，当前只读展示默认值。'; reloadBtn.hidden = true;
    }
    const closeLabel = isHome ? '关闭' : '返回上一级';
    closeBtn.setAttribute('aria-label', closeLabel);
    closeBtn.title = closeLabel;
    titleEl.textContent = isHome ? '子代理设置'
      : view.type === 'role' ? (ROLES.find(r => r[0] === view.role)?.[1] || view.role)
      : view.type === 'panel' ? '多方会审'
      : view.type === 'new-seat' ? '添加席位' : `席位 #${view.index + 1}`;
    fields.className = isHome ? 'ds-fields' : 'ds-fields is-sub';
    const kids = isHome ? renderHome(p)
      : view.type === 'role' ? renderRoleDetail(p, view.role)
      : view.type === 'panel' ? renderPanelList(p)
      : view.type === 'new-seat' ? renderSeatDetail(p, -1, view.seat) : renderSeatDetail(p, view.index);
    fields.replaceChildren(...kids);
    bodyEl.scrollTop = prevScroll;
    sync();
  }

  async function api(url, opts) {
    const res = await fetch(withToken(url), opts), data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) {
      const err = data?.error || `请求失败 (${res.status})`;
      const msg = res.status === 403 && err === 'invalid CSRF token' ? '面板已重载，请刷新后重试'
        : res.status === 403 ? `保存被拒绝 (${err})` : err;
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
    if (loading) return;
    loading = true; sync(); setMsg('读取中…');
    try {
      saved = await api(endpoint + '?resolve=1', { cache: 'no-store' });
      draft = structuredClone(saved.policy); conflicted = false; pendingSave = false; reloadBtn.hidden = true;
      render(); setMsg('');
    } catch (e) {
      reloadBtn.textContent = '重试'; reloadBtn.hidden = false; setMsg(e.message, 'err');
    } finally { loading = false; sync(); }
  }
  function commit(shouldRender = true) {
    if (!draft || !saved || conflicted) return;
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
        const post = () => api(endpoint, {
          method: 'POST', cache: 'no-store',
          headers: { 'Content-Type': 'application/json', 'X-Cockpit-CSRF': csrf, ...(location.protocol === 'plugin:' ? { 'X-Cockpit-Plugin-Origin': location.origin } : {}) },
          body: JSON.stringify(snapshot),
        });
        let out;
        try { out = await post(); }
        catch (e) { if (e.status === 403 && e.code === 'invalid CSRF token' && await refreshCsrf()) out = await post(); else throw e; }
        saved.policy = out.policy; saved.exists = true; draft.revision = out.policy.revision;
      }
      if (!conflicted && !pendingSave) {
        setMsg('已保存', 'ok', true);
        const fresh = await api(endpoint + '?resolve=1', { cache: 'no-store' }).catch(() => null);
        if (fresh && !pendingSave && fresh.policy.revision === saved.policy.revision) {
          saved = fresh;
          if (view.type === 'home') render();
        }
      }
    } catch (e) {
      pendingSave = false;
      reloadBtn.textContent = '重载'; reloadBtn.hidden = false;
      if (e.status === 409) { conflicted = true; setMsg('配置已在别处变更，请重载', 'err'); sync(); }
      else { draft = structuredClone(saved.policy); render(); setMsg(e.message, 'err'); }
    } finally {
      saving = false;
      if (pendingSave && !conflicted) flushSave();
    }
  }

  const stepBackOrClose = () => {
    if (view.type === 'seat' || view.type === 'new-seat') go({ type: 'panel' });
    else if (view.type !== 'home') go({ type: 'home' });
    else drawer.close();
  };
  closeBtn.addEventListener('click', stepBackOrClose);
  reloadBtn.addEventListener('click', () => load());
  trigger.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') e.stopPropagation(); });
  trigger.addEventListener('click', e => {
    e.stopPropagation(); if (drawer.open) return;
    if (document.getElementById('quotaDrawer')?.classList.contains('open')) document.getElementById('qdClose')?.click();
    view = { type: 'home' }; drawer.showModal(); trigger.setAttribute('aria-expanded', 'true');
    if (!loading && !saving && !pendingSave && !conflicted) load(); else render();
  });
  drawer.addEventListener('click', e => {
    if (e.target !== drawer) return;
    const r = drawer.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX >= r.right || e.clientY < r.top || e.clientY >= r.bottom) stepBackOrClose();
  });
  drawer.addEventListener('cancel', e => { e.preventDefault(); stepBackOrClose(); });
  drawer.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); stepBackOrClose(); } });
  drawer.addEventListener('close', () => { view = { type: 'home' }; trigger.setAttribute('aria-expanded', 'false'); trigger.focus(); });
  sync();
})();
