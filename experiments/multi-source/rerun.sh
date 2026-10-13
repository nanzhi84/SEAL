#!/usr/bin/env bash
# SEAL 多来源闭环演示：对 config/*.yaml 每个 Source 依次执行
#   collect → recheck(--enqueue) → worker(--once) → COPY 导出
# 全部来源共用一个独立数据库与一个私有归档；每轮工件冻结进 artifacts/<运行名>/。
# 用法：bash experiments/multi-source/rerun.sh [运行名]
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
EXP="$ROOT/experiments/multi-source"
RUN="${1:-multi-$(date +%Y%m%d-%H%M%S)}"
DB="seal_$(printf '%s' "$RUN" | tr -c 'a-z0-9' '_' | sed 's/_*$//' | tr -s '_')"
WORK="$EXP/work/$RUN"
ART="$EXP/artifacts/$RUN"

umask 077
mkdir -p "$WORK/archive" "$ART"
cd "$ROOT"
export SEAL_DATABASE_URL="postgresql:///$DB"
export SEAL_ARCHIVE="$WORK/archive"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
fail() { echo "!! $1，回执保留在 $ART"; exit 1; }

step "环境：数据库 $DB / 归档 $WORK/archive"
createdb "$DB" || exit 1
cp "$EXP"/config/*.yaml "$ART/"

step "seal init"
uv run --frozen seal init >"$ART/init.json" 2>"$ART/init.stderr.log" || fail "init 失败"

for YAML in "$EXP"/config/*.yaml; do
  SID=$(basename "$YAML" .yaml)
  step "Source ${SID}：collect"
  uv run --frozen seal collect "$YAML" >"$ART/${SID}-collect.json" 2>"$ART/${SID}-collect.stderr.log"
  python3 -c "
import json,sys
r=json.load(open('$ART/${SID}-collect.json'))
print(json.dumps({k:r.get(k) for k in ('run_id','mode','status','errors')}, ensure_ascii=False))
print(' ', (r.get('report') or {}).get('resource_counts'), (r.get('report') or {}).get('document_counts'))
sys.exit(0 if r.get('status')=='complete' else 1)" || fail "${SID} collect 未通过"

  step "Source ${SID}：recheck --enqueue → worker"
  uv run --frozen seal recheck "$YAML" --enqueue >"$ART/${SID}-recheck-enqueue.json" 2>"$ART/${SID}-recheck-enqueue.stderr.log"
  uv run --frozen seal worker --once >"$ART/${SID}-worker.json" 2>"$ART/${SID}-worker.stderr.log" || fail "${SID} worker 失败"
  RID=$(python3 -c "import json; print(json.load(open('$ART/${SID}-recheck-enqueue.json'))['run_id'])")
  psql "$DB" -c "SELECT r.status, count(e.*) FILTER (WHERE e.change_kind='unchanged') AS unchanged,
         count(e.*) FILTER (WHERE e.change_kind='updated') AS updated
  FROM seal.run r LEFT JOIN seal.document_emission e ON e.run_id=r.id
  WHERE r.id='$RID' GROUP BY r.status;"

  step "Source ${SID}：COPY 导出"
  psql "$DB" -c "\COPY (SELECT d.id, d.url, v.id AS version_id, v.content FROM seal.document d JOIN seal.document_version v ON v.id = d.latest_document_version WHERE d.source_id='${SID}' ORDER BY d.url) TO '$ART/${SID}-documents.csv' CSV HEADER"
done

step "汇总：各 Source 文档与观测"
psql "$DB" -c "SELECT d.source_id, count(*) AS documents FROM seal.document d GROUP BY 1 ORDER BY 1;"
psql "$DB" -c "SELECT o.origin, count(*) FROM seal.fetch_observation o GROUP BY 1 ORDER BY 1;"
psql "$DB" -c "SELECT mode, status, count(*) FROM seal.run GROUP BY 1,2 ORDER BY 1,2;"

cat >"$ART/environment.json" <<EOF
{
  "run_name": "$RUN",
  "date": "$(date +%F)",
  "python": "$(uv run --frozen python --version | awk '{print $2}')",
  "postgresql": "$(psql --version | awk '{print $3}')",
  "git_commit": "$(git rev-parse HEAD)",
  "git_dirty": $(git diff --quiet && git diff --cached --quiet && echo false || echo true),
  "database": "$DB",
  "archive": "experiments/multi-source/work/$RUN/archive"
}
EOF

step "完成"
echo "数据库：$DB    归档：$WORK/archive    工件：$ART"
