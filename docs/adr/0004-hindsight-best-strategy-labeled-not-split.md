# The "best" strategy is hindsight-selected and mitigated by labeling, not a train/test split

The report shows, per ticker, the single best-performing strategy of the 16, chosen by
CAGR over the full window. This is **in-sample optimization**: the "best" is the luckiest
of 16 tries seen with full hindsight, and it will overstate forward performance. We
mitigate this with an **honest bilingual label** ("best-fit over the full history —
hindsight-selected, not a forward recommendation") rather than an out-of-sample train/test
split. The split would be more rigorous, but the ~2-year history barely covers the sma240
warm-up (~240 bars), leaving a test slice too short and noisy to trust — the rigor would be
illusory. For a personal, exploratory tool where the user knows the number is a ceiling,
clear labeling is the honest and proportionate choice.

## Considered options

- **Train/test split**: rigorous in principle, but the short history makes the held-out
  slice tiny and noisy.
- **No "best," show grid spread vs B&H only**: avoids the optics but contradicts the
  chosen compact per-ticker output.
- **Show in-sample best + honest label** (chosen): keeps the useful "ceiling" number while
  being explicit that it is not predictive.
