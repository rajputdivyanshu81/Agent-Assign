"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import styles from "./page.module.css";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
interface LogEntry {
  step: number;
  step_type: string;
  message: string;
  timestamp: string;
}

type AgentStatus =
  | "idle"
  | "running"
  | "paused"
  | "stopped"
  | "completed"
  | "error"
  | "blocked"
  | "awaiting_approval";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export default function Home() {
  const [goal, setGoal] = useState("");
  const [status, setStatus] = useState<AgentStatus>("idle");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [screenshot, setScreenshot] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [approvalMode, setApprovalMode] = useState(false);
  const [resultData, setResultData] = useState<Record<string, unknown> | null>(null);

  const ws = useRef<WebSocket | null>(null);
  const logsEndRef = useRef<HTMLDivElement | null>(null);
  const reconnectTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  // -----------------------------------------------------------------------
  // WebSocket connection with auto-reconnect
  // -----------------------------------------------------------------------
  const connectWs = useCallback(() => {
    const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || "ws://localhost:8000";
    const socket = new WebSocket(`${backendUrl}/ws`);
    ws.current = socket;

    socket.onopen = () => {
      setWsConnected(true);
      if (reconnectTimeout.current) {
        clearTimeout(reconnectTimeout.current);
        reconnectTimeout.current = null;
      }
    };

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);

      switch (data.type) {
        case "status":
          setStatus(data.status as AgentStatus);
          if (data.run_id) setRunId(data.run_id);
          break;

        case "log":
          setLogs((prev) => [
            ...prev,
            {
              step: data.step,
              step_type: data.step_type,
              message: data.message,
              timestamp: new Date().toLocaleTimeString(),
            },
          ]);
          break;

        case "screenshot":
          setScreenshot(data.image);
          break;

        case "result":
          setResultData(data.data);
          break;

        case "error":
          setLogs((prev) => [
            ...prev,
            {
              step: 0,
              step_type: "error",
              message: data.message,
              timestamp: new Date().toLocaleTimeString(),
            },
          ]);
          break;
      }
    };

    socket.onclose = () => {
      setWsConnected(false);
      // Auto-reconnect after 3s
      reconnectTimeout.current = setTimeout(() => connectWs(), 3000);
    };

    socket.onerror = () => {
      socket.close();
    };
  }, []);

  useEffect(() => {
    connectWs();
    return () => {
      ws.current?.close();
      if (reconnectTimeout.current) clearTimeout(reconnectTimeout.current);
    };
  }, [connectWs]);

  // Auto-scroll logs
  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  // -----------------------------------------------------------------------
  // Actions
  // -----------------------------------------------------------------------
  const send = (msg: Record<string, unknown>) => {
    if (ws.current?.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify(msg));
    }
  };

  const handleStart = () => {
    if (!goal.trim() || goal.trim().length < 10) {
      alert("Please enter a goal with at least 10 characters.");
      return;
    }
    setLogs([]);
    setScreenshot(null);
    setResultData(null);
    setRunId(null);
    setStatus("idle");
    send({ type: "start", goal });
  };

  const handleStop = () => send({ type: "stop" });
  const handlePause = () => send({ type: "pause" });
  const handleResume = () => send({ type: "resume" });
  const handleApprove = () => send({ type: "approve" });

  const toggleApprovalMode = () => {
    const next = !approvalMode;
    setApprovalMode(next);
    send({ type: "set_approval_mode", enabled: next });
  };

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------
  const isRunning = status === "running" || status === "paused" || status === "awaiting_approval";

  const getStatusStyle = () => {
    const map: Record<string, string> = {
      idle: styles.agentStatusIdle,
      running: styles.agentStatusRunning,
      paused: styles.agentStatusPaused,
      stopped: styles.agentStatusError,
      completed: styles.agentStatusCompleted,
      error: styles.agentStatusError,
      blocked: styles.agentStatusBlocked,
      awaiting_approval: styles.agentStatusPaused,
    };
    return map[status] || styles.agentStatusIdle;
  };

  const getStatusLabel = () => {
    if (status === "awaiting_approval") return "Awaiting Approval";
    return status.charAt(0).toUpperCase() + status.slice(1);
  };

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------
  return (
    <main className={styles.main}>
      {/* ===== HEADER ===== */}
      <header className={styles.header}>
        <div className={styles.headerLeft}>
          <div className={styles.logo}>M</div>
          <h1 className={styles.headerTitle}>Minerva</h1>
        </div>
        <div className={styles.headerRight}>
          <div className={`${styles.agentStatus} ${getStatusStyle()}`}>
            {getStatusLabel()}
          </div>
          <div className={styles.statusBadge}>
            <span
              className={`${styles.statusDot} ${
                wsConnected ? styles.statusDotConnected : styles.statusDotDisconnected
              }`}
            />
            {wsConnected ? "Connected" : "Disconnected"}
          </div>
        </div>
      </header>

      {/* ===== CONSOLE GRID ===== */}
      <div className={styles.consoleGrid}>
        {/* --- Left Pane --- */}
        <div className={styles.leftPane}>
          {/* Goal Input Card */}
          <div className={styles.card}>
            <div className={styles.cardHeader}>
              <span className={styles.cardIcon}>🎯</span>
              <span className={styles.cardTitle}>Agent Goal</span>
            </div>
            <textarea
              className={styles.inputGoal}
              placeholder="e.g., Research pricing plans for Slack, Notion, and Asana and compile them into a comparison table..."
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              disabled={isRunning}
            />
            <div className={styles.controls}>
              {!isRunning ? (
                <button
                  className={styles.btnPrimary}
                  onClick={handleStart}
                  disabled={!wsConnected || !goal.trim()}
                >
                  ▶ Run Agent
                </button>
              ) : (
                <>
                  {status === "paused" ? (
                    <button className={styles.btnSecondary} onClick={handleResume}>
                      ▶ Resume
                    </button>
                  ) : (
                    <button className={styles.btnSecondary} onClick={handlePause}>
                      ⏸ Pause
                    </button>
                  )}
                  {status === "awaiting_approval" && (
                    <button className={styles.btnApprove} onClick={handleApprove}>
                      ✅ Approve
                    </button>
                  )}
                  <button className={styles.btnDanger} onClick={handleStop}>
                    ⏹ Stop
                  </button>
                </>
              )}
            </div>

            {/* Approval mode toggle */}
            <div className={styles.toggleContainer}>
              <button
                className={`${styles.toggle} ${approvalMode ? styles.toggleActive : ""}`}
                onClick={toggleApprovalMode}
                disabled={isRunning}
              >
                <div className={styles.toggleKnob} />
              </button>
              <span className={styles.toggleLabel}>Step-by-step approval mode</span>
            </div>
          </div>

          {/* Reasoning Feed Card */}
          <div className={styles.logsCard}>
            <div className={styles.cardHeader}>
              <span className={styles.cardIcon}>🧠</span>
              <span className={styles.cardTitle}>Reasoning Feed</span>
            </div>
            <div className={styles.logsContainer}>
              {logs.length === 0 ? (
                <div className={styles.logsEmpty}>
                  Agent reasoning will appear here...
                </div>
              ) : (
                logs.map((log, idx) => (
                  <div
                    key={idx}
                    className={`${styles.logEntry} ${styles[log.step_type] || ""}`}
                  >
                    <span className={styles.logTime}>[{log.timestamp}]</span>
                    <span className={styles.logStep}>
                      {log.step > 0 ? `Step ${log.step}` : ""}{" "}
                      ({log.step_type.toUpperCase()})
                    </span>
                    <span className={styles.logMsg}>{log.message}</span>
                  </div>
                ))
              )}
              <div ref={logsEndRef} />
            </div>
          </div>
        </div>

        {/* --- Right Pane --- */}
        <div className={styles.rightPane}>
          <div className={styles.browserCard}>
            <div className={styles.cardHeader}>
              <span className={styles.cardIcon}>🌐</span>
              <span className={styles.cardTitle}>Live Browser View</span>
            </div>

            {/* Faux browser chrome */}
            <div className={styles.browserUrlBar}>
              <div className={styles.browserUrlDots}>
                <span className={`${styles.browserDot} ${styles.browserDotRed}`} />
                <span className={`${styles.browserDot} ${styles.browserDotYellow}`} />
                <span className={`${styles.browserDot} ${styles.browserDotGreen}`} />
              </div>
              <span className={styles.browserUrlText}>
                {screenshot ? "Agent is browsing..." : "Waiting for agent..."}
              </span>
            </div>

            {/* Viewport */}
            <div className={styles.browserViewport}>
              {screenshot ? (
                <img
                  className={styles.browserScreenshot}
                  src={`data:image/jpeg;base64,${screenshot}`}
                  alt="Live browser screenshot"
                  onError={() => setScreenshot(null)}
                />
              ) : (
                <div className={styles.browserPlaceholder}>
                  <div className={styles.browserPlaceholderIcon}>🖥️</div>
                  <p className={styles.browserPlaceholderText}>
                    Live screenshots will stream here once the agent starts browsing.
                  </p>
                </div>
              )}

              {/* Thinking indicator overlay */}
              {status === "running" && (
                <div className={styles.thinkingIndicator}>
                  <div className={styles.thinkingDots}>
                    <span className={styles.thinkingDot} />
                    <span className={styles.thinkingDot} />
                    <span className={styles.thinkingDot} />
                  </div>
                  Thinking...
                </div>
              )}
            </div>
          </div>

          {/* Result card */}
          {resultData && (
            <div className={styles.resultCard}>
              <div className={styles.cardHeader}>
                <span className={styles.cardIcon}>📊</span>
                <span className={styles.cardTitle}>Extracted Results</span>
              </div>
              <table className={styles.resultTable}>
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Value</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(resultData).map(([key, value]) => (
                    <tr key={key}>
                      <td>{key}</td>
                      <td>
                        {typeof value === "object"
                          ? JSON.stringify(value, null, 2)
                          : String(value)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
