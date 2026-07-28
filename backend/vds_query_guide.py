VDS_QUERY_GUIDE = """
query-datasource requires datasourceLuid (UUID from list-datasources) and a query object with fields[].

Rules:
- Use exact fieldCaption strings from get-datasource-metadata or list-published-datasource-fields (case-sensitive).
- Prefer get-datasource-metadata first (works when Metadata GraphQL is forbidden).
- Do NOT put "limit" inside query (rejected by this MCP schema). Keep queries small with few fields.
- Every filter needs filterType and field.fieldCaption.
- Numeric thresholds (e.g. Outstanding Amount > 10000): use filterType QUANTITATIVE_NUMERICAL with
  quantitativeFilterType "MIN" and min: 10000 (includeNulls optional). Prefer MIN over RANGE for "greater than".
- For "last year" when the current year is Y: prefer QUANTITATIVE_DATE on the date field with minDate Y-1-01-01 and maxDate Y-1-12-31.
- Start with 1–3 fields; add dimensions only after a simple query succeeds.
- If query-datasource errors, read the error text, fix filterType/captions, and retry.

Example — invoices with Outstanding Amount >= 10000:
{
  "datasourceLuid": "<uuid>",
  "query": {
    "fields": [
      { "fieldCaption": "Invoice #" },
      { "fieldCaption": "Creditor" },
      { "fieldCaption": "Outstanding Amount" }
    ],
    "filters": [
      {
        "field": { "fieldCaption": "Outstanding Amount" },
        "filterType": "QUANTITATIVE_NUMERICAL",
        "quantitativeFilterType": "MIN",
        "min": 10000,
        "includeNulls": false
      }
    ]
  }
}

Example — SUM measure without filter:
{
  "datasourceLuid": "<uuid>",
  "query": {
    "fields": [
      { "fieldCaption": "Outstanding Amount", "function": "SUM", "fieldAlias": "Total Outstanding" }
    ]
  }
}
""".strip()
