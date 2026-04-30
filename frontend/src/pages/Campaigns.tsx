import { useEffect, useState } from 'react';
import { Check, ChevronRight, Users, Tag, Wallet, Eye, FileText } from 'lucide-react';
import { toast } from 'sonner';
import { useNavigate } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import dayjs from 'dayjs';
import {
  campaignApi, campaignKeys,
} from '@/features/campaigns/api/campaignApi';
import {
  defaultForm, segmentsToIds, useWizard, type WizardForm,
} from '@/store/wizardStore';
import { useDebounced } from '@/shared/hooks/useDebounced';
import type { CampaignCreatePayload } from '@/shared/api/types';

// ── Constants ─────────────────────────────────────────────────────────────────

const STEPS = [
  { id: 1, label: `Основные параметры`, icon: FileText },
  { id: 2, label: `Целевая аудитория`,   icon: Users },
  { id: 3, label: `Категории кэшбэка`,    icon: Tag },
  { id: 4, label: `Бюджетирование`,       icon: Wallet },
  { id: 5, label: `Ревью и запуск`,       icon: Eye },
];

const SEGMENTS = [`Premium`, `Mass`, `VIP`, `Новые`, `Спящие`, `Активные`];
const rfmOptions = [`1`, `2`, `3`, `4`, `5`];

const MCC_CATALOG = [
  { code: `5411`, label: `Супермаркеты` },
  { code: `5812`, label: `Рестораны` },
  { code: `5541`, label: `АЗС` },
  { code: `5912`, label: `Аптеки` },
  { code: `5651`, label: `Одежда` },
  { code: `5732`, label: `Электроника` },
  { code: `4111`, label: `Транспорт` },
  { code: `7996`, label: `Развлечения` },
  { code: `5941`, label: `Спорт` },
  { code: `4722`, label: `Путешествия` },
  { code: `5999`, label: `Прочее` },
  { code: `7011`, label: `Отели` },
];

function fmtNum(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(0)}K` : String(n);
}

function toIso(date: string): string {
  // <input type="date"> returns YYYY-MM-DD; Postgres TIMESTAMPTZ wants ISO.
  if (!date) return ``;
  return dayjs(date).startOf(`day`).toISOString();
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StepIndicator({ current, onGoto }: { current: number; onGoto: (s: number) => void }) {
  return (
    <div className="flex items-center gap-0 mb-8">
      {STEPS.map((step, idx) => {
        const done = current > step.id;
        const active = current === step.id;
        return (
          <div key={step.id} className="flex items-center">
            <button
              onClick={() => done && onGoto(step.id)}
              className="flex flex-col items-center gap-1.5"
              style={{ cursor: done ? `pointer` : `default` }}
            >
              <div
                className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-bold transition-all duration-200 ${done ? `wizard-step-done` : active ? `wizard-step-active` : `wizard-step-idle`}`}
              >
                {done ? <Check size={15} /> : step.id}
              </div>
              <span className={`text-xs font-medium whitespace-nowrap ${active ? `text-foreground` : `text-muted-foreground`}`}>
                {step.label}
              </span>
            </button>
            {idx < STEPS.length - 1 && (
              <div
                className="mx-3 mb-5 flex-1 transition-all duration-300"
                style={{
                  height: 2,
                  width: 48,
                  background: done ? `#10b981` : `var(--border)`,
                  borderRadius: 2,
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Steps ─────────────────────────────────────────────────────────────────────

function Step1({ form, setForm }: { form: WizardForm; setForm: (f: WizardForm) => void }) {
  return (
    <div className="flex flex-col gap-5 max-w-xl">
      <div>
        <label className="text-sm font-medium text-foreground block mb-1.5">Название кампании</label>
        <input
          type="text"
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder={`Например: Летний кэшбэк 2025`}
          className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </div>
      <div className="flex gap-4">
        <div className="flex-1">
          <label className="text-sm font-medium text-foreground block mb-1.5">Дата начала</label>
          <input
            type="date"
            value={form.startDate}
            onChange={(e) => setForm({ ...form, startDate: e.target.value })}
            className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div className="flex-1">
          <label className="text-sm font-medium text-foreground block mb-1.5">Дата окончания</label>
          <input
            type="date"
            value={form.endDate}
            onChange={(e) => setForm({ ...form, endDate: e.target.value })}
            className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
      </div>
      <div>
        <label className="text-sm font-medium text-foreground block mb-1.5">Ставка кэшбэка (%)</label>
        <input
          type="number"
          min="0.5"
          max="30"
          step="0.5"
          value={form.cashbackRate}
          onChange={(e) => setForm({ ...form, cashbackRate: e.target.value })}
          placeholder={`5`}
          className="w-48 px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
        />
        <p className="text-xs text-muted-foreground mt-1.5">Рекомендуемый диапазон: 1% – 15%</p>
      </div>
    </div>
  );
}

function Step2({ form, setForm }: { form: WizardForm; setForm: (f: WizardForm) => void }) {
  // Debounce 400ms before calling estimateAudience.
  const debouncedSegments = useDebounced(form.segments, 400);
  const debouncedRfm = useDebounced(
    { r: form.rfmR, f: form.rfmF, m: form.rfmM }, 400,
  );

  const segIds = segmentsToIds(debouncedSegments);
  const enabled = segIds.length > 0;

  const audienceQ = useQuery({
    enabled,
    queryKey: campaignKeys.audience(
      segIds,
      debouncedRfm.r ? Number(debouncedRfm.r) : null,
      debouncedRfm.f ? Number(debouncedRfm.f) : null,
      debouncedRfm.m ? Number(debouncedRfm.m) : null,
    ),
    queryFn: () =>
      campaignApi.estimateAudience({
        segment_ids: segIds,
        rfmR: debouncedRfm.r ? Number(debouncedRfm.r) : null,
        rfmF: debouncedRfm.f ? Number(debouncedRfm.f) : null,
        rfmM: debouncedRfm.m ? Number(debouncedRfm.m) : null,
      }),
  });

  function toggleSeg(s: string) {
    const segs = form.segments.includes(s)
      ? form.segments.filter((x) => x !== s)
      : [...form.segments, s];
    setForm({ ...form, segments: segs });
  }

  const audience = audienceQ.data?.estimated_users ?? 0;

  return (
    <div className="flex gap-8">
      <div className="flex flex-col gap-5 flex-1">
        {/* Segments */}
        <div>
          <label className="text-sm font-medium text-foreground block mb-2">Сегменты клиентов</label>
          <div className="flex flex-wrap gap-2">
            {SEGMENTS.map((seg) => {
              const sel = form.segments.includes(seg);
              return (
                <button
                  key={seg}
                  onClick={() => toggleSeg(seg)}
                  className="px-3 py-1.5 rounded-lg text-sm font-medium border transition-all duration-150"
                  style={{
                    background: sel ? `var(--primary)` : `var(--muted)`,
                    color: sel ? `#fff` : `var(--muted-foreground)`,
                    borderColor: sel ? `var(--primary)` : `var(--border)`,
                  }}
                >
                  {seg}
                </button>
              );
            })}
          </div>
        </div>

        {/* RFM filters */}
        <div>
          <label className="text-sm font-medium text-foreground block mb-2">Фильтры RFM-квантилей</label>
          <div className="flex gap-4">
            {[
              { key: `rfmR`, label: `Recency (R)` },
              { key: `rfmF`, label: `Frequency (F)` },
              { key: `rfmM`, label: `Monetary (M)` },
            ].map(({ key, label }) => (
              <div key={key} className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">{label}</span>
                <select
                  value={(form as unknown as Record<string, string>)[key]}
                  onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                  className="px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
                >
                  <option value={``}>Все</option>
                  {rfmOptions.map((o) => (
                    <option key={o} value={o}>≥ {o}</option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Audience preview */}
      <div
        className="rounded-xl p-5 border border-border flex flex-col items-center justify-center"
        style={{ width: 200, background: `var(--accent)` }}
      >
        <span className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-2">Охват</span>
        <span className="text-4xl font-extrabold" style={{ color: `var(--primary)` }}>
          {enabled ? (audienceQ.isLoading ? `…` : fmtNum(audience)) : `—`}
        </span>
        <span className="text-xs text-muted-foreground mt-1">уникальных клиентов</span>
        {!enabled && (
          <p className="text-xs text-muted-foreground mt-3 text-center leading-relaxed">Выберите хотя бы один сегмент</p>
        )}
        {enabled && audienceQ.isError && (
          <p className="text-xs mt-3 text-center" style={{ color: `#ef4444` }}>
            Ошибка при оценке
          </p>
        )}
      </div>
    </div>
  );
}

function Step3({ form, setForm }: { form: WizardForm; setForm: (f: WizardForm) => void }) {
  const [search, setSearch] = useState(``);
  const filtered = MCC_CATALOG.filter(
    (m) =>
      m.label.toLowerCase().includes(search.toLowerCase()) ||
      m.code.includes(search)
  );

  function toggleMcc(code: string) {
    const codes = form.mccCodes.includes(code)
      ? form.mccCodes.filter((c) => c !== code)
      : [...form.mccCodes, code];
    setForm({ ...form, mccCodes: codes });
  }

  return (
    <div className="flex gap-8">
      <div className="flex flex-col gap-4 flex-1">
        <div>
          <label className="text-sm font-medium text-foreground block mb-1.5">Поиск по MCC-каталогу</label>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={`Введите название или код...`}
            className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div className="flex flex-col gap-2 max-h-56 overflow-y-auto pr-1">
          {filtered.map((m) => {
            const sel = form.mccCodes.includes(m.code);
            return (
              <button
                key={m.code}
                onClick={() => toggleMcc(m.code)}
                className="flex items-center justify-between px-3 py-2.5 rounded-lg border text-sm transition-all duration-150 text-left"
                style={{
                  background: sel ? `var(--secondary)` : `var(--muted)`,
                  borderColor: sel ? `var(--primary)` : `var(--border)`,
                  color: `var(--foreground)`,
                }}
              >
                <span>{m.label}</span>
                <span className="text-xs text-muted-foreground ml-2">{m.code}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Min amounts */}
      <div className="flex flex-col gap-3 flex-1">
        <label className="text-sm font-medium text-foreground block">Мин. сумма транзакции (₽)</label>
        {form.mccCodes.length === 0 && (
          <p className="text-xs text-muted-foreground">Выберите категории слева</p>
        )}
        {form.mccCodes.map((code) => {
          const mcc = MCC_CATALOG.find((m) => m.code === code);
          return (
            <div key={code} className="flex items-center gap-3">
              <span className="text-sm flex-1 text-foreground">{mcc?.label}</span>
              <input
                type="number"
                min="0"
                value={form.minTxAmount[code] ?? ``}
                onChange={(e) =>
                  setForm({ ...form, minTxAmount: { ...form.minTxAmount, [code]: e.target.value } })
                }
                placeholder={`500`}
                className="w-28 px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
              />
              <span className="text-xs text-muted-foreground">₽</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Step4({ form, setForm }: { form: WizardForm; setForm: (f: WizardForm) => void }) {
  return (
    <div className="flex flex-col gap-5 max-w-lg">
      <div>
        <label className="text-sm font-medium text-foreground block mb-1.5">Общий бюджет (₽)</label>
        <input
          type="number"
          min="0"
          value={form.totalBudget}
          onChange={(e) => setForm({ ...form, totalBudget: e.target.value })}
          placeholder={`1000000`}
          className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
        />
      </div>
      <div>
        <label className="text-sm font-medium text-foreground block mb-1.5">Дневной лимит (₽)</label>
        <input
          type="number"
          min="0"
          value={form.dailyLimit}
          onChange={(e) => setForm({ ...form, dailyLimit: e.target.value })}
          placeholder={`50000`}
          className="w-full px-3 py-2 text-sm border border-border rounded-lg bg-background focus:outline-none focus:ring-2 focus:ring-ring"
        />
        <p className="text-xs text-muted-foreground mt-1.5">Максимальные расходы в сутки</p>
      </div>
      <div className="flex items-center gap-3 p-4 rounded-xl border border-border bg-muted">
        <button
          onClick={() => setForm({ ...form, autoPause: !form.autoPause })}
          className="relative w-10 h-6 rounded-full transition-all duration-200 flex-shrink-0"
          style={{ background: form.autoPause ? `var(--primary)` : `var(--border)` }}
        >
          <span
            className="absolute top-0.5 w-5 h-5 bg-white rounded-full shadow transition-all duration-200"
            style={{ left: form.autoPause ? `calc(100% - 22px)` : 2 }}
          />
        </button>
        <div>
          <p className="text-sm font-medium text-foreground">Автоматическая приостановка</p>
          <p className="text-xs text-muted-foreground">Кампания приостановится при исчерпании бюджета</p>
        </div>
      </div>
      {form.totalBudget && form.dailyLimit && (
        <div className="p-4 rounded-xl border border-border bg-card">
          <p className="text-xs text-muted-foreground mb-1">Прогноз продолжительности</p>
          <p className="text-lg font-bold text-foreground">
            {Math.ceil(Number(form.totalBudget) / Number(form.dailyLimit))} дней
          </p>
        </div>
      )}
    </div>
  );
}

function Step5({ form, onGoto }: { form: WizardForm; onGoto: (s: number) => void }) {
  const rows = [
    { label: `Название`, value: form.name || `—`, step: 1 },
    { label: `Период`, value: form.startDate && form.endDate ? `${form.startDate} — ${form.endDate}` : `—`, step: 1 },
    { label: `Ставка кэшбэка`, value: form.cashbackRate ? `${form.cashbackRate}%` : `—`, step: 1 },
    { label: `Сегменты`, value: form.segments.join(`, `) || `—`, step: 2 },
    { label: `RFM фильтры`, value: [form.rfmR && `R≥${form.rfmR}`, form.rfmF && `F≥${form.rfmF}`, form.rfmM && `M≥${form.rfmM}`].filter(Boolean).join(`, `) || `Без ограничений`, step: 2 },
    { label: `MCC категории`, value: form.mccCodes.length ? `${form.mccCodes.length} категорий` : `—`, step: 3 },
    { label: `Общий бюджет`, value: form.totalBudget ? `₽${Number(form.totalBudget).toLocaleString()}` : `—`, step: 4 },
    { label: `Дневной лимит`, value: form.dailyLimit ? `₽${Number(form.dailyLimit).toLocaleString()}` : `—`, step: 4 },
    { label: `Авто-пауза`, value: form.autoPause ? `Включена` : `Выключена`, step: 4 },
  ];

  return (
    <div className="max-w-2xl">
      <div className="bg-card rounded-xl border border-border overflow-hidden">
        {rows.map((row) => (
          <div key={row.label} className="flex items-center px-5 py-3 border-b border-border last:border-0 hover:bg-muted/40 group transition-colors">
            <span className="text-sm text-muted-foreground w-44 flex-shrink-0">{row.label}</span>
            <span className="text-sm font-medium text-foreground flex-1">{row.value}</span>
            <button
              onClick={() => onGoto(row.step)}
              className="text-xs font-medium opacity-0 group-hover:opacity-100 transition-opacity"
              style={{ color: `var(--primary)` }}
            >
              Изменить
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────────

export default function Campaigns() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const wizard = useWizard();
  const { step, form } = wizard;

  // Local mirror so we can pass setForm-style signature into the steps.
  // (Form lives in zustand for persistence; this just adapts the API.)
  const [localForm, setLocalForm] = useState<WizardForm>(form);
  useEffect(() => setLocalForm(form), [form]);
  function setForm(f: WizardForm) {
    setLocalForm(f);
    wizard.set(f);
  }
  function setStep(s: number) {
    wizard.setStep(s);
  }

  function next() {
    if (step < 5) setStep(step + 1);
  }

  function prev() {
    if (step > 1) setStep(step - 1);
  }

  const create = useMutation({
    mutationFn: (payload: CampaignCreatePayload) => campaignApi.create(payload),
    onSuccess: (camp) => {
      toast.success(`Кампания «${camp.name}» создана!`);
      queryClient.invalidateQueries({ queryKey: campaignKeys.active });
      wizard.reset();
      setLocalForm(defaultForm);
      navigate(`/campaigns`);
    },
    onError: () => {
      // The Axios interceptor already showed a toast.
    },
  });

  function launch() {
    const f = localForm;

    if (!f.name) {
      toast.error(`Введите название кампании`);
      return setStep(1);
    }
    if (!f.startDate || !f.endDate) {
      toast.error(`Укажите даты начала и окончания`);
      return setStep(1);
    }
    if (!f.cashbackRate) {
      toast.error(`Укажите ставку кэшбэка`);
      return setStep(1);
    }
    const segIds = segmentsToIds(f.segments);
    if (segIds.length === 0) {
      toast.error(`Выберите хотя бы один сегмент`);
      return setStep(2);
    }
    if (f.mccCodes.length === 0) {
      toast.error(`Выберите хотя бы одну MCC-категорию`);
      return setStep(3);
    }
    if (!f.totalBudget) {
      toast.error(`Укажите общий бюджет`);
      return setStep(4);
    }

    // Take the smallest non-empty min_transaction_amount as the
    // campaign-level minimum; per-MCC minimums can be added once the
    // backend exposes that endpoint.
    const minPerMcc = f.mccCodes
      .map((c) => Number(f.minTxAmount[c]))
      .filter((v) => Number.isFinite(v) && v > 0);
    const minTx = minPerMcc.length ? Math.min(...minPerMcc) : null;

    const payload: CampaignCreatePayload = {
      name: f.name,
      target_segment_ids: segIds,
      cashback_rate: f.cashbackRate,
      min_transaction_amount: minTx,
      budget_total: f.totalBudget,
      start_date: toIso(f.startDate),
      end_date: toIso(f.endDate),
      allowed_channels: [`ONLINE`, `POS`, `MOBILE`],
      require_existing_behavior: false,
      rate_tiers: null,
      mcc_codes: f.mccCodes,
    };

    create.mutate(payload);
  }

  const stepTitles: Record<number, string> = {
    1: `Основные параметры`,
    2: `Целевая аудитория`,
    3: `Категории кэшбэка`,
    4: `Бюджетирование`,
    5: `Ревью и запуск`,
  };

  return (
    <div data-cmp="Campaigns" className="p-8">
      <div className="bg-card rounded-xl shadow-custom border border-border p-8">
        <div className="mb-6">
          <h2 className="text-lg font-bold text-foreground mb-0.5">Создание кампании</h2>
          <p className="text-sm text-muted-foreground">Шаг {step} из 5 — {stepTitles[step]}</p>
        </div>

        <StepIndicator current={step} onGoto={setStep} />

        <div className="min-h-64">
          <div className={step === 1 ? `` : `hidden`}><Step1 form={localForm} setForm={setForm} /></div>
          <div className={step === 2 ? `` : `hidden`}><Step2 form={localForm} setForm={setForm} /></div>
          <div className={step === 3 ? `` : `hidden`}><Step3 form={localForm} setForm={setForm} /></div>
          <div className={step === 4 ? `` : `hidden`}><Step4 form={localForm} setForm={setForm} /></div>
          <div className={step === 5 ? `` : `hidden`}><Step5 form={localForm} onGoto={setStep} /></div>
        </div>

        {/* Nav buttons */}
        <div className="flex items-center justify-between mt-8 pt-6 border-t border-border">
          <button
            onClick={prev}
            className="px-5 py-2.5 rounded-lg border border-border text-sm font-medium text-foreground hover:bg-muted transition-colors"
            style={{ visibility: step === 1 ? `hidden` : `visible` }}
          >
            ← Назад
          </button>
          <div className="flex items-center gap-2">
            {STEPS.map((s) => (
              <div
                key={s.id}
                className="rounded-full transition-all duration-200"
                style={{
                  width: step === s.id ? 20 : 8,
                  height: 8,
                  background: step === s.id ? `var(--primary)` : step > s.id ? `#10b981` : `var(--border)`,
                }}
              />
            ))}
          </div>
          {step < 5 ? (
            <button
              onClick={next}
              className="px-5 py-2.5 rounded-lg text-sm font-semibold text-primary-foreground transition-colors flex items-center gap-2"
              style={{ background: `var(--primary)` }}
            >
              Далее <ChevronRight size={15} />
            </button>
          ) : (
            <button
              onClick={launch}
              disabled={create.isPending}
              className="px-6 py-2.5 rounded-lg text-sm font-semibold text-primary-foreground transition-colors flex items-center gap-2"
              style={{ background: `#10b981`, opacity: create.isPending ? 0.7 : 1 }}
            >
              <Check size={15} />
              {create.isPending ? `Создаём…` : `Запустить кампанию`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
