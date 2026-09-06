# Cardly Pokemon Card Database

The catalog is generated from pluggable source adapters and published as bulk
JSON files. TCGdex is the initial source for both international and Japanese
cards. Consumers should read `manifest.json` first and verify each listed
file's SHA-256 hash before importing it.

## Files

```text
manifest.json
data/sets.json
data/cards.json
data/prices.json
data-japanese/sets.json
data-japanese/cards.json
data-japanese/prices.json
```

`manifest.json` is written only after a complete export. Its version and file
hashes are the publication boundary: consumers must not use in-progress files
without a matching manifest.

## Generate locally

```bash
python -m pip install -r requirements.txt
python database-updater.py --output .
```

Card requests run concurrently and completed work is checkpointed in the
background every 10 minutes. Use `--checkpoint-interval 0` to disable
checkpoints.

## Source adapters

Adapters live in `sources/` and expose the functions documented in
[`sources/README.md`](sources/README.md). Add another adapter
there and pass its file with `--source-module` to generate the same stable
catalog schema from another provider.

## Automation

The `Update Card Database` workflow runs twice daily and can also be
started manually. It rebuilds sets and cards when the upstream TCGdex release
changes; otherwise it refreshes prices from the existing card files.

