# Card Database

The catalog is generated from public card-data sources and published as bulk
JSON files.

## Files

```text
pokemon/manifest.json
pokemon/data-english/sets.json
pokemon/data-english/cards.json
pokemon/data-english/prices.json
pokemon/data-japanese/sets.json
pokemon/data-japanese/cards.json
pokemon/data-japanese/prices.json
mtg/manifest.json
mtg/data-english/sets.json
mtg/data-english/cards/00.json ... 1f.json
mtg/data-english/prices/00.json ... 1f.json
mtg/data-japanese/sets.json
mtg/data-japanese/cards/00.json ... 1f.json
mtg/data-japanese/prices/00.json ... 1f.json
```

Each game's `manifest.json` is written only after a complete export. Its version and file
hashes are the publication boundary: consumers must not use in-progress files
without a matching manifest.

## Generate locally

```bash
python -m pip install -r requirements.txt
python pokemon-updater.py --output .
python mtg-updater.py --output .
```

Card requests run concurrently and completed work is checkpointed in the
background every 10 minutes. Use `--checkpoint-interval 0` to disable
checkpoints.

## Pokémon

The Pokémon catalog contains international English and Japanese cards. It
stores set details, card rules and attributes, variants, legalities, artwork,
and normalized market prices. English price data includes TCGPlayer prices in
USD and Cardmarket prices in EUR; Japanese price data currently includes
Cardmarket prices in EUR. Run a
price-only refresh without rebuilding sets and cards:

```bash
python pokemon-updater.py --output . --prices-only
```

Pokémon uses one bulk file each for sets, cards, and prices in both language
directories. `pokemon/manifest.json` lists their counts and SHA-256 hashes.

## Magic: The Gathering

The MTG catalog contains English and Japanese card printings. It stores gameplay
fields such as mana cost, localized and Oracle rules text, card faces, colors,
formats/legalities, artists, images, and external IDs. Run a price-only MTG
refresh without rebuilding sets and cards:

```bash
python mtg-updater.py --output . --prices-only
```

MTG cards and prices use 32 deterministic shards. `mtg/manifest.json` lists
every shard and its SHA-256 hash.

## Source adapters

Adapters live in `sources/` and expose the functions documented in
[`sources/README.md`](sources/README.md). Add another adapter
there and pass its file with `--source-module` to generate the same stable
catalog schema from another provider.

## Automation

The `Update Card Database` workflow runs twice daily. Pokémon prices refresh on
every run, while sets and cards rebuild when the TCGdex release
changes. MTG prices refresh daily, while the full MTG set/card catalog refreshes
every five days.

## Disclaimer

Cardly is an unofficial project and is not affiliated with or endorsed by The
Pokémon Company, Nintendo, Creatures Inc., GAME FREAK inc., or Wizards of the
Coast. Pokémon and its related names and marks belong to their respective
owners. Magic: The Gathering is © Wizards of the Coast.

Cardly is not produced by, endorsed by, supported by, or affiliated with
Scryfall. Card data, artwork, trademarks, and pricing remain attributed to their
respective providers and owners.

## Sources

- [**TCGdex**](https://tcgdex.dev/): Pokémon set and card data, images,
  and pricing.
- [**Scryfall**](https://scryfall.com/docs/api): Magic card data and images.
- [**TCG Tracking**](https://openapi.tcgtracking.com/): Magic set, product,
  SKU, and pricing data.

## Data disclaimer

The card data and prices are provided **as is** for informational purposes.
They are not guaranteed to be accurate, complete, current, or continuously
available and should not be treated as an appraisal or as financial or
investment advice. Verify important information with the original source or
marketplace before making a purchase, sale, trade, or other decision. To the
maximum extent permitted by law, the project contributors are not
responsible for losses or damages resulting from use of or reliance on this
data.

## License

The original source code in this repository is available under the
[MIT License](LICENSE). That license does not grant rights to third-party card
data, images, artwork, prices, names, logos, or trademarks; those materials
remain subject to their respective owners' and providers' terms.
