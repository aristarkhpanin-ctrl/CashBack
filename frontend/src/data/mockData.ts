// frontend/src/data/mockData.ts
// Локальный импорт mock-данных из второго дизайна (Cashback2.zip).
// Используется страницами как fallback, когда API не отдаёт данных
// (новый стенд / Adminer ещё не наполнен через `make seed-demo`).
/* eslint-disable */

export interface UserRecord {
  id: number;
  name: string;
  email: string;
  role: "admin" | "marketer" | "analyst";
  avatar: string;
  lastLogin: string;
}

export const USERS: UserRecord[] = [
  { id: 1, name: "Аристарх Панин",    email: "a.petrov@bank.ru",    role: "admin",      avatar: "АП", lastLogin: "01.05.2026 09:12" },
  { id: 2, name: "Мария Соколова",    email: "m.sokolova@bank.ru",  role: "marketer",   avatar: "МС", lastLogin: "01.05.2026 08:45" },
  { id: 3, name: "Дмитрий Иванов",    email: "d.ivanov@bank.ru",    role: "analyst",    avatar: "ДИ", lastLogin: "30.04.2026 17:33" },
  { id: 4, name: "Ольга Новикова",    email: "o.novikova@bank.ru",  role: "marketer",   avatar: "ОН", lastLogin: "30.04.2026 14:20" },
  { id: 5, name: "Сергей Козлов",     email: "s.kozlov@bank.ru",    role: "analyst",    avatar: "СК", lastLogin: "29.04.2026 11:05" },
  { id: 6, name: "Наталья Лебедева",  email: "n.lebedeva@bank.ru",  role: "marketer",   avatar: "НЛ", lastLogin: "28.04.2026 16:50" },
];

export const ROLE_LABELS: Record<UserRecord["role"], string> = {
  admin: "Администратор", marketer: "Маркетолог", analyst: "Аналитик",
};

export const PERMISSIONS: Record<UserRecord["role"], Record<string, boolean>> = {
  admin:    { dashboard: true,  campaigns_view: true,  campaigns_create: true,  campaigns_edit: true,  campaigns_delete: true,  analytics: true,  users: true  },
  marketer: { dashboard: true,  campaigns_view: true,  campaigns_create: true,  campaigns_edit: true,  campaigns_delete: false, analytics: false, users: false },
  analyst:  { dashboard: true,  campaigns_view: true,  campaigns_create: false, campaigns_edit: false, campaigns_delete: false, analytics: true,  users: false },
};

// Иконки — имена Lucide (фаза 27), совпадают с reference_data.py.
export const MCC_CATEGORIES = [
  { code: "5411", name: "Супермаркеты", icon: "shopping-cart" },
  { code: "5912", name: "Аптеки", icon: "pill" },
  { code: "5541", name: "АЗС", icon: "fuel" },
  { code: "5812", name: "Рестораны", icon: "utensils" },
  { code: "5999", name: "Прочая розница", icon: "store" },
  { code: "7011", name: "Отели", icon: "hotel" },
  { code: "4111", name: "Транспорт", icon: "bus" },
  { code: "5045", name: "Электроника", icon: "laptop" },
  { code: "5600", name: "Одежда", icon: "shirt" },
  { code: "7832", name: "Кинотеатры", icon: "clapperboard" },
  { code: "5251", name: "DIY / Строительство", icon: "hammer" },
  { code: "5122", name: "Косметика", icon: "sparkles" },
];

export const SEGMENTS = [
  { id: "premium",   name: "Премиум",        count: 124500 },
  { id: "mass",      name: "Массовый",       count: 892000 },
  { id: "young",     name: "Молодежь",       count: 340000 },
  { id: "senior",    name: "Средний класс",  count: 210000 },
  { id: "business",  name: "Бизнес",         count: 87000  },
];

export interface MockCampaign {
  id: number;
  name: string;
  status: "active" | "paused" | "completed" | "draft";
  startDate: string;
  endDate: string;
  cashbackRate: number;
  budget: number;
  spent: number;
  dailyLimit: number;
  segments: string[];
  categories: string[];
  minTxAmount: number;
  reach: number;
  ctr: number;
  roi: number;
  createdBy: number;
}

export const CAMPAIGNS: MockCampaign[] = [
  { id: 1, name: "Летний кэшбэк — Супермаркеты", status: "active",   startDate: "2026-04-01", endDate: "2026-06-30", cashbackRate: 5,  budget: 5000000, spent: 2340000, dailyLimit: 100000, segments: ["premium", "mass"],     categories: ["5411"], minTxAmount: 500,  reach: 892000,  ctr: 18.4, roi: 2.3, createdBy: 2 },
  { id: 2, name: "Аптечный кэшбэк — Премиум",    status: "active",   startDate: "2026-03-15", endDate: "2026-05-31", cashbackRate: 7,  budget: 2000000, spent: 1650000, dailyLimit: 50000,  segments: ["premium"],              categories: ["5912"], minTxAmount: 300,  reach: 124500,  ctr: 24.1, roi: 3.1, createdBy: 2 },
  { id: 3, name: "АЗС — Молодежь и Массовый",    status: "paused",   startDate: "2026-04-15", endDate: "2026-07-15", cashbackRate: 3,  budget: 3000000, spent: 780000,  dailyLimit: 80000,  segments: ["young", "mass"],        categories: ["5541"], minTxAmount: 1000, reach: 1232000, ctr: 12.7, roi: 1.8, createdBy: 4 },
  { id: 4, name: "Рестораны выходного дня",      status: "completed",startDate: "2026-02-01", endDate: "2026-03-31", cashbackRate: 10, budget: 1500000, spent: 1500000, dailyLimit: 30000,  segments: ["premium", "young"],     categories: ["5812"], minTxAmount: 1500, reach: 464500,  ctr: 31.2, roi: 4.2, createdBy: 2 },
  { id: 5, name: "Электроника — Бизнес",         status: "draft",    startDate: "2026-05-15", endDate: "2026-08-15", cashbackRate: 4,  budget: 4000000, spent: 0,       dailyLimit: 120000, segments: ["business"],             categories: ["5045"], minTxAmount: 5000, reach: 87000,   ctr: 0,    roi: 0,   createdBy: 4 },
];

// Dashboard: daily offers accepted (last 30 days)
export function generateTrendData() {
  const data = [];
  const base = new Date("2026-04-01");
  for (let i = 0; i < 30; i++) {
    const d = new Date(base);
    d.setDate(d.getDate() + i);
    const label = `${d.getDate().toString().padStart(2, "0")}.${(d.getMonth() + 1).toString().padStart(2, "0")}`;
    data.push({
      date: label,
      premium: Math.round(800 + Math.random() * 400 + i * 15),
      mass:    Math.round(3200 + Math.random() * 1200 + i * 40),
      young:   Math.round(1500 + Math.random() * 600 + i * 20),
    });
  }
  return data;
}

export const MCC_HEATMAP = [
  { category: "Супермаркеты", premium: 91, mass: 78, young: 65, senior: 80, business: 45 },
  { category: "Аптеки",       premium: 88, mass: 62, young: 40, senior: 90, business: 30 },
  { category: "АЗС",          premium: 55, mass: 70, young: 80, senior: 35, business: 85 },
  { category: "Рестораны",    premium: 72, mass: 48, young: 88, senior: 30, business: 60 },
  { category: "Электроника",  premium: 60, mass: 40, young: 75, senior: 20, business: 90 },
  { category: "Одежда",       premium: 80, mass: 55, young: 92, senior: 40, business: 25 },
  { category: "Транспорт",    premium: 42, mass: 60, young: 85, senior: 50, business: 70 },
];

export const FUNNEL_DATA = [
  { stage: "Целевая аудитория",    value: 892000, color: "oklch(0.55 0.18 230)" },
  { stage: "Получили предложение", value: 714000, color: "oklch(0.55 0.18 220)" },
  { stage: "Открыли",              value: 428000, color: "oklch(0.55 0.18 210)" },
  { stage: "Приняли",              value: 163000, color: "oklch(0.55 0.18 200)" },
  { stage: "Совершили транзакцию", value: 98000,  color: "oklch(0.55 0.18 190)" },
  { stage: "Получили кэшбэк",      value: 91000,  color: "oklch(0.60 0.18 160)" },
];

export const MATRIX_DATA = {
  segments: ["Премиум", "Массовый", "Молодежь", "Средний класс", "Бизнес"],
  categories: ["Супермаркеты", "Аптеки", "АЗС", "Рестораны", "Электроника", "Одежда", "Транспорт"],
  values: [
    [91, 88, 55, 72, 60, 80, 42],
    [78, 62, 70, 48, 40, 55, 60],
    [65, 40, 80, 88, 75, 92, 85],
    [80, 90, 35, 30, 20, 40, 50],
    [45, 30, 85, 60, 90, 25, 70],
  ],
};

export const CHANNEL_DATA = [
  { channel: "Push",  sent: 450000, opened: 198000, converted: 67000 },
  { channel: "SMS",   sent: 320000, opened: 112000, converted: 45000 },
  { channel: "Email", sent: 280000, opened: 84000,  converted: 28000 },
  { channel: "App",   sent: 195000, opened: 130000, converted: 52000 },
];

export function computeActivityMatrix(campaign: MockCampaign | null, period: "7d" | "30d" | "90d" = "30d") {
  const periodMult = ({ "7d": 0.62, "30d": 1.00, "90d": 1.12 } as Record<string, number>)[period] || 1;
  let s = period === "7d" ? 7 : period === "90d" ? 91 : 31;
  const rng = () => { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646; };

  const SEG_NAME_BY_ID: Record<string, string> = Object.fromEntries(SEGMENTS.map(seg => [seg.id, seg.name]));
  const CAT_NAME_BY_CODE: Record<string, string> = {
    "5411": "Супермаркеты", "5912": "Аптеки", "5541": "АЗС", "5812": "Рестораны",
    "5045": "Электроника", "5600": "Одежда", "4111": "Транспорт", "5999": "Прочая розница",
  };
  const SEG_KEY_BY_NAME: Record<string, string> = {
    "Премиум": "premium", "Массовый": "mass", "Молодежь": "young",
    "Средний класс": "senior", "Бизнес": "business",
  };

  const activeSegNames = new Set<string>();
  const activeCatNames = new Set<string>();
  if (campaign) {
    campaign.segments.forEach((id: string) => { if (SEG_NAME_BY_ID[id]) activeSegNames.add(SEG_NAME_BY_ID[id]); });
    campaign.categories.forEach((code: string) => { if (CAT_NAME_BY_CODE[code]) activeCatNames.add(CAT_NAME_BY_CODE[code]); });
  }

  const values = MATRIX_DATA.values.map((segRow, si) => {
    const segName = MATRIX_DATA.segments[si];
    const inSeg = activeSegNames.has(segName);
    return segRow.map((v, ci) => {
      const catName = MATRIX_DATA.categories[ci];
      const inCat = activeCatNames.has(catName);
      let base = v;
      if (campaign && inSeg && inCat) base = v * 1.15;
      const variation = 0.96 + rng() * 0.08;
      return Math.max(0, Math.min(99, Math.round(base * periodMult * variation)));
    });
  });

  const rows = MATRIX_DATA.categories.map((cat, ci) => {
    const row: any = { category: cat };
    MATRIX_DATA.segments.forEach((segName, si) => {
      const key = SEG_KEY_BY_NAME[segName];
      if (key) row[key] = values[si][ci];
    });
    return row;
  });

  return {
    segments: MATRIX_DATA.segments,
    categories: MATRIX_DATA.categories,
    values, rows,
    activeSegments: activeSegNames,
    activeCategories: activeCatNames,
  };
}

export type MockAppData = {
  USERS: typeof USERS;
  ROLE_LABELS: typeof ROLE_LABELS;
  PERMISSIONS: typeof PERMISSIONS;
  MCC_CATEGORIES: typeof MCC_CATEGORIES;
  SEGMENTS: typeof SEGMENTS;
  CAMPAIGNS: typeof CAMPAIGNS;
  generateTrendData: typeof generateTrendData;
  MCC_HEATMAP: typeof MCC_HEATMAP;
  FUNNEL_DATA: typeof FUNNEL_DATA;
  MATRIX_DATA: typeof MATRIX_DATA;
  CHANNEL_DATA: typeof CHANNEL_DATA;
  computeActivityMatrix: typeof computeActivityMatrix;
};
