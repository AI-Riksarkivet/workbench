# rask's published contract, vendored

The workbench is a client of a rask lakehouse and reaches it only through what rask publishes:

- the catalog's and lineage's HTTP doors, `catalog-openapi.json` and `lineage-openapi.json`;
- the events it emits, `COMMIT-EVENTS.md`.

Data goes through stock `lance_namespace` and pylance on credentials the catalog vends.

These three files are copied unchanged from `AI-Riksarkivet/rask` at `639b02dac` (`docs/`, 2026-11-14). Refresh them with:

```bash
for f in catalog-openapi.json lineage-openapi.json COMMIT-EVENTS.md; do
  git -C <rask checkout> show origin/main:docs/$f > docs/rask/$f
done
bun --cwd=frontend run gen:types
```

Then record the new sha here.
