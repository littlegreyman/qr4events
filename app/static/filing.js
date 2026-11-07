/* 备案信息悬挂 · 平台中立通用码
 *
 * 方案 C 合规要求：网站通过 ICP 备案后，须在页面底部悬挂备案号并链接工信部；
 * 通过后 30 日内还须完成公安联网备案并悬挂公安备案号。
 *
 * 用法：取得备案号后，把下方 DEFAULT.icp / DEFAULT.police 填好即可全站生效；
 * 也可在页面中先于本脚本定义 window.QR4EVENTS_FILING = { icp, police } 覆盖，
 * 便于同一份静态资源适配不同部署环境。
 *
 * 重要：备案号为空时不渲染任何内容 —— 备案审核期间网站本就必须关闭、域名
 * 不得解析，此时页面不应出现伪造或占位的备案号。
 */
(function () {
  'use strict';

  var DEFAULT = { icp: '', police: '' };
  var override = window.QR4EVENTS_FILING;
  var cfg = (override && typeof override === 'object') ? {
    icp: override.icp || '',
    police: override.police || ''
  } : DEFAULT;

  if (!cfg.icp && !cfg.police) return;
  var host = document.getElementById('filing-line');
  if (!host) return;

  function text(value) { return document.createTextNode(value); }
  function link(href, label) {
    var a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.appendChild(text(label));
    return a;
  }

  if (cfg.icp) {
    host.appendChild(link('https://beian.miit.gov.cn/', cfg.icp));
  }
  if (cfg.icp && cfg.police) {
    host.appendChild(text(' · '));
  }
  if (cfg.police) {
    // 公安联网备案号按官方要求链接到全国互联网安全管理服务平台。
    // 这是用户主动点击的合规外链，不是页面自动加载的子资源，不构成混合内容，
    // 也不影响「扫码零跳转直达」约束。
    host.appendChild(link('http://www.beian.gov.cn/', cfg.police));
  }
})();
