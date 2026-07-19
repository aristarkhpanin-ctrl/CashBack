// @ts-nocheck
/* eslint-disable */
import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
import * as Recharts from "recharts";
import {
  USERS, ROLE_LABELS, PERMISSIONS, MCC_CATEGORIES, SEGMENTS,
  CAMPAIGNS, generateTrendData, MCC_HEATMAP, FUNNEL_DATA,
  MATRIX_DATA, CHANNEL_DATA, computeActivityMatrix,
} from "@/data/mockData";
import {
  StatusBadge, Card, StatCard, ProgressBar, Table,
  Button, Input, Select, SectionHeader, Tabs, Modal, Toast,
  STATUS_CONFIG,
} from "../UI";
import { Icon } from "../Icon";

const AppData = {
  USERS, ROLE_LABELS, PERMISSIONS, MCC_CATEGORIES, SEGMENTS,
  CAMPAIGNS, generateTrendData, MCC_HEATMAP, FUNNEL_DATA,
  MATRIX_DATA, CHANNEL_DATA, computeActivityMatrix,
};



const PERMISSION_LABELS = {
  dashboard:          "Дашборд — просмотр",
  campaigns_view:     "Кампании — просмотр",
  campaigns_create:   "Кампании — создание",
  campaigns_edit:     "Кампании — редактирование",
  campaigns_delete:   "Кампании — удаление",
  analytics:          "Аналитика — просмотр",
  users:              "Пользователи — управление",
};

const PERMISSION_GROUPS = [
  { label: "Дашборд", keys: ["dashboard"] },
  { label: "Кампании", keys: ["campaigns_view", "campaigns_create", "campaigns_edit", "campaigns_delete"] },
  { label: "Аналитика", keys: ["analytics"] },
  { label: "Система", keys: ["users"] },
];

function Users({ currentUser, liveUsers }) {
  const { USERS: initialUsers, ROLE_LABELS, PERMISSIONS: initialPerms } = AppData;
  const [localUsers, setLocalUsers] = useState(initialUsers);
  const [permissions, setPermissions] = useState({ ...initialPerms });
  const [activeTab, setActiveTab] = useState("users");
  const [showAddModal, setShowAddModal] = useState(false);
  const [editUser, setEditUser] = useState(null);
  const [toast, setToast] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null);

  // live-режим (фаза 15): ростер из /auth/users, мутации через API
  const isLive = !!liveUsers?.enabled;
  const users = isLive ? liveUsers.users : localUsers;

  // Фаза 26: матрица прав — live из /roles/permissions, иначе локальный мок.
  const livePerms = liveUsers?.perms;
  const permissionsView = (isLive && livePerms?.matrix) ? livePerms.matrix : permissions;

  async function handleRoleChange(userId, newRole) {
    if (isLive) {
      const ok = await liveUsers.changeRole(userId, newRole);
      if (!ok) return;
    } else {
      setLocalUsers(us => us.map(u => u.id === userId ? { ...u, role: newRole } : u));
    }
    setToast({ msg: "Роль пользователя обновлена", type: "success" });
  }

  async function handlePermToggle(role, key) {
    const next = !permissionsView[role]?.[key];
    if (isLive && livePerms) {
      const ok = await livePerms.toggle(role, key, next);   // PATCH /roles/:role/permissions
      if (!ok) return;
      setToast({ msg: "Права роли обновлены", type: "success" });
    } else {
      setPermissions(prev => ({
        ...prev,
        [role]: { ...prev[role], [key]: next },
      }));
    }
  }

  async function handleSaveUser(data) {
    const isEdit = !!data.id;
    if (isLive) {
      const ok = await liveUsers.save(data, isEdit);
      if (!ok) return;
    } else if (isEdit) {
      setLocalUsers(us => us.map(u => u.id === data.id ? data : u));
    } else {
      setLocalUsers(us => [...us, { ...data, id: Date.now(), lastLogin: "Ещё не входил" }]);
    }
    setToast({
      msg: isEdit ? "Данные пользователя обновлены" : "Пользователь добавлен",
      type: "success",
    });
    setShowAddModal(false);
    setEditUser(null);
  }

  async function handleDelete(id) {
    if (isLive) {
      // Удаления в live нет — деактивация сохраняет аудит-след.
      const ok = await liveUsers.deactivate(id);
      if (!ok) return;
      setToast({ msg: "Пользователь деактивирован", type: "info" });
    } else {
      setLocalUsers(us => us.filter(u => u.id !== id));
      setToast({ msg: "Пользователь удалён", type: "info" });
    }
    setConfirmDelete(null);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24, maxWidth: 1100 }}>
      <Tabs
        tabs={[
          { id: "users", label: "Пользователи" },
          { id: "permissions", label: "Матрица прав" },
        ]}
        active={activeTab}
        onChange={setActiveTab}
      />

      {activeTab === "users" && (
        <UsersTab
          users={users}
          currentUser={currentUser}
          onRoleChange={handleRoleChange}
          onEdit={u => { setEditUser(u); setShowAddModal(true); }}
          onDelete={id => setConfirmDelete(id)}
          onAdd={() => { setEditUser(null); setShowAddModal(true); }}
        />
      )}

      {activeTab === "permissions" && (
        <PermissionsMatrix
          permissions={permissionsView}
          onToggle={handlePermToggle}
        />
      )}

      {showAddModal && (
        <UserModal
          user={editUser}
          needPassword={isLive && !editUser}
          onSave={handleSaveUser}
          onClose={() => { setShowAddModal(false); setEditUser(null); }}
        />
      )}

      {confirmDelete && (
        <Modal open title={isLive ? "Подтвердите деактивацию" : "Подтвердите удаление"} onClose={() => setConfirmDelete(null)} width={420}>
          <div style={{ fontSize: 14, color: "#374151", marginBottom: 20 }}>
            {isLive ? (
              <>Деактивировать пользователя <strong>{users.find(u => u.id === confirmDelete)?.name}</strong>?
              Он потеряет доступ, но останется в журнале аудита.</>
            ) : (
              <>Вы уверены, что хотите удалить пользователя <strong>{users.find(u => u.id === confirmDelete)?.name}</strong>?
              Это действие необратимо.</>
            )}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <Button variant="danger" style={{ flex: 1 }} onClick={() => handleDelete(confirmDelete)}>
              {isLive ? "Деактивировать" : "Удалить"}
            </Button>
            <Button variant="secondary" style={{ flex: 1 }} onClick={() => setConfirmDelete(null)}>Отмена</Button>
          </div>
        </Modal>
      )}

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}

// ── Users Tab ─────────────────────────────────────────────────────────────────
function UsersTab({ users, currentUser, onRoleChange, onEdit, onDelete, onAdd }) {
  const { ROLE_LABELS } = AppData;
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("all");

  const filtered = users.filter(u => {
    const matchSearch = !search || u.name.toLowerCase().includes(search.toLowerCase()) || u.email.toLowerCase().includes(search.toLowerCase());
    const matchRole = roleFilter === "all" || u.role === roleFilter;
    return matchSearch && matchRole;
  });

  const roleColors = {
    admin:    { bg: "#ede9fe", color: "#7c3aed" },
    marketer: { bg: "#dbeafe", color: "#1d4ed8" },
    analyst:  { bg: "#dcfce7", color: "#15803d" },
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <div style={{ flex: 1, position: "relative" }}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
            <circle cx="6" cy="6" r="4.5" stroke="#94a3b8" strokeWidth="1.5"/>
            <path d="M9.5 9.5l2.5 2.5" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Поиск по имени или email…" style={{
            width: "100%", padding: "9px 12px 9px 32px", border: "1px solid #e2e8f0",
            borderRadius: 8, fontSize: 13, fontFamily: "inherit", outline: "none",
            background: "white", boxSizing: "border-box", color: "#0d1929",
          }} />
        </div>
        <div style={{ display: "flex", gap: 2, background: "white", borderRadius: 8, padding: 3, border: "1px solid #e2e8f0" }}>
          {[{ v: "all", l: "Все" }, { v: "admin", l: "Администраторы" }, { v: "marketer", l: "Маркетологи" }, { v: "analyst", l: "Аналитики" }].map(f => (
            <button key={f.v} onClick={() => setRoleFilter(f.v)} style={{
              padding: "6px 12px", borderRadius: 6, fontSize: 12, fontWeight: 600,
              border: "none", cursor: "pointer", transition: "all 0.15s",
              background: roleFilter === f.v ? "oklch(0.55 0.18 230)" : "transparent",
              color: roleFilter === f.v ? "white" : "#64748b",
            }}>{f.l}</button>
          ))}
        </div>
        <Button variant="primary" onClick={onAdd}>
          <span style={{ fontSize: 16 }}>+</span> Добавить
        </Button>
      </div>

      {/* Stats row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
        {[
          { label: "Всего", count: users.length, color: "#374151" },
          { label: "Администраторов", count: users.filter(u => u.role === "admin").length, color: "#7c3aed" },
          { label: "Маркетологов", count: users.filter(u => u.role === "marketer").length, color: "#1d4ed8" },
          { label: "Аналитиков", count: users.filter(u => u.role === "analyst").length, color: "#15803d" },
        ].map(s => (
          <div key={s.label} style={{ background: "white", borderRadius: 10, padding: "14px 18px", border: "1px solid #e8edf4" }}>
            <div style={{ fontSize: 11, color: "#8896a8", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.5px" }}>{s.label}</div>
            <div style={{ fontSize: 26, fontWeight: 700, color: s.color, fontFamily: "'JetBrains Mono', monospace", marginTop: 4 }}>{s.count}</div>
          </div>
        ))}
      </div>

      {/* User table */}
      <Card style={{ padding: 0, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #f1f5f9", background: "#fafbfc" }}>
              {["Пользователь", "Email", "Роль", "Последний вход", ""].map((h, i) => (
                <th key={i} style={{ textAlign: "left", padding: "11px 18px", fontSize: 11, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map(u => {
              const rc = roleColors[u.role];
              const isSelf = u.id === currentUser.id;
              return (
                <tr key={u.id} style={{ borderBottom: "1px solid #f8fafc", transition: "background 0.1s" }}
                  onMouseEnter={e => e.currentTarget.style.background = "#fafbfc"}
                  onMouseLeave={e => e.currentTarget.style.background = ""}>
                  <td style={{ padding: "14px 18px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div style={{
                        width: 36, height: 36, borderRadius: "50%",
                        background: isSelf ? "oklch(0.55 0.18 230)" : "#e8edf4",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        fontSize: 12, fontWeight: 700, color: isSelf ? "white" : "#64748b", flexShrink: 0,
                      }}>{u.avatar}</div>
                      <div>
                        <div style={{ fontWeight: 700, color: u.isActive === false ? "#94a3b8" : "#0d1929" }}>
                          {u.name}
                          {isSelf && <span style={{ marginLeft: 6, fontSize: 10, background: "#f0f7ff", color: "oklch(0.45 0.18 230)", padding: "2px 6px", borderRadius: 10, fontWeight: 600 }}>Вы</span>}
                          {u.isActive === false && <span style={{ marginLeft: 6, fontSize: 10, background: "#fef2f2", color: "#dc2626", padding: "2px 6px", borderRadius: 10, fontWeight: 600 }}>деактивирован</span>}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td style={{ padding: "14px 18px", color: "#64748b" }}>{u.email}</td>
                  <td style={{ padding: "14px 18px" }}>
                    {isSelf ? (
                      <span style={{ display: "inline-flex", alignItems: "center", padding: "4px 12px", borderRadius: 20, fontSize: 12, fontWeight: 600, background: rc.bg, color: rc.color }}>
                        {ROLE_LABELS[u.role]}
                      </span>
                    ) : (
                      <select value={u.role} onChange={e => onRoleChange(u.id, e.target.value)} style={{
                        padding: "5px 10px", borderRadius: 20, fontSize: 12, fontWeight: 600,
                        background: rc.bg, color: rc.color,
                        border: "none", cursor: "pointer", fontFamily: "inherit", outline: "none",
                      }}>
                        <option value="admin">Администратор</option>
                        <option value="marketer">Маркетолог</option>
                        <option value="analyst">Аналитик</option>
                      </select>
                    )}
                  </td>
                  <td style={{ padding: "14px 18px", color: "#94a3b8", fontSize: 12 }}>{u.lastLogin}</td>
                  <td style={{ padding: "14px 18px" }}>
                    <div style={{ display: "flex", gap: 6 }}>
                      <Button variant="ghost" size="sm" onClick={() => onEdit(u)}>
                        <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M9.5 2.5l2 2L4 12H2v-2L9.5 2.5z" stroke="#64748b" strokeWidth="1.4" strokeLinejoin="round"/></svg>
                      </Button>
                      {!isSelf && (
                        <Button variant="ghost" size="sm" onClick={() => onDelete(u.id)}>
                          <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 4h10M5 4V2.5h4V4M5.5 6.5v4M8.5 6.5v4M3 4l.8 7.5h6.4L11 4" stroke="#ef4444" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/></svg>
                        </Button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

// ── Permissions Matrix ────────────────────────────────────────────────────────
function PermissionsMatrix({ permissions, onToggle }) {
  const roles = ["admin", "marketer", "analyst"];
  const { ROLE_LABELS } = AppData;

  const roleColors = { admin: "oklch(0.55 0.18 280)", marketer: "oklch(0.55 0.18 230)", analyst: "oklch(0.55 0.18 160)" };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ background: "#fef9c3", borderRadius: 10, padding: "12px 16px", fontSize: 13, color: "#92400e", display: "flex", gap: 8, alignItems: "flex-start" }}>
        <Icon name="triangle-alert" size={16} color="#92400e" style={{ flexShrink: 0 }} />

        <span>Изменения матрицы прав применяются немедленно. Снятие прав у активных пользователей ограничит их доступ при следующем действии.</span>
      </div>

      <Card style={{ padding: 0, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ background: "#f8fafc", borderBottom: "2px solid #e8edf4" }}>
              <th style={{ textAlign: "left", padding: "14px 20px", fontSize: 12, fontWeight: 700, color: "#374151", width: "40%" }}>Право доступа</th>
              {roles.map(r => (
                <th key={r} style={{ textAlign: "center", padding: "14px 20px", fontSize: 13, fontWeight: 700 }}>
                  <div style={{
                    display: "inline-block", padding: "4px 14px", borderRadius: 20,
                    background: roleColors[r] + "18", color: roleColors[r],
                    fontWeight: 700,
                  }}>{ROLE_LABELS[r]}</div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {PERMISSION_GROUPS.map((group, gi) => (
              <React.Fragment key={gi}>
                <tr>
                  <td colSpan={4} style={{ padding: "10px 20px 6px", background: "#fafbfc", borderBottom: "1px solid #f1f5f9" }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>{group.label}</span>
                  </td>
                </tr>
                {group.keys.map(key => (
                  <tr key={key} style={{ borderBottom: "1px solid #f8fafc" }}
                    onMouseEnter={e => e.currentTarget.style.background = "#fafbfc"}
                    onMouseLeave={e => e.currentTarget.style.background = ""}>
                    <td style={{ padding: "13px 20px", fontSize: 13, color: "#374151" }}>{PERMISSION_LABELS[key]}</td>
                    {roles.map(role => {
                      const enabled = permissions[role][key];
                      const isAdmin = role === "admin";
                      return (
                        <td key={role} style={{ textAlign: "center", padding: "13px 20px" }}>
                          <div
                            onClick={() => !isAdmin && onToggle(role, key)}
                            title={isAdmin ? "Администратор всегда имеет полный доступ" : ""}
                            style={{
                              display: "inline-flex", alignItems: "center", justifyContent: "center",
                              width: 28, height: 28, borderRadius: 8,
                              background: enabled ? (isAdmin ? "oklch(0.88 0.06 280)" : "oklch(0.88 0.08 160)") : "#f1f5f9",
                              cursor: isAdmin ? "not-allowed" : "pointer",
                              transition: "all 0.15s",
                              opacity: isAdmin ? 0.7 : 1,
                            }}
                          >
                            {enabled ? (
                              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                                <path d="M2.5 7l3.5 3.5 5.5-6" stroke={isAdmin ? "oklch(0.45 0.18 280)" : "oklch(0.35 0.18 160)"} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                              </svg>
                            ) : (
                              <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                                <path d="M3 3l6 6M9 3l-6 6" stroke="#cbd5e1" strokeWidth="1.8" strokeLinecap="round"/>
                              </svg>
                            )}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

// ── User Modal ────────────────────────────────────────────────────────────────
function UserModal({ user, needPassword, onSave, onClose }) {
  const { ROLE_LABELS } = AppData;
  const [form, setForm] = useState(user || { name: "", email: "", role: "marketer", avatar: "", password: "" });

  const passwordOk = !needPassword || (form.password || "").length >= 6;

  function handleSubmit() {
    if (!form.name || !form.email || !passwordOk) return;
    const initials = form.name.split(" ").slice(0, 2).map(p => p[0]).join("").toUpperCase();
    onSave({ ...form, avatar: initials });
  }

  return (
    <Modal open title={user ? "Редактировать пользователя" : "Добавить пользователя"} onClose={onClose} width={480}>
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Input label="Полное имя" value={form.name} onChange={v => setForm(f => ({ ...f, name: v }))} placeholder="Иван Иванов" required />
        <Input label="Email" type="email" value={form.email} onChange={v => setForm(f => ({ ...f, email: v }))} placeholder="ivan@bank.ru" required />
        {needPassword && (
          <Input label="Пароль (мин. 6 символов)" type="password" value={form.password}
                 onChange={v => setForm(f => ({ ...f, password: v }))} placeholder="••••••••" required />
        )}
        <div>
          <label style={{ fontSize: 12, fontWeight: 600, color: "#374151", display: "block", marginBottom: 8 }}>Роль <span style={{ color: "#ef4444" }}>*</span></label>
          <div style={{ display: "flex", gap: 8 }}>
            {Object.entries(ROLE_LABELS).map(([v, l]) => (
              <div key={v} onClick={() => setForm(f => ({ ...f, role: v }))} style={{
                flex: 1, padding: "10px 12px", borderRadius: 10, textAlign: "center",
                border: form.role === v ? "2px solid oklch(0.65 0.18 230)" : "1px solid #e2e8f0",
                background: form.role === v ? "oklch(0.97 0.03 230)" : "white",
                cursor: "pointer", fontSize: 13, fontWeight: form.role === v ? 700 : 500,
                color: form.role === v ? "oklch(0.45 0.18 230)" : "#374151", transition: "all 0.15s",
              }}>{l}</div>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, marginTop: 8 }}>
          <Button variant="primary" style={{ flex: 1 }} onClick={handleSubmit} disabled={!form.name || !form.email || !passwordOk}>
            {user ? "Сохранить" : "Добавить пользователя"}
          </Button>
          <Button variant="secondary" onClick={onClose}>Отмена</Button>
        </div>
      </div>
    </Modal>
  );
}

export default Users;
