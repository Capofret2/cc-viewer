"""花费存盘的代码版本：只按真正决定存盘内容的那些代码算，改了别处不再让存盘整份作废。

以前花费存盘的版本是 spendmap、ctxmap、calls、peak 四个文件整份的 sha1：改一个不相干的函数、一行注释，重启后大会话的花费都要从头拆。
调用表存盘不用它，照旧按 calls.py、peak.py 整个文件算：那里读出的调用直接进账本，宁可多作废，不能有漏算的代码（见 calls.py「存盘」）。
花费存盘只给花费视角的环形图用，算错了不进账，所以可以窄一点。
现在从入口（读一个文件、存盘、读盘的那几个函数和类）出发，顺着代码里用到的名字往下找，把找到的这些算进版本：
- 函数和类：只跟进 modules 里列的模块（hostfs、memtrim 这些只管怎么读、不管读出什么，不跟），算的是编译出来的字节码、
  用到的名字和常量，不算源码文本：改注释、改文档字符串、挪行号都不变，逻辑一改就变；
- 用到的大写常量（FIELDS、HOLIDAYS、CALL 这些）算它的值；小写的模块变量是运行时的状态（缓存表、锁、存盘目录），不算；
- 用到 modules 里的模块（calls.CHUNK 这种写法），同一段代码里的其他名字也到那个模块里去找。
多算只会多作废一次（和以前一样慢），少算才会读出错的东西，所以拿不准的一律算进去。
找不到的只有靠字符串、getattr 这类间接用到的代码：真有这种依赖，把它加进调用的地方给的入口。
可以单独跑，列出算进去的都有哪些：python3 codever.py（以实例的运行用户、指着它的数据目录跑，CALL 这类常量跟着实例走）。
对照测试的做法：抄一份代码逐项改一处，看该变的变、不该变的不变。
"""
import hashlib
import sys
import types

IGNORE = {"VERSION"}   # 版本自己：存盘、读盘的代码会用到它


def _nested(code: types.CodeType):
    yield code
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            yield from _nested(c)


def _functions(obj):
    """函数本身，或者类里的每个方法（含静态方法、类方法、属性的取值函数）。"""
    if isinstance(obj, types.FunctionType):
        return [obj]
    out = []
    for v in vars(obj).values():
        f = v.__func__ if isinstance(v, (staticmethod, classmethod)) else v.fget if isinstance(v, property) else v
        if isinstance(f, types.FunctionType):
            out.append(f)
    return out


def _const(v, follow) -> str:
    """常量换成每次运行都一样的文字。集合按文字排序（字符串的散列每次启动都不一样，集合的顺序跟着变）；
    里面装着函数、类的，记名字并跟进去；带内存地址的说明换个进程就变，版本就永远对不上，直接报错让人看见。"""
    if v is None or v is Ellipsis or isinstance(v, (str, bytes, int, float, complex, bool)):
        return repr(v)
    if isinstance(v, (tuple, list)):
        return f"{type(v).__name__}[" + ",".join(_const(x, follow) for x in v) + "]"
    if isinstance(v, (set, frozenset)):
        return "set{" + ",".join(sorted(_const(x, follow) for x in v)) + "}"
    if isinstance(v, dict):
        return "dict{" + ",".join(f"{_const(k, follow)}:{_const(x, follow)}" for k, x in v.items()) + "}"
    if isinstance(v, types.CodeType):
        return "code"   # 嵌套的函数体另外按字节码算（_digest_function）
    if isinstance(v, (types.FunctionType, type)):
        follow(v)
        return f"<{v.__module__}.{v.__qualname__}>"
    if hasattr(v, "pattern") and hasattr(v, "flags"):   # 编译好的正则
        return f"re({v.pattern!r},{v.flags})"
    r = repr(v)
    if " at 0x" in r:
        raise ValueError(f"算存盘版本时碰到一个每次运行都不一样的常量：{r}")
    return r


def _digest_function(f: types.FunctionType, follow) -> str:
    h = hashlib.sha1()
    doc = f.__doc__
    for code in _nested(f.__code__):
        h.update(code.co_code)
        h.update(code.co_exceptiontable)
        h.update(repr(code.co_names).encode())
        h.update(repr((code.co_argcount, code.co_kwonlyargcount, code.co_flags & 0x0C)).encode())   # 参数个数、*args/**kwargs
        consts = code.co_consts
        if code is f.__code__ and doc is not None and consts and consts[0] == doc:
            consts = consts[1:]   # 文档字符串
        h.update(_const(tuple(consts), follow).encode())
    h.update(_const(f.__defaults__, follow).encode())
    h.update(_const(f.__kwdefaults__, follow).encode())
    return h.hexdigest()


def parts(entries, modules) -> dict[str, str]:
    """从 entries 出发找到的每一样 → 它的指纹（函数是字节码的 sha1，常量是值的文字）。"""
    mods = {m if isinstance(m, str) else m.__name__ for m in modules}
    out: dict[str, str] = {}
    todo = list(entries)

    def follow(obj):
        if obj.__module__ in mods:
            todo.append(obj)

    while todo:
        obj = todo.pop()
        key = f"{obj.__module__}.{obj.__qualname__}"
        if key in out:
            continue
        out[key] = ""   # 先占上：互相调用的不会转圈
        ns = vars(sys.modules[obj.__module__])
        digests = []
        if isinstance(obj, type):
            for base in obj.__mro__[1:]:
                follow(base)
            for k, v in vars(obj).items():   # 类里的常量（方法另外算）
                if not k.startswith("__") and not isinstance(v, (types.FunctionType, staticmethod, classmethod, property)):
                    digests.append(f"{k}={_const(v, follow)}")
        for f in _functions(obj):
            digests.append(f"{f.__name__}:{_digest_function(f, follow)}")
            for code in _nested(f.__code__):
                names = code.co_names
                spaces = [(obj.__module__, ns)]
                spaces += [(m.__name__, vars(m)) for n in names
                           if isinstance(m := ns.get(n), types.ModuleType) and m.__name__ in mods]
                for n in names:
                    for mod, space in spaces:
                        if n not in space or n in IGNORE:
                            continue
                        v = space[n]
                        if isinstance(v, (types.FunctionType, type)):
                            follow(v)
                        elif isinstance(v, types.ModuleType):
                            pass
                        elif n.upper() == n and any(ch.isalpha() for ch in n):
                            out.setdefault(f"{mod}:{n}", _const(v, follow))
        out[key] = ";".join(digests)
    return out


def version(entries, modules) -> str:
    h = hashlib.sha1()
    for k, v in sorted(parts(entries, modules).items()):
        h.update(f"{k}\0{v}\n".encode())
    return h.hexdigest()


if __name__ == "__main__":
    import spendmap
    entries, modules = spendmap.DISK_CODE
    found = parts(entries, modules)
    print(f"花费存盘的版本 {version(entries, modules)[:12]}，算进去 {len(found)} 样：")
    for k in sorted(found):
        print("  ", k)
