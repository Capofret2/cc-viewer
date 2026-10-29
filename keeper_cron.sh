#!/bin/bash
# 把开窗服务（keeper.py，见它开头的说明）装进当前用户的 crontab：每分钟用 flock 拉一次，已经在跑就直接退出，
# 机器重启后一分钟内自己起来。
# 用法：bash keeper_cron.sh [数据目录]
#   数据目录就是代码目录时不给参数；查看器以别的系统用户运行、数据目录另放时，以那个用户的身份跑并给出数据目录：
#   bash keeper_cron.sh ~/.cc_viewer
# 用哪个 Python：环境变量 PYTHON 指定，不设就用 PATH 里的 python3（要装好 requirements.txt 里的依赖）。
# 装过的不会重复装（按 keeper.py 和数据目录认）。卸掉：crontab -e 删掉那一行，再按数据目录 keeper_state.json 里的 pid 收掉它。
set -e
CODE=$(cd "$(dirname "$0")" && pwd)
DATA=$(cd "${1:-$CODE}" && pwd)
PY=${PYTHON:-$(command -v python3)}
LINE="* * * * * cd $CODE && CC_VIEWER_DATA=$DATA flock -n $DATA/.keeper.lock $PY keeper.py >> $DATA/keeper.log 2>&1"
if crontab -l 2>/dev/null | grep -F "keeper.py" | grep -qF "CC_VIEWER_DATA=$DATA "; then
    echo "已经装过了：$(crontab -l | grep -F "CC_VIEWER_DATA=$DATA ")"
    exit 0
fi
(crontab -l 2>/dev/null || true; echo "$LINE") | crontab -
echo "装好了：$LINE"
