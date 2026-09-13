# Experiment Summary Figure Contract

- Backend: existing Python / matplotlib workflow; no image generation.
- Core conclusion: the current pilot has incomplete task/condition coverage; report all measured outcomes and only available paired ablations without implying a complete benchmark.
- Evidence: (1) nine-task six-condition raw-return matrix with execution states; (2) full-minus-ablation differences only where both completed; (3) completed-episode token costs, including attributed activation setup.
- Archetype: quantitative grids with a primary result matrix and subordinate ablation/cost panels.
- Source: one explicitly selected run's original episode CSV and summaries, never a mixture of favorable seeds. The current source retains inherited completed episodes and their origin record.
- Missingness: unstarted, interrupted and provider-blocked cells are distinct non-numeric states; none is drawn as zero. Real zero results remain numeric.
- Error handling: all completed rollouts are retained, including model/review/execution errors, with explicit visual flags. Partial rollouts have no terminal return.
- Statistics: one paired initial seed per task/condition. No confidence interval, significance, cross-task raw-reward average, or attribution of a stochastic single-run difference to one mechanism as a general causal result.
- Scales: raw returns are compared within tasks; ablation differences remain in task-specific reward units. Token-cost axes share the same units and range. Do not pool latency across different request-scheduling regimes.
- Deliverables: PNG previews (300 dpi), PDF and editable-text SVG for each chart, multipage summary PDF, tidy CSVs, concise Chinese summary, figure captions and QA JSON.
- Typography: Arial/DejaVu Sans, white background, restrained teal/blue/neutral palette, clear direct labels and compact grid lines.
- Current export size: report-ready landscape pages, not claimed to be final journal-column layouts while the experiment is incomplete.
- QA: all expected task/condition cells present exactly once; numeric values match source; paired differences recomputed; count usable cells; inspect rendered figures for overlap and clipping; verify nonblank raster output and editable SVG text.

## Continuation Provenance
- User authorized a combined exploratory summary: retain completed V4 Flash outcomes and fill remaining conditions using deepseek-flash (user-specified V4.1 Flash), without repeated completed experiments.
- Keep a model field in each source row. Mark retained V4 values with [4]; use hollow points for mixed-backbone ablation pairs. Model differences are documented, not assumed equivalent.
- Preserve both positive and negative differences; allow the full negative range on ablation axes. Flag output errors without deleting affected returns.
- Restarted provider-failed attempts have separate IDs; archived partial evidence and usage are retained but never averaged into completed episode returns.
