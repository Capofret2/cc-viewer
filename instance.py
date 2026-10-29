"""这个查看器实例是谁的：运行时数据放在哪、能用哪些号、界面上怎么称呼用它的人。

同一份代码可以以不同的系统用户跑成几个实例（查看器和 Claude 可以是同一个用户，也可以分开，见下面的 run_as），
各实例不一样的地方都集中在这里，别处一律从这里取：
- 数据目录：环境变量 CC_VIEWER_DATA 指定，不设就是代码目录。所有运行时小文件
  （标签、选的号和模式、额度、调用记录……）都写在这里。用环境变量而不用命令行参数，是因为状态栏脚本
  由 Claude Code 拉起，只能从环境里拿到它。
- 账号表、不能用的号、称呼：数据目录里有 instance.json 就按它来，没有就用下面 else 里的默认值（单用户、只有一个用默认配置目录的号）。它长这样：
    {"accounts": {"cc1": "~/.claude-1", "cc2": "~/.claude-2"}, "banned": {"cc2": "已被封"}, "call": "用户",
     "users": true, "admins": ["小明"], "free": ["cc1"]}
  accounts 的值是这个号的 Claude Code 配置目录（null 表示不设 CLAUDE_CONFIG_DIR 的默认目录），顺序就是界面上的顺序。
  CLI 经 claude_via_bashrc.sh 启动：~/.bashrc 里有和号同名的别名就用别名，没有就直接运行 claude，配置目录按这里设。
- 用户（见 users.py）：users 为 true 是多用户实例，网页上能注册登录、不登录就是游客；不写就是单用户，
  谁打开都是同一个人（界面上的称呼见 call）。admins 是查看器账号的用户名。限量号是额度有限、按点数记账的号（不一定是花钱买的；用它的调用记进会话主人的账本）：
  accounts 里的号默认都是限量号，只有写进 free 的才是免费号；
  free 必须写（没有免费号就写 []），旧配置没写 free、又有不在 paid 里的号时启动就报错，免得它们以前免费的花费被补扣。
  paid 写成 {"cc2": "2026-10-10T16:47:25+08:00", "cc3": null}：值是从什么时候起算限量号（null 是一直都是，新号不写进来也一样）。
  账本每次都按名单把全部历史重算一遍，免费号改成限量号时要从 free 里拿掉、在 paid 里写上这个时刻，
  不写的话它以前免费时的花费就会被补记进账本。
  terminal_owner 写一个用户名：没记主人的会话（终端里建的）都算他的、只有他自己看得到，
  后台循环再把它们正式记到他名下（账本按记下的主人找会话）。运行 Claude 的系统用户只有一个人在用时才写；
  好几个人共用一个运行 Claude 的系统用户时，终端里建的会话认不出是谁的，不写，照旧是无主会话。
- 上游代理（必填）：proxy_port 写本机一个 HTTP 代理的端口（比如 7890），查看器拉起的 CLI、刷新额度、查平台余额都经它出网，所有号走同一个。
  没写就拒绝启动，不会悄悄直连。确实要直连，得在 instance.json 里亲手写上 "allow_direct": true（模板里是 false），
  像开服前签 EULA 一样，表示知道直连时服务商会直接看到这台机器的出口地址；打开了也是写了 proxy_port 就照样经代理。
  号多、要分开走不同代理时，proxy_table 写一张端口表的路径：每行「系统用户 号 端口」，号写 * 是这一组的默认端口，
  系统用户是 run_as（没写就是跑查看器的用户）。表里有的号走自己的端口，没有的走这一组的默认端口，表里没有默认端口再看 proxy_port。
  proxy_info 写一份 {"端口": {"tag": 节点名, "region": 地区}} 的 JSON 的路径，账号面板上照它写节点和地区。
  环境变量 CC_PROXY_TABLE 能临时换一张端口表（测试用）。
- 机器在界面上的叫法：host_name 写成 "实验室服务器" 这样，界面和报错里提到查看器所在的机器时用它；不写是「服务器」。
- 只准经别名启动：alias_only 写 true 时，~/.bashrc 里没有和号同名的别名就拒绝启动 CLI；不写是没有别名就直接运行 claude，
  配置目录按 accounts 设。别名里管着代理、账号这些启动约束时打开，免得哪个号漏配别名就绕了过去。
- 看板娘：xxlb 写 true 表示已经自己放好了 static/live2d/xxlb/ 的模型和 Live2D 运行库（开源版不带，见 static/js/xxlb.js），
  新会话页才画她；不写就不去请求这些文件。
- 电脑通路：pc_setup_url 写装电脑脚本、卸载脚本对外的地址（比如 "https://example.com"，不带结尾的斜杠），
  设备页给的卸载命令从这里下；不写就用打开设备页时的地址。
- 分享链接的地址：share_url 写成 "https://example.com" 这样（不带结尾的斜杠），主动分享时签发的只读链接在它后面接 /share/<随机凭证>。
  不写就用复制的人当时打开页面的地址；局域网地址发给校外的人是打不开的，所以给别人用的实例要写公网入口。
- 额度换算：quota 写成 {"pts_per_five": 1.96, "five_in_week": 0.133}。pts_per_five 是这组号 5 小时额度的 1% 合多少点
  （点数单价见 points_rates.json；关了计费标记时，同样的 token 只扣一半 5 小时额度，要另写这个数），
  five_in_week 是 5 小时窗口的 1% 约合本周额度的百分之几。血条按本周砍短、预定额度的定价和折算、预测拦截都用这两个数；
  不写就是 1 和 0.071。
- 运行 Claude 的系统用户：run_as 写成 "claude-runner" 这样。会话记录、各号的配置目录、工作目录都在他名下，查看器拉起 CLI、
  读写那边的文件都换成他的身份（见 hostfs.py）；不写就是跑查看器的这个用户自己。多人共用时让查看器以专用用户运行、
  Claude 以 run_as 运行，大家的 Claude 就碰不到查看器的数据。
  账号表里的 ~ 按这个用户的家目录展开（HOME）。
- Claude 那边的数据目录：guest_data 写成 "~/.cc_viewer"。状态栏脚本跟着 Claude 以 run_as 的身份跑，记的额度和调用
  写在这里，查看器定时收进自己的数据目录（inbox.py）；刷新额度用的空目录（.usage_cwd，各号都信任过）也在这里。
  不写就是数据目录本身。
- 登录 cookie 的名字：cookie 写成 "ccv_5464fb36" 这样。不写就按数据目录的路径算（users.COOKIE）；数据目录搬了家
  （比如换了运行查看器的系统用户），要写上搬家前的名字，浏览器里已有的登录才照样认得，不用都重新登录。
- ssh 门卫的套接字：gw_socket，见下面 GW_SOCKET 和 sshgw.py。
- 注入的说明（代替 Claude 那边的全局 CLAUDE.md）：instructions 写一个文件的路径，
  共用环境的网页会话拉起 CLI 时现读、接在系统提示词后面；不写就不注入。人设卡接口 ccpersona 的套接字：persona_socket，
  不写就放在数据目录里按端口起名；数据目录别人进不去时（分了 run_as）要写一个运行 Claude 的用户进得去的地方。都见 personas.py。
- GPT 号：gpt 写成 {"gpt1": {"codex_home": "~/.codex", "five_in_week": 0.155}}。GPT 号照样是 Claude Code，
  只是别名里把 ANTHROPIC_BASE_URL 指到本机的翻译层（翻译层自备，不在本仓库里；bridge_port 写它的端口，不写是 18765），
  由它转到 ChatGPT 的 Codex 后端，用的是 codex_home 里 Codex 的 ChatGPT 登录（额度也拿这份登录去查，见 usage_probe.py）。
  登录和翻译层都在查看器自己这边（分了 run_as 时 Claude 那边读不到），所以 codex_home 的 ~ 按查看器自己的家目录展开。
  five_in_week 是这个号自己的换算（ChatGPT Plus 的 5 小时窗口用满约合本周的 15.5%，按 Codex 历史记录估的），
  pts_per_five 也可以单写；不写就按周额度同价算（GPT 号用满一周和这组的 Claude 号用满一周一样贵），
  即 PTS_PER_FIVE × 这个号的 five_in_week ÷ FIVE_IN_WEEK。GPT 模型的单价表写的是「GPT 号 5 小时额度的 1%」，
  读单价时乘这个数换成点（GPT_SCALE、rates.load）。
  每个 GPT 号有自己的 ChatGPT 登录，翻译层也是一个号一份：bridge_port 可以写在号里，不写就用实例的 bridge_port。
  没有 5 小时限额、只有本周额度的号（比如 ChatGPT Pro）写 "week_only": true。
  所有 GPT 号用同一套 token 单价，所以它的 5 小时 1% 只是个计价单位，合的点数和别的 GPT 号的一样（GPT_SCALE），
  five_in_week 写这个单位约合它本周额度的百分之几（必须写）。预测拦截、预定都按本周额度算，血条画本周的。
- 网页对话的默认权限模式：default_mode 写成 "bypassPermissions" 这样，用在没选过权限模式的会话和新会话标签页上；不写是 auto。
  （auto 在 GPT 号上按 dontAsk 跑，用户在聊天里说「允许」不生效，多人共用时可以考虑 bypassPermissions。）
- 网页对话的默认思考强度：default_effort 写成 "high"。会话没选过强度、CLI 读的那份 Claude Code 设置里
  也没给这个模型写强度时，拉起 CLI 就带 --effort 这一档；设置里写了的照设置（CLI 自己的默认是 medium）。
- 网页对话的默认模型：default_model 写成 "claude-opus-5-5"。Claude 号的会话没选过模型、CLI 读的那份设置里也没写 model 时，
  拉起 CLI 就带 --model 这个模型，界面上也照它写；设置里写了的照设置。
- 限时特惠：promo 写成 {"gpt2": {"from": "2026-10-13T22:24:00+08:00", "until": "2026-10-16T21:55:03+08:00",
  "factor": 0.1, "label": "特惠 1折"}}。这段时间里在这个号上发的调用，花费照单价算，只有 factor 那几成进账本（界面上红的，有预定就由预定付、变蓝），
  其余免掉（绿的，不扣钱也不扣预定）；预定按原价收，不受特惠影响。
  额度本身（预测拦截、预定能订多少、拟合）照旧，不打折。到点自动恢复，网页上的标签也跟着消失。
  一个号有好几段时写成一串：{"gpt3": [{...第一段...}, {...第二段...}]}，几段重叠取低的。
  免掉的那几成要有绿条（大家共用的额度）来出，绿条不够就少免、用完了就不免（见 reserve._settle）。
- 自动特惠：auto_promo 写成 {"since": "2026-10-18T05:00:00+08:00", "hours": 24, "factor": 0.5, "label": "特惠半价"}，
  Claude 限量号（AUTO_ACCOUNTS）在本周额度重置前 hours 小时里自动按 factor 进账本，规矩和限时特惠一样，两个都在取低的（reserve.promo_factor）。
  再写 "official_since": "2026-10-23T..." 的话，从这一刻起自动特惠只给号主是查看器账号的号（官方号），
  别人名下的号由号主自己在预定面板里定折扣（reserve.set_owner_promo）；这一刻以前已经免掉的照旧算，不补扣。
- 号的到期时间：expires 写成 {"cc3": "2026-11-08T15:26:00+08:00"}，到期以后才开始的周和窗口不能预定（见下面 EXPIRES）。
- 号主：owners 写成 {"gpt3": {"user": "小明", "since": "2026-10-21T03:00:00+08:00"}, "cc1": {"user": "admin"}}。
  从 since 起（不写就是从头），谁在这个号上花了钱，同样的数记进号主的收入（见 users.earnings）；号主自己用就是先扣后收。
- 远端回环：remote_loops 写成 {"cc2（API回环）": {"account": "cc2", "url": "http://127.0.0.1:8787",
  "key_file": "/绝对路径"}}。用同一台机器上另一个实例的回环时，每个入口一把只开那个号的密钥，网页端选哪个入口就带哪把钥匙；
  请求直接打那个实例的本机接口，不读那边的登录文件，也不让那边的 API 端自动换号。remote_gpt 写一个 GPT 号列表，
  表示翻译层在本机、直接经闸门连（不靠 .bashrc 别名）。
- 回忆搜索：recall 写 true，共用环境里查看器账号的网页会话在系统提示词里多一段 sessions.py grep 的用法（chat.VIEWER_RECALL）。
  这个脚本要读查看器的数据目录，默认的写法（本机直接运行 sessions.py）只有 Claude 以跑查看器的这个用户运行时才能用。
  分了 run_as 的实例要另写 recall_cmd：会话里运行的回忆命令（到 sessions.py 为止，后面接 grep 和参数），
  比如 "ssh <查看器账号在设备页登记的、以跑查看器的用户登录本机的连接> /路径/python /路径/sessions.py"。门卫只让登记这条连接的账号用它，
  别的用户的会话照旧读不到。
- 普通用户回忆：recall_users 写 true，共用环境里会话主人不是查看器账号的网页会话在系统提示词里多一段 ccrecall 的用法
  （chat.VIEWER_RECALL_USER），ccrecall 经人设卡接口的套接字由查看器以自己的身份只搜本实例里主人是他的会话（sessions.serve_request）。
  不写就不给说明，普通用户敲 ccrecall 会被拒；查看器账号敲 ccrecall 不受它限制（搜本实例全部会话），系统提示词里照旧只有上面那段。
- 待办共用：todos_shared 写成 {"file": "/绝对路径/todos.json", "users": {"网页账号": "共用文件里的名字"}}，
  这几个网页账号的待办不存本实例的数据目录，存进那份共用文件里右边那个名字底下；两个实例写同一个文件、同一个名字，就是同一份（见 todos.py）。
- 频道：channels 写成 {"file": "/绝对路径/channels.json"}，会话之间的群聊存在这份文件里；两个实例写同一份就互通
  （目录要给两边的系统用户开 ACL，见 channels.py）。不写就不开频道。
只用标准库：状态栏脚本、只读模式下都能放心导入。
"""
import json
import re
import os
import pwd
from datetime import datetime
from pathlib import Path

CODE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("CC_VIEWER_DATA") or CODE).expanduser().resolve()

_file = DATA / "instance.json"
_conf: dict = {}
if _file.exists():
    # 文件在却读不了、格式不对，就直接报错停下：不能悄悄当成默认配置，拿错号去用
    _conf = json.loads(_file.read_text())
    _accounts, BANNED, CALL = _conf["accounts"], _conf.get("banned", {}), _conf.get("call", "用户")
    _paid = _conf.get("paid", [])
    MULTI, ADMINS = bool(_conf.get("users")), set(_conf.get("admins", []))
    # 号默认是限量号，只有写进 free 的才免费（老版本反过来，加号漏写 paid 就悄悄成了免费号）。
    # 没写 free 的旧配置里，不在 paid 里的号原来是免费的，照新规矩会变成限量号、账本按全部历史补扣，所以直接报错停下，要写清楚
    if "free" not in _conf and (_old_free := [a for a in _accounts if a not in _paid]):
        raise ValueError(f"instance.json 没写 free：号默认是限量号，{'、'.join(_old_free)} 以前按免费号算，"
                         "照旧免费就写进 free，要收钱就写进 paid 并写上起算时刻")
    FREE: set[str] | None = set(_conf.get("free", []))
    if _bad := [a for a in FREE if a not in _accounts or a in _paid]:
        raise ValueError(f"instance.json 的 free 里 {'、'.join(_bad)} 不在 accounts 里，或者同时写进了 paid")
    # 号 → 从这个 unix 秒起才是限量号；不在表里的一直都是
    PAID_SINCE = {a: datetime.fromisoformat(t).timestamp() for a, t in _paid.items() if t} if isinstance(_paid, dict) else {}
    SHARE_URL = _conf.get("share_url")
    _quota = _conf.get("quota", {})
else:
    # 没有 instance.json 时的默认值：单用户，只有一个号，用 Claude Code 的默认配置目录（~/.claude）
    _accounts = {"claude": None}
    BANNED = {}   # 不能用的号和原因，列出来但不许选
    CALL = "用户"
    # 单用户：只有一个人，没有限量号（FREE 为 None 表示全部免费）
    MULTI, ADMINS, FREE, PAID_SINCE = False, set(), None, {}
    SHARE_URL = None   # 自己发给自己的设备，用当时打开页面的地址就行
    _quota = {}
# 5 小时额度的 1% 合多少点；5 小时窗口的 1% 约合本周额度的百分之几。默认 1 点按定义就是 5 小时的 1%；
# 0.071 是从 quota_history.jsonl 估的（23 组，单组 0.049～0.090）
PTS_PER_FIVE: float = float(_quota.get("pts_per_five", 1.0))
FIVE_IN_WEEK: float = float(_quota.get("five_in_week", 0.071))
RUN_AS: str | None = _conf.get("run_as")
DEFAULT_MODE: str = _conf.get("default_mode") or "auto"   # 网页对话没选过权限模式时用它（server.py 启动时核对是不是认得的模式）
# 网页对话没选过思考强度、Claude Code 设置里也没给这个模型写强度时，拉起 CLI 带的 --effort（server.launch_effort）；
# 不写就不带，按 CLI 自己的默认（medium）。server.py 启动时核对是不是认得的一档
DEFAULT_EFFORT: str | None = _conf.get("default_effort")
# Claude 号的网页对话没选过模型、Claude Code 设置里也没写默认模型时，拉起 CLI 带的 --model（server.launch_model）；
# 不写就不带，按 CLI 自己的默认。GPT 号的默认模型由启动脚本定（GPT_MODELS 的第一个），不归它管。server.py 启动时核对
DEFAULT_MODEL: str | None = _conf.get("default_model")
COOKIE: str | None = _conf.get("cookie")
# 终端里建的会话归谁（见开头 users 的说明）。只在多用户实例里有意义，写错了地方就报错停下，不能当成那些会话有人管着
TERMINAL_OWNER: str | None = _conf.get("terminal_owner")
if TERMINAL_OWNER and not MULTI:
    raise ValueError("instance.json 写了 terminal_owner，却没开 users：单用户实例里所有会话本来就是那一个人的")
# ssh 门卫（sshgw.py）的 Unix 套接字，gw_socket 写成 "/srv/cc-viewer/gw/ssh.sock" 这样；不写就不开门卫。
# 套接字所在的目录要让运行 Claude 的那个用户进得去（一路 711），里面只放套接字（人设卡接口的 persona.sock 也可以放在那里）
GW_SOCKET: str | None = _conf.get("gw_socket")
INSTRUCTIONS: str | None = _conf.get("instructions")       # 共用环境注入的说明（见开头）
PERSONA_SOCKET: str | None = _conf.get("persona_socket")   # ccpersona 的套接字（见开头）
RECALL: bool = bool(_conf.get("recall"))                    # 提示词里给不给回忆搜索的用法（见开头）
RECALL_CMD: str | None = _conf.get("recall_cmd")            # 会话里运行回忆的命令，不写是本机直接运行 sessions.py（见开头）
RECALL_USERS: bool = bool(_conf.get("recall_users"))        # 普通用户能不能用 ccrecall 回忆自己的会话（见开头）
TODOS_SHARED: dict | None = _conf.get("todos_shared")       # 待办共用的文件和账号对应（见开头）
CHANNELS: dict | None = _conf.get("channels")               # 频道存在哪份文件（见开头）
# 运行 Claude 的那个用户的家目录。没分 run_as 时按环境变量 HOME 来
HOME: str = pwd.getpwnam(RUN_AS).pw_dir if RUN_AS else str(Path.home())


def expand(p: str) -> str:
    """~ 开头的路径按运行 Claude 的那个用户的家目录展开（查看器自己不一定是这个用户）。"""
    return HOME + p[1:] if p == "~" or p.startswith("~/") else str(Path(p).expanduser())


GUEST_DATA = Path(expand(_conf["guest_data"])) if _conf.get("guest_data") else DATA
ACCOUNTS: dict[str, str | None] = {a: expand(d) if d else None for a, d in _accounts.items()}

# 回环只是同一订阅的另一个入口，不增加号池容量，也不能指向另一个回环。
API_ENTRIES: dict[str, dict] = _conf.get("api_entries") or {}
for _name, _entry in API_ENTRIES.items():
    _base = _entry.get("account") if isinstance(_entry, dict) else None
    if _name in ACCOUNTS or _base not in ACCOUNTS or _base in API_ENTRIES:
        raise ValueError(f"API入口 {_name} 必须使用独立名称并指向现有底层账号")
    if _base in (_conf.get("gpt") or {}) or _base in (_conf.get("api") or {}):
        raise ValueError(f"API回环 {_name} 当前只支持Claude订阅账号")
    ACCOUNTS[_name] = ACCOUNTS[_base]
    if _base in PAID_SINCE:
        PAID_SINCE[_name] = PAID_SINCE[_base]
    if _base in BANNED:
        BANNED[_name] = BANNED[_base]
    elif not (_conf.get("api_gateway") or {}).get("enabled") or _base not in (_conf.get("api_gateway") or {}).get("accounts", []):
        BANNED[_name] = "API回环未开放"


# 隐藏原生入口只影响网页选号；底层号仍供网关、认证和历史记账使用。
WEB_ENTRIES: dict[str, str] = {}
for _name, _entry in API_ENTRIES.items():
    if _entry.get("hide_base"):
        _base = _entry["account"]
        if _base in WEB_ENTRIES:
            raise ValueError(f"{_base} 只能指定一个替代原生入口的API回环")
        WEB_ENTRIES[_base] = _name


def web_account(account):
    return WEB_ENTRIES.get(account, account)


def base_account(account):
    return (API_ENTRIES.get(account) or {}).get("account", account)


# 远端回环：把同一台机器上另一个实例已经做成回环的号当成本地账号用。每个入口一把只开那个号的密钥，
# 网页端选哪个入口就带哪把钥匙发请求，那边没有可换的号；请求直接打那个实例的本机接口，不碰那边的登录文件。
# remote_loops 写成 {"cc2（API回环）": {"account": "cc2", "url": "http://127.0.0.1:8787", "key_file": "/绝对/路径"}}。
# 账号表里加的这份用默认的 ~/.claude 配置目录，计价、模型和会话都照 Claude 号走。
_remote = _conf.get("remote_loops") or {}
if not isinstance(_remote, dict):
    raise ValueError("instance.json 的 remote_loops 必须是对象")
REMOTE: dict[str, dict] = {}
for _name, _r in _remote.items():
    _r = _r if isinstance(_r, dict) else {}
    _url, _key = _r.get("url"), _r.get("key_file")
    _upstream = _r.get('billing') == 'upstream'
    # 远端统一结算的号沿用本机同名的 API 号或 GPT 号配置（模型、历史单价），所以可以和账号表同名
    _metered = _name in (_conf.get('api') or {}) or _name in (_conf.get('gpt') or {})
    if (_name in ACCOUNTS and not (_upstream and _metered)) or not isinstance(_url, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]+', _url):
        raise ValueError(f"远端回环 {_name} 名称重复，或者 url 不是本机 http 地址")
    if _upstream:
        if not _metered or _name in (_conf.get('remote_gpt') or []) or not _r.get('account') or not _r.get('user'):
            raise ValueError(f'{_name} 远端结算必须是 API 号或 GPT 号（不能同时写在 remote_gpt 里），并指定固定账号和本地使用者')
    if not isinstance(_key, str) or not _key.startswith("/"):
        raise ValueError(f"远端回环 {_name} 的 key_file 必须是绝对路径")
    REMOTE[_name] = {"account": str(_r.get("account") or ""), "url": _url, "key_file": _key,
                     **({'billing': 'upstream', 'user': str(_r['user'])} if _upstream else {})}
    ACCOUNTS.setdefault(_name, None)


def remote_billed(account):
    """当前路由是否在上游结算；历史归属按每条调用的来源判断，不能靠安装配置的时刻划线。"""
    return REMOTE.get(account, {}).get('billing') == 'upstream'


# 限量号：没写进 free 的号全是（回环跟着底层的号走）
PAID: set[str] = set() if FREE is None else {a for a in ACCOUNTS if base_account(a) not in FREE}

# 上游代理（见开头的说明）：写了 proxy_port 就经本机这个 HTTP 代理出网，所有号走同一个。
# 没写又没亲手打开 allow_direct 时，server 启动就退出（server.py 解析完参数先查），proxy_port() 也抛错：
# 出网的地方都从 proxy_port() 取端口，哪一处漏了检查也拿不到「直连」这个结果
# 端口表（proxy_table）：这一组（run_as，没写是跑查看器的用户）的号各走各的端口，号写 * 的是这一组的默认端口
_table = os.environ.get("CC_PROXY_TABLE") or _conf.get("proxy_table")
PROXY_TABLE: Path | None = Path(_table).expanduser() if _table else None
_group = RUN_AS or pwd.getpwuid(os.getuid()).pw_name
_ports: dict[str, int] = {}
_table_default: int | None = None
if PROXY_TABLE:
    for _line in PROXY_TABLE.read_text().splitlines():   # 写了表却读不了就直接报错停下，不能当成没配代理
        _f = _line.split()
        if len(_f) >= 3 and not _f[0].startswith("#") and _f[0] == _group:
            if _f[1] == "*":
                _table_default = _table_default or int(_f[2])
            elif _f[1] in ACCOUNTS:
                _ports[_f[1]] = int(_f[2])
DEFAULT_PROXY_PORT: int | None = _table_default or (int(_conf["proxy_port"]) if _conf.get("proxy_port") else None)
ALLOW_DIRECT: bool = _conf.get("allow_direct") is True
NO_PROXY = ("没配上游代理，拒绝启动：在数据目录的 instance.json 里写上 proxy_port（本机 HTTP 代理的端口），号多时也可以写 proxy_table（见 instance.py 开头）。"
            f"还没有 instance.json 就先把代码目录里的 instance.example.json 复制成 {_file} 再改。"
            "确实要不经代理直连，在 instance.json 里亲手把 allow_direct 改成 true（服务商会直接看到这台机器的出口地址）")


def proxy_port(account: str) -> int | None:
    """这个号的网络通讯走本机哪个代理端口；None 是直连，只有 instance.json 亲手打开了 allow_direct 才会这样，没打开就抛错。"""
    port = _ports.get(base_account(account), DEFAULT_PROXY_PORT)
    if port is None and not ALLOW_DIRECT:
        raise RuntimeError(NO_PROXY)
    return port


# 账号面板上显示的：代理端口（打开了 allow_direct 又没配代理时是 None，界面上写「直连」）
# 节点名和地区照 proxy_info 写，没写或读不了就空着（只是显示用）
_info_file = _conf.get("proxy_info")
try:
    _info = json.loads(Path(_info_file).expanduser().read_text()) if _info_file else {}
except (OSError, ValueError):
    _info = {}


def _proxy_row(account: str) -> dict | None:
    port = _ports.get(base_account(account), DEFAULT_PROXY_PORT)
    if port is None:
        return None
    i = _info.get(str(port)) or {}
    return {"port": port, "tag": i.get("tag"), "region": i.get("region", "")}


PROXIES: dict[str, dict | None] = {a: _proxy_row(a) for a in ACCOUNTS}

# 机器在界面上的叫法、只准经别名启动、看板娘、电脑通路的对外地址（见开头的说明）
HOST_NAME: str = _conf.get("host_name") or "服务器"
ALIAS_ONLY: bool = _conf.get("alias_only") is True
XXLB: bool = _conf.get("xxlb") is True
PC_SETUP_URL: str | None = (_conf.get("pc_setup_url") or "").rstrip("/") or None

# GPT 号 → {codex_home, bridge_port, five_in_week, pts_per_five, week_only}（见开头的说明）；bridge_port 是这个号的翻译层在本机哪个端口
_bridge_port = int(_conf.get("bridge_port") or 18765)
GPT: dict[str, dict] = {a: {**g, "codex_home": str(Path(g.get("codex_home") or "~/.codex").expanduser()),
                            "bridge_port": int(g.get("bridge_port") or _bridge_port)}
                        for a, g in (_conf.get("gpt") or {}).items() if a in ACCOUNTS}
# 没有 5 小时限额、只有本周额度的 GPT 号
WEEK_ONLY: set[str] = {a for a, g in GPT.items() if g.get("week_only")}
for _a in WEEK_ONLY:
    if not GPT[_a].get("five_in_week"):
        raise ValueError(f"{_a} 没有 5 小时限额，要在 instance.json 里写 five_in_week（一个计价单位约合它本周的百分之几）")
GPT_MODELS = ("gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna",   # 网页上给 GPT 号列的模型；第一个是别名里的默认模型
              # 快速档：翻译层认 -fast 后缀，按优先处理发给 ChatGPT，单价见 points_rates.json
              "gpt-6-astra-fast", "gpt-6-sol-fast", "gpt-6.1-sol-fast", "gpt-6-luna-fast")

# 远端 GPT 号：翻译层是本机服务，直接连它，不靠 .bashrc 里的别名。
_bad_remote_gpt = [a for a in (_conf.get("remote_gpt") or []) if a not in GPT]
if _bad_remote_gpt:
    raise ValueError(f"remote_gpt 里有不是 GPT 号的账号：{'、'.join(_bad_remote_gpt)}")
REMOTE_GPT: set[str] = set(_conf.get("remote_gpt") or [])


def is_gpt_model(model: str | None) -> bool:
    return bool(model) and model.startswith("gpt-")


def five_in_week(account: str | None) -> float:
    """这个号 5 小时窗口的 1% 约合本周额度的百分之几：GPT 号各写各的，其余按实例的。"""
    return float((GPT.get(account or "") or {}).get("five_in_week") or FIVE_IN_WEEK)


def pts_per_five(account: str | None) -> float:
    """这个号 5 小时额度的 1% 合多少点。GPT 号按周额度同价：它本周的 1% 和这组 Claude 号本周的 1% 一样多点。
    没有 5 小时限额的 GPT 号（WEEK_ONLY）和别的 GPT 号同一套 token 单价，它的「5 小时 1%」就是 GPT_SCALE 这么多点。"""
    if account in GPT:
        if GPT[account].get("pts_per_five"):
            return float(GPT[account]["pts_per_five"])
        if account in WEEK_ONLY:
            return GPT_SCALE
        return PTS_PER_FIVE * five_in_week(account) / FIVE_IN_WEEK
    return PTS_PER_FIVE


# GPT 模型的单价表（points_rates.json）按「GPT 号 5 小时额度的 1%」写，乘这个数才是点（rates.load 里乘）。
# 单价表按模型不按号，各 GPT 号的换算不一样就没法都按周同价，直接报错停下，不能挑一个凑合。
# 价钱由有 5 小时窗口的 GPT 号定；只有本周额度的号跟着用同一个数
_gpt_scales = {round(pts_per_five(a), 6) for a in GPT if a not in WEEK_ONLY or GPT[a].get("pts_per_five")}
if len(_gpt_scales) > 1:
    raise ValueError(f"GPT 号的 5 小时 1% 合点数不一样（{sorted(_gpt_scales)}），单价表只有一份，先把单价表改成按号分开")
if WEEK_ONLY and not _gpt_scales:
    raise ValueError(f"{sorted(WEEK_ONLY)} 没有 5 小时限额，单价要跟着别的 GPT 号定，可这个实例里没有别的 GPT 号，也没写 pts_per_five")
GPT_SCALE: float = _gpt_scales.pop() if _gpt_scales else 1.0   # 没有 GPT 号就不会有 GPT 模型的调用


def bridge_port(account: str) -> int:
    """这个 GPT 号的翻译层在本机哪个端口。"""
    return GPT[account]["bridge_port"]


# 按 token 付费的 API 号（比如 DeepSeek）：api 写成
# {"ds1": {"vendor": "deepseek", "key_file": "~/.config/cc_viewer/ds1.key"}}。照样跑 Claude Code，只是请求经网络闸门转到这家的
# Anthropic 兼容接口（api_relay.py）。密钥只放在查看器自己这边（key_file 按查看器自己的家目录展开、只有它能读），由闸门转发时补上：
# CLI 手里只有占位的令牌：多人共用 RUN_AS 时，会话里的工具读得到 CLI 的环境，密钥放进去谁都能拿走。
# 没有 5 小时、本周额度，用多少扣多少钱：不开窗、不能预定、没有按额度的预测拦截，账号面板上写这家平台查到的余额（usage_probe.api_balance）。
# 单价按官方价写成元（points_rates.json，rates.load 换成点），收钱就是官方价，不乘 rates.PRICE；高峰、空闲两个价见 peak.py。
API: dict[str, dict] = {a: {**g, "key_file": str(Path(g["key_file"]).expanduser())}
                        for a, g in (_conf.get("api") or {}).items() if a in ACCOUNTS}
# 各家的号在网页上能选的模型，第一个是默认（启动时 ANTHROPIC_MODEL 设的，api_relay.launch_env）。
# DeepSeek 只接了 Flash；[1m] 让 Claude Code 按 1M 上下文算（官方接入说明就这样写），记录里回的模型名不带它
# 智谱只接 GLM-5.3：子代理、后台小模型也用它（不用 Flash），单价表只有一个模型要定
API_MODELS: dict[str, tuple[str, ...]] = {"deepseek": ("deepseek-flash[1m]",), "glm": ("glm-5.3[1m]",)}
# 中转站：Anthropic 兼容接口的转发平台，加一个新站只写配置、不改代码。号的配置里写
# {"vendor": "<站名，小写字母开头>", "relay": {"name": 界面上的平台名, "host": 主站域名, "prefix": 接口路径前缀（没有就不写）,
#  "models": [能选的模型，第一个是默认], "labels": {模型: 下拉里的名字}}, "key_file", "rates", 需要时再写 model_overrides、cache_ttl、cost_audit}。
# vendor 同时是这个号的「家」（account_family）：同一家的号之间才能换，不同站的号互不顶替，和订阅号也不混。
# 一个站可以接好几家的模型：Claude、Gemini、GPT 模型都认，下拉里一律写模型本来的名字，不写站内渠道（渠道名只在 model_overrides 里）
RELAY_MODEL_PREFIXES = ("claude-", "gemini-", "gpt-")
RELAY: dict[str, dict] = {}   # 中转站的号 → {name, host, prefix, models, labels}
for _a, _g in API.items():
    _v = _g.get("vendor")
    if not isinstance(_v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", _v) or _v in ("claude", "gpt"):
        raise ValueError(f"{_a} 的 vendor 写的是 {_v!r}：要写小写字母开头的站名（不能是 claude、gpt）")
    if _v in API_MODELS:
        if "relay" in _g:
            raise ValueError(f"{_a} 是 {_v}，不是中转站，不能写 relay")
        continue
    _r = {"prefix": "", "labels": {}, **(_g.get("relay") or {})}
    if not _r.get("name") or not _r.get("host"):
        raise ValueError(f"{_a} 的 vendor {_v!r} 不是内置的平台，要在 relay 里写 name 和 host（只认得 {sorted(API_MODELS)} 和中转站）")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", _r["host"]):
        raise ValueError(f"{_a} 的 relay.host 写的是 {_r['host']!r}，只写域名（不带 https:// 和路径）")
    if _r["prefix"] and not re.fullmatch(r"(/[A-Za-z0-9_.-]+)+", _r["prefix"]):
        raise ValueError(f"{_a} 的 relay.prefix 写的是 {_r['prefix']!r}，要以 / 开头、结尾不带 /")
    if not _r.get("models") or not all(isinstance(m, str) and m.startswith(RELAY_MODEL_PREFIXES) for m in _r["models"]):
        raise ValueError(f"{_a} 的 relay.models 要写至少一个模型，只认 {' 或 '.join(RELAY_MODEL_PREFIXES)} 开头的")
    # 没有单价的模型不许用（rates.unpriced），加号时漏写单价就在启动时说清楚，不等到发话才拦
    if _missing := [m for m in _r["models"] if m.split("[")[0] not in (_g.get("rates") or {})]:
        raise ValueError(f"{_a} 的 rates 里没有 {'、'.join(_missing)} 的单价")
    RELAY[_a] = {k: _r[k] for k in ("name", "host", "prefix", "models", "labels")}
# 同一站的第二把令牌（比如同一个中转站里不同的分组）：站内分组隔开模型目录，一把钥匙进不了两个组，只能一站两个号。
# present_as 把并进来的号藏到主号名下（选号列表、账号面板都不列），主号的 model_keys 把某些模型指到拿另一把钥匙的号上，
# 闸门按模型换钥匙（api_relay.model_body），计价、对账、统计都只认发起的那个号，用户看到的始终是一家。
PRESENT_AS: dict[str, str] = {a: g["present_as"] for a, g in API.items() if g.get("present_as")}
for _a, _p in PRESENT_AS.items():
    if _p not in API or _p in PRESENT_AS or API[_a]["vendor"] != API[_p]["vendor"]:
        raise ValueError(f"{_a} 的 present_as 指到 {_p}：要指同一家（vendor 相同）的另一个号，不能互相指或指自己")
for _a, _g in API.items():
    for _m, _h in (_g.get("model_keys") or {}).items():
        if PRESENT_AS.get(_h) != _a and PRESENT_AS.get(_a) != _h \
           or _m not in RELAY.get(_a, {}).get("models", []) or _m not in RELAY.get(_h, {}).get("models", []):
            raise ValueError(f"{_a} 的 model_keys 把 {_m} 指到 {_h}：{_h} 和 {_a} 要用 present_as 并成一家，两边的 relay.models 都要有这个模型")


def api_models(account: str) -> tuple[str, ...]:
    """这个 API 号在网页上能选的模型，第一个是默认：中转站按号的配置，DeepSeek、智谱按 API_MODELS。"""
    return tuple(RELAY[account]["models"]) if account in RELAY else API_MODELS[API[account]["vendor"]]


def model_family(model: str | None) -> str:
    """模型是哪一家的：gpt、deepseek、glm（智谱）、gemini，其余都算 claude。号只能用自家的模型（server.run_model）。
    API 号的家名就是 instance.json 里 vendor 写的那个（account_family）。Gemini 模型只经中转站用（中转站按号接了哪几个认，不看这里），
    单列一家是为了别的号不会把它当成 Claude 模型带上：从中转站换到订阅号时，选着的 Gemini 会退回订阅号的默认模型"""
    m = model or ""
    return ("gpt" if m.startswith("gpt-") else "deepseek" if m.startswith("deepseek-") else "glm" if m.startswith("glm-")
            else "gemini" if m.startswith("gemini-") else "claude")


def account_family(account: str | None) -> str:
    """号是哪一家的，和 model_family 同一套说法。"""
    return "gpt" if account in GPT else API[account]["vendor"] if account in API else "claude"


def is_api_model(model: str | None) -> bool:
    """是不是按 token 付费的 API 号的模型（单价表写的是元，见 rates.load）。"""
    return model_family(model) in API_MODELS


# 限时特惠：号 → [{from, until（unix 秒）, factor, label}]（见开头的说明）。一个号可以有好几段（配置里写一条或一串都认）：
# 账本按全部历史重算，给号加新的一段时不能把以前那段换掉，不然以前免掉的会被补扣
PROMO: dict[str, list[dict]] = {a: [{**p, "from": datetime.fromisoformat(p["from"]).timestamp(),
                                     "until": datetime.fromisoformat(p["until"]).timestamp(), "factor": float(p["factor"])}
                                    for p in (ps if isinstance(ps, list) else [ps])]
                                for a, ps in (_conf.get("promo") or {}).items() if a in ACCOUNTS}


# 自动特惠（instance.json 的 auto_promo）：Claude 限量号本周额度离重置不到 hours 小时，花费就只有 factor 那几成进账本，
# since 以前的调用不算；时段按额度历史算（reserve.promo_factor）。不写就没有
_ap = _conf.get("auto_promo")
AUTO_PROMO: dict | None = {**_ap, "since": datetime.fromisoformat(_ap["since"]).timestamp(), "hours": float(_ap.get("hours", 24)),
                           "factor": float(_ap["factor"]),
                           # 从这一刻起只给官方号（号主是查看器账号的号）；不写就是所有 Claude 限量号，和以前一样
                           "official_since": datetime.fromisoformat(_ap["official_since"]).timestamp()
                           if _ap.get("official_since") else None} if _ap else None
AUTO_ACCOUNTS: set[str] = {a for a in PAID if a in ACCOUNTS and a not in GPT and a not in API and a not in API_ENTRIES} if AUTO_PROMO else set()

# 不开放预定的号（instance.json 的 no_reserve）：打开预定面板、下单都由 reserve.plan 拦下
# API 号没有额度窗口，用多少扣多少，本来就没有可订的，不用另外写进 no_reserve
NO_RESERVE: set[str] = {a for a in (_conf.get("no_reserve") or []) if a in ACCOUNTS} | set(API)

# 号的到期时间（instance.json 的 expires）：号 → unix 秒。到期以后才开始的周和 5 小时窗口不能预定，预定面板里也不列（reserve.plan）。
# 建议的填法：Claude 号是官方订阅开通时间往后一个自然月，GPT 号是登录令牌里这一期订阅的到期时刻；还没验证过准不准。没写的号不限
EXPIRES: dict[str, float] = {a: datetime.fromisoformat(t).timestamp()
                             for a, t in (_conf.get("expires") or {}).items() if a in ACCOUNTS}


def week_end(account: str, resets_at: float) -> float:
    """这个号本周额度用到哪一刻为止：订阅在本周重置之前就到期，重置时刻没有意义，按到期时刻算。
    挑号排序、自动特惠、预定都按它；读数里的 resets_at 不改，过了它就当额度已重置的那些判断照旧看真的重置时刻。
    到期时间是估的（见上面 EXPIRES），没验证过，先这样用"""
    return min(resets_at, EXPIRES.get(account, resets_at))


# 号主（instance.json 的 owners，见开头的说明）：号 → (用户名, 从这个 unix 秒起算)；没写号主的号，花的钱不流向谁
OWNERS: dict[str, tuple[str, float]] = {a: (o["user"], datetime.fromisoformat(o["since"]).timestamp() if o.get("since") else 0.0)
                                        for a, o in (_conf.get("owners") or {}).items() if a in ACCOUNTS}


def owns(user: str | None, account: str) -> bool:
    """这个人是不是这个号的号主。号主用自己的号花的钱又流回自己（users.earnings），所以发话、下预定都不按余额拦
    （users.paid_why、server.can_pay）；号主还能关掉别人在这个号上的预定、撤销自己在这个号上的单。"""
    return user is not None and (OWNERS.get(base_account(account)) or (None,))[0] == user


def promo_factor(account: str | None, t: float | None) -> float:
    """t 这一刻在这个号上花的钱按原价的几成收：在特惠时段里是 factor（几段重叠取低的），别的都是 1。不知道时刻的按原价（取不到不能当成打折）。"""
    if t is None:
        return 1.0
    return min((p["factor"] for p in PROMO.get(account or "", ()) if p["from"] <= t < p["until"]), default=1.0)
