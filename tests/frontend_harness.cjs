/* M2 前端行为验证 harness（Node 内置模块，零第三方依赖）
 *
 * 用极简 DOM 桩在 vm 中真实执行 app/static/p.js，覆盖：
 *  - 各 action 分支（info/message/button/form/combo_form）的渲染
 *  - fieldsConfig 动态字段、regMode（whitelist/invite）分支
 *  - 隐私勾选与手机号校验、提交报名后的凭证页
 *  - 网络异常时的错误兜底
 *  - filing.js 备案号渲染 / 未备案时不渲染
 * 成功输出 FRONTEND_HARNESS_OK，失败抛 AssertionError 并以非 0 退出。
 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const ROOT = path.join(__dirname, '..');
const P_SRC = fs.readFileSync(path.join(ROOT, 'app', 'static', 'p.js'), 'utf8');
const FILING_SRC = fs.readFileSync(path.join(ROOT, 'app', 'static', 'filing.js'), 'utf8');
const AID = 'abc123';
const BASE = '/api/activity/' + AID + '/';

function makeClassList(node) {
  const set = new Set();
  const sync = () => { node.className = Array.from(set).join(' '); };
  return {
    add(c) { set.add(c); sync(); },
    remove(c) { set.delete(c); sync(); },
    toggle(c, force) {
      if (force === undefined) { if (set.has(c)) set.delete(c); else set.add(c); }
      else if (force) set.add(c); else set.delete(c);
      sync();
    },
    contains(c) { return set.has(c); }
  };
}

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: '', _text: '',
    value: '', checked: false, disabled: false,
    id: '', htmlFor: '', href: '', target: '', rel: '',
    type: '', placeholder: '', maxLength: 0, autocomplete: '', inputMode: '',
    noValidate: false, options: [],
    children: [], listeners: {},
    appendChild(n) { this.children.push(n); return n; },
    removeChild(n) {
      const i = this.children.indexOf(n);
      if (i >= 0) this.children.splice(i, 1);
      return n;
    },
    addEventListener(ev, fn) { (this.listeners[ev] || (this.listeners[ev] = [])).push(fn); },
    setAttribute(k, v) { this[k] = v; },
    get firstChild() { return this.children.length ? this.children[0] : null; },
    get textContent() {
      let t = this._text;
      for (const c of this.children) t += (c.textContent || '');
      return t;
    },
    set textContent(v) { this._text = v == null ? '' : String(v); this.children = []; }
  };
  el.classList = makeClassList(el);
  return el;
}

function textOf(node) { return (node && node.textContent) || ''; }

function findAll(node, pred, out) {
  out = out || [];
  if (!node) return out;
  if (pred(node)) out.push(node);
  (node.children || []).forEach((c) => findAll(c, pred, out));
  return out;
}

function baseStatus(over) {
  return Object.assign({
    ok: true,
    stage: 'registration', userState: 'unknown', action: 'form',
    message: '请填写以下信息完成报名', combo: false, needsPhone: true,
    serverTime: '2026-11-20T09:00:00+08:00',
    fieldsConfig: {
      type_options: ['普通', 'VIP', '其他'],
      department: { enabled: true, required: false },
      unit: { enabled: true, required: false },
      remark: { enabled: false, required: false }
    },
    activityInfo: {
      id: AID, name: '测试大会', description: '一场测试活动', location: '一楼大厅',
      regStart: '2026-11-20T08:00:00+08:00', regEnd: '2026-11-20T12:00:00+08:00',
      checkInStart: '2026-11-20T13:00:00+08:00', checkInEnd: '2026-11-20T18:00:00+08:00',
      regMode: 'open', status: 'active'
    }
  }, over || {});
}

/* 运行一次 p.js，返回 { ids, calls, state } */
async function run(statusOver, opts) {
  opts = opts || {};
  const ids = {};
  ['toast', 'loading', 'app', 'activity-card', 'identity-card', 'main',
   'phone-lookup', 'lookup-btn', 'lookup-error'].forEach((id) => {
    const el = makeEl('div'); el.id = id; ids[id] = el;
  });
  const store = {};
  Object.assign(store, opts.seedStore || {});
  const calls = [];
  const responses = {};
  responses[BASE + 'status'] = { ok: true, status: 200, body: baseStatus(statusOver) };
  responses[BASE + 'register'] = {
    ok: true, status: 200,
    body: {
      ok: true, status: 'registered', checkedIn: false,
      message: '报名成功，请在活动开始后扫码报到',
      serverTime: '2026-11-20T14:30:00+08:00',
      registration: { name: '张三', phone: '138****8000', category: '普通', activityName: '测试大会' }
    }
  };
  responses[BASE + 'checkin'] = {
    ok: true, status: 200,
    body: {
      ok: true, status: 'checked_in', checkedIn: true, message: '报到成功，欢迎参加本次活动',
      serverTime: '2026-11-20T14:30:00+08:00',
      registration: { name: '张三', phone: '138****8000', category: '普通', activityName: '测试大会' }
    }
  };

  const document = {
    querySelector(sel) { return sel.charAt(0) === '#' ? (ids[sel.slice(1)] || null) : null; },
    getElementById(id) { return ids[id] || null; },
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { const n = makeEl('#text'); n._text = String(t); return n; }
  };
  const winListeners = {};
  const window = {
    addEventListener(ev, fn) { (winListeners[ev] || (winListeners[ev] = [])).push(fn); },
    setTimeout() { return 0; },
    clearTimeout() {}
  };
  const localStorage = {
    getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); }
  };
  async function fetch(url, reqOpts) {
    calls.push({ url: url, body: JSON.parse((reqOpts && reqOpts.body) || '{}') });
    if (url.indexOf(BASE + 'status') === 0 && opts.failNetwork) {
      throw new Error('network down');
    }
    const key = url.split('?')[0];
    const entry = responses[key];
    if (!entry) {
      return { ok: false, status: 404, json: async () => ({ message: 'no mock for ' + url }) };
    }
    return {
      ok: entry.ok !== false, status: entry.status || 200,
      json: async () => entry.body
    };
  }
  const ctx = vm.createContext({
    document, window, location: { pathname: '/p/' + AID }, localStorage, fetch,
    console, encodeURIComponent, Date, JSON, Error, RegExp, Promise, Math
  });
  vm.runInContext(P_SRC, ctx, { filename: 'p.js' });
  await tick(12);
  return { ids, calls, store };
}

async function tick(n) {
  for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r));
}

async function scenario(name, statusOver, check, opts) {
  const r = await run(statusOver, opts);
  check(r);
  console.log('  ok  ' + name);
}

async function main() {
  let passed = 0;

  await scenario('报名期渲染动态表单（字段配置+隐私勾选）', { action: 'form' }, (r) => {
    const t = textOf(r.ids.main);
    ['活动报名', '姓名', '手机号', '类型', '所属部门', '单位', '隐私政策', '提交报名']
      .forEach((k) => assert.ok(t.indexOf(k) >= 0, '缺少: ' + k));
    assert.ok(t.indexOf('备注') < 0, 'remark 未启用却渲染了');
    assert.ok(r.ids.loading.className.indexOf('hidden') >= 0, '骨架屏未隐藏');
    assert.ok(r.ids.app.className.indexOf('hidden') < 0, '主内容未显示');
    assert.ok(r.ids['identity-card'].className.indexOf('hidden') < 0, 'form 应显示身份查询卡');
  });
  passed++;

  await scenario('报到期已报名 → 一键签到按钮', {
    action: 'button', stage: 'checkin', userState: 'registered',
    message: '您已报名成功，点击下方按钮完成现场报到'
  }, (r) => {
    const t = textOf(r.ids.main);
    assert.ok(t.indexOf('现场报到') >= 0 && t.indexOf('一键签到') >= 0, t);
  });
  passed++;

  await scenario('报到期未报名且允许 2合1 → combo 表单', {
    action: 'combo_form', stage: 'checkin', userState: 'not_registered', combo: true,
    message: '现场报名 + 报到：填写信息后一次完成'
  }, (r) => {
    const t = textOf(r.ids.main);
    assert.ok(t.indexOf('现场报名 + 报到') >= 0 && t.indexOf('提交并完成报到') >= 0, t);
  });
  passed++;

  await scenario('未开始 → info 且隐藏身份查询卡', {
    action: 'info', stage: 'not_started', message: '报名未开始，请在报名开放后扫码'
  }, (r) => {
    assert.ok(textOf(r.ids.main).indexOf('报名未开始') >= 0, '未渲染 info');
    assert.ok(r.ids['identity-card'].className.indexOf('hidden') >= 0, 'info 应隐藏身份卡');
  });
  passed++;

  await scenario('报名已截止 → message 兜底', {
    action: 'message', stage: 'waiting', message: '报名已截止，如有疑问请联系组织者'
  }, (r) => {
    assert.ok(textOf(r.ids.main).indexOf('报名已截止') >= 0, '未渲染 message');
  });
  passed++;

  await scenario('白名单制 → 名单制提示', {
    action: 'form',
    activityInfo: Object.assign(baseStatus().activityInfo, { regMode: 'whitelist' })
  }, (r) => {
    assert.ok(textOf(r.ids.main).indexOf('邀请名单制') >= 0, '缺少名单制提示');
  });
  passed++;

  await scenario('邀请码制 → 邀请码字段', {
    action: 'form',
    activityInfo: Object.assign(baseStatus().activityInfo, { regMode: 'invite' })
  }, (r) => {
    assert.ok(textOf(r.ids.main).indexOf('邀请码') >= 0, '缺少邀请码字段');
  });
  passed++;

  await scenario('表单校验与提交 → 凭证页', { action: 'form' }, async (r) => {
    const form = findAll(r.ids.main, (n) => n.tagName === 'FORM')[0];
    assert.ok(form, '未找到 form');
    const submit = (form.listeners['submit'] || [])[0];
    assert.ok(submit, '未绑定 submit');

    // 1) 空表单 + 未勾选隐私 → 必须被拦下且提示隐私政策
    await submit({ preventDefault() {} });
    await tick(6);
    assert.ok(textOf(r.ids.main).indexOf('请先阅读并同意隐私政策') >= 0, '隐私校验缺失');
    assert.ok(r.calls.filter((c) => c.url.indexOf('register') >= 0).length === 0, '不应发起请求');

    // 2) 填写完整并勾选 → 提交 register，渲染凭证页
    findAll(form, (n) => n.autocomplete === 'name').forEach((n) => { n.value = '张三'; });
    findAll(form, (n) => n.type === 'tel').forEach((n) => { n.value = '13800138000'; });
    findAll(form, (n) => n.tagName === 'SELECT').forEach((n) => { n.value = '普通'; });
    findAll(form, (n) => n.type === 'checkbox').forEach((n) => { n.checked = true; });

    await submit({ preventDefault() {} });
    await tick(8);
    const regCalls = r.calls.filter((c) => c.url.indexOf('register') >= 0);
    assert.ok(regCalls.length === 1, 'register 未被调用');
    assert.strictEqual(regCalls[0].body.phone, '13800138000');
    assert.strictEqual(regCalls[0].body.source, 'qr');
    const t = textOf(r.ids.main);
    assert.ok(t.indexOf('报名成功') >= 0, '未渲染凭证: ' + t);
    assert.ok(t.indexOf('138****8000') >= 0, '凭证应展示脱敏手机号');
    assert.ok(t.indexOf('11-20 14:30') >= 0, '凭证应使用服务端 serverTime');
  });
  passed++;

  await scenario('网络异常 → 错误兜底卡片', {}, (r) => {
    assert.ok(textOf(r.ids.main).indexOf('暂时无法加载活动') >= 0, '未渲染错误兜底');
    assert.ok(textOf(r.ids.main).indexOf('重新加载') >= 0, '缺少重试按钮');
  }, { failNetwork: true });
  passed++;

  await scenario('弱网暂存队列重放 → 清空队列并刷新状态', {}, (r) => {
    const regCalls = r.calls.filter((c) => c.url.indexOf('register') >= 0);
    assert.ok(regCalls.length === 1, '队列未重放');
    const left = JSON.parse(r.store['qr4events.queue.' + AID] || '[]');
    assert.ok(left.length === 0, '重放成功后队列未清空');
    assert.ok(r.calls.filter((c) => c.url.indexOf('status') >= 0).length >= 2,
      '重放成功后未刷新状态');
  }, {
    seedStore: {
      ['qr4events.queue.' + AID]: JSON.stringify([{
        url: BASE + 'register',
        payload: { phone: '13800138000', name: '张三', category: '普通', source: 'qr' }
      }])
    }
  });
  passed++;

  // filing.js：备案号渲染 / 未备案时不渲染
  function runFiling(override) {
    const host = makeEl('span'); host.id = 'filing-line';
    const document = {
      getElementById(id) { return id === 'filing-line' ? host : null; },
      createElement(tag) { return makeEl(tag); },
      createTextNode(t) { const n = makeEl('#text'); n._text = String(t); return n; }
    };
    const ctx = vm.createContext({ window: { QR4EVENTS_FILING: override }, document, console });
    vm.runInContext(FILING_SRC, ctx, { filename: 'filing.js' });
    return host;
  }
  const withFiling = runFiling({ icp: '粤ICP备2026000000号', police: '粤公网安备44030000000000号' });
  assert.ok(withFiling.children.length >= 1, '备案号未渲染');
  const links = findAll(withFiling, (n) => n.tagName === 'A');
  assert.ok(links.length === 2, '应有两个备案链接');
  assert.ok(links[0].href.indexOf('beian.miit.gov.cn') >= 0, 'ICP 链接不正确');
  assert.ok(links[1].href.indexOf('beian.gov.cn') >= 0, '公安备案链接不正确');
  assert.ok(textOf(withFiling).indexOf('粤ICP备2026000000号') >= 0);
  console.log('  ok  备案号悬挂（ICP + 公安联网备案）');
  passed++;

  const emptyFiling = runFiling(undefined);
  assert.ok(emptyFiling.children.length === 0, '未备案时不应渲染任何备案信息');
  console.log('  ok  未备案时不渲染占位备案号');
  passed++;

  console.log('FRONTEND_HARNESS_OK scenarios=' + passed);
}

main().catch((e) => {
  console.error('FRONTEND_HARNESS_FAIL', e && e.stack ? e.stack : e);
  process.exit(1);
});
