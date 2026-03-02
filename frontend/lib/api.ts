import { Project, ProjectCreate, ProjectDetail, AnalysisProgress, AnalysisTrigger, NotificationSettings, SessionProvider, ProviderUpdate } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ── Token helpers ───────────────────────────────────────────────────────────

function getAuthToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage?.getItem("auth_token") || null;
}

function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage?.getItem("refresh_token") || null;
}

function setTokens(access: string, refresh?: string) {
  if (typeof window === "undefined") return;
  window.sessionStorage?.setItem("auth_token", access);
  if (refresh) window.sessionStorage?.setItem("refresh_token", refresh);
}

function clearTokens() {
  if (typeof window === "undefined") return;
  window.sessionStorage?.removeItem("auth_token");
  window.sessionStorage?.removeItem("refresh_token");
}

// ── Refresh lock (prevent concurrent refresh calls) ─────────────────────────

let refreshPromise: Promise<string | null> | null = null;

async function tryRefreshToken(): Promise<string | null> {
  // Deduplicate: if a refresh is already in flight, reuse it
  if (refreshPromise) return refreshPromise;

  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;

  refreshPromise = (async () => {
    try {
      const res = await fetch(`${API_URL}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });

      if (!res.ok) {
        clearTokens();
        return null;
      }

      const data = await res.json();
      setTokens(data.access_token, data.refresh_token);
      return data.access_token as string;
    } catch {
      return null;
    } finally {
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

// ── Core fetch with auto-refresh on 401 ─────────────────────────────────────

async function fetchApi<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const token = getAuthToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: {
      ...headers,
      ...(options?.headers as Record<string, string> || {}),
    },
  });

  // On 401 — attempt token refresh and retry once
  if (response.status === 401 && token) {
    const newToken = await tryRefreshToken();
    if (newToken) {
      const retryHeaders: Record<string, string> = {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${newToken}`,
      };

      const retryResponse = await fetch(`${API_URL}${path}`, {
        ...options,
        headers: {
          ...retryHeaders,
          ...(options?.headers as Record<string, string> || {}),
        },
      });

      if (!retryResponse.ok) {
        const error = await retryResponse.json().catch(() => ({ detail: "Request failed" }));
        throw new Error(error.detail || `HTTP ${retryResponse.status}`);
      }

      return retryResponse.json();
    }

    // Refresh failed — redirect to login
    if (typeof window !== "undefined") {
      clearTokens();
      window.location.href = "/login";
    }
    throw new Error("Session expired — please log in again");
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  return response.json();
}

// ── API functions ───────────────────────────────────────────────────────────

export async function listProjects(): Promise<Project[]> {
  return fetchApi<Project[]>("/projects");
}

export async function getProject(id: string): Promise<ProjectDetail> {
  return fetchApi<ProjectDetail>(`/projects/${id}`);
}

export async function listProviders(): Promise<SessionProvider[]> {
  return fetchApi<SessionProvider[]>("/projects/providers");
}

export async function createProject(data: ProjectCreate): Promise<Project> {
  return fetchApi<Project>("/projects", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateProjectProvider(projectId: string, data: ProviderUpdate): Promise<Project> {
  return fetchApi<Project>(`/projects/${projectId}/provider`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export async function triggerRun(projectId: string): Promise<{ message: string }> {
  return fetchApi<{ message: string }>(`/projects/${projectId}/run`, {
    method: "POST",
  });
}

export async function triggerAnalysis(projectId: string): Promise<AnalysisTrigger> {
  return fetchApi<AnalysisTrigger>(`/projects/${projectId}/analyze`, {
    method: "POST",
  });
}

export async function getAnalysisProgress(projectId: string, analysisId: string): Promise<AnalysisProgress> {
  return fetchApi<AnalysisProgress>(`/projects/${projectId}/analyze/${analysisId}`);
}

export async function getLatestAnalysis(projectId: string): Promise<AnalysisProgress> {
  return fetchApi<AnalysisProgress>(`/projects/${projectId}/analyze/latest`);
}

export interface IssueDetail {
  fingerprint: string;
  title: string;
  description: string;
  severity: string;
  category: string;
  event_type: string;
  is_ai_detected: boolean;
  page_url: string | null;
  count: number;
  affected_users: number;
  first_seen: string | null;
  last_seen: string | null;
  status: string;
  session_ids: string[];
  session_event_times: Record<string, string>;
  session_start_times: Record<string, string>;
  why_issue: string | null;
  reproduction_steps: string[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  evidence: any[];
  confidence: number | null;
  element: { tag: string; text: string; selector: string } | null;
  github_issue_id?: number | null;
  github_issue_url?: string | null;
}

export interface PaginatedIssues {
  items: import("./types").AnomalyCluster[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export async function listProjectIssues(
  projectId: string,
  opts: { status?: string; page?: number; pageSize?: number } = {}
): Promise<PaginatedIssues> {
  const params = new URLSearchParams();
  if (opts.status) params.set("status", opts.status);
  if (opts.page) params.set("page", String(opts.page));
  if (opts.pageSize) params.set("page_size", String(opts.pageSize));
  const qs = params.toString();
  return fetchApi<PaginatedIssues>(`/projects/${projectId}/issues${qs ? `?${qs}` : ""}`);
}

export async function getIssueByFingerprint(projectId: string, fingerprint: string): Promise<IssueDetail> {
  return fetchApi<IssueDetail>(`/projects/${projectId}/issues/${encodeURIComponent(fingerprint)}`);
}

export async function getNotificationSettings(projectId: string): Promise<NotificationSettings> {
  return fetchApi<NotificationSettings>(`/projects/${projectId}/notifications`);
}

export async function updateNotificationSettings(projectId: string, settings: NotificationSettings): Promise<NotificationSettings> {
  return fetchApi<NotificationSettings>(`/projects/${projectId}/notifications`, {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

export async function testNotification(projectId: string, channel?: string): Promise<{ message: string; results: Record<string, string> }> {
  const params = channel ? `?channel=${channel}` : "";
  return fetchApi<{ message: string; results: Record<string, string> }>(`/projects/${projectId}/notifications/test${params}`, {
    method: "POST",
  });
}

export async function updateIssueStatus(projectId: string, fingerprint: string, status: string): Promise<{ status: string; fingerprint: string }> {
  return fetchApi<{ status: string; fingerprint: string }>(`/projects/${projectId}/issues/${encodeURIComponent(fingerprint)}/status`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
}

export async function createGitHubIssue(
  projectId: string,
  fingerprint: string
): Promise<{ github_issue_id: number; github_issue_url: string; status: string }> {
  return fetchApi<{ github_issue_id: number; github_issue_url: string; status: string }>(
    `/projects/${projectId}/issues/${encodeURIComponent(fingerprint)}/github`,
    { method: "POST" }
  );
}

// ── Credential Validation ───────────────────────────────────────────────────

export async function validatePosthog(data: {
  api_key: string;
  project_id: string;
  host: string;
}): Promise<{ valid: boolean; error?: string }> {
  return fetchApi<{ valid: boolean; error?: string }>("/projects/validate/posthog", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function validateGithub(data: {
  repo: string;
  token: string;
}): Promise<{ valid: boolean; error?: string; repo_name?: string }> {
  return fetchApi<{ valid: boolean; error?: string; repo_name?: string }>("/projects/validate/github", {
    method: "POST",
    body: JSON.stringify(data),
  });
}
