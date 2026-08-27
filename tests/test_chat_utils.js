#!/usr/bin/env node
/**
 * chat.js 的安全 Markdown 渲染 + 多模态内容拼装 —— 纯函数断言。
 *
 *     node tests/test_chat_utils.js
 *
 * 为什么这个文件被重写过
 * --------------------
 * 原版 require('../web/chat-utils.js')，而那个文件在前端模块化时就被删了
 * （函数内联进 chat.js），断言的 web/app.js、WBChatUtils.*、#chatImageInput
 * 也全都不存在了。测试于是一直是坏的（Cannot find module），CI 里等于没覆盖。
 * renderMarkdown 的输出走 innerHTML（chat.js 里 sh.body.innerHTML = ...），
 * 是实打实的 XSS 面，这个覆盖不能丢，所以按现状重写。
 *
 * 为什么要复制到临时目录再 import
 * ----------------------------
 * 前端用 importmap 把 '@/shared.js' 映射成 '/static/shared.js?v=<hash>'。
 * Node 不认 importmap，直接 import 会 ERR_MODULE_NOT_FOUND。复制一份把 '@/'
 * 改写成 './' 最省事，且不依赖 Node 版本（registerHooks 旧版本没有）。
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const WEB = path.join(__dirname, '..', 'web');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'wbchat-'));

for (const f of fs.readdirSync(WEB).filter((n) => n.endsWith('.js'))) {
  const src = fs.readFileSync(path.join(WEB, f), 'utf8')
    // '@/shared.js' -> './shared.js'，顺带吃掉可能存在的 ?v=hash
    .replace(/(['"])@\/([^'"?]+?)(\?v=[0-9a-f]+)?\1/g, '$1./$2$1');
  fs.writeFileSync(path.join(TMP, f), src);
}

// shared.js 顶层的 ASSET 是个 IIFE，会去读 <link rel="icon">。Node 里没有
// document，不 stub 的话 import 立刻抛。querySelector 返回 null 就够了 ——
// ASSET 内部用了 ?. 和 || './' 兜底。
globalThis.document = {
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => ({
    setAttribute() {}, appendChild() {}, addEventListener() {},
    style: {}, classList: { add() {}, remove() {}, toggle() {} },
  }),
  body: { appendChild() {} },
  addEventListener() {},
};

let failed = 0;
function ck(name, cond, extra) {
  if (cond) {
    console.log('  OK  ' + name);
  } else {
    failed += 1;
    console.log(' FAIL ' + name + (extra === undefined ? '' : '  <- ' + String(extra).slice(0, 200)));
  }
}

(async () => {
  const { renderMarkdown, buildMessageContent } =
    await import(pathToFileURL(path.join(TMP, 'chat.js')).href);

  ck('chat.js 导出 renderMarkdown', typeof renderMarkdown === 'function');
  ck('chat.js 导出 buildMessageContent', typeof buildMessageContent === 'function');
  if (typeof renderMarkdown !== 'function' || typeof buildMessageContent !== 'function') {
    console.log('\n导出缺失，后续断言无意义');
    process.exit(1);
  }

  // ---------------------------------------------------------------- Markdown
  console.log('\n── 1. Markdown 基本语法 ──');
  const sample = [
    '# 标题', '',
    '这是 **加粗**、*斜体*、~~删除~~ 和 `代码`。', '',
    '- 第一项', '- 第二项', '',
    '1. 有序一', '2. 有序二', '',
    '> 引用', '',
    '[安全链接](https://example.com)', '',
    '| a | b |', '| --- | --- |', '| 1 | 2 |', '',
    '```js', 'const x = 1 < 2;', '```',
  ].join('\n');
  const html = renderMarkdown(sample);

  ck('h1', /<h1>标题<\/h1>/.test(html), html.slice(0, 120));
  ck('strong', /<strong>加粗<\/strong>/.test(html));
  ck('em', /<em>斜体<\/em>/.test(html));
  ck('del', /<del>删除<\/del>/.test(html));
  ck('inline code', /<code>代码<\/code>/.test(html));
  ck('ul', /<ul><li>第一项<\/li><li>第二项<\/li><\/ul>/.test(html));
  ck('ol', /<ol><li>有序一<\/li><li>有序二<\/li><\/ol>/.test(html));
  ck('blockquote', /<blockquote>引用<\/blockquote>/.test(html));
  ck('链接带 target=_blank', /target="_blank"/.test(html));
  ck('链接带 rel=noopener', /rel="noopener noreferrer"/.test(html));
  ck('表格渲染成 chat-md-tbl', /<table class="chat-md-tbl">/.test(html));
  ck('代码块带语言标签', /chat-code-h[\s\S]*>js</.test(html));
  ck('代码块内容被转义（1 &lt; 2）', /&lt; 2;/.test(html), html);
  ck('围栏标记不残留', !html.includes('```'));

  // ---------------------------------------------------------------- XSS
  // renderMarkdown 的结果直接进 innerHTML，这几条是安全底线。
  console.log('\n── 2. XSS（输出走 innerHTML，这几条是底线）──');
  const evil = renderMarkdown('<script>alert(1)</script>');
  ck('script 标签被转义', !evil.includes('<script>') && evil.includes('&lt;script&gt;'), evil);

  const img = renderMarkdown('<img src=x onerror=alert(1)>');
  ck('img 标签被转义', !img.includes('<img'), img);

  const inCode = renderMarkdown('```\n<script>alert(1)</script>\n```');
  ck('代码块里的 script 也被转义', !inCode.includes('<script>'), inCode);

  const js = renderMarkdown('[点我](javascript:alert(1))');
  ck('javascript: 链接不生成 href', !/href="javascript:/i.test(js), js);

  const dataUri = renderMarkdown('[点我](data:text/html;base64,PHNjcmlwdD4=)');
  ck('data: 链接不生成 href', !/href="data:/i.test(dataUri), dataUri);

  const attr = renderMarkdown('**a" onmouseover="alert(1)**');
  ck('引号被转义，无法逃出属性', !attr.includes('onmouseover="alert'), attr);

  ck('null/undefined 不炸', renderMarkdown(null) === '' && renderMarkdown(undefined) === '');

  // ---------------------------------------------- buildMessageContent
  console.log('\n── 3. 多模态内容拼装 ──');
  const noImg = buildMessageContent('只有文字', []);
  ck('无图时返回字符串（不是数组）', noImg === '只有文字', JSON.stringify(noImg));
  ck('images 为 undefined 时也返回字符串', buildMessageContent('x', undefined) === 'x');

  const withImg = buildMessageContent('请分析这张图', [
    { name: 'test.png', dataUrl: 'data:image/png;base64,AAAA' },
  ]);
  ck('文本+图 -> [text, image_url]', JSON.stringify(withImg) === JSON.stringify([
    { type: 'text', text: '请分析这张图' },
    { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
  ]), JSON.stringify(withImg));

  const onlyImg = buildMessageContent('', [
    { name: 'a.jpg', dataUrl: 'data:image/jpeg;base64,BBBB' },
  ]);
  ck('只有图 -> 不塞空 text 段', JSON.stringify(onlyImg) === JSON.stringify([
    { type: 'image_url', image_url: { url: 'data:image/jpeg;base64,BBBB' } },
  ]), JSON.stringify(onlyImg));

  const multi = buildMessageContent('两张', [
    { dataUrl: 'data:image/png;base64,A' }, { dataUrl: 'data:image/png;base64,B' },
  ]);
  ck('多图按顺序全部带上', multi.length === 3 && multi[1].image_url.url.endsWith('A')
    && multi[2].image_url.url.endsWith('B'), JSON.stringify(multi));

  // ---------------------------------------------------------------- UI 契约
  // 原版断言的 #chatImageInput / web/app.js 都已不存在，改成断言现状的真实标识。
  console.log('\n── 4. UI 契约（对着现在的文件，不是删掉的 app.js）──');
  const chatJs = fs.readFileSync(path.join(WEB, 'chat.js'), 'utf8');
  const chatCss = fs.readFileSync(path.join(WEB, 'chat.css'), 'utf8');

  ck('有图片 file input 且只收图片类型',
    /accept:\s*'image\/png,image\/jpeg,image\/webp,image\/gif'/.test(chatJs));
  ck('支持粘贴上传', /addEventListener\('paste'/.test(chatJs));
  ck('支持拖拽上传', /addEventListener\('drop'/.test(chatJs));
  ck('send() 用 buildMessageContent 拼装',
    /buildMessageContent\(text, state\.images\)/.test(chatJs));
  ck('渲染走 renderMarkdown', /renderMarkdown\(item\.content\)/.test(chatJs));
  ck('.chat-thumbs 有样式', /\.chat-thumbs\s*\{/.test(chatCss));
  ck('.chat-code 有样式', /\.chat-code\s*\{/.test(chatCss));
  ck('.chat-md-tbl 有样式', /\.chat-md-tbl\s*\{/.test(chatCss));

  fs.rmSync(TMP, { recursive: true, force: true });
  console.log(failed ? `\n${failed} 项失败` : '\nchat.js 纯函数与 UI 契约：全部通过');
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.error('测试自身异常:', e);
  process.exit(1);
});
