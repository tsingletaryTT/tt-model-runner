# app/ad_facts.py
# SPDX-License-Identifier: Apache-2.0
"""Ad unit content: static Did-you-know facts and dynamic model recommendations."""
import json
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from compat_catalog import CompatCatalog
    from model_catalog import ModelCatalog

log = logging.getLogger(__name__)

# Path to the JSON data file relative to this module's location.
# Layout: app/ad_facts.py → project root → data/did-you-know.json
_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "did-you-know.json"


def _load_did_you_know() -> List[Dict[str, str]]:
    """Load the 'Did you know?' card list from data/did-you-know.json.

    Returns the ``cards`` list from the JSON file.  Falls back to an empty
    list (with a logged warning) if the file is missing or malformed so the
    rest of the app continues to run without crashing.
    """
    try:
        with _DATA_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        cards = data["cards"]
        if not isinstance(cards, list):
            raise TypeError(f"'cards' must be a list, got {type(cards).__name__}")
        return cards
    except FileNotFoundError:
        log.warning("did-you-know.json not found at %s — no static cards loaded", _DATA_FILE)
        return []
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("Failed to parse did-you-know.json: %s — no static cards loaded", exc)
        return []


# Populated once at import time from the JSON data file.
DID_YOU_KNOW: List[Dict[str, str]] = _load_did_you_know()


def get_model_cards(
    catalog: Optional["ModelCatalog"],
    device_type: Optional[str],
    compat_catalog: Optional["CompatCatalog"] = None,
) -> List[Dict[str, str]]:
    """Return model-recommendation cards for the current device.

    Sources: existing ModelCatalog (tag='Model') + CompatCatalog for
    tt-forge/tt-metal extras that aren't in the server catalog (tag='Ecosystem').
    """
    cards: List[Dict[str, str]] = []

    if catalog and device_type:
        compatible = catalog.get_compatible([device_type]).all_entries()
        prod = [e for e in compatible if e.status == "PRODUCTION"]
        sample = random.sample(prod, min(4, len(prod))) if prod else compatible[:4]
        for e in sample:
            size_str = f"{e.param_count:.0f}B params  ·  " if e.param_count else ""
            cards.append({
                "headline": e.display_name,
                "body": (
                    f"{size_str}Engine: {e.inference_engine}  ·  "
                    f"Device: {e.device_type}  ·  Status: {e.status}"
                ),
                "tag": "Model",
                "model_id": e.model_name,   # used by AdUnit "Find in Rail" link
            })

    if compat_catalog and device_type:
        server_names: set = set()
        if catalog:
            server_names = {e.display_name.lower() for e in catalog.all_entries()}
        extra = []
        for sw in ("tt-forge", "tt-metal"):
            for e in compat_catalog.get_for_hardware(device_type, software=sw):
                if e.display_name.lower() not in server_names and e.model_description:
                    extra.append((e, sw))
        random.shuffle(extra)
        for e, sw in extra[:3]:
            cards.append({
                "headline": e.display_name,
                "body": (
                    f"{e.model_description}\n"
                    f"Available via {sw} on {device_type} — run via Developer Image."
                ),
                "tag": "Ecosystem",
            })

    return cards


def get_all_cards(
    catalog: Optional["ModelCatalog"],
    device_type: Optional[str],
    compat_catalog: Optional["CompatCatalog"] = None,
) -> List[Dict[str, str]]:
    """Return a shuffled pool of static facts + model recommendations."""
    cards = list(DID_YOU_KNOW) + get_model_cards(catalog, device_type, compat_catalog)
    random.shuffle(cards)
    return cards
