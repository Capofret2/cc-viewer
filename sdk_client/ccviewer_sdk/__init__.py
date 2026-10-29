"""ccviewer_sdk：接口和官方 claude_agent_sdk 一样，请求经网页查看器去跑（挑号、额度拦截、计费、请求核验都在查看器那边）。

实验代码照官方写法写，换回官方包只改导入。类型、异常、选项都直接用官方包里的（这里原样转出），
只有 query 和 ClaudeSDKClient 换成经查看器的版本。查看器那边只核对选项、不改写：影响模型看到什么的都要实验代码自己写明，
不合规就报错说明原因。用法、环境变量、不支持的选项见 README「脚本接口」。
"""
from claude_agent_sdk import *  # noqa: F401,F403  官方的类型、异常、选项原样转出
from claude_agent_sdk import __all__ as _official_all

from ._client import ClaudeSDKClient, ViewerError, query  # noqa: F401

__all__ = [*_official_all, "ViewerError"]
