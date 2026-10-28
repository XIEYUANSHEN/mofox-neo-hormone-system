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
import time
import urllib.error
import urllib.request

OWNER, REPO = "XIEYUANSHEN", "mofox-neo-hormone-system"
API = "https://api.github.com"
MARKET = "https://39.96.71.162"
MSG = "0.4.4: 梦带日期，状态/命令/注入不再一律说昨晚"

HERE = os.path.dirname(os.path.abspath(__file__))
UP1 = os.path.dirname(HERE)  # 工作目录布局：UP1 = <ws>；仓库布局：UP1 = <repo>/tools

if os.path.isfile(os.path.join(UP1, "docsync", "README.md")):
    # 工作目录：文档在 <ws>/docsync/，提交脚本在 <ws>/market_submit/
    DOCS = os.path.join(UP1, "docsync")
else:
    # 仓库里跑：文档就在仓库根（<repo>/tools/market_submit 往上两层）
    DOCS = os.path.dirname(UP1)

# 工作目录里的插件源码在 <ws>/hormone_prod/（仓库里是 hormone_system/）
PKG = os.path.join(UP1, "hormone_prod") if os.path.isdir(os.path.join(UP1, "hormone_prod")) else os.path.join(DOCS, "hormone_system")

FILES = {
    "README.md": os.path.join(DOCS, "README.md"),
    "SPEC.md": os.path.join(DOCS, "SPEC.md"),
    "test_hormone_v030.py": os.path.join(UP1 if os.path.isdir(os.path.join(UP1, "hormone_prod")) else DOCS, "test_hormone_v030.py"),
    "test_sim_v04_e2e.py": os.path.join(UP1 if os.path.isdir(os.path.join(UP1, "hormone_prod")) else DOCS, "test_sim_v04_e2e.py"),
    "tools/market_submit/submit.py": os.path.join(HERE, "submit.py"),
    "tools/market_submit/push_docs.py": os.path.abspath(__file__),
}
for _n in ("__init__.py", "config.py", "hormone.py", "manifest.json", "plugin.py", "prompt.py", "scorer.py", "state_store.py"):
    FILES[f"hormone_system/{_n}"] = os.path.join(PKG, _n)
for _p in FILES.values():
    if not os.path.isfile(_p):
        raise SystemExit(f"缺文件：{_p}（在工作目录含 docsync/ 的位置跑，或把脚本放到仓库 tools/market_submit/ 下）")


def _fill(env=None):
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n", capture_output=True,
                         text=True, timeout=100, env=env).stdout
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


def call(method, url, token, payload=None, accept="application/vnd.github+json", tries=3):
    """带重试：这台机到 api.github.com 会间歇挂起（60s 超时），重试一次通常就好。"""
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for i in range(tries):
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", "Bearer " + token)
        r.add_header("Accept", accept)
        r.add_header("User-Agent", "hormone-doc-sync")
        try:
            with urllib.request.urlopen(r, timeout=45) as resp:
                body = resp.read()
                return resp.status, (json.loads(body) if body and accept.endswith("json") else body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except Exception:  # noqa: BLE001
                return e.code, raw[:400]
        except Exception as e:  # 超时/连接重置：退避重试
            last = e
            if i + 1 < tries:
                print(f"     …网络抖动（{type(e).__name__}），{2 + i * 2}s 后重试")
                time.sleep(2 + i * 2)
    raise SystemExit(f"请求失败（已重试 {tries} 次）：{url} -> {last}")


def git_blob_sha(raw: bytes) -> str:
    """git 的 blob 哈希，可直接和 tree API 里的 sha 比对，不用逐个文件下载内容。"""
    h = hashlib.sha1()
    h.update(("blob %d" % len(raw)).encode("ascii") + bytes([0]))
    h.update(raw)
    return h.hexdigest()


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
    st, tr = call("GET", f"{API}/repos/{OWNER}/{REPO}/git/trees/main?recursive=1", token)
    if st != 200:
        raise SystemExit(f"读 main 的树失败 {st}: {str(tr)[:200]}")
    remote = {x["path"]: x.get("sha", "") for x in tr.get("tree", []) if x.get("type") == "blob"}
    changed = []
    for repo_path, raw in locals_.items():
        want = git_blob_sha(raw)
        have = remote.get(repo_path, "")
        same = have == want
        print(f"{repo_path:38} {len(raw):>6}B  blob {want[:12]}  仓库 {'一致' if same else ('缺' if not have else have[:12])}")
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
        # 实测：只传 readme_markdown 最稳（带全量字段反而偶发 500）；500 时重试一次，最终以回读为准
        for attempt in (1, 2):
            st, res = call("PUT", f"{MARKET}/api/v1/plugins/hormone_system", token,
                           {"readme_markdown": md}, accept="application/json")
            ok = isinstance(res, dict) and res.get("plugin_id", "hormone_system") == "hormone_system"
            if ok:
                print("market readme -> PUT 200 已刷新")
                break
            print(f"market readme -> PUT {st}{'，重试一次' if attempt == 1 else ''}")
            time.sleep(5)
        st, rd = call("GET", f"{MARKET}/api/v1/plugins/hormone_system/readme", token)
        html = (rd or {}).get("html", "") if isinstance(rd, dict) else ""
        fresh = "不扣 token" in html and "0.4.4.mfp" in html
        print(f"回读市场页 readme：{len(html)} chars｜" + ("已是新版 ✓" if fresh else "仍是旧版，需要人工看一眼"))


if __name__ == "__main__":
    main()
