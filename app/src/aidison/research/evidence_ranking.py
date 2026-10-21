"""Deterministic, provenance-preserving evidence ranking for research proposals."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from statistics import mean

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
_SIMHASH_BITS = 64


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    """One citation candidate before bounded deduplication and ranking.

    ``ref`` identifies the immutable artifact or source record.  It is kept
    alongside the URL so that URL-level deduplication never loses provenance.
    """

    ref: str
    url: str
    title: str = ""
    excerpt: str = ""
    authority: float = 0.0

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("evidence ref must not be blank")
        if not self.url.strip():
            raise ValueError("evidence url must not be blank")
        if not 0.0 <= self.authority <= 1.0:
            raise ValueError("evidence authority must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RankedEvidence:
    """The canonical citation retained for one exact/near-duplicate cluster."""

    candidate: EvidenceCandidate
    score: float
    provenance_refs: tuple[str, ...]
    duplicate_urls: tuple[str, ...]


def tokenize(text: str) -> tuple[str, ...]:
    """Return a small, locale-independent token stream suitable for ranking."""

    return tuple(token.lower() for token in _TOKEN_PATTERN.findall(text))


def simhash(text: str) -> int:
    """Compute an explainable 64-bit SimHash over unique lexical features."""

    counts = Counter(tokenize(text))
    if not counts:
        return 0
    weights = [0] * _SIMHASH_BITS
    for token, frequency in counts.items():
        digest = int.from_bytes(sha256(token.encode("utf-8")).digest()[:8], "big")
        for bit in range(_SIMHASH_BITS):
            weights[bit] += frequency if digest & (1 << bit) else -frequency
    return sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0)


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def rank_evidence(
    *,
    query: str,
    candidates: Sequence[EvidenceCandidate],
    near_duplicate_distance: int = 3,
) -> tuple[RankedEvidence, ...]:
    """Rank a bounded evidence set without dropping citation provenance.

    The pipeline is deliberately deterministic:

    1. exact URL clusters retain one canonical citation and all source refs;
    2. SimHash clusters suppress near-identical text while merging provenance;
    3. BM25 relevance plus an explicit authority/title rule ranks clusters;
    4. URL and ref are final tie-breakers.
    """

    if not 0 <= near_duplicate_distance <= _SIMHASH_BITS:
        raise ValueError("near_duplicate_distance must be between 0 and 64")
    if not candidates:
        return ()

    scored = _score_candidates(query, candidates)
    by_url: dict[str, list[_ScoredCandidate]] = {}
    for item in scored:
        by_url.setdefault(item.candidate.url, []).append(item)

    exact_clusters = [
        _cluster_url_duplicates(items)
        for _, items in sorted(by_url.items(), key=lambda item: item[0])
    ]
    exact_clusters.sort(key=_cluster_sort_key)

    retained: list[_Cluster] = []
    for cluster in exact_clusters:
        duplicate = next(
            (
                existing
                for existing in retained
                if hamming_distance(existing.simhash_value, cluster.simhash_value)
                <= near_duplicate_distance
            ),
            None,
        )
        if duplicate is None:
            retained.append(cluster)
        else:
            _merge_cluster(duplicate, cluster)

    retained.sort(key=_cluster_sort_key)
    return tuple(
        RankedEvidence(
            candidate=cluster.primary.candidate,
            score=round(cluster.primary.score, 12),
            provenance_refs=tuple(sorted(cluster.provenance_refs)),
            duplicate_urls=tuple(sorted(cluster.urls)),
        )
        for cluster in retained
    )


@dataclass(slots=True)
class _ScoredCandidate:
    candidate: EvidenceCandidate
    score: float
    simhash_value: int


@dataclass(slots=True)
class _Cluster:
    primary: _ScoredCandidate
    simhash_value: int
    provenance_refs: set[str]
    urls: set[str]


def _score_candidates(
    query: str,
    candidates: Sequence[EvidenceCandidate],
) -> list[_ScoredCandidate]:
    query_tokens = tokenize(query)
    documents = [
        tokenize(f"{candidate.title} {candidate.title} {candidate.excerpt}")
        for candidate in candidates
    ]
    document_frequency: Counter[str] = Counter(
        token for document in documents for token in set(document)
    )
    average_length = mean(len(document) for document in documents) or 1.0
    total = len(documents)
    scored: list[_ScoredCandidate] = []
    for candidate, document in zip(candidates, documents, strict=True):
        frequencies = Counter(document)
        bm25 = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            inverse_frequency = math.log(
                1 + (total - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5)
            )
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * len(document) / average_length)
            bm25 += inverse_frequency * frequency * 2.2 / denominator
        title_bonus = sum(token in tokenize(candidate.title) for token in set(query_tokens)) * 0.25
        score = bm25 + title_bonus + candidate.authority * 0.2
        scored.append(
            _ScoredCandidate(
                candidate=candidate,
                score=score,
                simhash_value=simhash(f"{candidate.title} {candidate.excerpt}"),
            )
        )
    return scored


def _cluster_url_duplicates(items: Iterable[_ScoredCandidate]) -> _Cluster:
    ordered = sorted(items, key=_scored_sort_key)
    primary = ordered[0]
    return _Cluster(
        primary=primary,
        simhash_value=primary.simhash_value,
        provenance_refs={item.candidate.ref for item in ordered},
        urls={item.candidate.url for item in ordered},
    )


def _merge_cluster(target: _Cluster, incoming: _Cluster) -> None:
    target.provenance_refs.update(incoming.provenance_refs)
    target.urls.update(incoming.urls)
    if _scored_sort_key(incoming.primary) < _scored_sort_key(target.primary):
        target.primary = incoming.primary
        target.simhash_value = incoming.simhash_value


def _scored_sort_key(item: _ScoredCandidate) -> tuple[float, str, str]:
    return (-item.score, item.candidate.url, item.candidate.ref)


def _cluster_sort_key(cluster: _Cluster) -> tuple[float, str, str]:
    return _scored_sort_key(cluster.primary)
