/* ============================================================
 * mock.js —— 静态演示站的 fetch 拦截层
 *
 * GitHub Pages 只能托管静态文件，跑不了 FastAPI 后端。这一层在所有页面
 * 模块加载**之前**把 window.fetch 换掉，从 fixtures.json 里返回预先抓好的
 * 真实响应，于是 web/*.js 一个字节都不用改就能在纯静态环境里跑起来。
 *
 * fixtures.json 里的数据是从真后端（demo 实例，全是编的账号和流水）逐个
 * 端点抓下来的，不是手写的 —— 手写契约（字段名、嵌套形状）一旦写错，
 * 页面会「元素在但内容空」，那种 bug 极难归因。
 *
 * 关键实现点
 *   * 必须返回真的 Response 对象。shared.js 的 apiFetch 会读 r.ok / r.text()，
 *     chat.js 更进一步用 res.body.getReader() 逐块读 SSE、还读 X-WB-Account
 *     响应头 —— 自己拼个 {ok:true, text:()=>...} 的假对象撑不住。
 *   * 查询参数要容错匹配。前端的 health 档位是 1h/6h/24h/7d 四组
 *     (window_h,buckets)，history 的 gap 有 5 档，fixtures 不可能穷举；
 *     所以先精确匹配，不中再退回「同路径忽略参数」的那条。
 *   * 写操作一律 403 + 明确文案。演示站不该让人以为改动生效了，
 *     报错比静默假成功诚实。
 * ============================================================ */

(function () {
  'use strict';

  var BASE = (function () {
    // Pages 部署在 /<repo>/ 子路径下，也可能在根。取当前目录当基路径。
    var p = location.pathname;
    return p.replace(/[^/]*$/, '');
  })();

  var FIXTURES = null;
  var LOADED = null;          // Promise，保证只加载一次

  function loadFixtures() {
    if (LOADED) return LOADED;
    LOADED = fetch(BASE + 'fixtures.json', { cache: 'no-cache' })
      .then(function (r) { return r.json(); })
      .then(function (j) { FIXTURES = j; return j; })
      .catch(function (e) {
        console.error('[demo] fixtures.json 加载失败', e);
        FIXTURES = {};
        return {};
      });
    return LOADED;
  }

  /* ------------------------------------------------------------ 路径处理 */

  function toPath(input) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    // 绝对 URL 取 pathname+search；相对路径直接用
    if (/^https?:\/\//i.test(url)) {
      try {
        var u = new URL(url);
        return u.pathname + u.search;
      } catch (e) { /* 继续 */ }
    }
    return url;
  }

  function isApi(path) {
    return path.indexOf('/api/') === 0 || path.indexOf('api/') === 0;
  }

  /** 归一成 fixtures 里的键形态：以 /api/ 开头 */
  function normalize(path) {
    var i = path.indexOf('/api/');
    return i >= 0 ? path.slice(i) : path;
  }

  /**
   * 找 fixture。三级回退：
   *   1) 完全相同的 path?query
   *   2) 同 path、忽略 query（health 的档位、history 的 gap 都靠这条兜住）
   *   3) 同 path 前缀（/api/history/status/xxx 这类带路径参数的）
   */
  function pick(path) {
    if (!FIXTURES) return null;
    if (FIXTURES[path]) return FIXTURES[path];

    var bare = path.split('?')[0];
    if (FIXTURES[bare]) return FIXTURES[bare];

    var keys = Object.keys(FIXTURES);
    var i;
    // 同路径不同参数
    for (i = 0; i < keys.length; i++) {
      if (keys[i].split('?')[0] === bare) return FIXTURES[keys[i]];
    }
    // 路径参数：取最长的匹配前缀
    var best = null, bestLen = -1;
    for (i = 0; i < keys.length; i++) {
      var kb = keys[i].split('?')[0];
      if (bare.indexOf(kb) === 0 && kb.length > bestLen) {
        best = FIXTURES[keys[i]];
        bestLen = kb.length;
      }
    }
    return best;
  }

  function jsonResponse(obj, status) {
    return new Response(JSON.stringify(obj), {
      status: status || 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  var READONLY = {
    detail: '这是只读演示站：数据是虚构的，写操作已禁用。'
          + '想实际使用请按 README 自行部署。',
  };

  /* ------------------------------------------------------- 假流式对话 */

  var REPLY = '这是演示站的模拟回复。\n\n'
    + '真实部署时，这里会把请求转发到账号池里的上游模型，并把 SSE 逐 token '
    + '透传回来。演示站没有后端，所以这段文字是本地生成的。\n\n'
    + '- 账号池调度、倍率统计、签到都在服务端跑\n'
    + '- 静态站只保留界面本身\n';

  function sse(payload) {
    var model = (payload && payload.model) || 'unknown';
    var stream = (payload && payload.stream) !== false;

    // claude 系在假上游里故意报错，用来展示错误态渲染
    var fail = /claude/i.test(model);

    if (!stream) {
      if (fail) {
        return jsonResponse({
          error: { message: '模型暂不可用（演示站模拟错误）', code: 11140 },
        }, 200);
      }
      return new Response(JSON.stringify({
        choices: [{ message: { role: 'assistant', content: REPLY },
                    finish_reason: 'stop' }],
        usage: { prompt_tokens: 12, completion_tokens: 96,
                 total_tokens: 108, credit: 0.42 },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json',
                   'X-WB-Account': '138****1111' },
      });
    }

    var enc = new TextEncoder();
    var body = new ReadableStream({
      start: function (ctrl) {
        var chunks = [];
        chunks.push({ choices: [{ delta: { role: 'assistant' } }] });
        if (fail) {
          chunks.push({ error: { message: '模型暂不可用（演示站模拟错误）',
                                 code: 11140 } });
        } else {
          // 按字符切，模拟 token 流
          for (var i = 0; i < REPLY.length; i += 3) {
            chunks.push({ choices: [{ delta: { content: REPLY.slice(i, i + 3) } }] });
          }
          chunks.push({ choices: [{ delta: {}, finish_reason: 'stop' }],
                        usage: { prompt_tokens: 12, completion_tokens: 96,
                                 total_tokens: 108, credit: 0.42 } });
        }

        var n = 0;
        (function step() {
          if (n >= chunks.length) {
            ctrl.enqueue(enc.encode('data: [DONE]\n\n'));
            ctrl.close();
            return;
          }
          ctrl.enqueue(enc.encode('data: ' + JSON.stringify(chunks[n]) + '\n\n'));
          n += 1;
          setTimeout(step, n < 3 ? 260 : 18);   // 首帧慢一点，让 TTFT 看得出来
        })();
      },
    });

    return new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream',
                 'X-WB-Account': '138****1111' },
    });
  }

  /* ------------------------------------------------------------ 拦截 */

  var realFetch = window.fetch.bind(window);

  window.fetch = function (input, init) {
    var path = toPath(input);
    var method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();

    // 非 API 请求（fixtures.json、静态资源、CDN）走真网络
    if (!isApi(path)) return realFetch(input, init);

    var p = normalize(path);

    return loadFixtures().then(function () {
      // 对话调试：伪造流式响应
      if (p.indexOf('/api/chat/completions') === 0) {
        var payload = null;
        try {
          payload = init && init.body ? JSON.parse(init.body) : null;
        } catch (e) { payload = null; }
        return sse(payload);
      }

      // 登出：演示站没有 session，什么都不做
      if (p.indexOf('/api/auth/logout') === 0) {
        return jsonResponse({ ok: true });
      }

      if (method !== 'GET') return jsonResponse(READONLY, 403);

      var fx = pick(p);
      if (!fx) {
        console.warn('[demo] 没有对应 fixture:', p);
        return jsonResponse({ detail: '演示站没有这个端点的数据：' + p }, 404);
      }
      return jsonResponse(fx.json, fx.status || 200);
    });
  };

  // 预热，避免首屏第一个请求还在等 fixtures
  loadFixtures();
})();
