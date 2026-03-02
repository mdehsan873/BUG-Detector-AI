"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { getProject, triggerAnalysis, getAnalysisProgress, getLatestAnalysis } from "@/lib/api";
import { ProjectDetail, AnomalyCluster, AnalysisProgress, SessionIssue } from "@/lib/types";

const statusColors: Record<string, string> = {
  new: "bg-amber-50 text-amber-700 border border-amber-100",
  in_progress: "bg-blue-50 text-blue-700 border border-blue-100",
  github_issued: "bg-sky-50 text-sky-700 border border-sky-100",
  resolved: "bg-emerald-50 text-emerald-700 border border-emerald-100",
  closed: "bg-slate-100 text-slate-600 border border-slate-200",
  not_an_issue: "bg-slate-50 text-slate-500 border border-slate-200",
};

const eventTypeLabels: Record<string, string> = {
  // Rule-based detection categories
  rage_click: "Rage Click",
  dead_click: "Dead Click",
  navigation_loop: "Navigation Loop",
  rapid_back_nav: "Instant Bounce",
  stuck_interaction: "Stuck Interaction",
  form_abandonment: "Form Abandoned",
  button_spam: "Button Spam",
  broken_flow: "Flow Drop-off",
  scroll_frustration: "Scroll Frustration",
  rapid_refresh: "Rapid Refresh",
  unexpected_exit: "Exit Spike",
  // Legacy AI categories (kept for backward compat)
  ai_broken_ui: "Broken UI",
  ai_error: "Error",
  ai_ux_friction: "UX Friction",
  ai_dead_end: "Dead End",
  ai_confusing_flow: "Confusing Flow",
  ai_performance: "Performance",
  ai_data_loss: "Data Loss",
  ai_form_validation: "Form Validation",
  ai_dead_click: "Dead Click",
  ai_refresh_workaround: "Refresh Workaround",
  ai_session_expiry: "Session Expiry",
  ai_broken_navigation: "Broken Navigation",
  ai_double_action: "Double Action",
};

const eventTypeColors: Record<string, string> = {
  // Rule-based detection categories
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
  // Legacy AI categories
  ai_broken_ui: "bg-red-50 text-red-700 border border-red-100",
  ai_error: "bg-rose-50 text-rose-700 border border-rose-100",
  ai_ux_friction: "bg-amber-50 text-amber-700 border border-amber-100",
  ai_dead_end: "bg-pink-50 text-pink-700 border border-pink-100",
  ai_confusing_flow: "bg-indigo-50 text-indigo-700 border border-indigo-100",
  ai_performance: "bg-yellow-50 text-yellow-700 border border-yellow-100",
  ai_data_loss: "bg-red-50 text-red-800 border border-red-200",
  ai_form_validation: "bg-orange-50 text-orange-700 border border-orange-100",
  ai_dead_click: "bg-violet-50 text-violet-700 border border-violet-100",
  ai_refresh_workaround: "bg-amber-50 text-amber-800 border border-amber-200",
  ai_session_expiry: "bg-red-50 text-red-800 border border-red-200",
  ai_broken_navigation: "bg-purple-50 text-purple-700 border border-purple-100",
  ai_double_action: "bg-rose-50 text-rose-700 border border-rose-100",
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

/* ── Analysis Progress Card ───────────────────────────────────────────────── */

function AnalysisCard({
  analysis,
  onStartAnalysis,
  analyzing,
}: {
  analysis: AnalysisProgress | null;
  onStartAnalysis: () => void;
  analyzing: boolean;
}) {
  const isRunning = analysis?.status === "running";
  const isCompleted = analysis?.status === "completed";
  const progress =
    isRunning && analysis.sessions_total > 0
      ? Math.round((analysis.sessions_analyzed / analysis.sessions_total) * 100)
      : isCompleted
        ? 100
        : 0;

  return (
    <div className="bg-white border border-slate-200/80 rounded-2xl p-6 mb-8 shadow-sm">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${isRunning ? "bg-indigo-100" : isCompleted ? "bg-emerald-100" : "bg-slate-100"}`}>
            {isRunning ? (
              <svg className="animate-spin h-5 w-5 text-indigo-600" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            ) : isCompleted ? (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#059669" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" />
                <path d="M9 12l2 2 4-4" />
              </svg>
            ) : (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="11" cy="11" r="8" />
                <path d="M21 21l-4.35-4.35" />
              </svg>
            )}
          </div>
          <div>
            <h3 className="text-[15px] font-semibold text-slate-900">
              {isRunning ? "Analyzing Sessions" : isCompleted ? "Analysis Complete" : "AI Session Analysis"}
            </h3>
            <p className="text-xs text-slate-500 mt-0.5">
              {isRunning
                ? "Scanning your session recordings for issues"
                : isCompleted
                  ? `Found ${analysis.issues_found} issue${analysis.issues_found !== 1 ? "s" : ""} across ${analysis.sessions_analyzed} sessions`
                  : "Use AI to analyze real user sessions and find hidden bugs"}
            </p>
          </div>
        </div>
        {!isRunning && (
          <button
            onClick={onStartAnalysis}
            disabled={analyzing}
            className="bg-indigo-600 text-white px-4 py-2 rounded-xl text-sm font-semibold hover:bg-indigo-700 transition shadow-md shadow-indigo-500/15 disabled:opacity-50 flex items-center gap-2"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8" />
              <path d="M21 21l-4.35-4.35" />
            </svg>
            {isCompleted ? "Re-analyze" : "Start Analysis"}
          </button>
        )}
      </div>

      {/* Progress bar */}
      {(isRunning || isCompleted) && (
        <div className="space-y-3">
          <div className="flex items-center justify-between text-xs">
            <span className="text-slate-500 font-medium">Progress</span>
            <span className="text-slate-700 font-semibold">
              {analysis!.sessions_analyzed} / {analysis!.sessions_total || "..."} sessions
            </span>
          </div>
          <div className="w-full bg-slate-100 rounded-full h-2 overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${isCompleted ? "bg-emerald-500" : "bg-indigo-500"}`}
              style={{ width: `${progress}%` }}
            />
          </div>

          {isRunning && (
            <p className="text-xs text-slate-400 flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-indigo-500 animate-pulse" />
              Processing sessions...
            </p>
          )}

          {/* Stats row */}
          <div className="flex gap-8 pt-2">
            <div>
              <p className="text-2xl font-bold text-slate-900">{analysis!.sessions_analyzed}</p>
              <p className="text-[11px] text-slate-500 font-medium">Analyzed</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ── AI Issue Card ────────────────────────────────────────────────────────── */

function AIIssueCard({ issue, projectId }: { issue: SessionIssue; projectId: string }) {
  const fp = issue.fingerprint ? encodeURIComponent(issue.fingerprint) : "";
  const Wrapper = fp ? Link : "div" as any;
  const wrapperProps = fp ? { href: `/projects/${projectId}/issues/${fp}` } : {};

  return (
    <Wrapper {...wrapperProps} className="block bg-white border border-slate-200/80 rounded-2xl p-5 hover:shadow-md hover:border-indigo-200 transition-all cursor-pointer group">
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-md ${severityColors[issue.severity] || "bg-slate-50 text-slate-600 border border-slate-100"}`}>
            {issue.severity}
          </span>
          <span className="text-[11px] font-semibold px-2 py-0.5 rounded-md bg-violet-50 text-violet-700 border border-violet-100">
            AI Detected
          </span>
          <span className="text-[11px] font-medium px-2 py-0.5 rounded-md bg-slate-50 text-slate-600 border border-slate-100">
            {issue.category.replace(/_/g, " ")}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0 ml-2">
          <span className="text-xs text-slate-400 font-medium">
            {Math.round(issue.confidence * 100)}% confidence
          </span>
          {issue.session_id && (
            <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 border border-blue-100 flex items-center gap-1">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              Replay
            </span>
          )}
        </div>
      </div>

      <p className="text-[14px] font-semibold text-slate-900 mb-1 group-hover:text-indigo-700 transition-colors">{issue.title}</p>
      <p className="text-sm text-slate-500 leading-relaxed line-clamp-2">{issue.description}</p>

      {issue.page_url && (
        <p className="text-xs text-slate-400 truncate mt-2 font-mono">{issue.page_url}</p>
      )}

      <div className="mt-3 flex items-center justify-between">
        {issue.evidence && issue.evidence.length > 0 && (
          <span className="text-xs text-slate-400">{issue.evidence.length} evidence items</span>
        )}
        <span className="text-xs text-indigo-600 font-semibold opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
          View details
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 18l6-6-6-6" />
          </svg>
        </span>
      </div>
    </Wrapper>
  );
}

/* ── Anomaly Card ─────────────────────────────────────────────────────────── */

function AnomalyCard({ anomaly, projectId }: { anomaly: AnomalyCluster; projectId: string }) {
  const fp = encodeURIComponent(anomaly.fingerprint);
  const hasSession = anomaly.sample_session_ids && anomaly.sample_session_ids.length > 0;

  return (
    <Link
      href={`/projects/${projectId}/issues/${fp}`}
      className="block bg-white border border-slate-200/80 rounded-2xl p-5 hover:shadow-md hover:border-indigo-200 transition-all group"
    >
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-md ${eventTypeColors[anomaly.event_type] || "bg-slate-50 text-slate-600 border border-slate-100"}`}>
            {eventTypeLabels[anomaly.event_type] || anomaly.event_type}
          </span>
          <span className={`text-[11px] font-semibold px-2 py-0.5 rounded-md ${statusColors[anomaly.status] || "bg-slate-50 text-slate-600 border border-slate-100"}`}>
            {anomaly.status}
          </span>
          {hasSession && (
            <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 border border-blue-100 flex items-center gap-1">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
              Replay
            </span>
          )}
        </div>
        <span className="text-xs text-slate-400 font-medium">{timeAgo(anomaly.last_seen)}</span>
      </div>

      <p className="text-[13px] text-slate-800 font-mono truncate font-medium group-hover:text-indigo-700 transition-colors">
        {anomaly.error_message || anomaly.endpoint || anomaly.css_selector || "Unknown"}
      </p>

      {anomaly.page_url && (
        <p className="text-xs text-slate-400 truncate mt-1">{anomaly.page_url}</p>
      )}

      <div className="flex items-center justify-between mt-4">
        <div className="flex gap-5 text-xs">
          <span className="text-slate-500">
            <span className="font-semibold text-slate-700">{anomaly.count}</span> occurrences
          </span>
          <span className="text-slate-500">
            <span className="font-semibold text-slate-700">{anomaly.affected_users}</span> users
          </span>
          <span className="text-slate-400">First: {timeAgo(anomaly.first_seen)}</span>
        </div>
        <span className="text-xs text-indigo-600 font-semibold opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1">
          View
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9 18l6-6-6-6" />
          </svg>
        </span>
      </div>
    </Link>
  );
}

/* ── Main Page ────────────────────────────────────────────────────────────── */

export default function ProjectDetailPage() {
  const params = useParams();
  const projectId = params.id as string;

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Analysis state
  const [analysis, setAnalysis] = useState<AnalysisProgress | null>(null);
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const pollRef = useRef<NodeJS.Timeout | null>(null);

  useEffect(() => {
    if (projectId) {
      getProject(projectId)
        .then(setProject)
        .catch((e) => setError(e.message))
        .finally(() => setLoading(false));

      // Check for existing analysis
      getLatestAnalysis(projectId)
        .then((a) => {
          if (a.status !== "none") {
            setAnalysis(a);
            if (a.status === "running") setAnalyzing(true);
          }
        })
        .catch(() => {});
    }
  }, [projectId]);

  // Poll analysis progress
  useEffect(() => {
    if (analysisId && analyzing) {
      pollRef.current = setInterval(async () => {
        try {
          const progress = await getAnalysisProgress(projectId, analysisId);
          setAnalysis(progress);
          if (progress.status === "completed" || progress.status === "failed") {
            setAnalyzing(false);
            if (pollRef.current) clearInterval(pollRef.current);
            // Refresh project data to get new anomalies
            getProject(projectId).then(setProject);
          }
        } catch {
          // ignore
        }
      }, 2000);

      return () => {
        if (pollRef.current) clearInterval(pollRef.current);
      };
    }
  }, [analysisId, analyzing, projectId]);

  const handleStartAnalysis = useCallback(async () => {
    try {
      setAnalyzing(true);
      const result = await triggerAnalysis(projectId);
      setAnalysisId(result.analysis_id);
      setAnalysis({
        project_id: projectId,
        status: "running",
        sessions_total: 0,
        sessions_analyzed: 0,
        issues_found: 0,
        issues: [],
      });
    } catch {
      setAnalyzing(false);
    }
  }, [projectId]);

  if (loading)
    return (
      <div className="flex items-center justify-center py-20">
        <svg className="animate-spin h-6 w-6 text-indigo-400" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      </div>
    );
  if (error)
    return <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-xl text-sm">{error}</div>;
  if (!project)
    return <p className="text-slate-500">Project not found</p>;

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-[22px] font-bold text-slate-900">{project.name}</h1>
          <div className="flex items-center gap-3 mt-2">
            <span className="text-sm text-slate-500 flex items-center gap-1.5">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22" />
              </svg>
              <span className="font-medium">{project.github_repo}</span>
            </span>
            <span className="text-xs text-slate-400 bg-slate-100 px-2.5 py-0.5 rounded-md font-medium">
              Threshold: {project.detection_threshold}
            </span>
            <span className="text-xs text-slate-400 bg-slate-100 px-2.5 py-0.5 rounded-md font-medium">
              Min Sessions: {(project as any).min_sessions_threshold ?? 2}
            </span>
            <span className="text-xs text-indigo-600 bg-indigo-50 px-2.5 py-0.5 rounded-md font-medium border border-indigo-100">
              PostHog
            </span>
            {(project as any).skip_page_patterns?.length > 0 && (
              <span className="text-xs text-amber-600 bg-amber-50 px-2.5 py-0.5 rounded-md font-medium border border-amber-100">
                Skip: {(project as any).skip_page_patterns.join(", ")}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* AI Session Analysis Card — only show when PostHog is connected */}
      {(project as any).provider_project_id && (
        <AnalysisCard
          analysis={analysis}
          onStartAnalysis={handleStartAnalysis}
          analyzing={analyzing}
        />
      )}

      {/* Rule-Based Anomalies (exclude not_an_issue) */}
      {(() => {
        const visibleAnomalies = project.recent_anomalies.filter((a) => a.status !== "not_an_issue" && a.status !== "resolved" && a.status !== "closed");
        return (
          <>
            <h2 className="text-lg font-semibold text-slate-900 mb-4">
              Detected Issues <span className="text-slate-400 font-normal">({visibleAnomalies.length})</span>
            </h2>

            {visibleAnomalies.length === 0 ? (
              <div className="text-center py-16 bg-white border border-slate-200/80 rounded-2xl shadow-sm">
                <p className="text-sm text-slate-500">
                  No anomalies detected yet. Run a detection or start an AI analysis.
                </p>
              </div>
            ) : (
              <div className="grid gap-3">
                {visibleAnomalies.map((anomaly) => (
                  <AnomalyCard key={anomaly.id} anomaly={anomaly} projectId={projectId} />
                ))}
              </div>
            )}
          </>
        );
      })()}
    </div>
  );
}
