/* 组织者后台 SPA · 纯原生 JS，零平台 SDK、零第三方依赖、零跳转。
 *
 * 契约要点：
 * - 后端读取返回 camelCase（regStart/checkInStart/regMode/...），写入用 snake_case
 *   （reg_start/checkin_start/reg_mode/...），本文件负责双向转换。
 * - 所有管理端接口带 Authorization: Bearer <token>；401 自动登出。
 * - 二维码/导出为二进制响应，用 fetch + blob + objectURL 下载，不能用 <img src> 直连
 *   （那样无法携带鉴权头）。
 * - 时间一律按北京时间（UTC+8）解释；新建活动的默认时间窗取自 /healthz 的 serverTime，
 *   不依赖组织者本机时钟。
 */
(function () {
  'use strict';

  var TOKEN_KEY = 'qr4events.admin.token';
  var USER_KEY = 'qr4events.admin.user';
  var TABS = [
    { key: 'overview', label: '概览' },
    { key: 'edit', label: '编辑活动' },
    { key: 'fields', label: '字段配置' },
    { key: 'stats', label: '数据看板' },
    { key: 'details', label: '报名明细' },
    { key: 'qrcode', label: '二维码' },
    { key: 'whitelist', label: '白名单' }
  ];
  var MODE_LABELS = { open: '开放报名', whitelist: '邀请名单制', invite: '邀请码制' };
  var STATUS_LABELS = { draft: '草稿', active: '进行中', ended: '已结束' };

  var state = {
    token: '', user: '', activities: [], currentId: '', current: null,
    tab: 'overview', busy: false, qrObjectUrl: '',
    filters: { search: '', status: 'all', category: '', order: 'desc' }
  };

  // ---------------------------------------------------------------- DOM 辅助

  function h(tag, attrs, children) {
    var n = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined) return;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = String(v);
      else if (k === 'value') n.value = v;
      else if (k === 'checked') n.checked = !!v;
      else if (k === 'disabled') n.disabled = !!v;
      else if (k === 'htmlFor') n.htmlFor = v;
      else if (k.slice(0, 2) === 'on' && typeof v === 'function') {
        n.addEventListener(k.slice(2).toLowerCase(), v);
      } else n.setAttribute(k, v);
    });
    (children || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      n.appendChild(typeof c === 'string' || typeof c === 'number'
        ? document.createTextNode(String(c)) : c);
    });
    return n;
  }

  function $(id) { return document.getElementById(id); }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  function toast(msg, kind) {
    var t = $('toast');
    t.className = 'toast show ' + (kind || 'info');
    t.textContent = msg;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () { t.className = 'toast'; }, 5000);
  }

  function readStore(key, fallback) {
    try { return sessionStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }
  function writeStore(key, value) {
    try { sessionStorage.setItem(key, value); } catch (e) { /* 隐私模式忽略 */ }
  }
  function dropStore(key) {
    try { sessionStorage.removeItem(key); } catch (e) { /* 忽略 */ }
  }

  function fmt(iso) {
    if (!iso) return '';
    var m = String(iso).match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    return m ? (m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]) : String(iso);
  }
  function toLocal(iso) { return iso ? String(iso).slice(0, 16) : ''; }
  function toIso(v) {
    v = String(v || '').trim();
    return v ? v.slice(0, 16) + ':00+08:00' : '';
  }
  function nextDay(day) {
    var p = day.split('-');
    return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + 1)).toISOString().slice(0, 10);
  }
  function basePath() {
    return '/api/admin/activities/' + encodeURIComponent(state.currentId);
  }

  // ---------------------------------------------------------------- 网络

  function headers() {
    var hd = { 'Content-Type': 'application/json' };
    if (state.token) hd['Authorization'] = 'Bearer ' + state.token;
    return hd;
  }

  function errorText(data, status) {
    if (data && typeof data.message === 'string' && data.message) return data.message;
    if (data && typeof data.detail === 'string' && data.detail) return data.detail;
    return '请求失败（' + status + '）';
  }

  async function api(method, path, body, raw) {
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = controller
      ? window.setTimeout(function () { controller.abort(); }, 15000) : 0;
    var res;
    try {
      res = await fetch(path, {
        method: method,
        headers: headers(),
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller ? controller.signal : undefined
      });
    } catch (e) {
      throw new Error('网络连接不稳定，请稍后重试');
    } finally {
      if (controller) window.clearTimeout(timer);
    }
    if (res.status === 401) {
      logout(true);
      throw new Error('登录已过期，请重新登录');
    }
    if (raw) {
      if (!res.ok) throw new Error('下载失败（' + res.status + '）');
      return await res.blob();
    }
    var data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(errorText(data, res.status));
    return data;
  }

  async function download(path, filename) {
    var blob = await api('GET', path, undefined, true);
    var url = URL.createObjectURL(blob);
    var a = h('a', { href: url, download: filename });
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function copyText(text) {
    var ta = h('textarea', { value: text });
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      toast('已复制到剪贴板', 'success');
    } catch (e) {
      toast('复制失败，请手动复制下方地址', 'error');
    }
    document.body.removeChild(ta);
  }

  // ---------------------------------------------------------------- 认证

  async function doLogin(username, password) {
    var res;
    try {
      res = await fetch('/api/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password })
      });
    } catch (e) {
      throw new Error('网络连接不稳定，请稍后重试');
    }
    var data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) throw new Error(errorText(data, res.status) || '账号或密码不正确');
    state.token = data.token;
    state.user = data.username;
    writeStore(TOKEN_KEY, data.token);
    writeStore(USER_KEY, data.username);
    showApp();
    await loadActivities();
  }

  function logout(expired) {
    state.token = ''; state.user = ''; state.current = null; state.currentId = '';
    state.activities = []; state.tab = 'overview';
    state.filters = { search: '', status: 'all', category: '', order: 'desc' };
    if (state.qrObjectUrl) {
      URL.revokeObjectURL(state.qrObjectUrl);
      state.qrObjectUrl = '';
    }
    dropStore(TOKEN_KEY); dropStore(USER_KEY);
    showLogin(expired ? '登录已过期，请重新登录' : '');
  }

  function showLogin(msg) {
    $('login-view').classList.remove('hidden');
    $('app-view').classList.add('hidden');
    $('login-error').textContent = msg || '';
    $('login-pass').value = '';
  }
  function showApp() {
    $('login-view').classList.add('hidden');
    $('app-view').classList.remove('hidden');
    $('whoami').textContent = '当前管理员：' + (state.user || 'admin');
  }

  // ---------------------------------------------------------------- 活动列表

  async function loadActivities(preferId) {
    var data = await api('GET', '/api/admin/activities');
    state.activities = data.items || [];
    renderActivitySelect();
    var target = preferId || state.currentId;
    var hit = state.activities.filter(function (a) { return a.id === target; })[0];
    if (hit) {
      await selectActivity(hit.id);
    } else if (state.activities.length) {
      await selectActivity(state.activities[0].id);
    } else {
      state.currentId = ''; state.current = null;
      renderTabs();
      renderEmptyActivity();
    }
  }

  function renderActivitySelect() {
    var sel = $('activity-select');
    clear(sel);
    if (!state.activities.length) {
      sel.appendChild(h('option', { value: '', text: '（暂无活动）' }));
      return;
    }
    state.activities.forEach(function (a) {
      var label = a.name + '（' + a.id + ' · ' + (STATUS_LABELS[a.status] || a.status) + '）';
      sel.appendChild(h('option', { value: a.id, text: label }));
    });
    if (state.currentId) sel.value = state.currentId;
  }

  async function selectActivity(id) {
    state.currentId = id;
    var data = await api('GET', '/api/admin/activities/' + encodeURIComponent(id));
    state.current = data.activity;
    state.qrUrl = '';
    $('activity-select').value = id;
    renderTabs();
    await renderTab();
  }

  async function newActivity() {
    var day = '';
    try {
      var r = await fetch('/healthz');
      var d = await r.json();
      day = (d.serverTime || '').slice(0, 10);
    } catch (e) { /* 取不到则用下方兜底 */ }
    if (!day) day = new Date().toISOString().slice(0, 10);
    var nd = nextDay(day);
    state.currentId = '';
    state.current = {
      id: '', name: '', description: '', location: '',
      regStart: day + 'T09:00', regEnd: day + 'T18:00',
      checkInStart: nd + 'T08:00', checkInEnd: nd + 'T12:00',
      regMode: 'open', status: 'active', inviteCode: '',
      regEnabled: true, checkinEnabled: true, allowOnsiteCombo: true,
      qrUrl: '', regCount: 0, checkinCount: 0, fieldsConfig: null
    };
    state.tab = 'edit';
    $('activity-select').value = '';
    renderTabs();
    await renderTab();
  }

  // ---------------------------------------------------------------- Tabs

  function renderTabs() {
    var box = $('tabs');
    clear(box);
    var visible = TABS.filter(function (t) {
      return t.key !== 'whitelist' ||
        (state.current && state.current.regMode === 'whitelist');
    });
    var active = visible.filter(function (t) { return t.key === state.tab; })[0];
    if (!active) state.tab = 'overview';
    visible.forEach(function (t) {
      var b = h('button', {
        class: 'tab' + (state.tab === t.key ? ' active' : ''),
        type: 'button', text: t.label
      });
      b.addEventListener('click', function () {
        state.tab = t.key;
        renderTabs();
        renderTab();
      });
      box.appendChild(b);
    });
  }

  async function renderTab() {
    // 切换页签时释放上一张二维码的 blob URL，避免内存泄漏。
    if (state.qrObjectUrl) {
      URL.revokeObjectURL(state.qrObjectUrl);
      state.qrObjectUrl = '';
    }
    // 新建活动尚未提交时 currentId 为空，除编辑页外的页签一律显示空态，
    // 否则会拿空 ID 请求 /api/admin/activities//stats 导致 404。
    if (!state.currentId && state.tab !== 'edit') { renderEmptyActivity(); return; }
    var content = $('tab-content');
    clear(content);
    if (state.busy) return;
    state.busy = true;
    try {
      if (state.tab === 'overview') renderOverview(content);
      else if (state.tab === 'edit') renderEditor(content);
      else if (state.tab === 'fields') renderFields(content);
      else if (state.tab === 'stats') await renderStats(content);
      else if (state.tab === 'details') await renderDetails(content);
      else if (state.tab === 'qrcode') await renderQrcode(content);
      else if (state.tab === 'whitelist') await renderWhitelist(content);
    } catch (e) {
      clear(content);
      content.appendChild(h('div', { class: 'card' }, [
        h('h2', { text: '加载失败' }),
        h('p', { class: 'muted', text: e.message }),
        h('button', { class: 'btn secondary', type: 'button', text: '重试',
          onclick: function () { renderTab(); } })
      ]));
    } finally {
      state.busy = false;
    }
  }

  function renderEmptyActivity() {
    var c = $('tab-content');
    clear(c);
    c.appendChild(h('div', { class: 'card' }, [
      h('h2', { text: '还没有活动' }),
      h('p', { class: 'muted', text: '点击「+ 新建活动」创建第一个活动，创建后即可下载二维码。' }),
      h('button', { class: 'btn', type: 'button', text: '+ 新建活动', onclick: newActivity })
    ]));
  }

  function metaRow(label, value) {
    return h('div', { class: 'meta-row' }, [
      h('span', { class: 'meta-label', text: label }),
      h('span', { class: 'meta-value', text: value || '—' })
    ]);
  }

  // ---------------------------------------------------------------- 概览

  function renderOverview(parent) {
    var a = state.current;
    var card = h('div', { class: 'card' }, [
      h('h1', { text: a.name || '未命名活动' }),
      h('span', { class: 'pill ' + (a.status === 'active' ? 'ok' : 'off'),
        text: STATUS_LABELS[a.status] || a.status }),
      h('div', { class: 'divider' })
    ]);
    card.appendChild(metaRow('活动 ID', a.id));
    card.appendChild(metaRow('地点', a.location));
    card.appendChild(metaRow('报名', fmt(a.regStart) + ' — ' + fmt(a.regEnd)));
    card.appendChild(metaRow('报到', fmt(a.checkInStart) + ' — ' + fmt(a.checkInEnd)));
    card.appendChild(metaRow('资格', MODE_LABELS[a.regMode] || a.regMode));

    var grid = h('div', { class: 'grid', style: 'margin-top:16px' }, [
      h('div', { class: 'stat' }, [
        h('div', { class: 'stat-num', text: a.regCount || 0 }),
        h('div', { class: 'stat-label', text: '报名人数' })
      ]),
      h('div', { class: 'stat' }, [
        h('div', { class: 'stat-num', text: a.checkinCount || 0 }),
        h('div', { class: 'stat-label', text: '报到人数' })
      ])
    ]);
    card.appendChild(grid);

    card.appendChild(h('div', { class: 'divider' }));
    card.appendChild(h('div', { class: 'tiny', text: '参与者二维码地址（同一地址，报名与报到由服务端时间窗决定）：' }));
    card.appendChild(h('div', { class: 'url-box mono', text: a.qrUrl }));
    var row = h('div', { class: 'btn-row' }, [
      h('button', { class: 'btn small', type: 'button', text: '查看二维码',
        onclick: function () { state.tab = 'qrcode'; renderTabs(); renderTab(); } }),
      h('button', { class: 'btn secondary small', type: 'button', text: '复制地址',
        onclick: function () { copyText(a.qrUrl); } }),
      h('button', { class: 'btn secondary small', type: 'button', text: '导出 Excel',
        onclick: function () {
          download(basePath() + '/export?format=xlsx',
            'activity_' + a.id + '.xlsx').catch(function (e) { toast(e.message, 'error'); });
        } })
    ]);
    card.appendChild(row);
    parent.appendChild(card);

    parent.appendChild(h('div', { class: 'card' }, [
      h('h2', { text: '危险操作' }),
      h('p', { class: 'muted', text: '删除活动会同时删除其全部报名与报到记录，且不可恢复。' }),
      h('button', { class: 'btn danger small', type: 'button', text: '删除该活动',
        onclick: deleteCurrent })
    ]));
  }

  async function deleteCurrent() {
    var a = state.current;
    if (!a || !a.id) return;
    if (!window.confirm('确定删除「' + a.name + '」及其全部报名/报到数据？此操作不可恢复。')) return;
    try {
      await api('DELETE', '/api/admin/activities/' + encodeURIComponent(a.id));
      toast('活动已删除', 'success');
      state.currentId = ''; state.current = null; state.tab = 'overview';
      await loadActivities();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------------------------------------------------------- 编辑活动

  function field(labelText, input, required, hint) {
    var children = [h('label', { text: labelText })];
    if (required) children[0].appendChild(h('span', { class: 'req', text: '*' }));
    children.push(input);
    if (hint) children.push(h('div', { class: 'field-hint', text: hint }));
    children.push(h('div', { class: 'field-error' }));
    return h('div', { class: 'field' }, children);
  }

  function selectEl(options, value) {
    var sel = h('select', {});
    options.forEach(function (o) {
      sel.appendChild(h('option', { value: o[0], text: o[1] }));
    });
    sel.value = value;
    return sel;
  }

  function checkRow(label, checked) {
    var id = 'chk-' + Math.random().toString(36).slice(2, 9);
    var box = h('input', { type: 'checkbox', id: id, checked: checked });
    return h('div', { class: 'switch-row' }, [
      box, h('label', { htmlFor: id, text: label })
    ]);
  }

  function renderEditor(parent) {
    var a = state.current || {};
    var isNew = !state.currentId;
    var card = h('div', { class: 'card' });
    card.appendChild(h('h1', { text: isNew ? '新建活动' : '编辑活动' }));
    card.appendChild(h('p', { class: 'muted',
      text: '以下时间均按北京时间（UTC+8）解释，请按活动实际安排修改。阶段判定只依据服务端时间。' }));
    card.appendChild(h('div', { class: 'divider' }));

    var form = h('form', {});
    form.noValidate = true;

    var nameEl = h('input', { type: 'text', maxlength: '200', value: a.name || '',
      placeholder: '例如：2026 年度大会' });
    var descEl = h('textarea', { maxlength: '4000', value: a.description || '',
      placeholder: '选填，活动说明' });
    var locEl = h('input', { type: 'text', maxlength: '200', value: a.location || '',
      placeholder: '例如：一楼报告厅' });
    form.appendChild(field('活动名称', nameEl, true));
    form.appendChild(field('活动说明', descEl, false));
    form.appendChild(field('地点', locEl, false));

    var times = h('div', { class: 'field-grid' });
    var rs = h('input', { type: 'datetime-local', value: toLocal(a.regStart) });
    var re = h('input', { type: 'datetime-local', value: toLocal(a.regEnd) });
    var cs = h('input', { type: 'datetime-local', value: toLocal(a.checkInStart) });
    var ce = h('input', { type: 'datetime-local', value: toLocal(a.checkInEnd) });
    times.appendChild(field('报名开始', rs, true));
    times.appendChild(field('报名结束', re, true));
    times.appendChild(field('报到开始', cs, true));
    times.appendChild(field('报到结束', ce, true));
    form.appendChild(times);

    var combos = h('div', { class: 'field-grid' });
    var statusEl = selectEl([['draft', '草稿'], ['active', '进行中'], ['ended', '已结束']],
      a.status || 'active');
    var modeEl = selectEl([['open', '开放报名'], ['whitelist', '邀请名单制'], ['invite', '邀请码制']],
      a.regMode || 'open');
    combos.appendChild(field('活动状态', statusEl, true));
    combos.appendChild(field('报名资格', modeEl, true));
    form.appendChild(combos);

    var invEl = h('input', { type: 'text', maxlength: '64', value: a.inviteCode || '',
      placeholder: '仅「邀请码制」需要填写' });
    form.appendChild(field('邀请码', invEl, false, '选择「邀请码制」时必填，参与者报名/现场报到时需输入。'));

    form.appendChild(h('div', { class: 'divider' }));
    var comboChk = h('input', { type: 'checkbox' });
    var regChk = h('input', { type: 'checkbox' });
    var ciChk = h('input', { type: 'checkbox' });
    comboChk.checked = a.allowOnsiteCombo !== false;
    regChk.checked = a.regEnabled !== false;
    ciChk.checked = a.checkinEnabled !== false;
    form.appendChild(checkRowWith(comboChk, '允许现场「报名 + 报到」2合1（未报名者一次填表完成报到）'));
    form.appendChild(checkRowWith(regChk, '报名通道开启'));
    form.appendChild(checkRowWith(ciChk, '报到通道开启'));

    var err = h('div', { class: 'field-error' });
    var submitBtn = h('button', { class: 'btn', type: 'submit',
      text: isNew ? '创建活动' : '保存修改' });
    form.appendChild(err);
    form.appendChild(submitBtn);
    form.appendChild(h('p', { class: 'tiny',
      text: '字段配置（参与类型、部门、单位、备注）请在「字段配置」页单独设置。' }));

    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var payload = {
        name: String(nameEl.value || '').trim(),
        description: String(descEl.value || '').trim(),
        location: String(locEl.value || '').trim(),
        reg_start: toIso(rs.value), reg_end: toIso(re.value),
        checkin_start: toIso(cs.value), checkin_end: toIso(ce.value),
        status: statusEl.value, reg_mode: modeEl.value,
        invite_code: String(invEl.value || '').trim(),
        allow_onsite_combo: comboChk.checked,
        reg_enabled: regChk.checked,
        checkin_enabled: ciChk.checked
      };
      // PUT 必须带回原字段配置，否则会被后端默认值覆盖重置。
      if (a.fieldsConfig) payload.fields_config = a.fieldsConfig;
      err.textContent = '';
      if (!payload.name) { err.textContent = '活动名称不能为空'; return; }
      if (!payload.reg_start || !payload.reg_end ||
          !payload.checkin_start || !payload.checkin_end) {
        err.textContent = '请填写完整的报名与报到时间窗';
        return;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = '正在保存…';
      try {
        var path = isNew ? '/api/admin/activities' : basePath();
        var data = await api(isNew ? 'POST' : 'PUT', path, payload);
        state.currentId = data.activity.id;
        state.current = data.activity;
        state.tab = 'overview';
        await loadActivities(state.currentId);
        toast(isNew ? '活动已创建，可去「二维码」页下载' : '修改已保存', 'success');
      } catch (e) {
        err.textContent = e.message;
        submitBtn.disabled = false;
        submitBtn.textContent = isNew ? '创建活动' : '保存修改';
      }
    });

    card.appendChild(form);
    parent.appendChild(card);
  }

  function checkRowWith(box, label) {
    var id = 'chk-' + Math.random().toString(36).slice(2, 9);
    box.id = id;
    return h('div', { class: 'switch-row' }, [
      box, h('label', { htmlFor: id, text: label })
    ]);
  }

  function renderFields(parent) {
    var a = state.current;
    var cfg = a.fieldsConfig || {
      type_options: ['普通', 'VIP', '其他'],
      department: { enabled: true, required: false },
      unit: { enabled: true, required: false },
      remark: { enabled: false, required: false }
    };
    var card = h('div', { class: 'card' });
    card.appendChild(h('h1', { text: '字段配置' }));
    card.appendChild(h('p', { class: 'muted',
      text: '控制参与者报名/现场报到表单显示哪些字段。姓名、手机号、类型为固定必填项。' }));
    card.appendChild(h('div', { class: 'divider' }));

    var optEl = h('input', { type: 'text', maxlength: '500',
      value: (cfg.type_options || []).join(', '),
      placeholder: '例如：普通, VIP, 嘉宾, 工作人员' });
    card.appendChild(field('参与类型选项', optEl, true, '用逗号分隔，至少一项，最多 20 项。'));

    var toggles = {};
    [['department', '所属部门'], ['unit', '单位'], ['remark', '备注']].forEach(function (p) {
      var c = cfg[p[0]] || {};
      var en = h('input', { type: 'checkbox', checked: !!c.enabled });
      var req = h('input', { type: 'checkbox', checked: !!c.required });
      var box = h('div', { class: 'sub-card' }, [
        h('h3', { text: p[1] }),
        checkRowWith(en, '启用该字段'),
        checkRowWith(req, '设为必填')
      ]);
      card.appendChild(box);
      toggles[p[0]] = { en: en, req: req };
    });

    var err = h('div', { class: 'field-error' });
    var btn = h('button', { class: 'btn', type: 'button', text: '保存字段配置' });
    btn.addEventListener('click', async function () {
      var options = String(optEl.value || '').split(/[,，]/)
        .map(function (s) { return s.trim(); })
        .filter(function (s) { return s; });
      var body = { type_options: options };
      ['department', 'unit', 'remark'].forEach(function (k) {
        body[k] = { enabled: toggles[k].en.checked, required: toggles[k].req.checked };
      });
      err.textContent = '';
      if (!options.length) { err.textContent = '参与类型选项至少需要一项'; return; }
      btn.disabled = true;
      try {
        var data = await api('PUT', basePath() + '/fields', body);
        state.current.fieldsConfig = data.fieldsConfig;
        toast('字段配置已保存', 'success');
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
    card.appendChild(err);
    card.appendChild(btn);
    parent.appendChild(card);
  }

  // ---------------------------------------------------------------- 数据看板

  async function renderStats(parent) {
    var data = await api('GET', basePath() + '/stats');
    var s = data.stats;
    var card = h('div', { class: 'card' });
    card.appendChild(h('h1', { text: '数据看板' }));
    card.appendChild(h('p', { class: 'tiny', text: '服务端时间：' + fmt(s.serverTime) + '（Asia/Shanghai）' }));
    card.appendChild(h('div', { class: 'grid', style: 'margin-top:14px' }, [
      h('div', { class: 'stat' }, [
        h('div', { class: 'stat-num', text: s.regCount }),
        h('div', { class: 'stat-label', text: '报名人数' })
      ]),
      h('div', { class: 'stat' }, [
        h('div', { class: 'stat-num', text: s.checkinCount }),
        h('div', { class: 'stat-label', text: '报到人数' })
      ]),
      h('div', { class: 'stat' }, [
        h('div', { class: 'stat-num', text: s.pendingCount }),
        h('div', { class: 'stat-label', text: '未报到' })
      ]),
      h('div', { class: 'stat' }, [
        h('div', { class: 'stat-num', text: s.checkinRate + '%' }),
        h('div', { class: 'stat-label', text: '报到率' })
      ])
    ]));
    parent.appendChild(card);

    var list = h('div', { class: 'card' });
    list.appendChild(h('h2', { text: '最近动态' }));
    if (!s.recent || !s.recent.length) {
      list.appendChild(h('p', { class: 'empty', text: '暂无报名或报到记录' }));
    } else {
      s.recent.forEach(function (x) {
        list.appendChild(h('div', { class: 'list-item' }, [
          h('span', { text: (x.type === 'checkin' ? '✓ 报到 · ' : '＋ 报名 · ') + x.name }),
          h('span', { class: 'time', text: fmt(x.time) })
        ]));
      });
    }
    parent.appendChild(list);
  }

  // ---------------------------------------------------------------- 报名明细

  async function renderDetails(parent) {
    var f = state.filters;
    var card = h('div', { class: 'card' });
    card.appendChild(h('h1', { text: '报名明细' }));
    card.appendChild(h('p', { class: 'muted',
      text: '此处展示完整手机号，仅限组织者核对使用，请勿外传或公开。导出为全量明细，不受上方筛选影响。' }));

    var searchEl = h('input', { type: 'text', value: f.search,
      placeholder: '按姓名或完整手机号搜索' });
    var statusEl = selectEl([['all', '全部状态'], ['checked_in', '已报到'], ['pending', '未报到']], f.status);
    var options = (state.current.fieldsConfig && state.current.fieldsConfig.type_options) || [];
    var catOptions = [['', '全部类型']].concat(options.map(function (o) { return [o, o]; }));
    var catEl = selectEl(catOptions, f.category);
    var orderEl = selectEl([['desc', '最新在前'], ['asc', '最早在前']], f.order);
    var tbody = h('tbody', {});

    var toolbar = h('div', { class: 'toolbar' }, [searchEl, statusEl, catEl, orderEl]);
    card.appendChild(toolbar);

    var btnRow = h('div', { class: 'btn-row' }, [
      h('button', { class: 'btn secondary small', type: 'button', text: '导出 Excel',
        onclick: function () {
          download(basePath() + '/export?format=xlsx', 'activity_' + state.currentId + '.xlsx')
            .catch(function (e) { toast(e.message, 'error'); });
        } }),
      h('button', { class: 'btn secondary small', type: 'button', text: '导出 CSV',
        onclick: function () {
          download(basePath() + '/export?format=csv', 'activity_' + state.currentId + '.csv')
            .catch(function (e) { toast(e.message, 'error'); });
        } })
    ]);
    card.appendChild(btnRow);
    card.appendChild(h('div', { class: 'divider' }));

    var table = h('div', { class: 'table-scroll' }, [
      h('table', {}, [
        h('thead', {}, [h('tr', {}, [
          h('th', { text: '姓名' }), h('th', { text: '手机号' }),
          h('th', { text: '类型' }), h('th', { text: '部门' }),
          h('th', { text: '单位' }), h('th', { text: '报名时间' }),
          h('th', { text: '报到' }), h('th', { text: '来源' })
        ])]),
        tbody
      ])
    ]);
    card.appendChild(table);
    var count = h('p', { class: 'tiny', text: '' });
    card.appendChild(count);
    var offset = 0;
    var pageSize = 100;
    var prevPage = h('button', { class: 'btn secondary small', type: 'button', text: '上一页', disabled: true });
    var nextPage = h('button', { class: 'btn secondary small', type: 'button', text: '下一页', disabled: true });
    card.appendChild(h('div', { class: 'btn-row' }, [prevPage, nextPage]));
    parent.appendChild(card);

    async function reload(resetPage) {
      if (resetPage) offset = 0;
      f.search = String(searchEl.value || '').trim();
      f.status = statusEl.value;
      f.category = catEl.value;
      f.order = orderEl.value;
      var q = '?search=' + encodeURIComponent(f.search) +
        '&status=' + encodeURIComponent(f.status) +
        '&category=' + encodeURIComponent(f.category) +
        '&order=' + encodeURIComponent(f.order) +
        '&limit=' + pageSize + '&offset=' + offset;
      var data = await api('GET', basePath() + '/details' + q);
      var items = data.items || [];
      clear(tbody);
      if (!items.length) {
        tbody.appendChild(h('tr', {}, [
          h('td', { colspan: '8', class: 'empty', text: '没有符合条件的记录' })
        ]));
      } else {
        items.forEach(function (r) {
          tbody.appendChild(h('tr', {}, [
            h('td', { text: r.name }),
            h('td', { class: 'mono', text: r.phoneFull }),
            h('td', { text: r.category }),
            h('td', { text: r.department }),
            h('td', { text: r.unit }),
            h('td', { text: fmt(r.createdAt) }),
            h('td', { text: r.checkedIn ? '已报到' : '未报到',
              class: r.checkedIn ? 'pill ok' : 'pill off' }),
            h('td', { text: r.source })
          ]));
        });
      }
      var total = Number(data.total || 0);
      count.textContent = '显示 ' + (total ? (offset + 1) : 0) + '–' +
        Math.min(offset + items.length, total) + ' 条，共 ' + total + ' 条记录';
      prevPage.disabled = offset <= 0;
      nextPage.disabled = offset + items.length >= total;
    }

    prevPage.addEventListener('click', function () {
      offset = Math.max(0, offset - pageSize);
      reload(false).catch(function (e) { toast(e.message, 'error'); });
    });
    nextPage.addEventListener('click', function () {
      offset += pageSize;
      reload(false).catch(function (e) { toast(e.message, 'error'); });
    });

    var searchTimer = 0;
    searchEl.addEventListener('input', function () {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(function () {
        reload(true).catch(function (e) { toast(e.message, 'error'); });
      }, 300);
    });
    searchEl.addEventListener('change', function () { reload(true).catch(function (e) { toast(e.message, 'error'); }); });
    statusEl.addEventListener('change', function () { reload(true).catch(function (e) { toast(e.message, 'error'); }); });
    catEl.addEventListener('change', function () { reload(true).catch(function (e) { toast(e.message, 'error'); }); });
    orderEl.addEventListener('change', function () { reload(true).catch(function (e) { toast(e.message, 'error'); }); });
    await reload();
  }

  // ---------------------------------------------------------------- 二维码

  async function renderQrcode(parent) {
    var a = state.current;
    var card = h('div', { class: 'card' });
    card.appendChild(h('h1', { text: '活动二维码' }));
    card.appendChild(h('p', { class: 'muted',
      text: '同一个二维码同时用于报名与现场报到，由服务端时间窗自动切换，无需更换。' }));
    card.appendChild(h('div', { class: 'divider' }));

    card.appendChild(h('div', { class: 'tiny', text: '二维码指向的页面地址：' }));
    card.appendChild(h('div', { class: 'url-box mono', text: a.qrUrl }));
    card.appendChild(h('div', { class: 'btn-row' }, [
      h('button', { class: 'btn secondary small', type: 'button', text: '复制地址',
        onclick: function () { copyText(a.qrUrl); } })
    ]));

    var sizeEl = selectEl([['6', '小'], ['10', '标准'], ['16', '大'], ['24', '超大']], '10');
    var fmtEl = selectEl([['png', 'PNG 图片'], ['svg', 'SVG 矢量']], 'png');
    var img = h('img', { alt: '活动二维码' });
    var qrBox = h('div', { class: 'qr-box' }, [img]);
    var controls = h('div', { class: 'toolbar' }, [sizeEl, fmtEl]);
    card.appendChild(controls);
    card.appendChild(h('div', { class: 'divider' }));
    card.appendChild(qrBox);

    var dlBtn = h('button', { class: 'btn', type: 'button', text: '下载二维码' });
    dlBtn.addEventListener('click', function () {
      var fmt = fmtEl.value;
      download(basePath() + '/qrcode?size=' + sizeEl.value + '&format=' + fmt,
        'qrcode_' + a.id + '.' + fmt).catch(function (e) { toast(e.message, 'error'); });
    });
    card.appendChild(h('div', { class: 'btn-row' }, [dlBtn]));
    card.appendChild(h('p', { class: 'tiny',
      text: '打印或投屏请使用 SVG/大尺寸；扫码距离较远时优先用「大」或「超大」。' }));

    parent.appendChild(card);

    parent.appendChild(h('div', { class: 'card' }, [
      h('h2', { text: '上线前四项硬约束' }),
      h('p', { class: 'tiny', text: '① 标准 443 端口；② 域名已完成 ICP 备案；③ 主流 CA 证书并自动续期；④ 零跳转直达（不短链、不 302、不 iframe、不第三方 SDK）。' }),
      h('p', { class: 'tiny', text: '二维码地址来自 PUBLIC_BASE_URL，部署时务必配置为 https://已备案域名（不带端口、不带路径），否则微信内可能被拦截。' })
    ]));

    async function refreshQr() {
      var blob = await api('GET', basePath() + '/qrcode?size=' + sizeEl.value +
        '&format=' + fmtEl.value, undefined, true);
      if (state.qrObjectUrl) URL.revokeObjectURL(state.qrObjectUrl);
      state.qrObjectUrl = URL.createObjectURL(blob);
      img.src = state.qrObjectUrl;
    }
    sizeEl.addEventListener('change', function () {
      refreshQr().catch(function (e) { toast(e.message, 'error'); });
    });
    fmtEl.addEventListener('change', function () {
      refreshQr().catch(function (e) { toast(e.message, 'error'); });
    });
    await refreshQr();
  }

  // ---------------------------------------------------------------- 白名单

  async function renderWhitelist(parent) {
    var data = await api('GET', basePath() + '/whitelist');
    var items = data.items || [];
    var card = h('div', { class: 'card' });
    card.appendChild(h('h1', { text: '邀请名单（白名单）' }));
    card.appendChild(h('p', { class: 'muted',
      text: '仅「邀请名单制」活动生效。保存时会用下方内容全量替换现有名单；服务端只保存手机号哈希，不保存明文。' }));
    card.appendChild(h('div', { class: 'divider' }));
    card.appendChild(h('p', { class: 'tiny', text: '当前名单：' + items.length + ' 人（下方显示哈希前缀用于核对，不显示明文手机号）' }));
    if (items.length) {
      var box = h('div', { class: 'sub-card' });
      items.forEach(function (w) {
        box.appendChild(h('div', { class: 'list-item' }, [
          h('span', { text: w.name || '（未填姓名）' }),
          h('span', { class: 'mono time', text: w.phoneHash })
        ]));
      });
      card.appendChild(box);
    }
    var ta = h('textarea', { class: 'mono',
      placeholder: '每行一条，格式：姓名,手机号\n例如：\n张三,13800138000\n李四,13900139000' });
    card.appendChild(h('div', { class: 'field' }, [
      h('label', { text: '名单内容（全量替换）' }), ta,
      h('div', { class: 'field-hint', text: '手机号支持空格、连字符；重复号码自动去重。' })
    ]));
    var err = h('div', { class: 'field-error' });
    var btn = h('button', { class: 'btn', type: 'button', text: '保存名单' });
    btn.addEventListener('click', async function () {
      var list = [];
      String(ta.value || '').split(/\r?\n/).forEach(function (line) {
        var s = line.trim();
        if (!s) return;
        var parts = s.split(/[,，\t]/);
        var phone = parts.length > 1 ? parts.slice(1).join('').trim() : parts[0].trim();
        var name = parts.length > 1 ? parts[0].trim() : '';
        list.push({ name: name, phone: phone });
      });
      err.textContent = '';
      btn.disabled = true;
      try {
        var d = await api('PUT', basePath() + '/whitelist', { items: list });
        toast(d.message || '名单已保存', 'success');
        ta.value = '';
        // 整页重渲染，保证「当前名单」计数与列表同步（旧方案在空名单时无容器可更新）。
        renderTabs();
        await renderTab();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
    card.appendChild(err);
    card.appendChild(btn);
    parent.appendChild(card);

    // 保存后由 renderTab() 整页重渲染，无需局部刷新。
  }

  // ---------------------------------------------------------------- 启动

  function boot() {
    // Remove persistent credentials created by earlier versions.
    try { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY); } catch (e) { /* ignore */ }
    $('login-form').addEventListener('submit', async function (ev) {
      ev.preventDefault();
      var u = String($('login-user').value || '').trim();
      var p = $('login-pass').value;
      $('login-error').textContent = '';
      if (!u || !p) { $('login-error').textContent = '请输入账号和密码'; return; }
      var btn = $('login-btn');
      btn.disabled = true; btn.textContent = '登录中…';
      try { await doLogin(u, p); }
      catch (e) { $('login-error').textContent = e.message; }
      finally { btn.disabled = false; btn.textContent = '登录'; }
    });
    $('logout-btn').addEventListener('click', function () { logout(false); });
    $('new-btn').addEventListener('click', function () { newActivity(); });
    $('refresh-btn').addEventListener('click', function () {
      loadActivities().catch(function (e) { toast(e.message, 'error'); });
    });
    $('activity-select').addEventListener('change', function () {
      if (this.value) selectActivity(this.value).catch(function (e) { toast(e.message, 'error'); });
    });

    var token = readStore(TOKEN_KEY, '');
    if (token) {
      state.token = token;
      state.user = readStore(USER_KEY, 'admin');
      showApp();
      loadActivities().catch(function (e) { toast(e.message, 'error'); });
    } else {
      showLogin('');
    }
  }

  boot();
})();
