import { apiUrl, isTableauHost } from "./config";
import { readJson } from "./api";

export type WorkbookSummary = {
  id: string;
  name: string;
  contentUrl?: string;
  projectName?: string;
  defaultViewId?: string;
};

export type DatasourceSummary = {
  id: string;
  name: string;
  projectName?: string;
  isPublished?: boolean;
};

export type ExtensionContext = {
  workbook: WorkbookSummary;
  datasources: DatasourceSummary[];
  dashboardName: string;
  worksheetNames: string[];
  source:
    | "settings"
    | "query"
    | "tableau-name"
    | "tableau-contentUrl"
    | "referrer-contentUrl";
};

const SETTINGS_WORKBOOK_ID = "workbookId";
const SETTINGS_WORKBOOK_NAME = "workbookName";
const SETTINGS_CONTENT_URL = "contentUrl";

function sanitizeParam(value: string | null): string | null {
  if (!value) return null;
  const cleaned = value.trim().replace(/\\+$/g, "").trim();
  return cleaned || null;
}

function testParamsFromQuery(): {
  workbookId: string | null;
  workbookName: string | null;
  projectName: string | null;
  contentUrl: string | null;
} {
  const params = new URLSearchParams(window.location.search);
  return {
    workbookId: sanitizeParam(params.get("workbookId")),
    workbookName: sanitizeParam(params.get("workbookName")),
    projectName: sanitizeParam(params.get("projectName")),
    contentUrl: sanitizeParam(params.get("contentUrl")),
  };
}

async function resolveWorkbook(options: {
  workbookId?: string;
  name?: string;
  projectName?: string;
  contentUrl?: string;
}): Promise<WorkbookSummary> {
  const qs = new URLSearchParams();
  if (options.workbookId) qs.set("workbookId", options.workbookId);
  if (options.name) qs.set("name", options.name);
  if (options.projectName) qs.set("projectName", options.projectName);
  if (options.contentUrl) qs.set("contentUrl", options.contentUrl);
  const res = await fetch(apiUrl(`/api/workbooks/resolve?${qs}`));
  const data = await readJson<{ workbook: WorkbookSummary; detail?: string }>(res);
  if (!data.workbook?.id) {
    throw new Error(data.detail ?? "Could not resolve workbook on the server.");
  }
  return data.workbook;
}

async function resolveDatasources(options: {
  names?: string[];
  workbookId?: string;
}): Promise<DatasourceSummary[]> {
  const qs = new URLSearchParams();
  if (options.names?.length) qs.set("names", options.names.join(","));
  if (options.workbookId) qs.set("workbookId", options.workbookId);
  if (![...qs.keys()].length) return [];
  try {
    const res = await fetch(apiUrl(`/api/datasources/resolve?${qs}`));
    if (!res.ok) return [];
    const data = await readJson<{ datasources?: DatasourceSummary[] }>(res);
    return Array.isArray(data.datasources) ? data.datasources : [];
  } catch {
    return [];
  }
}

async function collectTableauDatasources(dashboard: TableauDashboard): Promise<
  Array<{ name: string; isPublished?: boolean }>
> {
  const byName = new Map<string, { name: string; isPublished?: boolean }>();

  const workbook = dashboard.workbook;
  if (typeof workbook.getAllDataSourcesAsync === "function") {
    try {
      const all = await workbook.getAllDataSourcesAsync();
      for (const ds of all) {
        if (!ds?.name) continue;
        byName.set(ds.name, {
          name: ds.name,
          isPublished: typeof ds.isPublished === "boolean" ? ds.isPublished : undefined,
        });
      }
    } catch {
      /* fall through to worksheets */
    }
  }

  if (byName.size === 0) {
    const results = await Promise.all(
      dashboard.worksheets.map(async (ws) => {
        try {
          return await ws.getDataSourcesAsync();
        } catch {
          return [] as TableauDataSource[];
        }
      })
    );
    for (const list of results) {
      for (const ds of list) {
        if (!ds?.name) continue;
        byName.set(ds.name, {
          name: ds.name,
          isPublished: typeof ds.isPublished === "boolean" ? ds.isPublished : undefined,
        });
      }
    }
  }

  return [...byName.values()];
}

async function attachDatasources(
  workbook: WorkbookSummary,
  detected: Array<{ name: string; isPublished?: boolean }> = []
): Promise<DatasourceSummary[]> {
  const names = detected.map((d) => d.name);
  const resolved = await resolveDatasources({
    names: names.length ? names : undefined,
    workbookId: workbook.id,
  });
  if (resolved.length > 0) return resolved;

  // Fall back to detected names without server LUID (chat will try resolve again)
  return detected.map((d) => ({
    id: "",
    name: d.name,
    isPublished: d.isPublished,
  }));
}

/**
 * Extract workbook contentUrl slug from Tableau dashboard URLs.
 * e.g. .../views/AccountsPayableAI-MCP/ExecutiveSummary
 */
export function parseContentUrlFromTableauUrl(url: string): string | null {
  try {
    const decoded = decodeURIComponent(url);
    const patterns = [
      /\/views\/([^/?#:]+)(?:\/|[?#:]|$)/i,
      /#\/[^/]+\/[^/]+\/views\/([^/?#:]+)(?:\/|[?#:]|$)/i,
      /[?&]contentUrl=([^&]+)/i,
    ];
    for (const pattern of patterns) {
      const match = decoded.match(pattern);
      if (match?.[1]) return match[1];
    }
  } catch {
    /* ignore malformed URLs */
  }
  return null;
}

function getDashboardUrlHints(): string[] {
  const hints: string[] = [];
  if (document.referrer) hints.push(document.referrer);
  hints.push(window.location.href);

  const ancestorOrigins = (
    document.location as Location & { ancestorOrigins?: DOMStringList }
  ).ancestorOrigins;
  if (ancestorOrigins) {
    for (let i = 0; i < ancestorOrigins.length; i++) {
      hints.push(ancestorOrigins[i]);
    }
  }

  return [...new Set(hints)];
}

function parseContentUrlFromDashboardHints(): string | null {
  for (const hint of getDashboardUrlHints()) {
    const contentUrl = parseContentUrlFromTableauUrl(hint);
    if (contentUrl) return contentUrl;
  }
  return null;
}

/** Poll — dashboard URL / referrer may appear shortly after iframe load. */
async function waitForContentUrlHint(timeoutMs = 5_000): Promise<string | null> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const contentUrl = parseContentUrlFromDashboardHints();
    if (contentUrl) return contentUrl;
    await new Promise((r) => setTimeout(r, 100));
  }
  return null;
}

async function detectDashboardContentUrl(
  test: ReturnType<typeof testParamsFromQuery>
): Promise<string | null> {
  if (test.contentUrl) return test.contentUrl;
  return parseContentUrlFromDashboardHints() || (await waitForContentUrlHint());
}

function sessionWorkbookKey(contentUrl: string): string {
  return `mcp-workbook:${contentUrl}`;
}

function readSessionWorkbookId(contentUrl: string): string | null {
  try {
    return sessionStorage.getItem(sessionWorkbookKey(contentUrl));
  } catch {
    return null;
  }
}

function writeSessionWorkbookId(contentUrl: string, workbookId: string): void {
  try {
    sessionStorage.setItem(sessionWorkbookKey(contentUrl), workbookId);
  } catch {
    /* ignore */
  }
}

async function resolveByContentUrl(
  contentUrl: string,
  source: "query" | "referrer-contentUrl"
): Promise<ExtensionContext> {
  const cachedId = readSessionWorkbookId(contentUrl);
  if (cachedId) {
    try {
      const workbook = await resolveWorkbook({ workbookId: cachedId });
      const datasources = await attachDatasources(workbook);
      return extensionContextWithoutApi(workbook, contentUrl, source, datasources);
    } catch {
      /* stale session cache */
    }
  }

  const workbook = await resolveWorkbook({ contentUrl });
  writeSessionWorkbookId(contentUrl, workbook.id);
  const datasources = await attachDatasources(workbook);
  return extensionContextWithoutApi(workbook, contentUrl, source, datasources);
}

function contentUrlCandidatesFromName(name: string): string[] {
  const trimmed = name.trim();
  if (!trimmed) return [];
  const candidates = [
    trimmed.replace(/[()]/g, "").replace(/\s+/g, ""),
    trimmed.replace(/\s+/g, ""),
    trimmed.replace(/[^a-zA-Z0-9]/g, ""),
  ];
  return [...new Set(candidates.filter((c) => c.length > 0))];
}

async function resolveFromTableauWorkbook(
  workbookName: string,
  projectName?: string
): Promise<{ workbook: WorkbookSummary; source: "tableau-name" | "tableau-contentUrl" }> {
  try {
    const workbook = await resolveWorkbook({ name: workbookName, projectName });
    return { workbook, source: "tableau-name" };
  } catch (nameErr) {
    for (const contentUrl of contentUrlCandidatesFromName(workbookName)) {
      try {
        const workbook = await resolveWorkbook({ contentUrl });
        return { workbook, source: "tableau-contentUrl" };
      } catch {
        /* try next slug */
      }
    }
    throw nameErr instanceof Error ? nameErr : new Error(String(nameErr));
  }
}

async function waitForTableauApi(timeoutMs = 12_000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (window.tableau?.extensions) return window.tableau.extensions;
    await new Promise((r) => setTimeout(r, 80));
  }
  return null;
}

async function persistWorkbookContext(
  settings: TableauSettings,
  workbook: WorkbookSummary,
  tableauWorkbookName: string,
  contentUrl?: string
): Promise<void> {
  settings.set(SETTINGS_WORKBOOK_ID, workbook.id);
  settings.set(SETTINGS_WORKBOOK_NAME, tableauWorkbookName);
  if (contentUrl) settings.set(SETTINGS_CONTENT_URL, contentUrl);
  await settings.saveAsync();
  if (contentUrl) writeSessionWorkbookId(contentUrl, workbook.id);
}

function extensionContextFromDashboard(
  dashboard: TableauDashboard,
  workbook: WorkbookSummary,
  source: ExtensionContext["source"],
  datasources: DatasourceSummary[] = []
): ExtensionContext {
  return {
    workbook,
    datasources,
    dashboardName: dashboard.name,
    worksheetNames: dashboard.worksheets.map((w) => w.name),
    source,
  };
}

function extensionContextWithoutApi(
  workbook: WorkbookSummary,
  dashboardName: string,
  source: ExtensionContext["source"],
  datasources: DatasourceSummary[] = []
): ExtensionContext {
  return {
    workbook,
    datasources,
    dashboardName,
    worksheetNames: [],
    source,
  };
}

/** Dynamic path: read workbook slug from dashboard URL (parallel with Tableau API). */
async function loadFromDashboardUrl(
  test: ReturnType<typeof testParamsFromQuery>
): Promise<ExtensionContext> {
  const contentUrl = await detectDashboardContentUrl(test);
  if (!contentUrl) {
    throw new Error("No workbook slug in dashboard URL");
  }
  return resolveByContentUrl(contentUrl, test.contentUrl ? "query" : "referrer-contentUrl");
}

async function loadFromTableauApi(
  test: ReturnType<typeof testParamsFromQuery>
): Promise<ExtensionContext> {
  const api = await waitForTableauApi();
  if (!api) {
    throw new Error("Tableau Extensions API not available");
  }

  await api.initializeAsync();
  await api.settings.initializeAsync();
  const dashboard = api.dashboardContent.dashboard;
  const settings = api.settings;
  const tableauWorkbookName = dashboard.workbook.name;
  const detectedContentUrl = parseContentUrlFromDashboardHints();
  const detectedDatasources = await collectTableauDatasources(dashboard);

  const storedId = sanitizeParam(settings.get(SETTINGS_WORKBOOK_ID) ?? null);
  const storedName = sanitizeParam(settings.get(SETTINGS_WORKBOOK_NAME) ?? null);
  const storedContentUrl = sanitizeParam(settings.get(SETTINGS_CONTENT_URL) ?? null);

  const cacheKeyMatches =
    (detectedContentUrl && storedContentUrl === detectedContentUrl) ||
    (storedName && storedName === tableauWorkbookName);

  if (storedId && cacheKeyMatches) {
    try {
      const workbook = await resolveWorkbook({ workbookId: storedId });
      const datasources = await attachDatasources(workbook, detectedDatasources);
      return extensionContextFromDashboard(dashboard, workbook, "settings", datasources);
    } catch {
      /* stale cache */
    }
  }

  if (detectedContentUrl) {
    try {
      const workbook = await resolveWorkbook({ contentUrl: detectedContentUrl });
      await persistWorkbookContext(settings, workbook, tableauWorkbookName, detectedContentUrl);
      const datasources = await attachDatasources(workbook, detectedDatasources);
      return extensionContextFromDashboard(
        dashboard,
        workbook,
        "referrer-contentUrl",
        datasources
      );
    } catch {
      /* try name resolve */
    }
  }

  const { workbook, source } = await resolveFromTableauWorkbook(
    tableauWorkbookName,
    test.projectName ?? undefined
  );
  await persistWorkbookContext(
    settings,
    workbook,
    tableauWorkbookName,
    workbook.contentUrl ?? detectedContentUrl ?? undefined
  );
  const datasources = await attachDatasources(workbook, detectedDatasources);
  return extensionContextFromDashboard(dashboard, workbook, source, datasources);
}

async function loadFromTableauHost(test: ReturnType<typeof testParamsFromQuery>): Promise<ExtensionContext> {
  if (test.workbookId) {
    const workbook = await resolveWorkbook({ workbookId: test.workbookId });
    const datasources = await attachDatasources(workbook);
    return extensionContextWithoutApi(workbook, "Dashboard", "query", datasources);
  }

  if (test.contentUrl) {
    return resolveByContentUrl(test.contentUrl, "query");
  }

  // Prefer Tableau API when it yields datasources; URL slug is the fallback.
  const results = await Promise.allSettled([
    loadFromTableauApi(test),
    loadFromDashboardUrl(test),
  ]);
  const contexts = results
    .filter((r): r is PromiseFulfilledResult<ExtensionContext> => r.status === "fulfilled")
    .map((r) => r.value);
  if (contexts.length === 0) {
    throw new Error(
      "Could not detect workbook for this dashboard. Allowlist https://mcp-test-ldxl.onrender.com on Tableau Server (site demo), then reload."
    );
  }
  return (
    contexts.find((c) => c.datasources.length > 0) ??
    contexts.find((c) => c.source !== "referrer-contentUrl") ??
    contexts[0]
  );
}

async function loadFromLocalTest(test: ReturnType<typeof testParamsFromQuery>): Promise<ExtensionContext> {
  if (test.workbookId) {
    const workbook = await resolveWorkbook({ workbookId: test.workbookId });
    const datasources = await attachDatasources(workbook);
    return {
      workbook,
      datasources,
      dashboardName: "Test dashboard",
      worksheetNames: [],
      source: "query",
    };
  }
  if (test.contentUrl) {
    return resolveByContentUrl(test.contentUrl, "query");
  }
  if (test.workbookName) {
    const { workbook, source } = await resolveFromTableauWorkbook(
      test.workbookName,
      test.projectName ?? undefined
    );
    const datasources = await attachDatasources(workbook);
    return {
      workbook,
      datasources,
      dashboardName: "Test dashboard",
      worksheetNames: [],
      source,
    };
  }

  const contentUrl = await detectDashboardContentUrl(test);
  if (contentUrl) {
    return resolveByContentUrl(contentUrl, "referrer-contentUrl");
  }

  throw new Error(
    "Open from a Tableau dashboard extension, or add ?contentUrl=WorkbookSlug for testing."
  );
}

/**
 * Dynamically resolve workbook ID on extension start.
 * .trex URL must be plain (no query params) — slug is read from each dashboard URL.
 */
export async function loadExtensionContext(): Promise<ExtensionContext> {
  const test = testParamsFromQuery();
  if (isTableauHost()) {
    return loadFromTableauHost(test);
  }
  return loadFromLocalTest(test);
}
