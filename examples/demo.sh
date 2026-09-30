#!/usr/bin/env bash
# 端到端调用示例: 录入资料 -> 预演 -> 发布 -> 评审人读取匿名稿
# 用法: ./examples/demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"

echo "== 0. 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1. 会务方录入评审人 (编号/凭据/擅长主题/机构/容量/回避论文) =="
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R1", "credential": "r1-secret", "topics": ["AI", "ML"],
  "institution": "Inst-A", "capacity": 2, "avoid_papers": []
}'; echo
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R2", "credential": "r2-secret", "topics": ["DB"],
  "institution": "Inst-B", "capacity": 2, "avoid_papers": ["P2"]
}'; echo
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R3", "credential": "r3-secret", "topics": ["AI"],
  "institution": "Inst-C", "capacity": 2, "avoid_papers": []
}'; echo
curl -s -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R4", "credential": "r4-secret", "topics": ["ML"],
  "institution": "Univ-X", "capacity": 2, "avoid_papers": []
}'; echo

echo "== 2. 会务方录入论文 (编号/匿名稿/主题/作者机构) =="
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-X"]
}'; echo
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["ML"],
  "institutions": ["Univ-Y"]
}'; echo
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P3", "manuscript": "P3 匿名稿全文...", "topics": ["DB"],
  "institutions": ["Univ-Z"]
}'; echo

echo "== 3. 预演 (返回方案 + 资料修订号; R4 因与 P1 作者同机构被排除, R2 回避 P2) =="
DRY=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG")
echo "$DRY"
REV=$(echo "$DRY" | python3 -c 'import sys, json; print(json.load(sys.stdin)["revision"])')
echo "资料修订号: $REV"

echo "== 4. 资料变动后用旧修订号发布 -> 409 拒绝 =="
curl -s -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P4", "manuscript": "P4 匿名稿全文...", "topics": ["AI"],
  "institutions": ["Univ-W"]
}'; echo
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/assignment/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV}"

echo "== 5. 重新预演并用新修订号发布 =="
DRY=$(curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG")
REV=$(echo "$DRY" | python3 -c 'import sys, json; print(json.load(sys.stdin)["revision"])')
curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}"; echo

echo "== 6. 会务方查询已发布分配与排除原因 =="
curl -s "$BASE/assignment" -H "$ORG"; echo

echo "== 7. 评审人凭自身凭据读取已分配匿名稿 (仅本人名下) =="
curl -s "$BASE/reviewer/assignments" -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret'; echo

echo "== 8. 错误演示: 失效凭据 / 非法容量 / 重复编号 =="
curl -s -o /dev/null -w '失效凭据        -> HTTP %{http_code}\n' "$BASE/reviewer/assignments" \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: wrong'
curl -s -o /dev/null -w '非法容量        -> HTTP %{http_code}\n' -X POST "$BASE/reviewers" \
  -H "$ORG" -H 'Content-Type: application/json' -d '{
    "reviewer_id": "R9", "credential": "x", "topics": [], "institution": "I",
    "capacity": 0, "avoid_papers": []}'
curl -s -o /dev/null -w '重复编号        -> HTTP %{http_code}\n' -X POST "$BASE/papers" \
  -H "$ORG" -H 'Content-Type: application/json' -d '{
    "paper_id": "P1", "manuscript": "x", "topics": [], "institutions": []}'

echo "== 9. 评审人决定: R1 确认 P1 (有效决定推进资料修订号; 重复提交幂等) =="
curl -s -X POST "$BASE/reviewer/decisions" -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'; echo
curl -s -X POST "$BASE/reviewer/decisions" -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "confirm"}'; echo

echo "== 10. R2 提交回避 (非空原因); 回避后 R2 立即读不到 P1, 其他人不受影响 =="
curl -s -X POST "$BASE/reviewer/decisions" -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' \
  -H 'Content-Type: application/json' -d '{"paper_id": "P1", "decision": "decline", "reason": "与作者近期合作"}'; echo
curl -s "$BASE/reviewer/assignments" -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret'; echo

echo "== 11. 决定错误演示: 已回避再确认 -> 409; 未分配任务 -> 404; 空回避原因 -> 422 =="
curl -s -o /dev/null -w '已回避再确认    -> HTTP %{http_code}\n' -X POST "$BASE/reviewer/decisions" \
  -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret' -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -w '未分配任务      -> HTTP %{http_code}\n' -X POST "$BASE/reviewer/decisions" \
  -H 'X-Reviewer-Id: R3' -H 'X-Reviewer-Credential: r3-secret' -H 'Content-Type: application/json' \
  -d '{"paper_id": "P1", "decision": "confirm"}'
curl -s -o /dev/null -w '空回避原因      -> HTTP %{http_code}\n' -X POST "$BASE/reviewer/decisions" \
  -H 'X-Reviewer-Id: R3' -H 'X-Reviewer-Credential: r3-secret' -H 'Content-Type: application/json' \
  -d '{"paper_id": "P2", "decision": "decline", "reason": "  "}'

echo "== 12. 会务方核对: 发布方案不变, 决定状态可见 =="
curl -s "$BASE/assignment" -H "$ORG" | python3 -c 'import sys, json; d = json.load(sys.stdin); print(json.dumps({"plan": d["plan"], "decisions": d["decisions"]}, ensure_ascii=False))'

echo "== 13. 补位预演: 固定已确认关系 (P1<-R1), 回避成为硬回避; 不改变发布版 =="
RE_DRY=$(curl -s -X POST "$BASE/assignment/reassign/dry-run" -H "$ORG")
echo "$RE_DRY" | python3 -c 'import sys, json; d = json.load(sys.stdin); print(json.dumps({k: d[k] for k in ("feasible", "plan", "pinned", "revision", "published_version")}, ensure_ascii=False))'
REV2=$(echo "$RE_DRY" | python3 -c 'import sys, json; print(json.load(sys.stdin)["revision"])')
PUBV=$(echo "$RE_DRY" | python3 -c 'import sys, json; print(json.load(sys.stdin)["published_version"])')

echo "== 14. 补位发布须复核两种版本: 过期发布版序号 -> 409 =="
curl -s -o /dev/null -w '过期版本        -> HTTP %{http_code}\n' -X POST "$BASE/assignment/reassign/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV2, \"base_published_version\": 99}"

echo "== 15. 携带预演返回的 (资料修订号, 发布版序号) 补位发布 =="
curl -s -X POST "$BASE/assignment/reassign/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV2, \"base_published_version\": $PUBV}"; echo

echo "== 16. 补位发布后仅按新发布版授权取稿 (R2 不再持有 P1, R3 补上) =="
curl -s "$BASE/reviewer/assignments" -H 'X-Reviewer-Id: R2' -H 'X-Reviewer-Credential: r2-secret'; echo
curl -s "$BASE/reviewer/assignments" -H 'X-Reviewer-Id: R3' -H 'X-Reviewer-Credential: r3-secret'; echo
