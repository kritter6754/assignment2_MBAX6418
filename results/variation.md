# Run-to-run variation (3 runs, 35 questions, 2026-10-15 00:04)

Mean (min–max) across identical runs.

## Retrieval

| Mode | hit@1 | hit@3 | MRR |
|---|---|---|---|
| hybrid | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) |
| hybrid_no_rerank | 0.90 (0.90–0.90) | 1.00 (1.00–1.00) | 0.94 (0.94–0.94) |
| text_vector | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) |
| keyword | 0.83 (0.83–0.83) | 0.97 (0.97–0.97) | 0.90 (0.90–0.90) |

## End-to-end

| Mode | answered + correct slide | visual: same | answerable marked not found | unanswerable refused | false answers | partly supported | median s |
|---|---|---|---|---|---|---|---|
| hybrid | 1.00 (1.00–1.00) | 1.00 (1.00–1.00) | 0.00 (0.00–0.00) | 1.00 (1.00–1.00) | 0.00 (0.00–0.00) | 0.67 (0.00–1.00) | 2.17 (2.10–2.30) |
| text_vector | 0.93 (0.93–0.93) | 0.78 (0.78–0.78) | 0.07 (0.07–0.07) | 1.00 (1.00–1.00) | 0.00 (0.00–0.00) | 1.00 (1.00–1.00) | 1.27 (1.20–1.30) |

## Questions whose outcome changed between runs

- **hybrid t07** (text): answered +gold → partially_supported +gold → partially_supported +gold
- **text_vector:** none
