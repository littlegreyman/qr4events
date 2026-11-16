/* M3 后台行为验证 harness（Node 内置模块，零第三方依赖）
 *
 * 在 vm 中用极简 DOM 桩真实执行 app/static/admin.js，按用户操作顺序驱动：
 * 登录 → 活动列表 → 新建/编辑活动的 snake_case 契约 → 字段配置 → 看板 →
 * 明细筛选与导出 → 二维码 blob → 白名单 → 删除 → 401 自动登出。
 * 成功输出 ADMIN_HARNESS_OK，失败抛错退出码 1。
 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const ROOT = path.join(__dirname, '..');
const SRC = fs.readFileSync(path.join(ROOT, 'app', 'static', 'admin.js'), 'utf8');

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

function findAll(node, pred, out) {
  out = out || [];
  if (!node) return out;
  if (pred(node)) out.push(node);
  (node.children || []).forEach((c) => findAll(c, pred, out));
  return out;
}

function makeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(),
    className: '', _text: '', value: '', checked: false, disabled: false,
    id: '', htmlFor: '', href: '', download: '', src: '', alt: '', type: '',
    placeholder: '', colspan: '', style: {}, children: [], listeners: {}, attrs: {},
    appendChild(n) { this.children.push(n); return n; },
    removeChild(n) {
      const i = this.children.indexOf(n);
      if (i >= 0) this.children.splice(i, 1);
      return n;
    },
    addEventListener(ev, fn) { (this.listeners[ev] || (this.listeners[ev] = [])).push(fn); },
    setAttribute(k, v) { this.attrs[k] = v; this[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    select() {}, click() {},
    querySelector(sel) {
      if (sel.charAt(0) === '.') {
        return findAll(this, (n) => n.className.split(/\s+/).indexOf(sel.slice(1)) >= 0)[0] || null;
      }
      if (sel.charAt(0) === '#') {
        return findAll(this, (n) => n.id === sel.slice(1))[0] || null;
      }
      return null;
    },
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

const IDS = [
  'toast', 'login-view', 'login-form', 'login-user', 'login-pass', 'login-error',
  'login-btn', 'app-view', 'whoami', 'logout-btn', 'activity-bar', 'activity-select',
  'new-btn', 'refresh-btn', 'tabs', 'tab-content', 'filing-line'
];
const ids = {};
IDS.forEach((id) => { const e = makeEl('div'); e.id = id; ids[id] = e; });

const errors = [];
function fire(el, ev, arg) {
  const fns = (el.listeners[ev] || []).slice();
  fns.forEach((fn) => {
    try {
      const p = fn.call(el, arg);
      if (p && typeof p.catch === 'function') p.catch((e) => errors.push(e));
    } catch (e) { errors.push(e); }
  });
}

function findText(node, needle) { return textOf(node).indexOf(needle) >= 0; }

// ---- 后端 mock ----
const calls = [];
const mock = { regMode: 'open', confirmResult: true, failNext: null };
let whitelistItems = [];
let activity = {
  id: 'abc123', name: '秋季大会', description: '年度总结会', location: '一楼报告厅',
  regStart: '2026-11-20T09:00:00+08:00', regEnd: '2026-11-20T18:00:00+08:00',
  checkInStart: '2026-11-21T08:00:00+08:00', checkInEnd: '2026-11-21T12:00:00+08:00',
  regMode: 'open', status: 'active', inviteCode: '',
  regEnabled: true, checkinEnabled: true, allowOnsiteCombo: true,
  fieldsConfig: {
    type_options: ['普通', 'VIP', '其他'],
    department: { enabled: true, required: false },
    unit: { enabled: true, required: false },
    remark: { enabled: false, required: false }
  },
  qrUrl: 'https://qr.example.com/p/abc123', regCount: 2, checkinCount: 1,
  createdAt: '2026-11-01T10:00:00+08:00', updatedAt: '2026-11-01T10:00:00+08:00'
};

function resp(status, body) {
  return {
    ok: status >= 200 && status < 300, status: status,
    json: async () => body,
    blob: async () => ({ mockBlob: true })
  };
}

async function fetch(url, opts) {
  opts = opts || {};
  const method = opts.method || 'GET';
  const headers = opts.headers || {};
  const body = opts.body ? JSON.parse(opts.body) : undefined;
  calls.push({ method, url, headers, body });
  if (mock.failNext) {
    const f = mock.failNext; mock.failNext = null;
    return resp(f.status, f.body || { detail: '未登录或登录已过期，请重新登录' });
  }

  if (url === '/healthz') {
    return resp(200, { ok: true, serverTime: '2026-11-20T10:00:00+08:00', tz: 'Asia/Shanghai' });
  }
  if (url === '/api/admin/login') {
    if (body.password === 'wrong') return resp(401, { detail: '账号或密码不正确' });
    return resp(200, { ok: true, token: 'TOKEN123', username: 'admin' });
  }
  if (url === '/api/admin/activities') {
    if (method === 'GET') return resp(200, { ok: true, items: [activity] });
    if (method === 'POST') {
      activity = Object.assign({}, activity, { id: 'newid01', name: body.name });
      return resp(200, { ok: true, activity: Object.assign({}, activity) });
    }
  }
  if (url.indexOf('/api/admin/activities/') === 0) {
    const rest = url.slice('/api/admin/activities/'.length);
    const q = rest.indexOf('?');
    const pathPart = q >= 0 ? rest.slice(0, q) : rest;
    const pieces = pathPart.split('/');
    const suffix = pieces[1] || '';
    if (!suffix) {
      if (method === 'GET') return resp(200, { ok: true, activity: Object.assign({}, activity) });
      if (method === 'PUT') {
        activity = Object.assign({}, activity, {
          name: body.name, regStart: body.reg_start, regEnd: body.reg_end,
          checkInStart: body.checkin_start, checkInEnd: body.checkin_end,
          regMode: body.reg_mode, status: body.status
        });
        return resp(200, { ok: true, activity: Object.assign({}, activity) });
      }
      if (method === 'DELETE') return resp(200, { ok: true, message: '活动及其报名/报到数据已删除' });
    }
    if (suffix === 'stats') {
      return resp(200, { ok: true, stats: {
        activityId: activity.id, regCount: 2, checkinCount: 1, pendingCount: 1,
        checkinRate: 50.0, serverTime: '2026-11-20T14:00:00+08:00',
        recent: [
          { type: 'register', name: '张三', time: '2026-11-20T09:10:00+08:00' },
          { type: 'checkin', name: '张三', time: '2026-11-21T08:05:00+08:00' }
        ]
      } });
    }
    if (suffix === 'details') {
      return resp(200, { ok: true, items: [{
        name: '张三', phoneFull: '13800138000', category: '普通', department: '技术部',
        unit: '测试单位', remark: '', createdAt: '2026-11-20T09:10:00+08:00',
        checkedIn: true, source: 'qr'
      }] });
    }
    if (suffix === 'export' || suffix === 'qrcode') return resp(200, {});
    if (suffix === 'fields') {
      if (method === 'GET') return resp(200, { ok: true, fieldsConfig: activity.fieldsConfig });
      return resp(200, { ok: true, fieldsConfig: body });
    }
    if (suffix === 'whitelist') {
      if (method === 'GET') return resp(200, { ok: true, items: whitelistItems });
      whitelistItems = (body.items || []).map(function (it, i) {
        return { id: i + 1, name: it.name, phoneHash: 'hash' + i };
      });
      return resp(200, { ok: true, count: whitelistItems.length,
        message: '白名单已更新，共 ' + whitelistItems.length + ' 人' });
    }
  }
  return resp(404, { message: 'no mock for ' + url });
}

const store = {};
const persistentStore = {};
const localStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(persistentStore, k) ? persistentStore[k] : null; },
  setItem(k, v) { persistentStore[k] = String(v); },
  removeItem(k) { delete persistentStore[k]; }
};
const sessionStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem(k, v) { store[k] = String(v); },
  removeItem(k) { delete store[k]; }
};
const documentBody = makeEl('body');
const document = {
  body: documentBody,
  getElementById(id) { return ids[id] || null; },
  createElement(tag) { return makeEl(tag); },
  createTextNode(t) { const n = makeEl('#text'); n._text = String(t); return n; },
  execCommand() { return true; }
};
const window = {
  setTimeout() { return 0; }, clearTimeout() {},
  confirm() { return mock.confirmResult; }
};
const URL = {
  createObjectURL() { return 'blob:mock-url'; },
  revokeObjectURL() {}
};
class AbortController {
  constructor() { this.signal = {}; }
  abort() {}
}

const ctx = vm.createContext({
  document, window, localStorage, sessionStorage, fetch, URL, AbortController, console
});

async function tick(n) {
  for (let i = 0; i < (n || 15); i++) await new Promise((r) => setImmediate(r));
}

function lastCall(method, fragment) {
  return calls.filter((c) => c.method === method && c.url.indexOf(fragment) >= 0).pop();
}

function buttons(label) {
  return findAll(ids['tab-content'], (n) => n.tagName === 'BUTTON' && textOf(n).indexOf(label) >= 0);
}

function tabButton(label) {
  return findAll(ids.tabs, (n) => n.tagName === 'BUTTON' && textOf(n) === label)[0];
}

async function main() {
  let passed = 0;
  const ok = (name) => { console.log('  ok  ' + name); passed++; };

  vm.runInContext(SRC, ctx, { filename: 'admin.js' });
  await tick();

  // 1. 初始为登录视图
  assert.ok(ids['login-view'].className.indexOf('hidden') < 0, '初始应显示登录页');
  assert.ok(ids['app-view'].className.indexOf('hidden') >= 0, '初始应隐藏主界面');
  ok('初始状态为登录视图');

  // 2. 登录
  ids['login-user'].value = 'admin';
  ids['login-pass'].value = 'TestAdmin!2026';
  fire(ids['login-form'], 'submit', { preventDefault() {} });
  await tick(25);
  const login = lastCall('POST', '/api/admin/login');
  assert.ok(login, '未发起登录请求');
  assert.strictEqual(login.body.username, 'admin');
  assert.strictEqual(store['qr4events.admin.token'], 'TOKEN123', 'token 未持久化');
  assert.ok(ids['app-view'].className.indexOf('hidden') < 0, '登录后未进入主界面');
  assert.ok(findText(ids['tab-content'], '秋季大会'), '概览未渲染活动名');
  assert.ok(findText(ids['tab-content'], 'https://qr.example.com/p/abc123'), '未展示二维码地址');
  const listCall = lastCall('GET', '/api/admin/activities');
  assert.strictEqual(listCall.headers.Authorization, 'Bearer TOKEN123', '列表请求未带 Bearer token');
  ok('登录成功、Bearer 鉴权、活动列表与概览渲染');

  // 3. 新建活动：camelCase → snake_case + 北京时间 ISO
  fire(ids['new-btn'], 'click');
  await tick(25);
  assert.ok(findText(ids['tab-content'], '新建活动'), '未进入新建表单');
  // 审查修复 P1：新建未提交时切换页签，不得拿空 currentId 请求 404
  fire(tabButton('数据看板'), 'click');
  await tick(20);
  assert.ok(findText(ids['tab-content'], '还没有活动'), '新建未提交时应显示空态');
  assert.ok(calls.filter((c) => c.url.indexOf('//stats') >= 0).length === 0,
    '不得用空活动 ID 请求 stats');
  fire(tabButton('编辑活动'), 'click');
  await tick(20);
  const health = lastCall('GET', '/healthz');
  assert.ok(health, '新建默认时间窗未取服务端 /healthz');
  const form = findAll(ids['tab-content'], (n) => n.tagName === 'FORM')[0];
  assert.ok(form, '未找到编辑表单');
  findAll(form, (n) => n.autocomplete === undefined && n.tagName === 'INPUT' && n.type === 'text')
    .forEach((n) => { if (!n.value) n.value = '春季大会'; });
  const dt = findAll(form, (n) => n.type === 'datetime-local');
  assert.strictEqual(dt.length, 4, '应有 4 个时间输入');
  ['2026-12-01T09:00', '2026-12-01T18:00', '2026-12-02T08:00', '2026-12-02T12:00']
    .forEach((v, i) => { dt[i].value = v; });
  fire(form, 'submit', { preventDefault() {} });
  await tick(25);
  const create = lastCall('POST', '/api/admin/activities');
  assert.ok(create, '未发起创建请求');
  assert.strictEqual(create.body.name, '春季大会');
  assert.strictEqual(create.body.reg_start, '2026-12-01T09:00:00+08:00');
  assert.strictEqual(create.body.checkin_end, '2026-12-02T12:00:00+08:00');
  assert.ok(!('regStart' in create.body), '写入不应出现 camelCase 字段');
  assert.ok(!('fields_config' in create.body), '新建不应覆盖后端默认字段配置');
  ok('新建活动：snake_case 契约与北京时间 ISO 转换');

  // 4. 编辑已有活动：PUT 必须带回 fields_config（审查修复项）
  fire(tabButton('编辑活动'), 'click');
  await tick(20);
  const editForm = findAll(ids['tab-content'], (n) => n.tagName === 'FORM')[0];
  assert.ok(editForm, '未找到编辑表单');
  fire(editForm, 'submit', { preventDefault() {} });
  await tick(25);
  const update = lastCall('PUT', '/api/admin/activities/');
  assert.ok(update, '未发起 PUT 更新');
  assert.ok(update.body.fields_config, 'PUT 未带回原字段配置，会被默认值覆盖');
  assert.deepStrictEqual(update.body.fields_config.type_options, ['普通', 'VIP', '其他']);
  ok('编辑活动：PUT 保留原字段配置');

  // 5. 字段配置保存 → PUT /fields
  fire(tabButton('字段配置'), 'click');
  await tick(20);
  const saveFields = buttons('保存字段配置')[0];
  assert.ok(saveFields, '未找到保存字段配置按钮');
  fire(saveFields, 'click');
  await tick(20);
  const putFields = lastCall('PUT', '/fields');
  assert.ok(putFields, '未保存字段配置');
  assert.ok(Array.isArray(putFields.body.type_options), 'type_options 应为数组');
  assert.ok('department' in putFields.body && 'unit' in putFields.body && 'remark' in putFields.body);
  ok('字段配置保存');

  // 6. 数据看板
  fire(tabButton('数据看板'), 'click');
  await tick(20);
  assert.ok(lastCall('GET', '/stats'), '未请求看板');
  assert.ok(findText(ids['tab-content'], '数据看板') && findText(ids['tab-content'], '50%'),
    '看板统计未渲染');
  assert.ok(findText(ids['tab-content'], '报到 · 张三'), '最近动态未渲染');
  ok('数据看板统计与动态');

  // 7. 报名明细 + 筛选 + 导出
  fire(tabButton('报名明细'), 'click');
  await tick(20);
  assert.ok(lastCall('GET', '/details'), '未请求明细');
  assert.ok(findText(ids['tab-content'], '13800138000'), '明细未展示完整手机号');
  const search = findAll(ids['tab-content'], (n) => n.tagName === 'INPUT' && n.type === 'text')[0];
  assert.ok(search, '未找到搜索框');
  search.value = '13800138000';
  fire(search, 'change');
  await tick(20);
  const filtered = calls.filter((c) => c.url.indexOf('/details?search=13800138000') >= 0);
  assert.ok(filtered.length >= 1, '搜索未带上正确查询参数');
  const exportBtn = buttons('导出 Excel')[0];
  assert.ok(exportBtn, '未找到导出按钮');
  fire(exportBtn, 'click');
  await tick(20);
  assert.ok(lastCall('GET', '/export?format=xlsx'), '未请求导出');
  ok('报名明细、筛选与导出');

  // 8. 二维码 blob + 下载
  fire(tabButton('二维码'), 'click');
  await tick(20);
  assert.ok(lastCall('GET', '/qrcode'), '未请求二维码');
  const img = findAll(ids['tab-content'], (n) => n.tagName === 'IMG')[0];
  assert.ok(img && img.src === 'blob:mock-url', '二维码未通过 blob 渲染');
  const dl = buttons('下载二维码')[0];
  assert.ok(dl, '未找到下载按钮');
  fire(dl, 'click');
  await tick(20);
  assert.ok(calls.filter((c) => c.url.indexOf('/qrcode?size=') >= 0).length >= 2, '下载未请求二维码');
  ok('二维码 blob 渲染与下载（携带鉴权头）');

  // 9. 白名单：切换为名单制后 tab 出现，PUT /whitelist
  activity.regMode = 'whitelist';
  fire(ids['refresh-btn'], 'click');
  await tick(25);
  const wlTab = tabButton('白名单');
  assert.ok(wlTab, '白名单制下未出现白名单 tab');
  fire(wlTab, 'click');
  await tick(20);
  assert.ok(lastCall('GET', '/whitelist'), '未读取白名单');
  const ta = findAll(ids['tab-content'], (n) => n.tagName === 'TEXTAREA')[0];
  assert.ok(ta, '未找到名单输入框');
  ta.value = '张三,13800138000\n李四，13900139000';
  const saveWl = buttons('保存名单')[0];
  assert.ok(saveWl, '未找到保存名单按钮');
  fire(saveWl, 'click');
  await tick(20);
  const putWl = lastCall('PUT', '/whitelist');
  assert.ok(putWl, '未保存白名单');
  assert.strictEqual(putWl.body.items.length, 2, '名单解析条数不对');
  assert.strictEqual(putWl.body.items[0].phone, '13800138000');
  assert.strictEqual(putWl.body.items[1].phone, '13900139000', '中文逗号未正确分隔');
  assert.ok(findText(ids['tab-content'], '当前名单：2 人'),
    '审查修复 P1：保存后名单计数未刷新');
  ok('白名单全量替换、逐行解析与保存后刷新');

  // 10. 删除活动（confirm）
  fire(tabButton('概览'), 'click');
  await tick(20);
  const del = buttons('删除该活动')[0];
  assert.ok(del, '未找到删除按钮');
  fire(del, 'click');
  await tick(20);
  assert.ok(lastCall('DELETE', '/api/admin/activities/'), '未发起删除请求');
  ok('删除活动（二次确认）');

  // 11. 401 自动登出
  mock.failNext = { status: 401 };
  fire(ids['refresh-btn'], 'click');
  await tick(25);
  assert.ok(ids['login-view'].className.indexOf('hidden') < 0, '401 后未回到登录页');
  assert.ok(ids['app-view'].className.indexOf('hidden') >= 0, '401 后未隐藏主界面');
  assert.ok(!store['qr4events.admin.token'], '401 后未清除 token');
  ok('401 自动登出并清除凭据');

  assert.ok(errors.length === 0, '存在未捕获的异步错误: ' + errors.map(String).join(' | '));
  console.log('ADMIN_HARNESS_OK scenarios=' + passed);
}

main().catch((e) => {
  console.error('ADMIN_HARNESS_FAIL', e && e.stack ? e.stack : e);
  process.exit(1);
});
