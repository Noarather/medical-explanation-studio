# v1.7 evaluation JSONL

Each line contains `id`, evidence `grade` (`A`, `B`, or `C`),
`relevant_chunk_ids`, and `candidates`. Each candidate has `chunk_id`,
`cosine`, `rerank_score`, plus optional page/source fields. Judge samples may
also contain `question`, `answer`, and verbatim `evidence` contexts.

Never commit real questions or textbook text. Real samples and reports belong
under the gitignored `work/evals/` directory.

`pdf_page` is a positive physical page number (1-based), not the printed page.
With `tools/evaluate_v17.py --verify-pdfs`, optional candidate `source_path`
points to a local PDF. The offline audit checks existence, readability and page
bounds; missing paths are reported as unverified, never silently counted as a
successful file check. It reads only, does not change automatic review decisions
and does not call a model. All candidates are audited, not only accepted Top 3.
