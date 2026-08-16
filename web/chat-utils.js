/* Safe Markdown rendering and OpenAI multimodal message helpers. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.WBChatUtils = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));

  function safeUrl(url) {
    const value = String(url || '').trim();
    if (/^(https?:\/\/|mailto:)/i.test(value)) return escapeHtml(value);
    return '';
  }

  function inlineMarkdown(source) {
    const code = [];
    let text = String(source ?? '').replace(/`([^`\n]+)`/g, (_, value) => {
      const token = `\u0000CODE${code.length}\u0000`;
      code.push(`<code>${escapeHtml(value)}</code>`);
      return token;
    });
    text = escapeHtml(text);
    text = text.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, label, url) => {
      const href = safeUrl(url);
      return href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    });
    text = text
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
      .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');
    return text.replace(/\u0000CODE(\d+)\u0000/g, (_, i) => code[Number(i)] || '');
  }

  function isTableDivider(line) {
    const cells = String(line).trim().replace(/^\||\|$/g, '').split('|');
    return cells.length > 1 && cells.every(cell => /^\s*:?-{3,}:?\s*$/.test(cell));
  }

  function tableCells(line) {
    return String(line).trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim());
  }

  function blockStart(lines, index) {
    const line = lines[index] || '';
    return /^\s*```/.test(line) || /^\s{0,3}#{1,6}\s+/.test(line) ||
      /^\s{0,3}(?:[-+*]\s+|\d+[.)]\s+|>\s?)/.test(line) ||
      /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line) ||
      (line.includes('|') && isTableDivider(lines[index + 1] || ''));
  }

  function renderMarkdown(source) {
    const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }

      const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
      if (fence) {
        const language = fence[1] ? ` class="language-${escapeHtml(fence[1])}"` : '';
        const body = [];
        i += 1;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
        if (i < lines.length) i += 1;
        out.push(`<pre><code${language}>${escapeHtml(body.join('\n'))}</code></pre>`);
        continue;
      }

      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        i += 1; continue;
      }

      if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        out.push('<hr>'); i += 1; continue;
      }

      if (line.includes('|') && isTableDivider(lines[i + 1] || '')) {
        const headers = tableCells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(tableCells(lines[i++]));
        out.push('<div class="md-table-wrap"><table><thead><tr>' +
          headers.map(x => `<th>${inlineMarkdown(x)}</th>`).join('') +
          '</tr></thead><tbody>' + rows.map(row => '<tr>' + headers.map((_, n) =>
            `<td>${inlineMarkdown(row[n] || '')}</td>`).join('') + '</tr>').join('') +
          '</tbody></table></div>');
        continue;
      }

      const unordered = line.match(/^\s{0,3}[-+*]\s+(.+)$/);
      const ordered = line.match(/^\s{0,3}\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        const tag = unordered ? 'ul' : 'ol';
        const items = [];
        const matcher = unordered ? /^\s{0,3}[-+*]\s+(.+)$/ : /^\s{0,3}\d+[.)]\s+(.+)$/;
        while (i < lines.length) {
          const item = lines[i].match(matcher);
          if (!item) break;
          items.push(`<li>${inlineMarkdown(item[1])}</li>`); i += 1;
        }
        out.push(`<${tag}>${items.join('')}</${tag}>`);
        continue;
      }

      if (/^\s{0,3}>\s?/.test(line)) {
        const quote = [];
        while (i < lines.length && /^\s{0,3}>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s{0,3}>\s?/, '')); i += 1;
        }
        out.push(`<blockquote>${quote.map(inlineMarkdown).join('<br>')}</blockquote>`);
        continue;
      }

      const paragraph = [line.trim()];
      i += 1;
      while (i < lines.length && lines[i].trim() && !blockStart(lines, i)) paragraph.push(lines[i++].trim());
      out.push(`<p>${paragraph.map(inlineMarkdown).join('<br>')}</p>`);
    }
    return out.join('');
  }

  function buildMessageContent(text, images) {
    const cleanText = String(text ?? '').trim();
    const validImages = (images || []).filter(image =>
      /^data:image\/(?:png|jpe?g|webp|gif);base64,/i.test(String(image?.dataUrl || '')));
    if (!validImages.length) return cleanText;
    const parts = [];
    if (cleanText) parts.push({ type: 'text', text: cleanText });
    for (const image of validImages) {
      parts.push({ type: 'image_url', image_url: { url: image.dataUrl } });
    }
    return parts;
  }

  return { escapeHtml, renderMarkdown, buildMessageContent };
});
