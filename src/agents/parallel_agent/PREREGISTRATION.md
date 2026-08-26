# Study 5 pre-registration commitment

The predictions for Study 5 were written and hashed **before any run was
collected**, and before this architecture had produced a single data point.
`docs/` is gitignored in this repository (it lives in a separate private repo),
so the SHA-256 digests are recorded here to fix the content and make it
verifiable from the public tree:

```
653e7d8b6f7ad0018b1ee056529c8325716da28797d2541eb59584f5685dd638  docs/planning/study5-prereg.md
084d6466f9aa22cb60a719310ca4bf5e8989608d8f56dbae0b358f227f68de67  docs/planning/study5-provenance.md
```

Verify with `shasum -a 256 docs/planning/study5-prereg.md`.

Registered at source commit `bc69d10` (Study 4 complete, working tree clean).

## Summary of what was registered

Study 5 is an **exploratory** third architecture: a manager decomposes the
request once, per-service agents run concurrently, and a merge step arbitrates
the shared host memory budget before validation. It exists to test whether the
dissertation's null result was an artefact of having compared against only a
star topology.

- **P1** — parallel will not improve correctness over the single agent
  (threshold: macro F1 delta ≥ +0.05 with a CI excluding zero).
- **P2** — parallel will use *more tokens* than the star arm but *less
  wall-clock*. Both halves scored independently.
- **P3** — RabbitMQ recall should resemble the other arms (0.00–0.20);
  a large move means the manager prompt, not the topology, is doing the work.
- **P4** — arbitration will need to reduce agent requests in >50% of runs with
  two or more memory-reserving services.

The pre-registration discloses that the protocol-mandated smoke run was observed
before registration, and that it pointed **against** both P2 and P4. The
predictions were registered in their original directional form anyway.

The single and multi arms are unchanged: 620 pre-existing tests pass, and the
Study 1–4 datasets are untouched.
