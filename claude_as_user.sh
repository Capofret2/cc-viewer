#!/bin/bash
# 查看器和运行 Claude 的不是同一个系统用户时（instance.json 的 run_as，见 hostfs.py），
# SDK 和开窗服务把这个脚本当成 claude 启动（chat.cli_launch）：经 sudo 换成 CC_VIEWER_RUN_AS 那个用户，
# 再交给 claude_via_bashrc.sh 照常用别名启动，参数原样往后传。sudoers 只给查看器的系统用户以 run_as 身份运行命令的权限
# （sudoers 里配），-n：要密码就直接失败，不会卡在那里等人输。
# sudo 默认清空环境，SDK 交给 CLI 的环境变量（别名、入口标记、闸门通行证……）要点名留下（sudoers 里给了 SETENV）；
# 家目录、用户名、PATH 这些要换成那个用户自己的，查看器自己的数据目录也不带过去。
# 工作目录在 CC_VIEWER_CWD 里：查看器进不去那个用户的目录，SDK 不能先切过去，换了身份由 claude_via_bashrc.sh 切。
# exec 换成 sudo：SDK 收掉的就是 sudo，sudo 会把 SIGTERM 这类信号转给 CLI。前面加 setsid 让 sudo 自成一个会话：
# 发信号的和 sudo 同在一个进程组时，sudo 认为命令也收到了同一个信号，就不转发（实测 SIGTERM 给 sudo 以后它和命令都不退）。
# SDK 起的子进程不是进程组组长，setsid 不会再分叉，进程号不变。
# 测试时查看器和 Claude 是同一个用户（hostfs.py 的 CC_VIEWER_HOSTFS=helper），不经 sudo，直接交给 claude_via_bashrc.sh。
: "${CC_VIEWER_RUN_AS:?没指定以哪个系统用户运行 Claude（CC_VIEWER_RUN_AS）}"
here=$(cd "$(dirname "$0")" && pwd)
if [ "$CC_VIEWER_RUN_AS" = "$(id -un)" ]; then
    exec "$here/claude_via_bashrc.sh" "$@"
fi
keep=$(compgen -e | grep -vxE 'HOME|USER|LOGNAME|SHELL|MAIL|PATH|PWD|OLDPWD|SHLVL|_|TERM|LS_COLORS|CC_VIEWER_DATA|XDG_[A-Z_]*|SUDO_[A-Z_]*|DBUS_[A-Z_]*' | paste -sd, -)
exec setsid sudo -n -u "$CC_VIEWER_RUN_AS" --preserve-env="$keep" -- "$here/claude_via_bashrc.sh" "$@"
