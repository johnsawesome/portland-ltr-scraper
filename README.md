# Portland, ME Long-Term Rental Registration Scraper

Exports Portland, Maine **Long Term Housing Registrations** (LTR), including each registration's
**"More Info" supplemental rent table**, from the City's public
[EnerGov Citizen Self-Service portal](https://selfservice.portlandmaine.gov/EnerGov_Prod/SelfService#/home)
into JSON and CSV for bulk analysis.

No login is needed. Everything here is shown to anonymous visitors on the portal's public pages.

## Usage

```bash
python scrape_ltr.py --year 2026            # full run; resumable if interrupted
python scrape_ltr.py --year 2026 --limit 5  # quick test
python scrape_ltr.py --all-years            # every LTR record, regardless of year
```

The script uses only the Python 3 standard library. By default it runs 3 workers, each pausing 0.3 s
between licenses. A full 2026 run took about 50 minutes.

## Output (`out/`)

| File | Contents |
|---|---|
| `ltr_2026_units.csv` | **One row per unit**: rents, increases, deposit, bedrooms and baths |
| `ltr_2026_licenses.csv` | One row per registration: address, owner/DBA, status, unit counts, flags |
| `ltr_2026.json` | Full nested record per registration (license detail, More Info fields, tables) |
| `ltr_all_search.json` | Raw search listing of all ~15,246 LTR records, all years |
| `details_cache.jsonl` | Per-license fetch cache that makes runs resumable |

The 2026 snapshot was taken 2026-11-12: 4,082 registrations and 19,132 unit rows.

### Unit table columns ("BL Supplimental Information")
Unit Number · Unit Description\* · Base Rent (June 2020) · Previous Rent · a) Allowable Increase ($) ·
b) Tax Rate Rent Adjustment ($) · b) 5% New Tenancy Increase ($) · c) Banked Rent Used ($) ·
d) Rent Board Approved Increase ($) · Total Increase ($) · Current Rent (as of Nov 1) ·
Banked Rent Remaining · Current Security Deposit · Other Payments Collected · Bedrooms · Bathrooms · Kitchen Included

\*Despite its label, "Unit Description" holds the unit's **rent-control exemption answer**
(e.g. "None of the above", "2to4 unit building one of which landlord occupies?").

## How the API works

The portal is a Tyler Technologies **EnerGov Citizen Self-Service** (CSS) AngularJS app. The browser
gets its data from an undocumented JSON REST API under:

```
https://selfservice.portlandmaine.gov/EnerGov_Prod/SelfService/api/energov/
```

Every request sends these headers, the same ones the web app sends:

```
tenantId: 1
tenantName: EnerGovProd
Tyler-TenantUrl: home
Tyler-Tenant-Culture: en-US
Content-Type: application/json;charset=UTF-8
```

Responses are wrapped as `{"Result": ..., "Success": true, "ErrorMessage": ...}`.

### 1. Search: `GET search/criteria`, then `POST search/search`
`search/criteria` returns an empty criteria template. Fill it in and POST it to `search/search`:

- `SearchModule: 10` (License) and `FilterModule: 1`. With `SearchModule: 1` (the global "All"
  search), the advanced criteria are **silently ignored**.
- `LicenseCriteria.LicenseTypeId` must be `"<typeId>_<classId>"`. The LTR type is
  `ddf09feb-ce7d-4437-839d-33296fd1c849`, and the classes are Multi Family `c7f4be57-…`, Single Family
  `5c7dadf7-…` and Two Family `0b3794bb-…`. Unused ID filters are set to `"none"`.
- Paging goes in `LicenseCriteria.PageNumber` / `PageSize` (500 works).
- The backend is Elasticsearch with a **10,000-hit result window**, so the script runs one query per class.
- Rows from this module have `LicenseYear: null`, so the script pre-filters by expiration and apply
  dates, then confirms the year from the detail call.

### 2. License detail: `GET licenses/business/{caseId}`
Returns the license header (number, type, class, status, `LicenseYear`, dates, company/DBA) plus
`LayoutId` and `OnlineLayoutId`, which point to the custom-field layout. The public UI page for a
license is `#/businessLicense/{caseId}?tab=moreinfo`.

### 3. "More Info" custom fields: `POST customfields/data/`
```json
{"EntityId": "<caseId>", "ModuleId": 8, "LayoutId": "<LayoutId>", "OnlineLayoutId": "<OnlineLayoutId>"}
```
Returns `CustomGroups[].CustomFields[]`. Scalar fields carry a `Value`. Table fields (`IsTable: true`)
carry `CustomFieldTableRows[]` with cells `Column0..ColumnN`, each holding a `Label`
(e.g. `"Base Rent @ Row 1"`) and a `Value`. Pick-list values come back as item GUIDs, which the
script resolves to text from each cell's `PickListItems`.

## Caveats
- Each registration is a single case that is renewed every year, so the portal only shows the
  **current** values. Rent history is limited to the fields in the table: Base Rent (June 2020),
  Previous Rent and Current Rent.
- Candidate selection for a year uses dates. `--all-years` checks every record instead.
- The API is undocumented and may change if the City upgrades EnerGov.
