"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Bot, Check, CircleAlert, Database, ExternalLink, FileClock, Layers3, LoaderCircle, MessageSquarePlus, Plus, Send, Trash2, Workflow, X } from "lucide-react";
import type { ResearchContext } from "./research-context";

const API = "";
const STORAGE_KEY = "quantweave-ai-sessions";
type Source = { id: string; name: string };
type Message = { id: string; role: "user" | "assistant"; content: string; sources?: Source[] };
type Session = { id: string; title: string; updatedAt: string; messages: Message[] };
type AiTool = { id: string; name: string; kind: "local" | "external"; available: boolean; auth_configured?: boolean };
type AiStatus = { provider: string; base_url: string; model: string; api_key_configured: boolean; status: string; detail: string; tools: AiTool[] };

const welcome = (): Message => ({ id: crypto.randomUUID(), role: "assistant", content: "我是量构研究助手。你可以直接描述看盘想法、询问某个因子，或让我检查已经保存的工作流和回测记录。" });
const newSession = (): Session => ({ id: crypto.randomUUID(), title: "新研究对话", updatedAt: new Date().toISOString(), messages: [welcome()] });
function loadSessions(): Session[] { if (typeof window === "undefined") return []; try { const rows = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"); return Array.isArray(rows) ? rows.slice(0, 30) : []; } catch { return []; } }
const providerName = (provider?: string) => provider === "ollama" ? "Ollama" : provider === "deepseek" ? "DeepSeek" : provider?.startsWith("openai") ? "OpenAI 兼容" : provider || "未配置";
function ToolIcon({ id, kind }: { id: string; kind: string }) { if (kind === "external") return <ExternalLink/>; if (id === "market_database") return <Database/>; if (id === "workflow_catalog") return <Workflow/>; if (id === "backtest_runs") return <FileClock/>; return <Layers3/>; }

type AiWorkspaceProps = { embedded?: boolean; context?: ResearchContext; onClose?: () => void };

export default function AiWorkspace({ embedded = false, context, onClose }: AiWorkspaceProps) {
  const [sessions, setSessions] = useState<Session[]>([]), [activeId, setActiveId] = useState(""), [status, setStatus] = useState<AiStatus | null>(null);
  const [selectedTools, setSelectedTools] = useState<string[]>(["market_database", "workflow_catalog", "backtest_runs", "factor_catalog"]);
  const [draft, setDraft] = useState(""), [sending, setSending] = useState(false), [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    queueMicrotask(() => {
      const saved = loadSessions(), initial = saved.length ? saved : [newSession()];
      setSessions(initial);
      setActiveId(initial[0].id);
    });
    fetch(`${API}/api/v1/ai/status`, { cache: "no-store" }).then(async response => { const payload = await response.json(); if (!response.ok) throw new Error(payload.detail || "AI 状态不可用"); setStatus(payload); }).catch(reason => setError(reason instanceof Error ? reason.message : "AI 状态不可用"));
  }, []);
  useEffect(() => { if (sessions.length) localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions.slice(0, 30))); }, [sessions]);
  const active = useMemo(() => sessions.find(item => item.id === activeId) || sessions[0], [sessions, activeId]);
  useEffect(() => { bottomRef.current?.scrollIntoView({ block: "end" }); }, [active?.messages, sending]);
  const createSession = () => { const session = newSession(); setSessions(current => [session, ...current]); setActiveId(session.id); setDraft(""); setError(""); };
  const removeSession = (id: string) => { const next = sessions.filter(item => item.id !== id), rows = next.length ? next : [newSession()]; setSessions(rows); if (activeId === id) setActiveId(rows[0].id); };
  const updateActive = (updater: (session: Session) => Session) => setSessions(current => current.map(item => item.id === activeId ? updater(item) : item));
  const toggleTool = (id: string) => setSelectedTools(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id]);
  const send = async (text = draft) => {
    const content = text.trim(); if (!content || sending || !active) return;
    const user: Message = { id: crypto.randomUUID(), role: "user", content };
    const baseMessages = active.messages.at(-1)?.role === "user" ? active.messages.slice(0, -1) : active.messages;
    const conversation = [...baseMessages, user];
    updateActive(session => ({ ...session, title: session.messages.length <= 1 ? content.slice(0, 24) : session.title, updatedAt: new Date().toISOString(), messages: conversation })); setDraft(""); setSending(true); setError("");
    try {
      const response = await fetch(`${API}/api/v1/ai/chat`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ messages: conversation.slice(1).map(({ role, content: value }) => ({ role, content: value })), tools: selectedTools, context: context || null }) });
      const payload = await response.json(); if (!response.ok) throw new Error(payload.detail || "AI 暂时无法回答");
      const assistant: Message = { id: crypto.randomUUID(), role: "assistant", content: payload.answer, sources: payload.sources || [] };
      updateActive(session => ({ ...session, updatedAt: new Date().toISOString(), messages: [...session.messages, assistant] }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "AI 暂时无法回答"); } finally { setSending(false); }
  };
  const suggestions = context?.suggestions?.length ? context.suggestions : ["把“放量突破20日均线”拆成可回测节点", "解释 Rank IC、ICIR 和五分组收益的区别", "检查最近一次回测最需要关注的风险", "设计一个不使用未来数据的动量轮动流程"];
  return <section className={`ai-workspace ${embedded ? "embedded" : ""}`}>
    <aside className="ai-session-list"><header><div><Bot/><span><b>研究对话</b><small>{sessions.length} 个本地会话</small></span></div><button onClick={createSession} title="新建对话"><Plus/></button></header><button className="ai-new-chat" onClick={createSession}><MessageSquarePlus/>新建对话</button><div className="ai-sessions">{sessions.map(session => <button key={session.id} className={session.id === activeId ? "active" : ""} onClick={() => setActiveId(session.id)}><span><b>{session.title}</b><small>{new Date(session.updatedAt).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</small></span><i role="button" tabIndex={0} onClick={event => { event.stopPropagation(); removeSession(session.id); }} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); removeSession(session.id); } }} title="删除对话"><Trash2/></i></button>)}</div><footer><span className={`ai-provider-dot ${status?.status || "loading"}`}/><div><b>{providerName(status?.provider)}</b><small>{status?.model || "等待配置"}</small></div></footer></aside>
    <main className={`ai-chat-panel ${context ? "has-context" : ""}`}><header><div><Bot/><span><b>{active?.title || "新研究对话"}</b><small>{providerName(status?.provider)} · {status?.model || "等待模型"}</small></span></div><strong className={status?.status || "loading"}>{status?.status === "online" || status?.status === "configured" ? <><Check/>可提问</> : <><CircleAlert/>待接入</>}</strong>{embedded && onClose && <button className="ai-embedded-close" onClick={onClose} title="收起 AI 助手"><X/></button>}</header>{context && <div className="ai-context-banner"><span>当前页面</span><b>{context.title}</b><small>{context.summary}</small>{context.details?.length ? <em>{context.details.join(" · ")}</em> : null}</div>}<div className="ai-messages">{(active?.messages || []).map(message => <article key={message.id} className={message.role}><div className="ai-avatar">{message.role === "assistant" ? <Bot/> : "你"}</div><div className="ai-bubble"><p>{message.content}</p>{message.sources?.length ? <div className="ai-sources"><span>本次引用</span>{message.sources.map(source => <em key={source.id}>{source.name}</em>)}</div> : null}</div></article>)}{active?.messages.length === 1 && <section className="ai-suggestions">{suggestions.map(item => <button key={item} onClick={() => void send(item)}>{item}<Send/></button>)}</section>}{sending && <article className="assistant"><div className="ai-avatar"><Bot/></div><div className="ai-bubble loading"><LoaderCircle/>正在读取所选数据并生成回答</div></article>}<div ref={bottomRef}/></div>{error && <div className="ai-error"><CircleAlert/><span><b>当前无法生成回答</b>{error}</span></div>}<div className="ai-composer"><textarea value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} placeholder={context ? `围绕“${context.title}”提问…` : "描述你的选股、因子或回测问题…"}/><button onClick={() => void send()} disabled={!draft.trim() || sending} title="发送"><Send/></button><small>Enter 发送 · Shift + Enter 换行 · 仅用于研究，不构成投资建议</small></div></main>
    <aside className="ai-context-panel"><header><span>CONTEXT TOOLS</span><h2>回答数据</h2></header><div className="ai-provider-card"><div><Bot/><span><b>{providerName(status?.provider)}</b><small>{status?.base_url || "等待 API 地址"}</small></span></div><p>{status?.detail || "正在检查模型服务"}</p></div><div className="ai-tool-list"><h3>本次允许读取</h3>{(status?.tools || []).map(tool => <label key={tool.id} className={!tool.available || tool.auth_configured === false ? "disabled" : ""}><input type="checkbox" checked={selectedTools.includes(tool.id)} disabled={!tool.available || tool.auth_configured === false} onChange={() => toggleTool(tool.id)}/><ToolIcon id={tool.id} kind={tool.kind}/><span><b>{tool.name}</b><small>{tool.kind === "external" ? tool.auth_configured === false ? "等待数据密钥" : "外部只读接口" : "本地只读"}</small></span></label>)}</div><div className="ai-boundary"><CircleAlert/><span><b>研究边界</b><small>模型不能下单；外部数据必须由后端白名单配置；回答需复核数据日期与回测口径。</small></span></div></aside>
  </section>;
}
