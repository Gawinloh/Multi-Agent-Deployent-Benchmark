# Benchmark Scenarios

Each YAML file in this directory defines one infrastructure-provisioning task
that the experiment runner feeds to the agent.

## Schema

```yaml
id: <string>            # unique identifier, e.g. smoke_test_001
description: <string>   # one-line human summary

request: |              # the natural-language prompt the agent receives
  ...

ground_truth:           # optional — used by result_logger.compute_scores()
  workload_class: <WorkloadClass enum>
  compliance: <ComplianceProfile enum>
  expected_postgres:
    shared_buffers_range: [<min>, <max>]   # acceptable range (strings)
    max_connections_range: [<min>, <max>]   # acceptable range (ints)
    ssl: <bool>
    password_encryption: <string>
  expected_nginx:
    server_tokens: <bool>
    ssl_protocols_min: <string>
  expected_redis:
    maxmemory_set: <bool>
    maxmemory_policy_acceptable: [<string>, ...]
    requirepass_or_acl: <bool>
  expected_cis_minimum_pass_rate: <float>  # 0.0–1.0
```

## Conventions

- **Ranges** express acceptable values, not exact targets. The scorer checks
  that the agent's output falls within the range.
- **Acceptable sets** (e.g. `maxmemory_policy_acceptable`) list all policies
  that count as correct for the given workload.
- **Minimum thresholds** (e.g. `expected_cis_minimum_pass_rate: 0.75`) are
  lower bounds — higher is better.
- Ground truth is optional. Scenarios without it still produce a result JSON
  with token counts, wall-clock time, and the validator report; they just
  skip the ground-truth scoring step.

## Scenarios

| File | ID | Description |
|------|----|-------------|
| `smoke_test.yaml` | `smoke_test_001` | Small dev environment, 5 users, 8 GB RAM, no compliance |
