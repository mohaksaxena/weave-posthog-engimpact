#!/usr/bin/env python3
"""
PostHog Engineering Impact Analyzer

Metrics:
  Delivery (40%)          — Shannon entropy of file changes across top-level dirs.
                            A PR touching frontend/, posthog/, ee/, plugin-server/
                            equally scores ~1.0; a single-dir PR scores ~0.1.
                            Weighted by log(1 + files_changed) per PR.

  Review Quality (35%)    — CHANGES_REQUESTED scores 2x APPROVED.
                            Inline comment count adds bonus. Body length adds bonus.
                            Responding within 24 h applies a 1.5x multiplier.

  Collaboration Breadth (25%) — Unique teammates worked with (you reviewed their PR
                            or they reviewed yours), log-scaled.

Output: CSV to stdout. Redirect to engineers.csv.
"""

import csv
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = "PostHog/posthog"
BASE_URL = "https://api.github.com"
MAX_PRS = 400
DAYS_BACK = 90

HEADERS = {"Accept": "application/vnd.github.v3+json"}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"
else:
    print("WARNING: No GITHUB_TOKEN set — unauthenticated rate limit is 60/h.", file=sys.stderr)

BOT_PATTERNS = ["bot", "automation", "github-actions", "dependabot", "renovate", "stale"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _sleep_until_reset(headers):
    reset = int(headers.get("X-RateLimit-Reset", time.time() + 60))
    wait = max(reset - time.time() + 2, 2)
    print(f"  Rate limited — sleeping {wait:.0f}s...", file=sys.stderr)
    time.sleep(min(wait, 300))


def api_get(url, params=None, retries=3):
    for _ in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  Network error: {exc}", file=sys.stderr)
            time.sleep(5)
            continue
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429):
            _sleep_until_reset(r.headers)
        elif r.status_code == 422:
            # Search API: query window issue — just return None
            return None
        else:
            print(f"  HTTP {r.status_code} for {url}", file=sys.stderr)
            time.sleep(2)
    return None


def paginate(url, params=None, max_items=None):
    params = dict(params or {})
    params["per_page"] = 100
    items = []
    page = 1
    while True:
        params["page"] = page
        r = api_get(url, params)
        if r is None:
            break
        chunk = r.json()
        if not isinstance(chunk, list) or not chunk:
            break
        items.extend(chunk)
        if max_items and len(items) >= max_items:
            items = items[:max_items]
            break
        if len(chunk) < 100:
            break
        page += 1
    return items


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def shannon_entropy(file_paths):
    """Normalized Shannon entropy of files across top-level directories (0–1)."""
    if not file_paths:
        return 0.0
    counts = defaultdict(int)
    for f in file_paths:
        top = f.split("/")[0] if "/" in f else "__root__"
        counts[top] += 1
    n = len(counts)
    if n == 1:
        return 0.1
    total = sum(counts.values())
    H = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return H / math.log2(n)


def review_score(state, body, inline_count, response_hours):
    """Score a single review event."""
    base = {"CHANGES_REQUESTED": 2.0, "APPROVED": 1.0, "COMMENTED": 0.4}.get(state, 0.1)
    inline_bonus = min(inline_count * 0.3, 2.0)
    body_bonus = min(len(body or "") / 600, 0.5)
    score = base + inline_bonus + body_bonus
    if response_hours is not None and 0 <= response_hours <= 24:
        score *= 1.5
    return score


def is_bot(login):
    return any(p in login.lower() for p in BOT_PATTERNS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
    print(f"Cutoff date: {cutoff.date()}  (last {DAYS_BACK} days)", file=sys.stderr)

    # ---- 1. Collect merged PRs ----
    print(f"Fetching up to {MAX_PRS} merged PRs from {REPO}…", file=sys.stderr)
    raw_prs = []
    page = 1
    while len(raw_prs) < MAX_PRS:
        params = {
            "q": f"repo:{REPO} is:pr is:merged merged:>={cutoff.strftime('%Y-%m-%d')}",
            "sort": "updated",
            "order": "desc",
            "per_page": 100,
            "page": page,
        }
        r = api_get(f"{BASE_URL}/search/issues", params)
        if r is None:
            break
        data = r.json()
        items = data.get("items", [])
        if not items:
            break
        raw_prs.extend(items)
        print(f"  …{len(raw_prs)} PRs collected", file=sys.stderr)
        if len(items) < 100 or len(raw_prs) >= MAX_PRS:
            break
        page += 1
        time.sleep(1.2)  # Search API: 30 req/min

    raw_prs = raw_prs[:MAX_PRS]
    print(f"Analyzing {len(raw_prs)} PRs…", file=sys.stderr)

    # ---- 2. Per-engineer accumulators ----
    eng = defaultdict(lambda: {
        "login": "",
        "avatar_url": "",
        "display_name": "",
        "pr_count": 0,
        "total_additions": 0,
        "total_deletions": 0,
        "total_files": 0,
        "dirs_touched": set(),
        "delivery_pr_scores": [],      # list of (entropy, log_weight)
        "review_scores_given": [],     # list of float
        "review_response_hours": [],   # list of float
        "reviews_given": 0,
        "changes_requested": 0,
        "collaborators": set(),        # unique teammate logins
        "top_prs": [],                 # (entropy, number, title, url)
    })

    # ---- 3. Process each PR ----
    for idx, pr_item in enumerate(raw_prs):
        pr_num = pr_item["number"]
        author = pr_item["user"]["login"]
        avatar = pr_item["user"]["avatar_url"]
        pr_title = pr_item["title"]
        pr_url = pr_item["html_url"]
        pr_created_str = pr_item["created_at"]
        pr_created = datetime.fromisoformat(pr_created_str.replace("Z", "+00:00"))

        if is_bot(author):
            continue

        if (idx + 1) % 25 == 0:
            print(f"  PR {idx+1}/{len(raw_prs)} — #{pr_num}", file=sys.stderr)

        a = eng[author]
        a["login"] = author
        a["avatar_url"] = avatar
        a["pr_count"] += 1

        # -- PR detail (additions/deletions) --
        pr_detail = api_get(f"{BASE_URL}/repos/{REPO}/pulls/{pr_num}")
        if pr_detail:
            d = pr_detail.json()
            a["total_additions"] += d.get("additions", 0)
            a["total_deletions"] += d.get("deletions", 0)

        # -- Files changed --
        files_resp = paginate(f"{BASE_URL}/repos/{REPO}/pulls/{pr_num}/files")
        file_paths = [f["filename"] for f in files_resp]
        a["total_files"] += len(file_paths)
        for fp in file_paths:
            a["dirs_touched"].add(fp.split("/")[0] if "/" in fp else "__root__")

        entropy = shannon_entropy(file_paths)
        weight = math.log1p(len(file_paths))
        a["delivery_pr_scores"].append((entropy, weight))
        a["top_prs"].append((entropy, pr_num, pr_title[:90], pr_url))

        # -- Reviews --
        reviews = api_get(f"{BASE_URL}/repos/{REPO}/pulls/{pr_num}/reviews")
        reviews_data = reviews.json() if reviews else []

        # Inline review comments: count per reviewer
        inline_counts: dict[str, int] = defaultdict(int)
        comments = paginate(f"{BASE_URL}/repos/{REPO}/pulls/{pr_num}/comments")
        for c in comments:
            commenter = (c.get("user") or {}).get("login", "")
            if commenter and commenter != author and not is_bot(commenter):
                inline_counts[commenter] += 1

        # Score each reviewer's strongest review
        strongest: dict[str, dict] = {}
        for rv in reviews_data:
            rv_login = (rv.get("user") or {}).get("login", "")
            rv_avatar = (rv.get("user") or {}).get("avatar_url", "")
            state = rv.get("state", "")
            body = rv.get("body") or ""
            submitted = rv.get("submitted_at", "")

            if not rv_login or rv_login == author or is_bot(rv_login):
                continue

            response_h = None
            if submitted:
                sub_dt = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
                response_h = max((sub_dt - pr_created).total_seconds() / 3600, 0)

            prev = strongest.get(rv_login)
            state_rank = {"CHANGES_REQUESTED": 3, "APPROVED": 2, "COMMENTED": 1}.get(state, 0)
            prev_rank = {"CHANGES_REQUESTED": 3, "APPROVED": 2, "COMMENTED": 1}.get(
                (prev or {}).get("state", ""), 0
            )
            if prev is None or state_rank > prev_rank:
                strongest[rv_login] = {
                    "state": state,
                    "body": body,
                    "response_h": response_h,
                    "avatar": rv_avatar,
                }

        for rv_login, rv_data in strongest.items():
            score = review_score(
                rv_data["state"],
                rv_data["body"],
                inline_counts.get(rv_login, 0),
                rv_data["response_h"],
            )
            r_eng = eng[rv_login]
            r_eng["login"] = rv_login
            if not r_eng["avatar_url"]:
                r_eng["avatar_url"] = rv_data["avatar"]

            r_eng["review_scores_given"].append(score)
            r_eng["reviews_given"] += 1
            if rv_data["state"] == "CHANGES_REQUESTED":
                r_eng["changes_requested"] += 1
            if rv_data["response_h"] is not None:
                r_eng["review_response_hours"].append(rv_data["response_h"])

            # Collaboration edges (bidirectional)
            a["collaborators"].add(rv_login)
            r_eng["collaborators"].add(author)

        time.sleep(0.08)

    # ---- 4. Compute raw scores ----
    for login, a in eng.items():
        # Delivery: weighted-average entropy + directory-diversity bonus
        if a["delivery_pr_scores"]:
            total_w = sum(w for _, w in a["delivery_pr_scores"])
            wavg_entropy = (
                sum(e * w for e, w in a["delivery_pr_scores"]) / total_w if total_w else 0.0
            )
        else:
            wavg_entropy = 0.0

        # Directory diversity: how many unique top-level dirs across all PRs
        dir_div = math.log1p(len(a["dirs_touched"])) / math.log1p(20)
        a["delivery_raw"] = wavg_entropy * 0.7 + dir_div * 0.3

        # Review Quality: sum of scores (rewards volume + depth)
        a["review_quality_raw"] = sum(a["review_scores_given"])

        # Collaboration: log-scaled unique collaborators
        a["collaboration_raw"] = math.log1p(len(a["collaborators"]))

        # Derived metadata
        a["avg_response_h"] = (
            sum(a["review_response_hours"]) / len(a["review_response_hours"])
            if a["review_response_hours"]
            else None
        )
        a["dirs_count"] = len(a["dirs_touched"])

    # ---- 5. Filter noise ----
    # Keep engineers with >= 2 authored PRs OR >= 3 reviews given
    eng = {
        login: a for login, a in eng.items()
        if not is_bot(login) and (a["pr_count"] >= 2 or a["reviews_given"] >= 3)
    }

    if not eng:
        print("No engineers passed the filter — nothing to output.", file=sys.stderr)
        sys.exit(1)

    # ---- 6. Normalize each component 0–100 (top = 100) ----
    def normalize(key_raw, key_norm):
        vals = [a[key_raw] for a in eng.values()]
        mx = max(vals) if vals else 1.0
        mx = mx or 1.0
        for a in eng.values():
            a[key_norm] = (a[key_raw] / mx) * 100.0

    normalize("delivery_raw", "delivery_norm")
    normalize("review_quality_raw", "review_quality_norm")
    normalize("collaboration_raw", "collaboration_norm")

    for a in eng.values():
        a["composite"] = (
            a["delivery_norm"] * 0.40
            + a["review_quality_norm"] * 0.35
            + a["collaboration_norm"] * 0.25
        )

    mx_comp = max(a["composite"] for a in eng.values()) or 1.0
    for a in eng.values():
        a["composite_norm"] = (a["composite"] / mx_comp) * 100.0

    # ---- 7. Sort and emit CSV ----
    ranked = sorted(eng.values(), key=lambda x: x["composite_norm"], reverse=True)

    for rank, a in enumerate(ranked, 1):
        a["rank"] = rank
        top3 = sorted(a["top_prs"], reverse=True)[:3]
        a["top_prs_json"] = json.dumps(
            [{"number": n, "title": t, "url": u} for _, n, t, u in top3]
        )

    FIELDS = [
        "rank",
        "login",
        "avatar_url",
        # Raw activity
        "pr_count",
        "total_additions",
        "total_deletions",
        "total_files",
        "dirs_count",
        "reviews_given",
        "changes_requested",
        "avg_response_h",
        "unique_collaborators",
        # Raw metric scores
        "delivery_raw",
        "review_quality_raw",
        "collaboration_raw",
        # Normalized (0–100, top=100)
        "delivery_norm",
        "review_quality_norm",
        "collaboration_norm",
        # Composite
        "composite_norm",
        # Context
        "top_prs_json",
    ]

    writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for a in ranked:
        row = {
            **{k: a.get(k, "") for k in FIELDS},
            "unique_collaborators": len(a["collaborators"]),
            "avg_response_h": f"{a['avg_response_h']:.1f}" if a["avg_response_h"] is not None else "",
            "delivery_raw": f"{a['delivery_raw']:.4f}",
            "review_quality_raw": f"{a['review_quality_raw']:.4f}",
            "collaboration_raw": f"{a['collaboration_raw']:.4f}",
            "delivery_norm": f"{a['delivery_norm']:.1f}",
            "review_quality_norm": f"{a['review_quality_norm']:.1f}",
            "collaboration_norm": f"{a['collaboration_norm']:.1f}",
            "composite_norm": f"{a['composite_norm']:.1f}",
        }
        writer.writerow(row)

    print(f"\nDone. {len(ranked)} engineers written.", file=sys.stderr)


if __name__ == "__main__":
    main()
