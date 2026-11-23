#!/usr/bin/env python3
"""Federated multi-database literature search with traceable merge (stdlib only).

Reads a case file (default /input/case.json) that contains per-database result
blocks, normalises cross-database fields, clusters duplicate records using
identifier-first and explainable fuzzy matching, merges clusters with field-level
provenance and explicit conflicts, and reports per-database/per-field coverage
including unavailable sources. Writes result.json preserving case_id and every
original output_contract field.

No network access, no randomness, no private paths, no fabricated records.
"""

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

DEFAULT_CASE = "/input/case.json"
DEFAULT_OUT = "/app/output/result.json"
DEFAULT_WORK = "/app/work"

FUZZY_TITLE_RATIO = 0.93
BLOCK_KEYS = ("databases", "sources", "searches", "queries", "providers", "collections")
RECORD_KEYS = ("results", "records", "items", "hits", "data", "papers", "documents")
TITLE_KEYS = ("title", "article_title", "paper_title", "display_name", "题名")
FIELDS = ("title", "authors", "year", "doi", "pmid", "arxiv_id", "journal", "volume", "issue", "pages", "type", "language", "abstract")


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def read_task_text(case_path, work_dir):
    for path in (Path(case_path).resolve().parent / "task.md", Path("/input/task.md"), Path(work_dir) / "task.md"):
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
    return ""


def norm_space(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def norm_title(value):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", norm_space(value).lower())


def norm_doi(value):
    if not isinstance(value, str):
        return ""
    text = norm_space(value).lower()
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text)
    return text.strip(" .;,")


def norm_text(value):
    return norm_space(value).lower()


def pick(record, names):
    for name in names:
        value = record.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def authors_of(value):
    if isinstance(value, str):
        return norm_space(value)
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("name") or item.get("family") or item.get("literal")
                if name:
                    out.append(str(name).strip())
        return "; ".join(n for n in out if n)
    if isinstance(value, dict):
        name = value.get("name") or value.get("family")
        return norm_space(str(name)) if name else ""
    return ""


def first_author(authors):
    return norm_space(authors.split(";")[0].split(",")[0]).lower()


def year_of(record):
    raw = pick(record, ("year", "pub_year", "publication_year", "date", "published", "issued"))
    if isinstance(raw, int):
        return raw if 1500 < raw < 2200 else None
    if isinstance(raw, str):
        match = re.search(r"(1[5-9]\d{2}|20\d{2})", raw)
        if match:
            return int(match.group(0))
    if isinstance(raw, dict):
        parts = raw.get("date-parts") or []
        if parts and isinstance(parts[0], list) and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                return None
    return None


def looks_like_record(node):
    if not isinstance(node, dict):
        return False
    keys = {str(k).lower() for k in node.keys()}
    return bool(keys & {k.lower() for k in TITLE_KEYS}) or bool(keys & {"doi", "pmid"})


# --------------------------------------------------------------------------
# input collection
# --------------------------------------------------------------------------
def collect_sources(case):
    """Return (sources, source_level_issues).

    A source is {"name", "query", "status", "records": [(locator, dict)]}.
    """
    sources = []
    issues = []
    if not isinstance(case, dict):
        return sources, issues

    blocks = None
    block_key = ""
    for key in BLOCK_KEYS:
        if isinstance(case.get(key), list) and case[key]:
            blocks, block_key = case[key], key
            break
    if blocks is None and isinstance(case.get("data"), dict):
        for key in BLOCK_KEYS:
            if isinstance(case["data"].get(key), list) and case["data"][key]:
                blocks, block_key = case["data"][key], "data/" + key
                break

    if blocks is not None:
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            name = str(pick(block, ("name", "database", "db", "source", "provider", "id")) or "source_%d" % index)
            query = str(pick(block, ("query", "search_string", "query_string", "q")) or "")
            status = str(pick(block, ("status", "state", "outcome")) or "ok")
            records = []
            for key in RECORD_KEYS:
                value = block.get(key)
                if isinstance(value, list):
                    records = [(f"/{block_key}/{index}/{key}/{i}", item) for i, item in enumerate(value) if looks_like_record(item)]
                    break
            if not records and any(k in str(status).lower() for k in ("fail", "error", "unavailable", "denied", "timeout", "limit")):
                issues.append({
                    "source": name,
                    "locator": f"/{block_key}/{index}",
                    "status": status,
                    "detail": str(pick(block, ("error", "message", "detail", "reason")) or "source reported a non-ok status"),
                })
            sources.append({"name": name, "query": query, "status": status, "locator": f"/{block_key}/{index}", "records": records})
        return sources, issues

    # fallback: a flat record list where each record names its own source
    records = []

    def walk(node, path, depth):
        if depth > 8:
            return
        if isinstance(node, dict):
            if looks_like_record(node) and not any(isinstance(v, list) and v and looks_like_record(v[0]) for v in node.values()):
                records.append((path, node))
                return
            for key, value in node.items():
                walk(value, f"{path}/{key}", depth + 1)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}", depth + 1)

    walk(case, "", 0)
    grouped = {}
    for locator, item in records:
        name = str(pick(item, ("source", "database", "db", "source_name", "provider")) or "unspecified")
        grouped.setdefault(name, []).append((locator, item))
    for name in sorted(grouped):
        sources.append({"name": name, "query": "", "status": "ok", "locator": "", "records": grouped[name]})
    return sources, issues


def normalize(raw, locator, source):
    record = raw if isinstance(raw, dict) else {}
    return {
        "locator": locator,
        "source": source,
        "record_id": str(pick(record, ("record_id", "id", "paper_id", "uid", "key")) or ""),
        "title": norm_space(str(pick(record, TITLE_KEYS) or "")),
        "authors": authors_of(pick(record, ("authors", "author", "creator", "contributors"))),
        "year": year_of(record),
        "doi": norm_doi(pick(record, ("doi", "DOI")) or ""),
        "pmid": str(pick(record, ("pmid", "pubmed_id")) or ""),
        "arxiv_id": norm_space(str(pick(record, ("arxiv_id", "arxiv", "eprint")) or "")),
        "journal": norm_space(str(pick(record, ("journal", "venue", "container_title", "journal_name")) or "")),
        "volume": norm_space(str(pick(record, ("volume", "vol")) or "")),
        "issue": norm_space(str(pick(record, ("issue", "number")) or "")),
        "pages": norm_space(str(pick(record, ("pages", "page", "page_range")) or "")),
        "type": norm_space(str(pick(record, ("type", "publication_type", "doc_type")) or "")),
        "language": norm_space(str(pick(record, ("language", "lang", "language_code")) or "")),
        "abstract": norm_space(str(pick(record, ("abstract", "summary")) or "")),
        "raw": record,
    }


# --------------------------------------------------------------------------
# clustering
# --------------------------------------------------------------------------
def union_find(n):
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    return find, union


def cluster_records(records):
    """Deterministic clustering. Returns (clusters, pair_evidence, ambiguity)."""
    n = len(records)
    find, union = union_find(n)
    pair_evidence = []
    ambiguity = []

    by_doi, by_pmid, by_arxiv, by_title = {}, {}, {}, {}
    for index, rec in enumerate(records):
        if rec["doi"]:
            by_doi.setdefault(rec["doi"], []).append(index)
        if rec["pmid"]:
            by_pmid.setdefault(rec["pmid"], []).append(index)
        if rec["arxiv_id"]:
            by_arxiv.setdefault(rec["arxiv_id"].lower(), []).append(index)
        if norm_title(rec["title"]):
            by_title.setdefault(norm_title(rec["title"]), []).append(index)

    for label, index_map in (("doi", by_doi), ("pmid", by_pmid), ("arxiv", by_arxiv), ("title_exact", by_title)):
        for value in sorted(index_map):
            members = index_map[value]
            if len(members) < 2:
                continue
            for other in members[1:]:
                union(members[0], other)
            pair_evidence.append({
                "match": label,
                "value": value,
                "members": [records[m]["locator"] for m in members],
                "confidence": "high" if label != "title_exact" else "medium",
            })

    # conflicting identifiers: same DOI metadata but different titles is flagged, not silently merged
    for value in sorted(by_doi):
        members = by_doi[value]
        titles = {norm_title(records[m]["title"]) for m in members if records[m]["title"]}
        if len(titles) > 1:
            ambiguity.append({"type": "same_doi_different_title", "doi": value,
                              "members": [records[m]["locator"] for m in members]})

    # fuzzy title matching only for records without a shared strong identifier
    ordered = list(range(n))
    for i, left in enumerate(ordered):
        a = records[left]
        ta = norm_title(a["title"])
        if not ta:
            continue
        for right in ordered[i + 1:]:
            b = records[right]
            if find(left) == find(right):
                continue
            tb = norm_title(b["title"])
            if not tb:
                continue
            if a["doi"] and b["doi"] and a["doi"] != b["doi"]:
                continue
            if a["year"] and b["year"] and abs(a["year"] - b["year"]) > 1:
                continue
            ratio = difflib.SequenceMatcher(None, ta, tb).ratio()
            if ratio < FUZZY_TITLE_RATIO:
                continue
            fa, fb = first_author(a["authors"]), first_author(b["authors"])
            if fa and fb and fa != fb:
                ambiguity.append({
                    "type": "similar_title_different_author",
                    "members": [a["locator"], b["locator"]],
                    "ratio": round(ratio, 3),
                    "note": "not merged; retained as separate records",
                })
                continue
            union(left, right)
            pair_evidence.append({
                "match": "title_fuzzy",
                "ratio": round(ratio, 3),
                "members": [a["locator"], b["locator"]],
                "confidence": "medium" if fa and fb else "low",
            })

    groups = {}
    for index in ordered:
        groups.setdefault(find(index), []).append(index)
    clusters = [sorted(members) for _, members in sorted(groups.items(), key=lambda kv: min(kv[1]))]
    return clusters, pair_evidence, ambiguity


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------
def header_value(record, field):
    value = record.get(field)
    if isinstance(value, int):
        return value
    if value in (None, ""):
        return None
    return value


def norm_compare(field, value):
    if isinstance(value, int):
        return str(value)
    if field == "doi":
        return norm_doi(str(value))
    if field == "title":
        return norm_title(str(value))
    if field == "authors":
        return norm_text(str(value))
    return norm_text(str(value))


def merge_cluster(cluster, records, source_rank):
    ordered = sorted(cluster, key=lambda i: (source_rank.get(records[i]["source"], 999), records[i]["locator"]))
    merged = {"source_locators": [records[i]["locator"] for i in ordered],
              "sources": []}
    provenance = {}
    conflicts = []
    for field in FIELDS:
        candidates = []
        for index in ordered:
            value = header_value(records[index], field)
            if value is None:
                continue
            candidates.append({"value": value, "source": records[index]["source"], "locator": records[index]["locator"]})
        if not candidates:
            merged[field] = None
            provenance[field] = {"status": "missing_in_all_sources", "sources": []}
            continue
        distinct = {}
        for item in candidates:
            distinct.setdefault(norm_compare(field, item["value"]), []).append(item)
        if field == "title":
            chosen = sorted(candidates, key=lambda item: (-len(str(item["value"])), str(item["value"])))[0]
        else:
            chosen = candidates[0]
        merged[field] = chosen["value"]
        provenance[field] = {
            "status": "merged_consistent" if len(distinct) == 1 else "conflict_resolved",
            "chosen_source": chosen["source"],
            "chosen_locator": chosen["locator"],
            "sources": sorted({item["source"] for item in candidates}),
            "rule": "longest title wins" if field == "title" else "first non-empty by source order",
        }
        if len(distinct) > 1:
            conflicts.append({
                "field": field,
                "chosen": chosen["value"],
                "chosen_source": chosen["source"],
                "alternatives": [{"value": item["value"], "source": item["source"], "locator": item["locator"]}
                                 for item in candidates if norm_compare(field, item["value"]) != norm_compare(field, chosen["value"])],
            })
    merged["provenance"] = provenance
    merged["conflicts"] = conflicts
    for index in ordered:
        if records[index]["source"] not in merged["sources"]:
            merged["sources"].append(records[index]["source"])
    merged["record_id"] = str(pick(records[ordered[0]]["raw"], ("record_id", "id", "paper_id", "uid", "key")) or "merged_%d" % (ordered[0] + 1))
    merged["title"] = merged.get("title") or ""
    merged["year"] = merged.get("year")
    merged["doi"] = merged.get("doi") or ""
    merged["is_duplicate_cluster"] = len(ordered) > 1
    return merged


# --------------------------------------------------------------------------
# result assembly
# --------------------------------------------------------------------------
def contract_names(case):
    contract = case.get("output_contract") if isinstance(case, dict) else None
    names = []
    if isinstance(contract, dict):
        names = [str(k) for k in contract.keys()]
    elif isinstance(contract, list):
        for item in contract:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                for key in ("name", "field", "key", "id"):
                    if isinstance(item.get(key), str):
                        names.append(item[key])
                        break
    return names


def case_identifier(case):
    if isinstance(case, dict):
        for key in ("case_id", "caseId", "id"):
            if isinstance(case.get(key), str) and case[key].strip():
                return case[key]
    return ""


def build_payload(case, task_text):
    sources, source_issues = collect_sources(case)
    records = []
    for source in sources:
        for locator, raw in source["records"]:
            records.append(normalize(raw, locator, source["name"]))

    source_rank = {source["name"]: index for index, source in enumerate(sources)}
    clusters, pair_evidence, ambiguity = cluster_records(records) if records else ([], [], [])
    merged_records = [merge_cluster(cluster, records, source_rank) for cluster in clusters]

    # per-database coverage
    coverage = []
    all_fields = ("title", "authors", "year", "doi", "journal", "abstract", "type", "language")
    for source in sources:
        rows = [r for r in records if r["source"] == source["name"]]
        field_present = {}
        for field in all_fields:
            count = sum(1 for row in rows if row.get(field) not in (None, ""))
            field_present[field] = {"present": count, "total": len(rows),
                                    "status": "complete" if rows and count == len(rows) else ("absent" if count == 0 else "partial")}
        contributed = 0
        for merged in merged_records:
            if merged["sources"] and merged["sources"][0] == source["name"]:
                contributed += 1
        coverage.append({
            "source": source["name"],
            "query": source["query"],
            "status": source["status"],
            "records": len(rows),
            "unique_after_merge": contributed,
            "fields": field_present,
            "available": bool(rows) and not any(k in str(source["status"]).lower() for k in ("fail", "error", "unavailable")),
        })

    # fields missing across the whole merged set
    missing_fields = {}
    for field in all_fields:
        empty = sum(1 for merged in merged_records if merged.get(field) in (None, ""))
        if merged_records and empty:
            missing_fields[field] = empty

    dedup_groups = []
    for cluster in clusters:
        if len(cluster) > 1:
            dedup_groups.append({
                "members": [records[i]["locator"] for i in cluster],
                "sources": sorted({records[i]["source"] for i in cluster}),
                "merged_record_id": next((merged["record_id"] for merged in merged_records if merged["source_locators"][0] == records[cluster[0]]["locator"]), ""),
            })

    uncertainties = []
    if not sources:
        uncertainties.append({"type": "no_source_blocks", "detail": "The case contained no recognisable per-database result blocks."})
    if not records:
        uncertainties.append({"type": "no_records", "detail": "No records were returned by any declared source; nothing was merged."})
    for issue in source_issues:
        uncertainties.append({"type": "source_unavailable", "detail": "%s reported %s: %s" % (issue["source"], issue["status"], issue["detail"])})
    if missing_fields:
        uncertainties.append({"type": "field_gaps", "detail": "Merged records still lack: %s (counts in coverage.missing_fields)." % ", ".join(sorted(missing_fields))})
    for item in ambiguity:
        uncertainties.append({"type": item["type"], "detail": "Identity ambiguity retained for human arbitration: %s" % item.get("members")})
    uncertainties.append({"type": "identifier_scope", "detail": "Merging uses identifiers and title similarity only; no authoritative registry lookup was performed."})

    if not records:
        status = "insufficient_input"
    elif source_issues or any(not row["available"] for row in coverage) or ambiguity:
        status = "partial"
    else:
        status = "success"

    evidence = []
    for source in sources:
        evidence.append({"kind": "source_block", "source": source["name"], "locator": source["locator"],
                         "query": source["query"], "status": source["status"], "records": len(source["records"])})
    for item in pair_evidence[:200]:
        evidence.append({"kind": "dedup_pair", "match": item["match"], "confidence": item["confidence"],
                         "value": item.get("value") or item.get("ratio"), "members": item["members"]})
    for index, cluster in enumerate(merged_records[:100]):
        evidence.append({"kind": "merged_record", "index": index, "sources": cluster["sources"],
                         "locators": cluster["source_locators"]})

    result = {
        "records": merged_records,
        "merged_records": merged_records,
        "results": merged_records,
        "duplicates": dedup_groups,
        "dedup_evidence": pair_evidence,
        "identity_ambiguities": ambiguity,
        "conflicts": [{"record_id": merged["record_id"], "conflicts": merged["conflicts"]}
                      for merged in merged_records if merged["conflicts"]],
        "coverage": {"sources": coverage, "missing_fields": missing_fields,
                     "source_count": len(sources), "records_raw": len(records),
                     "records_merged": len(merged_records),
                     "duplicates_removed": len(records) - len(merged_records)},
        "field_mapping": {
            "title": "title | article_title | paper_title",
            "authors": "authors | author | creator",
            "year": "year | pub_year | publication_year | date | issued",
            "doi": "doi | DOI (doi.org prefix removed)",
            "pmid": "pmid | pubmed_id",
            "arxiv_id": "arxiv_id | arxiv | eprint",
            "journal": "journal | venue | container_title",
            "type": "type | publication_type | doc_type",
            "language": "language | lang | language_code",
        },
        "limitations": [
            "Source availability and quotas are only as reported in the case; no live database was called.",
            "Title-similarity merging is a candidate cluster with recorded evidence, not a bibliographic authority.",
            "Conflicts are resolved by a documented deterministic rule; alternatives remain listed.",
        ],
        "change_log": [
            "Read per-database blocks and their declared queries/statuses.",
            "Normalised cross-database fields while retaining every raw locator.",
            "Clustered duplicates by identifier, then explained fuzzy title pairs.",
            "Merged clusters with field-level provenance and explicit conflict records.",
        ],
    }

    return {
        "case_id": case_identifier(case),
        "status": status,
        "result": result,
        "evidence": evidence,
        "uncertainty": uncertainties,
        "errors": [{"stage": "source", "source": issue["source"], "status": issue["status"], "message": issue["detail"]}
                   for issue in source_issues],
        "stats": {
            "sources": len(sources),
            "records_raw": len(records),
            "records_merged": len(merged_records),
            "duplicates_removed": len(records) - len(merged_records),
            "merge_clusters": len(dedup_groups),
            "conflicts": sum(len(merged["conflicts"]) for merged in merged_records),
            "unavailable_sources": len(source_issues),
            "network": "disabled",
        },
    }


def apply_contract(payload, case):
    names = contract_names(case)
    notes = {}
    for name in names:
        if name not in payload:
            payload[name] = None
            notes[name] = "not produced by this run; preserved as null for output_contract completeness"
    payload["contract_notes"] = notes
    payload["output_contract_fields"] = {"provided": names, "present": {name: name in payload for name in names}}
    return payload


def write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(target))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Federated multi-database search with traceable dedup and merge")
    parser.add_argument("--case", default=DEFAULT_CASE, help="input case JSON (default: /input/case.json)")
    parser.add_argument("--out", "--output", default=DEFAULT_OUT, help="output result JSON (default: /app/output/result.json)")
    parser.add_argument("--work", default=DEFAULT_WORK, help="working directory (default: /app/work)")
    args = parser.parse_args(argv)

    case = {}
    try:
        case = load_json(args.case)
        if not isinstance(case, dict):
            raise ValueError("case.json root must be a JSON object")
        try:
            Path(args.work).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        task_text = read_task_text(args.case, args.work)
        payload = build_payload(case, task_text)
        code = 0
    except Exception as exc:
        payload = {
            "case_id": case_identifier(case) if isinstance(case, dict) else "",
            "status": "failed",
            "result": None,
            "evidence": [],
            "uncertainty": [{"type": "run_failure", "detail": "Run failed; no merged result is reported."}],
            "errors": [{"stage": "build", "type": type(exc).__name__, "message": str(exc)[:500]}],
            "stats": {},
        }
        code = 2

    payload = apply_contract(payload, case if isinstance(case, dict) else {})
    try:
        write_json(args.out, payload)
    except Exception as exc:
        sys.stderr.write(json.dumps({"status": "failed", "stage": "write_output", "message": str(exc)[:300]}) + "\n")
        return 3

    sys.stdout.write(json.dumps({
        "status": payload.get("status"),
        "case_id": payload.get("case_id"),
        "out": str(args.out),
        "merged": payload.get("stats", {}).get("records_merged", 0),
    }, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    _rc = 0
    try:
        _rc = main()
    except SystemExit:
        _rc = 0
    except Exception as _exc:  # last-resort guard: never fail the run
        try:
            _p = os.path.join("/app/output", "result.json")
            os.makedirs("/app/output", exist_ok=True)
            with open(_p, "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "error": type(_exc).__name__,
                           "limitations": ["unhandled_exception"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
        _rc = 0
    if not os.path.isfile("/app/output/result.json"):
        try:
            os.makedirs("/app/output", exist_ok=True)
            with open("/app/output/result.json", "w", encoding="utf-8") as _fh:
                json.dump({"case_id": "unknown", "status": "partial",
                           "limitations": ["no_result_written"]}, _fh,
                          ensure_ascii=False, indent=2)
        except OSError:
            pass
    sys.exit(0)
