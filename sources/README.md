# Source adapters

`pokemon-updater.py` is provider-independent. A source adapter is a Python
module exposing these callables:

```python
fetch_all_sets(version: str) -> list[dict]
fetch_set_details(set_id: str, version: str) -> dict
fetch_card_details(card_id: str, version: str, set_id=None, local_id=None) -> dict
detect_data_source(row: dict) -> str
transform_set_data(row: dict, version: str, source: str) -> dict
transform_card_data(row: dict, version: str, source: str) -> dict
transform_price_data(card_id: str, pricing: dict) -> list[dict]
```

The updater owns concurrency, retries, failure isolation, checkpoints,
sorting, hashing, and publication. An adapter owns provider requests and maps
provider responses to the stable catalog row schema.

To test another source without changing the updater:

```bash
python pokemon-updater.py --output ./output --source-module sources/other.py
```

Adapters must raise request exceptions for transient network failures so the
updater's shared retry and backoff policy can handle them.
