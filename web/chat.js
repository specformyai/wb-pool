import {
  el, apiFetch, fmtMoney, fmtInt, fmtDur, toast,
  escapeHtml, confirmDialog, refreshIcons, copyText,
} from '@/shared.js';
import { selectify } from '@/selectify.js';

/* ================= 常量与极小工具（非 shared 同名） ================= */
const DONE = Symbol('done');
const MAX_IMG = 4;
const MAX_IMG_SIZE = 10 * 1024 * 1024;
const IMG_TYPE_RE = /^image\/(png|jpeg|webp|gif)$/;
const clampNum = (n, a, b) => Math.min(b, Math.max(a, n));
const trimNum = (n) => String(Math.round(Number(n) * 1000) / 1000);

/* ================= 安全 Markdown 渲染器（先转义，再解析） ================= */
function mdInline(t) {
  const codes = [];
  t = t.replace(/`([^`\n]+)`/g, (m, c) => { codes.push(c); return '\u0001' + (codes.length - 1) + '\u0001'; });
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
  t = t.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  t = t.replace(/(^|[^_\w])_([^_\n]+)_/g, '$1<em>$2</em>');
  t = t.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  t = t.replace(/\u0001(\d+)\u0001/g, (m, i) => '<code>' + codes[+i] + '</code>');
  return t;
}

function mdCodeBlock(b) {
  return '<div class="chat-code"><div class="chat-code-h"><span>' +
    escapeHtml(b.lang || 'code') +
    '</span><button type="button" class="chat-code-copy">复制</button></div><pre><code>' +
    escapeHtml(b.code) + '</code></pre></div>';
}

function mdTable(rows) {
  const parse = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
  const head = parse(rows[0]);
  let h = '<div class="chat-tblwrap"><table class="chat-md-tbl"><thead><tr>' +
    head.map((c) => '<th>' + mdInline(c) + '</th>').join('') + '</tr></thead><tbody>';
  for (const r of rows.slice(2)) {
    h += '<tr>' + parse(r).map((c) => '<td>' + mdInline(c) + '</td>').join('') + '</tr>';
  }
  return h + '</tbody></table></div>';
}

function renderMarkdown(src) {
  const blocks = [];
  let s = String(src == null ? '' : src);
  // 先抽离围栏代码块（未转义的原文），占位符后续替换
  s = s.replace(/```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g, (m, lang, code) => {
    blocks.push({ lang: String(lang || '').trim(), code: String(code).replace(/\n+$/, '') });
    return '\n\u0000' + (blocks.length - 1) + '\u0000\n';
  });
  s = escapeHtml(s); // 关键：绝不注入上游原始 HTML

  const lines = s.split('\n');
  const out = [];
  let para = [];
  let i = 0;
  const flushPara = () => {
    if (para.length) { out.push('<p>' + para.map(mdInline).join('<br>') + '</p>'); para = []; }
  };

  while (i < lines.length) {
    const line = lines[i];
    const trim = line.trim();
    const ph = /^\u0000(\d+)\u0000$/.exec(trim);
    if (ph) { flushPara(); out.push(mdCodeBlock(blocks[+ph[1]])); i++; continue; }
    if (/^\s*$/.test(line)) { flushPara(); i++; continue; }
    let m = /^(#{1,6})\s+(.*)$/.exec(trim);
    if (m) { flushPara(); const lv = m[1].length; out.push('<h' + lv + '>' + mdInline(m[2]) + '</h' + lv + '>'); i++; continue; }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trim)) { flushPara(); out.push('<hr>'); i++; continue; }
    if (/^\|/.test(trim) && i + 1 < lines.length && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim()) && lines[i + 1].includes('-')) {
      flushPara();
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i].trim())) { rows.push(lines[i]); i++; }
      if (rows.length >= 2) out.push(mdTable(rows));
      continue;
    }
    if (/^(&gt;|>)\s?/.test(trim)) {
      flushPara();
      const q = [];
      while (i < lines.length && /^(&gt;|>)\s?/.test(lines[i].trim())) {
        q.push(lines[i].trim().replace(/^(&gt;|>)\s?/, '')); i++;
      }
      out.push('<blockquote>' + q.map(mdInline).join('<br>') + '</blockquote>');
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*]\s+/, '')); i++; }
      out.push('<ul>' + items.map((t) => '<li>' + mdInline(t) + '</li>').join('') + '</ul>');
      continue;
    }
    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+[.)]\s+/, '')); i++; }
      out.push('<ol>' + items.map((t) => '<li>' + mdInline(t) + '</li>').join('') + '</ol>');
      continue;
    }
    para.push(line); i++;
  }
  flushPara();
  return out.join('');
}

/* ================= 页面挂载 ================= */
export function mountChat(root) {
  root.classList.add('page', 'chat-page');

  const LS = 'chat.param.';
  const readLS = (k, dft) => { const v = localStorage.getItem(LS + k); return v === null ? dft : v; };
  const cfg = {
    model: readLS('model', ''),
    account: readLS('account', ''),
    system: readLS('system', ''),
    temp: clampNum(parseFloat(readLS('temp', '0.7')) || 0, 0, 2),
    maxTokens: Math.max(1, parseInt(readLS('maxTokens', '2048'), 10) || 2048),
    timeout: clampNum(parseInt(readLS('timeout', '120'), 10) || 120, 10, 180),
    stream: readLS('stream', '1') === '1',
  };
  const saveLS = (k, v) => { try { localStorage.setItem(LS + k, String(v)); } catch {} };

  const state = {
    details: [], accounts: [], images: [], items: [],
    sending: false, abort: null,
  };
  let renderTimer = null;

  /* ---------- 头部 ---------- */
  const panelBtn = el('button', { class: 'btn ghost sm chat-panel-btn', html: '<i data-lucide="sliders-horizontal"></i> 参数' });
  const clearBtn = el('button', { class: 'btn ghost sm', html: '<i data-lucide="trash-2"></i> 清空对话' });
  const head = el('div', { class: 'page-h' });
  head.innerHTML = '<div><h1>对话调试</h1><p>直连上游模型，验证「账号 × 模型」链路是否可用</p></div>';
  const headActs = el('div', { class: 'acts' });
  headActs.append(panelBtn, clearBtn);
  head.append(headActs);

  /* ---------- 消息区 ---------- */
  const msgs = el('div', { class: 'chat-msgs' });
  const empty = el('div', {
    class: 'chat-empty',
    html: '<i data-lucide="message-square"></i><div class="chat-empty-t">选择模型与账号，发一条消息验证链路</div><div class="chat-empty-s">流式逐字输出 · 思考过程可折叠 · 支持图片粘贴 / 拖拽上传</div>',
  });
  msgs.append(empty);
  const hideEmpty = () => { if (empty.parentNode) empty.remove(); };
  const showEmptyIfNeeded = () => { if (!state.items.length && !empty.parentNode) msgs.append(empty); };
  const nearBottom = () => msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 140;
  const scrollBottom = (force) => { if (force || nearBottom()) msgs.scrollTop = msgs.scrollHeight; };

  /* ---------- 发送区 ---------- */
  const ta = el('textarea', { class: 'chat-ta', rows: '1', placeholder: '输入消息，Enter 发送，Shift+Enter 换行；可直接粘贴或拖拽图片' });
  const thumbs = el('div', { class: 'chat-thumbs' });
  const attachBtn = el('button', { class: 'btn ghost sm', html: '<i data-lucide="paperclip"></i> 图片', title: '上传图片（PNG/JPEG/WebP/GIF，最多 4 张）' });
  const fileInp = el('input', { type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif', multiple: 'multiple' });
  fileInp.style.display = 'none';
  const sendBtn = el('button', { class: 'btn primary sm', html: '<i data-lucide="send"></i> 发送' });
  const stopBtn = el('button', { class: 'btn bad sm', html: '<i data-lucide="square"></i> 停止' });
  stopBtn.style.display = 'none';
  const bar = el('div', { class: 'chat-bar' });
  const grow = el('span', { class: 'chat-grow' });
  bar.append(attachBtn, el('span', { class: 'chat-hint', text: 'Enter 发送 · Shift+Enter 换行 · 最多 4 张图片' }), grow, sendBtn, stopBtn);
  const composer = el('div', { class: 'chat-composer' });
  composer.append(thumbs, ta, bar, fileInp);

  const autoGrow = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 220) + 'px'; };

  /* ---------- 参数面板 ---------- */
  const side = el('aside', { class: 'chat-side' });
  side.innerHTML =
    '<div class="card chat-card">' +
      '<div class="card-h"><div><h3>链路参数</h3><p>请求路由与生成配置</p></div></div>' +
      '<label class="field"><span>模型</span><select class="inp chat-sel-model"></select></label>' +
      '<div class="chat-note chat-model-note"></div>' +
      '<label class="field"><span>指定账号</span><select class="inp chat-sel-account"></select></label>' +
      '<div class="chat-note">指定后 max_tries=1，失败不会自动换号重试</div>' +
      '<label class="field"><span>System Prompt</span><textarea class="inp chat-in-system" rows="3" placeholder="留空则不携带 system 消息"></textarea></label>' +
      '<label class="field"><span>Temperature</span><span class="chat-range"><input type="range" min="0" max="2" step="0.1" class="chat-in-temp"><output class="chat-temp-v"></output></span></label>' +
      '<label class="field"><span>Max Tokens</span><input class="inp chat-in-max" type="number" min="1" max="262144" step="1"></label>' +
      '<label class="field"><span>上游无数据超时（秒，10~180）</span><input class="inp chat-in-timeout" type="number" min="10" max="180" step="1"></label>' +
      '<label class="chat-switch"><span>流式输出（SSE）</span><input type="checkbox" class="chat-in-stream"></label>' +
    '</div>';
  // 换成自绘下拉：原生 select 的展开层由系统绘制，深色面板里点开是白底黑字。
  // selectify 保留了 .value / change / innerHTML / append(option) 的用法，
  // 所以下面填充模型和账号清单的代码不用改。
  const modelSel = selectify(side.querySelector('.chat-sel-model'), { width: '100%' });
  const accSel = selectify(side.querySelector('.chat-sel-account'), { width: '100%' });
  const modelNote = side.querySelector('.chat-model-note');
  const sysTa = side.querySelector('.chat-in-system');
  const tempRg = side.querySelector('.chat-in-temp');
  const tempV = side.querySelector('.chat-temp-v');
  const maxIn = side.querySelector('.chat-in-max');
  const toIn = side.querySelector('.chat-in-timeout');
  const streamCk = side.querySelector('.chat-in-stream');

  sysTa.value = cfg.system;
  tempRg.value = String(cfg.temp);
  tempV.textContent = cfg.temp.toFixed(1);
  maxIn.value = String(cfg.maxTokens);
  toIn.value = String(cfg.timeout);
  streamCk.checked = cfg.stream;

  panelBtn.onclick = () => side.classList.toggle('open');
  sysTa.addEventListener('input', () => { cfg.system = sysTa.value; saveLS('system', cfg.system); });
  tempRg.addEventListener('input', () => { cfg.temp = clampNum(parseFloat(tempRg.value) || 0, 0, 2); tempV.textContent = cfg.temp.toFixed(1); saveLS('temp', cfg.temp); });
  maxIn.addEventListener('change', () => { cfg.maxTokens = Math.max(1, parseInt(maxIn.value, 10) || 2048); maxIn.value = String(cfg.maxTokens); saveLS('maxTokens', cfg.maxTokens); });
  toIn.addEventListener('change', () => { cfg.timeout = clampNum(parseInt(toIn.value, 10) || 120, 10, 180); toIn.value = String(cfg.timeout); saveLS('timeout', cfg.timeout); });
  streamCk.addEventListener('change', () => { cfg.stream = streamCk.checked; saveLS('stream', cfg.stream ? '1' : '0'); });
  accSel.addEventListener('change', () => { cfg.account = accSel.value; saveLS('account', cfg.account); });

  const modelDetail = () => state.details.find((d) => d.id === cfg.model) || null;
  const imagesAllowed = () => { const d = modelDetail(); return !d || d.supports_images !== false; };

  function updateModelUI() {
    const d = modelDetail();
    if (d) {
      const bits = [];
      if (d.owned_by) bits.push(d.owned_by);
      if (d.context_length) bits.push('上下文 ' + fmtInt(d.context_length));
      bits.push(d.supports_images ? '支持图片' : '不支持图片');
      bits.push(d.supports_tool_call ? '支持工具调用' : '不支持工具调用');
      modelNote.textContent = bits.join(' · ');
    } else {
      modelNote.textContent = '';
    }
    const ok = imagesAllowed();
    attachBtn.disabled = !ok;
    attachBtn.title = ok ? '上传图片（PNG/JPEG/WebP/GIF，最多 4 张）' : '当前模型不支持图片输入';
    if (!ok && state.images.length) {
      state.images = []; renderThumbs();
      toast('当前模型不支持图片，已移除待发送图片', 'bad');
    }
  }

  modelSel.addEventListener('change', () => { cfg.model = modelSel.value; saveLS('model', cfg.model); updateModelUI(); });

  async function loadModels() {
    modelSel.innerHTML = '<option value="">加载中…</option>';
    try {
      const d = await apiFetch('/api/models');
      state.details = Array.isArray(d.details) ? d.details : [];
      const ids = Array.isArray(d.models) && d.models.length ? d.models : state.details.map((x) => x.id);
      modelSel.innerHTML = '';
      if (!ids.length) modelSel.append(el('option', { value: '', text: '（无可用模型）' }));
      for (const id of ids) {
        const det = state.details.find((x) => x.id === id) || {};
        const tags = [det.supports_images ? '图片' : '纯文本'];
        if (det.supports_tool_call) tags.push('工具');
        modelSel.append(el('option', { value: id, text: id + '（' + tags.join('·') + '）' }));
      }
      if (cfg.model && ids.includes(cfg.model)) modelSel.value = cfg.model;
      cfg.model = modelSel.value;
      updateModelUI();
    } catch (e) {
      modelSel.innerHTML = '<option value="">模型清单加载失败</option>';
      toast('模型清单加载失败：' + e.message, 'bad');
    }
  }

  async function loadAccounts() {
    accSel.innerHTML = '<option value="">自动轮询</option>';
    try {
      const d = await apiFetch('/api/pool');
      state.accounts = Array.isArray(d.accounts) ? d.accounts : [];
      for (const a of state.accounts) {
        const label = a.label ? a.phone + ' · ' + a.label : String(a.phone);
        if (a.usable) {
          accSel.append(el('option', { value: a.phone, text: label + ' · 余额 ' + fmtMoney(a.credits_total || 0) }));
        } else {
          const o = el('option', { value: a.phone, text: label + ' · 不可用（' + (a.status || 'unknown') + '）' });
          o.disabled = true;
          accSel.append(o);
        }
      }
      if (cfg.account) accSel.value = cfg.account;
      if (accSel.value !== cfg.account) cfg.account = accSel.value; // 之前选的账号可能已不可用（option disabled）
    } catch (e) {
      toast('账号清单加载失败：' + e.message, 'bad');
    }
  }

  /* ---------- 图片 ---------- */
  function renderThumbs() {
    thumbs.innerHTML = '';
    state.images.forEach((im, idx) => {
      const t = el('div', { class: 'chat-thumb' });
      t.append(el('img', { src: im.dataUrl, alt: im.name || '' }));
      const x = el('button', { class: 'chat-thumb-x', text: '×', title: '移除' });
      x.onclick = () => { state.images.splice(idx, 1); renderThumbs(); };
      t.append(x);
      thumbs.append(t);
    });
  }

  function addFiles(files) {
    if (!imagesAllowed()) { toast('当前模型不支持图片输入', 'bad'); return; }
    for (const f of files) {
      if (state.images.length >= MAX_IMG) { toast('最多 ' + MAX_IMG + ' 张图片', 'bad'); break; }
      if (!IMG_TYPE_RE.test(f.type)) { toast('仅支持 PNG / JPEG / WebP / GIF', 'bad'); continue; }
      if (f.size > MAX_IMG_SIZE) { toast('「' + (f.name || '图片') + '」超过 10MB，已跳过', 'bad'); continue; }
      const rd = new FileReader();
      rd.onload = () => { state.images.push({ dataUrl: rd.result, name: f.name }); renderThumbs(); };
      rd.readAsDataURL(f);
    }
  }

  attachBtn.onclick = () => { if (!attachBtn.disabled) fileInp.click(); };
  fileInp.addEventListener('change', () => { addFiles([...fileInp.files]); fileInp.value = ''; });
  ta.addEventListener('paste', (e) => {
    const fs = [...((e.clipboardData && e.clipboardData.files) || [])];
    if (fs.length) { e.preventDefault(); addFiles(fs); }
  });
  ['dragenter', 'dragover'].forEach((ev) => composer.addEventListener(ev, (e) => { e.preventDefault(); composer.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach((ev) => composer.addEventListener(ev, (e) => { e.preventDefault(); composer.classList.remove('drag'); }));
  composer.addEventListener('drop', (e) => {
    const fs = [...((e.dataTransfer && e.dataTransfer.files) || [])];
    if (fs.length) addFiles(fs);
  });

  /* ---------- 消息 DOM ---------- */
  const textOf = (c) => typeof c === 'string' ? c
    : c.map((p) => p.type === 'text' ? p.text : '[图片]').join('\n');

  function makeActs(item, onResend, resendTitle) {
    const acts = el('div', { class: 'chat-macts' });
    const cp = el('button', { class: 'btn ghost sm', html: '<i data-lucide="copy"></i>', title: '复制整条消息' });
    cp.onclick = () => copyText(textOf(item.content));
    const re = el('button', { class: 'btn ghost sm', html: '<i data-lucide="rotate-ccw"></i>', title: resendTitle });
    re.onclick = () => onResend(item);
    acts.append(cp, re);
    return acts;
  }

  function addUserMessage(content) {
    hideEmpty();
    const item = { role: 'user', content };
    const wrap = el('div', { class: 'chat-msg user' });
    if (typeof content === 'string') {
      wrap.append(el('div', { class: 'chat-bubble', text: content }));
    } else {
      const imgs = content.filter((p) => p.type === 'image_url');
      const txt = (content.find((p) => p.type === 'text') || {}).text || '';
      if (imgs.length) {
        const row = el('div', { class: 'chat-uimgs' });
        for (const p of imgs) row.append(el('img', { src: p.image_url.url, alt: '' }));
        wrap.append(row);
      }
      if (txt) wrap.append(el('div', { class: 'chat-bubble', text: txt }));
    }
    wrap.append(makeActs(item, resendUser, '从此消息重新发送'));
    msgs.append(wrap);
    item.el = wrap;
    state.items.push(item);
    refreshIcons(wrap);
    scrollBottom(true);
    return item;
  }

  function addAssistantShell() {
    hideEmpty();
    const item = { role: 'assistant', content: '', reasoning: '' };
    const wrap = el('div', { class: 'chat-msg asst' });
    const headRow = el('div', { class: 'chat-mhead' });
    const modelBadge = el('span', { class: 'badge acc nodot', text: cfg.model || '(未选模型)' });
    const accBadge = el('span', { class: 'badge idle nodot', text: '账号：协商中…' });
    headRow.append(modelBadge, accBadge);

    const think = el('div', { class: 'chat-think' });
    think.style.display = 'none';
    const thinkH = el('div', { class: 'chat-think-h', html: '<i data-lucide="chevron-down"></i><span>思考过程</span>' });
    const thinkB = el('div', { class: 'chat-think-body' });
    thinkH.onclick = () => think.classList.toggle('closed');
    think.append(thinkH, thinkB);

    const body = el('div', { class: 'chat-md live' });
    const tools = el('div', { class: 'chat-tools', text: '模型发起了工具调用（调试页不执行工具）' });
    tools.style.display = 'none';
    const errBox = el('div', { class: 'chat-err' });
    errBox.style.display = 'none';
    const meta = el('div', { class: 'chat-meta' });

    wrap.append(headRow, think, body, tools, errBox, meta, makeActs(item, regenAssistant, '重新生成此回复'));
    msgs.append(wrap);
    item.el = wrap;
    state.items.push(item);
    refreshIcons(wrap);
    scrollBottom(true);
    return { item, wrap, accBadge, think, thinkB, body, tools, errBox, meta };
  }

  // 代码块复制按钮（事件委托，textContent 自动还原转义前的代码）
  msgs.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('.chat-code-copy');
    if (!btn) return;
    const code = btn.closest('.chat-code');
    const pre = code && code.querySelector('pre code');
    if (pre) copyText(pre.textContent || '');
  });

  /* ---------- 重发 / 重新生成 ---------- */
  function truncateFrom(item) {
    const idx = state.items.indexOf(item);
    if (idx < 0) return;
    for (const it of state.items.splice(idx)) { if (it.el) it.el.remove(); }
    showEmptyIfNeeded();
  }
  function resendUser(item) {
    if (state.sending) { toast('正在发送中', 'bad'); return; }
    const content = item.content;
    truncateFrom(item);
    addUserMessage(content);
    runCompletion();
  }
  function regenAssistant(item) {
    if (state.sending) { toast('正在发送中', 'bad'); return; }
    truncateFrom(item);
    runCompletion();
  }

  /* ---------- SSE 解析 ---------- */
  async function readSSE(res, onEvent) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    const handleLine = (raw) => {
      const line = raw.trim();
      if (!line.startsWith('data:')) return;
      const data = line.slice(5).trim();
      if (!data) return;
      if (data === '[DONE]') { onEvent(DONE); return; }
      try { onEvent(JSON.parse(data)); } catch { /* 忽略坏帧 */ }
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n')) >= 0) { handleLine(buf.slice(0, i)); buf = buf.slice(i + 1); }
    }
    buf += dec.decode();
    if (buf.trim()) handleLine(buf);
  }

  /* ---------- 主发送流程 ---------- */
  function syncSendUI() {
    sendBtn.style.display = state.sending ? 'none' : '';
    stopBtn.style.display = state.sending ? '' : 'none';
  }
  stopBtn.onclick = () => { if (state.abort) state.abort.abort(); };

  async function runCompletion() {
    if (state.sending) return;
    if (!cfg.model) { toast('请先在右侧选择模型', 'bad'); return; }
    if (!state.items.length || state.items[state.items.length - 1].role !== 'user') return;

    state.sending = true; syncSendUI();
    const ctrl = new AbortController();
    state.abort = ctrl;
    const sh = addAssistantShell();
    const item = sh.item;

    const t0 = performance.now();
    let ttft = null, usage = null, errText = null, errKind = null, stopped = false;
    const markTTFT = () => { if (ttft === null) ttft = performance.now() - t0; };
    const flush = () => { sh.body.innerHTML = renderMarkdown(item.content); scrollBottom(false); };
    const schedule = () => {
      if (!renderTimer) renderTimer = setTimeout(() => { renderTimer = null; flush(); }, 90);
    };

    const headers = {
      'Content-Type': 'application/json',
      'X-WB-Debug-Timeout': String(clampNum(cfg.timeout, 10, 180)),
    };
    if (cfg.account) headers['X-WB-Force-Account'] = cfg.account;

    const messages = [];
    if (cfg.system.trim()) messages.push({ role: 'system', content: cfg.system.trim() });
    for (const it of state.items) {
      if (it === item) continue; // 刚刚追加的空 assistant 壳
      if (it.role === 'assistant' && !it.content) continue; // 不带空回复进上下文
      messages.push({ role: it.role, content: it.content });
    }
    const payload = {
      model: cfg.model, stream: cfg.stream, messages,
      temperature: cfg.temp, max_tokens: cfg.maxTokens,
    };

    try {
      const res = await fetch('/api/chat/completions', {
        method: 'POST', headers, body: JSON.stringify(payload), signal: ctrl.signal,
      });
      const usedAcc = res.headers.get('X-WB-Account');
      sh.accBadge.textContent = '账号：' + (usedAcc || '未知');
      if (!res.ok) {
        let msg = 'HTTP ' + res.status;
        try { const j = await res.json(); if (j && j.error && j.error.message) msg = j.error.message; } catch {}
        const err = new Error(msg);
        err.kind = res.status === 502 ? 'reject' : 'http';
        throw err;
      }
      if (cfg.stream) {
        await readSSE(res, (obj) => {
          if (obj === DONE) return;
          if (obj.error) { // 上游错误帧：HTTP 仍是 200，保留已流出内容
            errText = obj.error.message || '上游返回未知错误';
            errKind = 'upstream';
            return;
          }
          if (obj.usage) usage = obj.usage;
          const ch = obj.choices && obj.choices[0];
          const d = ch && ch.delta;
          if (!d) return;
          if (d.reasoning_content) {
            markTTFT();
            item.reasoning += d.reasoning_content;
            sh.think.style.display = '';
            sh.thinkB.textContent = item.reasoning;
            sh.thinkB.scrollTop = sh.thinkB.scrollHeight;
          }
          if (d.content) { markTTFT(); item.content += d.content; schedule(); }
          if (d.tool_calls) sh.tools.style.display = '';
        });
      } else {
        const j = await res.json();
        if (j && j.error) {
          const err = new Error(j.error.message || '上游错误');
          err.kind = 'upstream';
          throw err;
        }
        const m = (j.choices && j.choices[0] && j.choices[0].message) || {};
        markTTFT();
        if (m.reasoning_content) {
          item.reasoning = m.reasoning_content;
          sh.think.style.display = '';
          sh.thinkB.textContent = item.reasoning;
        }
        if (m.content) item.content = m.content;
        if (m.tool_calls) sh.tools.style.display = '';
        usage = j.usage || null;
      }
    } catch (e) {
      if (e && e.name === 'AbortError') {
        stopped = true;
      } else if (!errText) {
        errKind = (e && e.kind) || 'network';
        errText = errKind === 'network'
          ? '连接中断或网关不可达（' + (e && e.message ? e.message : 'network failure') + '）'
          : (e && e.message) || '未知错误';
      }
    }

    // ---- 收尾渲染 ----
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    flush();
    sh.body.classList.remove('live');
    if (item.reasoning) sh.think.classList.add('closed'); // 流式结束后自动折叠
    if (!item.content && !item.reasoning) {
      sh.body.innerHTML = '<span class="dim">（无内容）</span>';
    }
    const dur = performance.now() - t0;
    const parts = ['耗时 <b>' + fmtDur(Math.round(dur)) + '</b>'];
    if (ttft !== null) parts.push('首字 <b>' + fmtDur(Math.round(ttft)) + '</b>');
    if (usage) {
      parts.push('输入 <b>' + fmtInt(usage.prompt_tokens || 0) + '</b> / 输出 <b>' + fmtInt(usage.completion_tokens || 0) + '</b> tokens');
      if (usage.credit != null) parts.push('credit <b>' + trimNum(usage.credit) + '</b>');
    }
    sh.meta.innerHTML = parts.map((p) => '<span>' + p + '</span>').join('');

    if (stopped) {
      sh.errBox.style.display = '';
      sh.errBox.classList.add('chat-stop-note');
      sh.errBox.innerHTML = '<span class="badge idle nodot">已停止</span><span class="dim">已手动中断请求，已接收的内容保留如上</span>';
    } else if (errText) {
      sh.errBox.style.display = '';
      const label = { reject: '网关拒绝（502）', upstream: '上游错误', http: 'HTTP 错误', network: '网络错误' }[errKind] || '错误';
      sh.errBox.innerHTML = '<span class="badge bad nodot">' + label + '</span><span>' + escapeHtml(errText) + '</span>' +
        (item.content || item.reasoning ? '<span class="dim">（已保留部分内容）</span>' : '');
    }

    state.sending = false;
    state.abort = null;
    syncSendUI();
    scrollBottom(false);
  }

  function send() {
    if (state.sending) return;
    const text = ta.value.trim();
    if (!text && !state.images.length) return;
    if (!cfg.model) { toast('请先在右侧选择模型', 'bad'); return; }
    let content;
    if (state.images.length) {
      content = [];
      if (text) content.push({ type: 'text', text });
      for (const im of state.images) content.push({ type: 'image_url', image_url: { url: im.dataUrl } });
    } else {
      content = text;
    }
    addUserMessage(content);
    ta.value = ''; autoGrow();
    state.images = []; renderThumbs();
    runCompletion();
  }
  sendBtn.onclick = send;
  ta.addEventListener('input', autoGrow);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });

  /* ---------- 清空 ---------- */
  clearBtn.onclick = async () => {
    if (state.sending) { toast('正在发送中，请先停止', 'bad'); return; }
    if (!state.items.length) return;
    const ok = await confirmDialog({ title: '清空对话', body: '将删除当前全部消息记录，且不可恢复。', okText: '清空' });
    if (!ok) return;
    for (const it of state.items) { if (it.el) it.el.remove(); }
    state.items = [];
    showEmptyIfNeeded();
    toast('已清空对话');
  };

  /* ---------- 组装与卸载清理 ---------- */
  const col = el('div', { class: 'chat-col' });
  col.append(msgs, composer);
  const main = el('div', { class: 'chat-main' });
  main.append(col, side);
  root.append(head, main);
  refreshIcons(root);
  autoGrow();
  loadModels();
  loadAccounts();

  const mo = new MutationObserver(() => {
    if (!root.isConnected) {
      try { if (state.abort) state.abort.abort(); } catch {}
      if (renderTimer) clearTimeout(renderTimer);
      mo.disconnect();
    }
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });
}