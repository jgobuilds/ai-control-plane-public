# Scope-hierarchy templates

Ready-made `context/scopes.json` starting points. The installer applies one:

```sh
python scripts/install.py --scope-template consulting   # or solo | corp | keep
```

| Template | Shape | Use when |
|---|---|---|
| `solo.json` | one personal scope, pooled | a single practitioner, no confidentiality boundaries |
| `corp.json` | enterprise › department › team › personal, no walls | internal org; departments handle block-PII (dedicated runners), teams share their department's runner |
| `consulting.json` | enterprise › consulting › siloed clients (+ ethical walls) + internal | a firm with competing clients that must never share a runner |

After editing the tree, regenerate the derived artifacts and re-check invariants:

```sh
python scripts/gen_compose.py && python scripts/gen_usecase_register.py \
  && python scripts/gen_compliance_map.py && python scripts/conformance_test.py
```

Each template validates against the conformance suite as shipped. Rename the
example nodes (`client-a`, `data-team`, `you`, …) to your own; add `owner`,
`useCase`, `status`, and `agentName` fields per node as your governance matures.
