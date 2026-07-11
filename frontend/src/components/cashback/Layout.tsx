// @ts-nocheck
// Layout / AppShell / Sidebar / TopBar — ported from Cashback2.zip
/* eslint-disable */
import React, { useState, useContext, createContext } from "react";
import { USERS, ROLE_LABELS, PERMISSIONS } from "@/data/mockData";

// ── App Context ──────────────────────────────────────────────────────────────
export const AppContext = createContext<any>(null);
export function useApp() { return useContext(AppContext); }

// ── SVG Icons ─────────────────────────────────────────────────────────────────
function NavIconDashboard({ active }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <rect x="1" y="1" width="7" height="7" rx="1.5" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
      <rect x="10" y="1" width="7" height="7" rx="1.5" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
      <rect x="1" y="10" width="7" height="7" rx="1.5" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
      <rect x="10" y="10" width="7" height="7" rx="1.5" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
    </svg>
  );
}
function NavIconCampaigns({ active }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <path d="M2 4h14M2 9h10M2 14h7" stroke={active ? "oklch(0.65 0.18 230)" : "currentColor"} strokeWidth="1.8" strokeLinecap="round" opacity={active ? 1 : 0.5}/>
      <circle cx="14" cy="13" r="3.5" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
      <path d="M13 13l.8.8 1.4-1.4" stroke="white" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
function NavIconAnalytics({ active }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <rect x="2" y="10" width="3" height="6" rx="1" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
      <rect x="7.5" y="6" width="3" height="10" rx="1" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
      <rect x="13" y="2" width="3" height="14" rx="1" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
    </svg>
  );
}
function NavIconExplanations({ active }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <circle cx="9" cy="9" r="7" stroke={active ? "#fff" : "currentColor"} strokeWidth="1.5" opacity={active ? 1 : 0.6}/>
      <path d="M5 11 L7 8 L10 10 L13 6" stroke={active ? "#fff" : "currentColor"} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" opacity={active ? 1 : 0.8}/>
      <circle cx="7" cy="8" r="1.3" fill={active ? "#fff" : "currentColor"}/>
      <circle cx="13" cy="6" r="1.3" fill={active ? "#fff" : "currentColor"}/>
    </svg>
  );
}
function NavIconMlLimits({ active }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <path d="M9 1.5 L15 4 V9 C15 12.5 12.5 15 9 16.5 C5.5 15 3 12.5 3 9 V4 Z" stroke={active ? "#fff" : "currentColor"} strokeWidth="1.5" fill={active ? "rgba(255,255,255,0.15)" : "none"} opacity={active ? 1 : 0.6}/>
      <path d="M6.5 9 L8.5 11 L11.5 7" stroke={active ? "#fff" : "currentColor"} strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}
function NavIconUsers({ active }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <circle cx="7" cy="6" r="3" fill={active ? "oklch(0.65 0.18 230)" : "currentColor"} opacity={active ? 1 : 0.5}/>
      <path d="M1 15c0-3.314 2.686-5 6-5s6 1.686 6 5" stroke={active ? "oklch(0.65 0.18 230)" : "currentColor"} strokeWidth="1.6" strokeLinecap="round" opacity={active ? 1 : 0.5}/>
      <path d="M13 8c1.657 0 3 .9 3 2.5V15" stroke={active ? "oklch(0.65 0.18 230)" : "currentColor"} strokeWidth="1.6" strokeLinecap="round" opacity={active ? 1 : 0.5}/>
      <path d="M11 4.5a2.5 2.5 0 0 1 0-4" stroke={active ? "oklch(0.65 0.18 230)" : "currentColor"} strokeWidth="1.5" strokeLinecap="round" opacity={active ? 1 : 0.5}/>
    </svg>
  );
}

function NavIconExperiments({ active }) {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
      <path d="M9 2v6M9 8l-4.5 7a1 1 0 0 0 .9 1.5h7.2a1 1 0 0 0 .9-1.5L9 8z"
            stroke={active ? "#fff" : "currentColor"} strokeWidth="1.5"
            strokeLinecap="round" strokeLinejoin="round" opacity={active ? 1 : 0.6}/>
      <circle cx="7.5" cy="12.5" r="1" fill={active ? "#fff" : "currentColor"}/>
      <circle cx="10.5" cy="14" r="0.8" fill={active ? "#fff" : "currentColor"}/>
    </svg>
  );
}

// ── Nav items by role ─────────────────────────────────────────────────────────
const NAV_ITEMS = [
  { id: "dashboard",      label: "Дашборд",        icon: NavIconDashboard,    perm: "dashboard" },
  { id: "campaigns",      label: "Кампании",        icon: NavIconCampaigns,    perm: "campaigns_view" },
  { id: "analytics",      label: "Аналитика",       icon: NavIconAnalytics,    perm: "analytics" },
  { id: "experiments",    label: "Эксперименты",    icon: NavIconExperiments,  perm: "analytics" },
  { id: "explanations",   label: "ML-объяснения",   icon: NavIconExplanations, perm: "analytics" },
  { id: "ml_limits",      label: "ML-лимиты",       icon: NavIconMlLimits,     perm: "users" },
  { id: "users",          label: "Пользователи",    icon: NavIconUsers,        perm: "users" },
];

// ── Sidebar ───────────────────────────────────────────────────────────────────
export function Sidebar({ currentPage, onNavigate, currentUser, campaigns }) {
  const perms = PERMISSIONS[currentUser.role];
  const activeCampaignsCount = (campaigns || []).filter((c: any) => c.status === "active").length;

  return (
    <aside style={{
      width: 250, minWidth: 250,
      background: "linear-gradient(180deg, #0d1929 0%, #0f2040 100%)",
      display: "flex", flexDirection: "column",
      borderRight: "1px solid rgba(255,255,255,0.06)",
      height: "100vh", position: "sticky", top: 0,
      userSelect: "none",
    }}>
      {/* Logo */}
      <div style={{ padding: "24px 20px 20px", borderBottom: "1px solid rgba(255,255,255,0.07)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{
            width: 36, height: 36, borderRadius: 10,
            background: "oklch(0.65 0.18 230)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
              <path d="M3 10C3 6.686 6.134 4 10 4s7 2.686 7 6-3.134 6-7 6H3" stroke="white" strokeWidth="1.8" strokeLinecap="round"/>
              <path d="M7 10h6M10 7v6" stroke="white" strokeWidth="1.8" strokeLinecap="round"/>
            </svg>
          </div>
          <div>
            <div style={{ color: "white", fontWeight: 700, fontSize: 15, letterSpacing: "-0.3px" }}>CashBack</div>
            <div style={{ color: "rgba(255,255,255,0.4)", fontSize: 11 }}>Admin Platform</div>
          </div>
        </div>
      </div>

      {/* Nav */}
      <nav style={{ flex: 1, padding: "12px 10px", display: "flex", flexDirection: "column", gap: 2 }}>
        {NAV_ITEMS.map(item => {
          if (!perms[item.perm]) return null;
          const active = currentPage === item.id;
          const Icon = item.icon;
          return (
            <button key={item.id} onClick={() => onNavigate(item.id)} style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "9px 12px", borderRadius: 8,
              background: active ? "rgba(99,179,237,0.12)" : "transparent",
              border: active ? "1px solid rgba(99,179,237,0.2)" : "1px solid transparent",
              color: active ? "white" : "rgba(255,255,255,0.55)",
              cursor: "pointer", textAlign: "left", fontSize: 14,
              fontWeight: active ? 600 : 400,
              transition: "all 0.15s",
              width: "100%",
            }}
            onMouseEnter={e => { if (!active) (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.06)"; }}
            onMouseLeave={e => { if (!active) (e.currentTarget as HTMLButtonElement).style.background = "transparent"; }}
            >
              <Icon active={active} />
              <span>{item.label}</span>
              {item.id === "campaigns" && (
                <span style={{
                  marginLeft: "auto", fontSize: 11, fontWeight: 600,
                  background: "oklch(0.65 0.18 230)", color: "white",
                  borderRadius: 10, padding: "1px 7px",
                }}>
                   {activeCampaignsCount}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {/* User profile */}
      <div style={{
        padding: "14px 16px", borderTop: "1px solid rgba(255,255,255,0.07)",
        display: "flex", alignItems: "center", gap: 10,
      }}>
        <div style={{
          width: 34, height: 34, borderRadius: "50%",
          background: "oklch(0.65 0.18 230)",
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 12, fontWeight: 700, color: "white", flexShrink: 0,
        }}>{currentUser.avatar}</div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ color: "white", fontSize: 13, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{currentUser.name}</div>
          <div style={{ color: "rgba(255,255,255,0.4)", fontSize: 11 }}>{ROLE_LABELS[currentUser.role]}</div>
        </div>
        <div style={{ width: 8, height: 8, borderRadius: "50%", background: "oklch(0.65 0.18 160)", flexShrink: 0 }}></div>
      </div>
    </aside>
  );
}

// ── TopBar ─────────────────────────────────────────────────────────────────────
export function TopBar({ title, subtitle, action, currentUser, onUserSwitch, onLogout }) {
  const [showMenu, setShowMenu] = useState(false);
  return (
    <header style={{
      height: 60, background: "white",
      borderBottom: "1px solid #e8edf4",
      display: "flex", alignItems: "center",
      padding: "0 28px", gap: 16, flexShrink: 0,
      position: "sticky", top: 0, zIndex: 10,
    }}>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 17, fontWeight: 700, color: "#0d1929", letterSpacing: "-0.3px" }}>{title}</div>
        {subtitle && <div style={{ fontSize: 12, color: "#8896a8", marginTop: 1 }}>{subtitle}</div>}
      </div>
      {action}
      {/* Role switcher (demo) */}
      <div style={{ position: "relative" }}>
        <button onClick={() => setShowMenu(v => !v)} style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "6px 12px", borderRadius: 8,
          border: "1px solid #e2e8f0", background: "white",
          cursor: "pointer", fontSize: 13, color: "#374151",
        }}>
          <div style={{
            width: 28, height: 28, borderRadius: "50%",
            background: "oklch(0.65 0.18 230)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 11, fontWeight: 700, color: "white",
          }}>{currentUser.avatar}</div>
          <span style={{ fontWeight: 600 }}>{currentUser.name.split(" ")[0]}</span>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M3 4.5l3 3 3-3" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round"/></svg>
        </button>
        {showMenu && onLogout && (
          <div style={{
            position: "absolute", right: 0, top: "calc(100% + 6px)",
            background: "white", borderRadius: 10, border: "1px solid #e2e8f0",
            boxShadow: "0 8px 24px rgba(0,0,0,0.12)", minWidth: 220, zIndex: 100,
            overflow: "hidden",
          }}>
            <div style={{ padding: "10px 12px", borderBottom: "1px solid #f1f5f9" }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: "#0d1929" }}>{currentUser.name}</div>
              <div style={{ fontSize: 11, color: "#8896a8" }}>{currentUser.email} · {ROLE_LABELS[currentUser.role]}</div>
            </div>
            <button onClick={() => { setShowMenu(false); onLogout(); }} style={{
              display: "flex", alignItems: "center", gap: 8,
              width: "100%", padding: "10px 12px",
              background: "white", border: "none", cursor: "pointer",
              textAlign: "left", fontSize: 13, fontWeight: 600, color: "#dc2626",
              fontFamily: "inherit",
            }}
            onMouseEnter={e => { e.currentTarget.style.background = "#fef2f2"; }}
            onMouseLeave={e => { e.currentTarget.style.background = "white"; }}>
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M5 2H3a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h2M9 10l3-3-3-3M12 7H5" stroke="#dc2626" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Выйти
            </button>
          </div>
        )}
        {showMenu && !onLogout && (
          <div style={{
            position: "absolute", right: 0, top: "calc(100% + 6px)",
            background: "white", borderRadius: 10, border: "1px solid #e2e8f0",
            boxShadow: "0 8px 24px rgba(0,0,0,0.12)", minWidth: 220, zIndex: 100,
            overflow: "hidden",
          }}>
            <div style={{ padding: "8px 12px", borderBottom: "1px solid #f1f5f9", fontSize: 11, color: "#8896a8", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px" }}>Сменить роль (демо)</div>
            {USERS.map(u => (
              <button key={u.id} onClick={() => { onUserSwitch(u); setShowMenu(false); }} style={{
                display: "flex", alignItems: "center", gap: 10,
                width: "100%", padding: "9px 12px",
                background: u.id === currentUser.id ? "#f0f7ff" : "white",
                border: "none", cursor: "pointer", textAlign: "left",
                borderLeft: u.id === currentUser.id ? "3px solid oklch(0.65 0.18 230)" : "3px solid transparent",
              }}>
                <div style={{
                  width: 28, height: 28, borderRadius: "50%",
                  background: u.id === currentUser.id ? "oklch(0.65 0.18 230)" : "#e2e8f0",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 11, fontWeight: 700,
                  color: u.id === currentUser.id ? "white" : "#64748b",
                }}>{u.avatar}</div>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: "#0d1929" }}>{u.name}</div>
                  <div style={{ fontSize: 11, color: "#8896a8" }}>{ROLE_LABELS[u.role]}</div>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </header>
  );
}

// ── Main Layout Shell ─────────────────────────────────────────────────────────
export function AppShell({ children, currentPage, onNavigate, topBarProps, currentUser, onUserSwitch, onLogout, campaigns }) {
  return (
    <div style={{ display: "flex", height: "100vh", fontFamily: "'Inter', sans-serif", background: "#f1f5f9" }}>
      <Sidebar currentPage={currentPage} onNavigate={onNavigate} currentUser={currentUser} campaigns={campaigns} />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <TopBar {...topBarProps} currentUser={currentUser} onUserSwitch={onUserSwitch} onLogout={onLogout} />
        <main style={{ flex: 1, overflow: "auto", padding: "28px" }}>
          {children}
        </main>
      </div>
    </div>
  );
}
