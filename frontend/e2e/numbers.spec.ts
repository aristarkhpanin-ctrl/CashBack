/**
 * Этап D «цифры правдивые» (B10, B11, B50, B51, B34) — live-режим против
 * стаб-бэкенда.
 *
 * В live UI не подставляет демо-цифры: у каждого виджета — загрузка, «нет
 * данных» или ошибка с HTTP-статусом и «Повторить». Стаб стартует в режиме
 * STUB_FAIL_ANALYTICS=500 (все /analytics/* отвечают 500, как необработанное
 * исключение FastAPI); прочие сбои и «пустые» ответы включаются через
 * /__stub/config ({fail: {regex пути: статус}, empty: [regex пути]}).
 *
 * Тесты независимы друг от друга (своя страница и свой режим стаба), поэтому
 * не serial: на старом коде падает каждый по отдельности, а не первый.
 */
import {
  expect, test, type Browser, type BrowserContext, type Locator, type Page,
} from '@playwright/test';
import type { ChildProcess } from 'node:child_process';
import {
  STUB_USERS, collectErrors, isoDay, loginAs, openNewCampaignWizard, startStub, stopStub,
  stubApi, stubControl, unexpectedErrors, type StubUser,
} from './utils';

const API = '/api/campaigns';
const ANALYTICS_500 = { '^/analytics/': 500 };

let stub: ChildProcess;
const contexts: BrowserContext[] = [];
const pageErrors = new Map<Page, string[]>();

test.beforeAll(async () => {
  stub = await startStub({ STUB_FAIL_ANALYTICS: '500' });
});

test.beforeEach(async () => {
  // Логинов в файле больше лимита стаба на IP (30 за 15 мин).
  await stubControl('reset-throttle', {});
});

// Каждая страница теста — без console-ошибок (кроме сетевого шума 4xx/5xx).
test.afterEach(async () => {
  for (const [page, errs] of pageErrors) {
    expect(unexpectedErrors(errs), `console errors on ${page.url()}`).toEqual([]);
  }
  pageErrors.clear();
  await Promise.all(contexts.splice(0).map(c => c.close()));
});

test.afterAll(async () => {
  await stopStub(stub);
});

/** Режим стаба: какие пути падают (статус) и какие отвечают «данных нет». */
async function stubMode(mode: { fail?: Record<string, number>; empty?: string[] } = {}) {
  await stubControl('config', { fail: mode.fail ?? {}, empty: mode.empty ?? [] });
}

async function newPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext();
  contexts.push(context);
  const page = await context.newPage();
  pageErrors.set(page, collectErrors(page));
  return page;
}

/** Новая вкладка → логин → live-режим (бейдж LIVE API, а не демо). */
async function openLive(browser: Browser, user: StubUser = STUB_USERS.admin): Promise<Page> {
  const page = await newPage(browser);
  await page.goto('/');
  await loginAs(page, user);
  await expect(page.locator('aside').getByText('Дашборд')).toBeVisible();
  await expect(page.locator('header').getByText('LIVE API')).toBeVisible();
  return page;
}

const nav = (page: Page, label: string) => page.locator(`aside >> text=${label}`).first().click();
// Вкладки страницы (в сайдбаре — тоже кнопки, поэтому только внутри main).
const tab = (page: Page, label: string) =>
  page.locator('main').getByRole('button', { name: label, exact: true }).click();

/** Виджет в состоянии ошибки: HTTP-статус и «Повторить». */
const failed = (scope: Page | Locator, status = 500) =>
  scope.getByRole('alert').filter({ hasText: `Не удалось загрузить данные — HTTP ${status}` });

async function expectNoNaN(page: Page): Promise<void> {
  expect(await page.locator('body').innerText()).not.toContain('NaN');
}

async function adminAuth(): Promise<Record<string, string>> {
  const r = await stubApi('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(STUB_USERS.admin),
  });
  return { Authorization: `Bearer ${(await r.json()).access_token}` };
}

// ── B10/B50: сбой аналитики — ошибка, а не демо-цифры ──────────────────────
test('500 аналитики с самого старта: дашборд — «HTTP 500» + «Повторить», ни одной демо-цифры', async ({ browser }) => {
  // Режим STUB_FAIL_ANALYTICS=500 стаба (без /__stub/config).
  const page = await openLive(browser);
  await expect(failed(page)).toHaveCount(3, { timeout: 10_000 });       // KPI, динамика, тепловая карта
  await expect(page.getByRole('button', { name: 'Повторить' })).toHaveCount(3);
  // KPI не подменены расчётом по кампаниям, ряды — не модельные, карта — не мок.
  await expect(page.getByText('Охват аудитории')).toHaveCount(0);
  await expect(page.getByText(/модельная динамика/)).toHaveCount(0);
  await expect(page.getByRole('columnheader', { name: 'Прем', exact: true })).toHaveCount(0);
  await expect(page.locator('header').getByText('LIVE API')).toBeVisible();
  await expectNoNaN(page);
});

test('500 аналитики: сводка, воронка, матрица, каналы — статус ошибки, без NaN и демо-баннера', async ({ browser }) => {
  await stubMode({ fail: ANALYTICS_500 });
  const page = await openLive(browser);
  await nav(page, 'Аналитика');
  await expect(failed(page)).toHaveCount(2, { timeout: 10_000 });       // сводка + воронка
  await expect(page.getByText('Целевая аудитория')).toHaveCount(0);
  await tab(page, 'Матрица сегмент × категория');
  await expect(failed(page)).toHaveCount(2);
  await tab(page, 'Эффективность каналов');
  await expect(failed(page)).toHaveCount(2);
  await expect(page.getByText(/не накопился|модельные \(демо\)/)).toHaveCount(0);
  await expect(page.getByText('Open rate')).toHaveCount(0);
  await expectNoNaN(page);
});

test('«Повторить» после восстановления сервиса показывает живые цифры', async ({ browser }) => {
  // Дашборд: три виджета в ошибке → сервис ожил → «Повторить» на каждом.
  await stubMode({ fail: ANALYTICS_500 });
  const page = await openLive(browser);
  await expect(failed(page)).toHaveCount(3, { timeout: 10_000 });
  await stubMode();
  for (let i = 3; i > 0; i--) {
    await failed(page).first().getByRole('button', { name: 'Повторить' }).click();
    await expect(failed(page)).toHaveCount(i - 1);
  }
  await expect(page.getByText('1.2М').first()).toBeVisible();                    // reach 1 245 000
  await expect(page.getByText('Охват аудитории')).toBeVisible();

  // Аналитика: воронка (и сводка по ней) и каналы.
  await stubMode({ fail: ANALYTICS_500 });
  await nav(page, 'Аналитика');
  await expect(failed(page)).toHaveCount(2, { timeout: 10_000 });
  await stubMode();
  await failed(page).last().getByRole('button', { name: 'Повторить' }).click();   // воронка
  await expect(page.getByText('16К').first()).toBeVisible();                     // аудитория стаба 16 200
  await expect(failed(page)).toHaveCount(0);
  await tab(page, 'Эффективность каналов');
  await failed(page).getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('Open rate').first()).toBeVisible();
  await expect(failed(page)).toHaveCount(0);
  await expectNoNaN(page);
});

test('загрузка: пока /analytics/kpis не ответил — «Загрузка…», а не цифры', async ({ browser }) => {
  await stubMode();
  const page = await newPage(browser);
  let release!: () => void;
  const held = new Promise<void>(r => { release = r; });
  await page.route(u => u.pathname === `${API}/analytics/kpis`, async route => {
    await held;
    await route.fallback();
  });
  await page.goto('/');
  await loginAs(page, STUB_USERS.admin);
  await expect(page.locator('header').getByText('LIVE API')).toBeVisible();
  await expect(page.getByText('Загрузка…').first()).toBeVisible();
  await expect(page.getByText('Охват аудитории')).toHaveCount(0);
  release();
  await expect(page.getByText('1.2М').first()).toBeVisible();
  await expect(page.getByText('Охват аудитории')).toBeVisible();
});

test('пусто: «нет данных» вместо демо-рядов и нулевых KPI с трендами', async ({ browser }) => {
  await stubMode({ empty: ['^/analytics/'] });
  const page = await openLive(browser);
  await expect(page.getByText('Нет данных за 30 дн.: нет работающих кампаний или доставленных офферов'))
    .toBeVisible();
  await expect(page.getByText('За 30 дн. принятых предложений нет')).toBeVisible();
  await expect(page.getByText('За 30 дн. показов нет', { exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'Прем', exact: true })).toHaveCount(0);

  await nav(page, 'Аналитика');
  await expect(page.getByText(/^За 30 дн\. офферов не доставлено \(целевая аудитория: 16\s200\)$/).first())
    .toBeVisible();
  await tab(page, 'Матрица сегмент × категория');
  await expect(page.getByText('За 30 дн. показов нет — матрица пуста')).toBeVisible();
  await tab(page, 'Эффективность каналов');
  await expect(page.getByText('За 30 дн. офферов не доставлено ни по одному каналу')).toBeVisible();
  await expect(failed(page)).toHaveCount(0);
  await expectNoNaN(page);
});

// ── B11: тренды KPI ─────────────────────────────────────────────────────────
test('KPI-тренды: null → «—», 0 — нейтрально, подпись «vs предыдущие N дней» из trend_window_days', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  await expect(page.getByText('1.2М').first()).toBeVisible();
  // Стаб за 30 дней: reach +8.2, spent 0.0, ctr null.
  await expect(page.getByText('vs предыдущие 30 дней')).toHaveCount(3);
  await expect(page.getByText('vs прошлый месяц')).toHaveCount(0);
  await expect(page.locator('[data-trend="up"]')).toHaveText(['▲ 8.2%']);
  const flat = page.locator('[data-trend="flat"]');
  await expect(flat).toHaveText(['0%']);
  await expect(flat).not.toHaveCSS('color', 'rgb(220, 38, 38)');     // не «красный минус»
  await expect(page.locator('[data-trend="none"]')).toHaveText(['—']);
  await expect(page.getByText('▼ 0%')).toHaveCount(0);

  // 7 дней: база предыдущего окна мала — все тренды null.
  await tab(page, '7 дн.');
  await expect(page.getByText('vs предыдущие 7 дней')).toHaveCount(3);
  await expect(page.locator('[data-trend="none"]')).toHaveText(['—', '—', '—']);
  await expect(page.locator('[data-trend="up"], [data-trend="down"], [data-trend="flat"]')).toHaveCount(0);
});

// ── B33: каналы ─────────────────────────────────────────────────────────────
test('каналы: UNKNOWN — «Канал не записан», open rate < 100%, «Приняли» из API', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  await nav(page, 'Аналитика');
  await tab(page, 'Эффективность каналов');
  const push = page.locator('div', { has: page.getByText('Push', { exact: true }) })
    .filter({ hasText: 'Open rate' }).last();
  await expect(push.getByText('44.0%')).toBeVisible();               // 1850 / 4200
  await expect(push.getByText('900', { exact: true })).toBeVisible(); // accepted
  await expect(page.getByText('Канал не записан').first()).toBeVisible();
  await expect(page.getByText('100.0%')).toHaveCount(0);
  await expectNoNaN(page);
});

// ── B34: A/B — вердикт по направлению ──────────────────────────────────────
test('A/B: тест лучше / тест хуже — не катить / разница не значима — по winner', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  await nav(page, 'Эксперименты');
  // LightGBM vs SVD: 8.2% → 12.0%, значимо, winner=treatment.
  await page.getByText('LightGBM vs SVD-baseline (ranking)').first().click();
  await expect(page.getByText(/тест лучше — вариант «treatment» превосходит контроль/)).toBeVisible();

  // Вечерняя отправка: 12.5% → 9.0%, значимо, winner=control.
  await page.getByText('Push: вечерняя vs утренняя отправка').first().click();
  await expect(page.getByText(/тест хуже — вариант «evening» уступает контролю, не катить/)).toBeVisible();
  await expect(page.getByText(/можно катить в прод/)).toHaveCount(0);
  await expect(page.getByText('Статистически значимо')).toBeVisible();

  // Баннер: 10.0% vs 10.5% на 800 назначений — p ≫ 0.2, winner=null.
  await page.getByText('Баннер оффера: компактный vs полный').first().click();
  await expect(page.getByText('Разница не значима')).toBeVisible();
  await expect(page.getByText(/катить/)).toHaveCount(0);
});

test('A/B: создание эксперимента — контроль явно первый вариант (is_control)', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  await nav(page, 'Эксперименты');
  await page.getByRole('button', { name: /Новый эксперимент/ }).click();
  await page.getByPlaceholder('Например: LightGBM v4 vs v3').fill(`E2E-B34-${Date.now()}`);
  const created = page.waitForRequest(r => new URL(r.url()).pathname === `${API}/experiments`
    && r.method() === 'POST');
  await page.getByRole('button', { name: 'Создать', exact: true }).click();
  const body = (await created).postDataJSON();
  expect(body.variants.map((v: { is_control?: boolean }) => v.is_control)).toEqual([true, false]);
  await expect(page.getByText('Эксперимент создан (статус DRAFT)')).toBeVisible();
});

// ── B51: правка черновика ───────────────────────────────────────────────────
const CAMPAIGN_PATCH = /^\/api\/campaigns\/campaigns\/[0-9a-f-]{36}$/;

function recordCampaignPatches(page: Page): unknown[] {
  const bodies: unknown[] = [];
  page.on('request', r => {
    if (r.method() === 'PATCH' && CAMPAIGN_PATCH.test(new URL(r.url()).pathname)) {
      bodies.push(r.postDataJSON());
    }
  });
  return bodies;
}

/** Черновик с полями, которых визард не показывает (как из API/сидера). */
async function createHiddenFieldsDraft(name: string): Promise<{ id: string; auth: Record<string, string> }> {
  const auth = await adminAuth();
  const r = await stubApi('/campaigns', {
    method: 'POST',
    headers: { ...auth, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name, target_segment_ids: [10], cashback_rate: '4.5',
      min_transaction_amount: '300', budget_total: '250000',
      start_date: `${isoDay(1)}T00:00:00Z`, end_date: `${isoDay(30)}T23:59:59Z`,
      allowed_channels: ['POS'], require_existing_behavior: true,
      rate_tiers: [{ min_amount: '0', rate: '4.5' }, { min_amount: '5000', rate: '6' }],
      mcc_codes: ['5411'], min_tx_amounts: { '5411': '300' },
      daily_limit: '20000', auto_pause: true,
    }),
  });
  expect(r.status).toBe(201);
  return { id: (await r.json()).campaign_id, auth };
}

async function openDraftEditor(page: Page, name: string): Promise<void> {
  await nav(page, 'Дашборд');
  await nav(page, 'Кампании');
  await page.getByText(name).first().click();
  await page.getByRole('button', { name: 'Редактировать', exact: true }).click();
  await expect(page.getByText(/Шаг 1 из 5/)).toBeVisible();
}

const next = (page: Page) => page.getByRole('button', { name: /Далее/ }).click();

test('черновик: PATCH несёт только изменённые поля — скрытые поля и децили не затираются', async ({ browser }) => {
  await stubMode();
  const name = `B51-черновик-${Date.now()}`;
  const { id, auth } = await createHiddenFieldsDraft(name);
  const page = await openLive(browser);
  const patches = recordCampaignPatches(page);

  // 1) Только имя.
  await openDraftEditor(page, name);
  await page.getByPlaceholder(/Летний кэшбэк/).fill(`${name} v2`);
  for (let i = 0; i < 4; i++) await next(page);
  await expect(page.getByText(/Шаг 5 из 5/)).toBeVisible();
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(page.getByText('Кампания обновлена')).toBeVisible();
  expect(patches).toEqual([{ name: `${name} v2` }]);
  let saved = await (await stubApi(`/campaigns/${id}`, { headers: auth })).json();
  expect(saved).toMatchObject({
    name: `${name} v2`, allowed_channels: ['POS'], require_existing_behavior: true,
    rate_tiers: [{ min_amount: '0', rate: '4.5' }, { min_amount: '5000', rate: '6' }],
    target_segment_ids: [10], mcc_codes: ['5411'], min_tx_amounts: { '5411': '300' },
    cashback_rate: '4.5', daily_limit: '20000',
  });

  // 2) Добавили корзину «Массовый»: выбранная «Премиум» не расширяется до [9, 10].
  await expect(page.getByText('Кампания обновлена')).toHaveCount(0);
  await openDraftEditor(page, `${name} v2`);
  await next(page);
  await page.getByText('Массовый', { exact: true }).click();
  for (let i = 0; i < 3; i++) await next(page);
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(page.getByText('Кампания обновлена')).toBeVisible();
  expect(patches[1]).toEqual({ target_segment_ids: [5, 6, 7, 8, 10] });
  saved = await (await stubApi(`/campaigns/${id}`, { headers: auth })).json();
  expect(saved).toMatchObject({ allowed_channels: ['POS'], target_segment_ids: [5, 6, 7, 8, 10] });
});

test('черновик без изменений: «Сохранить черновик» не шлёт PATCH полей', async ({ browser }) => {
  await stubMode();
  const name = `B51-без-правок-${Date.now()}`;
  await createHiddenFieldsDraft(name);
  const page = await openLive(browser);
  const patches = recordCampaignPatches(page);
  await openDraftEditor(page, name);
  for (let i = 0; i < 4; i++) await next(page);
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(page.getByText(/Шаг \d из 5/)).toBeHidden();
  await page.waitForTimeout(300);
  expect(patches).toEqual([]);
});

// ── B51: пользователи ───────────────────────────────────────────────────────
test('пользователи: email в форме правки только для чтения, PATCH без email', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  await nav(page, 'Пользователи');
  const row = page.locator('tr', { hasText: STUB_USERS.analyst.email });
  await row.locator('button[title="Редактировать"]').click();
  const email = page.locator('input[type="email"]');
  await expect(email).toHaveValue(STUB_USERS.analyst.email);
  await expect(email).toHaveJSProperty('readOnly', true);
  await expect(page.getByText('Email — логин пользователя, после создания не меняется')).toBeVisible();

  const patch = page.waitForRequest(r => r.method() === 'PATCH'
    && /^\/api\/campaigns\/auth\/users\/[0-9a-f-]{36}$/.test(new URL(r.url()).pathname));
  await page.getByPlaceholder('Иван Иванов').fill('Дмитрий Иванов (E2E)');
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  const body = (await patch).postDataJSON();
  expect(body).toEqual({ full_name: 'Дмитрий Иванов (E2E)', role: 'ANALYST' });
  await expect(page.getByText('Данные пользователя обновлены')).toBeVisible();
  await expect(row.getByText('Дмитрий Иванов (E2E)')).toBeVisible();
});

test('роль с правом users без campaigns_view видит живой ростер, а не мок', async ({ browser }) => {
  await stubMode();
  await stubControl('role-permissions/MARKETER', { users: true, campaigns_view: false });
  try {
    const page = await newPage(browser);
    const calls: string[] = [];
    page.on('request', r => {
      const u = new URL(r.url());
      if (u.pathname.startsWith(`${API}/`)) calls.push(`${r.method()} ${u.pathname.slice(API.length)}`);
    });
    await page.goto('/');
    await loginAs(page, STUB_USERS.marketer);
    await expect(page.locator('header').getByText('LIVE API')).toBeVisible();
    await expect(page.getByText('нет права на просмотр кампаний')).toBeVisible();
    await nav(page, 'Пользователи');
    await expect(page.getByText('bootstrap@bank.ru')).toBeVisible();      // есть только в стабе
    await expect(page.getByText('o.novikova@bank.ru')).toHaveCount(0);    // есть только в моке
    await expect(page.getByText('Недостаточно прав')).toHaveCount(0);
    expect(calls.filter(c => c.startsWith('GET /campaigns'))).toEqual([]);
  } finally {
    await stubControl('role-permissions/MARKETER', { users: false, campaigns_view: true });
  }
});

test('ростер пользователей и матрица прав: 500 → «HTTP 500» + «Повторить», не демо', async ({ browser }) => {
  await stubMode({ fail: { '^/auth/users$': 500, '^/roles/permissions$': 500 } });
  const page = await openLive(browser);
  await nav(page, 'Пользователи');
  await expect(failed(page)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText('o.novikova@bank.ru')).toHaveCount(0);
  await tab(page, 'Матрица прав');
  await expect(failed(page)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText('Право доступа')).toHaveCount(0);           // заголовок мок-матрицы

  await stubMode();
  await failed(page).getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('Право доступа')).toBeVisible();
  await tab(page, 'Пользователи');
  await failed(page).getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('bootstrap@bank.ru')).toBeVisible();
});

// ── B10: прочие live-виджеты ────────────────────────────────────────────────
test('ML-лимиты: ошибка чтения — «HTTP 500», а не форма с дефолтами', async ({ browser }) => {
  await stubMode({ fail: { '^/ml-limits$': 500 } });
  const page = await openLive(browser);
  await nav(page, 'ML-лимиты');
  await expect(failed(page)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/ML-рекомендации (включены|отключены) глобально/)).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Сохранить', exact: true })).toHaveCount(0);
  await stubMode();
  await failed(page).getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('ML-рекомендации включены глобально')).toBeVisible();
});

test('эксперименты: 500 списка — ошибка, а не «Экспериментов пока нет»', async ({ browser }) => {
  await stubMode({ fail: { '^/experiments$': 500 } });
  const page = await openLive(browser);
  await nav(page, 'Эксперименты');
  await expect(failed(page)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText('Экспериментов пока нет')).toHaveCount(0);
  await stubMode();
  await failed(page).getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('LightGBM vs SVD-baseline (ranking)').first()).toBeVisible();
});

test('кампании: 500 списка — ошибка + «Повторить», UI остаётся live (без мок-кампаний)', async ({ browser }) => {
  await stubMode({ fail: { '^/campaigns$': 500 } });
  // SSE stats-событие само перезапрашивает список — «Повторить» проверяем без него.
  await stubControl('config', { sse_stats_event: false });
  try {
    const page = await openLive(browser);
    await expect(page.getByText('список кампаний не загружен')).toBeVisible({ timeout: 10_000 });
    await nav(page, 'Кампании');
    await expect(failed(page)).toBeVisible();
    await expect(page.getByText('Кампании не найдены')).toHaveCount(0);
    await expect(page.getByText('Летний кэшбэк — Супермаркеты')).toHaveCount(0);
    await expect(page.locator('header').getByText('LIVE API')).toBeVisible();
    await stubMode();
    await failed(page).getByRole('button', { name: 'Повторить' }).click();
    await expect(page.getByText('Супермаркеты 5% (live)').first()).toBeVisible();
  } finally {
    await stubControl('config', { sse_stats_event: true });
  }
});

// ── B51/B50: ML-объяснения ──────────────────────────────────────────────────
test('ML-объяснения: SHAP в лог-шансах — значения и подписи не в «%»', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  await nav(page, 'ML-объяснения');
  await expect(page.getByText('Force plot — вклад факторов')).toBeVisible();
  await expect(page.getByText(/Вклады SHAP — в лог-шансах принятия/)).toBeVisible();
  await expect(page.getByText('ось: лог-шансы (logit), под ней — вероятность')).toBeVisible();
  await expect(page.getByText('Вклад, лог-шансы')).toBeVisible();
  // top_factors стаба: mcc_5411_cnt_90d = 0.21 (лог-шансы), не «+21.0%».
  await expect(page.locator('[data-shap-value]').first()).toHaveText('+0.210');
  await expect(page.getByText('+21.0%')).toHaveCount(0);
  // Вероятности (база 18% и предсказание 78%) по-прежнему в процентах.
  await expect(page.getByText('База 18%')).toBeVisible();
  await expect(page.getByText('= 78%')).toBeVisible();
});

test('ML-объяснения: пустой и упавший /ml/customers — не мок-клиенты', async ({ browser }) => {
  await stubMode({ empty: ['^/ml/customers$'] });
  let page = await openLive(browser);
  await nav(page, 'ML-объяснения');
  await expect(page.getByText(/Клиентов с рекомендациями пока нет/)).toBeVisible();
  await expect(page.getByText('Иван Кузнецов')).toHaveCount(0);
  // Ручной lookup по UUID работает и без ростера.
  await page.locator('input[placeholder*="UUID"]').fill('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
  await page.getByText('Получить SHAP-объяснение').click();
  await expect(page.getByText('Force plot — вклад факторов')).toBeVisible();

  await stubMode({ fail: { '^/ml/customers$': 500 } });
  page = await openLive(browser);
  await nav(page, 'ML-объяснения');
  await expect(failed(page)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText('Иван Кузнецов')).toHaveCount(0);
});

// ── B10: статистика кампаний, справочник сегментов, счётчик в сайдбаре ─────
const STATS_500 = { '^/campaigns/[0-9a-f-]{36}/stats$': 500 };

test('статистика кампаний: 500 — «—» и ошибка с «Повторить», а не «Охват 0»; черновик не удалить', async ({ browser }) => {
  await stubMode({ fail: STATS_500 });
  // SSE stats-событие само перезапрашивает список — «Повторить» проверяем без него.
  await stubControl('config', { sse_stats_event: false });
  try {
    const page = await openLive(browser);
    // Дашборд: таблица кампаний — ошибка статистики, CTR/ROI не нули.
    await expect(failed(page.locator('[data-widget="campaign-stats"]'))).toBeVisible({ timeout: 10_000 });
    const row = page.getByRole('row', { name: /Супермаркеты 5% \(live\)/ });
    await expect(row.locator('td').nth(3)).toHaveText('—');                    // CTR
    await expect(row.locator('td').nth(4)).toHaveText('—');                    // ROI

    await nav(page, 'Кампании');
    const stats = page.locator('[data-widget="campaign-stats"]');
    await expect(failed(stats)).toContainText('статистика кампаний: охват, CTR, ROI');
    await expect(page.locator('[data-stat="reach"]').first()).toHaveText('—');
    expect(new Set(await page.locator('[data-stat="reach"]').allInnerTexts())).toEqual(new Set(['—']));
    expect(new Set(await page.locator('[data-stat="roi"]').allInnerTexts())).toEqual(new Set(['—']));
    // История черновика неизвестна — удаление не предлагается.
    await page.getByText('АЗС черновик (live)').first().click();
    await expect(page.getByText('— (статистика не загружена)')).toBeVisible();
    await expect(page.getByText(/Статистика черновика не загружена/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Удалить кампанию' })).toHaveCount(0);

    // Сервис ожил: «Повторить» — живые цифры, и в открытой карточке тоже.
    await stubMode();
    await failed(stats).getByRole('button', { name: 'Повторить' }).click();
    await expect(stats).toHaveCount(0);
    // Охват — различные клиенты из stats.reach (7 200), а не офферы (12 000).
    await expect(page.locator('[data-stat="reach"]').first()).toHaveText('7К');
    await expect(page.getByText('— (статистика не загружена)')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Удалить кампанию' })).toBeVisible();
    await expectNoNaN(page);
  } finally {
    await stubControl('config', { sse_stats_event: true });
  }
});

test('справочник сегментов: 500 — фильтр, ML-лимиты и шаг аудитории без демо-размеров сегментов', async ({ browser }) => {
  await stubMode({ fail: { '^/reference/segments$': 500 } });
  const page = await openLive(browser);
  await nav(page, 'Аналитика');
  await expect(page.getByText(/сегменты не загружены — HTTP 500/)).toBeVisible({ timeout: 10_000 });
  const segmentFilter = page.locator('select', { has: page.locator('option', { hasText: 'Все сегменты' }) });
  await expect(segmentFilter.locator('option')).toHaveCount(1);

  await nav(page, 'ML-лимиты');
  await expect(failed(page.locator('[data-widget="reference-segments"]'))).toBeVisible();
  await expect(page.getByText(/^— клиентов · текущий лимит/)).toHaveCount(5);

  await openNewCampaignWizard(page);
  await page.getByPlaceholder(/Летний кэшбэк/).fill('B10-сегменты');
  await page.locator('input[type="date"]').first().fill(isoDay(0));
  await page.locator('input[type="date"]').nth(1).fill(isoDay(30));
  await next(page);
  const ref = page.locator('[data-widget="reference-segments"]');
  await expect(failed(ref)).toBeVisible();
  await expect(page.getByText(/Клиентов:/)).toHaveCount(0);                  // демо-размеры
  await expect(page.getByText('Предварительный охват аудитории')).toHaveCount(0);

  await stubMode();
  await failed(ref).getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByText('Клиентов: 125К')).toBeVisible();              // из API
  await expect(page.getByText('Предварительный охват аудитории')).toBeVisible();
  await expectNoNaN(page);
});

test('сайдбар: список кампаний не загружен — без счётчика «0», загружен — число активных', async ({ browser }) => {
  await stubMode({ fail: { '^/campaigns$': 500 } });
  await stubControl('config', { sse_stats_event: false });
  try {
    const page = await openLive(browser);
    const item = page.locator('aside').getByRole('button', { name: /^Кампании/ });
    await expect(page.getByText('список кампаний не загружен')).toBeVisible({ timeout: 10_000 });
    await expect(item).toHaveText(/^\s*Кампании\s*$/);
    await stubMode();
    await nav(page, 'Кампании');
    await failed(page).getByRole('button', { name: 'Повторить' }).click();
    await expect(item).toHaveText(/^\s*Кампании\s*1\s*$/);                  // одна ACTIVE в стабе
  } finally {
    await stubControl('config', { sse_stats_event: true });
  }
});

// ── B33: сводка аналитики и матрица — те же данные, что и под ними ─────────
test('аналитика: /kpis без работающих кампаний (has_data=false) — сводка всё равно из воронки', async ({ browser }) => {
  // Как сервис, когда офферы окна есть только у завершённой кампании.
  await stubMode({ empty: ['^/analytics/kpis$'] });
  const page = await openLive(browser);
  await nav(page, 'Аналитика');
  await expect(page.getByText('16К').first()).toBeVisible();                 // аудитория воронки
  await expect(page.getByText('12.0% от целевой аудитории')).toBeVisible();  // 1 944 из 16 200
  await expect(page.getByText('нет данных')).toHaveCount(0);
});

test('аналитика: матрица выбранной кампании запрашивается с её campaign_id', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  const auth = await adminAuth();
  const list = await (await stubApi('/campaigns', { headers: auth })).json();
  const live = list.find((c: { name: string }) => c.name === 'Супермаркеты 5% (live)');
  await nav(page, 'Аналитика');
  await tab(page, 'Матрица сегмент × категория');
  await expect(page.locator('main table tbody tr')).toHaveCount(3);         // все кампании: 3 MCC
  const matrix = page.waitForRequest(r => new URL(r.url()).pathname === `${API}/analytics/segment-matrix`
    && new URL(r.url()).searchParams.get('campaign_id') === live.campaign_id, { timeout: 10_000 });
  await page.locator('select').first().selectOption({ label: 'Супермаркеты 5% (live)' });
  await matrix;
  await expect(page.getByText(/^Супермаркеты 5% \(live\) · 30 дней — показатель/)).toBeVisible();
  await expect(page.locator('main table tbody tr')).toHaveCount(1);         // только MCC кампании
  await expect(page.locator('main table tbody tr')).toContainText('Супермаркеты');
});

test('«Израсходовано»: подпись тренда — начисления окна, а не расход за всё время', async ({ browser }) => {
  await stubMode();
  const page = await openLive(browser);
  await expect(page.getByText('1.2М').first()).toBeVisible();
  await expect(page.getByText('начисления за 30 дн. vs предыдущие 30 дней')).toBeVisible();
  await tab(page, '7 дн.');
  await expect(page.getByText('начисления за 7 дн. vs предыдущие 7 дней')).toBeVisible();
});

// ── B51: мин. суммы категорий при правке черновика ──────────────────────────
async function createDraft(name: string, extra: Record<string, unknown>): Promise<{ id: string; auth: Record<string, string> }> {
  const auth = await adminAuth();
  const r = await stubApi('/campaigns', {
    method: 'POST',
    headers: { ...auth, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name, target_segment_ids: [10], cashback_rate: '4.5', budget_total: '250000',
      start_date: `${isoDay(1)}T00:00:00Z`, end_date: `${isoDay(30)}T23:59:59Z`,
      allowed_channels: ['POS'], daily_limit: '20000', auto_pause: true, ...extra,
    }),
  });
  expect(r.status).toBe(201);
  return { id: (await r.json()).campaign_id, auth };
}

const minInputs = (page: Page) => page.locator('input[type="number"]');

test('черновик с разными мин. суммами категорий: показаны свои суммы, добавление категории их не затирает', async ({ browser }) => {
  await stubMode();
  const name = `B51-минимумы-${Date.now()}`;
  const { id, auth } = await createDraft(name, {
    min_transaction_amount: '300', mcc_codes: ['5411', '5812'],
    min_tx_amounts: { '5411': '300', '5812': '700' },
  });
  const page = await openLive(browser);
  const patches = recordCampaignPatches(page);
  await openDraftEditor(page, name);
  await next(page);
  await next(page);                                                          // → категории
  await expect(minInputs(page)).toHaveCount(2);
  expect(await minInputs(page).evaluateAll(els => els.map(e => (e as HTMLInputElement).value)))
    .toEqual(['300', '700']);
  await page.getByText('Аптеки', { exact: true }).first().click();           // + 5912
  await next(page);
  await next(page);
  await expect(page.getByText('от ₽300')).toBeVisible();
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(page.getByText('Кампания обновлена')).toBeVisible();
  expect(patches).toEqual([{
    mcc_codes: ['5411', '5812', '5912'],
    min_tx_amounts: { '5411': '300', '5812': '700', '5912': '500' },
    min_transaction_amount: '300',
  }]);
  const saved = await (await stubApi(`/campaigns/${id}`, { headers: auth })).json();
  expect(saved.min_tx_amounts).toEqual({ '5411': '300', '5812': '700', '5912': '500' });
});

test('черновик без мин. суммы: поле показывает сохранённое значение (0), а не ₽500', async ({ browser }) => {
  await stubMode();
  const name = `B51-без-минимума-${Date.now()}`;
  await createDraft(name, { min_transaction_amount: null, mcc_codes: ['5411'] });
  const page = await openLive(browser);
  const patches = recordCampaignPatches(page);
  await openDraftEditor(page, name);
  await next(page);
  await next(page);
  await expect(minInputs(page)).toHaveValue('0');
  await next(page);
  await next(page);
  await expect(page.getByText('от ₽0')).toBeVisible();
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(page.getByText(/Шаг \d из 5/)).toBeHidden();
  await page.waitForTimeout(300);
  expect(patches).toEqual([]);                                               // ничего не менял
});

test('черновик: очищенная мин. сумма категории — 0 и на ревью, и в PATCH', async ({ browser }) => {
  await stubMode();
  const name = `B51-очищено-${Date.now()}`;
  const { id, auth } = await createDraft(name, {
    min_transaction_amount: '300', mcc_codes: ['5411', '5812'],
    min_tx_amounts: { '5411': '300', '5812': '700' },
  });
  const page = await openLive(browser);
  const patches = recordCampaignPatches(page);
  await openDraftEditor(page, name);
  await next(page);
  await next(page);                                                          // → категории
  await minInputs(page).nth(1).fill('');                                     // 5812: без минимума
  await expect(minInputs(page).nth(1)).toHaveValue('0');
  await next(page);
  await next(page);
  await expect(page.getByText('от ₽0')).toBeVisible();
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(page.getByText('Кампания обновлена')).toBeVisible();
  expect(patches).toEqual([{
    mcc_codes: ['5411', '5812'],
    min_tx_amounts: { '5411': '300', '5812': '0' },
    min_transaction_amount: '0',
  }]);
  const saved = await (await stubApi(`/campaigns/${id}`, { headers: auth })).json();
  expect(saved.min_tx_amounts).toEqual({ '5411': '300', '5812': '0' });
});

// ── Стаб повторяет §2–§3 сервиса (форма ответов, на которую опирается UI) ──
test('стаб: KPI несут trend_window_days и null-тренды; A/B — is_control, winner, 409/422', async () => {
  await stubMode();
  const auth = await adminAuth();
  const json = { ...auth, 'Content-Type': 'application/json' };
  const kpis7 = await (await stubApi('/analytics/kpis?period=7', { headers: auth })).json();
  expect(kpis7).toMatchObject({ trend_window_days: 7, trends: { reach: null, spent: null, ctr: null } });

  const exps = await (await stubApi('/experiments', { headers: auth })).json();
  const worse = exps.find((e: { name: string }) => e.name === 'Push: вечерняя vs утренняя отправка');
  expect(worse.variants.map((v: { is_control: boolean }) => v.is_control)).toEqual([true, false]);
  const res = await (await stubApi(`/experiments/${worse.experiment_id}/results`, { headers: auth })).json();
  expect(res).toMatchObject({ significance: 'significant', winner: 'control' });
  expect(res.p_value).toBeGreaterThanOrEqual(0);
  expect(res.p_value).toBeLessThanOrEqual(1);

  const variant = (name: string, extra = {}) =>
    ({ name, traffic_weight: 0.5, strategy_class: 'S', ...extra });
  const create = (body: unknown) => stubApi('/experiments', { method: 'POST', headers: json, body: JSON.stringify(body) });
  const base = { target_metric: 'acceptance_rate', start_date: new Date().toISOString(), end_date: null };
  const twoControls = await create({ ...base, name: `two-${Date.now()}`,
    variants: [variant('a', { is_control: true }), variant('b', { is_control: true })] });
  expect(twoControls.status).toBe(422);
  const name = `stub-contract-${Date.now()}`;
  const ok = await create({ ...base, name, variants: [variant('a'), variant('b')] });
  expect(ok.status).toBe(201);
  const exp = await ok.json();
  expect(exp.variants.map((v: { is_control: boolean }) => v.is_control)).toEqual([true, false]);
  expect((await create({ ...base, name, variants: [variant('a'), variant('b')] })).status).toBe(409);

  const assign = () => stubApi(`/experiments/${exp.experiment_id}/assign/u-1`, { method: 'POST', headers: auth });
  expect((await assign()).status).toBe(409);                                     // DRAFT
  await stubApi(`/experiments/${exp.experiment_id}/status?action=start`, { method: 'PATCH', headers: auth });
  const first = await (await assign()).json();
  const again = await (await assign()).json();
  expect(again.variant_id).toBe(first.variant_id);                               // идемпотентно
});
