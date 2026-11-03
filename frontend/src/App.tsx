import { lazy } from "react";
import { Route, Routes } from "react-router-dom";
import { AppLayout } from "./layouts/AppLayout";


// Every page is its own chunk: the shell loads first, each section on first visit.
const AdminPage = lazy(() => import("./pages/admin/AdminPage").then((m) => ({ default: m.AdminPage })));
const AccountPage = lazy(() => import("./pages/auth/AccountPage").then((m) => ({ default: m.AccountPage })));
const LoginPage = lazy(() => import("./pages/auth/LoginPage").then((m) => ({ default: m.LoginPage })));
const BatchDetailPage = lazy(() => import("./pages/BatchDetailPage").then((m) => ({ default: m.BatchDetailPage })));
const DataOperationsPage = lazy(() => import("./pages/DataOperationsPage").then((m) => ({ default: m.DataOperationsPage })));
const ImportWizardPage = lazy(() => import("./pages/ImportWizardPage").then((m) => ({ default: m.ImportWizardPage })));
const MappingProfilesPage = lazy(() => import("./pages/MappingProfilesPage").then((m) => ({ default: m.MappingProfilesPage })));
const ProductDetailPage = lazy(() => import("./pages/ProductsPage").then((m) => ({ default: m.ProductDetailPage })));
const ProductsPage = lazy(() => import("./pages/ProductsPage").then((m) => ({ default: m.ProductsPage })));
const QualityPage = lazy(() => import("./pages/QualityPage").then((m) => ({ default: m.QualityPage })));
const QualityBatchPage = lazy(() => import("./pages/quality/QualityBatchPage").then((m) => ({ default: m.QualityBatchPage })));
const QualityEntityPage = lazy(() => import("./pages/quality/QualityEntityPage").then((m) => ({ default: m.QualityEntityPage })));
const QualityFindingsPage = lazy(() => import("./pages/quality/QualityFindingsPage").then((m) => ({ default: m.QualityFindingsPage })));
const QualityLayout = lazy(() => import("./pages/quality/QualityLayout").then((m) => ({ default: m.QualityLayout })));
const QualityOverviewPage = lazy(() => import("./pages/quality/QualityOverviewPage").then((m) => ({ default: m.QualityOverviewPage })));
const QualityRulesPage = lazy(() => import("./pages/quality/QualityRulesPage").then((m) => ({ default: m.QualityRulesPage })));
const QualityRunsPage = lazy(() => import("./pages/quality/QualityRunsPage").then((m) => ({ default: m.QualityRunsPage })));
const CandidateReviewPage = lazy(() => import("./pages/registry/CandidateReviewPage").then((m) => ({ default: m.CandidateReviewPage })));
const DuplicatesPage = lazy(() => import("./pages/registry/DuplicatesPage").then((m) => ({ default: m.DuplicatesPage })));
const MergePage = lazy(() => import("./pages/registry/MergePage").then((m) => ({ default: m.MergePage })));
const MergeDetailPage = lazy(() => import("./pages/registry/MergesPage").then((m) => ({ default: m.MergeDetailPage })));
const MergesPage = lazy(() => import("./pages/registry/MergesPage").then((m) => ({ default: m.MergesPage })));
const RegistryExplorerPage = lazy(() => import("./pages/registry/RegistryExplorerPage").then((m) => ({ default: m.RegistryExplorerPage })));
const RegistryLayout = lazy(() => import("./pages/registry/RegistryLayout").then((m) => ({ default: m.RegistryLayout })));
const RegistryOverviewPage = lazy(() => import("./pages/registry/RegistryOverviewPage").then((m) => ({ default: m.RegistryOverviewPage })));
const RegistryQualityPage = lazy(() => import("./pages/registry/RegistryQualityPage").then((m) => ({ default: m.RegistryQualityPage })));
const RegistryRecordPage = lazy(() => import("./pages/registry/RegistryRecordPage").then((m) => ({ default: m.RegistryRecordPage })));
const InvAboutPage = lazy(() => import("./pages/replenishment/AboutPage").then((m) => ({ default: m.AboutPage })));
const InvCostsPage = lazy(() => import("./pages/replenishment/CostsPage").then((m) => ({ default: m.CostsPage })));
const InvLayout = lazy(() => import("./pages/replenishment/InvLayout").then((m) => ({ default: m.InvLayout })));
const InvOverviewPage = lazy(() => import("./pages/replenishment/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const InvPlanPage = lazy(() => import("./pages/replenishment/PlanPage").then((m) => ({ default: m.PlanPage })));
const InvRunHistoryPage = lazy(() => import("./pages/replenishment/RunHistoryPage").then((m) => ({ default: m.RunHistoryPage })));
const InvRunPage = lazy(() => import("./pages/replenishment/RunPage").then((m) => ({ default: m.RunPage })));
const AlertDetailPage = lazy(() => import("./pages/alerting/AlertDetailPage").then((m) => ({ default: m.AlertDetailPage })));
const AlertingLayout = lazy(() => import("./pages/alerting/AlertingLayout").then((m) => ({ default: m.AlertingLayout })));
const OpsAlertsPage = lazy(() => import("./pages/alerting/AlertsPage").then((m) => ({ default: m.AlertsPage })));
const InboxPage = lazy(() => import("./pages/alerting/InboxPage").then((m) => ({ default: m.InboxPage })));
const OperationsPage = lazy(() => import("./pages/alerting/OperationsPage").then((m) => ({ default: m.OperationsPage })));
const RulesPage = lazy(() => import("./pages/alerting/RulesPage").then((m) => ({ default: m.RulesPage })));
const AnalyticsAlertsPage = lazy(() => import("./pages/analytics/AlertsPage").then((m) => ({ default: m.AlertsPage })));
const AnalyticsLayout = lazy(() => import("./pages/analytics/AnalyticsLayout").then((m) => ({ default: m.AnalyticsLayout })));
const AnalyticsOverviewPage = lazy(() => import("./pages/analytics/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const AnalyticsReportsPage = lazy(() => import("./pages/analytics/ReportsPage").then((m) => ({ default: m.ReportsPage })));
const AnalyticsSuppliersPage = lazy(() => import("./pages/analytics/SuppliersPage").then((m) => ({ default: m.SuppliersPage })));
const AnalyticsDisruptionPage = lazy(() => import("./pages/analytics/WorkspacePages").then((m) => ({ default: m.DisruptionPage })));
const AnalyticsInventoryPage = lazy(() => import("./pages/analytics/WorkspacePages").then((m) => ({ default: m.InventoryPage })));
const AnalyticsLogisticsPage = lazy(() => import("./pages/analytics/WorkspacePages").then((m) => ({ default: m.LogisticsPage })));
const AnalyticsNetworkPage = lazy(() => import("./pages/analytics/WorkspacePages").then((m) => ({ default: m.NetworkPage })));
const DecisionAboutPage = lazy(() => import("./pages/decision/AboutPage").then((m) => ({ default: m.AboutPage })));
const DecisionBuilderPage = lazy(() => import("./pages/decision/BuilderPage").then((m) => ({ default: m.BuilderPage })));
const DecisionCasePage = lazy(() => import("./pages/decision/CasePage").then((m) => ({ default: m.CasePage })));
const DecisionLayout = lazy(() => import("./pages/decision/DecisionLayout").then((m) => ({ default: m.DecisionLayout })));
const DecisionOverviewPage = lazy(() => import("./pages/decision/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const ResAboutPage = lazy(() => import("./pages/resilience/AboutPage").then((m) => ({ default: m.AboutPage })));
const ResConfigurePage = lazy(() => import("./pages/resilience/ConfigurePage").then((m) => ({ default: m.ConfigurePage })));
const ResilienceLayout = lazy(() => import("./pages/resilience/ResilienceLayout").then((m) => ({ default: m.ResilienceLayout })));
const ResRunHistoryPage = lazy(() => import("./pages/resilience/RunHistoryPage").then((m) => ({ default: m.RunHistoryPage })));
const ResRunPage = lazy(() => import("./pages/resilience/RunPage").then((m) => ({ default: m.RunPage })));
const SourcingAboutPage = lazy(() => import("./pages/sourcing/AboutPage").then((m) => ({ default: m.AboutPage })));
const SourcingOffersPage = lazy(() => import("./pages/sourcing/OffersPage").then((m) => ({ default: m.OffersPage })));
const RequirementPage = lazy(() => import("./pages/sourcing/RequirementPage").then((m) => ({ default: m.RequirementPage })));
const SourcingRunHistoryPage = lazy(() => import("./pages/sourcing/RunHistoryPage").then((m) => ({ default: m.RunHistoryPage })));
const SourcingRunPage = lazy(() => import("./pages/sourcing/RunPage").then((m) => ({ default: m.RunPage })));
const SourcingLayout = lazy(() => import("./pages/sourcing/SourcingLayout").then((m) => ({ default: m.SourcingLayout })));
const NetworkLanesPage = lazy(() => import("./pages/transport/NetworkLanesPage").then((m) => ({ default: m.NetworkLanesPage })));
const PlanningPage = lazy(() => import("./pages/transport/PlanningPage").then((m) => ({ default: m.PlanningPage })));
const RunHistoryPage = lazy(() => import("./pages/transport/RunHistoryPage").then((m) => ({ default: m.RunHistoryPage })));
const RunPage = lazy(() => import("./pages/transport/RunPage").then((m) => ({ default: m.RunPage })));
const TransportAboutPage = lazy(() => import("./pages/transport/TransportAboutPage").then((m) => ({ default: m.TransportAboutPage })));
const TransportLayout = lazy(() => import("./pages/transport/TransportLayout").then((m) => ({ default: m.TransportLayout })));
const ModelDetailPage = lazy(() => import("./pages/modelops/ModelDetailPage").then((m) => ({ default: m.ModelDetailPage })));
const ModelopsLayout = lazy(() => import("./pages/modelops/ModelopsLayout").then((m) => ({ default: m.ModelopsLayout })));
const ModelopsAboutPage = lazy(() => import("./pages/modelops/ModelopsRegistryPage").then((m) => ({ default: m.ModelopsAboutPage })));
const ModelopsComparePage = lazy(() => import("./pages/modelops/ModelopsRegistryPage").then((m) => ({ default: m.ModelopsComparePage })));
const ModelopsRegistryPage = lazy(() => import("./pages/modelops/ModelopsRegistryPage").then((m) => ({ default: m.ModelopsRegistryPage })));
const LeadtimeAnalyticsPage = lazy(() => import("./pages/leadtime/LeadtimeAnalyticsPage").then((m) => ({ default: m.LeadtimeAnalyticsPage })));
const LeadtimeExplorerPage = lazy(() => import("./pages/leadtime/LeadtimeExplorerPage").then((m) => ({ default: m.LeadtimeExplorerPage })));
const LeadtimeImpactPage = lazy(() => import("./pages/leadtime/LeadtimeImpactPage").then((m) => ({ default: m.LeadtimeImpactPage })));
const LeadtimeImportPage = lazy(() => import("./pages/leadtime/LeadtimeImportPage").then((m) => ({ default: m.LeadtimeImportPage })));
const LeadtimeLayout = lazy(() => import("./pages/leadtime/LeadtimeLayout").then((m) => ({ default: m.LeadtimeLayout })));
const LeadtimeModelsPage = lazy(() => import("./pages/leadtime/LeadtimeModelsPage").then((m) => ({ default: m.LeadtimeModelsPage })));
const LeadtimeRunPage = lazy(() => import("./pages/leadtime/LeadtimeModelsPage").then((m) => ({ default: m.LeadtimeRunPage })));
const LeadtimeOverviewPage = lazy(() => import("./pages/leadtime/LeadtimeOverviewPage").then((m) => ({ default: m.LeadtimeOverviewPage })));
const LeadtimePredictPage = lazy(() => import("./pages/leadtime/LeadtimePredictPage").then((m) => ({ default: m.LeadtimePredictPage })));
const ForecastLayout = lazy(() => import("./pages/forecast/ForecastLayout").then((m) => ({ default: m.ForecastLayout })));
const ForecastMonitoringPage = lazy(() => import("./pages/forecast/ForecastMonitoringPage").then((m) => ({ default: m.ForecastMonitoringPage })));
const ForecastOverviewPage = lazy(() => import("./pages/forecast/ForecastOverviewPage").then((m) => ({ default: m.ForecastOverviewPage })));
const ForecastRunPage = lazy(() => import("./pages/forecast/ForecastRunPage").then((m) => ({ default: m.ForecastRunPage })));
const ForecastRunsPage = lazy(() => import("./pages/forecast/ForecastRunsPage").then((m) => ({ default: m.ForecastRunsPage })));
const ForecastSeriesDetailPage = lazy(() => import("./pages/forecast/ForecastSeriesDetailPage").then((m) => ({ default: m.ForecastSeriesDetailPage })));
const ForecastSeriesPage = lazy(() => import("./pages/forecast/ForecastSeriesPage").then((m) => ({ default: m.ForecastSeriesPage })));
const InventoryAnalysesPage = lazy(() => import("./pages/inventory/InventoryAnalysesPage").then((m) => ({ default: m.InventoryAnalysesPage })));
const InventoryAnalysisPage = lazy(() => import("./pages/inventory/InventoryAnalysisPage").then((m) => ({ default: m.InventoryAnalysisPage })));
const InventoryComparePage = lazy(() => import("./pages/inventory/InventoryComparePage").then((m) => ({ default: m.InventoryComparePage })));
const InventoryExplorerPage = lazy(() => import("./pages/inventory/InventoryExplorerPage").then((m) => ({ default: m.InventoryExplorerPage })));
const InventoryImportPage = lazy(() => import("./pages/inventory/InventoryImportPage").then((m) => ({ default: m.InventoryImportPage })));
const InventoryLayout = lazy(() => import("./pages/inventory/InventoryLayout").then((m) => ({ default: m.InventoryLayout })));
const InventoryOverviewPage = lazy(() => import("./pages/inventory/InventoryOverviewPage").then((m) => ({ default: m.InventoryOverviewPage })));
const InventoryPoliciesPage = lazy(() => import("./pages/inventory/InventoryPoliciesPage").then((m) => ({ default: m.InventoryPoliciesPage })));
const ImpactAnalysisPage = lazy(() => import("./pages/impact/ImpactAnalysisPage").then((m) => ({ default: m.ImpactAnalysisPage })));
const ImpactComparePage = lazy(() => import("./pages/impact/ImpactComparePage").then((m) => ({ default: m.ImpactComparePage })));
const ImpactLayout = lazy(() => import("./pages/impact/ImpactLayout").then((m) => ({ default: m.ImpactLayout })));
const ImpactListPage = lazy(() => import("./pages/impact/ImpactListPage").then((m) => ({ default: m.ImpactListPage })));
const ScenarioComparePage = lazy(() => import("./pages/scenarios/ScenarioComparePage").then((m) => ({ default: m.ScenarioComparePage })));
const ScenarioEditorPage = lazy(() => import("./pages/scenarios/ScenarioEditorPage").then((m) => ({ default: m.ScenarioEditorPage })));
const ScenarioLayout = lazy(() => import("./pages/scenarios/ScenarioLayout").then((m) => ({ default: m.ScenarioLayout })));
const ScenarioLibraryPage = lazy(() => import("./pages/scenarios/ScenarioLibraryPage").then((m) => ({ default: m.ScenarioLibraryPage })));
const ScenarioNewPage = lazy(() => import("./pages/scenarios/ScenarioNewPage").then((m) => ({ default: m.ScenarioNewPage })));
const ScenarioRunPage = lazy(() => import("./pages/scenarios/ScenarioRunPage").then((m) => ({ default: m.ScenarioRunPage })));
const ScenarioRunsPage = lazy(() => import("./pages/scenarios/ScenarioRunsPage").then((m) => ({ default: m.ScenarioRunsPage })));
const RiskAlertsPage = lazy(() => import("./pages/risk/RiskAlertsPage").then((m) => ({ default: m.RiskAlertsPage })));
const RiskComparePage = lazy(() => import("./pages/risk/RiskComparePage").then((m) => ({ default: m.RiskComparePage })));
const RiskLayout = lazy(() => import("./pages/risk/RiskLayout").then((m) => ({ default: m.RiskLayout })));
const RiskMethodologyPage = lazy(() => import("./pages/risk/RiskMethodologyPage").then((m) => ({ default: m.RiskMethodologyPage })));
const RiskOverviewPage = lazy(() => import("./pages/risk/RiskOverviewPage").then((m) => ({ default: m.RiskOverviewPage })));
const RiskRunsPage = lazy(() => import("./pages/risk/RiskRunsPage").then((m) => ({ default: m.RiskRunsPage })));
const RiskSupplierPage = lazy(() => import("./pages/risk/RiskSupplierPage").then((m) => ({ default: m.RiskSupplierPage })));
const RiskSuppliersPage = lazy(() => import("./pages/risk/RiskSuppliersPage").then((m) => ({ default: m.RiskSuppliersPage })));
const ApiKeysPage = lazy(() => import("./pages/integrations/ApiKeysPage").then((m) => ({ default: m.ApiKeysPage })));
const IntegrationDetailPage = lazy(() => import("./pages/integrations/IntegrationDetailPage").then((m) => ({ default: m.IntegrationDetailPage })));
const IntegrationsLayout = lazy(() => import("./pages/integrations/IntegrationsLayout").then((m) => ({ default: m.IntegrationsLayout })));
const IntegrationsPage = lazy(() => import("./pages/integrations/IntegrationsPage").then((m) => ({ default: m.IntegrationsPage })));
const WebhooksPage = lazy(() => import("./pages/integrations/WebhooksPage").then((m) => ({ default: m.WebhooksPage })));
const DashboardPage = lazy(() => import("./pages/DashboardPage").then((m) => ({ default: m.DashboardPage })));
const FacilitiesPage = lazy(() => import("./pages/FacilitiesPage").then((m) => ({ default: m.FacilitiesPage })));
const FacilityDetailPage = lazy(() => import("./pages/FacilityDetailPage").then((m) => ({ default: m.FacilityDetailPage })));
const NetworkPage = lazy(() => import("./pages/NetworkPage").then((m) => ({ default: m.NetworkPage })));
const NetworkExplorerPage = lazy(() => import("./pages/network/NetworkExplorerPage").then((m) => ({ default: m.NetworkExplorerPage })));
const NotFoundPage = lazy(() => import("./pages/NotFoundPage").then((m) => ({ default: m.NotFoundPage })));
const SupplierDetailPage = lazy(() => import("./pages/SupplierDetailPage").then((m) => ({ default: m.SupplierDetailPage })));
const SuppliersPage = lazy(() => import("./pages/SuppliersPage").then((m) => ({ default: m.SuppliersPage })));

export function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<DashboardPage />} />
        <Route path="suppliers" element={<SuppliersPage />} />
        <Route path="suppliers/:supplierId" element={<SupplierDetailPage />} />
        <Route path="facilities" element={<FacilitiesPage />} />
        <Route path="facilities/:facilityId" element={<FacilityDetailPage />} />
        <Route path="network" element={<NetworkPage />} />
        <Route path="network/explorer" element={<NetworkExplorerPage />} />
        <Route path="products" element={<ProductsPage />} />
        <Route path="products/:productId" element={<ProductDetailPage />} />
        <Route path="data" element={<DataOperationsPage />} />
        <Route path="data/import" element={<ImportWizardPage />} />
        <Route path="data/mappings" element={<MappingProfilesPage />} />
        <Route path="data/quality" element={<QualityPage />} />
        <Route path="data/batches/:batchId" element={<BatchDetailPage />} />
        <Route path="registry" element={<RegistryLayout />}>
          <Route index element={<RegistryOverviewPage />} />
          <Route path="explorer" element={<RegistryExplorerPage />} />
          <Route path="duplicates" element={<DuplicatesPage />} />
          <Route path="duplicates/:id" element={<CandidateReviewPage />} />
          <Route path="merge" element={<MergePage />} />
          <Route path="merges" element={<MergesPage />} />
          <Route path="merges/:id" element={<MergeDetailPage />} />
          <Route path="quality" element={<RegistryQualityPage />} />
          <Route path=":kind/:id" element={<RegistryRecordPage />} />
        </Route>
        <Route path="quality" element={<QualityLayout />}>
          <Route index element={<QualityOverviewPage />} />
          <Route path="findings" element={<QualityFindingsPage />} />
          <Route path="runs" element={<QualityRunsPage />} />
          <Route path="rules" element={<QualityRulesPage />} />
          <Route path="entity/:kind/:id" element={<QualityEntityPage />} />
          <Route path="batches/:id" element={<QualityBatchPage />} />
        </Route>
        <Route path="risk" element={<RiskLayout />}>
          <Route index element={<RiskOverviewPage />} />
          <Route path="suppliers" element={<RiskSuppliersPage />} />
          <Route path="suppliers/:supplierId" element={<RiskSupplierPage />} />
          <Route path="compare" element={<RiskComparePage />} />
          <Route path="alerts" element={<RiskAlertsPage />} />
          <Route path="runs" element={<RiskRunsPage />} />
          <Route path="methodology" element={<RiskMethodologyPage />} />
        </Route>
        <Route path="impact" element={<ImpactLayout />}>
          <Route index element={<ImpactListPage />} />
          <Route path="compare" element={<ImpactComparePage />} />
          <Route path=":id" element={<ImpactAnalysisPage />} />
        </Route>
        <Route path="inventory" element={<InventoryLayout />}>
          <Route index element={<InventoryOverviewPage />} />
          <Route path="positions" element={<InventoryExplorerPage />} />
          <Route path="analyses" element={<InventoryAnalysesPage />} />
          <Route path="analyses/:id" element={<InventoryAnalysisPage />} />
          <Route path="compare" element={<InventoryComparePage />} />
          <Route path="import" element={<InventoryImportPage />} />
          <Route path="policies" element={<InventoryPoliciesPage />} />
        </Route>
        <Route path="forecast" element={<ForecastLayout />}>
          <Route index element={<ForecastOverviewPage />} />
          <Route path="series" element={<ForecastSeriesPage />} />
          <Route path="series/:productId/:facilityId" element={<ForecastSeriesDetailPage />} />
          <Route path="runs" element={<ForecastRunsPage />} />
          <Route path="runs/:id" element={<ForecastRunPage />} />
          <Route path="monitoring" element={<ForecastMonitoringPage />} />
        </Route>
        <Route path="leadtime" element={<LeadtimeLayout />}>
          <Route index element={<LeadtimeOverviewPage />} />
          <Route path="deliveries" element={<LeadtimeExplorerPage />} />
          <Route path="analytics" element={<LeadtimeAnalyticsPage />} />
          <Route path="predict" element={<LeadtimePredictPage />} />
          <Route path="models" element={<LeadtimeModelsPage />} />
          <Route path="models/:id" element={<LeadtimeRunPage />} />
          <Route path="impact" element={<LeadtimeImpactPage />} />
          <Route path="import" element={<LeadtimeImportPage />} />
        </Route>
        <Route path="models" element={<ModelopsLayout />}>
          <Route index element={<ModelopsRegistryPage />} />
          <Route path="compare" element={<ModelopsComparePage />} />
          <Route path="about" element={<ModelopsAboutPage />} />
          <Route path=":uid" element={<ModelDetailPage />} />
        </Route>
        <Route path="transport" element={<TransportLayout />}>
          <Route index element={<PlanningPage />} />
          <Route path="runs" element={<RunHistoryPage />} />
          <Route path="runs/:uid" element={<RunPage />} />
          <Route path="network" element={<NetworkLanesPage />} />
          <Route path="about" element={<TransportAboutPage />} />
        </Route>
        <Route path="resilience" element={<ResilienceLayout />}>
          <Route index element={<ResConfigurePage />} />
          <Route path="runs" element={<ResRunHistoryPage />} />
          <Route path="runs/:uid" element={<ResRunPage />} />
          <Route path="about" element={<ResAboutPage />} />
        </Route>
        <Route path="analytics" element={<AnalyticsLayout />}>
          <Route index element={<AnalyticsOverviewPage />} />
          <Route path="suppliers" element={<AnalyticsSuppliersPage />} />
          <Route path="inventory" element={<AnalyticsInventoryPage />} />
          <Route path="logistics" element={<AnalyticsLogisticsPage />} />
          <Route path="network" element={<AnalyticsNetworkPage />} />
          <Route path="disruption" element={<AnalyticsDisruptionPage />} />
          <Route path="alerts" element={<AnalyticsAlertsPage />} />
          <Route path="reports" element={<AnalyticsReportsPage />} />
        </Route>
        <Route path="integrations" element={<IntegrationsLayout />}>
          <Route index element={<IntegrationsPage />} />
          <Route path="webhooks" element={<WebhooksPage />} />
          <Route path="keys" element={<ApiKeysPage />} />
          <Route path=":uid" element={<IntegrationDetailPage />} />
        </Route>
        <Route path="alerting" element={<AlertingLayout />}>
          <Route index element={<OpsAlertsPage />} />
          <Route path="alerts/:uid" element={<AlertDetailPage />} />
          <Route path="operations" element={<OperationsPage />} />
          <Route path="rules" element={<RulesPage />} />
          <Route path="inbox" element={<InboxPage />} />
        </Route>
        <Route path="decision" element={<DecisionLayout />}>
          <Route index element={<DecisionOverviewPage />} />
          <Route path="new" element={<DecisionBuilderPage />} />
          <Route path="cases/:uid" element={<DecisionCasePage />} />
          <Route path="about" element={<DecisionAboutPage />} />
        </Route>
        <Route path="sourcing" element={<SourcingLayout />}>
          <Route index element={<RequirementPage />} />
          <Route path="runs" element={<SourcingRunHistoryPage />} />
          <Route path="runs/:uid" element={<SourcingRunPage />} />
          <Route path="offers" element={<SourcingOffersPage />} />
          <Route path="about" element={<SourcingAboutPage />} />
        </Route>
        <Route path="replenishment" element={<InvLayout />}>
          <Route index element={<InvOverviewPage />} />
          <Route path="plan" element={<InvPlanPage />} />
          <Route path="runs" element={<InvRunHistoryPage />} />
          <Route path="runs/:uid" element={<InvRunPage />} />
          <Route path="costs" element={<InvCostsPage />} />
          <Route path="about" element={<InvAboutPage />} />
        </Route>
        <Route path="scenarios" element={<ScenarioLayout />}>
          <Route index element={<ScenarioLibraryPage />} />
          <Route path="new" element={<ScenarioNewPage />} />
          <Route path="runs" element={<ScenarioRunsPage />} />
          <Route path="runs/:runId" element={<ScenarioRunPage />} />
          <Route path="compare" element={<ScenarioComparePage />} />
          <Route path=":id" element={<ScenarioEditorPage />} />
        </Route>
        <Route path="login" element={<LoginPage />} />
        <Route path="account" element={<AccountPage />} />
        <Route path="admin" element={<AdminPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
