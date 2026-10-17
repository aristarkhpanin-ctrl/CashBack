/**
 * Live-режим, этап B (STAGE_B_CONTRACT §3, §5, §6, §9): сессии и права
 * против стаба, повторяющего поведение campaign_manager — одноразовые
 * refresh-токены с отзывом семейства, logout, обязательная смена пароля,
 * троттлинг входа, SSE по sse-токену, ML-объяснения через /ml/explain,
 * правила для ADMIN-аккаунтов. Каждый тест — в своём контексте браузера
 * (чистый localStorage), стаб — один на файл.
 */
import {
  expect, test, type Browser, type BrowserContext, type Page, type Request,
} from '@playwright/test';
import type { ChildProcess } from 'node:child_process';
import {
  STUB_USERS, loginAs, startStub, stopStub, stubApi, stubControl, type StubUser,
} from './utils';

test.describe.configure({ mode: 'serial' });

const API = '/api/campaigns';
const REFRESH_KEY = 'cashback.refresh_token';

type StubState = {
  refresh_ok: number;
  refresh_reuse: number;
  logout_tokens: string[];
  sse_tokens: string[];
  sse_connects: string[];
  explain_calls: string[];
  rec_calls: number;
  users: Record<string, { ver: number; must_change_password: boolean }>;
};

let stub: ChildProcess;
const contexts: BrowserContext[] = [];

test.beforeAll(async () => {
  stub = await startStub();
});

// Страницы прошлых тестов не должны жить дальше: их фоновые запросы
// (refetch, SSE) с теми же учётками исказили бы счётчики стаба.
test.afterEach(async () => {
  await Promise.all(contexts.splice(0).map(c => c.close()));
});

test.afterAll(async () => {
  await stopStub(stub);
});

async function openApp(browser: Browser, ctx?: BrowserContext): Promise<Page> {
  const context = ctx ?? await browser.newContext();
  if (!ctx) contexts.push(context);
  const page = await context.newPage();
  await page.goto('/');
  return page;
}

async function expectApp(page: Page): Promise<void> {
  await expect(page.locator('aside').getByText('Дашборд')).toBeVisible();
  await expect(page.locator('header').getByText('LIVE API')).toBeVisible();
}

const loginForm = (page: Page) => page.locator('input[type="email"]');
const refreshTokenOf = (page: Page) => page.evaluate(k => localStorage.getItem(k), REFRESH_KEY);
const state = () => stubControl<StubState>('state');

/** Запросы к campaign_manager как «METHOD /path» (без health-пинга). */
function recordApi(page: Page): string[] {
  const calls: string[] = [];
  page.on('request', (r: Request) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith(API) && !u.pathname.endsWith('/health/live')) {
      calls.push(`${r.method()} ${u.pathname.slice(API.length)}`);
    }
  });
  return calls;
}

/**
 * Вход без фонового шума: SSE-событие (и refetch по нему) выключено, все
 * стартовые запросы приложения отработали. Следующий запрос к API делает
 * сам тест — правка состояния стаба не «срабатывает» раньше его клика.
 */
async function loginQuiet(page: Page, user: StubUser): Promise<void> {
  await stubControl('config', { sse_stats_event: false });
  try {
    const started = Promise.all(['/reference/segments', '/reference/mcc-categories', '/events/stream']
      .map(p => page.waitForResponse(r => new URL(r.url()).pathname === `${API}${p}`)));
    await loginAs(page, user);
    await expectApp(page);
    await started;
  } finally {
    await stubControl('config', { sse_stats_event: true });
  }
}

function directRefresh(token: string): Promise<Response> {
  return stubApi('/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: token }),
  });
}

// ── Вход ─────────────────────────────────────────────────────────────────────
test('страница входа без подсказок с учётными данными', async ({ browser }) => {
  const page = await openApp(browser);
  await expect(loginForm(page)).toBeVisible();
  await expect(loginForm(page)).toHaveValue('');
  await expect(page.getByText(/Демо-доступ/)).toHaveCount(0);
  await expect(page.getByText(/admin@bank\.ru/)).toHaveCount(0);
  await expect(page.getByText(/seed-demo/)).toHaveCount(0);
});

test('перебор пароля → 429: «слишком много попыток» с Retry-After', async ({ browser }) => {
  const page = await openApp(browser);
  const attempt = async () => {
    const resp = page.waitForResponse(r => r.url().endsWith('/auth/login'));
    await page.getByText('Войти', { exact: true }).click();
    return (await resp).status();
  };
  await loginForm(page).fill(STUB_USERS.marketer.email);
  await page.locator('input[type="password"]').fill('wrong-password-123');
  for (let i = 0; i < 5; i++) {
    expect(await attempt()).toBe(401);
    await expect(page.getByText('Неверный email или пароль')).toBeVisible();
  }
  // 6-я попытка — даже с верным паролем: окно (email, IP) исчерпано.
  await page.locator('input[type="password"]').fill(STUB_USERS.marketer.password);
  expect(await attempt()).toBe(429);
  await expect(page.getByText(/^Слишком много попыток входа\. Повторите через \d+ мин\.$/))
    .toBeVisible();
  await expect(page.getByText('Неверный email или пароль')).toHaveCount(0);
  // Ошибки формы — inline, без тостов интерцептора («требуется вход»).
  await expect(page.getByText(/требуется вход/)).toHaveCount(0);
  await expect(loginForm(page)).toBeVisible();
  await stubControl('reset-throttle', {});
});

// ── Обязательная смена пароля ────────────────────────────────────────────────
test('must_change_password: вход → «Смена пароля» → приложение на новой паре токенов', async ({ browser }) => {
  const page = await openApp(browser);
  const calls = recordApi(page);
  const loginResp = page.waitForResponse(r => r.url().endsWith('/auth/login'));
  await loginAs(page, STUB_USERS.bootstrap);
  const loginRefresh = (await (await loginResp).json()).refresh_token as string;

  await expect(page.getByText('Смена пароля', { exact: true })).toBeVisible();
  await expect(page.locator('aside')).toHaveCount(0);
  // С токеном до смены пароля UI ходит только туда, куда его пускают.
  await page.waitForTimeout(1000);
  expect(new Set(calls)).toEqual(new Set(['POST /auth/login', 'GET /auth/me']));

  const current = page.getByLabel('Текущий пароль', { exact: true });
  const next = page.getByLabel('Новый пароль', { exact: true });
  const repeat = page.getByLabel('Повторите новый пароль', { exact: true });
  const submit = page.getByRole('button', { name: 'Сменить пароль' });

  // Клиентские проверки политики.
  await current.fill(STUB_USERS.bootstrap.password);
  await next.fill('short-pass');
  await repeat.fill('short-pass');
  await expect(page.getByText('Не короче 12 символов', { exact: true })).toBeVisible();
  await expect(submit).toBeDisabled();
  await next.fill(STUB_USERS.bootstrap.email);
  await repeat.fill(STUB_USERS.bootstrap.email);
  await expect(page.getByText('Не должен совпадать с email', { exact: true })).toBeVisible();
  await next.fill('Fresh-Passw0rd-2026');
  await repeat.fill('Fresh-Passw0rd-2027');
  await expect(page.getByText('Пароли не совпадают', { exact: true })).toBeVisible();
  await expect(submit).toBeDisabled();

  // Неверный текущий пароль: 401 формы — не повод продлевать сессию.
  await current.fill('not-the-current-one');
  await repeat.fill('Fresh-Passw0rd-2026');
  await submit.click();
  await expect(page.getByText('Неверный текущий пароль')).toBeVisible();
  expect(calls).not.toContain('POST /auth/refresh');
  await expect(page.getByText('Смена пароля', { exact: true })).toBeVisible();

  // Известный дефолт отклоняет сервер (422).
  await current.fill(STUB_USERS.bootstrap.password);
  await next.fill('please-change-me');
  await repeat.fill('please-change-me');
  await submit.click();
  await expect(page.getByText(/известный пароль по умолчанию/)).toBeVisible();

  // Успех: новая пара из ответа — в работе, прежняя гаснет.
  await next.fill('Fresh-Passw0rd-2026');
  await repeat.fill('Fresh-Passw0rd-2026');
  const changed = page.waitForResponse(r => r.url().endsWith('/auth/change-password') && r.status() === 200);
  await submit.click();
  const pair = await (await changed).json();
  expect(pair.must_change_password).toBe(false);
  await expectApp(page);
  expect(await refreshTokenOf(page)).toBe(pair.refresh_token);
  expect((await directRefresh(loginRefresh)).status).toBe(401);
  expect((await state()).users[STUB_USERS.bootstrap.email].must_change_password).toBe(false);
});

test('403 «password change required» с любого эндпоинта → экран смены пароля', async ({ browser }) => {
  const page = await openApp(browser);
  await loginQuiet(page, STUB_USERS.analyst);
  // Оператор потребовал смену пароля уже после выдачи токена.
  await stubControl(`users/${STUB_USERS.analyst.email}`, { must_change_password: true });
  await page.locator('aside >> text=Эксперименты').first().click();   // GET /experiments → 403
  await expect(page.getByText('Смена пароля', { exact: true })).toBeVisible();
  await expect(page.getByText('Недостаточно прав')).toHaveCount(0);

  await page.getByLabel('Текущий пароль', { exact: true }).fill(STUB_USERS.analyst.password);
  await page.getByLabel('Новый пароль', { exact: true }).fill('Analyst-Fresh-Passw0rd');
  await page.getByLabel('Повторите новый пароль', { exact: true }).fill('Analyst-Fresh-Passw0rd');
  await page.getByRole('button', { name: 'Сменить пароль' }).click();
  await expectApp(page);
  await page.locator('aside >> text=Эксперименты').first().click();
  await expect(page.getByText('LightGBM vs SVD-baseline (ranking)').first()).toBeVisible();
});

test('прочий 403 → тост «Недостаточно прав», сессия жива', async ({ browser }) => {
  // На входе у маркетолога есть analytics (пункт меню виден), затем право
  // снимают — сервер отвечает 403, UI не должен разлогинивать.
  await stubControl('role-permissions/MARKETER', { analytics: true });
  const page = await openApp(browser);
  await loginAs(page, STUB_USERS.marketer);
  await expectApp(page);
  await stubControl('role-permissions/MARKETER', { analytics: false });
  await page.locator('aside >> text=Аналитика').first().click();
  await expect(page.getByText('Недостаточно прав', { exact: true })).toBeVisible();
  await expect(page.locator('aside').getByText('Дашборд')).toBeVisible();
  await expect(loginForm(page)).toHaveCount(0);
  expect(await refreshTokenOf(page)).toBeTruthy();
});

// ── Ротация refresh-токена ───────────────────────────────────────────────────
test('параллельные 401 → ровно один POST /auth/refresh, новый refresh-токен сохранён', async ({ browser }) => {
  const page = await openApp(browser);
  await loginAs(page, STUB_USERS.admin);
  await expectApp(page);
  await expect(page.getByText(/обновлено \d+ с назад/)).toBeVisible({ timeout: 6000 });
  const before = await refreshTokenOf(page);
  const reuseBefore = (await state()).refresh_reuse;

  const refreshes: Request[] = [];
  const unauthorized: string[] = [];
  page.on('request', r => { if (r.url().endsWith('/auth/refresh')) refreshes.push(r); });
  page.on('response', r => { if (r.status() === 401) unauthorized.push(r.url()); });

  // «Прошло 15 минут» + медленный refresh: все 401 приходят, пока обмен в полёте.
  await stubControl('config', { refresh_delay_ms: 400 });
  await stubControl('expire-access', { email: STUB_USERS.admin.email });
  try {
    await page.locator('aside >> text=Аналитика').first().click();
    await expect(page.getByText('16К').first()).toBeVisible();   // воронка после повтора
    await page.waitForTimeout(800);
  } finally {
    await stubControl('config', { refresh_delay_ms: 0 });
  }
  expect(unauthorized.length, '>=2 параллельных 401 — иначе single-flight не проверен')
    .toBeGreaterThanOrEqual(2);
  expect(refreshes).toHaveLength(1);
  const rotated = (await (await refreshes[0].response())!.json()).refresh_token;
  expect((await state()).refresh_reuse).toBe(reuseBefore);
  expect(await refreshTokenOf(page)).toBe(rotated);
  expect(rotated).not.toBe(before);
  await expectApp(page);
});

test('перезагрузка: сессия восстанавливается обменом refresh-токена, без 401', async ({ browser }) => {
  const page = await openApp(browser);
  await loginAs(page, STUB_USERS.admin);
  await expectApp(page);
  const before = await refreshTokenOf(page);
  const calls = recordApi(page);
  const statuses: number[] = [];
  page.on('response', r => { if (r.url().includes(`${API}/auth/`)) statuses.push(r.status()); });
  await page.reload();
  await expectApp(page);
  expect(calls.filter(c => c.includes('/auth/')).slice(0, 2))
    .toEqual(['POST /auth/refresh', 'GET /auth/me']);
  expect(statuses).not.toContain(401);
  expect(await refreshTokenOf(page)).not.toBe(before);
});

test('две вкладки продлевают сессию по очереди — семейство не отзывается', async ({ browser }) => {
  const ctx = await browser.newContext();
  contexts.push(ctx);
  const p1 = await openApp(browser, ctx);
  await loginAs(p1, STUB_USERS.admin);
  await expectApp(p1);
  const p2 = await openApp(browser, ctx);          // общий localStorage → restore
  await expectApp(p2);
  const okBefore = (await state()).refresh_ok;
  const reuseBefore = (await state()).refresh_reuse;

  await stubControl('config', { refresh_delay_ms: 500 });
  await stubControl('expire-access', { email: STUB_USERS.admin.email });
  try {
    await Promise.all([
      p1.locator('aside >> text=Аналитика').first().click(),
      p2.locator('aside >> text=Эксперименты').first().click(),
    ]);
    await expect(p1.getByText('16К').first()).toBeVisible();
    await expect(p2.getByText('LightGBM vs SVD-baseline (ranking)').first()).toBeVisible();
  } finally {
    await stubControl('config', { refresh_delay_ms: 0 });
  }
  const after = await state();
  expect(after.refresh_reuse).toBe(reuseBefore);
  expect(after.refresh_ok - okBefore).toBe(2);
  await expectApp(p1);
  await expectApp(p2);
});

test('отозванная сессия (refresh → 401) → страница входа без тостов', async ({ browser }) => {
  const page = await openApp(browser);
  await loginQuiet(page, STUB_USERS.admin);
  // Роль/статус/пароль сменили в БД — token_version вырос, токены мертвы.
  await stubControl(`users/${STUB_USERS.admin.email}`, { bump_token_version: true });
  await page.locator('aside >> text=Эксперименты').first().click();
  await expect(loginForm(page)).toBeVisible();
  expect(await refreshTokenOf(page)).toBeNull();
  await expect(page.getByText(/требуется вход/)).toHaveCount(0);
});

test('выход: POST /auth/logout с текущим refresh-токеном, затем очистка', async ({ browser }) => {
  const page = await openApp(browser);
  await loginAs(page, STUB_USERS.admin);
  await expectApp(page);
  const token = await refreshTokenOf(page);
  expect(token).toBeTruthy();
  const logout = page.waitForRequest(r => r.url().endsWith('/auth/logout'));
  await page.locator('header').getByText('Аристарх').click();
  await page.getByText('Выйти').click();
  expect((await logout).postDataJSON()).toEqual({ refresh_token: token });
  await expect(loginForm(page)).toBeVisible();
  expect(await refreshTokenOf(page)).toBeNull();
  // Семейство отозвано на сервере: украденная копия токена бесполезна.
  expect((await directRefresh(token as string)).status).toBe(401);
});

// ── SSE ──────────────────────────────────────────────────────────────────────
test('SSE: поток только по sse-токену; обрыв → свежий токен и переподключение', async ({ browser }) => {
  expect((await stubApi('/events/stream')).status).toBe(401);
  expect((await stubApi('/events/stream?token=forged')).status).toBe(401);

  const page = await openApp(browser);
  expect((await page.request.get(`${API}/events/stream`)).status()).toBe(401);
  const issued: string[] = [];
  const streamed: string[] = [];
  page.on('response', r => {
    if (r.url().endsWith('/events/token') && r.status() === 200) {
      r.json().then(b => issued.push(b.token)).catch(() => {});
    }
  });
  page.on('request', r => {
    const u = new URL(r.url());
    if (u.pathname === `${API}/events/stream`) streamed.push(u.searchParams.get('token') ?? '');
  });

  await loginAs(page, STUB_USERS.admin);
  await expectApp(page);
  await expect(page.getByText(/обновлено \d+ с назад/)).toBeVisible({ timeout: 6000 });
  await expect.poll(() => streamed.length).toBe(1);
  expect(issued).toHaveLength(1);
  expect(streamed[0]).toBe(issued[0]);

  await stubControl('sse/drop', {});
  await expect.poll(() => streamed.length, { timeout: 10_000 }).toBe(2);
  expect(issued).toHaveLength(2);
  expect(streamed[1]).toBe(issued[1]);
  expect(streamed[1]).not.toBe(streamed[0]);
  await expect.poll(async () => (await state()).sse_connects.slice(-1)[0]).toBe(issued[1]);
});

// ── ML-объяснения (B27) ──────────────────────────────────────────────────────
test('ML-объяснения: campaign_manager /ml/explain, в recommendation_api — ни запроса', async ({ browser }) => {
  const page = await openApp(browser);
  const recCalls: string[] = [];
  page.on('request', r => {
    if (new URL(r.url()).pathname.startsWith('/api/recommendations/')) recCalls.push(r.url());
  });
  const explainReq = page.waitForRequest(r => new URL(r.url()).pathname.startsWith(`${API}/ml/explain/`));
  await loginAs(page, STUB_USERS.admin);
  await expectApp(page);
  await page.locator('aside >> text=ML-объяснения').first().click();
  await expect(page.getByText('Force plot — вклад факторов')).toBeVisible();
  expect(new URL((await explainReq).url()).searchParams.get('top_k')).toBe('5');

  const input = page.locator('input[placeholder*="UUID"]');
  await input.fill('ffffffff-ffff-ffff-ffff-ffffffffffff');      // rec_api недоступен → 502
  await page.getByText('Получить SHAP-объяснение').click();
  await expect(page.getByText('ML API недоступен — объяснение не получено').first()).toBeVisible();
  await input.fill('00000000-0000-0000-0000-000000000000');      // 404 насквозь
  await page.getByText('Получить SHAP-объяснение').click();
  await expect(page.getByText(/не найден в feature store/).first()).toBeVisible();

  expect(recCalls).toEqual([]);
  const st = await state();
  expect(st.rec_calls).toBe(0);
  expect(st.explain_calls).toEqual(expect.arrayContaining([
    'ffffffff-ffff-ffff-ffff-ffffffffffff', '00000000-0000-0000-0000-000000000000',
  ]));
});

// ── Пользователи: правила для ADMIN-аккаунтов ────────────────────────────────
test('делегированное право users не даёт трогать ADMIN и матрицу прав', async ({ browser }) => {
  await stubControl('role-permissions/MARKETER', { users: true });
  try {
    const page = await openApp(browser);
    const calls = recordApi(page);
    await loginAs(page, STUB_USERS.marketer);
    await expectApp(page);
    // ML-лимиты читаются по праву analytics, которого у маркетолога нет.
    await expect(page.locator('aside').getByText('ML-лимиты')).toHaveCount(0);
    await page.locator('aside >> text=Пользователи').first().click();
    // Ростер — из /auth/users (bootstrap@bank.ru есть только в стабе).
    await expect(page.getByText('bootstrap@bank.ru')).toBeVisible();

    const adminRow = page.locator('tr', { hasText: STUB_USERS.admin.email });
    await expect(adminRow.locator('select')).toHaveCount(0);
    await expect(adminRow.locator('button[title="Редактировать"]')).toHaveCount(0);
    await expect(adminRow.locator('button[title="Удалить"]')).toHaveCount(0);
    const analystRow = page.locator('tr', { hasText: STUB_USERS.analyst.email });
    await expect(analystRow.locator('select option')).toHaveText(['Маркетолог', 'Аналитик']);

    await page.getByRole('button', { name: /Добавить/ }).click();
    const modal = page.locator('div', { hasText: /^Добавить пользователя/ }).last();
    await expect(page.getByText('Маркетолог', { exact: true }).last()).toBeVisible();
    await expect(modal.getByText('Администратор', { exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Отмена' }).click();

    await page.getByText('Матрица прав').click();
    await expect(page.getByText(/Матрицу прав меняет только администратор/)).toBeVisible();
    const row = page.locator('tr', { hasText: 'Кампании — создание' });
    await row.locator('td').nth(3).locator('div').first().click();
    await page.waitForTimeout(500);
    expect(calls.filter(c => c.startsWith('PATCH /roles/'))).toEqual([]);
    // Аналитика маркетологу закрыта — UI её и не запрашивает (нет 403-тостов).
    expect(calls.filter(c => c.includes('/analytics/') || c === 'GET /ml-limits')).toEqual([]);
    await expect(page.getByText('Недостаточно прав')).toHaveCount(0);
  } finally {
    await stubControl('role-permissions/MARKETER', { users: false });
  }
});
