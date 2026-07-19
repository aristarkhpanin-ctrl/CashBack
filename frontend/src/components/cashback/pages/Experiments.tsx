// @ts-nocheck
/* eslint-disable */
// A/B-эксперименты (фаза 16): список из GET /experiments, серверный
// two-proportion z-test из /experiments/{id}/results, start/stop по FSM.
// В демо-режиме — статичные mock-данные, чтобы страница жила без бэкенда.
import React, { useEffect, useMemo, useState } from "react";
import { Card, Button, Input, Modal, Toast } from "../UI";
import { Icon } from "../Icon";
import {
  useExperiments, useExperimentResults, useExperimentOps,
} from "@/shared/api/live";

// ── Демо-данные (backend офлайн) ──────────────────────────────────────────────
const MOCK_EXPERIMENTS = [
  {
    experiment_id: "mock-1",
    name: "LightGBM vs SVD-baseline (ranking)",
    status: "ACTIVE",
    target_metric: "acceptance_rate",
    start_date: "2026-06-20T00:00:00Z",
    end_date: null,
    variants: [
      { variant_id: "m1a", name: "control", traffic_weight: 0.5, strategy_class: "SVDRanker" },
      { variant_id: "m1b", name: "treatment", traffic_weight: 0.5, strategy_class: "LightGBMRanker" },
    ],
  },
  {
    experiment_id: "mock-2",
    name: "OST: вечерняя vs утренняя отправка push",
    status: "DRAFT",
    target_metric: "open_rate",
    start_date: "2026-07-09T00:00:00Z",
    end_date: null,
    variants: [
      { variant_id: "m2a", name: "morning", traffic_weight: 0.5, strategy_class: "MorningSendStrategy" },
      { variant_id: "m2b", name: "evening", traffic_weight: 0.5, strategy_class: "EveningSendStrategy" },
    ],
  },
];

const MOCK_RESULTS = {
  "mock-1": {
    experiment_id: "mock-1",
    target_metric: "acceptance_rate",
    control:   { variant_id: "m1a", name: "control",   n: 1000, successes: 82,  rate: 0.082 },
    treatment: { variant_id: "m1b", name: "treatment", n: 1000, successes: 104, rate: 0.104 },
    diff: 0.022, z: 2.31, p_value: 0.0209,
    confidence_interval: [0.0033, 0.0407],
    significance: "significant",
  },
};

const STATUS_STYLE = {
  ACTIVE:  { label: "Идёт",      bg: "oklch(0.93 0.06 160)", color: "oklch(0.35 0.15 160)" },
  DRAFT:   { label: "Черновик",  bg: "#f1f5f9",              color: "#64748b" },
  STOPPED: { label: "Остановлен", bg: "#fef3c7",             color: "#92400e" },
};

const SIGNIFICANCE_STYLE = {
  significant: { label: "Статистически значимо", bg: "oklch(0.93 0.06 160)", color: "oklch(0.30 0.15 160)", icon: "circle-check" },
  trending:    { label: "Тренд (p < 0.20)",       bg: "#fef3c7",              color: "#92400e",              icon: "trending-up" },
  no_data:     { label: "Недостаточно данных",    bg: "#f1f5f9",              color: "#64748b",              icon: "" },
};

function StatusPill({ status }) {
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.DRAFT;
  return (
    <span style={{
      display: "inline-flex", padding: "3px 10px", borderRadius: 20,
      fontSize: 11, fontWeight: 700, background: s.bg, color: s.color,
    }}>{s.label}</span>
  );
}

// ── Страница ──────────────────────────────────────────────────────────────────
function Experiments({ currentUser, isLive }) {
  const isAdmin = currentUser.role === "admin";
  const listQ = useExperiments(!!isLive);
  const ops = useExperimentOps();
  const [selectedId, setSelectedId] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [toast, setToast] = useState(null);

  const experiments = isLive ? (listQ.data ?? []) : MOCK_EXPERIMENTS;
  const selected = experiments.find(e => e.experiment_id === selectedId) ?? experiments[0] ?? null;

  useEffect(() => {
    if (!selectedId && experiments.length) setSelectedId(experiments[0].experiment_id);
  }, [experiments, selectedId]);

  const resultsQ = useExperimentResults(
    selected && isLive ? selected.experiment_id : null, !!isLive,
  );
  const results = isLive
    ? (resultsQ.data ?? null)
    : (selected ? MOCK_RESULTS[selected.experiment_id] ?? null : null);

  async function handleStatus(action) {
    if (!selected) return;
    if (!isLive) {
      setToast({ msg: "В демо-режиме статусы не меняются", type: "info" });
      return;
    }
    try {
      await ops.setStatus.mutateAsync({ id: selected.experiment_id, action });
      setToast({
        msg: action === "start" ? "Эксперимент запущен" : "Эксперимент остановлен",
        type: "success",
      });
    } catch { /* тост — от интерцептора */ }
  }

  async function handleCreate(payload) {
    if (!isLive) {
      setToast({ msg: "Создание доступно в live-режиме", type: "info" });
      return;
    }
    try {
      const created = await ops.create.mutateAsync(payload);
      setSelectedId(created.experiment_id);
      setShowCreate(false);
      setToast({ msg: "Эксперимент создан (статус DRAFT)", type: "success" });
    } catch { /* тост — от интерцептора */ }
  }

  const fmtDate = iso => iso ? new Date(iso).toLocaleDateString("ru") : "—";

  return (
    <div style={{ display: "flex", gap: 20, maxWidth: 1400 }}>
      {/* Left: list */}
      <div style={{ width: 340, flexShrink: 0, display: "flex", flexDirection: "column", gap: 10 }}>
        {isAdmin && (
          <Button variant="primary" style={{ justifyContent: "center" }} onClick={() => setShowCreate(true)}>
            <span style={{ fontSize: 16 }}>+</span> Новый эксперимент
          </Button>
        )}
        {experiments.length === 0 && (
          <Card style={{ padding: 24, textAlign: "center", color: "#94a3b8", fontSize: 13 }}>
            Экспериментов пока нет
          </Card>
        )}
        {experiments.map(e => {
          const sel = selected?.experiment_id === e.experiment_id;
          return (
            <div key={e.experiment_id} onClick={() => setSelectedId(e.experiment_id)} style={{
              background: "white", borderRadius: 10, padding: "14px 16px", cursor: "pointer",
              border: sel ? "2px solid oklch(0.65 0.18 230)" : "1px solid #e8edf4",
              boxShadow: sel ? "0 0 0 3px oklch(0.85 0.08 230)" : "0 1px 3px rgba(0,0,0,0.04)",
              transition: "all 0.15s",
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                <div style={{ fontWeight: 700, fontSize: 13, color: "#0d1929", textWrap: "balance" }}>{e.name}</div>
                <StatusPill status={e.status} />
              </div>
              <div style={{ fontSize: 11, color: "#8896a8", marginTop: 6 }}>
                метрика: <strong style={{ color: "#64748b" }}>{e.target_metric}</strong>
                {" · "}{e.variants.length} варианта{" · "}с {fmtDate(e.start_date)}
              </div>
            </div>
          );
        })}
      </div>

      {/* Right: detail */}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
        {!selected ? (
          <Card style={{ padding: 48, textAlign: "center", color: "#94a3b8" }}>Выберите эксперимент</Card>
        ) : (
          <>
            {/* Header + actions */}
            <Card style={{ padding: "20px 24px" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                <div>
                  <div style={{ fontSize: 17, fontWeight: 700, color: "#0d1929" }}>{selected.name}</div>
                  <div style={{ fontSize: 12, color: "#8896a8", marginTop: 4 }}>
                    Целевая метрика: <strong>{selected.target_metric}</strong> · старт {fmtDate(selected.start_date)}
                    {isLive && <> · <strong style={{ color: "oklch(0.45 0.15 160)" }}>live API</strong></>}
                  </div>
                </div>
                {isAdmin && (
                  <div style={{ display: "flex", gap: 8 }}>
                    {selected.status === "DRAFT" && (
                      <Button variant="success" onClick={() => handleStatus("start")}>▶ Запустить</Button>
                    )}
                    {selected.status === "ACTIVE" && (
                      <Button variant="danger" onClick={() => handleStatus("stop")}>⏹ Остановить</Button>
                    )}
                  </div>
                )}
              </div>
            </Card>

            {/* Variants */}
            <Card style={{ padding: 24 }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: "#0d1929", marginBottom: 14 }}>Варианты и трафик</div>
              <div style={{ display: "grid", gridTemplateColumns: `repeat(${Math.min(selected.variants.length, 4)}, 1fr)`, gap: 12 }}>
                {selected.variants.map((v, i) => (
                  <div key={v.variant_id} style={{
                    borderRadius: 10, padding: "14px 16px",
                    border: "1px solid #e8edf4",
                    background: i === 0 ? "#f8fafc" : "oklch(0.97 0.03 230)",
                  }}>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#0d1929" }}>{v.name}</div>
                    <div style={{ fontSize: 11, color: "#8896a8", marginTop: 4, fontFamily: "'JetBrains Mono', monospace" }}>{v.strategy_class}</div>
                    <div style={{ marginTop: 10, fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: "oklch(0.45 0.18 230)" }}>
                      {Math.round(v.traffic_weight * 100)}%
                    </div>
                    <div style={{ fontSize: 10, color: "#94a3b8" }}>доля трафика</div>
                  </div>
                ))}
              </div>
            </Card>

            {/* Results */}
            <Card style={{ padding: 24 }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: "#0d1929", marginBottom: 4 }}>
                Результаты — two-proportion z-test
              </div>
              <div style={{ fontSize: 12, color: "#8896a8", marginBottom: 18 }}>
                Листинг 3.14 диссертации: сравнение конверсии control vs treatment
              </div>

              {!results ? (
                <div style={{ padding: "22px 16px", background: "#f8fafc", borderRadius: 10, fontSize: 13, color: "#64748b", textAlign: "center" }}>
                  Результатов пока нет — эксперимент не собрал события
                </div>
              ) : (
                <>
                  {/* Variant stats */}
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 18 }}>
                    {[results.control, results.treatment].map((v, i) => (
                      <div key={v.variant_id} style={{
                        borderRadius: 10, padding: "16px 18px",
                        border: i === 1 && results.significance === "significant"
                          ? "2px solid oklch(0.65 0.18 160)" : "1px solid #e8edf4",
                      }}>
                        <div style={{ display: "flex", justifyContent: "space-between" }}>
                          <span style={{ fontSize: 13, fontWeight: 700, color: "#0d1929" }}>{v.name}</span>
                          <span style={{ fontSize: 11, color: "#94a3b8" }}>{i === 0 ? "контроль" : "тест"}</span>
                        </div>
                        <div style={{ marginTop: 10, fontSize: 26, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: i === 1 ? "oklch(0.45 0.18 160)" : "#374151" }}>
                          {(v.rate * 100).toFixed(2)}%
                        </div>
                        <div style={{ fontSize: 11, color: "#8896a8", marginTop: 2 }}>
                          {v.successes.toLocaleString("ru")} конверсий из {v.n.toLocaleString("ru")}
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Stats row */}
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 16 }}>
                    {[
                      { l: "Δ конверсии", v: `${results.diff >= 0 ? "+" : ""}${(results.diff * 100).toFixed(2)} п.п.` },
                      { l: "z-статистика", v: results.z != null ? results.z.toFixed(2) : "—" },
                      { l: "p-value", v: results.p_value != null ? results.p_value.toFixed(4) : "—" },
                      { l: "95% CI, п.п.", v: results.confidence_interval
                          ? `[${(results.confidence_interval[0] * 100).toFixed(2)}; ${(results.confidence_interval[1] * 100).toFixed(2)}]`
                          : "—" },
                    ].map(m => (
                      <div key={m.l} style={{ background: "#f8fafc", borderRadius: 10, padding: "12px 14px" }}>
                        <div style={{ fontSize: 10, fontWeight: 700, color: "#8896a8", textTransform: "uppercase", letterSpacing: "0.5px" }}>{m.l}</div>
                        <div style={{ fontSize: 17, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: "#0d1929", marginTop: 4 }}>{m.v}</div>
                      </div>
                    ))}
                  </div>

                  {/* Verdict */}
                  {(() => {
                    const s = SIGNIFICANCE_STYLE[results.significance] ?? SIGNIFICANCE_STYLE.no_data;
                    return (
                      <div style={{ background: s.bg, color: s.color, borderRadius: 10, padding: "12px 16px", fontSize: 13, fontWeight: 600, display: "flex", gap: 8, alignItems: "center" }}>
                        {s.icon && <Icon name={s.icon} size={15} color={s.color} />}
                        {s.label}
                        {results.significance === "significant" && (
                          <span style={{ fontWeight: 400 }}>
                            — вариант «{results.treatment.name}» лучше контроля, результат можно катить в прод
                          </span>
                        )}
                      </div>
                    );
                  })()}
                </>
              )}
            </Card>
          </>
        )}
      </div>

      {showCreate && (
        <CreateExperimentModal
          onSave={handleCreate}
          onClose={() => setShowCreate(false)}
        />
      )}

      {toast && <Toast message={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}

// ── Создание эксперимента (A/B, 50/50) ────────────────────────────────────────
function CreateExperimentModal({ onSave, onClose }) {
  const [form, setForm] = useState({
    name: "",
    target_metric: "acceptance_rate",
    control: "control",
    treatment: "treatment",
    controlStrategy: "BaselineStrategy",
    treatmentStrategy: "CandidateStrategy",
  });
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const valid = form.name && form.target_metric && form.control && form.treatment;

  function submit() {
    if (!valid) return;
    onSave({
      name: form.name,
      target_metric: form.target_metric,
      start_date: new Date().toISOString(),
      end_date: null,
      variants: [
        { name: form.control, traffic_weight: 0.5, strategy_class: form.controlStrategy },
        { name: form.treatment, traffic_weight: 0.5, strategy_class: form.treatmentStrategy },
      ],
    });
  }

  return (
    <Modal open title="Новый A/B-эксперимент" onClose={onClose} width={520}>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <Input label="Название" value={form.name} onChange={v => set("name", v)}
               placeholder="Например: LightGBM v4 vs v3" required />
        <Input label="Целевая метрика" value={form.target_metric}
               onChange={v => set("target_metric", v)} placeholder="acceptance_rate" required />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Input label="Вариант A (контроль)" value={form.control} onChange={v => set("control", v)} required />
          <Input label="Вариант B (тест)" value={form.treatment} onChange={v => set("treatment", v)} required />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Input label="Стратегия A" value={form.controlStrategy} onChange={v => set("controlStrategy", v)} />
          <Input label="Стратегия B" value={form.treatmentStrategy} onChange={v => set("treatmentStrategy", v)} />
        </div>
        <div style={{ fontSize: 12, color: "#8896a8", background: "#f8fafc", borderRadius: 8, padding: "10px 12px" }}>
          Трафик делится 50/50. Эксперимент создаётся в статусе DRAFT — запустите его из панели.
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <Button variant="primary" style={{ flex: 1 }} disabled={!valid} onClick={submit}>Создать</Button>
          <Button variant="secondary" onClick={onClose}>Отмена</Button>
        </div>
      </div>
    </Modal>
  );
}

export default Experiments;
