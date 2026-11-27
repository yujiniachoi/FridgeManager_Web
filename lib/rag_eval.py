"""레시피 검색(RAG) 평가 — 질문 세트로 lib.recipes.search_recipes() 의 품질을 숫자로 봐요.

실행은 scripts/eval_rag.py 에서 해요. 이 파일에는 테스트할 수 있는 부분만 모았어요.

평가 질문 (tests/rag_eval/cases.jsonl, 한 줄에 하나)
    id, type        : 질문 이름과 유형 (single / alias / multi / tag / constraint / free_text)
    input           : search_recipes 에 넘길 값 (ingredients, tags, max_minutes)
    query           : 자유 문장 (지금 search_recipes 에는 칸이 없어서, 칸이 생기면 함께 넘겨요)
    user            : 가짜 사용자 {allergies: [...], have: [...]} (DB 사용자 없이 평가)
    relevant        : '맞는 레시피' 규칙. 적힌 조건을 모두 만족하면 맞는 레시피예요
        uses_any / uses_all : 재료를 하나라도 / 모두 씀 (recipe_rules.same_ingredient 로 비교)
        tags_any / tags_all / tags_none : 태그 조건
        title_any            : 제목에 이 글자 중 하나가 들어감
        max_minutes          : 조리 시간 상한
      + user.allergies 가 있으면 알레르기 재료가 없는 레시피만 맞는 레시피예요

지표 (질문마다 → 유형별·전체 평균)
    n          : 결과 수 (최대 30)
    zero       : 결과가 0개면 1
    hit@3      : 상위 3개(홈 카드·채팅 카드) 안에 맞는 레시피가 있으면 1
    p@3, p@10  : 상위 k개 중 맞는 레시피 비율. 분모는 min(k, 전체에서 맞는 레시피 수) 라서 최대 1
                 (결과가 k개보다 적으면 그만큼 깎여요 — '결과가 적은 것'도 문제로 봐요)
    cov@10     : 재료 2개 이상일 때, 입력 재료 중 상위 10개에서 한 번이라도 쓰인 재료 비율
    unsafe     : 상위 10개 중 사용자 알레르기 재료가 든 레시피 수 (0이어야 해요)
"""
from __future__ import annotations

import gzip
import json
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from unittest import mock

from lib.models import CatalogItem, Recipe, RecipeIngredient, RecipeMatch
from lib.recipe_rules import _same_key, canonical

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "tests" / "rag_eval" / "cases.jsonl"
SEED_PATH = ROOT / "db" / "seed" / "recipes_seed.jsonl.gz"
CATALOG_SQL_PATH = ROOT / "db" / "0002_seed_catalog.sql"

CASE_TYPES = ("single", "alias", "multi", "tag", "constraint", "free_text")
RULE_KEYS = {"uses_any", "uses_all", "tags_any", "tags_all", "tags_none", "title_any", "max_minutes"}
METRICS = ("n", "zero", "hit@3", "p@3", "p@10", "cov@10", "unsafe", "ms")


# ── 질문 세트 ─────────────────────────────────────
@dataclass
class Case:
    id: str
    type: str
    input: dict
    relevant: dict
    query: Optional[str] = None
    user: dict = field(default_factory=dict)
    note: Optional[str] = None

    @property
    def ingredients(self) -> list[str]:
        return list(self.input.get("ingredients") or [])

    @property
    def allergies(self) -> list[str]:
        return list(self.user.get("allergies") or [])

    @property
    def have(self) -> list[str]:
        return list(self.user.get("have") or [])


def parse_case(obj: dict) -> Case:
    """한 줄 → Case. 모양이 틀리면 ValueError (어느 질문인지 알려 줘요)."""
    cid = obj.get("id") or "?"
    if obj.get("type") not in CASE_TYPES:
        raise ValueError(f"{cid}: type 은 {CASE_TYPES} 중 하나여야 해요.")
    rel = obj.get("relevant") or {}
    if not rel:
        raise ValueError(f"{cid}: relevant 규칙이 비어 있어요.")
    unknown = set(rel) - RULE_KEYS
    if unknown:
        raise ValueError(f"{cid}: 모르는 relevant 조건 {sorted(unknown)}")
    unknown = set(obj) - {"id", "type", "input", "relevant", "query", "user", "note"}
    if unknown:
        raise ValueError(f"{cid}: 모르는 칸 {sorted(unknown)}")
    return Case(
        id=cid, type=obj["type"], input=obj.get("input") or {}, relevant=rel,
        query=obj.get("query"), user=obj.get("user") or {}, note=obj.get("note"),
    )


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    cases = [parse_case(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [c.id for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"id 가 겹쳐요: {sorted(dup)}")
    return cases


# ── 레시피 모음 (시드 파일 = DB 에 들어간 것과 같은 레시피·임베딩) ─────────
def row_to_recipe(row: dict) -> Recipe:
    return Recipe(
        id=str(row.get("id") or row.get("source_id")),
        title=row["title"],
        ingredients=[
            RecipeIngredient(name=i.get("name", ""), qty=i.get("qty"), unit=i.get("unit"), optional=bool(i.get("optional")))
            for i in (row.get("ingredients") or [])
        ],
        steps=row.get("steps") or "",
        cook_minutes=row.get("cook_minutes"),
        servings=row.get("servings"),
        tags=list(row.get("tags") or []),
        allergens=list(row.get("allergens") or []),
        image_url=row.get("image_url"),
    )


def load_seed_rows(path: Path = SEED_PATH) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


_CATALOG_ROW = re.compile(r"\(\s*'([^']+)'\s*,\s*'\{([^}]*)\}'\s*,\s*'([^']*)'\s*,\s*(\d+)\s*,\s*'(\w+)'\s*\)")


def load_catalog_sql(path: Path = CATALOG_SQL_PATH) -> list[CatalogItem]:
    """db/0002_seed_catalog.sql 의 insert 값 → CatalogItem (DB 없이 같은 별칭을 쓰려고)."""
    items = []
    for n, m in enumerate(_CATALOG_ROW.finditer(path.read_text(encoding="utf-8")), start=1):
        name, aliases, category, days, storage = m.groups()
        alias_list = [a.strip().strip('"') for a in aliases.split(",") if a.strip()]
        items.append(CatalogItem(id=n, name=name, aliases=alias_list, category=category or None,
                                 default_shelf_days=int(days), storage=storage))
    return items


# ── 맞는 레시피 판정 ─────────────────────────────────
_CANON: dict[tuple[int, str], str] = {}


def _canon(name: str, catalog) -> str:
    """recipe_rules.canonical 을 기억해 둬요 (전체 레시피를 판정하느라 같은 이름을 수만 번 찾아서)."""
    key = (id(catalog), name)
    if key not in _CANON:
        _CANON[key] = canonical(name, catalog)
    return _CANON[key]


def _uses(recipe: Recipe, name: str, catalog) -> bool:
    """same_ingredient(name, 재료, catalog) 와 같은 판정."""
    k = _canon(name, catalog)
    return any(_same_key(k, _canon(i.name, catalog)) for i in recipe.ingredients)


def is_relevant(recipe: Recipe, case: Case, catalog=None) -> bool:
    rule = case.relevant
    tags = set(recipe.tags)
    if "uses_any" in rule and not any(_uses(recipe, n, catalog) for n in rule["uses_any"]):
        return False
    if "uses_all" in rule and not all(_uses(recipe, n, catalog) for n in rule["uses_all"]):
        return False
    if "tags_any" in rule and not tags & set(rule["tags_any"]):
        return False
    if "tags_all" in rule and not set(rule["tags_all"]) <= tags:
        return False
    if "tags_none" in rule and tags & set(rule["tags_none"]):
        return False
    if "title_any" in rule and not any(t in recipe.title for t in rule["title_any"]):
        return False
    if "max_minutes" in rule and (recipe.cook_minutes is None or recipe.cook_minutes > rule["max_minutes"]):
        return False
    if case.allergies and is_unsafe(recipe, case.allergies):
        return False
    return True


def is_unsafe(recipe: Recipe, allergies: list[str]) -> bool:
    """recipe_source 의 알레르기 검사(그룹 이름까지 넓혀서)와 같은 기준."""
    from lib.recipe_source import contains_allergen

    return contains_allergen(RecipeMatch(recipe=recipe, score=0.0), allergies)


# ── 지표 ─────────────────────────────────────────
def case_metrics(results: list[Recipe], case: Case, n_relevant: int, catalog=None) -> dict:
    """한 질문의 결과(점수 순 레시피) → 지표. n_relevant: 전체 레시피 중 맞는 레시피 수."""
    rel = [is_relevant(r, case, catalog) for r in results]

    def precision(k: int) -> Optional[float]:
        denom = min(k, n_relevant)
        return round(sum(rel[:k]) / denom, 4) if denom else None

    ings = case.ingredients
    cov = None
    if len(ings) >= 2:
        top = results[:10]
        cov = round(sum(any(_uses(r, n, catalog) for r in top) for n in ings) / len(ings), 4)
    return {
        "n": len(results),
        "zero": int(not results),
        "hit@3": int(any(rel[:3])) if n_relevant else None,
        "p@3": precision(3),
        "p@10": precision(10),
        "cov@10": cov,
        "unsafe": sum(is_unsafe(r, case.allergies) for r in results[:10]) if case.allergies else 0,
    }


def summarize(rows: list[dict]) -> dict:
    """질문별 결과 → {'all': {...}, 'single': {...}, ...} 평균 (값이 없는 지표는 빼고 평균)."""
    groups: dict[str, list[dict]] = {"all": rows}
    for r in rows:
        groups.setdefault(r["type"], []).append(r)
    out = {}
    for g, items in groups.items():
        s: dict[str, Any] = {"cases": len(items)}
        for m in METRICS:
            vals = [it["metrics"][m] for it in items if it["metrics"].get(m) is not None]
            if vals:
                s[m] = round(sum(vals) / len(vals), 4) if m != "unsafe" else sum(vals)
        out[g] = s
    return out


# ── 검색을 DB·사용자 없이 돌리기 ─────────────────────────
def _parse_vec(text: str):
    import numpy as np

    return np.array([float(x) for x in text.strip("[]").split(",")], dtype=np.float32)


class LocalStore:
    """시드 파일을 메모리에 올려 lib.recipes 의 SQL 을 흉내 내요 (터널·DB 없이 평가).

    lib/recipes.py 의 SQL 이 바뀌면 여기서 '모르는 SQL' 오류가 나요 → 같은 뜻으로 고쳐 주세요.
    벡터 검색은 정확한 코사인 거리(전수)라서, DB 의 HNSW(근사)와 순위가 아주 조금 다를 수 있어요.
    """

    def __init__(self, rows: list[dict]):
        import numpy as np

        self.rows = [{**r, "id": r.get("id") or r["source_id"]} for r in rows]
        with_emb = [r for r in self.rows if r.get("embedding")]
        self._emb_rows = with_emb
        self._matrix = np.array([r["embedding"] for r in with_emb], dtype=np.float32) if with_emb else None

    @staticmethod
    def _passes(row: dict, allergies, max_minutes) -> bool:
        if set(row.get("allergens") or []) & set(allergies or []):
            return False
        if max_minutes is not None and (row.get("cook_minutes") is None or row["cook_minutes"] > max_minutes):
            return False
        return True

    @staticmethod
    def _out(row: dict) -> dict:
        return {k: v for k, v in row.items() if k != "embedding"}

    def fetch_all(self, sql: str, params) -> list[dict]:
        from lib import recipes

        if sql == recipes._VECTOR_SQL:
            allergies, max_minutes, _, vec, limit = params
            q = _parse_vec(vec)
            dist = 1.0 - self._matrix @ q  # 임베딩은 정규화돼 있어서 코사인 거리 = 1 - 내적
            order = dist.argsort(kind="stable")
            picked = [self._emb_rows[i] for i in order if self._passes(self._emb_rows[i], allergies, max_minutes)]
            return [self._out(r) for r in picked[:limit]]
        if sql == recipes._TEXT_SQL:
            allergies, max_minutes, _, patterns, limit = params
            subs = [p.strip("%").lower() for p in patterns]
            picked = [
                r for r in self.rows
                if self._passes(r, allergies, max_minutes)
                and any(s in (i.get("name") or "").lower() for i in r["ingredients"] for s in subs)
            ]
            return [self._out(r) for r in sorted(picked, key=lambda r: r["title"])[:limit]]
        raise NotImplementedError(f"LocalStore 가 모르는 SQL 이에요 (lib/recipes.py 가 바뀌었나요?): {sql[:80]}…")


class QueryEmbedder:
    """검색어 임베딩 + 파일 캐시. mode='vector' 는 Bedrock(캐시에 없을 때), 'text' 는 일부러 실패시켜 글자 검색."""

    def __init__(self, mode: str, cache_path: Optional[Path] = None, embed_fn: Optional[Callable] = None):
        from lib.config import EMBED_MODEL_ID

        self.mode, self.cache_path, self.model = mode, cache_path, EMBED_MODEL_ID
        self._embed_fn = embed_fn
        self.cache: dict[str, list[float]] = {}
        if cache_path and cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
        self.calls = self.cache_hits = 0
        self.failures: list[str] = []

    def __call__(self, text: str) -> str:
        from lib.embeddings import to_pgvector

        if self.mode == "text":
            raise RuntimeError("평가: 글자 검색 모드")
        key = f"{self.model}|{text}"
        if key in self.cache:
            self.cache_hits += 1
            return to_pgvector(self.cache[key])
        try:
            if self._embed_fn is None:
                from lib.embeddings import embed_text as fn
            else:
                fn = self._embed_fn
            vec = list(fn(text))
        except Exception as e:
            self.failures.append(f"{type(e).__name__}: {e}")
            raise
        self.calls += 1
        self.cache[key] = vec
        return to_pgvector(vec)

    def save(self) -> None:
        if self.cache_path and self.calls:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")


def _search_kwargs(case: Case, search_fn) -> dict:
    import inspect

    kw = {k: case.input[k] for k in ("max_minutes", "servings", "tags") if case.input.get(k) is not None}
    # 자유 문장 칸(query)은 search_recipes 가 받을 때만 넘겨요 (지금은 없음 → 1단계에서 추가 예정)
    if case.query and "query" in inspect.signature(search_fn).parameters:
        kw["query"] = case.query
    return kw


def run_case(case: Case, catalog, embedder: QueryEmbedder, fetch_all=None) -> tuple[list[Recipe], float]:
    """질문 하나를 lib.recipes.search_recipes 로 돌려요. 사용자(알레르기·재고)는 질문에 적힌 가짜 값."""
    from lib import recipes
    from lib.allergens import normalize_allergy

    allergy_names = sorted({n for a in case.allergies for n in normalize_allergy(a)})
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(recipes, "_user_allergies", lambda _uid: allergy_names))
        stack.enter_context(mock.patch.object(recipes, "_user_have", lambda _uid: case.have))
        stack.enter_context(mock.patch.object(recipes, "_catalog", lambda: catalog))
        stack.enter_context(mock.patch.object(recipes, "_embed", embedder))
        if fetch_all is not None:
            stack.enter_context(mock.patch.object(recipes, "_fetch_all", fetch_all))
        t0 = time.perf_counter()
        matches = recipes.search_recipes("eval-user", case.ingredients, **_search_kwargs(case, recipes.search_recipes))
        ms = (time.perf_counter() - t0) * 1000
    return [m.recipe for m in matches], round(ms, 1)


def evaluate(
    cases: Iterable[Case],
    corpus: list[Recipe],
    catalog,
    embedder: QueryEmbedder,
    fetch_all=None,
    on_case: Optional[Callable[[dict], None]] = None,
) -> list[dict]:
    rows = []
    for case in cases:
        results, ms = run_case(case, catalog, embedder, fetch_all)
        n_rel = sum(is_relevant(r, case, catalog) for r in corpus)
        metrics = {**case_metrics(results, case, n_rel, catalog), "ms": ms}
        row = {
            "id": case.id, "type": case.type, "n_relevant": n_rel, "metrics": metrics,
            "top3": [r.title for r in results[:3]],
            "top10_relevant": [is_relevant(r, case, catalog) for r in results[:10]],
        }
        rows.append(row)
        if on_case:
            on_case(row)
    return rows


# ── 두 번 돌린 결과 비교 ─────────────────────────────
def compare(before: dict, after: dict, worse_threshold: float = 0.1) -> dict:
    """{'summary': {그룹: {지표: (전, 후, 차이)}}, 'worse': [나빠진 질문 id …]}"""
    summary = {}
    for g, a in after["summary"].items():
        b = before["summary"].get(g, {})
        summary[g] = {m: (b.get(m), a.get(m), None if b.get(m) is None or a.get(m) is None else round(a[m] - b[m], 4))
                      for m in METRICS if m in a or m in b}
    prev = {r["id"]: r["metrics"] for r in before["cases"]}
    worse = []
    for r in after["cases"]:
        p = prev.get(r["id"])
        if not p:
            continue
        m = r["metrics"]
        if (p.get("hit@3") or 0) > (m.get("hit@3") or 0) or (p.get("p@10") or 0) - (m.get("p@10") or 0) >= worse_threshold \
                or (m.get("unsafe") or 0) > (p.get("unsafe") or 0):
            worse.append(r["id"])
    return {"summary": summary, "worse": worse}
