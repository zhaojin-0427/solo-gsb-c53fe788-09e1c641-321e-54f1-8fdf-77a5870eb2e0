#!/usr/bin/env bash
# End-to-end example against a locally running API (default: http://localhost:8000).
#
#   bash examples/quickstart.sh
#
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
j() { python3 -m json.tool; }

echo "== 1. health =="
curl -fsS "$BASE/health"; echo

echo "== 2. supported units =="
curl -fsS "$BASE/units" | j; echo

echo "== 3. register immutable ingredients =="
curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "almond_flour",
  "name": "Almond Flour",
  "allergens": [{"allergen": "tree_nuts", "concentration": 100, "unit": "%"}],
  "cross_contact": ["peanut"]
}' | j

curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "wheat_flour",
  "name": "Wheat Flour",
  "allergens": [{"allergen": "gluten", "concentration": 10, "unit": "%"}]
}' | j

curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' -d '{
  "code": "sugar", "name": "Sugar"
}' | j

echo "== 4. register and publish EU threshold rules =="
for body in \
  '{"code":"eu-gluten-declare","jurisdiction":"EU","allergen":"gluten","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"}' \
  '{"code":"eu-gluten-prohibit-20ppm","jurisdiction":"EU","allergen":"gluten","threshold":20,"unit":"ppm","action":"prohibit","applies_to":"contains"}' \
  '{"code":"eu-tree-nuts-declare","jurisdiction":"EU","allergen":"tree_nuts","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"}' \
  '{"code":"eu-peanut-may-contain","jurisdiction":"EU","allergen":"peanut","threshold":0,"unit":"ppm","action":"warning","applies_to":"cross_contact"}'
do
  curl -fsS -X POST "$BASE/rules" -H 'Content-Type: application/json' -d "$body" | j
done
for code in eu-gluten-declare eu-gluten-prohibit-20ppm eu-tree-nuts-declare eu-peanut-may-contain; do
  curl -fsS -X POST "$BASE/rules/$code/publish" | j
done

echo "== 4b. traceable rule set: draft (ppm-normalized) -> conflicts -> immutable publish =="
curl -fsS -X POST "$BASE/rule-sets" -H 'Content-Type: application/json' \
  -d '{"code":"eu-2026","jurisdiction":"EU","name":"EU Allergens 2026"}' | j

# thresholds use mixed units: 20000 ppb / 0.002% are both normalized to 20 ppm
curl -fsS -X PUT "$BASE/rule-sets/eu-2026" -H 'Content-Type: application/json' -d '{
  "base_version": 0,
  "rules": [
    {"code":"gluten-declare-0","allergen":"gluten","threshold":0,"unit":"ppm",
     "action":"declare","applies_to":"contains"},
    {"code":"gluten-warning-0","allergen":"gluten","threshold":0,"unit":"ppm",
     "action":"warning","applies_to":"contains"},
    {"code":"gluten-prohibit-20ppm","allergen":"gluten","threshold":20000,"unit":"ppb",
     "action":"prohibit","applies_to":"contains"},
    {"allergen":"tree_nuts","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
    {"allergen":"peanut","threshold":0,"unit":"ppm","action":"warning","applies_to":"cross_contact"}
  ]
}' | j

echo "-- stale base_version -> 409 VERSION_CONFLICT with current_version (no write) --"
curl -sS -o /tmp/rs_conflict.json -w 'status=%{http_code}\n' \
  -X PUT "$BASE/rule-sets/eu-2026" -H 'Content-Type: application/json' \
  -d '{"base_version":0,"rules":[]}' || true
cat /tmp/rs_conflict.json | j

echo "-- publish v1 (declare+warning coexist; 20 ppm/0.002% are different allergens) --"
curl -fsS -X POST "$BASE/rule-sets/eu-2026/publish" | j
echo "-- republishing the same draft is idempotent (same version/digest) --"
curl -fsS -X POST "$BASE/rule-sets/eu-2026/publish" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("version:", d["version"], "digest:", d["summary"]["digest"][:16])'

echo "-- overlapping prohibit+declare at the same ppm -> publish rejected with details --"
curl -fsS -X POST "$BASE/rule-sets" -H 'Content-Type: application/json' \
  -d '{"code":"broken-set","jurisdiction":"EU"}' >/dev/null
curl -fsS -X PUT "$BASE/rule-sets/broken-set" -H 'Content-Type: application/json' -d '{
  "base_version": 0,
  "rules": [
    {"code":"p","allergen":"gluten","threshold":20,"unit":"ppm","action":"prohibit","applies_to":"contains"},
    {"code":"d","allergen":"gluten","threshold":0.002,"unit":"%","action":"declare","applies_to":"contains"}
  ]
}' >/dev/null
curl -sS -X POST "$BASE/rule-sets/broken-set/publish" | j || true

echo "== 5. versioned nested recipe (cake v1 uses 400 g filling v1 + 1 kg wheat flour) =="
curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"code":"filling","name":"Almond Filling"}' | j
curl -fsS -X POST "$BASE/recipes/filling/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "items": [
    {"kind":"ingredient","code":"almond_flour","quantity":100,"unit":"g"},
    {"kind":"ingredient","code":"sugar","quantity":100,"unit":"g"}
  ]
}' | j
curl -fsS -X POST "$BASE/recipes/filling/versions/1/publish" | j

curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"code":"cake","name":"Almond Cake"}' | j
curl -fsS -X POST "$BASE/recipes/cake/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "items": [
    {"kind":"recipe","code":"filling","quantity":400,"unit":"g"},
    {"kind":"ingredient","code":"wheat_flour","quantity":1,"unit":"kg"}
  ]
}' | j
curl -fsS -X POST "$BASE/recipes/cake/versions/1/publish" | j

echo "== 6. evaluate (first call -> 201 Created) =="
curl -fsS -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cake-eu-v1-20260923' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU"}' | j

echo "== 7. replay with the same key -> 200 OK, replayed=true =="
curl -fsS -o /dev/null -w 'status=%{http_code}\n' -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cake-eu-v1-20260923' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU"}'

echo "== 7b. evaluate bound to the immutable rule-set v1 snapshot -> fixed replay =="
curl -fsS -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: cake-eu-rs1-20260929' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU",
       "rule_set_code":"eu-2026","rule_set_version":1}' | j

echo "-- binding a draft (unpublished) version -> 409 RULE_SET_NOT_PUBLISHED --"
curl -fsS -X PUT "$BASE/rule-sets/eu-2026" -H 'Content-Type: application/json' -d '{
  "base_version": 1,
  "rules": [
    {"code":"gluten-declare-0","allergen":"gluten","threshold":0,"unit":"ppm",
     "action":"declare","applies_to":"contains"}
  ]
}' >/dev/null
curl -sS -o /tmp/unpub.json -w 'status=%{http_code}\n' -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: bad-binding' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU",
       "rule_set_code":"eu-2026","rule_set_version":2}' || true
cat /tmp/unpub.json | j

echo "-- query by rule-set version: summary + recipe version + stored result, same snapshot --"
curl -fsS "$BASE/rule-sets/eu-2026/versions/1/evaluations" | j

echo "== 8. incomplete input -> minimal missing set (422) =="
curl -fsS -X POST "$BASE/ingredients" -H 'Content-Type: application/json' \
  -d '{"code":"sugar2","name":"Sugar 2"}' >/dev/null
curl -fsS -X POST "$BASE/recipes" -H 'Content-Type: application/json' \
  -d '{"code":"broken","name":"Broken"}' >/dev/null
curl -fsS -X POST "$BASE/recipes/broken/versions" -H 'Content-Type: application/json' -d '{
  "version": 1,
  "items": [
    {"kind":"ingredient","code":"mystery","quantity":10,"unit":"g"},
    {"kind":"recipe","code":"nonexistent","version":7,"quantity":10,"unit":"g"}
  ]
}' >/dev/null
curl -fsS -X POST "$BASE/recipes/broken/versions/1/publish" >/dev/null
curl -sS -X POST "$BASE/evaluations" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: broken-1' \
  -d '{"recipe_code":"broken","recipe_version":1,"jurisdiction":"EU"}' | j || true
