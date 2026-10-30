#!/bin/bash
# Pull the latest lessons from GitHub (origin/main) into this computer.
#   ./sync.sh          sync
#   ./sync.sh --dry    only show what would happen
# Rules (同題以 GitHub 最新版為準,但絕不刪東西):
#   - 本機未 commit 的課,若 GitHub 已有同標題的課 → 視為舊草稿,搬到 ../old_drafts_bak_<日期>/
#   - 本機獨有的課 → 保留,提醒推上 GitHub
#   - 本機改過已追蹤的檔案,或有未推的 commit → 停下來,不覆蓋
cd "$(dirname "$0")" || exit 1
DRY=0; [ "$1" = "--dry" ] && DRY=1
GH=~/.local/bin/gh
BAK="../old_drafts_bak_$(date +%Y%m%d)"

git fetch -q origin || { echo "✗ git fetch 失敗(網路?)"; exit 1; }

# 1) refuse to clobber local work
DIRTY=$(git status --porcelain --untracked-files=no)
if [ -n "$DIRTY" ]; then
  echo "✗ 本機有改過、還沒 commit 的檔案,先處理再同步:"; echo "$DIRTY"; exit 1
fi
AHEAD=$(git rev-list --count origin/main..HEAD)
BEHIND=$(git rev-list --count HEAD..origin/main)
if [ "$AHEAD" -gt 0 ]; then
  echo "✗ 本機有 $AHEAD 個 commit 還沒推上 GitHub:"; git log --oneline origin/main..HEAD
  echo "  先推:git -c credential.helper= -c credential.helper='!$GH auth git-credential' push origin main"
  exit 1
fi

# 2) untracked lessons: same title on GitHub → old draft (move out); unique → keep
PLAN=$(git show origin/main:lessons/index.json | python3 -c '
import json, os, re, subprocess, sys
norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
remote = {norm(x["title"]): x["id"] for x in json.load(sys.stdin)}
out = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--directory", "lessons/"],
                     capture_output=True, text=True).stdout.split()
for d in out:
    d = d.rstrip("/")
    f = os.path.join(d, "data.json")
    if not os.path.isfile(f):
        continue
    t = json.load(open(f)).get("title", "")
    lid = os.path.basename(d)
    tracked_remote = subprocess.run(["git", "cat-file", "-e", "origin/main:" + d + "/data.json"],
                                    capture_output=True).returncode == 0
    if tracked_remote or norm(t) in remote:
        print("OLD\t%s\t%s\t%s" % (lid, remote.get(norm(t), lid), t))
    else:
        print("KEEP\t%s\t-\t%s" % (lid, t))
')
while IFS=$'\t' read -r KIND LID RID TITLE; do
  [ -z "$KIND" ] && continue
  if [ "$KIND" = OLD ]; then
    echo "↪ 舊草稿(GitHub 有同題 $RID):$TITLE"
    if [ $DRY = 0 ]; then mkdir -p "$BAK" && mv "lessons/$LID" "$BAK/" && echo "   已搬到 $BAK/$LID"; fi
  else
    echo "★ 本機獨有、GitHub 沒有:$TITLE  (lessons/$LID) → 保留,記得推上 GitHub"
  fi
done <<< "$PLAN"

if [ "$BEHIND" -eq 0 ]; then echo "✓ 已是 GitHub 最新版($(git log --oneline -1))"; exit 0; fi

# 3) fast-forward to GitHub
echo "GitHub 有 $BEHIND 個新 commit:"; git log --oneline HEAD..origin/main
[ $DRY = 1 ] && { echo "(--dry:沒有實際更新)"; exit 0; }
OLD=$(git rev-parse HEAD)
git merge -q --ff-only origin/main || { echo "✗ 無法快轉更新,請找 Claude 看"; exit 1; }

# 4) report lesson changes
python3 - "$OLD" <<'EOF'
import json, subprocess, sys
old = {x["id"]: x for x in json.loads(subprocess.run(["git", "show", sys.argv[1] + ":lessons/index.json"],
                                                     capture_output=True, text=True).stdout or "[]")}
for x in json.load(open("lessons/index.json")):
    o = old.get(x["id"])
    if o is None:
        print("  + 新課:", x["title"])
    elif o.get("updated") != x.get("updated"):
        print("  ~ 更新:", x["title"])
EOF

# 5) server code changed → restart the local server on :8000
if git diff --name-only "$OLD" HEAD | grep -qE '^(server\.py|start\.sh)$'; then
  PID=$(lsof -tiTCP:8000 -sTCP:LISTEN)
  [ -n "$PID" ] && kill $PID && sleep 1
  nohup ./start.sh >> ../server_site.log 2>&1 < /dev/null & disown
  echo "  server.py 有更新 → 已重啟 http://127.0.0.1:8000"
fi
echo "✓ 同步完成($(git log --oneline -1))"
