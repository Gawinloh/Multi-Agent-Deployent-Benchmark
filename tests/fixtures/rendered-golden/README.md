# Golden rendered output (pre-catalog)

Every file the harness deploys for the canonical three-service spec,
captured from commit `5a3182c` — *before* the L1 service-catalog
refactor — and asserted byte-for-byte by
`tests/test_services/test_compose_rendering.py`.

The point is regression, not documentation: L1 rewired how these files
are produced (per-service fragments assembled from
`src/services/catalog.py` instead of one hand-written template), and the
deployed nginx + PostgreSQL + Redis stack had to come out unchanged.
Study 1's 72 runs are frozen at `dataset-nano-n3`; a byte that moved
here would mean they were no longer reproducible.

Variants, all from `make_spec()` in `tests/test_validator/test_docker_runner.py`:

| directory | spec |
|---|---|
| `nossl/` | `make_spec()` — postgres SSL off |
| `ssl/` | `make_spec(ssl=True)` — adds the cert staging mounts and entrypoint |
| `nopass/` | `make_spec(requirepass=None)` — redis healthcheck without `-a` |
| `generate_config/` | the `generate_config` deterministic path over `_STACKSPEC_EXAMPLE` |

Two values that are random or host-dependent in production are pinned so
the comparison means something: `run_id` is `fixture0001`, the generated
`POSTGRES_PASSWORD` is `fixture-postgres-password`, and host-memory
clamping is disabled (it depends on the Docker daemon's reported RAM).

**Do not regenerate these files to make a failing test pass.** A diff
here is the test doing its job. Update them only alongside a deliberate,
documented change to what the harness deploys.

## Change log

Every *deployed* file above is still byte-for-byte what commit `5a3182c`
produced. One file has been regenerated:

- **`generate_config/spec.json`**, when the RabbitMQ service definition
  was added. It is a dump of the `StackSpec` object, not a deployed
  artefact, so a new optional field appears in it as `"rabbitmq": null`.
  The four `.conf` files it sits beside, and all three `docker-compose.yml`
  variants, were unchanged by that work — which is the property the
  three-service stack actually has to preserve.

- **`generate_config/spec.json` again, plus a new
  `generate_config/rabbitmq.conf`**, at L2. The input to this fixture is
  `_STACKSPEC_EXAMPLE`, which is prompt content: it gained a rabbitmq
  block so the completion model knows the shape of a queue config, and
  `requirements` gained `selected_services`. Both show up in the dump.
  The `nossl/`, `ssl/` and `nopass/` trees — the ones that represent what
  Docker actually runs for the Study 1 stack — were untouched again.
