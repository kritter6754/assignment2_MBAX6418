# Evaluation summary

Generated 2026-10-06 11:41 on 35 questions ({'text': 20, 'visual': 9, 'unanswerable': 6}).

## Retrieval (answerable questions)

| Mode | hit@1 | hit@3 | hit@8 | MRR | visual hit@3 | text hit@3 |
|---|---|---|---|---|---|---|
| hybrid | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid_no_rerank | 0.90 | 1.00 | 1.00 | 0.94 | 1.00 | 1.00 |
| text_vector | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| keyword | 0.83 | 0.97 | 1.00 | 0.90 | 0.89 | 1.00 |

## End-to-end answers

| Mode | answered + cited a gold slide | visual: same | answerable marked not found | partly supported | unanswerable correctly refused | false answers | dropped citations | median s |
|---|---|---|---|---|---|---|---|---|
| hybrid | 1.00 | 1.00 | 0.00 | 0 | 1.00 | 0 | 0 | 2.3 |
| text_vector | 0.93 | 0.78 | 0.07 | 2 | 1.00 | 0 | 0 | 1.3 |
