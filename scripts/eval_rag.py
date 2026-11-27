"""레시피 검색(RAG) 평가: tests/rag_eval/cases.jsonl 의 질문으로 lib.recipes.search_recipes() 를 채점해요.

    python scripts/eval_rag.py                         # 벡터 검색 (DB 없이, 시드 파일로)
    python scripts/eval_rag.py --mode text             # 글자 검색만 (Bedrock 못 부를 때의 대신 경로)
    python scripts/eval_rag.py --out tests/rag_eval/results/baseline_vector.json
    python scripts/eval_rag.py --compare tests/rag_eval/results/baseline_vector.json
    python scripts/eval_rag.py --only multi -v         # 유형 하나만, 질문별 상위 3개 제목까지

--backend local (기본) : 레시피·임베딩은 db/seed/recipes_seed.jsonl.gz, 카탈로그는 db/0002_seed_catalog.sql
--backend db           : 진짜 DB 의 recipes·카탈로그 (로컬은 scripts/tunnel.sh 먼저)
사용자 알레르기·재고는 두 경우 모두 질문에 적힌 가짜 값이에요.

벡터 모드는 검색어 임베딩에 Bedrock 이 필요해요 (.env 의 BEDROCK_REGION, 로컬은 aws login).
한 번 만든 검색어 임베딩은 data/rag_eval/query_embeddings.json 에 모아 둬서 다시 돌릴 때는 Bedrock 을 안 불러요.
지표 설명은 lib/rag_eval.py 맨 위에 있어요.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import rag_eval as ev  # noqa: E402

CACHE_PATH = ROOT / "data" / "rag_eval" / "query_embeddings.json"
SHOW = ("cases", "n", "zero", "hit@3", "p@3", "p@10", "cov@10", "unsafe", "ms")


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.0f}"
    return str(v)


def print_summary(summary: dict) -> None:
    order = ["all", *[t for t in ev.CASE_TYPES if t in summary]]
    print("\n" + "group".ljust(11) + "".join(m.rjust(8) for m in SHOW))
    for g in order:
        print(g.ljust(11) + "".join(_fmt(summary[g].get(m)).rjust(8) for m in SHOW))


def print_compare(diff: dict) -> None:
    print("\n비교 (이전 → 지금, 차이)")
    for g, ms in diff["summary"].items():
        parts = []
        for m in ("hit@3", "p@3", "p@10", "cov@10", "zero", "unsafe"):
            if m in ms:
                b, a, d = ms[m]
                parts.append(f"{m} {_fmt(b)}→{_fmt(a)}" + (f" ({d:+.3f})" if d else ""))
        print(f"  {g:<10} " + "  ".join(parts))
    print("  나빠진 질문: " + (", ".join(diff["worse"]) if diff["worse"] else "없음"))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="레시피 검색 평가")
    p.add_argument("--mode", choices=["vector", "text"], default="vector")
    p.add_argument("--backend", choices=["local", "db"], default="local")
    p.add_argument("--cases", type=Path, default=ev.CASES_PATH)
    p.add_argument("--only", help="이 유형만 (예: multi) — 쉼표로 여러 개")
    p.add_argument("--out", type=Path, help="결과를 JSON 으로 저장")
    p.add_argument("--compare", type=Path, help="이전에 --out 으로 저장한 결과와 비교")
    p.add_argument("-v", "--verbose", action="store_true", help="질문별 결과 출력")
    args = p.parse_args(argv)

    cases = ev.load_cases(args.cases)
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.type in wanted or c.id in wanted]
    seed_rows = ev.load_seed_rows()
    corpus = [ev.row_to_recipe(r) for r in seed_rows]  # 맞는 레시피 수(분모)는 항상 시드 파일 기준

    if args.backend == "local":
        catalog, fetch_all = ev.load_catalog_sql(), ev.LocalStore(seed_rows).fetch_all
    else:
        from lib.inventory import get_catalog

        catalog, fetch_all = get_catalog(), None

    embedder = ev.QueryEmbedder(args.mode, CACHE_PATH)

    def on_case(row: dict) -> None:
        if args.verbose:
            m = row["metrics"]
            print(f"- {row['id']:<32} n={m['n']:<3} hit@3={_fmt(m['hit@3'])} p@10={_fmt(m['p@10'])}"
                  f" cov={_fmt(m['cov@10'])} 맞는레시피={row['n_relevant']}  {' / '.join(row['top3'])}")
        if args.mode == "vector" and embedder.failures:
            raise SystemExit(
                "검색어 임베딩(Bedrock)을 못 불렀어요: " + embedder.failures[0]
                + "\n  → .env 에 BEDROCK_REGION 을 넣고, 로컬은 `aws login` 후 다시 돌려 주세요."
                + "\n  → 지금 바로 보려면 --mode text (글자 검색 경로)로 돌릴 수 있어요."
            )

    try:
        rows = ev.evaluate(cases, corpus, catalog, embedder, fetch_all, on_case)
    finally:
        embedder.save()
    summary = ev.summarize(rows)
    print(f"질문 {len(rows)}개 · mode={args.mode} · backend={args.backend}"
          + (f" · 임베딩 호출 {embedder.calls}번, 캐시 {embedder.cache_hits}번" if args.mode == "vector" else ""))
    print_summary(summary)

    result = {
        "meta": {
            "mode": args.mode, "backend": args.backend, "cases_file": str(args.cases.relative_to(ROOT)),
            "embed_model": embedder.model, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "summary": summary,
        "cases": rows,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n저장: {args.out}")
    if args.compare:
        print_compare(ev.compare(json.loads(args.compare.read_text(encoding="utf-8")), result))
    return 1 if summary["all"].get("unsafe") else 0


if __name__ == "__main__":
    sys.exit(main())
