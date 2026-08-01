/** Minimal Tableau Extensions API types used by this app. */
interface TableauDataSource {
  readonly id: string;
  readonly name: string;
  readonly isPublished?: boolean;
}

interface TableauWorksheet {
  readonly name: string;
  getDataSourcesAsync(): Promise<TableauDataSource[]>;
}

interface TableauWorkbook {
  readonly name: string;
  getAllDataSourcesAsync?: () => Promise<TableauDataSource[]>;
}

interface TableauDashboard {
  readonly name: string;
  readonly workbook: TableauWorkbook;
  readonly worksheets: ReadonlyArray<TableauWorksheet>;
}

interface TableauSettings {
  get(key: string): string | undefined;
  set(key: string, value: string): void;
  initializeAsync(): Promise<void>;
  saveAsync(): Promise<void>;
}

interface TableauEnvironment {
  readonly uniqueUserId?: string;
  readonly tableauVersion?: string;
  readonly apiVersion?: string;
  readonly mode?: string;
}

interface TableauExtensionsApi {
  initializeAsync(): Promise<void>;
  readonly dashboardContent: {
    readonly dashboard: TableauDashboard;
  };
  readonly settings: TableauSettings;
  readonly environment?: TableauEnvironment;
}

interface TableauGlobal {
  readonly extensions: TableauExtensionsApi;
}

interface Window {
  tableau?: TableauGlobal;
}
