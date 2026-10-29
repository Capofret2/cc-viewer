// 一轮走到哪一步的写法（看状态就能直接知道链路卡在哪一段，同一句话在任何情况下意思都一样）。
// 每条只说一件事：哪一段（src）、在等什么（text）。新会话、续接、插话、工具执行完接着请求，这些只写在括号里，不改主语义。
// 阶段由服务器推来（chat.Driver.status、server.Tab.prep、server.Birth），这里只管怎么写。

// 链路上的各段，从浏览器往外：网络 → 查看器 → Claude Code → 工具 / 出网 → 上游
export const SRC_TIPS = {
  网络: '浏览器和服务器上的查看器之间',
  查看器: '服务器上的网页查看器自己在做的事',
  'Claude Code': '服务器上的 Claude Code 进程',
  工具: 'Claude Code 在服务器上执行的工具',
  出网: '服务器连到目标网站的这一段（配了代理就经代理）',
  上游: 'Claude、ChatGPT 或 DeepSeek 的服务器',
};
// 标签配色：网络红、出网黄、上游主题色，其余灰
export const SRC_CLASS = { 网络: 'net', 出网: 'out', 上游: 'up' };

// 状态那句话底下的细进度条（存在感不用强）：一轮要走的各步按先后排成格子，走过的填满、当前这格扫光、没到的灰。
// 每一步要多久没法预知，所以只按走到第几格画，不假装知道百分之几。
// 它只表示这一轮走到了哪儿，只往前走：唯一的例外是工具跑完接着请求时回到「准备请求」那格，
// 前面送达、启动、连接那几格照旧是满的；复用老连接、CLI 早就开着时没经过的格子也一样算走过了。
// 此刻卡在链路的哪一段由文字开头的段名说，不靠格子：中途跑钩子、新开连接、出状况都停在当前这格（出状况标黄），不往回跳
export const STEPS = ['送到查看器', '查看器准备', '启动 Claude Code', '连本机代理', '代理经节点连目标网站', '连上服务器',
                      '准备请求', '等第一个字', '回复', '执行工具'];
// 拦下、代理不通、节点连不上都是请求发出去以后重试时才报的，和重试一样停在「等第一个字」
const AT = { checking: 1, blocked: 7, starting: 2, preparing: 6,
             waiting: 7, retry: 7, proxy_down: 7, dial_failed: 7,
             replying: 8, thinking: 8, writing: 8, tool_input: 8, compacting: 8, tool: 9 };
const NET = { proxy: 3, dial: 4, hello: 5 };
const WARN = new Set(['blocked', 'proxy_down', 'dial_failed', 'retry']);

// p：服务器给的阶段。返回第几格（从 0 起，回复完了在收尾就是全满）和这格是不是出了状况（标黄）
export function phaseStep(p) {
  if (!p) return { i: 0 };
  if (p.kind === 'finishing') return { i: STEPS.length };
  // 钩子插在别的步骤中间（收到话后、工具执行前后）：停在它打断的那一格（under）；旧后端不给 under，照旧算查看器准备
  if (p.kind === 'hook') return { i: p.under ? phaseStep({ kind: p.under }).i : 1 };
  let i = AT[p.kind];
  // 连接还没通：只有启动时才往前推连代理、经节点、连上服务器这三格；准备请求、等第一个字时新开的连接留在原来那格
  if (p.kind === 'net') i = { preparing: 6, waiting: 7 }[p.under] ?? NET[p.stage] ?? 5;
  // 连接通了在等服务器回话：启动时问的那几样东西算连上服务器这一步的尾巴，准备请求时问的算准备请求
  if (p.kind === 'await') i = p.under === 'preparing' ? 6 : 5;
  return { i: i ?? 8, warn: WARN.has(p.kind) };
}

// 没有阶段：浏览器正在把这句话交给查看器（发话、新建会话的接口都只做马上能做完的检查就回，见 server.say、new_session）
export const SENDING = { src: '网络', text: '正在发送至查看器' };

// p：服务器给的阶段；name：思考、写回复这几句的主语，挂了人设卡是卡名，没挂时 GPT 号是 GPT、DeepSeek 号是 DeepSeek，空就是 Claude；
// 连的是哪家服务器那几句照旧写 Claude / ChatGPT / DeepSeek。
// 复用老连接时不写「这条连接多少秒没收到数据」，服务器照旧给 rxQuiet，这里不用
export function phaseLine(p, name = '') {
  if (!p) return SENDING;
  const who = p.sub ? '子代理' : name || 'Claude';
  const via = p.port ? `${p.port}${p.tag ? `（${p.tag}）` : ''}` : '';
  const node = p.tag || p.port || '节点';
  // API 号（DeepSeek）服务器给 vendor、vhost：旧后端不给，照旧写 Claude
  const server = p.vendor || (p.bridge ? 'ChatGPT' : 'Claude');
  const host = p.host || p.vhost || (p.bridge ? 'chatgpt.com' : 'api.anthropic.com');
  // GPT 号去 ChatGPT 的那一段是它的翻译层在连，翻译层同时服务这个号的几个会话，只分得到号
  const by = p.bridge ? '翻译层' : '';
  const line = (src, text, warn = false) => ({ src, text, warn });
  // 写法：言简意赅、礼貌正式，段名标签已经说了是哪一段，文字里不再重复；不用「了」「已经」这类口语
  switch (p.kind) {
    // 发话后交给 Claude Code 之前：先核对额度（必要时换号），核过了再记账、交话（step，旧后端不给，照旧写核对额度）
    case 'checking': return line('查看器', p.step === 'handing' ? '正在提交至 Claude Code' : '正在核对额度');
    case 'hook': return line('查看器', '正在核对额度');
    case 'blocked': return line('查看器', '额度预测未通过，已暂停发送请求', true);
    case 'starting':
      return line('Claude Code', `正在启动（${p.resume ? '续接' : '新会话'}${p.account ? ` · ${p.account}` : ''}）`);
    case 'preparing': return line('Claude Code', '正在准备请求');
    case 'finishing': return line('Claude Code', '回复完成，正在收尾');
    case 'stopping': return line('Claude Code', '已请求中断，等待停止');
    case 'tool': {
      const tools = (p.tools || []).map(t => t.desc || `正在执行 ${t.name}`).join('；') || '正在执行工具';
      return line('工具', (p.sub ? '子代理 · ' : '') + tools);
    }
    case 'net': {
      // 去模型服务器的连接还没通：卡在哪一段就写哪一段（Claude Code 启动、准备请求时要先连网的，括号里写明）
      const when = { starting: '（启动时）', preparing: '（准备请求时）' }[p.under] || '';
      const text = {
        proxy: `${by}正在连接本机代理 ${via}`,
        dial: `${by}正在经 ${node} 连接 ${host}`,
        hello: `${by}正在与 ${host} 握手`,
      }[p.stage] || `${by}正在连接 ${host}：${p.stage}`;
      return line('出网', text + when);
    }
    case 'await': {
      // Claude Code 启动、准备请求时问服务器的几样东西还没回来（连接是通的）
      const when = { starting: '（启动时）', preparing: '（准备请求时）' }[p.under] || '';
      return line('上游', `等待 ${host} 响应${when}`);
    }
    case 'proxy_down': return line('出网', `无法连接本机代理 ${via}，正在重试`, true);
    case 'dial_failed': return line('出网', `无法经 ${node} 连接 ${host}，正在重试`, true);
    case 'waiting': {
      if (p.reused) return line('上游', `请求已经${by ? '翻译层的' : ''}现有连接发送，等待 ${server} 响应`);
      return line('上游', by ? '请求已经翻译层发送至 ChatGPT，等待响应' : `请求已发送，等待 ${server} 响应`);
    }
    case 'replying': return line('上游', `${who} 开始回复`);
    case 'thinking': return line('上游', `${who} 正在思考${p.tokens ? `（约 ${p.tokens} token）` : ''}`);
    case 'writing': return line('上游', `${who} 正在撰写回复`);
    case 'tool_input': return line('上游', `${who} 正在撰写工具调用${p.tool ? `：${p.tool}` : ''}`);
    case 'compacting': return line('上游', '正在压缩上下文');
    case 'retry':
      return line('上游', `${server} 返回${p.status ? ` ${p.status}` : '错误'}，正在重试（${p.attempt ?? '?'}/${p.max ?? '?'}）`, true);
    default: return line('Claude Code', p.kind);
  }
}
