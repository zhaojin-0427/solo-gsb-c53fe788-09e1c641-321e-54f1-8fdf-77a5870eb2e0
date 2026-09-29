"""Tests for traceable, per-jurisdiction rule sets: drafts, optimistic
versions, ppm normalization, overlap conflicts, immutable published snapshots,
bound evaluations and fixed replay."""

from tests.helpers import build_cake_scenario, evaluate, make_ingredient, make_recipe


GLUTEN_RULES_PPM = [
    {"allergen": "gluten", "threshold": 0, "unit": "ppm",
     "action": "declare", "applies_to": "contains"},
    {"code": "gluten-prohibit-20", "allergen": "gluten", "threshold": 20, "unit": "ppm",
     "action": "prohibit", "applies_to": "contains"},
    {"allergen": "tree_nuts", "threshold": 0, "unit": "ppm",
     "action": "declare", "applies_to": "contains"},
    {"allergen": "peanut", "threshold": 0, "unit": "ppm",
     "action": "warning", "applies_to": "cross_contact"},
]


def make_rule_set(client, code="eu-2026", jurisdiction="EU", rules=None, name=None):
    resp = client.post(
        "/rule-sets",
        json={"code": code, "jurisdiction": jurisdiction, "name": name},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["current_version"] == 0
    assert data["rules"] == []
    if rules is None:
        rules = GLUTEN_RULES_PPM
    resp = client.put(f"/rule-sets/{code}", json={"base_version": 0, "rules": rules})
    assert resp.status_code == 200, resp.text
    return resp.json()


def publish_rule_set(client, code="eu-2026"):
    resp = client.post(f"/rule-sets/{code}/publish")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- draft/version


def test_create_empty_draft_starts_at_base_version_zero(client):
    resp = client.post("/rule-sets", json={"code": "rs", "jurisdiction": "eu"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "rs"
    assert body["jurisdiction"] == "EU"
    assert body["current_version"] == 0
    assert body["status"] == "draft"

    # duplicate code rejected
    assert client.post("/rule-sets", json={"code": "rs", "jurisdiction": "EU"}).status_code == 409
    assert client.get("/rule-sets/missing").status_code == 404


def test_submit_normalizes_thresholds_to_ppm_and_autocodes(client):
    draft = make_rule_set(
        client,
        rules=[
            {"allergen": "gluten", "threshold": 20000, "unit": "ppb",
             "action": "declare", "applies_to": "contains"},
            {"allergen": "gluten", "threshold": 0.002, "unit": "%",
             "action": "warning", "applies_to": "contains"},
            {"allergen": "milk", "threshold": 1, "unit": "g/kg",
             "action": "declare", "applies_to": "contains"},
        ],
    )
    assert draft["current_version"] == 1
    by_allergen = {r["allergen"]: r for r in draft["rules"]}
    # 20000 ppb == 20 ppm == 0.002 % (boundary equality after normalization)
    assert by_allergen["gluten"]["threshold_ppm"] == 20.0
    # explicit codes missing -> deterministic auto codes
    assert {r["code"] for r in draft["rules"]} == {"auto-001", "auto-002", "auto-003"}
    # g/kg -> ppm
    assert by_allergen["milk"]["threshold_ppm"] == 1000.0


def test_submit_version_increments_but_stale_base_version_conflicts(client):
    make_rule_set(client)  # v1 at base 0

    # second submit based on v1 -> v2
    resp = client.put(
        "/rule-sets/eu-2026",
        json={"base_version": 1, "rules": GLUTEN_RULES_PPM},
    )
    assert resp.status_code == 200
    assert resp.json()["current_version"] == 2

    # stale base_version (0 or 1) -> 409 with current_version, no write
    stale = client.put(
        "/rule-sets/eu-2026",
        json={"base_version": 0, "rules": []},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["code"] == "VERSION_CONFLICT"
    assert detail["current_version"] == 2
    # nothing was written: rules still present at v2
    assert client.get("/rule-sets/eu-2026").json()["current_version"] == 2


def test_duplicate_rule_codes_within_submission_rejected(client):
    client.post("/rule-sets", json={"code": "rs", "jurisdiction": "EU"})
    resp = client.put(
        "/rule-sets/rs",
        json={
            "base_version": 0,
            "rules": [
                {"code": "same", "allergen": "gluten", "threshold": 1, "unit": "ppm",
                 "action": "declare", "applies_to": "contains"},
                {"code": "same", "allergen": "milk", "threshold": 1, "unit": "ppm",
                 "action": "declare", "applies_to": "contains"},
            ],
        },
    )
    assert resp.status_code == 422
    assert "duplicate" in resp.text


# -------------------------------------------------------------------- conflicts


def test_declare_and_warning_may_coexist_at_equal_threshold(client):
    client.post("/rule-sets", json={"code": "rs", "jurisdiction": "EU"})
    resp = client.put(
        "/rule-sets/rs",
        json={
            "base_version": 0,
            "rules": [
                {"code": "d", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "declare", "applies_to": "contains"},
                {"code": "w", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "warning", "applies_to": "contains"},
            ],
        },
    )
    assert resp.status_code == 200
    published = client.post("/rule-sets/rs/publish")
    assert published.status_code == 200
    assert published.json()["version"] == 1


def test_prohibit_conflicts_with_any_action_and_rejects_publish(client):
    client.post("/rule-sets", json={"code": "rs", "jurisdiction": "EU"})
    client.put(
        "/rule-sets/rs",
        json={
            "base_version": 0,
            "rules": [
                {"code": "p", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "prohibit", "applies_to": "contains"},
                {"code": "d", "allergen": "gluten", "threshold": 20000, "unit": "ppb",
                 "action": "declare", "applies_to": "contains"},
                {"code": "w", "allergen": "gluten", "threshold": 0.002, "unit": "%",
                 "action": "warning", "applies_to": "contains"},
            ],
        },
    )
    resp = client.post("/rule-sets/rs/publish")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "RULE_SET_CONFLICT"
    conflicts = detail["conflicts"]
    # prohibit vs declare and prohibit vs warning (equal ppm 20 across units)
    assert len(conflicts) == 2
    # stable ordering by (allergen, applies_to, threshold, rule_a, rule_b)
    pairs = [(c["rule_a"]["code"], c["rule_b"]["code"]) for c in conflicts]
    assert pairs == [("d", "p"), ("p", "w")]
    assert all(c["reason"] == "prohibit_conflict" for c in conflicts)
    assert all(c["threshold_ppm"] == 20.0 for c in conflicts)


def test_same_action_duplicates_conflict_but_different_thresholds_do_not(client):
    client.post("/rule-sets", json={"code": "rs", "jurisdiction": "EU"})
    client.put(
        "/rule-sets/rs",
        json={
            "base_version": 0,
            "rules": [
                {"code": "d1", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "declare", "applies_to": "contains"},
                {"code": "d2", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "declare", "applies_to": "contains"},
            ],
        },
    )
    resp = client.post("/rule-sets/rs/publish")
    assert resp.status_code == 409
    conflict = resp.json()["detail"]["conflicts"][0]
    assert conflict["reason"] == "duplicate_action"

    # relax one threshold -> no overlap -> publishes
    client.put(
        "/rule-sets/rs",
        json={
            "base_version": 1,
            "rules": [
                {"code": "d1", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "declare", "applies_to": "contains"},
                {"code": "d2", "allergen": "gluten", "threshold": 21, "unit": "ppm",
                 "action": "declare", "applies_to": "contains"},
            ],
        },
    )
    assert client.post("/rule-sets/rs/publish").status_code == 200


def test_conflicts_only_within_same_allergen_and_applies_to(client):
    client.post("/rule-sets", json={"code": "rs", "jurisdiction": "EU"})
    client.put(
        "/rule-sets/rs",
        json={
            "base_version": 0,
            "rules": [
                {"code": "p1", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "prohibit", "applies_to": "contains"},
                {"code": "p2", "allergen": "milk", "threshold": 20, "unit": "ppm",
                 "action": "prohibit", "applies_to": "contains"},
                {"code": "p3", "allergen": "gluten", "threshold": 20, "unit": "ppm",
                 "action": "prohibit", "applies_to": "cross_contact"},
            ],
        },
    )
    # different allergen or different applies_to -> rules do not overlap
    assert client.post("/rule-sets/rs/publish").status_code == 200


def test_publish_empty_draft_rejected(client):
    client.post("/rule-sets", json={"code": "rs", "jurisdiction": "EU"})
    resp = client.post("/rule-sets/rs/publish")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "RULE_SET_EMPTY"


# ------------------------------------------------------------------ publishing


def test_publish_is_idempotent_and_new_submit_yields_new_version(client):
    make_rule_set(client)
    first = publish_rule_set(client)
    assert first["version"] == 1
    assert first["status"] == "published"
    summary = first["summary"]
    assert summary["rule_count"] == 4
    assert summary["actions"] == {"declare": 2, "warning": 1, "prohibit": 1}
    assert summary["applies_to"] == {"contains": 3, "cross_contact": 1}
    assert summary["allergens"] == ["gluten", "peanut", "tree_nuts"]
    assert summary["threshold_ppm"] == {"min": 0.0, "max": 20.0}
    assert len(summary["digest"]) == 64
    assert "gluten-prohibit-20" in summary["rule_codes"]

    # republish without a new submit -> same snapshot, same version/digest
    again = client.post("/rule-sets/eu-2026/publish")
    assert again.status_code == 200
    assert again.json()["version"] == 1
    assert again.json()["summary"]["digest"] == summary["digest"]

    # versions listing
    versions = client.get("/rule-sets/eu-2026/versions").json()
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["summary"]["digest"] == summary["digest"]
    assert client.get("/rule-sets/eu-2026/versions/1").json()["rules"] == first["rules"]
    assert client.get("/rule-sets/eu-2026/versions/9").status_code == 404


def test_immutable_snapshot_replays_even_after_new_draft(client):
    make_rule_set(client)
    v1 = publish_rule_set(client)
    # v2 drops the tree_nuts declare rule
    new_rules = [r for r in GLUTEN_RULES_PPM if r.get("allergen") != "tree_nuts"]
    client.put("/rule-sets/eu-2026", json={"base_version": 1, "rules": new_rules})
    v2 = client.post("/rule-sets/eu-2026/publish").json()
    assert v2["version"] == 2
    assert v2["summary"]["digest"] != v1["summary"]["digest"]
    # v1 snapshot is unchanged and still fetchable
    fetched_v1 = client.get("/rule-sets/eu-2026/versions/1").json()
    assert fetched_v1["summary"]["digest"] == v1["summary"]["digest"]
    assert len(fetched_v1["rules"]) == 4


# ------------------------------------------------------------- bound evaluation


def test_evaluation_bound_to_published_rule_set(client):
    build_cake_scenario(client)
    make_rule_set(client)
    publish_rule_set(client)

    resp = client.post(
        "/evaluations",
        headers={"Idempotency-Key": "bound-1"},
        json={
            "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026", "rule_set_version": 1,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rule_set"] == {
        "code": "eu-2026",
        "version": 1,
        "summary": body["rule_set"]["summary"],
    }
    assert body["rule_set"]["summary"]["digest"]
    assert {o["allergen"] for o in body["obligations"]} == {"gluten", "tree_nuts"}
    assert {v["allergen"] for v in body["violations"]} == {"gluten"}
    assert body["warnings"][0]["allergen"] == "peanut"


def test_bound_evaluation_ignores_standalone_rules_and_newer_versions(client):
    build_cake_scenario(client)  # standalone EU rules published here
    # rule set v1: only a milk declare rule, which the cake does not contain
    make_rule_set(client, rules=[
        {"code": "milk-declare", "allergen": "milk", "threshold": 0, "unit": "ppm",
         "action": "declare", "applies_to": "contains"},
    ])
    publish_rule_set(client)
    # publish a v2 with gluten rules; binding must still see the v1 snapshot
    client.put("/rule-sets/eu-2026", json={"base_version": 1, "rules": GLUTEN_RULES_PPM})
    client.post("/rule-sets/eu-2026/publish")

    resp = client.post(
        "/evaluations",
        headers={"Idempotency-Key": "bound-old"},
        json={
            "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026", "rule_set_version": 1,
        },
    )
    body = resp.json()
    assert body["obligations"] == []
    assert body["violations"] == []
    # standalone gluten rules are not applied to a bound evaluation
    assert sorted(body["unregulated_allergens"]) == ["gluten", "peanut", "tree_nuts"]


def test_binding_requires_both_fields(client):
    resp = client.post(
        "/evaluations",
        headers={"Idempotency-Key": "k"},
        json={
            "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026",
        },
    )
    assert resp.status_code == 422
    assert "rule_set_version" in resp.text


def test_missing_or_unpublished_binding_returns_stable_errors(client):
    build_cake_scenario(client)
    base = {"recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU"}

    resp = client.post(
        "/evaluations", headers={"Idempotency-Key": "k1"},
        json={**base, "rule_set_code": "ghost", "rule_set_version": 1},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_NOT_FOUND"

    make_rule_set(client)  # draft at v1, not published
    resp = client.post(
        "/evaluations", headers={"Idempotency-Key": "k2"},
        json={**base, "rule_set_code": "eu-2026", "rule_set_version": 1},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "RULE_SET_NOT_PUBLISHED"
    assert detail["current_version"] == 1

    resp = client.post(
        "/evaluations", headers={"Idempotency-Key": "k3"},
        json={**base, "rule_set_code": "eu-2026", "rule_set_version": 5},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "RULE_SET_VERSION_NOT_FOUND"

    # failed binding attempts do not consume the idempotency key
    publish_rule_set(client)
    ok = client.post(
        "/evaluations", headers={"Idempotency-Key": "k2"},
        json={**base, "rule_set_code": "eu-2026", "rule_set_version": 1},
    )
    assert ok.status_code == 201


def test_binding_jurisdiction_mismatch(client):
    build_cake_scenario(client)
    make_rule_set(client, jurisdiction="US")
    publish_rule_set(client)
    resp = client.post(
        "/evaluations", headers={"Idempotency-Key": "k"},
        json={
            "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026", "rule_set_version": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "RULE_SET_JURISDICTION_MISMATCH"


def test_replay_by_evaluation_id_returns_summary_recipe_and_result(client):
    build_cake_scenario(client)
    make_rule_set(client)
    publish_rule_set(client)
    created = client.post(
        "/evaluations", headers={"Idempotency-Key": "bound"},
        json={
            "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026", "rule_set_version": 1,
        },
    ).json()
    eid = created["evaluation_id"]

    fetched = client.get(f"/evaluations/{eid}").json()
    assert fetched["evaluation_id"] == eid
    assert fetched["recipe"] == {"code": "cake", "version": 1}
    assert fetched["rule_set"]["code"] == "eu-2026"
    assert fetched["rule_set"]["version"] == 1
    assert fetched["rule_set"]["summary"]["digest"]
    # full verdict reproduced
    assert fetched["obligations"] == created["obligations"]
    assert fetched["violations"] == created["violations"]


def test_replay_by_rule_set_version_reproduces_same_verdict(client):
    build_cake_scenario(client)
    make_rule_set(client)
    publish_rule_set(client)
    first = client.post(
        "/evaluations", headers={"Idempotency-Key": "a"},
        json={
            "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026", "rule_set_version": 1,
        },
    ).json()
    # another evaluation against the same frozen snapshot
    client.post(
        "/evaluations", headers={"Idempotency-Key": "b"},
        json={
            "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026", "rule_set_version": 1,
        },
    )

    listing = client.get("/rule-sets/eu-2026/versions/1/evaluations").json()
    assert len(listing) == 2
    item = listing[0]
    assert item["rule_set"]["version"] == 1
    assert item["rule_set"]["summary"]["digest"] == first["rule_set"]["summary"]["digest"]
    assert item["recipe"] == {"code": "cake", "version": 1}
    assert item["result"]["violations"] == first["violations"]

    # equivalent filter on the generic listing
    generic = client.get(
        "/evaluations?rule_set_code=eu-2026&rule_set_version=1"
    ).json()
    assert len(generic) == 2

    # unknown snapshot version on the replay endpoint
    assert client.get("/rule-sets/eu-2026/versions/9/evaluations").status_code == 404


def test_idempotent_replay_of_bound_evaluation(client):
    build_cake_scenario(client)
    make_rule_set(client)
    publish_rule_set(client)
    payload = {
        "recipe_code": "cake", "recipe_version": 1, "jurisdiction": "EU",
        "rule_set_code": "eu-2026", "rule_set_version": 1,
    }
    first = client.post(
        "/evaluations", headers={"Idempotency-Key": "fixed"}, json=payload
    )
    assert first.status_code == 201
    second = client.post(
        "/evaluations", headers={"Idempotency-Key": "fixed"}, json=payload
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["evaluation_id"] == first.json()["evaluation_id"]


def test_legacy_jurisdiction_evaluation_still_works_without_rule_set(client):
    build_cake_scenario(client)
    resp = evaluate(client, "cake", 1, jurisdiction="EU", key="legacy")
    assert resp.status_code == 201
    body = resp.json()
    assert "rule_set" not in body
    assert {o["allergen"] for o in body["obligations"]} == {"gluten", "tree_nuts"}


def test_boundary_threshold_equality_inclusive(client):
    make_ingredient(
        client, "wheat",
        allergens=[{"allergen": "gluten", "concentration": 20, "unit": "ppm"}],
    )
    make_recipe(
        client, "bread",
        [{
            "version": 1,
            "items": [{"kind": "ingredient", "code": "wheat", "quantity": 1, "unit": "kg"}],
        }],
    )
    make_rule_set(
        client,
        rules=[
            {"code": "p", "allergen": "gluten", "threshold": 20, "unit": "ppm",
             "action": "prohibit", "applies_to": "contains"},
        ],
    )
    publish_rule_set(client)
    resp = client.post(
        "/evaluations", headers={"Idempotency-Key": "edge"},
        json={
            "recipe_code": "bread", "recipe_version": 1, "jurisdiction": "EU",
            "rule_set_code": "eu-2026", "rule_set_version": 1,
        },
    )
    # concentration == threshold (20 ppm) triggers (boundary inclusive)
    assert [v["allergen"] for v in resp.json()["violations"]] == ["gluten"]
