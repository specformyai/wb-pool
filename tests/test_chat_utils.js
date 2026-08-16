const assert = require('assert');
const utils = require('../web/chat-utils.js');

const sample = [
  '# 标题', '', '这是 **加粗**、*斜体*、~~删除~~ 和 `代码`。', '',
  '- 第一项', '- 第二项', '', '> 引用', '', '[安全链接](https://example.com)', '',
  '```js', 'const x = 1 < 2;', '```',
].join('\n');
const html = utils.renderMarkdown(sample);
assert.match(html, /<h1>标题<\/h1>/);
assert.match(html, /<strong>加粗<\/strong>/);
assert.match(html, /<em>斜体<\/em>/);
assert.match(html, /<del>删除<\/del>/);
assert.match(html, /<code>代码<\/code>/);
assert.match(html, /<ul>[\s\S]*<li>第一项<\/li>[\s\S]*<\/ul>/);
assert.match(html, /<blockquote>引用<\/blockquote>/);
assert.match(html, /target="_blank"/);
assert.match(html, /<pre><code class="language-js">[\s\S]*&lt; 2;[\s\S]*<\/code><\/pre>/);
assert.ok(!html.includes('<script>'));
assert.ok(!html.includes('`# 标题'));

const parts = utils.buildMessageContent('请分析这张图', [
  { name: 'test.png', type: 'image/png', dataUrl: 'data:image/png;base64,AAAA' },
]);
assert.deepStrictEqual(parts, [
  { type: 'text', text: '请分析这张图' },
  { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
]);
assert.deepStrictEqual(utils.buildMessageContent('', [
  { name: 'test.jpg', type: 'image/jpeg', dataUrl: 'data:image/jpeg;base64,BBBB' },
]), [
  { type: 'image_url', image_url: { url: 'data:image/jpeg;base64,BBBB' } },
]);

const fs = require('fs');
const path = require('path');
const web = name => fs.readFileSync(path.join(__dirname, '..', 'web', name), 'utf8');
const index = web('index.html');
const app = web('app.js');
const css = web('app.css');
assert.ok(index.includes('id="chatImageInput"'));
assert.ok(index.includes('id="chatAttachments"'));
assert.ok(index.indexOf('/static/chat-utils.js') < index.indexOf('/static/app.js'));
assert.match(app, /addEventListener\('paste'/);
assert.match(app, /addEventListener\('drop'/);
assert.match(app, /WBChatUtils\.renderMarkdown/);
assert.match(app, /WBChatUtils\.buildMessageContent/);
assert.match(css, /\.chat-attachments/);
assert.match(css, /\.msg-md/);
console.log('chat-utils and chat UI: all assertions passed');
