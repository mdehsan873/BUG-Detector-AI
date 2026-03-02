"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";
import { listProjects, listProjectIssues, getProject, getIssueByFingerprint, updateIssueStatus, IssueDetail, PaginatedIssues } from "@/lib/api";
import { AnomalyCluster, Project, ProjectDetail } from "@/lib/types";

// Base type labels (without ai_ prefix)
const baseTypeLabels: Record<string, string> = {
  rage_click: "Rage Click", dead_click: "Dead Click", navigation_loop: "Navigation Loop",
  rapid_back_nav: "Instant Bounce", stuck_interaction: "Stuck Interaction", form_abandonment: "Form Abandoned",
  button_spam: "Button Spam", broken_flow: "Flow Drop-off", scroll_frustration: "Scroll Frustration",
  rapid_refresh: "Rapid Refresh", unexpected_exit: "Exit Spike",
  exception: "Exception", console_error: "Console Error", api_failure: "API Failure",
  dead_end: "Dead End", confusing_flow: "Confusing Flow", broken_ui: "Broken UI",
  error: "Error", ux_friction: "UX Friction", performance: "Performance",
  data_loss: "Data Loss", form_validation: "Form Validation", refresh_workaround: "Refresh Workaround",
  session_expiry: "Session Expiry", broken_navigation: "Broken Navigation", double_action: "Double Action",
};

const baseTypeColors: Record<string, string> = {
  rage_click: "bg-red-50 text-red-700 border border-red-100",
  dead_click: "bg-violet-50 text-violet-700 border border-violet-100",
  navigation_loop: "bg-indigo-50 text-indigo-700 border border-indigo-100",
  rapid_back_nav: "bg-amber-50 text-amber-700 border border-amber-100",
  stuck_interaction: "bg-yellow-50 text-yellow-800 border border-yellow-200",
  form_abandonment: "bg-orange-50 text-orange-700 border border-orange-100",
  button_spam: "bg-rose-50 text-rose-700 border border-rose-100",
  broken_flow: "bg-red-50 text-red-800 border border-red-200",
  scroll_frustration: "bg-cyan-50 text-cyan-700 border border-cyan-100",
  rapid_refresh: "bg-pink-50 text-pink-700 border border-pink-100",
  unexpected_exit: "bg-purple-50 text-purple-700 border border-purple-100",
  exception: "bg-red-50 text-red-700 border border-red-100",
  console_error: "bg-rose-50 text-rose-700 border border-rose-100",
  api_failure: "bg-amber-50 text-amber-700 border border-amber-100",
  dead_end: "bg-pink-50 text-pink-700 border border-pink-100",
  confusing_flow: "bg-indigo-50 text-indigo-700 border border-indigo-100",
  broken_ui: "bg-orange-50 text-orange-700 border border-orange-100",
  error: "bg-red-50 text-red-700 border border-red-100",
  ux_friction: "bg-amber-50 text-amber-700 border border-amber-100",
  performance: "bg-sky-50 text-sky-700 border border-sky-100",
  data_loss: "bg-rose-50 text-rose-700 border border-rose-100",
  form_validation: "bg-orange-50 text-orange-700 border border-orange-100",
  refresh_workaround: "bg-amber-50 text-amber-800 border border-amber-200",
  session_expiry: "bg-red-50 text-red-800 border border-red-200",
  broken_navigation: "bg-purple-50 text-purple-700 border border-purple-100",
  double_action: "bg-rose-50 text-rose-700 border border-rose-100",
};

function getBaseType(eventType: string): string {
  return eventType.startsWith("ai_") ? eventType.slice(3) : eventType;
}
function getTypeLabel(eventType: string): string {
  const base = getBaseType(eventType);
  return baseTypeLabels[base] || base.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
function getTypeColor(eventType: string): string {
  const base = getBaseType(eventType);
  return baseTypeColors[base] || "bg-slate-50 text-slate-600 border border-slate-100";
}
function isAiDetected(eventType: string): boolean {
  return eventType.startsWith("ai_");
}

const statusColors: Record<string, string> = {
  new: "bg-amber-50 text-amber-700 border border-amber-100",
  in_progress: "bg-blue-50 text-blue-700 border border-blue-100",
  github_issued: "bg-sky-50 text-sky-700 border border-sky-100",
  resolved: "bg-emerald-50 text-emerald-700 border border-emerald-100",
  closed: "bg-slate-100 text-slate-600 border border-slate-200",
  not_an_issue: "bg-slate-50 text-slate-500 border border-slate-200",
};

const severityColors: Record<string, string> = {
  critical: "bg-red-100 text-red-800 border border-red-200",
  high: "bg-orange-50 text-orange-700 border border-orange-100",
  medium: "bg-amber-50 text-amber-700 border border-amber-100",
  low: "bg-slate-50 text-slate-600 border border-slate-100",
};

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function getReplayUrl(
  provider: string,
  host: string,
  projectId: string,
  sessionId: string,
  eventTimestamp?: string,
  sessionStart?: string,
): string {
  let base: string;
  switch (provider) {
    case "fullstory":
      base = `https://app.fullstory.com/ui/${projectId}/session/${sessionId}`;
      break;
    case "logrocket":
      base = `https://app.logrocket.com/${projectId}/sessions/${sessionId}`;
      break;
    case "clarity":
      base = `https://clarity.microsoft.com/projects/${projectId}/session/${sessionId}`;
      break;
    default:
      base = `https://${host || "eu.posthog.com"}/project/${projectId}/replay/${sessionId}`;
      break;
  }
  if (eventTimestamp && sessionStart) {
    try {
      const offsetMs = Math.max(0, new Date(eventTimestamp).getTime() - new Date(sessionStart).getTime());
      return `${base}${base.includes("?") ? "&" : "?"}t=${offsetMs}`;
    } catch { return base; }
  }
  return base;
}

function formatEventTime(dateStr: string): string {
  try {
    return new Date(dateStr).toLocaleString("en-US", {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    });
  } catch { return dateStr; }
}

function formatRelativeTime(eventTimestamp: string, sessionStart: string): string {
  try {
    const offset = Math.max(0, Math.floor((new Date(eventTimestamp).getTime() - new Date(sessionStart).getTime()) / 1000));
    return `${Math.floor(offset / 60)}:${(offset % 60).toString().padStart(2, "0")}`;
  } catch { return ""; }
}

/* ── Status Dropdown ────────────────────────────────────────────────────── */

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
    <div className="relative" onClick={(e) => e.stopPropagation()}>
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
          <div className="absolute right-0 top-full mt-1 bg-white border border-slate-200 rounded-xl shadow-xl py-1 z-50 min-w-[140px]">
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

/* ── Issue Detail Slide-over Panel ──────────────────────────────────────── */

function IssueSlideOver({
  projectId,
  fingerprint,
  onClose,
  onStatusChanged,
}: {
  projectId: string;
  fingerprint: string;
  onClose: () => void;
  onStatusChanged: (fingerprint: string, newStatus: string) => void;
}) {
  const [issue, setIssue] = useState<IssueDetail | null>(null);
  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatingStatus, setUpdatingStatus] = useState(false);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      getIssueByFingerprint(projectId, fingerprint),
      getProject(projectId),
    ])
      .then(([iss, proj]) => {
        setIssue(iss);
        setProject(proj);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [projectId, fingerprint]);

  const handleStatusChange = async (newStatus: string) => {
    if (!issue) return;
    setUpdatingStatus(true);
    try {
      await updateIssueStatus(projectId, fingerprint, newStatus);
      setIssue({ ...issue, status: newStatus });
      onStatusChanged(fingerprint, newStatus);
    } catch {}
    finally { setUpdatingStatus(false); }
  };

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/20 z-40 backdrop-blur-sm" onClick={onClose} />
      {/* Panel */}
      <div className="fixed right-0 top-0 bottom-0 w-full max-w-2xl bg-white z-50 shadow-2xl overflow-y-auto border-l border-slate-200">
        {/* Header */}
        <div className="sticky top-0 bg-white border-b border-slate-100 px-6 py-4 flex items-center justify-between z-10">
          <h2 className="text-[15px] font-semibold text-slate-900">Issue Detail</h2>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-lg hover:bg-slate-100 flex items-center justify-center transition"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6L6 18" />
              <path d="M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="p-6">
          {loading ? (
            <div className="flex items-center justify-center py-20">
              <svg className="animate-spin h-6 w-6 text-indigo-400" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            </div>
          ) : !issue ? (
            <div className="text-center py-20">
              <p className="text-slate-500 text-sm">Issue not found</p>
            </div>
          ) : (
            <div className="space-y-6">
              {/* Badges + Status dropdown */}
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
                <div className="ml-auto">
                  <StatusDropdown
                    currentStatus={issue.status || "new"}
                    onStatusChange={handleStatusChange}
                    disabled={updatingStatus}
                  />
                </div>
              </div>

              {/* Title */}
              <div>
                <h1 className="text-lg font-bold text-slate-900 mb-2">{issue.title}</h1>
                <p className="text-sm text-slate-600 leading-relaxed">{issue.description}</p>
              </div>

              {/* Element info */}
              {issue.element && (issue.element.tag || issue.element.text || issue.element.selector) && (
                <div className="flex items-center gap-2 bg-amber-50/60 border border-amber-100 rounded-lg px-3 py-2">
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
                <div className="flex items-center gap-2">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                    <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
                  </svg>
                  <span className="text-xs text-slate-400 font-mono">{issue.page_url}</span>
                </div>
              )}

              {/* Stats */}
              <div className="flex gap-6 pt-4 border-t border-slate-100">
                <div>
                  <p className="text-2xl font-bold text-slate-900">{issue.count}</p>
                  <p className="text-[11px] text-slate-500 font-medium">Occurrences</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-slate-900">{issue.affected_users}</p>
                  <p className="text-[11px] text-slate-500 font-medium">Affected Users</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-slate-900">{(issue.session_ids || []).length}</p>
                  <p className="text-[11px] text-slate-500 font-medium">Sessions</p>
                </div>
                {issue.first_seen && (
                  <div className="ml-auto text-right">
                    <p className="text-sm font-semibold text-slate-700">{timeAgo(issue.first_seen)}</p>
                    <p className="text-[11px] text-slate-500 font-medium">First seen</p>
                  </div>
                )}
              </div>

              {/* Why This Is an Issue */}
              {issue.why_issue && (
                <div>
                  <h3 className="text-[14px] font-semibold text-slate-900 mb-2 flex items-center gap-2">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#e11d48" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <circle cx="12" cy="12" r="10" />
                      <path d="M12 8v4" /><path d="M12 16h.01" />
                    </svg>
                    Why This Is an Issue
                  </h3>
                  <p className="text-sm text-slate-700 leading-relaxed bg-rose-50/50 border border-rose-100 rounded-xl p-4">
                    {issue.why_issue}
                  </p>
                </div>
              )}

              {/* Steps to Reproduce */}
              {issue.reproduction_steps && issue.reproduction_steps.length > 0 && (
                <div>
                  <h3 className="text-[14px] font-semibold text-slate-900 mb-2 flex items-center gap-2">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                      <path d="M14 2v6h6" /><path d="M16 13H8" /><path d="M16 17H8" /><path d="M10 9H8" />
                    </svg>
                    Steps to Reproduce
                  </h3>
                  <ol className="space-y-2">
                    {issue.reproduction_steps.map((step, idx) => (
                      <li key={idx} className="flex items-start gap-3">
                        <span className="flex-shrink-0 w-5 h-5 rounded-full bg-indigo-100 text-indigo-700 text-[10px] font-bold flex items-center justify-center mt-0.5">
                          {idx + 1}
                        </span>
                        <span className="text-sm text-slate-700 leading-relaxed">{typeof step === "string" ? step : JSON.stringify(step)}</span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}

              {/* Evidence */}
              {issue.evidence && issue.evidence.length > 0 && (
                <div>
                  <h3 className="text-[14px] font-semibold text-slate-900 mb-2 flex items-center gap-2">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                      <circle cx="12" cy="12" r="3" />
                    </svg>
                    Evidence
                  </h3>
                  <div className="space-y-1.5">
                    {issue.evidence.map((item: any, idx: number) => {
                      let timestamp = "";
                      let eventText = "";
                      if (typeof item === "string") {
                        const tsMatch = item.match(/^\[?([\d-]+T[\d:.+Z-]+)\]?\s*(.*)/);
                        if (tsMatch) { timestamp = tsMatch[1]; eventText = tsMatch[2] || item; }
                        else { eventText = item; }
                      } else if (typeof item === "object" && item !== null) {
                        timestamp = item.timestamp || "";
                        eventText = item.event || item.description || Object.entries(item).filter(([k]) => k !== "timestamp").map(([k, v]) => `${k}: ${v}`).join(" — ");
                      } else { eventText = String(item); }

                      const sessionIds = issue.session_ids || [];
                      const sid = sessionIds[0];
                      const startTime = sid ? (issue.session_start_times || {})[sid] : "";
                      let timeLabel = "";
                      if (timestamp && startTime) {
                        const rel = formatRelativeTime(timestamp, startTime);
                        if (rel) timeLabel = rel;
                      } else if (timestamp) { timeLabel = formatEventTime(timestamp); }

                      return (
                        <div key={idx} className="flex items-start gap-2 text-xs font-mono bg-slate-50 border border-slate-200/80 rounded-lg px-3 py-2">
                          {timeLabel && <span className="shrink-0 text-amber-600 font-semibold min-w-[3.5rem]">{timeLabel}</span>}
                          <span className="text-slate-600 break-all">{eventText}</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              {/* Confidence */}
              {issue.confidence !== null && issue.confidence !== undefined && (
                <div className="flex items-center gap-2 text-xs text-slate-500">
                  <span className="font-medium">AI Confidence:</span>
                  <div className="w-24 h-1.5 bg-slate-200 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full ${issue.confidence >= 0.8 ? "bg-emerald-500" : issue.confidence >= 0.6 ? "bg-amber-500" : "bg-red-400"}`}
                      style={{ width: `${(issue.confidence * 100).toFixed(0)}%` }}
                    />
                  </div>
                  <span className="font-semibold">{(issue.confidence * 100).toFixed(0)}%</span>
                </div>
              )}

              {/* Session replays */}
              {(issue.session_ids || []).length > 0 && (() => {
                const sessionProvider = (project as any)?.session_provider || "posthog";
                const providerHost = (project as any)?.provider_host || "";
                const providerProjectId = (project as any)?.provider_project_id || "";
                const sessionIds = issue.session_ids || [];
                const sessionEventTimes = issue.session_event_times || {};
                const sessionStartTimes = issue.session_start_times || {};
                return (
                  <div>
                    <h3 className="text-[14px] font-semibold text-slate-900 mb-3 flex items-center gap-2">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polygon points="5 3 19 12 5 21 5 3" />
                      </svg>
                      Sessions ({sessionIds.length})
                    </h3>
                    <div className="space-y-2">
                      {sessionIds.map((sid: string, idx: number) => {
                        const eventTime = sessionEventTimes[sid];
                        const startTime = sessionStartTimes[sid];
                        const url = project
                          ? getReplayUrl(sessionProvider, providerHost, providerProjectId, sid, eventTime, startTime)
                          : null;
                        const relTime = eventTime && startTime ? formatRelativeTime(eventTime, startTime) : "";
                        return (
                          <a
                            key={sid}
                            href={url || "#"}
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => { if (!url) e.preventDefault(); e.stopPropagation(); }}
                            className="flex items-center gap-3 p-3 bg-slate-50 border border-slate-200/80 rounded-xl hover:border-indigo-200 hover:bg-indigo-50/30 transition-all group"
                          >
                            {/* Play button thumbnail */}
                            <div className="w-16 h-11 bg-slate-900 rounded-lg flex items-center justify-center shrink-0 relative overflow-hidden group-hover:bg-indigo-900 transition-colors">
                              <div className="absolute inset-0 bg-gradient-to-br from-slate-700/50 to-transparent" />
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="white" className="relative z-10 opacity-80 group-hover:opacity-100 transition">
                                <polygon points="8 5 20 12 8 19 8 5" />
                              </svg>
                              {relTime ? (
                                <div className="absolute bottom-0.5 left-0.5 right-0.5 text-[8px] text-amber-300 font-mono text-center bg-black/60 rounded px-0.5">
                                  {relTime}
                                </div>
                              ) : (
                                <div className="absolute bottom-0.5 right-1 text-[8px] text-white/60 font-mono">REC</div>
                              )}
                            </div>

                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 mb-0.5">
                                <span className="text-xs font-semibold text-slate-800 group-hover:text-indigo-700 transition-colors">
                                  Session {idx + 1}
                                </span>
                                {idx === 0 && (
                                  <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700">Primary</span>
                                )}
                              </div>
                              <p className="text-[10px] text-slate-400 font-mono truncate">{sid}</p>
                              {eventTime && (
                                <div className="flex items-center gap-1 mt-0.5">
                                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <circle cx="12" cy="12" r="10" />
                                    <path d="M12 6v6l4 2" />
                                  </svg>
                                  <span className="text-[10px] text-amber-600 font-semibold">
                                    {relTime ? `Issue at ${relTime}` : `Issue at ${formatEventTime(eventTime)}`}
                                  </span>
                                </div>
                              )}
                            </div>

                            <div className="flex items-center gap-1 text-[11px] text-indigo-600 font-semibold shrink-0 group-hover:text-indigo-700">
                              {eventTime ? "Jump" : "Watch"}
                              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                                <path d="M15 3h6v6" />
                                <path d="M10 14L21 3" />
                              </svg>
                            </div>
                          </a>
                        );
                      })}
                    </div>
                  </div>
                );
              })()}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ── Main Issues Page ───────────────────────────────────────────────────── */

interface IssueWithProject extends AnomalyCluster {
  project_name: string;
}

export default function IssuesPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [issues, setIssues] = useState<IssueWithProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const PAGE_SIZE = 10;

  // Slide-over state
  const [selectedIssue, setSelectedIssue] = useState<{ projectId: string; fingerprint: string } | null>(null);

  useEffect(() => {
    if (!authLoading && !user) router.push("/login");
  }, [user, authLoading, router]);

  // Fetch projects first
  useEffect(() => {
    if (!user) return;
    listProjects().then(setProjects).catch(() => {});
  }, [user]);

  // Fetch issues when projects, page, or filter changes
  const fetchIssues = useCallback(async () => {
    if (!projects.length) return;
    setLoading(true);
    try {
      const allIssues: IssueWithProject[] = [];
      // For each project, fetch paginated issues
      // Note: In a real app, you'd want a single cross-project endpoint
      for (const p of projects) {
        const result = await listProjectIssues(p.id, {
          status: statusFilter || undefined,
          page: currentPage,
          pageSize: PAGE_SIZE,
        });
        for (const item of result.items) {
          allIssues.push({ ...item, project_name: p.name });
        }
        // Use totals from first project for simplicity (multi-project pagination is approximate)
        if (projects.length === 1) {
          setTotalPages(result.total_pages);
          setTotalCount(result.total);
        }
      }

      // Sort by last_seen desc
      allIssues.sort((a, b) => new Date(b.last_seen).getTime() - new Date(a.last_seen).getTime());

      // If multi-project, do client-side pagination
      if (projects.length > 1) {
        setTotalCount(allIssues.length);
        setTotalPages(Math.ceil(allIssues.length / PAGE_SIZE));
        const start = (currentPage - 1) * PAGE_SIZE;
        setIssues(allIssues.slice(start, start + PAGE_SIZE));
      } else {
        setIssues(allIssues);
      }
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, [projects, statusFilter, currentPage]);

  useEffect(() => {
    fetchIssues();
  }, [fetchIssues]);

  // Reset page when filter changes
  useEffect(() => {
    setCurrentPage(1);
  }, [statusFilter]);

  const handleStatusChanged = (fingerprint: string, newStatus: string) => {
    // If changed to not_an_issue and we're not filtering by not_an_issue, remove from list
    if (newStatus === "not_an_issue" && statusFilter !== "not_an_issue") {
      setIssues((prev) => prev.filter((i) => i.fingerprint !== fingerprint));
      return;
    }
    setIssues((prev) =>
      prev.map((i) => (i.fingerprint === fingerprint ? { ...i, status: newStatus } : i))
    );
  };

  const handleRowStatusChange = async (issue: IssueWithProject, newStatus: string) => {
    try {
      await updateIssueStatus(issue.project_id, issue.fingerprint, newStatus);
      handleStatusChanged(issue.fingerprint, newStatus);
    } catch {}
  };

  if (authLoading || !user) {
    return (
      <div className="flex items-center justify-center py-20">
        <svg className="animate-spin h-6 w-6 text-indigo-400" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-[22px] font-bold text-slate-900">Issues</h1>
        <p className="text-sm text-slate-500 mt-1">
          All detected anomalies across your projects
        </p>
      </div>

      {/* Status filter */}
      <div className="flex items-center gap-2 mb-6 flex-wrap">
        {[
          { key: "", label: "All" },
          { key: "new", label: "New" },
          { key: "in_progress", label: "In Progress" },
          { key: "resolved", label: "Resolved" },
          { key: "closed", label: "Closed" },
        ].map((f) => (
          <button
            key={f.key}
            onClick={() => setStatusFilter(f.key)}
            className={`px-3.5 py-1.5 rounded-lg text-xs font-semibold transition-all ${
              statusFilter === f.key
                ? "bg-indigo-600 text-white shadow-md shadow-indigo-500/15"
                : "bg-white border border-slate-200 text-slate-600 hover:border-slate-300 hover:text-slate-800"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <svg className="animate-spin h-6 w-6 text-indigo-400" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        </div>
      ) : issues.length === 0 ? (
        <div className="text-center py-20 bg-white border border-slate-200/80 rounded-2xl shadow-sm">
          <div className="w-16 h-16 bg-slate-50 rounded-2xl flex items-center justify-center mx-auto mb-5">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <path d="M8 14c0 0 1.5 2 4 2s4-2 4-2" />
              <circle cx="9" cy="9" r="1" fill="#22c55e" />
              <circle cx="15" cy="9" r="1" fill="#22c55e" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold text-slate-800 mb-2">No issues found</h2>
          <p className="text-sm text-slate-500 max-w-xs mx-auto">
            {statusFilter ? "No issues match this filter." : "No anomalies detected yet. Issues will appear here once your projects start running."}
          </p>
        </div>
      ) : (
        <>
          <div className="bg-white border border-slate-200/80 rounded-2xl overflow-hidden shadow-sm">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-100 text-left bg-slate-50/50">
                  <th className="px-5 py-3.5 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Issue</th>
                  <th className="px-5 py-3.5 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Type</th>
                  <th className="px-5 py-3.5 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Project</th>
                  <th className="px-5 py-3.5 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Count</th>
                  <th className="px-5 py-3.5 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Users</th>
                  <th className="px-5 py-3.5 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Status</th>
                  <th className="px-5 py-3.5 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Last seen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {issues.map((issue) => (
                  <tr
                    key={issue.id}
                    className="hover:bg-indigo-50/30 transition cursor-pointer"
                    onClick={() => setSelectedIssue({ projectId: issue.project_id, fingerprint: issue.fingerprint })}
                  >
                    <td className="px-5 py-4">
                      <p className="font-mono text-slate-800 truncate max-w-xs text-[13px]">
                        {issue.error_message || issue.endpoint || issue.css_selector || issue.page_url || "Unknown"}
                      </p>
                    </td>
                    <td className="px-5 py-4">
                      <div className="flex items-center gap-1.5">
                        {isAiDetected(issue.event_type) && (
                          <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-violet-100 text-violet-700 border border-violet-200">AI</span>
                        )}
                        <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-md ${getTypeColor(issue.event_type)}`}>
                          {getTypeLabel(issue.event_type)}
                        </span>
                      </div>
                    </td>
                    <td className="px-5 py-4 text-slate-600 text-[13px] font-medium">{issue.project_name}</td>
                    <td className="px-5 py-4 text-slate-700 font-semibold text-[13px]">{issue.count}</td>
                    <td className="px-5 py-4 text-slate-700 font-semibold text-[13px]">{issue.affected_users}</td>
                    <td className="px-5 py-4">
                      <StatusDropdown
                        currentStatus={issue.status}
                        onStatusChange={(s) => handleRowStatusChange(issue, s)}
                      />
                    </td>
                    <td className="px-5 py-4 text-slate-400 text-xs">{timeAgo(issue.last_seen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4">
              <p className="text-xs text-slate-500">
                Showing {(currentPage - 1) * PAGE_SIZE + 1}–{Math.min(currentPage * PAGE_SIZE, totalCount)} of {totalCount} issues
              </p>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                  disabled={currentPage === 1}
                  className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition disabled:opacity-40"
                >
                  Previous
                </button>
                {Array.from({ length: Math.min(totalPages, 5) }, (_, i) => {
                  let page: number;
                  if (totalPages <= 5) page = i + 1;
                  else if (currentPage <= 3) page = i + 1;
                  else if (currentPage >= totalPages - 2) page = totalPages - 4 + i;
                  else page = currentPage - 2 + i;
                  return (
                    <button
                      key={page}
                      onClick={() => setCurrentPage(page)}
                      className={`w-8 h-8 rounded-lg text-xs font-semibold transition ${
                        currentPage === page
                          ? "bg-indigo-600 text-white"
                          : "bg-white border border-slate-200 text-slate-600 hover:bg-slate-50"
                      }`}
                    >
                      {page}
                    </button>
                  );
                })}
                <button
                  onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                  disabled={currentPage === totalPages}
                  className="px-3 py-1.5 rounded-lg text-xs font-semibold bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {/* Slide-over panel */}
      {selectedIssue && (
        <IssueSlideOver
          projectId={selectedIssue.projectId}
          fingerprint={selectedIssue.fingerprint}
          onClose={() => setSelectedIssue(null)}
          onStatusChanged={handleStatusChanged}
        />
      )}
    </div>
  );
}
