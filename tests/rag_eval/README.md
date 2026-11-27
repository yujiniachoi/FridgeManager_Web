# 레시피 검색 평가 세트

`lib.recipes.search_recipes()`를 질문 44개로 채점해요. 검색 전략·청킹을 바꾸기 **전과 후**에 돌려 숫자로 비교해요.

- `cases.jsonl` — 질문 (single / alias / multi / tag / constraint / free_text). 형식과 지표 설명은 `lib/rag_eval.py` 맨 위
- `results/` — 기준 결과. 바꾸기 전에 저장해 두고 `--compare`로 비교

```bash
python scripts/eval_rag.py --mode text --out tests/rag_eval/results/baseline_text.json   # Bedrock 없이 (글자 검색 경로)
python scripts/eval_rag.py --out tests/rag_eval/results/baseline_vector.json             # 벡터 검색 (aws login, BEDROCK_REGION)
python scripts/eval_rag.py --compare tests/rag_eval/results/baseline_vector.json -v      # 고친 뒤 비교
```

DB·터널 없이 시드 파일(`db/seed/recipes_seed.jsonl.gz`)로 돌아요 (`--backend db`면 진짜 DB).
질문을 추가할 때는 `relevant` 규칙으로 맞는 레시피를 정의해요. 레시피 id를 하나하나 적지 않아도 돼요.
`pytest tests/test_rag_eval.py`가 질문마다 맞는 레시피가 하나 이상 있는지 확인해요.
