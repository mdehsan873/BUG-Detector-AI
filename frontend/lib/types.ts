export interface Project {
  id: string;
  name: string;
  session_provider: string;
  provider_project_id: string;
  provider_host: string;
  github_repo: string;
  detection_threshold: number;
  min_sessions_threshold: number;
  skip_page_patterns: string[];
  created_at: string;
  updated_at: string;
}

export interface ProjectCreate {
  name: string;
  session_provider: string;
  provider_api_key: string;
  provider_project_id: string;
  provider_host: string;
  github_repo: string;
  github_token: string;
  detection_threshold: number;
  min_sessions_threshold: number;
  skip_page_patterns: string[];
}

export interface SessionProvider {
  id: string;
  name: string;
}

export interface AnomalyCluster {
  id: string;
  project_id: string;
  fingerprint: string;
  event_type: string;
  error_message: string | null;
  endpoint: string | null;
  css_selector: string | null;
  page_url: string | null;
  count: number;
  affected_users: number;
  first_seen: string;
  last_seen: string;
  sample_session_ids: string[];
  status: string;
  created_at: string;
  updated_at: string;
}

export interface JobRun {
  id: string;
  project_id: string;
  last_fetched_at: string;
  status: string;
  events_fetched: number;
  anomalies_detected: number;
  issues_created: number;
  created_at: string;
}

export interface ProjectDetail extends Project {
  recent_anomalies: AnomalyCluster[];
  last_job_run: JobRun | null;
}

export interface SessionIssue {
  title: string;
  description: string;
  severity: string;
  category: string;
  evidence?: string[];
  page_url?: string;
  confidence: number;
  session_id?: string;
  fingerprint?: string;
}

export interface AnalysisProgress {
  project_id: string;
  status: string; // none | running | completed | failed
  sessions_total: number;
  sessions_analyzed: number;
  issues_found: number;
  issues: SessionIssue[];
  started_at?: string;
  completed_at?: string;
}

export interface AnalysisTrigger {
  message: string;
  analysis_id: string;
}

export interface ProviderUpdate {
  session_provider: string;
  provider_api_key: string;
  provider_project_id: string;
  provider_host: string;
}

export interface NotificationSettings {
  email_enabled: boolean;
  email_address: string | null;
  slack_enabled: boolean;
  slack_webhook_url: string | null;
}
