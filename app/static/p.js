/* 参与者页 · 纯原生 JS，零平台 SDK、零跳转、零第三方依赖 */
(function () {
  'use strict';

  var segments = location.pathname.split('/').filter(Boolean);
  var activityId = segments[segments.length - 1] || '';
  var KEY_PHONE = 'qr4events.phone.' + activityId;
  var KEY_QUEUE = 'qr4events.queue.' + activityId;
  var PHONE_RE = /^1[3-9]\d{9}$/;
  var STAGE_LABELS = {
    not_started: '未开始', registration: '报名中', waiting: '报名已截止',
    checkin: '报到中', ended: '已结束'
  };
  var LABELS = {
    name: '姓名', phone: '手机号', category: '类型', department: '所属部门',
    unit: '单位', remark: '备注', invite_code: '邀请码'
  };

  var state = { status: null, phone: '', busy: false, view: 'main', ticket: null };

  function $(sel) { return document.querySelector(sel); }

  function node(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

  function toast(message, kind) {
    var t = $('#toast');
    t.className = 'toast show ' + (kind || 'info');
    t.textContent = message;
    window.clearTimeout(toast._timer);
    if (kind !== 'success') {
      toast._timer = window.setTimeout(function () { t.className = 'toast'; }, 6500);
    }
  }

  function cleanPhone(raw) {
    return String(raw || '').replace(/[\s\-()]/g, '').replace(/^\+86/, '');
  }

  function fmt(iso) {
    if (!iso) return '';
    var m = String(iso).match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    return m ? (m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]) : String(iso);
  }

  function api(path) { return '/api/activity/' + encodeURIComponent(activityId) + '/' + path; }

  async function post(url, body) {
    var res;
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = controller
      ? window.setTimeout(function () { controller.abort(); }, 12000) : 0;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
        signal: controller ? controller.signal : undefined
      });
    } catch (e) {
      var ne = new Error('网络连接不稳定，请稍后重试');
      ne.network = true;
      throw ne;
    } finally {
      if (controller) window.clearTimeout(timer);
    }
    var data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) {
      var err = new Error(data.message || ('请求失败（' + res.status + '）'));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function setBusy(btn, busy, text) {
    btn.disabled = !!busy;
    btn.textContent = text;
  }

  function readStore(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }

  function writeStore(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* 隐私模式下忽略 */ }
  }

  async function loadStatus(phone) {
    try {
      var data = await post(api('status'), phone ? { phone: phone } : {});
      state.status = data;
      if (phone) {
        state.phone = phone;
        writeStore(KEY_PHONE, phone);
      }
      state.view = 'main';
      state.ticket = null;
      render();
    } catch (e) {
      $('#loading').classList.add('hidden');
      $('#app').classList.remove('hidden');
      clear($('#activity-card'));
      clear($('#identity-card'));
      toast(e.message, 'error');
      var main = $('#main');
      clear(main);
      var card = node('div', 'card');
      card.appendChild(node('h2', '', '暂时无法加载活动'));
      card.appendChild(node('p', 'muted', e.message));
      var btn = node('button', 'btn', '重新加载');
      btn.type = 'button';
      btn.onclick = function () {
        $('#loading').classList.remove('hidden');
        loadStatus(state.phone || null);
      };
      card.appendChild(btn);
      main.appendChild(card);
    }
  }

  function infoTitle(s) {
    if (s.userState === 'checked_in') return '已完成报到';
    if (s.stage === 'ended') return '活动已结束';
    if (s.stage === 'not_started') return '报名未开始';
    if (s.stage === 'waiting') return '报名已截止';
    if (s.stage === 'checkin') return '现场报到';
    return '活动报名';
  }

  function renderActivity(s) {
    var info = s.activityInfo || {};
    var card = $('#activity-card');
    clear(card);
    card.appendChild(node('h1', '', info.name || '活动报名与签到'));
    if (info.description) card.appendChild(node('p', 'muted', info.description));
    var badge = node('span', 'badge' + (s.stage === 'checkin' ? ' ok' : ''),
      STAGE_LABELS[s.stage] || '活动');
    card.appendChild(badge);
    card.appendChild(node('div', 'divider'));

    function meta(label, value) {
      if (!value) return;
      var row = node('div', 'meta-row');
      row.appendChild(node('span', 'meta-label', label));
      row.appendChild(node('span', 'meta-value', value));
      card.appendChild(row);
    }
    meta('地点', info.location);
    if (info.regStart && info.regEnd) {
      meta('报名', fmt(info.regStart) + ' — ' + fmt(info.regEnd));
    }
    if (info.checkInStart && info.checkInEnd) {
      meta('报到', fmt(info.checkInStart) + ' — ' + fmt(info.checkInEnd));
    }
    var mode = info.regMode;
    if (mode === 'whitelist') meta('资格', '邀请名单制');
    if (mode === 'invite') meta('资格', '邀请码制');
  }

  function renderInfo(parent, s, tone) {
    var card = node('div', 'card');
    var box = node('div', 'status-card');
    box.appendChild(node('div', 'status-icon ' + (tone || ''),
      tone === 'ok' ? '✓' : (tone === 'warn' ? '!' : (tone === 'ended' ? '·' : 'i'))));
    var right = node('div');
    right.appendChild(node('div', 'status-title', infoTitle(s)));
    right.appendChild(node('p', 'status-msg', s.message || ''));
    box.appendChild(right);
    card.appendChild(box);
    if (s.needsPhone) {
      card.appendChild(node('p', 'tiny',
        '输入上方手机号可查看您是否已报名、是否需要报到。'));
    }
    parent.appendChild(card);
  }

  function makeTicket(data, status) {
    var r = data.registration || {};
    var checked = data.checkedIn === true || status === 'already_checked_in' ||
      status === 'checked_in' || status === 'registered_and_checked_in';
    var repeat = status === 'already_registered' || status === 'already_checked_in';
    return {
      title: repeat ? '您已提交过' : (checked ? '报到成功' : '报名成功'),
      sub: data.message || '',
      name: r.name || '',
      phone: r.phone || (state.phone ? state.phone.slice(0, 3) + '****' + state.phone.slice(-4) : ''),
      category: r.category || '',
      activityName: r.activityName || (state.status && state.status.activityInfo ?
        state.status.activityInfo.name : ''),
      stateText: checked ? '已报到' : '已报名',
      time: fmt(data.serverTime ||
        (state.status && state.status.serverTime) || new Date().toISOString())
    };
  }

  function renderTicket(parent) {
    var t = state.ticket || {};
    var card = node('div', 'card ticket');
    card.appendChild(node('div', 'ticket-mark', '✓'));
    card.appendChild(node('div', 'ticket-title', t.title || '提交成功'));
    card.appendChild(node('div', 'ticket-sub', t.sub || '请截图留存本页凭证'));
    var box = node('div', 'ticket-box');
    [['活动', t.activityName], ['姓名', t.name], ['手机号', t.phone],
     ['类型', t.category], ['状态', t.stateText], ['时间', t.time]].forEach(function (pair) {
      if (!pair[1]) return;
      var line = node('div', 'ticket-line');
      line.appendChild(node('span', '', pair[0]));
      line.appendChild(node('span', '', pair[1]));
      box.appendChild(line);
    });
    card.appendChild(box);
    var btn = node('button', 'btn secondary', '返回查看状态');
    btn.type = 'button';
    btn.onclick = function () { loadStatus(state.phone || null); };
    card.appendChild(btn);
    parent.appendChild(card);
  }

  function handleResult(data) {
    var status = data.status || '';
    state.ticket = makeTicket(data, status);
    state.view = 'ticket';
    renderMain(state.status);
    toast(data.message || '提交成功', 'success');
  }

  function handleError(e, queueItem) {
    if (e.network && queueItem) {
      try {
        var q = JSON.parse(readStore(KEY_QUEUE, '[]'));
        q.push(queueItem);
        writeStore(KEY_QUEUE, JSON.stringify(q));
        toast('网络不稳定，提交已暂存，恢复网络后自动重试', 'error');
      } catch (err) { toast(e.message, 'error'); }
    } else {
      toast(e.message, 'error');
    }
  }

  async function flushQueue() {
    var q = [];
    try { q = JSON.parse(readStore(KEY_QUEUE, '[]')); } catch (e) { return; }
    if (!q.length) return;
    var remain = [];
    for (var i = 0; i < q.length; i++) {
      try { await post(q[i].url, q[i].payload); } catch (e) { remain.push(q[i]); }
    }
    writeStore(KEY_QUEUE, JSON.stringify(remain));
    if (remain.length < q.length) {
      toast('暂存的提交已自动重试成功', 'success');
      await loadStatus(state.phone || null);
    }
  }

  function renderCheckinButton(parent, s) {
    var card = node('div', 'card');
    card.appendChild(node('h2', '', '现场报到'));
    card.appendChild(node('p', 'muted', s.message || '点击下方按钮完成签到'));
    var btn = node('button', 'btn ok', '一键签到');
    btn.type = 'button';
    btn.onclick = async function () {
      if (!state.phone) { toast('请先输入手机号查询状态', 'error'); return; }
      if (state.busy) return;
      state.busy = true;
      setBusy(btn, true, '正在签到…');
      var payload = { phone: state.phone, source: 'qr' };
      try {
        handleResult(await post(api('checkin'), payload));
      } catch (e) {
        handleError(e, { url: api('checkin'), payload: payload });
      } finally {
        state.busy = false;
        setBusy(btn, false, '一键签到');
      }
    };
    card.appendChild(btn);
    parent.appendChild(card);
  }

  function renderForm(parent, s, isCombo) {
    var cfg = s.fieldsConfig || {};
    var card = node('div', 'card');
    card.appendChild(node('h2', '', isCombo ? '现场报名 + 报到' : '活动报名'));
    card.appendChild(node('p', 'muted', s.message || '请填写以下信息完成报名'));
    var regMode = (s.activityInfo && s.activityInfo.regMode) || 'open';
    if (regMode === 'whitelist') {
      card.appendChild(node('p', 'tiny', '本活动为邀请名单制，仅名单内手机号可报名。'));
    }

    var form = node('form');
    form.noValidate = true;
    var fields = {};

    function addField(labelText, required, inputEl, hint) {
      var wrap = node('div', 'field');
      var label = node('label', '', labelText);
      if (required) label.appendChild(node('span', 'req', '*'));
      wrap.appendChild(label);
      wrap.appendChild(inputEl);
      if (hint) wrap.appendChild(node('div', 'field-hint', hint));
      var err = node('div', 'field-error');
      wrap.appendChild(err);
      form.appendChild(wrap);
      return err;
    }

    var nameInput = node('input');
    nameInput.type = 'text'; nameInput.autocomplete = 'name';
    nameInput.maxLength = 100; nameInput.placeholder = '请输入姓名';
    fields.name = { el: nameInput, required: true,
      err: addField('姓名', true, nameInput) };

    var phoneInput = node('input');
    phoneInput.type = 'tel'; phoneInput.inputMode = 'numeric';
    phoneInput.autocomplete = 'tel'; phoneInput.maxLength = 20;
    phoneInput.placeholder = '请输入手机号'; phoneInput.value = state.phone || '';
    fields.phone = { el: phoneInput, required: true,
      err: addField('手机号', true, phoneInput, '用于去重与签到识别，仅组织者可见') };

    var options = (cfg.type_options && cfg.type_options.length) ? cfg.type_options : ['普通'];
    var catSel = node('select');
    var placeholder = node('option', '', '请选择类型');
    placeholder.value = '';
    catSel.appendChild(placeholder);
    options.forEach(function (o) {
      var op = node('option', '', o); op.value = o; catSel.appendChild(op);
    });
    fields.category = { el: catSel, required: true, options: options,
      err: addField('类型', true, catSel) };

    [['department', '所属部门'], ['unit', '单位'], ['remark', '备注']].forEach(function (pair) {
      var key = pair[0], label = pair[1];
      var c = cfg[key] || {};
      if (!c.enabled) return;
      var input;
      if (key === 'remark') {
        input = node('textarea');
        input.maxLength = 2000; input.placeholder = '选填';
      } else {
        input = node('input');
        input.type = 'text'; input.maxLength = 200;
        input.placeholder = c.required ? '必填' : '选填';
      }
      fields[key] = { el: input, required: !!c.required,
        err: addField(label, !!c.required, input) };
    });

    if (regMode === 'invite') {
      var inv = node('input');
      inv.type = 'text'; inv.maxLength = 64; inv.placeholder = '请输入邀请码';
      fields.invite_code = { el: inv, required: true,
        err: addField('邀请码', true, inv) };
    }

    var checkRow = node('div', 'check-row');
    var chk = node('input');
    chk.type = 'checkbox'; chk.id = 'privacy-check';
    var lab = node('label', '');
    lab.htmlFor = 'privacy-check';
    lab.appendChild(document.createTextNode('我已阅读并同意 '));
    var link = node('a', '', '《隐私政策》');
    link.href = '/privacy'; link.target = '_blank'; link.rel = 'noopener';
    lab.appendChild(link);
    lab.appendChild(document.createTextNode('，同意为本次活动登记之目的收集上述信息。'));
    checkRow.appendChild(chk); checkRow.appendChild(lab);
    form.appendChild(checkRow);
    var privacyErr = node('div', 'field-error');
    form.appendChild(privacyErr);

    var submitBtn = node('button', 'btn', isCombo ? '提交并完成报到' : '提交报名');
    submitBtn.type = 'submit';
    form.appendChild(submitBtn);
    form.appendChild(node('p', 'tiny',
      '提交成功后显示凭证，可截图留存；重复提交不会重复计数。'));

    // 输入手机号后自动识别身份：已报名者立刻切换为一键报到，避免白填表单
    var blurTimer = null;
    phoneInput.addEventListener('blur', function () {
      var p = cleanPhone(phoneInput.value);
      if (!PHONE_RE.test(p) || p === state.phone) return;
      window.clearTimeout(blurTimer);
      blurTimer = window.setTimeout(async function () {
        try {
          var data = await post(api('status'), { phone: p });
          if (data.action !== 'form' && data.action !== 'combo_form') {
            state.status = data; state.phone = p; writeStore(KEY_PHONE, p);
            state.view = 'main'; state.ticket = null;
            render();
          }
        } catch (e) { /* 身份识别失败不阻塞填表 */ }
      }, 300);
    });
    phoneInput.addEventListener('input', function () {
      $('#phone-lookup').value = cleanPhone(phoneInput.value);
    });

    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      if (state.busy) return;
      var payload = {};
      var ok = true;
      Object.keys(fields).forEach(function (k) {
        var f = fields[k];
        var v = String(f.el.value || '').trim();
        f.err.textContent = '';
        if (f.required && !v) {
          f.err.textContent = (LABELS[k] || k) + '不能为空';
          ok = false;
        }
        payload[k] = v;
      });
      if (payload.phone && !PHONE_RE.test(payload.phone)) {
        fields.phone.err.textContent = '请输入正确的 11 位手机号';
        ok = false;
      }
      if (payload.category && fields.category.options.indexOf(payload.category) < 0) {
        fields.category.err.textContent = '请选择有效的类型';
        ok = false;
      }
      if (!chk.checked) {
        privacyErr.textContent = '请先阅读并同意隐私政策';
        ok = false;
      }
      if (!ok) { toast('请检查表单填写', 'error'); return; }
      payload.source = 'qr';

      state.busy = true;
      setBusy(submitBtn, true, '正在提交…');
      var url = api('register');
      try {
        var data = await post(url, payload);
        state.phone = payload.phone;
        writeStore(KEY_PHONE, payload.phone);
        handleResult(data);
      } catch (e) {
        handleError(e, { url: url, payload: payload });
      } finally {
        state.busy = false;
        setBusy(submitBtn, false, isCombo ? '提交并完成报到' : '提交报名');
      }
    });

    card.appendChild(form);
    parent.appendChild(card);
  }

  function renderMain(s) {
    var main = $('#main');
    clear(main);
    if (state.view === 'ticket') { renderTicket(main); return; }
    switch (s.action) {
      case 'info':
        renderInfo(main, s, s.userState === 'checked_in' ? 'ok' :
          (s.stage === 'ended' ? 'ended' : ''));
        break;
      case 'message': renderInfo(main, s, 'warn'); break;
      case 'button': renderCheckinButton(main, s); break;
      case 'form': renderForm(main, s, false); break;
      case 'combo_form': renderForm(main, s, true); break;
      default: renderInfo(main, s, '');
    }
  }

  function render() {
    var s = state.status;
    if (!s) return;
    $('#loading').classList.add('hidden');
    $('#app').classList.remove('hidden');
    renderActivity(s);
    $('#identity-card').classList.toggle('hidden', s.action === 'info');
    var input = $('#phone-lookup');
    if (state.phone && !input.value) input.value = state.phone;
    renderMain(s);
  }

  function doLookup() {
    var p = cleanPhone($('#phone-lookup').value);
    var err = $('#lookup-error');
    if (!PHONE_RE.test(p)) { err.textContent = '请输入正确的 11 位手机号'; return; }
    err.textContent = '';
    var btn = $('#lookup-btn');
    btn.disabled = true;
    loadStatus(p).finally(function () { btn.disabled = false; });
  }

  $('#lookup-btn').addEventListener('click', doLookup);
  $('#phone-lookup').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); doLookup(); }
  });
  window.addEventListener('online', flushQueue);

  var cached = cleanPhone(readStore(KEY_PHONE, ''));
  loadStatus(cached || null);
  flushQueue();
})();
