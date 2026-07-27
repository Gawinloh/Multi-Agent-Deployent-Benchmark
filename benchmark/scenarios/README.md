# Benchmark Scenarios

Each YAML file in this directory defines one infrastructure-provisioning task
that the experiment runner feeds to the agent.

> **These files are generated.** Ground truth is derived from documented rules
> in `benchmark/derive_scenarios.py`. Edit the rules there and regenerate with
> `python benchmark/derive_scenarios.py`; do not edit the YAML by hand. The
> derivation and its sources are written up in
> `docs/methodology/scenario-derivation.md`.

## Schema

```yaml
id: <string>            # unique identifier
description: <string>   # one-line human summary

request: |              # the natural-language prompt the agent receives
  ...

derivation:             # provenance, not consumed by the runner
  rationale: <string>   # why this scenario is in the set
  stated_facts: {...}   # the facts the request states, used by the generator

ground_truth:           # consumed by result_logger.compute_scores()
  workload_class: <WorkloadClass enum>
  compliance: <ComplianceProfile enum>
  expected_postgres: {...}
  expected_nginx: {...}
  expected_redis: {...}
  expected_cis_minimum_pass_rate: <float>  # 0.0-1.0
```

## Comparison shapes

The scorer infers the comparison from the shape of each ground-truth key:

| Key suffix | Comparison | Example |
|---|---|---|
| `_range` | inclusive bounds, numeric or size strings | `shared_buffers_range: ["1.2GB", "2.8GB"]` |
| `_acceptable` | membership in a list | `maxmemory_policy_acceptable: [allkeys-lru, ...]` |
| none | equality | `ssl: true` |

Size values are compared in bytes, never as strings, so `2GB` and `2048MB` are
equal. Keys resolve to their location in the generated `final_spec` through
`GROUND_TRUTH_PARAMETER_MAP` in `src/experiment/result_logger.py`. A key with no
entry in that table is recorded as `unmapped` and excluded from the denominator
rather than silently counted, and `tests/test_benchmark/test_scenarios.py` fails
if any shipped scenario contains one.

`expected_cis_minimum_pass_rate` is a lower-bound threshold reported alongside
`cis_pass_rate`; it is not part of the correctness fraction.

## Scoring semantics

- A parameter absent from `final_spec` counts as a **failure**.
- A run that never finalised produces **no** `correctness` key at all, rather
  than 0.0. Absent and wrong are different outcomes.
- `parameters_checked` varies by scenario (13 to 18 here) because compliance
  profiles add assertions, so it is recorded in every run JSON alongside the
  fraction.

## Scenarios

| File | ID | Workload | Compliance | Host | Users |
|---|---|---|---|---|---|
| `smoke_test.yaml` | `smoke_test_001` | BALANCED | NONE | 4 GB / 2 vCPU | 5 |
| `scenario_002_oltp_small.yaml` | `scenario_002_oltp_small` | OLTP | NONE | 4 GB / 2 vCPU | 25 |
| `scenario_003_oltp_gdpr.yaml` | `scenario_003_oltp_gdpr` | OLTP | GDPR_UK | 8 GB / 4 vCPU | 60 |
| `scenario_004_olap_large.yaml` | `scenario_004_olap_large` | OLAP | NONE | 8 GB / 4 vCPU | 10 |
| `scenario_005_cache_heavy.yaml` | `scenario_005_cache_heavy` | CACHING_HEAVY | NONE | 6 GB / 4 vCPU | 200 |
| `scenario_006_balanced_hipaa.yaml` | `scenario_006_balanced_hipaa` | BALANCED | HIPAA | 6 GB / 4 vCPU | 30 |
| `scenario_007_oltp_pci.yaml` | `scenario_007_oltp_pci` | OLTP | PCI_DSS | 8 GB / 4 vCPU | 100 |
| `scenario_008_olap_gdpr.yaml` | `scenario_008_olap_gdpr` | OLAP | GDPR_UK | 8 GB / 4 vCPU | 8 |
| `scenario_009_cache_constrained.yaml` | `scenario_009_cache_constrained` | CACHING_HEAVY | NONE | 2 GB / 2 vCPU | 80 |
| `scenario_010_balanced_large.yaml` | `scenario_010_balanced_large` | BALANCED | NONE | 8 GB / 4 vCPU | 50 |
| `scenario_011_oltp_hipaa_constrained.yaml` | `scenario_011_oltp_hipaa_constrained` | OLTP | HIPAA | 2 GB / 2 vCPU | 40 |
| `scenario_012_olap_pci.yaml` | `scenario_012_olap_pci` | OLAP | PCI_DSS | 6 GB / 4 vCPU | 15 |

Scenarios 009 and 011 are deliberate tension cases where the stated workload
does not fit the host comfortably.
