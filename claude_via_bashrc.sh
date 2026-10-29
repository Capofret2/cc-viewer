#!/bin/bash
# 网页对话的 SDK 把这个脚本当成 claude 来启动。
# 这里起一个交互式 bash（这样才会读 ~/.bashrc，里面设的环境变量、别名都照样生效），
# 有和号同名的别名（CC_ALIAS）就展开它去运行 CLI，没有就直接运行 claude，SDK 给的参数原样往后传。
# 一路 exec 换掉进程：SDK 收掉的就是 CLI 本身，不会留下孤儿进程。
# setsid 先脱离终端：server 要是在终端后台跑（nohup ... &），交互式 bash 一碰终端就会被系统挂起，
# 挂起是按进程组算的，server 会跟着一起停住；在前台跑时，终端里按 Ctrl+C 也不会直接打到 CLI 身上，
# 由 server 自己收尾。SDK 起的子进程不是进程组组长，setsid 不会再分叉，进程号不变。
# +m 关掉作业控制。没有终端时 bash 会往标准错误打一两行提示，不影响标准输出上的数据流。
# CC_VIEWER_GATE 是查看器的网络闸门（gate.py）：.bashrc 读完之后才换成它，不然会被 .bashrc 里设的代理改回去。
# 没有闸门时（开窗服务）用 CC_VIEWER_PROXY：配了代理时这个号的代理，同样在 .bashrc 之后换上；没配（打开了 allow_direct）就不给，直连。
# 没有同名别名时直接运行 claude，配置目录由查看器经 CLAUDE_CONFIG_DIR 给（instance.json 的 accounts）。
# CC_VIEWER_CWD：查看器经 sudo 换了用户来拉起时（claude_as_user.sh），它自己进不去会话的工作目录，由这里换好身份以后再切。
if [ -n "${CC_VIEWER_CWD:-}" ]; then
    cd -- "$CC_VIEWER_CWD" || { echo "进不去工作目录：$CC_VIEWER_CWD" >&2; exit 1; }
    unset CC_VIEWER_CWD
fi
# ~/.local/bin 放到 PATH 最前面：经 sudo 拉起时 PATH 被重置成系统默认的那一串，.bashrc 又不加它（是 .profile 加的），
# 装在那里的 ssh、sshfs、scp、sftp、ccssh 包装（走 ssh 门卫）在会话里就全找不到，连登记过的机器也会落到系统的 ssh 上。
# 不经 sudo 时 PATH 里本来就有，不重复加
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) [ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH" ;;
esac
# 经 sudo 拉起时，这个进程可能已经自成一个进程组（是组长）：setsid 这时会分叉、父进程先退，SDK 就以为 CLI 退出了。
# 那种情况下也没有终端可脱离，就不再 setsid。SDK 直接拉起的（不经 sudo）不是组长，照旧 setsid
SETSID=setsid
[ "$(ps -o pgid= -p $$ | tr -d ' ')" = "$$" ] && SETSID=
exec $SETSID bash +m -i -c '
# 代理先换好再分路：远端号也要经闸门（远端分支要是在换代理之前就启动 CLI，请求和会话里工具的上网都会绕开闸门）
if [ -n "$CC_VIEWER_GATE" ]; then
    export http_proxy=$CC_VIEWER_GATE https_proxy=$CC_VIEWER_GATE HTTP_PROXY=$CC_VIEWER_GATE HTTPS_PROXY=$CC_VIEWER_GATE
    export all_proxy=$CC_VIEWER_GATE ALL_PROXY=$CC_VIEWER_GATE   # 原来是 socks5 直通代理，留着就有一条绕过闸门的路
elif [ -n "$CC_VIEWER_PROXY" ]; then
    # 没有闸门（开窗服务）时换成配好的代理（chat.launch_env 给的）；all_proxy 按 socks5 写，要求这个端口 http、socks5 都认
    export http_proxy=$CC_VIEWER_PROXY https_proxy=$CC_VIEWER_PROXY HTTP_PROXY=$CC_VIEWER_PROXY HTTPS_PROXY=$CC_VIEWER_PROXY
    export all_proxy=${CC_VIEWER_PROXY/#http:/socks5:} ALL_PROXY=${CC_VIEWER_PROXY/#http:/socks5:}
fi
# 远端回环和远端 GPT：没有 .bashrc 别名，接口地址和密钥由查看器经环境变量给，直接运行 CLI。
if [ -n "${CC_VIEWER_REMOTE:-}" ]; then
    exec claude "$@"
fi
cmd=${BASH_ALIASES[$CC_ALIAS]:-}
# ~/.bashrc 里没有同名的别名：直接运行 claude，配置目录由查看器经 CLAUDE_CONFIG_DIR 给；
# instance.json 写了 alias_only（CC_VIEWER_ALIAS_ONLY）就拒绝启动：别名里管着代理和账号，不能绕过去
if [ -z "$cmd" ]; then
    [ -z "${CC_VIEWER_ALIAS_ONLY:-}" ] || { echo "~/.bashrc 里没有 ${CC_ALIAS:-（没指定）} 这个别名，这个实例只准经别名启动" >&2; exit 127; }
    exec claude "$@"
fi
if [ -n "$CC_VIEWER_GATE" ]; then
    # GPT 号（别名把 ANTHROPIC_BASE_URL 指到本机的翻译层 127.0.0.1）：127.0.0.1 在 no_proxy 里，CLI 会直连、绕过闸门。
    # 换成闸门认得的名字 gpt-bridge.cc-viewer，去翻译层的请求就经闸门走，拦下时一样拦得住（见 gate.py）；
    # 不动 no_proxy，CLI 里工具去本机别的端口照旧直连
    cmd=${cmd//ANTHROPIC_BASE_URL=http:\/\/127.0.0.1:/ANTHROPIC_BASE_URL=http:\/\/gpt-bridge.cc-viewer:}
fi
eval "exec env $cmd \"\$@\""' bash "$@"
