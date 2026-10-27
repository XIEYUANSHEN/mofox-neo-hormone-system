"""把文档与提交脚本推到 GitHub（data API），并刷新插件市场页的 README。

推的四样：README.md、SPEC.md、tools/market_submit/submit.py、tools/market_submit/push_docs.py（自己）。
token 只从 git credential manager 静默取（GCM_INTERACTIVE=never，取不到就快速失败，绝不卡住等窗口）；
不打印、不落盘。默认 dry-run，加 --apply 才真写。

    python tools/market_submit/push_docs.py          # 看差异
    python tools/market_submit/push_docs.py --apply  # 推 GitHub + 刷市场 readme
"""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request

OWNER, REPO = "XIEYUANSHEN", "mofox-neo-hormone-system"
API = "https://api.github.com"
MARKET = "https://39.96.71.162"
MSG = "README/SPEC：测试其实不调模型，补 0.4.3 与市场提交链路；提交脚本进仓库"

HERE = os.path.dirname(os.path.abspath(__file__))
UP1 = os.path.dirname(HERE)  # 工作目录布局：UP1 = <ws>；仓库布局：UP1 = <repo>/tools

if os.path.isfile(os.path.join(UP1, "docsync", "README.md")):
    # 工作目录：文档在 <ws>/docsync/，提交脚本在 <ws>/market_submit/
    DOCS = os.path.join(UP1, "docsync")
else:
    # 仓库里跑：文档就在仓库根（<repo>/tools/market_submit 往上两层）
    DOCS = os.path.dirname(UP1)

FILES = {
    "README.md": os.path.join(DOCS, "README.md"),
    "SPEC.md": os.path.join(DOCS, "SPEC.md"),
    "tools/market_submit/submit.py": os.path.join(HERE, "submit.py"),
    "tools/market_submit/push_docs.py": os.path.abspath(__file__),
}
for _p in FILES.values():
    if not os.path.isfile(_p):
        raise SystemExit(f"缺文件：{_p}（在工作目录含 docsync/ 的位置跑，或把脚本放到仓库 tools/market_submit/ 下）")


def _fill(env=None):
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n", capture_output=True,
                         text=True, timeout=35, env=env).stdout
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l).get("password")


def gh_token():
    # 先走正常调用（实测凭据在缓存里时 5 秒就返回）；不行再试静默模式。
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
        raise SystemExit("GCM 取不到 GitHub token——请在桌面终端跑一次 `git credential-manager github login` 后重试。")
    return tok


def call(method, url, token, payload=None, accept="application/vnd.github+json"):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", "Bearer " + token)
    r.add_header("Accept", accept)
    r.add_header("User-Agent", "hormone-doc-sync")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body and accept.endswith("json") else body.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, raw[:400]


def sha16(b):
    return hashlib.sha256(b).hexdigest()[:12] + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真推送 + 真刷市场")
    ap.add_argument("--local-only", action="store_true", help="只看本地文件（不需要 token，不联网）")
    args = ap.parse_args()

    locals_ = {}
    for repo_path, local in FILES.items():
        raw = open(local, "rb").read()
        assert b"\r\n" not in raw, f"{local} 里混进 CRLF，先归一化成 LF"
        locals_[repo_path] = raw
        print(f"{repo_path:38} {len(raw):>6}B  sha256 {sha16(raw)}  LF ok")
    if args.local_only:
        print("（--local-only：没取 token、没联网）")
        return
    token = gh_token()

    st, ref = call("GET", f"{API}/repos/{OWNER}/{REPO}/git/ref/heads/main", token)
    if st != 200:
        raise SystemExit(f"读 main ref 失败 {st}: {ref}")
    base_sha = ref["object"]["sha"]

    changed = []
    for repo_path, raw in locals_.items():
        st, cur = call("GET",
                       f"{API}/repos/{OWNER}/{REPO}/contents/{repo_path}?ref=main",
                       token, accept="application/vnd.github.raw")
        onrepo = cur.encode("utf-8") if isinstance(cur, str) else b""
        same = st == 200 and onrepo == raw
        print(f"{repo_path:38} 仓库 {len(onrepo):>6}B {sha16(onrepo) if onrepo else '(无)':<16} "
              f"本地 {len(raw):>6}B {sha16(raw)}  {'一致' if same else '要改'}")
        if not same:
            changed.append((repo_path, raw))

    if not changed:
        print("GitHub 上已是最新。")
    elif not args.apply:
        print(f"dry-run：{len(changed)} 个文件待推，加 --apply 才动。")
    else:
        tree = []
        for repo_path, raw in changed:
            st, blob = call("POST", f"{API}/repos/{OWNER}/{REPO}/git/blobs", token,
                            {"content": base64.b64encode(raw).decode(), "encoding": "base64"})
            tree.append({"path": repo_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        st, tr = call("POST", f"{API}/repos/{OWNER}/{REPO}/git/trees", token,
                      {"base_tree": base_sha, "tree": tree})
        st, com = call("POST", f"{API}/repos/{OWNER}/{REPO}/git/commits", token,
                       {"message": MSG, "tree": tr["sha"], "parents": [base_sha]})
        st, upd = call("PATCH", f"{API}/repos/{OWNER}/{REPO}/git/refs/heads/main", token,
                       {"sha": com["sha"]})
        print("push ->", st, com.get("sha"), "main 已更新" if st == 205 else upd)

    if args.apply:
        md = locals_["README.md"].decode("utf-8")
        # 坑：PUT 只传 readme_markdown 会让服务端 500，必须带完整字段（先读回当前记录再合并）
        st, cur = call("GET", f"{MARKET}/api/v1/plugins/{'hormone_system'}", token, accept="application/json")
        cur = cur if isinstance(cur, dict) else {}
        body = {k: cur.get(k) for k in ("display_name", "summary", "description", "homepage",
                                        "repository_url", "license", "categories", "tags", "maintainers")}
        body["plugin_dependencies"] = cur.get("plugin_dependencies") or ["kokoro_flow_chatter"]
        body["readme_markdown"] = md
        st, res = call("PUT", f"{MARKET}/api/v1/plugins/hormone_system", token, body, accept="application/json")
        ok = isinstance(res, dict) and res.get("plugin_id", "hormone_system") == "hormone_system"
        print("market readme ->", st, "已刷新" if ok else json.dumps(res, ensure_ascii=False)[:300])
        st, rd = call("GET", f"{MARKET}/api/v1/plugins/hormone_system/readme", token)
        html = (rd or {}).get("html", "") if isinstance(rd, dict) else ""
        print("回读市场页 readme：", st, len(html), "chars |",
              "已是新版" if "不扣 token" in html and "0.4.3.mfp" in html else "仍是旧版/待审核")
    elif changed:
        print("dry-run：市场页 README 也没刷，加 --apply。")


if __name__ == "__main__":
    main()
