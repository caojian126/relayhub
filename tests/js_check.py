"""前端 index.html 的静态检查（不需要浏览器）。

做两件事：

  1. 把 <script> 抠出来用 esprima 真正解析一遍 —— 比数括号靠谱，
     能报出准确的行号（会换算回 index.html 里的行号）。
  2. 扫一遍所有内联 on*="..." 事件，确认里面点名的函数真的定义了。
     （这个坑踩过：加了按钮忘了写函数，只有点下去才发现。）

没装 esprima 就跳过并返回 0，不要让 e2e 挂在一个可选的检查上。
启用方式： pip install esprima

用法： python3 tests/js_check.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = ROOT / "app" / "static" / "index.html"


def main():
    try:
        import esprima
    except ImportError:
        print("  [SKIP] 没装 esprima，跳过 JS 语法检查（pip install esprima 可启用）")
        return 0

    src = HTML.read_text(encoding="utf-8")
    start = src.index("<script>") + len("<script>")
    end = src.index("</script>", start)
    js = src[start:end]
    base = src.count("\n", 0, start)  # 第一行 JS 在 index.html 里的行号

    try:
        esprima.parseScript(js)
    except Exception as e:
        print(f"  [FAIL] JS 语法错误：{e}")
        ln = getattr(e, "lineNumber", None)
        if ln:
            print(f"         -> index.html 第 {base + ln} 行")
            lines = js.splitlines()
            for i in range(max(0, ln - 3), min(len(lines), ln + 2)):
                mark = ">>" if i == ln - 1 else "  "
                print(f"         {mark} {base + i + 1}: {lines[i][:150]}")
        return 1
    print(f"  [PASS] JS 语法 OK（{len(js.splitlines())} 行）")

    # 内联事件里点名的函数必须存在
    called = set()
    for m in re.finditer(r'''on(?:click|change|input|dragstart)\s*=\s*["']([^"']+)["']''', src):
        # 排除 this.foo( / .toggle( 这类方法调用，只要顶层函数名
        for name in re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", m.group(1)):
            called.add(name)

    defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
    defined |= set(re.findall(
        r"(?:async\s+)?([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", js))
    defined |= set(re.findall(r"RENDER\.([A-Za-z_$][\w$]*)\s*=", js))
    builtin = {"alert", "confirm", "prompt", "parseInt", "parseFloat", "Number",
               "String", "JSON", "Math", "Boolean", "Array", "Object",
               "encodeURIComponent", "isNaN"}

    missing = sorted(n for n in called if n not in defined and n not in builtin)
    if missing:
        print(f"  [FAIL] 内联事件调用了未定义的函数：{missing}")
        return 1
    print(f"  [PASS] 内联事件引用的 {len(called)} 个函数都有定义")
    return 0


if __name__ == "__main__":
    sys.exit(main())
