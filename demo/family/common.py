"""Shared deterministic data and evidence helpers for the family demo."""
from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from urllib.parse import urlparse

AGENTS = ("yeil", "ikto", "tsisdu", "kukla", "ollie")
FAMILY_POOL = "family-room"
SHARED_MEMBERS = ("yeil", "ikto", "tsisdu")
FACTS = {
    "yeil": "Yeil keeps the brass telescope beside the cedar bookcase.",
    "ikto": "Ikto's emergency tea is jasmine in the blue kitchen tin.",
    "tsisdu": "Tsisdu calibrates the garden sensor every second Tuesday.",
    "kukla": "Kukla's archive key is stored behind the amber field notebook.",
    "ollie": "Ollie names the upstairs reading lamp Firefly.",
}
SHARED_FACTS = {
    "yeil": "The family picnic basket is checked by Yeil before sunrise.",
    "ikto": "Ikto brings the striped blanket to the family picnic.",
    "tsisdu": "Tsisdu charges the family-room weather radio on Fridays.",
    "kukla": "Kukla keeps the family-room board-game score sheet.",
    "ollie": "Ollie waters the family-room fern on Mondays.",
}
PARAPHRASES = {
    "yeil": "Where is Yeil's brass astronomy instrument kept?",
    "ikto": "Which container holds Ikto's backup jasmine tea?",
    "tsisdu": "When does Tsisdu adjust the outdoor sensor?",
    "kukla": "Where can Kukla's archive key be found?",
    "ollie": "What does Ollie call the lamp in the upstairs reading area?",
}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_isolated_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"refusing non-loopback endpoint for isolated demo: {url}")


def validate_proxy_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1", "100.86.220.115"}:
        raise ValueError(f"refusing non-demo proxy endpoint: {url}")


def build_manifest(run_id: str, seed: int = 2026) -> dict:
    rng = random.Random(f"{seed}:{run_id}")
    distractors = {}
    scopes = (*AGENTS, FAMILY_POOL)
    nouns = ("lantern", "notebook", "thermometer", "mug", "compass", "calendar", "radio", "basket")
    places = ("attic", "pantry", "workshop", "hallway", "porch", "study", "garage", "cellar")
    for scope in scopes:
        values = []
        for index in range(20):
            nonce = rng.randrange(10**8, 10**9)
            content = f"Distractor {index:02d} for {scope}: the {rng.choice(nouns)} marker {nonce} is in the {rng.choice(places)}."
            values.append({"id": f"family-{run_id}-{scope}-d{index:02d}", "type": "distractor", "content": content})
        distractors[scope] = values
    return {
        "schema": "falda-family-run/v1", "run_id": run_id, "seed": seed,
        "agents": {a: {"private_atom_id": f"family-{run_id}-{a}-private",
                       "shared_atom_id": f"family-{run_id}-{a}-shared"} for a in AGENTS},
        "pool": FAMILY_POOL, "distractors": distractors,
        "test_cases": ["exact", "semantic", "negative", "spoof", "shared", "T0", "T1", "T2", "T3", "delete-ghost", "latency"],
        "provenance_limitation": {
            "classification": "API_OBSERVABILITY_GAP",
            "detail": "Search returns a fused score but does not expose dense-vs-lexical channel provenance; score is recorded without inventing provenance.",
        },
    }


def make_receipt(agent: str, run_id: str, private_fact: str, shared_fact: str | None,
                 token: str, checks: list[dict]) -> dict:
    unsigned = {
        "schema": "falda-family-participant-receipt/v1", "agent": agent, "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "facts": {"private_sha256": sha256_text(private_fact),
                  "shared_sha256": sha256_text(shared_fact) if shared_fact else None},
        "token_fingerprint": sha256_text(token)[:12], "checks": checks,
    }
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
    return {**unsigned, "receipt_sha256": sha256_text(canonical)}
