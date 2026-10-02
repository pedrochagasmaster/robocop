# Dispatch SSH/TUI latency benchmark

`latency_probe.py` measures the user-visible latency of Dispatch through the same local tmux + authenticated SSH pane used by the production harness. It is intentionally non-destructive: it does not launch or cancel Jobs and does not DROP tables. Browser measurements may issue the normal read-only `SHOW TABLES`, `DESCRIBE`, and table-stat metadata queries that the Browse screen already performs.

Run the benchmark from the same workstation, VPN/network path, Edge node, terminal dimensions, and sample count for both revisions. Start and authenticate the tmux session first:

```bash
python -m tools.prod_tui tmux start --config tools/prod_tui/config.yaml
```

Measure the currently deployed baseline:

```bash
python -m tools.prod_tui latency \
  --config tools/prod_tui/config.yaml \
  --samples 7 \
  --json-report tools/prod_tui/reports/latency-baseline.json
```

After the candidate revision has been deployed through the normal Release Operator workflow, run the same probe and compare against the saved baseline:

```bash
python -m tools.prod_tui latency \
  --config tools/prod_tui/config.yaml \
  --samples 7 \
  --json-report tools/prod_tui/reports/latency-candidate.json \
  --baseline-report tools/prod_tui/reports/latency-baseline.json
```

The report records the deployed Git SHA, raw samples, median and p95 latency, tmux `capture-pane` overhead, and the terminal size. It measures New Job navigation, History navigation, Browser first paint, Browser checkbox interaction, Job Detail search typing when a recent Job is available, and New Job SQL-path typing. Browser opening is capped at three samples because it invokes real read-only Impala metadata work.

The raw numbers include local tmux capture/poll overhead as well as SSH, terminal rendering, Textual processing, and Edge-node work. Use the reported `capture_overhead_ms` to understand the measurement floor; compare baseline and candidate medians rather than treating the values as pure network RTT.

Reports under `tools/prod_tui/reports/` are ignored by git and should not be committed. Run the probe on each Edge node separately if both nodes are part of the production validation.
