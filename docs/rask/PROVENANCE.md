# rask's published contract, vendored

The workbench is a client of a rask lakehouse and reaches it only through what rask publishes:

- the catalog's and lineage's HTTP doors, `catalog-openapi.json` and `lineage-openapi.json`;
- the events it emits, `COMMIT-EVENTS.md`;
- what the catalog enforces on a write (who writes, the run rule and its `x-rask-*` headers, the reserved keys),
  `WRITE-GOVERNANCE.md`.

Data goes through stock `lance_namespace` and pylance on credentials the catalog vends.

These four files are copied unchanged from `AI-Riksarkivet/rask` at `4f83dba0d` (`docs/`, 2026-11-18). Links inside
them that point at rask's own tree (`adr/`, `lance_docs/`) resolve in rask's repository, not here. Refresh them with:

```bash
for f in catalog-openapi.json lineage-openapi.json COMMIT-EVENTS.md WRITE-GOVERNANCE.md; do
  git -C <rask checkout> show origin/main:docs/$f > docs/rask/$f
done
bun --cwd=frontend run gen:types
```

Then record the new sha here.
