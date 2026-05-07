"""
clinical_taxonomy.py - injury_type -> body_region mapping.

Single source of truth for "what part of the body does this patient's injury
involve?" Used by:

  * chat_protocol_drafter  - injects body_region into the system prompt as a
    hard constraint so the model can't propose cross-region exercises.
  * researcher / planner   - same anchoring, plus deterministic post-LLM
    validation (any exercise whose body_region doesn't match raises).
  * protocol_loader        - filters legacy YAML fallbacks by region (or
    refuses them entirely for authenticated patients).

Two layers:

  1. INJURY_TO_BODY_REGION dict - explicit, cheap, deterministic. Covers the
     enumerated injury types the intake modal collects plus common variants.
     This is the fast path; ~99% of patients land here.

  2. classify_freetext()      - Haiku fallback for typos / free-text intake
     ("twisted my ankle last week"). Cached aggressively per process so the
     same string never costs more than one API call.

The 6 body_region values mirror the `injury_category` enum already defined in
protocols/schema.json: knee, ankle, shoulder, low_back, hamstring, elbow.
"multi" is reserved for multi-region presentations (e.g. polytrauma) and
is the only value that bypasses the post-LLM validator - clinicians must
review those drafts manually.

PHI hygiene: classify_freetext logs only a hash of the input string. Never
the raw freetext, never the patient name.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

BodyRegion = Literal[
    "knee", "ankle", "shoulder", "low_back", "hamstring", "elbow", "multi"
]

# Allowed regions surfaced via this module. Mirrored against
# protocols/schema.json `injury_category` enum + "multi". When this list
# changes, update the schema enum and the post-LLM validator wiring.
ALLOWED_REGIONS: frozenset[str] = frozenset(
    {"knee", "ankle", "shoulder", "low_back", "hamstring", "elbow", "multi"}
)


# Explicit map keyed by lower-cased, stripped injury_type. Order doesn't
# matter; we look up exact key first, then substring keys, then fall back to
# the LLM classifier. Keep entries lower-case and lowercase-only-letters/spaces
# so user_store._infer_injury_category and this map stay in sync.
INJURY_TO_BODY_REGION: dict[str, BodyRegion] = {
    # --- knee ---
    "post-acl reconstruction": "knee",
    "acl reconstruction": "knee",
    "acl tear": "knee",
    "acl repair": "knee",
    "mcl sprain": "knee",
    "mcl tear": "knee",
    "meniscus tear": "knee",
    "meniscectomy": "knee",
    "patellofemoral pain": "knee",
    "patellar tendinopathy": "knee",
    "knee replacement": "knee",
    "tkr": "knee",
    "knee sprain": "knee",
    "knee pain": "knee",

    # --- ankle ---
    "lateral ankle sprain": "ankle",
    "medial ankle sprain": "ankle",
    "high ankle sprain": "ankle",
    "ankle sprain": "ankle",
    "ankle fracture": "ankle",
    "achilles tendinopathy": "ankle",
    "achilles rupture": "ankle",
    "calf strain": "ankle",
    "peroneal tendinopathy": "ankle",

    # --- shoulder ---
    "rotator cuff repair": "shoulder",
    "rotator cuff tear": "shoulder",
    "rotator cuff strain": "shoulder",
    "rotator cuff tendinopathy": "shoulder",
    "labrum tear": "shoulder",
    "shoulder labrum repair": "shoulder",
    "slap tear": "shoulder",
    "shoulder impingement": "shoulder",
    "shoulder dislocation": "shoulder",
    "frozen shoulder": "shoulder",
    "adhesive capsulitis": "shoulder",

    # --- low_back ---
    "low back pain": "low_back",
    "lumbar strain": "low_back",
    "lumbar disc herniation": "low_back",
    "disc herniation": "low_back",
    "non-specific low back pain": "low_back",
    "lbp": "low_back",
    "sciatica": "low_back",

    # --- hamstring ---
    "hamstring strain": "hamstring",
    "hamstring tear": "hamstring",
    "grade 1 hamstring strain": "hamstring",
    "grade 2 hamstring strain": "hamstring",
    "proximal hamstring tendinopathy": "hamstring",

    # --- elbow ---
    "lateral epicondylitis": "elbow",
    "medial epicondylitis": "elbow",
    "tennis elbow": "elbow",
    "golfer's elbow": "elbow",
    "golfers elbow": "elbow",
    "elbow tendinopathy": "elbow",
    "elbow sprain": "elbow",
}


# Loose substring fallback for the explicit map. If the exact key misses,
# we walk this list and return the first prefix/keyword that matches the
# normalized injury_type. Order matters here: more specific terms first.
_SUBSTRING_RULES: list[tuple[BodyRegion, tuple[str, ...]]] = [
    ("knee",     ("acl", "mcl", "meniscus", "patell", "knee", "tkr")),
    ("ankle",    ("ankle", "achilles", "calf", "peroneal")),
    ("shoulder", ("shoulder", "rotator cuff", "labrum", "slap", "impingement",
                  "frozen shoulder", "adhesive capsulitis")),
    ("low_back", ("low back", "lumbar", "lbp", "sciatica", "disc herniation")),
    ("hamstring",("hamstring",)),
    ("elbow",    ("elbow", "tennis elbow", "golfer", "epicond")),
]


# Process-local cache for classify_freetext results. Keyed by normalized
# (lower-stripped) injury text; small, no eviction. Same string -> same
# region; we never re-hit Anthropic for an injury we've already classified.
_CLASSIFY_CACHE: dict[str, BodyRegion | None] = {}


def _normalize(injury_type: str | None) -> str:
    if not injury_type:
        return ""
    return injury_type.lower().strip()


def _hash_injury(injury_type: str) -> str:
    """Return a short stable hash for PHI-safe logging."""
    return hashlib.sha256(injury_type.encode("utf-8")).hexdigest()[:12]


def body_region(injury_type: str | None) -> BodyRegion | None:
    """Resolve an injury_type to a body_region using the deterministic table.

    Returns None when neither the explicit map nor the substring rules match.
    The caller may then fall back to classify_freetext() for an LLM
    classification, or raise / refuse depending on context.

    No network calls, no LLM. Safe to invoke on every protocol draft.
    """
    s = _normalize(injury_type)
    if not s:
        return None
    if s in INJURY_TO_BODY_REGION:
        return INJURY_TO_BODY_REGION[s]
    for region, keys in _SUBSTRING_RULES:
        if any(k in s for k in keys):
            return region
    return None


def classify_freetext(injury_type: str | None) -> BodyRegion | None:
    """LLM-classify a free-text injury description into one of ALLOWED_REGIONS.

    Used as a fallback when body_region() returns None. Caches results in
    process so repeated drafts for the same patient cost at most one Haiku
    call. PHI-safe: logs a short hash, never the raw text.

    Returns None when the LLM declines / the API isn't configured / the
    response can't be parsed. The caller decides whether to refuse the
    draft or proceed without anchoring.
    """
    s = _normalize(injury_type)
    if not s:
        return None
    if s in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[s]

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.info(
            "classify_freetext: no ANTHROPIC_API_KEY; injury_hash=%s -> None",
            _hash_injury(s),
        )
        _CLASSIFY_CACHE[s] = None
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning(
            "classify_freetext: anthropic SDK missing; injury_hash=%s -> None",
            _hash_injury(s),
        )
        _CLASSIFY_CACHE[s] = None
        return None

    client = anthropic.Anthropic(api_key=api_key)
    model = os.getenv("TAXONOMY_MODEL", "claude-haiku-4-5")
    system_prompt = (
        "You classify physical therapy injury descriptions into a single "
        "body region. Respond with EXACTLY one of: knee, ankle, shoulder, "
        "low_back, hamstring, elbow, multi. 'multi' means the description "
        "spans multiple regions and a single label is misleading. Never "
        "respond with anything else. No explanations, no punctuation."
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=8,
            system=system_prompt,
            messages=[{"role": "user", "content": injury_type}],
        )
    except Exception as exc:
        logger.warning(
            "classify_freetext anthropic error injury_hash=%s: %s",
            _hash_injury(s), exc,
        )
        _CLASSIFY_CACHE[s] = None
        return None

    raw = ""
    for block in resp.content or []:
        if getattr(block, "type", None) == "text":
            raw += getattr(block, "text", "")
    raw = raw.strip().lower().rstrip(".")

    if raw in ALLOWED_REGIONS:
        logger.info(
            "classify_freetext: injury_hash=%s -> %s (model=%s)",
            _hash_injury(s), raw, model,
        )
        _CLASSIFY_CACHE[s] = raw  # type: ignore[assignment]
        return raw  # type: ignore[return-value]

    logger.warning(
        "classify_freetext returned unrecognized region %r for injury_hash=%s",
        raw, _hash_injury(s),
    )
    _CLASSIFY_CACHE[s] = None
    return None


def resolve_body_region(injury_type: str | None) -> BodyRegion | None:
    """Convenience: try the deterministic map first, then the LLM fallback.

    The single entry-point most callers should use. Returns None when both
    paths fail; the caller decides what to do (refuse, log + proceed, etc.).
    """
    deterministic = body_region(injury_type)
    if deterministic is not None:
        return deterministic
    return classify_freetext(injury_type)
