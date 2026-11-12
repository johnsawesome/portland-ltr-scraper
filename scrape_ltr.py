"""
Scrape Portland, ME Long-Term Rental (LTR) registrations + "More Info" rent tables
from the public EnerGov Citizen Self-Service portal.

Usage:
    python scrape_ltr.py --year 2026            # full run (resumable)
    python scrape_ltr.py --year 2026 --limit 5  # quick test

Outputs (in ./out/):
    ltr_all_search.json      raw search listing for every LTR record (all years)
    details_cache.jsonl      one JSON object per fetched license (cache; makes runs resumable)
    ltr_<year>.json          combined list of license records whose LicenseYear == year
    ltr_<year>_licenses.csv  one row per license (scalar More Info fields flattened)
    ltr_<year>_units.csv     one row per unit from the supplemental rent table
"""
import argparse, csv, json, os, re, sys, time, urllib.request, urllib.error

BASE = "https://selfservice.portlandmaine.gov/EnerGov_Prod/SelfService/api/energov"
LTR_TYPE_ID = "ddf09feb-ce7d-4437-839d-33296fd1c849"  # Long Term Housing Registration
MODULE_LICENSE = 8
HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
    "tenantId": "1",
    "tenantName": "EnerGovProd",
    "Tyler-TenantUrl": "home",
    "Tyler-Tenant-Culture": "en-US",
    "User-Agent": "Mozilla/5.0 (research script; polite rate-limited)",
}


def call(path, body=None, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(BASE + path, data=data, headers=HEADERS,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.load(r)
            if not j.get("Success", True):
                raise RuntimeError(j.get("ErrorMessage"))
            return j["Result"]
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt * 2)


# ---------- search ----------
# The License-module search (SearchModule=10) honours LicenseCriteria; the type filter value
# is "<typeId>_<classId>". Elasticsearch caps any single query at 10,000 hits, so we query
# one class at a time (largest, Multi Family, is ~8k).
LTR_CLASSES = {
    "Multi Family": "c7f4be57-4935-461c-bcf1-8d89c7973b81",
    "Single Family": "5c7dadf7-db4e-4d4d-a1fa-c109dbf3c0c5",
    "Two Family": "0b3794bb-b86f-467c-b6c0-6d473a8c6966",
}


def search_all_ltr(page_size=500, delay=0.7):
    criteria = call("/search/criteria")
    rows = {}
    for cname, cid in LTR_CLASSES.items():
        page = 1
        while True:
            q = json.loads(json.dumps(criteria))
            q.update(Keyword="", ExactMatch=True, SearchModule=10, FilterModule=1,
                     PageNumber=0, PageSize=0, SortBy="relevance", SortAscending=True)
            q["LicenseCriteria"].update(
                LicenseTypeId=f"{LTR_TYPE_ID}_{cid}", LicenseClassId="none", LicenseStatusId="none",
                BusinessStatusId="none", CompanyTypeId="none", BusinessTypeId="none",
                PageNumber=page, PageSize=page_size, SortBy="relevance")
            res = call("/search/search", q)
            for r in res["EntityResults"]:
                rows[r["CaseId"]] = r
            print(f"  {cname}: page {page}/{res['TotalPages']} (total {res['TotalFound']})", file=sys.stderr)
            if page >= res["TotalPages"] or not res["EntityResults"]:
                break
            page += 1
            time.sleep(delay)
    return list(rows.values())


def is_candidate(r, year):
    """Search rows lack LicenseYear, so pre-filter by dates; detail fetch confirms the year."""
    exp = (r.get("ExpireDate") or "")[:4]
    if exp == str(year):
        return True
    # pending/no-expiry records (Submitted, In Review, ...) applied around the cycle start
    if exp in ("", "2999") and (r.get("ApplyDate") or "") >= f"{year - 1}-09-01":
        return True
    return False


# ---------- detail ----------
def strip_row(label):
    return re.sub(r"\s*@\s*Row\s*\d+\s*$", "", label or "").strip()


GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def resolve_picklist(value, *item_lists):
    """Translate pick-list item GUID(s) into their display text."""
    if not isinstance(value, str) or not value:
        return value
    lut = {}
    for items in item_lists:
        for it in items or []:
            pid = it.get("CustomFieldPickListItemID") or it.get("Id")
            if pid:
                lut[str(pid).lower()] = it.get("Value") or it.get("Label")
    parts = [p.strip() for p in value.split(",")]
    if lut and all(GUID.match(p) for p in parts):
        return "; ".join(lut.get(p.lower(), p) for p in parts)
    return value


def parse_custom_fields(cf):
    fields, tables = {}, {}
    for g in cf.get("CustomGroups") or []:
        for f in g.get("CustomFields") or []:
            name = f.get("Label") or f.get("FieldName") or f.get("CustomFieldId")
            if f.get("IsTable"):
                rows = []
                for row in f.get("CustomFieldTableRows") or []:
                    rec = {}
                    for k, cell in row.items():
                        if k.startswith("Column") and isinstance(cell, dict):
                            rec[strip_row(cell.get("Label")) or cell.get("CustomField")] = \
                                resolve_picklist(cell.get("Value"), cell.get("PickListItems"))
                    rows.append(rec)
                tables[name] = rows
            else:
                v = f.get("Value")
                if v == "Custom field type is not supported.":
                    v = None
                v = resolve_picklist(v, f.get("PickListItems"), f.get("CustomPickListItems"))
                if name:
                    fields[name] = v
    return fields, tables


def fetch_detail(case_id):
    lic = call(f"/licenses/business/{case_id}")
    cf = call("/customfields/data/", {
        "EntityId": case_id, "ModuleId": MODULE_LICENSE,
        "LayoutId": lic.get("LayoutId"), "OnlineLayoutId": lic.get("OnlineLayoutId"),
    })
    fields, tables = parse_custom_fields(cf)
    keep = ["LicenseNumber", "LicenseType", "Classification", "LicenseDetailsStatus", "LicenseYear",
            "ApplicationDate", "IssuedDate", "ExpirationDate", "LastRenewalDate", "Description",
            "Company", "DBAName", "CompanyType", "BusinessId", "AccountNumber", "IsRenewal"]
    return {"license": {k: lic.get(k) for k in keep}, "more_info": fields, "tables": tables}


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.3, help="seconds each worker waits between licenses")
    ap.add_argument("--workers", type=int, default=3, help="parallel workers (keep small; public site)")
    ap.add_argument("--all-years", action="store_true", help="fetch details for every LTR record, not just year candidates")
    ap.add_argument("--refresh-search", action="store_true", help="ignore cached search listing")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tag = f"ltr_{a.year}" if not a.all_years else "ltr_all"
    search_path = os.path.join(a.out, "ltr_all_search.json")
    cache_path = os.path.join(a.out, "details_cache.jsonl")  # shared across runs/years

    if os.path.exists(search_path) and not a.refresh_search:
        listing = json.load(open(search_path, encoding="utf-8"))
        print(f"Using cached search listing: {len(listing)} LTR records", file=sys.stderr)
    else:
        print("Searching LTR registrations...", file=sys.stderr)
        listing = search_all_ltr()
        json.dump(listing, open(search_path, "w", encoding="utf-8"), indent=0)
        print(f"{len(listing)} LTR records total", file=sys.stderr)
    candidates = listing if a.all_years else [r for r in listing if is_candidate(r, a.year)]
    print(f"{len(candidates)} candidate records", file=sys.stderr)

    done = {}
    if os.path.exists(cache_path):
        for line in open(cache_path, encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                done[r["case_id"]] = r
    todo = [r for r in candidates if r["CaseId"] not in done]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(done)} cached, {len(todo)} to fetch", file=sys.stderr)

    def work(s):
        d = fetch_detail(s["CaseId"])
        time.sleep(a.delay)
        return {
            "case_id": s["CaseId"], "case_number": s["CaseNumber"],
            "case_type": s.get("CaseType"), "workclass": s.get("CaseWorkclass"),
            "status": s.get("CaseStatus"), "license_year": d["license"].get("LicenseYear"),
            "address": s.get("AddressDisplay"), "parcel": s.get("MainParcel"),
            "dba": s.get("DBA"), "apply_date": s.get("ApplyDate"),
            "issue_date": s.get("IssueDate"), "expire_date": s.get("ExpireDate"),
            **d,
        }

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with open(cache_path, "a", encoding="utf-8") as fh, ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(work, s): s for s in todo}
        for i, f in enumerate(as_completed(futs), 1):
            s = futs[f]
            try:
                rec = f.result()
            except Exception as e:
                print(f"  ! {s['CaseNumber']}: {e}", file=sys.stderr)
                continue
            fh.write(json.dumps(rec) + "\n"); fh.flush()
            done[rec["case_id"]] = rec
            if i % 50 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} fetched", file=sys.stderr)

    cand_ids = {r["CaseId"] for r in candidates}
    records = [r for r in done.values() if r["case_id"] in cand_ids
               and (a.all_years or r.get("license_year") == a.year)]
    json.dump(records, open(os.path.join(a.out, f"{tag}.json"), "w", encoding="utf-8"), indent=1)
    write_csvs(records, a.out, tag)
    print(f"Wrote {len(records)} records to {a.out}/", file=sys.stderr)


def write_csvs(records, outdir, tag):
    base_cols = ["case_number", "case_id", "case_type", "workclass", "status", "license_year",
                 "address", "parcel", "dba", "apply_date", "issue_date", "expire_date"]
    lic_rows, unit_rows, mi_cols, unit_cols = [], [], [], []
    for r in records:
        row = {k: r.get(k) for k in base_cols}
        row["company"] = r["license"].get("Company")
        for k, v in r["more_info"].items():
            if k not in mi_cols:
                mi_cols.append(k)
            row["mi:" + k] = v
        lic_rows.append(row)
        for tname, trows in r["tables"].items():
            for n, u in enumerate(trows, 1):
                urow = {"case_number": r["case_number"], "address": r["address"], "status": r["status"],
                        "license_year": r["license_year"], "table": tname, "row": n, **u}
                for k in u:
                    if k not in unit_cols:
                        unit_cols.append(k)
                unit_rows.append(urow)
    lic_fields = base_cols + ["company"] + ["mi:" + c for c in mi_cols]
    with open(os.path.join(outdir, f"{tag}_licenses.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, lic_fields, extrasaction="ignore"); w.writeheader(); w.writerows(lic_rows)
    ufields = ["case_number", "address", "status", "license_year", "table", "row"] + unit_cols
    with open(os.path.join(outdir, f"{tag}_units.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ufields, extrasaction="ignore"); w.writeheader(); w.writerows(unit_rows)


if __name__ == "__main__":
    main()
