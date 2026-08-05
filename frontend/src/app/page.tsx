"use client";

import { useState, useEffect, useRef } from "react";
import styles from "./page.module.css";

interface LogEntry {
  step: number;
  step_type: string;
  message: string;
  timestamp: string;
}

export default function Home() {
  const [goal, setGoal] = useState("");
  const [status, setStatus] = useState("idle");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  
  const ws = useRef<WebSocket | null>(null);
  const logsEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // Connect to WebSocket backend
    const socket = new WebSocket("ws://localhost:8000/ws");
    ws.current = socket;

    socket.onopen = () => {
      console.log("WebSocket connected");
      setWsConnected(true);
    };

    socket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      console.log("WS received:", data);

      if (data.type === "status") {
        setStatus(data.status);
        if (data.run_id) {
          setRunId(data.run_id);
        }
      } else if (data.type === "log") {
        const newLog: LogEntry = {
          step: data.step,
          step_type: data.step_type,
          message: data.message,
          timestamp: new Date().toLocaleTimeString(),
        };
        setLogs((prev) => [...prev, newLog]);
      } else if (data.type === "result") {
        setStatus("completed");
        // We'll show the result data in a formatted way later
      } else if (data.type === "error") {
        setStatus("error");
        alert(data.message);
      }
    };

    socket.onclose = () => {
      console.log("WebSocket disconnected");
      setWsConnected(false);
    };

    return () => {
      socket.close();
    };
  }, []);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  const handleStart = () => {
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) {
      alert("WebSocket is not connected. Make sure the backend is running.");
      return;
    }
    if (!goal.trim()) {
      alert("Please enter a goal first.");
      return;
    }
    setLogs([]);
    setRunId(null);
    ws.current.send(
      JSON.stringify({
        type: "start",
        goal: goal,
      })
    );
  };

  const handleStop = () => {
    if (!ws.current || ws.current.readyState !== WebSocket.OPEN) return;
    ws.current.send(
      JSON.stringify({
        type: "stop",
      })
    );
  };

  return (
    <main className={styles.main}>
      <div className={styles.header}>
        <h1>Minerva AI Browser Agent</h1>
        <div className={styles.wsBadge}>
          Connection: {wsConnected ? <span style={{ color: "lightgreen" }}>Connected</span> : <span style={{ color: "red" }}>Disconnected</span>}
        </div>
      </div>

      <div className={styles.consoleGrid}>
        {/* Left Console */}
        <div className={styles.leftPane}>
          <div className={styles.card}>
            <h2>Goal Input</h2>
            <textarea
              className={styles.inputGoal}
              placeholder="E.g., Research SaaS tools pricing..."
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              disabled={status === "running"}
            />
            <div className={styles.controls}>
              {status === "running" ? (
                <button className={styles.btnDanger} onClick={handleStop}>
                  Stop Agent
                </button>
              ) : (
                <button className={styles.btnPrimary} onClick={handleStart} disabled={!wsConnected}>
                  Run Agent
                </button>
              )}
            </div>
            {runId && <p style={{ fontSize: "12px", color: "#888" }}>Run ID: {runId}</p>}
          </div>

          <div className={styles.card}>
            <h2>Reasoning Feed</h2>
            <div className={styles.logsContainer}>
              {logs.map((log, idx) => (
                <div key={idx} className={`${styles.logEntry} ${styles[log.step_type]}`}>
                  <span className={styles.logTime}>[{log.timestamp}]</span>
                  <span className={styles.logStep}>Step {log.step} ({log.step_type.toUpperCase()}):</span>
                  <span className={styles.logMsg}>{log.message}</span>
                </div>
              ))}
              <div ref={logsEndRef} />
            </div>
          </div>
        </div>

        {/* Right Pane (Live View placeholder for now) */}
        <div className={styles.rightPane}>
          <div className={styles.card} style={{ height: "100%" }}>
            <h2>Live Browser View</h2>
            <div className={styles.browserContainer}>
              <div className={styles.browserMockScreen}>
                <p>Status: {status.toUpperCase()}</p>
                <p style={{ color: "#666" }}>Live screenshot stream will render here.</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
