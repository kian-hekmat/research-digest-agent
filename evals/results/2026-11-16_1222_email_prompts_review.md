# Phase 2d round 1: owner review

Files: `2026-11-16_1222_email_prompts_overviews_review.json` (overviews) and `2026-11-16_1222_email_prompts_blurbs_blind.json` (blurb pairs). Don't open `2026-11-16_1222_email_prompts.json` until the pairs are done: it holds the A/B key.

How to fill in: replace each `_` and leave the rest. Notes are optional; a few words is plenty.

## Overviews

For each overview, check every `[arXiv ID]` claim against that paper's abstract in `offered`, and check the picks' hooks the same way.

- **Errors**: how many claims the abstracts contradict or don't contain (0, 1, 2…). Gate: at most 1 in total across all 17.
- **Bad cites**: the IDs whose abstract doesn't support the sentence citing them, or `none`. Gate: none at all.
- **Overclaim**: `y` if anything is stated more strongly than the abstract supports, otherwise `n`. Gate: none.
- **Useful**: optional, 1–5: would this overview tell you something about the week?

| # | Topic | Week | Errors | Bad cites | Overclaim | Useful | Note |
|---|---|---|---|---|---|---|---|
| 0 | RLHF | 2026-09-14 → 2026-09-21 | 2 | none | y | 3 | Invented framing: 'cheaper to supervise' and 'A third line pushes supervision into retrieval and memory' (RELEX gives recipes to human learners). Cited claims accurate. 125 words. |
| 1 | PINNs | 2026-09-14 → 2026-09-21 | 1 | 2609.11628 | y | 4 | Lead 'pushed hardest' isn't shown by a set dominated by methods papers; 'recovered a conductivity law with under-10% error' [2609.11628] omits that this was synthetic data. 136 words. |
| 2 | PINNs | 2026-09-21 → 2026-09-28 | 1 | 2609.24746, 2609.23493 | y | 4 | 'contrastive learning improved ... across data sizes': no gain reported for contrastive learning. Falkner-Skan numbers are one case, one seed; pick 'sub-micro error ... ablations showing where the gain comes from' overstates. |
| 3 | PINNs | 2026-09-28 → 2026-10-05 | 1 | 2609.40008 | y | 4 | Lead 'not just a residual' contradicts 'The physics residual acts as a safety fence'. Pick 'built into the network' vs 'enter through the loss functional'. |
| 4 | PINNs | 2026-10-05 → 2026-10-12 | 2 | 2610.02182, 2610.01874, 2610.04405 | y | 3 | Invented lead; 'conformal and multiphysics formulations' fits neither cite and hides that 2610.01874 is quantum ML. 'beat Adam, Muon, and SOAP' drops 'often'; pick says 'the core PINN failure mode'. |
| 5 | PINNs | 2026-10-12 → 2026-10-19 | 0 | 2610.14844 | y | 4 | Changed on second pass: 'couples a common Gaussian germ to ... random neuron locations' isn't stated. Holography figures exact. Covers 2 of 13 papers. |
| 6 | PINNs | 2026-10-19 → 2026-10-26 | 2 | 2610.30179, 2610.27193 | y | 4 | 'outperformed PINN baselines and CFD reconstructions': no CFD comparison in the abstract. Pick 'tests three parameterizations' (abstract: four). Strong content, 144 words. |
| 7 | PINNs | 2026-10-26 → 2026-11-02 | 2 | 2610.33133 | y | 5 | Lead 'pushed hardest' unsupported. 'matched commercial TCAD accuracy' inflates 'comparable to traditional simulation software'; pick invents 'hard-constrained solver'. |
| 8 | PINNs | 2026-11-02 → 2026-11-09 | 0 | 2611.02084, 2611.03485 | y | 5 | 'Love numbers with relative errors below 10^-5' (overview and pick) is only the 'finite dissipative response'; e15 '0.15%-0.51%' is the manufactured-data best case (FE data 1.19%). |
| 9 | PINNs | 2026-11-09 → 2026-11-16 | 0 | none | n | 4 | Clean; survived second pass. Covers 2 of 19 papers and says nothing about what PINNeval found. |
| 10 | Numerical Analysis | 2026-09-28 → 2026-10-05 | 0 | 2609.40015, 2609.35716 | y | 4 | Ray-transform 'Lipschitz stability' stated without 'sufficiently fine' partition; 'zero-residual non-solutions' overstates an a.e. solution that is 'not the viscosity solution'. 133 words. |
| 11 | Numerical Analysis | 2026-10-05 → 2026-10-12 | 1 | none | y | 4 | All cites check; lead 'dominated by structure-preserving discretizations and reliability guarantees' isn't shown by the 25-paper set. 126 words. |
| 12 | Numerical Analysis | 2026-10-12 → 2026-10-19 | 1 | 2610.13245, 2610.19748, 2610.22255 | y | 4 | 'A companion line of work pushes finite-volume methods' cites a textbook chapter. 'only near shocks' is inferred; pick's 'first ... well-posedness results' is unhedged and extended. 132 words. |
| 13 | Numerical Analysis | 2026-10-19 → 2026-10-26 | 0 | 2610.30492, 2610.31026 | y | 4 | Pick 'recover profiles with near machine precision' holds for Burgers only. The RKHS model-reduction clause sits under [2610.31026] but belongs to the uncited 2610.28224. |
| 14 | Numerical Analysis | 2026-10-26 → 2026-11-02 | 1 | 2610.36266 | y | 4 | Lead 'dominated by ... reduced-cost uncertainty quantification' unsupported. 'the first rigorous error bounds' drops 'to the best of our knowledge'. |
| 15 | Numerical Analysis | 2026-11-02 → 2026-11-09 | 1 | none | y | 4 | All cites check; lead 'emphasize provable accuracy for high-dimensional quadrature' rests on 1 of 25 papers. 137 words. |
| 16 | Numerical Analysis | 2026-11-09 → 2026-11-16 | 1 | 2611.16238, 2611.13523 | y | 3 | 'centers on two themes': one paper each. Pick extends Allen-Cahn-only proofs to 'stiff phase-field PDEs'; other pick credits prior work's root-finding reformulation. |

## Blurb pairs

In each pair, A and B describe the same paper; one is today's summary and one is the new blurb, in random order. Judge them against the abstract that's included.

- **Prefer**: `A`, `B` or `tie`: which would make you more likely to read the paper, if you can trust it? Gate: the new blurb is preferred overall.
- **Overclaim**: `A`, `B`, `both` or `none`: did either say more than the abstract supports? Gate: the new side never does.

| # | Paper | Prefer | Overclaim | Note |
|---|---|---|---|---|
| 0 | 2610.28873 | A | B | B drops the Dmv exception and the fmv/fint trade-off ('reduced parameter RMSE'); A keeps both and is more specific. |
| 1 | 2610.20041 | A | B | B's first line 'recovers ... L, C, and parasitics', but only the ESR accuracy is reported. A is vague but faithful, so it narrowly wins. |
| 2 | 2609.23493 | B | both | Both generalize a single (β0,β1) case. B states the case and the single-seed caveat with real numbers, though 'b2_0,b2_1)' is garbled. |
| 3 | 2609.17119 | A | none | Both faithful; A is tighter. B is padded and opens with 'This paper examines'. |
| 4 | 2609.38101 | B | A | A's first line gives the n^2 regime 'for m=~O(n)' without 'polynomially conditioned'. B is faithful. |
| 5 | 2610.30492 | A | both | A's first line claims '10^-14 accuracy' for all profiles (Burgers only); B drops 'in a local region' for Boussinesq. A is more specific. |
| 6 | 2610.39738 | B | none | Both faithful; B gives the explicit rate and ε range (raw notation). |
| 7 | 2609.22992 | B | none | Both faithful; B gives 23.4 K, 96.6% and the SHAP finding. Garbled 'liquid/supercooled-glass'. |
| 8 | 2610.36349 | A | both | A's first line drops 'maximally continuous displacement splines'; B's 'good convergence' isn't stated and it omits the factor-two result. |
| 9 | 2610.12228 | A | none | Both faithful on stability; B mis-scopes to 'symmetric unitary matrices' and opens with 'The paper studies'. |
| 10 | 2610.38814 | A | both | B credits the gain to 'Regional sampling'; the abstract says 'do not isolate regional sampling'. A's 'depends strongly' is a minor stretch. |
| 11 | 2611.07424 | A | B | B: 'improved stability over Monte Carlo' isn't in the abstract and it drops the summability condition; raw LaTeX. |
| 12 | 2610.39893 | B | none | Both faithful; B is more specific (method, scope, bounds) despite a long run-on sentence. |
| 13 | 2610.02182 | B | A | A drops 'often' from 'lower losses than Adam, Muon, and SOAP'; B keeps it. |
| 14 | 2610.12336 | A | B | B's 'much cheaper to compute' is unmeasured; A is concrete (banded example, drop-in API). |
| 15 | 2610.09797 | B | none | Both faithful; B adds exact energy conservation and the constitutive structure. |
| 16 | 2610.32668 | A | none | Both faithful; A names which parameters are and aren't identifiable. |
| 17 | 2610.04405 | B | A | A says 'speed up training and inference' (inference isn't stated); B is faithful. |
| 18 | 2610.27193 | A | B | B's 'drive the residual to zero' vs the abstract's 'small residuals'; A is faithful. |
| 19 | 2611.08559 | A | both | B invents 'differ substantially'; A's 'reproduce established benchmark settings' is a smaller stretch. |
