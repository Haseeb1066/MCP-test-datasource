VDS_QUERY_GUIDE = """
query-datasource requires datasourceLuid (UUID from list-datasources) and a query object with fields[].

Rules (apply to EVERY datasource):
- Always call get-datasource-metadata (or list-published-datasource-fields) BEFORE the first query.
- Use exact fieldCaption strings from metadata (case-sensitive). Never invent captions.
- Do NOT put "limit" inside query (rejected by this MCP schema). Keep queries small with few fields.
- Every filter needs filterType and field.fieldCaption.
- Numeric thresholds: filterType QUANTITATIVE_NUMERICAL with quantitativeFilterType "MIN"/"MAX"/"RANGE".
- Date ranges: prefer QUANTITATIVE_DATE with minDate/maxDate (ISO dates).
- Categorical buckets (status, region, term band, flag): filterType SET or MATCH with the exact values from data/metadata — never omit the filter.
- Measures for "total"/"sum": set function SUM (or AVG/MIN/MAX as asked).
- "How many X" / distinct entities (customers, creditors, invoices): use COUNTD on the entity id/name field — never COUNT of rows unless the user asked for row count.
- One question = one intended query shape. If the user asks short vs medium vs long (or any segment), run SEPARATE filtered queries — never reuse one unfiltered total for every segment.
- If two different questions would return the same number, re-check filters — that is usually a bug.
- If query-datasource errors, read the error, fix caption/filterType, and retry once or twice.
- If you cannot find a matching field, say so — do not invent a business definition or guess a number.

Example — filtered SUM:
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
