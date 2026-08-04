VDS_QUERY_GUIDE = """
query-datasource requires datasourceLuid (UUID from list-datasources) and a query object with fields[].

Rules (apply to EVERY datasource):
- Always call get-datasource-metadata (or list-published-datasource-fields) BEFORE the first query.
- Use exact fieldCaption strings from metadata (case-sensitive). Never invent captions.
- Do NOT put "limit" inside query (rejected by this MCP schema). Keep queries small with few fields.
- Every filter needs filterType and field.fieldCaption.
- Numeric thresholds: filterType QUANTITATIVE_NUMERICAL with quantitativeFilterType "MIN"/"MAX"/"RANGE".
- Date ranges: prefer QUANTITATIVE_DATE with minDate/maxDate (ISO dates, e.g. Aug 2021 → minDate 2021-08-01, maxDate 2021-08-31).
- Categorical buckets (status, region, term band, flag): filterType SET or MATCH with the exact values from data/metadata — never omit the filter.
- Aggregation from metadata (critical):
  * If defaultAggregation is AGG, or the field is an already-aggregated calculation (often named "Total Sales", "… YTD", "… MTD"):
    query it with NO function (omit "function") — never SUM/AVG/COUNT it again.
  * Errors like "cannot be further aggregated" / "Argument to SUM … invalid" mean remove the function and retry.
  * Only use function SUM/AVG/MIN/MAX/COUNT/COUNTD on raw measures whose defaultAggregation is SUM/AVG/etc. (not AGG).
- Prefer a raw measure + date filter for historic months (e.g. "Aug 2021"). Avoid "Current Month" / "Current Year" / "Previous …" calc fields for historic dates — those are relative to today.
- "How many X" / distinct entities (customers, creditors, invoices): use COUNTD on the entity id/name field — never COUNT of rows unless the user asked for row count.
- One question = one intended query shape. If the user asks short vs medium vs long (or any segment), run SEPARATE filtered queries — never reuse one unfiltered total for every segment.
- If two different questions would return the same number, re-check filters — that is usually a bug.
- If query-datasource errors, read the error, fix caption/filterType/aggregation, and retry once or twice.
- If you cannot find a matching field, say so — do not invent a business definition or guess a number.

Example — already-aggregated measure (defaultAggregation AGG) with month filter:
{
  "datasourceLuid": "<uuid>",
  "query": {
    "fields": [
      { "fieldCaption": "Total Sales", "fieldAlias": "Total Sales" }
    ],
    "filters": [
      {
        "field": { "fieldCaption": "<Date Dimension Caption>" },
        "filterType": "QUANTITATIVE_DATE",
        "quantitativeFilterType": "RANGE",
        "minDate": "2021-08-01",
        "maxDate": "2021-08-31"
      }
    ]
  }
}

Example — filtered SUM on a raw measure (defaultAggregation SUM):
{
  "datasourceLuid": "<uuid>",
  "query": {
    "fields": [
      { "fieldCaption": "<Measure Caption>", "function": "SUM", "fieldAlias": "Total" }
    ],
    "filters": [
      {
        "field": { "fieldCaption": "<Category Caption>" },
        "filterType": "SET",
        "values": ["ExactValueFromData"]
      }
    ]
  }
}

Example — distinct count:
{
  "datasourceLuid": "<uuid>",
  "query": {
    "fields": [
      { "fieldCaption": "<Entity Caption>", "function": "COUNTD", "fieldAlias": "Distinct Count" }
    ]
  }
}

Example — measure threshold:
{
  "datasourceLuid": "<uuid>",
  "query": {
    "fields": [
      { "fieldCaption": "<Id Caption>" },
      { "fieldCaption": "<Measure Caption>" }
    ],
    "filters": [
      {
        "field": { "fieldCaption": "<Measure Caption>" },
        "filterType": "QUANTITATIVE_NUMERICAL",
        "quantitativeFilterType": "MIN",
        "min": 10000,
        "includeNulls": false
      }
    ]
  }
}
""".strip()
