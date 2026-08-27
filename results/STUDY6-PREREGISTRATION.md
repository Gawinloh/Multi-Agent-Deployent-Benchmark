# Study 6 pre-registration commitment

The predictions for Study 6 were written and hashed **before any run was
collected**. `docs/` is gitignored in this repository (it lives in a separate
private repo), so the SHA-256 digests are recorded here to fix the content and
make it verifiable from the public tree:

```
7e91f17a0b1bc620128aa8c0b9cad333e461d814cb8afaf96d9b78de552eda18  docs/planning/study6-prereg.md
f36aae2e33ca9a307665534728183caeba637ad5dea91afba65c12e8a1f708f2  docs/planning/study6-provenance.md
```

Verify with `shasum -a 256 docs/planning/study6-prereg.md`.

Registered at source commit `c2b0dcb` (Study 5 complete, working tree clean).

## What was registered

Study 6 repeats the Study 4 matrix with **exactly one variable changed**:
`gpt-4.1-nano-2025-04-14` → `gpt-4.1-mini-2025-04-14`, the same dated snapshot in
the same family. It asks whether the dissertation's central null is a fact about
decomposition or only about a model too weak to benefit from it.

- **P1** — both arms improve in absolute terms (correctness +0.02 or macro F1
  +0.05 in both). Expected; not the finding. Failure means the manipulation did
  not take.
- **P2** — *central*: the architectural difference stays null. A real difference
  requires |paired macro F1 delta| ≥ 0.05 with a CI excluding zero.
- **P3** — *directional commitment*: the multi-agent arm gains **more** than the
  single arm, i.e. coordination demands capability and decomposition helps weaker
  models **less**. Measured as the architecture × capability interaction
  (multi − single at mini) − (multi − single at nano) being positive.
- **P4** — the multi/single token ratio narrows below 1.45 (Study 4: 1.596).
- **P5** — capability-dependent failures fall to zero non-`finalised`
  terminations (Study 4: 2 of 160).

Wall-clock comparison against Study 4 is declared confounded in advance — Study 4
ran through machine sleep with ~1.55× dark-wake throttling — and carries no
verdict.

No source code changes: Study 6 needs only a different `--model`.
