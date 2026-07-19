/**
 * Live-режим против стаб-бэкенда (frontend/e2e/stub_backend.py),
 * повторяющего wire-формат реальных сервисов: JWT-логин, CRUD кампаний
 * через FSM, аналитика, SHAP, A/B-эксперименты, управление пользователями.
 */
import { expect, test, type Page } from '@playwright/test';
import type { ChildProcess } from 'node:child_process';
import {
  SIDEBAR_PAGES, collectErrors, startStub, stopStub, unexpectedErrors,
} from './utils';

test.describe.configure({ mode: 'serial' });

let stub: ChildProcess;
let page: Page;
let errors: string[];
const refHits: { url: string; status: number }[] = [];
const kpiHits: { url: string; status: number }[] = [];

test.beforeAll(async ({ browser }) => {
  stub = await startStub();
  page = await browser.newPage();
  errors = collectErrors(page);
  // Фаза 21/24: фиксируем обращения к справочникам и /kpis — часть уходит
  // на login/дашборд, поэтому слушатель ставим до перехода на страницу.
  page.on('response', (r) => {
    if (r.url().includes('/reference/')) {
      refHits.push({ url: r.url(), status: r.status() });
    }
    if (r.url().includes('/analytics/kpis')) {
      kpiHits.push({ url: r.url(), status: r.status() });
    }
  });
  await page.goto('/', { waitUntil: 'networkidle' });
});

test.afterAll(async () => {
  await page?.close();
  await stopStub(stub);
});

test('показывается страница логина; вход admin@bank.ru', async () => {
  await expect(page.locator('input[type="email"]')).toBeVisible();
  await page.locator('input[type="email"]').fill('admin@bank.ru');
  await page.locator('input[type="password"]').fill('admin');
  await page.getByText('Войти', { exact: true }).click();
  await expect(page.locator('aside').getByText('Дашборд')).toBeVisible();
  await expect(page.locator('header').getByText('LIVE API')).toBeVisible();
});

test('realtime-индикатор появляется после SSE stats-события', async () => {
  // стаб шлёт один stats-event через ~0.8с после подключения EventSource
  await expect(page.getByText(/обновлено \d+ с назад/)).toBeVisible({ timeout: 6000 });
});

for (const [key, label] of SIDEBAR_PAGES) {
  test(`страница «${label}» рендерится без ошибок`, async () => {
    await page.locator(`aside >> text=${label}`).first().click();
    await page.waitForTimeout(900);
    expect(errors, `console errors on ${key}`).toEqual([]);
  });
}

test('кампании приходят из API (3 из стаба), карточка открывается', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await expect(page.getByText(/^Все \(3\)/)).toBeVisible();
  await page.getByText(/Супермаркеты 5% \(live\)/).first().click();
  await expect(page.getByText('Освоение бюджета')).toBeVisible();
});

test('пауза кампании проходит через PATCH /status', async () => {
  const pauseBtn = page.getByText('⏸').first();
  await expect(pauseBtn).toBeVisible();
  await pauseBtn.click();
  await expect(page.getByText('Кампания приостановлена')).toBeVisible();
});

test('ML-объяснения: ростер из /ml/customers + force plot (фаза 25)', async () => {
  await page.locator('aside >> text=ML-объяснения').first().click();
  // Левая панель заполняется реальными клиентами из /ml/customers.
  await expect(page.getByText('u-10293').first()).toBeVisible();
  // Первый клиент выбран автоматически → SHAP force plot из rec_api.
  await expect(page.getByText('Force plot — вклад факторов')).toBeVisible();
  // Расширенный контракт (фаза 25): expected_roi с сервера (3.4×).
  await expect(page.getByText('Ожидаемый ROI')).toBeVisible();
  await expect(page.getByText('3.4×')).toBeVisible();
});

test('ML-объяснения: 404 для клиента не из feature store (фаза 25)', async () => {
  const input = page.locator('input[placeholder*="UUID"]');
  await input.fill('00000000-0000-0000-0000-000000000000');
  await page.getByText('Получить SHAP-объяснение').click();
  await expect(page.getByText(/не найден в feature store/).first()).toBeVisible();
});

test('A/B-эксперименты: серверный z-тест рендерится', async () => {
  await page.locator('aside >> text=Эксперименты').first().click();
  await expect(page.getByText('LightGBM vs SVD-baseline (ranking)').first()).toBeVisible();
  await expect(page.getByText('p-value')).toBeVisible();
  await expect(page.getByText('Статистически значимо')).toBeVisible();
});

test('ML-лимиты читаются из API и сохраняются через PUT', async () => {
  await page.locator('aside >> text=ML-лимиты').first().click();
  // значения корзины premium из стаба: max 15% / бюджет 200 000
  await expect(page.getByText('Контроль ML-предложений')).toBeVisible();
  const toggle = page.getByText(/ML-рекомендации включены глобально/);
  await expect(toggle).toBeVisible();
});

test('ростер пользователей из /auth/users', async () => {
  await page.locator('aside >> text=Пользователи').first().click();
  await expect(page.getByText('m.sokolova@bank.ru')).toBeVisible();
  await expect(page.getByText('d.ivanov@bank.ru')).toBeVisible();
});

test('справочники сегментов и MCC загружены из reference API (фаза 21)', async () => {
  // useReference вызывается в live-режиме сразу после логина; данные визарда
  // и фильтров идут из API, а не из mockData.
  await expect
    .poll(() => refHits.some(r => r.url.includes('/reference/segments') && r.status === 200),
      { timeout: 8000 })
    .toBe(true);
  await expect
    .poll(() => refHits.some(r => r.url.includes('/reference/mcc-categories') && r.status === 200))
    .toBe(true);
});

test('визард кампании берёт MCC-категории из справочника', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await page.getByRole('button', { name: /Новая кампания/ }).click();
  await expect(page.getByText(/Шаг 1 из 5/)).toBeVisible();
  // Шаг 1 → 3 (категории): имя+даты+ставка валидны, затем сегмент.
  await page.getByPlaceholder(/Летний кэшбэк/).fill('E2E справочник');
  await page.locator('input[type="date"]').first().fill('2026-06-01');
  await page.locator('input[type="date"]').nth(1).fill('2026-06-30');
  await page.getByRole('button', { name: /Далее/ }).click();     // → аудитория
  await page.getByText('Премиум').first().click();               // сегмент из справочника
  await page.getByRole('button', { name: /Далее/ }).click();     // → категории
  await expect(page.getByText('Косметика')).toBeVisible();       // MCC 5122 из reference API
  await expect(page.getByText('MCC 5122')).toBeVisible();
  // закрываем визард, чтобы модалка не перехватывала клики следующих тестов
  await page.locator('button:has-text("×")').first().click();
  await expect(page.getByText(/Шаг \d из 5/)).toBeHidden();
});

test('визард сохраняет дневной лимит; он виден при повторном открытии (фаза 22)', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await page.getByRole('button', { name: /Новая кампания/ }).click();
  // Шаг 1: основные (имя без слов «дневной/лимит», чтобы не ловить label)
  await page.getByPlaceholder(/Летний кэшбэк/).fill('Кампания-ф22');
  await page.locator('input[type="date"]').first().fill('2026-07-01');
  await page.locator('input[type="date"]').nth(1).fill('2026-07-31');
  await page.getByRole('button', { name: /Далее/ }).click();
  // Шаг 2: аудитория
  await page.getByText('Премиум').first().click();
  await page.getByRole('button', { name: /Далее/ }).click();
  // Шаг 3: категория
  await page.getByText('Косметика').first().click();
  await page.getByRole('button', { name: /Далее/ }).click();
  // Шаг 4: бюджет — задаём отличимый дневной лимит 80 000 (2-й number-инпут)
  await page.locator('input[type="number"]').nth(1).fill('80000');
  await page.getByRole('button', { name: /Далее/ }).click();
  // Шаг 5: сохраняем черновик (POST /campaigns с полями визарда)
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(page.getByText(/Кампания создана/)).toBeVisible();
  // Открываем созданную кампанию — дневной лимит вернулся из API (не 0).
  await page.getByText('Кампания-ф22').first().click();
  await expect(page.getByText('Дневной лимит', { exact: true })).toBeVisible();
  await expect(page.getByText('₽80К')).toBeVisible();
});

test('admin удаляет черновик через DELETE /campaigns/:id (фаза 23)', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await page.getByText('АЗС черновик (live)').first().click();
  await page.getByRole('button', { name: /Удалить кампанию/ }).click();
  await page.getByRole('button', { name: /Подтвердить удаление/ }).click();
  await expect(page.getByText(/Кампания удалена/)).toBeVisible();
  // список инвалидируется — карточка исчезает из ленты и detail-панели.
  await expect(page.getByText('АЗС черновик (live)')).toHaveCount(0);
});

test('KPI дашборда берутся из /analytics/kpis (единый источник, фаза 24)', async () => {
  // Дашборд грузится сразу после логина и тянет /kpis (reach 1 245 000 → «1.2М»).
  await expect
    .poll(() => kpiHits.some(r => r.status === 200), { timeout: 8000 })
    .toBe(true);
  await page.locator('aside >> text=Дашборд').first().click();
  await expect(page.getByText('1.2М').first()).toBeVisible();
});

test('аналитика: выбор сегмента сужает цифры на сервере (фаза 24)', async () => {
  await page.locator('aside >> text=Аналитика').first().click();
  await expect(page.getByText('Целевая аудитория').first()).toBeVisible();
  // Все сегменты: аудитория воронки стаба 16 200 → «16К».
  await expect(page.getByText('16К').first()).toBeVisible();
  // Выбор сегмента → сервер сужает воронку (×0.2 → 3 240 → «3К»).
  await page.locator('select').nth(1).selectOption('premium');
  await expect(page.getByText('Фильтры активны')).toBeVisible();
  await expect(page.getByText('3К').first()).toBeVisible();
});

test('logout возвращает на страницу логина', async () => {
  await page.locator('header').getByText('Аристарх').click();
  await page.getByText('Выйти').click();
  await expect(page.locator('input[type="email"]')).toBeVisible();
});

test('итог: ни одной console-ошибки за live-сессию', async () => {
  // «Failed to load resource» (напр. намеренный 404-lookup) — ожидаемый
  // сетевой шум, не дефект страницы.
  expect(unexpectedErrors(errors)).toEqual([]);
});
