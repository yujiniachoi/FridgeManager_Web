"""레시피 검색 평가(lib/rag_eval.py) 검사 — DB·Bedrock 없이."""
import json

import pytest

from lib import rag_eval as ev
from lib.models import Recipe, RecipeIngredient


def _recipe(rid, title, names, tags=(), minutes=20, allergens=()):
    return Recipe(
        id=rid, title=title, steps="",
        ingredients=[RecipeIngredient(name=n) for n in names],
        cook_minutes=minutes, tags=list(tags), allergens=list(allergens),
    )


def _case(**kw):
    base = {"id": "c", "type": "single", "input": {"ingredients": ["두부"]}, "relevant": {"uses_any": ["두부"]}}
    return ev.parse_case({**base, **kw})


@pytest.fixture(scope="module")
def catalog():
    return ev.load_catalog_sql()


# ── 질문 세트 파일 ─────────────────────────────
def test_cases_file_is_valid_and_every_case_has_answers(catalog):
    cases = ev.load_cases()
    assert len(cases) >= 30
    assert {c.type for c in cases} == set(ev.CASE_TYPES)
    corpus = [ev.row_to_recipe(r) for r in ev.load_seed_rows()]
    empty = [c.id for c in cases if not any(ev.is_relevant(r, c, catalog) for r in corpus)]
    assert empty == []  # 맞는 레시피가 하나도 없는 질문은 채점이 안 돼요


@pytest.mark.parametrize("bad, msg", [
    ({"type": "nope"}, "type"),
    ({"relevant": {}}, "비어"),
    ({"relevant": {"uses": ["두부"]}}, "모르는 relevant"),
    ({"extra": 1}, "모르는 칸"),
])
def test_parse_case_rejects_bad_shapes(bad, msg):
    with pytest.raises(ValueError, match=msg):
        _case(**bad)


def test_duplicate_ids_rejected(tmp_path):
    line = json.dumps({"id": "a", "type": "single", "input": {}, "relevant": {"uses_any": ["두부"]}})
    p = tmp_path / "cases.jsonl"
    p.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="겹쳐요"):
        ev.load_cases(p)


def test_catalog_sql_keeps_aliases(catalog):
    by_name = {c.name: c for c in catalog}
    assert "파" in by_name["대파"].aliases
    assert by_name["양파"].aliases == []


# ── 맞는 레시피 판정 ─────────────────────────────
def test_relevance_uses_catalog_alias_and_suffix(catalog):
    case = _case(relevant={"uses_any": ["대파"]})
    assert ev.is_relevant(_recipe("1", "파전", ["파", "밀가루"]), case, catalog)  # 파 → 대파
    assert ev.is_relevant(_recipe("2", "볶음", ["다진대파"]), case, catalog)
    assert not ev.is_relevant(_recipe("3", "양파볶음", ["양파"]), case, catalog)


def test_relevance_tags_title_minutes():
    case = _case(relevant={"uses_any": ["두부"], "tags_all": ["국&찌개"], "tags_none": ["매운맛"],
                           "title_any": ["국"], "max_minutes": 20})
    ok = _recipe("1", "두부국", ["두부"], tags=["국&찌개"], minutes=20)
    assert ev.is_relevant(ok, case)
    assert not ev.is_relevant(_recipe("2", "두부국", ["두부"], tags=["국&찌개", "매운맛"]), case)
    assert not ev.is_relevant(_recipe("3", "두부국", ["두부"], tags=["국&찌개"], minutes=30), case)
    assert not ev.is_relevant(_recipe("4", "두부조림", ["두부"], tags=["국&찌개"]), case)


def test_allergy_makes_recipe_irrelevant_and_unsafe():
    case = _case(user={"allergies": ["계란"]})
    r = _recipe("1", "두부계란찜", ["두부", "달걀"], allergens=["달걀"])
    assert not ev.is_relevant(r, case)
    assert ev.case_metrics([r], case, n_relevant=5)["unsafe"] == 1


# ── 지표 ─────────────────────────────────────
def test_metrics_precision_is_capped_by_answer_count():
    case = _case()
    good, bad = _recipe("g", "두부조림", ["두부"]), _recipe("b", "감자조림", ["감자"])
    m = ev.case_metrics([bad, good], case, n_relevant=2)
    assert m["n"] == 2 and m["zero"] == 0 and m["hit@3"] == 1
    assert m["p@3"] == 0.5 and m["p@10"] == 0.5  # 분모 = min(k, 맞는 레시피 2개)
    m = ev.case_metrics([good], case, n_relevant=100)
    assert m["p@10"] == 0.1  # 결과가 적으면 깎여요


def test_metrics_empty_and_coverage():
    case = _case(type="multi", input={"ingredients": ["두부", "감자", "우유"]})
    assert ev.case_metrics([], case, n_relevant=3) == {
        "n": 0, "zero": 1, "hit@3": 0, "p@3": 0.0, "p@10": 0.0, "cov@10": 0.0, "unsafe": 0}
    m = ev.case_metrics([_recipe("1", "a", ["두부"]), _recipe("2", "b", ["감자"])], case, n_relevant=3)
    assert m["cov@10"] == round(2 / 3, 4)


def test_summarize_groups_and_skips_missing():
    rows = [
        {"type": "single", "metrics": {"hit@3": 1, "p@10": 1.0, "cov@10": None, "unsafe": 0}},
        {"type": "multi", "metrics": {"hit@3": 0, "p@10": 0.5, "cov@10": 0.5, "unsafe": 1}},
    ]
    s = ev.summarize(rows)
    assert s["all"]["hit@3"] == 0.5 and s["all"]["cov@10"] == 0.5 and s["all"]["unsafe"] == 1
    assert s["single"]["cases"] == 1 and "cov@10" not in s["single"]


def test_compare_lists_worse_cases():
    before = {"summary": {"all": {"hit@3": 1.0}}, "cases": [
        {"id": "a", "metrics": {"hit@3": 1, "p@10": 1.0, "unsafe": 0}},
        {"id": "b", "metrics": {"hit@3": 1, "p@10": 0.5, "unsafe": 0}}]}
    after = {"summary": {"all": {"hit@3": 0.5}}, "cases": [
        {"id": "a", "metrics": {"hit@3": 0, "p@10": 1.0, "unsafe": 0}},
        {"id": "b", "metrics": {"hit@3": 1, "p@10": 0.55, "unsafe": 0}}]}
    d = ev.compare(before, after)
    assert d["worse"] == ["a"]
    assert d["summary"]["all"]["hit@3"] == (1.0, 0.5, -0.5)


# ── 검색을 DB 없이 돌리기 ─────────────────────────
ROWS = [
    {"source_id": "s:1", "title": "두부조림", "ingredients": [{"name": "두부"}, {"name": "간장"}], "steps": "",
     "cook_minutes": 20, "servings": 1, "tags": ["반찬"], "allergens": ["대두"], "embedding": [1.0, 0.0, 0.0]},
    {"source_id": "s:2", "title": "두부달걀찜", "ingredients": [{"name": "두부"}, {"name": "달걀"}], "steps": "",
     "cook_minutes": 10, "servings": 1, "tags": ["찌기"], "allergens": ["대두", "달걀"], "embedding": [0.8, 0.6, 0.0]},
    {"source_id": "s:3", "title": "감자볶음", "ingredients": [{"name": "감자"}], "steps": "",
     "cook_minutes": 15, "servings": 1, "tags": ["반찬"], "allergens": [], "embedding": [0.0, 1.0, 0.0]},
]


def test_local_store_vector_order_and_filters():
    from lib import recipes

    store = ev.LocalStore(ROWS)
    rows = store.fetch_all(recipes._VECTOR_SQL, ([], None, None, "[1,0,0]", 30))
    assert [r["title"] for r in rows] == ["두부조림", "두부달걀찜", "감자볶음"]
    assert "embedding" not in rows[0]
    rows = store.fetch_all(recipes._VECTOR_SQL, (["달걀"], 15, 15, "[1,0,0]", 30))
    assert [r["title"] for r in rows] == ["감자볶음"]


def test_local_store_text_search_and_unknown_sql():
    from lib import recipes

    store = ev.LocalStore(ROWS)
    rows = store.fetch_all(recipes._TEXT_SQL, ([], None, None, ["%두부%"], 30))
    assert [r["title"] for r in rows] == ["두부달걀찜", "두부조림"]  # 제목 순
    with pytest.raises(NotImplementedError):
        store.fetch_all("select 1", ())


def test_run_case_end_to_end_without_db(tmp_path):
    embedder = ev.QueryEmbedder("vector", tmp_path / "q.json", embed_fn=lambda _t: [1.0, 0.0, 0.0])
    case = _case(user={"allergies": ["계란"]})
    results, ms = ev.run_case(case, [], embedder, ev.LocalStore(ROWS).fetch_all)
    assert [r.title for r in results] == ["두부조림"]  # 달걀 알레르기·두부 안 쓰는 레시피 빠짐
    assert ms >= 0 and embedder.calls == 1
    embedder.save()

    again = ev.QueryEmbedder("vector", tmp_path / "q.json", embed_fn=lambda _t: pytest.fail("캐시를 써야 해요"))
    ev.run_case(case, [], again, ev.LocalStore(ROWS).fetch_all)
    assert again.cache_hits == 1 and again.calls == 0


def test_text_mode_uses_fallback_search():
    embedder = ev.QueryEmbedder("text")
    results, _ = ev.run_case(_case(), [], embedder, ev.LocalStore(ROWS).fetch_all)
    assert {r.title for r in results} == {"두부조림", "두부달걀찜"}
    assert embedder.failures == []  # 일부러 실패시킨 건 '실패'로 세지 않아요


def test_vector_mode_records_bedrock_failure():
    def boom(_t):
        raise RuntimeError("no creds")

    embedder = ev.QueryEmbedder("vector", embed_fn=boom)
    ev.run_case(_case(), [], embedder, ev.LocalStore(ROWS).fetch_all)  # 검색은 글자 검색으로 계속돼요
    assert embedder.failures == ["RuntimeError: no creds"]
