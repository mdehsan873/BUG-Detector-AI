"use client";

import { useAuth } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

export default function LandingPage() {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && user) {
      router.push("/dashboard");
    }
  }, [user, loading, router]);

  return (
    <div className="min-h-screen bg-white">
      {/* Hero Section */}
      <header className="relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-slate-50 via-white to-indigo-50/40" />
        <div className="absolute inset-0">
          <div className="absolute top-20 left-10 w-72 h-72 bg-indigo-100 rounded-full blur-3xl opacity-30" />
          <div className="absolute top-40 right-20 w-96 h-96 bg-violet-100 rounded-full blur-3xl opacity-25" />
          <div className="absolute bottom-10 left-1/3 w-80 h-80 bg-sky-100 rounded-full blur-3xl opacity-20" />
        </div>

        {/* Nav */}
        <nav className="relative z-10 max-w-6xl mx-auto px-6 py-6 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <img src="/logo.svg" alt="Buglyft" className="w-9 h-9 rounded-xl shadow-lg shadow-indigo-500/20" />
            <span className="text-lg font-bold text-slate-900">Buglyft</span>
          </div>
          <div className="flex items-center gap-3">
            <a
              href="/login"
              className="text-sm font-medium text-slate-600 hover:text-slate-900 transition px-4 py-2"
            >
              Log in
            </a>
            <a
              href="/signup"
              className="text-sm font-semibold bg-indigo-600 text-white px-5 py-2.5 rounded-xl hover:bg-indigo-700 transition shadow-lg shadow-indigo-500/20"
            >
              Get Started
            </a>
          </div>
        </nav>

        {/* Hero Content */}
        <div className="relative z-10 max-w-6xl mx-auto px-6 pt-24 pb-36">
          <div className="max-w-3xl mx-auto text-center">
            <div className="inline-flex items-center gap-2 bg-indigo-50 border border-indigo-100 rounded-full px-4 py-1.5 mb-8">
              <span className="w-2 h-2 bg-indigo-500 rounded-full animate-pulse" />
              <span className="text-sm font-medium text-indigo-700">AI-powered production monitoring</span>
            </div>

            <h1 className="text-4xl sm:text-5xl lg:text-[3.5rem] font-bold text-slate-900 leading-[1.1] tracking-tight mb-6">
              Find bugs, friction &amp; UX issues
              <br />
              <span className="bg-gradient-to-r from-indigo-600 to-violet-600 bg-clip-text text-transparent">
                before your users complain
              </span>
            </h1>

            <p className="text-lg text-slate-500 max-w-xl mx-auto mb-10 leading-relaxed">
              AI monitors your sessions for errors, rage clicks, dead ends, and broken flows —
              then creates GitHub issues automatically. Zero manual triage.
            </p>

            <div className="flex items-center justify-center gap-4">
              <a
                href="/signup"
                className="bg-indigo-600 text-white px-8 py-3.5 rounded-xl text-sm font-semibold hover:bg-indigo-700 transition shadow-xl shadow-indigo-500/25"
              >
                Start Free
              </a>
              <a
                href="#how-it-works"
                className="bg-white text-slate-700 border border-slate-200 px-8 py-3.5 rounded-xl text-sm font-semibold hover:border-slate-300 hover:shadow-sm transition"
              >
                See How It Works
              </a>
            </div>
          </div>
        </div>
      </header>

      {/* How It Works */}
      <section id="how-it-works" className="py-28 bg-white border-t border-slate-100">
        <div className="max-w-6xl mx-auto px-6">
          <div className="text-center mb-16">
            <span className="text-xs font-semibold uppercase tracking-widest text-indigo-600 mb-3 block">How it works</span>
            <h2 className="text-3xl font-bold text-slate-900 mb-4">Set up in three simple steps</h2>
            <p className="text-slate-500 max-w-md mx-auto">
              Go from zero to automated issue detection in under five minutes.
            </p>
          </div>

          <div className="grid md:grid-cols-3 gap-6">
            {[
              {
                step: "01",
                title: "Connect PostHog",
                desc: "Link your PostHog project and GitHub repo. All credentials are encrypted at rest.",
                color: "bg-indigo-50 border-indigo-100",
                iconColor: "text-indigo-600",
                icon: (
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                    <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
                  </svg>
                ),
              },
              {
                step: "02",
                title: "AI Analyzes Sessions",
                desc: "Monitors console errors, API failures, rage clicks, dead ends, and visible error messages.",
                color: "bg-violet-50 border-violet-100",
                iconColor: "text-violet-600",
                icon: (
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="11" cy="11" r="8" />
                    <path d="M21 21l-4.35-4.35" />
                  </svg>
                ),
              },
              {
                step: "03",
                title: "Issues Created Automatically",
                desc: "High-confidence issues become GitHub issues with repro steps, severity, and session replays.",
                color: "bg-emerald-50 border-emerald-100",
                iconColor: "text-emerald-600",
                icon: (
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="10" />
                    <path d="M9 12l2 2 4-4" />
                  </svg>
                ),
              },
            ].map((item) => (
              <div
                key={item.step}
                className={`${item.color} border rounded-2xl p-8 hover:shadow-md transition-shadow`}
              >
                <div className={`w-12 h-12 bg-white rounded-xl flex items-center justify-center ${item.iconColor} shadow-sm mb-5`}>
                  {item.icon}
                </div>
                <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">
                  Step {item.step}
                </span>
                <h3 className="text-lg font-semibold text-slate-900 mt-2 mb-3">
                  {item.title}
                </h3>
                <p className="text-sm text-slate-500 leading-relaxed">{item.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="py-28 bg-slate-50/80 border-t border-slate-100">
        <div className="max-w-6xl mx-auto px-6">
          <div className="text-center mb-16">
            <span className="text-xs font-semibold uppercase tracking-widest text-indigo-600 mb-3 block">Features</span>
            <h2 className="text-3xl font-bold text-slate-900 mb-4">
              Built for engineering teams
            </h2>
            <p className="text-slate-500 max-w-md mx-auto">
              Focus on building features, not triaging issues manually.
            </p>
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-5">
            {[
              { title: "Console Error Detection", desc: "Catches repeated JavaScript errors and exceptions across sessions.", icon: "terminal" },
              { title: "API Failure Monitoring", desc: "Flags endpoints returning 500+ errors above your threshold.", icon: "server" },
              { title: "Rage Click Detection", desc: "Identifies frustrated users clicking the same element repeatedly.", icon: "cursor" },
              { title: "Dead Click & Dead End Detection", desc: "Finds UI elements that don't respond and pages where users get stuck.", icon: "block" },
              { title: "AI Issue Reports", desc: "AI generates structured reports with severity, root cause, and repro steps.", icon: "sparkle" },
              { title: "Session Replay Links", desc: "Every issue includes direct links to PostHog session replays.", icon: "play" },
            ].map((f) => (
              <div
                key={f.title}
                className="bg-white rounded-2xl border border-slate-200/80 p-6 hover:border-indigo-200 hover:shadow-md transition-all"
              >
                <h3 className="font-semibold text-slate-900 mb-2 text-[15px]">{f.title}</h3>
                <p className="text-sm text-slate-500 leading-relaxed">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* CTA */}
      <section className="py-28 bg-white border-t border-slate-100">
        <div className="max-w-2xl mx-auto px-6 text-center">
          <h2 className="text-3xl font-bold text-slate-900 mb-4">
            Stop missing production issues
          </h2>
          <p className="text-slate-500 mb-8">
            Set up in under 5 minutes. No credit card required.
          </p>
          <a
            href="/signup"
            className="inline-block bg-indigo-600 text-white px-8 py-3.5 rounded-xl text-sm font-semibold hover:bg-indigo-700 transition shadow-xl shadow-indigo-500/25"
          >
            Get Started Free
          </a>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-slate-200 py-8 bg-slate-50/50">
        <div className="max-w-6xl mx-auto px-6 flex items-center justify-between text-sm text-slate-400">
          <span className="font-medium text-slate-500">Buglyft</span>
          <span>Built with PostHog, OpenAI & GitHub</span>
        </div>
      </footer>
    </div>
  );
}
