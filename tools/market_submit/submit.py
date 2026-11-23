"""把 hormone_system 当前版（读 manifest.json）提交到 Neo-MoFox 插件市场（注册插件 + 提交版本）。

版本号/资产名都从 manifest 取，避免脚本里写死成上一版（0.4.4 那次就差点重发旧号）。
token 只从 git credential manager 取，不打印、不落盘。
"""
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request

MARKET = "https://39.96.71.162"
HERE = os.path.dirname(os.path.abspath(__file__))
UP1 = os.path.dirname(HERE)  # 工作目录布局：UP1 = <ws>；仓库布局：UP1 = <repo>/tools
DOCS = os.path.join(UP1, "docsync") if os.path.isfile(os.path.join(UP1, "docsync", "README.md")) else os.path.dirname(UP1)
# 工作目录里插件源码在 hormone_prod/，仓库里是 hormone_system/
PKG = os.path.join(UP1, "hormone_prod") if os.path.isdir(os.path.join(UP1, "hormone_prod")) else os.path.join(DOCS, "hormone_system")
MANIFEST = json.load(open(os.path.join(PKG, "manifest.json"), encoding="utf-8"))
VER = str(MANIFEST["version"])
MIN_CORE = str(MANIFEST.get("min_core_version", "1.3.0-alpha.0"))


def find_asset(name):
    """.mfp 不在仓库里（.gitignore 掉了 dist/*.mfp），所以按几个常见位置找，或用 HORMONE_MFP 指定。"""
    cands = [os.environ.get("HORMONE_MFP"),
             os.path.join(UP1, name), os.path.join(os.path.dirname(UP1), "dist", name),
             os.path.join(HERE, "..", name), os.path.join(DOCS, name)]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise SystemExit(f"找不到发布产物 {name}——设 HORMONE_MFP=<路径> 再跑")


ASSET = find_asset(f"hormone_system-{VER}.mfp")
README = os.path.join(DOCS, "README.md")
REPO = "https://github.com/XIEYUANSHEN/mofox-neo-hormone-system"
PLUGIN_ID = "hormone_system"


def _fill(env=None):
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, timeout=100, env=env).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l).get("password")


def gh_token():
    # 正常调用优先（凭据在缓存里时几秒就返回）；不行再试静默模式。
    # 反过来会踩坑：never 模式失败时留下的 GCM 子进程会让第二次调用撞锁卡死（2026-10-27 实遇）。
    try:
        tok = _fill()
        if tok:
            return tok
    except subprocess.TimeoutExpired:
        pass
    env = dict(os.environ, GCM_INTERACTIVE="never", GIT_TERMINAL_PROMPT="0")
    try:
        tok = _fill(env)
    except subprocess.TimeoutExpired:
        tok = None
    if not tok:
        raise SystemExit("git credential fill 拿不到 token：请先在桌面终端跑 `git credential-manager github login`。")
    return tok


def post(path, body, token):
    req = urllib.request.Request(
        MARKET + path,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
            "user-agent": "hormone-system-publish",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:600]}


def brief(obj, keys):
    return {k: obj.get(k) for k in keys if isinstance(obj, dict) and k in obj}


def main():
    data = open(ASSET, "rb").read()
    sha = hashlib.sha256(data).hexdigest()
    readme = open(README, encoding="utf-8").read()
    token = gh_token()

    register = {
        "plugin_id": PLUGIN_ID,
        "display_name": "赛博内分泌（Hormone System）",
        "summary": "给 KFC 角色一套会累积、会被睡觉修复的身心状态：她自己定几点睡、会困会欠睡眠债、夜里做梦、醒来才决定记不记得。",
        "description": (
            "六激素状态机 + 睡眠节律。情绪按半衰期回归基线并调制注入 prompt（只给自然语言，不出现数字与激素名）；"
            "睡眠层全自动：睡前收尾、按安静时长判定是否被吵醒、整夜累计安静算睡眠时长、睡眠债与连续短睡、白天自己决定补觉、"
            "熟睡中段用双方近两天的发言做梦料生成梦、醒来按规则判定记不记得/值不值得记进小本本。"
            "v0.4.4 起梦带日期（哪天夜里做的写在注入里，措辞交给模型，不再一律当「昨晚」）；v0.4.5 起防梦同质化（她自己话的占比、"
            "关于梦的话降权、别再重复最近两夜的意象）；v0.4.6 起打分与整理的默认模型任务从主回复 actor 降到 utils（8B 级），"
            "打分自带 temperature 0.2，超时预算按候选链上的任务分配。另有只读命令 /睡眠状态 与 README 的「已知边界与隐私」一节。"
            "依赖 kokoro_flow_chatter（KFC）。"
        ),
        "homepage": REPO,
        "repository_url": REPO,
        "license": "AGPL-3.0-only",
        "categories": ["chat"],
        "tags": ["kfc", "emotion", "sleep", "dream", "memory", "roleplay", "睡眠", "梦境"],
        "maintainers": ["中生", "github:XIEYUANSHEN"],
        "plugin_dependencies": ["kokoro_flow_chatter"],
        "readme_markdown": readme,
    }
    version = {
        "version": VER,
        "release_tag": "v" + VER,
        "release_title": f"hormone_system {VER}",
        "release_url": f"{REPO}/releases/tag/v{VER}",
        "asset_name": f"hormone_system-{VER}.mfp",
        "asset_download_url": f"{REPO}/releases/download/v{VER}/hormone_system-{VER}.mfp",
        "checksum_sha256": sha,
        "file_size": len(data),
        "is_prerelease": False,
        "plugin_api_version": "1.0",
        "min_host_version": MIN_CORE,
        "max_host_version": None,
        "supported_platforms": ["all"],
        "readme_markdown": readme,
    }
    print(f"要提交：{version['asset_name']} {len(data)}B sha256 {sha[:16]}… ｜ readme {len(readme)} chars ｜ min_host {MIN_CORE}")

    st, res = post("/api/v1/plugins", register, token)
    print("register ->", st, json.dumps(brief(res, ["plugin_id", "status", "owner_login", "trust_level"]),
                                        ensure_ascii=False) if isinstance(res, dict) else str(res)[:300])
    if not isinstance(res, dict) or "error" in res:
        print("         ", json.dumps(res, ensure_ascii=False)[:600])
        if st != 409:
            return

    st2, res2 = post(f"/api/v1/plugins/{PLUGIN_ID}/versions", version, token)
    print("version  ->", st2, json.dumps(brief(res2, ["plugin_id", "version", "status", "asset_name", "published_at"]),
                                         ensure_ascii=False) if isinstance(res2, dict) else str(res2)[:300])
    if isinstance(res2, dict) and "error" in res2:
        print("         ", json.dumps(res2, ensure_ascii=False)[:600])


if __name__ == "__main__":
    main()
