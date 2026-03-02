"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { getProject, getIssueByFingerprint, updateIssueStatus, createGitHubIssue, IssueDetail } from "@/lib/api";
import { ProjectDetail } from "@/lib/types";

const severityColors: Record<string, string> = {
  critical: "bg-red-100 text-red-800 border border-red-200",
  high: "bg-orange-50 text-orange-700 border border-orange-100",
  medium: "bg-amber-50 text-amber-700 border border-amber-100",
  low: "bg-slate-50 text-slate-600 border border-slate-100",
};

const statusColors: Record<string, string> = {
  new: "bg-amber-50 text-amber-700 border border-amber-100",
  in_progress: "bg-blue-50 text-blue-700 border border-blue-100",
  github_issued: "bg-sky-50 text-sky-700 border border-sky-100",
  resolved: "bg-emerald-50 text-emerald-700 border border-emerald-100",
  closed: "bg-slate-100 text-slate-600 border border-slate-200",
  not_an_issue: "bg-slate-50 text-slate-500 border border-slate-200",
};

function getReplayUrl(
  provider: string,
  host: string,
  projectId: string,
  sessionId: string,
  eventTimestamp?: string,
  sessionStart?: string
): string {
  let base: string;
  switch (provider) {
    case "fullstory":
      base = `https://app.fullstory.com/ui/session/${sessionId}`;
      break;
    case "logrocket":
      base = `https://app.logrocket.com/${projectId}/sessions/${sessionId}`;
      break;
    case "clarity":
      base = `https://clarity.microsoft.com/projects/${projectId}/session/${sessionId}`;
      break;
    default: // posthog
      base = `https://${host || "eu.posthog.com"}/project/${projectId}/replay/${sessionId}`;
      break;
  }
  if (eventTimestamp && sessionStart) {
    try {
      const eventMs = new Date(eventTimestamp).getTime();
      const startMs = new Date(sessionStart).getTime();
      const offsetMs = Math.max(0, eventMs - startMs);
      return `${base}${base.includes("?") ? "&" : "?"}t=${offsetMs}`;
    } catch {
      return base;
    }
  }
  return base;
}

function formatRelativeTime(eventTimestamp: string, sessionStart: string): string {
  try {
    const eventMs = new Date(eventTimestamp).getTime();
    const startMs = new Date(sessionStart).getTime();
    const offsetSec = Math.max(0, Math.floor((eventMs - startMs) / 1000));
    const mins = Math.floor(offsetSec / 60);
    const secs = offsetSec % 60;
    return `${mins}:${secs.toString().padStart(2, "0")}`;
  } catch {
    return "";
  }
}

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function formatEventTime(dateStr: string): string {
  try {
    const d = new Date(dateStr);
    return d.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return dateStr;
  }
}

function StatusDropdown({
  currentStatus,
  onStatusChange,
  disabled,
}: {
  currentStatus: string;
  onStatusChange: (status: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const statuses = [
    { key: "new", label: "New" },
    { key: "in_progress", label: "In Progress" },
    { key: "resolved", label: "Resolved" },
    { key: "closed", label: "Closed" },
    { key: "not_an_issue", label: "Not an Issue" },
  ];

  return (
    <div className="relative">
      <button
        onClick={() => !disabled && setOpen(!open)}
        disabled={disabled}
        className={`text-[11px] font-semibold px-2.5 py-1 rounded-md flex items-center gap-1 transition ${
          statusColors[currentStatus] || "bg-slate-50 text-slate-600 border border-slate-100"
        } ${disabled ? "opacity-50" : "hover:opacity-80 cursor-pointer"}`}
      >
        {currentStatus.replace(/_/g, " ")}
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-full mt-1 bg-white border border-slate-200 rounded-xl shadow-xl py-1 z-50 min-w-[140px]">
            {statuses
              .filter((s) => s.key !== currentStatus)
              .map((s) => (
                <button
                  key={s.key}
                  onClick={() => {
                    onStatusChange(s.key);
                    setOpen(false);
                  }}
                  className="w-full text-left px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 transition"
                >
                  <span className={`inline-block w-2 h-2 rounded-full mr-2 ${
                    s.key === "new" ? "bg-amber-500" :
                    s.key === "in_progress" ? "bg-blue-500" :
                    s.key === "resolved" ? "bg-emerald-500" :
                    s.key === "closed" ? "bg-slate-400" : "bg-slate-300"
                  }`} />
                  {s.label}
                </button>
              ))}
          </div>
        </>
      )}
    </div>
  );
}

export default function IssueDetailPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.id as string;
  const fingerprint = decodeURIComponent(params.fingerprint as string);

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [issue, setIssue] = useState<IssueDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatingStatus, setUpdatingStatus] = useState(false);
  const [creatingGithub, setCreatingGithub] = useState(false);
  const [githubError, setGithubError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId || !fingerprint) return;

    Promise.all([
      getProject(projectId),
      getIssueByFingerprint(projectId, fingerprint),
    ])
      .then(([proj, iss]) => {
        setProject(proj);
        setIssue(iss);
      })
      .catch((e) => {
        setError(e.message || "Failed to load issue");
      })
      .finally(() => setLoading(false));
  }, [projectId, fingerprint]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <svg className="animate-spin h-6 w-6 text-indigo-400" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      </div>
    );
  }

  if (error || !issue || !project) {
    return (
      <div className="text-center py-20">
        <div className="w-16 h-16 bg-slate-100 rounded-2xl flex items-center justify-center mx-auto mb-4">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M16 16s-1.5-2-4-2-4 2-4 2" />
            <line x1="9" y1="9" x2="9.01" y2="9" />
            <line x1="15" y1="9" x2="15.01" y2="9" />
          </svg>
        </div>
        <p className="text-slate-500 text-sm mb-1">{error || "Issue not found"}</p>
        <p className="text-xs text-slate-400 mb-4">This issue may have been resolved or the data was cleared.</p>
        <button
          onClick={() => router.back()}
          className="text-indigo-600 text-sm font-semibold hover:text-indigo-700 flex items-center gap-1.5 mx-auto"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M19 12H5" />
            <path d="M12 19l-7-7 7-7" />
          </svg>
          Back to project
        </button>
      </div>
    );
  }

  const handleStatusChange = async (newStatus: string) => {
    if (!issue) return;
    setUpdatingStatus(true);
    try {
      await updateIssueStatus(projectId, fingerprint, newStatus);
      setIssue({ ...issue, status: newStatus });
    } catch (e) {
      console.error("Failed to update status", e);
    } finally {
      setUpdatingStatus(false);
    }
  };

  const handleCreateGithubIssue = async () => {
    if (!issue) return;
    setCreatingGithub(true);
    setGithubError(null);
    try {
      const result = await createGitHubIssue(projectId, fingerprint);
      setIssue({
        ...issue,
        status: result.status,
        github_issue_id: result.github_issue_id,
        github_issue_url: result.github_issue_url,
      });
    } catch (e: any) {
      setGithubError(e.message || "Failed to create GitHub issue");
    } finally {
      setCreatingGithub(false);
    }
  };

  const sessionProvider = (project as any).session_provider || "posthog";
  const providerHost = (project as any).provider_host || "";
  const providerProjectId = (project as any).provider_project_id || "";
  const sessionIds = issue.session_ids || [];
  const sessionEventTimes = issue.session_event_times || {};
  const sessionStartTimes = issue.session_start_times || {};
  const primarySessionId = sessionIds[0] || null;
  const primaryEventTime = primarySessionId ? sessionEventTimes[primarySessionId] : undefined;
  const primaryStartTime = primarySessionId ? sessionStartTimes[primarySessionId] : undefined;
  const primaryReplayUrl = primarySessionId
    ? getReplayUrl(sessionProvider, providerHost, providerProjectId, primarySessionId, primaryEventTime, primaryStartTime)
    : null;

  return (
    <div>
      {/* Back button */}
      <button
        onClick={() => router.back()}
        className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-indigo-600 font-medium mb-6 transition"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M19 12H5" />
          <path d="M12 19l-7-7 7-7" />
        </svg>
        Back to project
      </button>

      {/* Issue Header */}
      <div className="bg-white border border-slate-200/80 rounded-2xl p-6 mb-6 shadow-sm">
        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-[11px] font-semibold px-2.5 py-0.5 rounded-md ${severityColors[issue.severity || "medium"] || "bg-slate-50 text-slate-600 border border-slate-100"}`}>
              {(issue.severity || "medium").toUpperCase()}
            </span>
            {issue.is_ai_detected && (
              <span className="text-[11px] font-semibold px-2.5 py-0.5 rounded-md bg-violet-50 text-violet-700 border border-violet-100">
                AI Detected
              </span>
            )}
            <span className="text-[11px] font-medium px-2.5 py-0.5 rounded-md bg-slate-50 text-slate-600 border border-slate-100">
              {(issue.category || "unknown").replace(/_/g, " ")}
            </span>
            <span className={`text-[11px] font-semibold px-2.5 py-0.5 rounded-md ${statusColors[issue.status || "new"] || "bg-slate-50 text-slate-600 border border-slate-100"}`}>
              {issue.status || "new"}
            </span>
          </div>
        </div>

        <h1 className="text-xl font-bold text-slate-900 mb-2">{issue.title}</h1>
        <p className="text-sm text-slate-600 leading-relaxed">{issue.description}</p>

        {/* Status Dropdown + GitHub Button */}
        <div className="flex items-center gap-3 mt-4 flex-wrap">
          <span className="text-xs text-slate-500 font-medium mr-1">Status:</span>
          <StatusDropdown
            currentStatus={issue.status || "new"}
            onStatusChange={handleStatusChange}
            disabled={updatingStatus}
          />

          {/* GitHub Issue Button / Link */}
          {(project as any).github_repo && (
            <>
              {issue.github_issue_url ? (
                <a
                  href={issue.github_issue_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-md bg-slate-900 text-white hover:bg-slate-800 transition"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
                  </svg>
                  View on GitHub
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                    <path d="M15 3h6v6" />
                    <path d="M10 14L21 3" />
                  </svg>
                </a>
              ) : (
                <button
                  onClick={handleCreateGithubIssue}
                  disabled={creatingGithub}
                  className="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-1 rounded-md bg-slate-900 text-white hover:bg-slate-800 transition disabled:opacity-50"
                >
                  {creatingGithub ? (
                    <>
                      <svg className="animate-spin h-3.5 w-3.5" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                      Creating...
                    </>
                  ) : (
                    <>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                        <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/>
                      </svg>
                      Create GitHub Issue
                    </>
                  )}
                </button>
              )}
              {githubError && (
                <span className="text-[11px] text-red-600 font-medium">{githubError}</span>
              )}
            </>
          )}
        </div>

        {/* Element info */}
        {issue.element && (issue.element.tag || issue.element.text || issue.element.selector) && (
          <div className="mt-4 flex items-center gap-2 bg-amber-50/60 border border-amber-100 rounded-lg px-3 py-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <path d="M9 9h6v6H9z" />
            </svg>
            <span className="text-xs font-medium text-amber-800">
              Element:{" "}
              {issue.element.tag && <code className="bg-amber-100 px-1 py-0.5 rounded text-amber-900">&lt;{issue.element.tag}&gt;</code>}
              {issue.element.text && <span className="ml-1.5 text-amber-700">&quot;{issue.element.text}&quot;</span>}
              {issue.element.selector && <span className="ml-1.5 font-mono text-amber-600 text-[11px]">{issue.element.selector}</span>}
            </span>
          </div>
        )}

        {issue.page_url && (
          <div className="mt-3 flex items-center gap-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
              <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
            </svg>
            <span className="text-xs text-slate-400 font-mono">{issue.page_url}</span>
          </div>
        )}

        {/* Stats */}
        <div className="flex gap-6 mt-5 pt-5 border-t border-slate-100">
          <div>
            <p className="text-2xl font-bold text-slate-900">{issue.count}</p>
            <p className="text-[11px] text-slate-500 font-medium">Occurrences</p>
          </div>
          <div>
            <p className="text-2xl font-bold text-slate-900">{issue.affected_users}</p>
            <p className="text-[11px] text-slate-500 font-medium">Affected Users</p>
          </div>
          <div>
            <p className="text-2xl font-bold text-slate-900">{sessionIds.length}</p>
            <p className="text-[11px] text-slate-500 font-medium">Sessions</p>
          </div>
          {issue.first_seen && (
            <div className="ml-auto text-right">
              <p className="text-sm font-semibold text-slate-700">{timeAgo(issue.first_seen)}</p>
              <p className="text-[11px] text-slate-500 font-medium">First seen</p>
            </div>
          )}
        </div>
      </div>

      {/* Why This Is an Issue */}
      {issue.why_issue && (
        <div className="bg-white border border-slate-200/80 rounded-2xl p-6 mb-6 shadow-sm">
          <h2 className="text-[15px] font-semibold text-slate-900 mb-3 flex items-center gap-2">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#e11d48" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 8v4" />
              <path d="M12 16h.01" />
            </svg>
            Why This Is an Issue
          </h2>
          <p className="text-sm text-slate-700 leading-relaxed bg-rose-50/50 border border-rose-100 rounded-xl p-4">
            {issue.why_issue}
          </p>
        </div>
      )}

      {/* Steps to Reproduce */}
      {issue.reproduction_steps && issue.reproduction_steps.length > 0 && (
        <div className="bg-white border border-slate-200/80 rounded-2xl p-6 mb-6 shadow-sm">
          <h2 className="text-[15px] font-semibold text-slate-900 mb-3 flex items-center gap-2">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <path d="M14 2v6h6" />
              <path d="M16 13H8" />
              <path d="M16 17H8" />
              <path d="M10 9H8" />
            </svg>
            Steps to Reproduce
          </h2>
          <ol className="space-y-2">
            {issue.reproduction_steps.map((step, idx) => {
              const text = typeof step === "string" ? step : JSON.stringify(step);
              return (
                <li key={idx} className="flex items-start gap-3">
                  <span className="flex-shrink-0 w-6 h-6 rounded-full bg-indigo-100 text-indigo-700 text-xs font-bold flex items-center justify-center mt-0.5">
                    {idx + 1}
                  </span>
                  <span className="text-sm text-slate-700 leading-relaxed">{text}</span>
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {/* Evidence */}
      {issue.evidence && issue.evidence.length > 0 && (
        <div className="bg-white border border-slate-200/80 rounded-2xl p-6 mb-6 shadow-sm">
          <h2 className="text-[15px] font-semibold text-slate-900 mb-3 flex items-center gap-2">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
              <circle cx="12" cy="12" r="3" />
            </svg>
            Evidence
          </h2>
          <div className="space-y-2">
            {issue.evidence.map((item, idx) => {
              // Handle both string evidence and object evidence ({timestamp, event} etc.)
              let timestamp = "";
              let eventText = "";
              if (typeof item === "string") {
                // Try to extract timestamp from strings like "[2026-02-26T01:33:08] ERROR: ..."
                const tsMatch = item.match(/^\[?([\d-]+T[\d:.+Z-]+)\]?\s*(.*)/);
                if (tsMatch) {
                  timestamp = tsMatch[1];
                  eventText = tsMatch[2] || item;
                } else {
                  eventText = item;
                }
              } else if (typeof item === "object" && item !== null) {
                timestamp = item.timestamp || "";
                eventText = item.event || item.description || Object.entries(item).filter(([k]) => k !== "timestamp").map(([k, v]) => `${k}: ${v}`).join(" — ");
              } else {
                eventText = String(item);
              }

              // Format timestamp as relative if we have session start
              const sid = sessionIds[0];
              const startTime = sid ? sessionStartTimes[sid] : "";
              let timeLabel = "";
              if (timestamp && startTime) {
                const rel = formatRelativeTime(timestamp, startTime);
                if (rel) timeLabel = rel;
              } else if (timestamp) {
                timeLabel = formatEventTime(timestamp);
              }

              return (
                <div key={idx} className="flex items-start gap-2 text-xs font-mono bg-slate-50 border border-slate-200/80 rounded-lg px-3 py-2">
                  {timeLabel && (
                    <span className="shrink-0 text-amber-600 font-semibold min-w-[3.5rem]">
                      {timeLabel}
                    </span>
                  )}
                  <span className="text-slate-600 break-all">{eventText}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Confidence Score */}
      {issue.confidence !== null && issue.confidence !== undefined && (
        <div className="flex items-center gap-2 mb-6 text-xs text-slate-500">
          <span className="font-medium">AI Confidence:</span>
          <div className="w-24 h-1.5 bg-slate-200 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full ${
                issue.confidence >= 0.8 ? "bg-emerald-500" : issue.confidence >= 0.6 ? "bg-amber-500" : "bg-red-400"
              }`}
              style={{ width: `${(issue.confidence * 100).toFixed(0)}%` }}
            />
          </div>
          <span className="font-semibold">{(issue.confidence * 100).toFixed(0)}%</span>
        </div>
      )}

      {/* Session Replay Section */}
      <div className="bg-white border border-slate-200/80 rounded-2xl p-6 mb-6 shadow-sm">
        <h2 className="text-[15px] font-semibold text-slate-900 mb-4 flex items-center gap-2">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="5 3 19 12 5 21 5 3" />
          </svg>
          Related Sessions
          <span className="text-slate-400 font-normal text-sm">({sessionIds.length})</span>
        </h2>

        {sessionIds.length === 0 ? (
          <div className="text-center py-10 bg-slate-50/50 rounded-xl border border-dashed border-slate-200">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="mx-auto mb-2">
              <rect x="2" y="3" width="20" height="14" rx="2" />
              <path d="M8 21h8" />
              <path d="M12 17v4" />
            </svg>
            <p className="text-sm text-slate-400">No session recordings linked to this issue.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {sessionIds.map((sid, idx) => {
              const eventTime = sessionEventTimes[sid];
              const startTime = sessionStartTimes[sid];
              const url = getReplayUrl(sessionProvider, providerHost, providerProjectId, sid, eventTime, startTime);
              const relTime = eventTime && startTime ? formatRelativeTime(eventTime, startTime) : "";
              return (
                <a
                  key={sid}
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-4 p-4 bg-slate-50 border border-slate-200/80 rounded-xl hover:border-indigo-200 hover:bg-indigo-50/30 transition-all group"
                >
                  {/* Play button thumbnail */}
                  <div className="w-24 h-16 bg-slate-900 rounded-lg flex items-center justify-center shrink-0 relative overflow-hidden group-hover:bg-indigo-900 transition-colors">
                    <div className="absolute inset-0 bg-gradient-to-br from-slate-700/50 to-transparent" />
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="white" className="relative z-10 opacity-80 group-hover:opacity-100 transition">
                      <polygon points="8 5 20 12 8 19 8 5" />
                    </svg>
                    {relTime ? (
                      <div className="absolute bottom-0.5 left-0.5 right-0.5 text-[9px] text-amber-300 font-mono text-center bg-black/60 rounded px-0.5">
                        {relTime}
                      </div>
                    ) : (
                      <div className="absolute bottom-1 right-1.5 text-[9px] text-white/60 font-mono">REC</div>
                    )}
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-sm font-semibold text-slate-800 group-hover:text-indigo-700 transition-colors">
                        Session {idx + 1}
                      </span>
                      {idx === 0 && (
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700">
                          Primary
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-slate-400 font-mono truncate">{sid}</p>
                    {eventTime && (
                      <div className="flex items-center gap-1.5 mt-1">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <circle cx="12" cy="12" r="10" />
                          <path d="M12 6v6l4 2" />
                        </svg>
                        <span className="text-[11px] text-amber-600 font-semibold">
                          {relTime
                            ? `Issue at ${relTime} into session`
                            : `Issue at ${formatEventTime(eventTime)}`
                          }
                        </span>
                        {eventTime && (
                          <span className="text-[10px] text-slate-400 ml-1">
                            ({formatEventTime(eventTime)})
                          </span>
                        )}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-1.5 text-xs text-indigo-600 font-semibold shrink-0 group-hover:text-indigo-700">
                    {eventTime ? "Jump to issue" : "Watch in PostHog"}
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                      <path d="M15 3h6v6" />
                      <path d="M10 14L21 3" />
                    </svg>
                  </div>
                </a>
              );
            })}
          </div>
        )}

        {/* Embedded replay for primary session */}
        {primaryReplayUrl && (
          <div className="mt-6">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-[13px] font-semibold text-slate-700">Session Preview</h3>
              <a
                href={primaryReplayUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-indigo-600 font-semibold hover:text-indigo-700 flex items-center gap-1"
              >
                Open in PostHog
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                  <path d="M15 3h6v6" />
                  <path d="M10 14L21 3" />
                </svg>
              </a>
            </div>
            <div className="w-full bg-slate-900 rounded-xl overflow-hidden border border-slate-200" style={{ aspectRatio: "16/9" }}>
              <iframe
                src={primaryReplayUrl}
                className="w-full h-full"
                title="PostHog Session Replay"
                allow="clipboard-write"
                sandbox="allow-same-origin allow-scripts allow-popups allow-forms"
              />
            </div>
            <p className="text-[11px] text-slate-400 mt-2">
              Session replay requires you to be logged into PostHog. If the preview is blank, click &quot;Open in PostHog&quot; above.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
