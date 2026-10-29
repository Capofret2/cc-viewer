// 把会话导出成一份 Markdown 存到本地
import { api, clock, fileName, who } from './util.js';

const RESULT_LINES = 200;   // 工具结果太长就截一截，不然导出的文件会大得没法看

const fence = (text, lang = '') => '```' + lang + '\n' + String(text).replace(/```/g, '``​`') + '\n```';

function clipLines(text, n = RESULT_LINES) {
  const lines = String(text).split('\n');
  return lines.length <= n ? text : lines.slice(0, n).join('\n') + `\n…（另有 ${lines.length - n} 行未导出）`;
}

function toolBlock(item, result) {
  const i = item.input || {};
  const head = i.command || i.file_path || i.notebook_path || i.pattern || i.url || i.query
    || i.description || i.skill || i.path || '';
  const out = [`**工具 · ${item.name}**` + (head ? ` \`${String(head).split('\n')[0].slice(0, 160)}\`` : '')];
  if (item.name === 'Bash' && i.command) out.push(fence(i.command, 'bash'));
  else if (i.old_string !== undefined || i.content !== undefined) {
    if (i.old_string) out.push('改之前：', fence(clipLines(i.old_string, 60)));
    out.push('改之后：', fence(clipLines(i.new_string ?? i.content ?? '', 60)));
  } else if (Object.keys(i).length) out.push(fence(JSON.stringify(i, null, 2), 'json'));
  if (result?.text) {
    const n = result.text.split('\n').length;
    out.push(`<details><summary>${result.error ? '出错' : '结果'} · ${n} 行</summary>`, '',
             fence(clipLines(result.text)), '', '</details>');
  }
  if (result?.images) out.push(`（结果含 ${result.images.length} 张图片，未包含在导出文件中）`);
  return out.join('\n\n');
}

function toMarkdown(tab, items) {
  const results = Object.fromEntries(items.filter(i => i.kind === 'result').map(i => [i.id, i]));
  const out = [`# ${tab.title || fileName(tab.path)}`, ''];
  if (tab.cwd) out.push(`- 工作目录：\`${tab.cwd}\``);
  out.push(`- 会话文件：\`${tab.path}\``,
           `- 导出于：${new Date().toLocaleString('zh-CN', { hour12: false })}`,
           `- 共 ${items.length} 条记录`, '', '---', '');
  for (const it of items) {
    const t = (it.text || '').trim();
    if (it.kind === 'user') out.push(`### ${it.queued ? '插话' : who.me} · ${clock(it.ts)}`, '',
                                     t || (it.image ? '（图片）' : ''), '');
    else if (it.kind === 'text') out.push(`### Claude · ${clock(it.ts)}${it.sidechain ? '（子代理）' : ''}`, '', t, '');
    else if (it.kind === 'thinking') out.push('<details><summary>思考</summary>', '', t, '', '</details>', '');
    else if (it.kind === 'tool') out.push(toolBlock(it, results[it.id]), '');
    else if (it.kind === 'command') {
      const tags = it.tags || {};
      const cmd = tags['command-name'] ? `${tags['command-name']} ${tags['command-args'] || ''}`.trim()
        : tags['bash-input'] ? `! ${tags['bash-input']}` : '';
      if (cmd) out.push(fence(cmd), '');
    } else if (it.kind === 'duration') out.push(`*本轮用时 ${(it.ms / 1000).toFixed(1)} 秒*`, '');
    else if (it.kind === 'notice' || it.kind === 'blocked' || it.subtype === 'away_summary') out.push(`*${t}*`, '');
  }
  return out.join('\n');
}

const safe = s => (s || 'session').replace(/[\\/:*?"<>|\n]+/g, '_').slice(0, 60).trim();

export async function exportMarkdown(tab) {
  // 手上可能只加载了末尾一段，导出要的是整段对话，所以重新取一遍全的
  const all = await api(`/api/tabs/${tab.id}/items?start=0&gen=${tab.gen}`);
  const md = toMarkdown(tab, all.items);
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '');
  const a = document.createElement('a');
  // 开头加 BOM：Blob 类型里的 charset 存成文件就没了，老记事本、WPS 这类软件见不到 BOM 会按 GBK 读成乱码
  a.href = URL.createObjectURL(new Blob(['﻿' + md], { type: 'text/markdown;charset=utf-8' }));
  a.download = `${safe(tab.title || fileName(tab.path))}_${stamp}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  return { items: all.items.length, size: md.length };
}
