# 方案 B″：基于腾讯云开发 CloudBase 的深入研究

| 项 | 内容 |
|---|---|
| 版本 | V1.0（2026-11-07） |
| 状态 | **方案研究，未实施**；未建代码骨架、未开通云资源 |
| 定位 | A′ 微信云托管方案的第一退路；与 `设计方案-A零域名.md` 配套阅读 |
| 约束前提 | 个人主体小程序、低频活动使用、零域名、零常驻服务器、不用时零费用 |

---

## 0. 结论摘要

**技术上完全可行，而且比我上一轮说的更容易；但在你最在意的两件事（不用时零费用、不用常年管理）上，CloudBase 反而比 A′ 更差。**

- 好消息：FastAPI **不用重写成 Node**（HTTP 云函数原生支持 Python + FastAPI）；个人主体开通云服务的官方口径比云托管更宽松；小程序码可用云调用免 `access_token`。
- 坏消息：CloudBase 是**资源点套餐制**，免费环境在小程序上线后第 15 天到期、需转付费，付费个人版 19.9 元/月常年扣；默认域名在浏览器里有「访问提示中间页」，官方称生产环境浏览器访问必须用已备案自定义域名 —— 域名和备案有被请回来的风险。
- 另一个时效变化：**2026-10-20 起 CloudBase 新环境不再支持新开通 MySQL**，只能选文档型数据库或 PostgreSQL。

**明确推荐：维持 A′ 微信云托管为首选，CloudBase 作为「云托管试建失败时的第一退路」。** 更关键的是：两方案共用约 90% 的代码基座，**不必现在二选一** —— 先按共用基座开发，等 5 分钟的云托管试建结果出来再决定部署层。

---

## 1. 先更正我上一轮的两处误判

上一轮我把 CloudBase 写成「需把 FastAPI 重写为 Node 云函数、放弃 Docker 与 MySQL」。核实后更正：

| 上一轮说法 | 核实结果 |
|---|---|
| 需把 FastAPI 重写成 Node 云函数 | **错误**。CloudBase 官方《运行环境支持》把 HTTP 云函数的 Python 3.11/3.10/3.9 与 **FastAPI** 列为推荐框架；官方另有 `TencentCloudBase/cloudrun-fastapi` 模板仓库。FastAPI 业务代码可原样保留 |
| 放弃 Docker 与 MySQL | 部分正确。Docker 确实不再需要（改为 `scf_bootstrap` 启动脚本）；MySQL 不是「放弃」而是**平台已停止新开通**（2026-10-20 起），需换成 PostgreSQL 或文档型数据库 |

此更正同步写入项目长期笔记。平台规则与计费时效性强，**一律以微信／腾讯云官方最新文档与购买页为准**。

---

## 2. CloudBase 方案架构

```
参与者：微信扫码小程序码 → 小程序原生页面（阶段分支 / 动态表单 / 一键签到）
   └─ 小程序端服务层 services/*.js（统一接口，内部适配 A′ 或 B″）
        ├─ [路径 1] wx.cloud.callFunction → 事件型云函数（免域名，推荐）
        └─ [路径 2] wx.request → HTTP 网关默认域名（受默认域名限制，需实测）
             └─ FastAPI 业务层（HTTP 云函数，Python 3.11 + uvicorn :9000）
                  ├─ /api/* 业务 API（阶段判定 / 报名 / 签到）
                  ├─ /api/admin/* 管理 API（token 鉴权）
                  └─ CloudBase 数据库：PostgreSQL（关系型）或文档型
小程序码：云调用 cloud.openapi.wxacode.getUnlimited → 云存储 fileID
管理后台：见 3.5（建议改为小程序管理分包）
```

### 2.1 与 A′ 共用的代码基座（本轮最有价值的结论）

| 共用 | 内容 |
|---|---|
| 小程序端 | 全部页面、组件、`dynamic-form` 渲染器、scene 解析、阶段分支、隐私弹窗 |
| 服务层接口 | `services/activity.js` 暴露 `status/register/checkin`，内部按环境切换适配器 |
| 后端业务 | FastAPI 路由、Pydantic 模型、阶段判定六条规则、幂等、字段配置校验、加密与限流 |
| 数据模型 | 五张表结构与 `(activityId, phoneHash)` 唯一约束；SQLAlchemy 只换 dialect |
| API 契约 | 需求第 8 节 9 个 API 的路径与语义 |
| 设计原则 | 阶段判定只在服务端 / 一码贯穿靠 scene 不变 / 单一数据源 |

**差异只有四处**：部署适配层（`Dockerfile` ↔ `scf_bootstrap`）、数据库驱动（MySQL ↔ PostgreSQL）、请求封装（`callContainer` ↔ `callFunction`）、二维码生成（自管 `access_token` ↔ 云调用）。因此决策可以推迟到部署阶段。

---

## 3. 技术实施方案

### 3.1 后端：FastAPI → HTTP 云函数

官方部署方式（以官方文档核实为准）：

```bash
# scf_bootstrap（无扩展名，需 chmod +x）
#!/bin/bash
export PORT=9000
export TZ=Asia/Shanghai
export PYTHONPATH="./third_party:$PYTHONPATH"
/var/lang/python311/bin/python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 9000
```

- HTTP 云函数**固定监听 9000 端口**，官方明确要求。
- 平台**不会自动安装 Python 依赖**，须 `pip install -r requirements.txt -t third_party` 后随代码包上传。
- 官方运行环境表：Python 3.11（推荐）/ 3.10 / 3.9；对应绝对路径见《运行环境支持》文档。
- HTTP 云函数特性：按请求计费、空闲零费用、毫秒级冷启动、**适合间歇性访问** —— 与活动期集中使用的场景天然契合。
- 时区：与 A′ 一样必须显式设 `TZ=Asia/Shanghai`，否则报名/签到时间窗可能偏移 8 小时（容器时区需实测确认）。

### 3.2 数据层：MySQL 已不能新开通

腾讯云《云开发 CloudBase 购买指南》明确：**自 2026-10-20 起，MySQL 数据库不再支持新开通**（已开通实例不受影响）；创建环境时只能选「云数据库（文档型）」或「PostgreSQL」。

| 选项 | 改动量 | 评价 |
|---|---|---|
| **PostgreSQL** | SQLAlchemy 换 dialect + 驱动，建表语句微调 | **推荐**。关系模型、唯一约束、事务、2 合 1 双写全部保留，改动最小 |
| 文档型数据库 | 重写数据层，幂等靠应用层 + 唯一键，2 合 1 事务受限 | 不推荐，参考库也提示其事务与复杂查询能力有限 |
| 连接自有 MySQL | 标准版起支持，但需自备数据库 | 违背零运维，排除 |

CloudBase 的 PostgreSQL 是否具备 Serverless 自动暂停、暂停时长与计费口径，**需以官方当前文档核实**（文档型/MySQL 的自动暂停规则不可直接套用）。活动前预热仍是必要动作。

### 3.3 小程序码：云调用免 access_token

微信云开发侧已确认可用（官方社区与腾讯云教程一致）：

```json
// 云函数 config.json —— 必须声明 openapi 权限
{
  "permissions": {
    "openapi": ["wxacode.getUnlimited"]
  }
}
```

```javascript
const cloud = require('wx-server-sdk');
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV });
const res = await cloud.openapi.wxacode.getUnlimited({
  scene: 'a=1001',
  page: 'pages/activity/index',
  width: 430
});
// res.buffer 为 JPEG 图片，上传云存储后保存 fileID；scene 不变则永不重生成
```

- 比 A′ 自己缓存 `access_token` 更简洁，且省去一次网络请求。
- 约束同前：scene 最大 32 个可见字符、字符集受限、`page` 为已发布页面、扫码后 `decodeURIComponent(options.scene)`。
- **待核实**：腾讯云侧 CloudBase 环境的免鉴权云调用是否同样覆盖 `wxacode.getUnlimited`；若不可用，回退到 AppID/AppSecret 换 `access_token` 的 HTTPS 调用（A′ 的做法）。

### 3.4 小程序端请求路径（CloudBase 特有的适配难点）

| 路径 | 做法 | 优点 | 风险 |
|---|---|---|---|
| **路径 1（推荐）** | `wx.cloud.callFunction` 调**事件型云函数**，函数内用 ASGI 内存调用（`httpx.ASGITransport` / `TestClient`）把 `event` 转成对 FastAPI app 的内部请求 | 走微信私有链路，**免域名、免备案、无默认域名风控**；与 A′ 的 `callContainer` 体验一致 | 需自写约数十行适配层；路由从 REST path 变为 `action` 分发；需实测验证微信云开发 Python 云函数的支持情况 |
| 路径 2 | `wx.request` 调 HTTP 网关默认域名 | FastAPI 原样、零适配 | 默认域名官方限定「仅开发测试」，非浏览器请求会被加 `Content-Disposition: attachment` 响应头，且生产环境可能被风控关停 —— **需实测**，不宜作为正式方案 |

统一做法：`services/activity.js` 保持 `status/register/checkin` 三个方法不变，内部按环境选择 `callContainer` / `callFunction` 适配器，业务页面零改动。

### 3.5 管理后台：三种形态，必须重新拍板

CloudBase 官方《HTTP 访问服务 / 默认域名访问限制》明确：默认域名（`*.tcloudbaseapp.com` 等）仅供开发测试，浏览器 `navigate` 请求会展示「访问提示中间页」；**生产环境若用浏览器直接访问静态网站托管 / 云托管 / HTTP 网关，必须使用已完成 ICP 备案的自定义域名**。

| 形态 | 说明 | 代价 |
|---|---|---|
| **A. 小程序内管理分包（推荐）** | 管理员口令登录后，在分包里做活动 CRUD、字段配置、看板、明细、二维码预览、导出 | 与用户第 3 项「只保留 Web 后台、不要小程序端看板」的确认冲突，需重新决策；导出改为云函数生成 xlsx/csv 存云存储 + `wx.openDocument` |
| B. 管理员本地运行 | 本地 FastAPI + 后台 SPA，经云开发 HTTP API / 管理云函数操作数据 | 需保管访问凭据、开放外网访问，配置成本较高 |
| C. 沿用 Web SPA + 默认域名 | 接受中间页（点一次「确定访问」，Cookie 有效期内不再出现） | 官方写明「严禁用于正式生产环境」，有风控关停风险；且正式生产要求备案域名 —— **不推荐** |

这是 B″ 相对 A′ 最实质的形态代价：A′ 的后台可随云托管服务走其公网域名按需访问，而 CloudBase 若坚持 Web 后台，会把已消除的备案域名请回来。

### 3.6 工程目录（B″ 增量）

```
miniprogram/                 小程序端（与 A′ 共用）
  services/activity.js       统一服务层 + 环境适配器
  packageAdmin/              管理分包（若选 3.5 形态 A）
cloudfunctions/
  api/                       事件型云函数（路径 1）或 HTTP 云函数（路径 2）
    app/main.py              FastAPI 业务（与 A′ 共用）
    app/db.py                SQLAlchemy → PostgreSQL
    third_party/             随包上传的 Python 依赖
    scf_bootstrap            启动脚本（HTTP 云函数形态）
    config.json              openapi 权限声明
  qrcode/                    小程序码生成 + 云存储上传
```

### 3.7 安全与合规

- 收集姓名、手机号、单位、部门 → 公众平台配置《用户隐私保护指引》+ 端内授权（`wx.getPrivacySetting` / `wx.requirePrivacyAuthorize` / `openPrivacyContract`，基础库版本与适用范围**以官方文档核实**）。
- 手机号由用户**手动填写**，不调用 `getPhoneNumber`，因此 `app.json` 的 `requiredPrivateInfos` 无需声明该接口；但隐私指引仍须如实声明收集手机号。
- 免登录填表、无支付、无诱导分享、无 UGC；管理员默认口令必须修改；提审备好测试账号、可扫码活动与功能说明。
- 类目「工具 → 预约/报名」对个人主体可选，名称/简介/页面须与类目一致；个人主体不得从事经营，定位为免费非经营工具。**以上以微信官方最新审核规范为准**。
- 云开发数据库安全规则不可设为 `true`；管理接口一律走云函数鉴权，禁止前端直连数据库写敏感集合。

---

## 4. 个人主体可行性（CloudBase 优于云托管的一点）

| 项 | 结论 |
|---|---|
| 能否开通云服务 | 腾讯云文档明确：「非个人类型小程序没有通过微信认证不能开通云服务，**个人小程序暂无微信认证限制**」；个人主体只要不涉及收款即可 |
| 前置 | **腾讯云账号已完成实名认证**（未实名会创建失败且报错不明显）；管理员微信扫码授权 |
| 常见坑 | 在开发者工具创建项目时须勾选「不使用云服务」，否则会自动新建一个腾讯云账号，导致后续在控制台看不到环境 |
| 对比 A′ | A′ 云托管存在「个人未认证号建环境卡住、需 30 元认证」的实测案例；CloudBase 官方口径更宽松，**开通确定性更高** |

---

## 5. 计费与生命周期：决定性的差异

### 5.1 CloudBase 资源点套餐（1000 资源点 ≈ 1 元）

| 套餐 | 价格 | 资源点 | 关键限制 |
|---|---|---|---|
| FREE | 0 元/月 | 3,000 点/月 | 每账号 1 个免费环境；**不支持按量付费**（用完当月停用）；单次续期 6 个月、到期前 1 个月内可续、**不支持自动续费**；存在未列明的功能限制 |
| PERSONAL | 19.9 元/月（限时优惠） | 40,000 点/月 | 支持超限按量、余额告警、99.95% SLA |
| STANDARD | 199 元/月 | 330,000 点/月 | 数据回档、连接自有 MySQL/SQL Server |
| ENTERPRISE | 999 元/月起 | 1,500,000 点/月 | 治理与审计能力 |

### 5.2 对低频活动最致命的一条

腾讯云与微信云开发计费文档均写明：**免费体验环境在小程序发布上线后，到期时间变更为「上线后的第 15 天」**，到期进入停服隔离期，未转付费则销毁环境。也就是说，小程序一上线就开始 15 天倒计时，靠免费环境长期承载「一年几次活动」不可行。

此外：免费环境 6 个月需手动续期、错过窗口没有兜底；资源点用完当月停用；付费版则是**按月常年扣费**。这与「平常不想为此专门租用和管理服务」「不用时零费用」直接冲突。

### 5.3 成本对照

| | A′ 微信云托管 | B″ CloudBase |
|---|---|---|
| 计费模型 | 按实际用量、精确到秒、最小副本 0 | 资源点套餐（月费）或按量（付费套餐超限） |
| 空闲期 | 缩容到 0，**0 元、无需任何操作** | 免费版需盯 15 天到期 / 6 个月续期 / 额度；付费版月月扣 |
| 单次活动 | 官方活动场景示例约 0.95 元/活动日量级 | 免费额度约 3 元/月资源量，千人活动大概率够用但需压测；正式上线需个人版 19.9 元/月 |
| 年成本（几次活动） | 几元量级 | 0 元（但随时可能停服）或约 240 元/年 |
| 管理动作 | 活动前预热、活动后导出归档并缩容 | 上述 + 套餐续期、额度告警、环境到期监控 |

---

## 6. 二选一对比与明确推荐

| 维度 | A′ 微信云托管（现方案） | B″ CloudBase（本轮研究） |
|---|---|---|
| 个人主体开通确定性 | 待实测，可能需 30 元认证 | 官方明确个人无认证限制，更稳 |
| FastAPI | Docker 容器，原样 | HTTP 云函数，几乎原样（scf_bootstrap + 9000 端口） |
| 数据库 | Serverless MySQL，原样 | 新环境已停开 MySQL，改 PostgreSQL（换 dialect） |
| 小程序码 | 自管 access_token | 云调用免 token（腾讯云侧待核实） |
| 小程序端 API | `callContainer` 免域名 | 需自写事件适配器才能同样免域名 |
| 管理后台 | 云托管公网域名按需开启 | 需改为小程序管理分包 / 本地运行，否则回到备案域名 |
| 不用时成本 | 0 元、零操作 | 需续期监控或 19.9 元/月 |
| 免费环境可用期 | 无此限制 | 上线后 15 天到期，需转付费 |
| 平台锁定 | 标准 Docker，易迁走 | SDK / 云函数形态绑定较深 |

**推荐：维持 A′ 为首选。** 理由是它同时命中「零域名、零服务器、不用时零费用、零常年管理」四条；CloudBase 的主要优势只有「个人主体开通更确定」，而这恰好可以用一次 5 分钟的试建环境来验证。

若试建云托管失败，再按「个人认证 30 元 → 转 B″（PostgreSQL + 事件型云函数 + 小程序管理分包 + 活动月开个人版）→ 改个体工商户主体」的顺序后退。

---

## 7. 实施路径：不必现在二选一

1. **现在只做一个动作**：用你的管理员微信扫码进微信云托管控制台，试建首个环境（5 分钟，零成本、不承诺资源）。
2. **无论结果如何，都可以先开工**：按第 2.1 节的共用基座写代码 —— 小程序端页面、FastAPI 业务逻辑、Pydantic/SQLAlchemy 模型、阶段判定、9 个 API 契约、测试用例。这些在两套方案里都不浪费。
3. **部署层留到最后**：成功则写 `Dockerfile` + MySQL 驱动；失败则写 `scf_bootstrap` + PostgreSQL 驱动 + 事件适配器。
4. 若走 B″，需你额外拍板 3.5 的管理后台形态（与第 3 项原确认冲突）。

---

## 8. 待核实清单（以官方为准，不阻塞共用基座开发）

1. 个人主体能否开通微信云托管（5 分钟试建，第一动作）。
2. CloudBase 免费体验环境「存在一些功能使用限制」的具体清单 —— 是否包含 HTTP 云函数、关系型数据库、静态托管。
3. CloudBase PostgreSQL 是否 Serverless 自动暂停、暂停时长与计费口径。
4. 腾讯云侧 CloudBase 环境的免鉴权云调用是否覆盖 `wxacode.getUnlimited`。
5. 事件型 Python 云函数用 ASGI 内存调用承载 FastAPI 的可行性与冷启动开销。
6. 路径 2（HTTP 网关默认域名）在小程序 `wx.request` 下是否被 `Content-Disposition` / 风控影响。
7. 3,000 资源点/月对千人级活动的实际消耗（需压测）。
8. 所有价格、限时优惠、免费体验活动截止时间（微信侧活动写明至 2026-12-31）以官网当前购买页为准。

---

## 9. 主要依据

- CloudBase《运行环境支持》《FastAPI 框架示例》《编写 HTTP 云函数》《HTTP 访问服务概述》《默认域名访问限制及中间页》
- 腾讯云《云开发 CloudBase 产品概述》《云开发 CloudBase 购买指南 / 资源点价格文档》《认证小程序至云开发平台》《MySQL 数据库》
- 微信《计费说明》（云开发免费体验与上线后 15 天规则）、《小程序备案操作指引》
- 微信开放社区《云调用能力—小程序码》与官方 `getUnlimited` 参数文档
- 专家参考库：`cloud-development.md`、`wechat-api-integration.md`、`business-scenario-templates.md`、`project-structure-config.md`、`review-compliance.md`

> 本轮仅交付研究文档，未建代码骨架、未安装依赖、未开通云资源、未创建《小程序项目档案》。
