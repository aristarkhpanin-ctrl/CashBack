/**
 * Демо-режим: backend выключен, UI живёт на встроенных mock-данных.
 *
 * Регрессия, от которой защищает этот спек, уже случалась: mockData.ts
 * не попал в коммит — фронтенд не собирался, а CI этого не видел.
 */
import { expect, test, type Page } from '@playwright/test';
import {
  SIDEBAR_PAGES, assertStubDown, collectErrors, fillWizardToReview, openNewCampaignWizard,
  unexpectedErrors,
} from './utils';

test.describe.configure({ mode: 'serial' });

let page: Page;
let errors: string[];

test.beforeAll(async ({ browser }) => {
  await assertStubDown();
  page = await browser.newPage();
  errors = collectErrors(page);
  await page.goto('/', { waitUntil: 'networkidle' });
});

test.afterAll(async () => {
  await page?.close();
});

test('вход не требуется, бейдж — ДЕМО-ДАННЫЕ', async () => {
  await expect(page.locator('aside').getByText('Дашборд')).toBeVisible();
  await expect(page.locator('header').getByText('ДЕМО-ДАННЫЕ')).toBeVisible();
});

for (const [key, label] of SIDEBAR_PAGES) {
  test(`страница «${label}» рендерится без ошибок`, async () => {
    await page.locator(`aside >> text=${label}`).first().click();
    await page.waitForTimeout(900); // анимации графиков / отложенные запросы
    expect(unexpectedErrors(errors), `console errors on ${key}`).toEqual([]);
  });
}

test('mock-кампании на месте (5 штук), карточка открывается', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await expect(page.getByText(/^Все \(5\)/)).toBeVisible();
  await page.getByText(/Летний кэшбэк/).first().click();
  await expect(page.getByText('Освоение бюджета')).toBeVisible();
});

test('мастер кампании открывается на шаге 1', async () => {
  await page.getByText('Новая кампания').first().click();
  await expect(page.getByText('Шаг 1 из 5')).toBeVisible();
  await page.getByText('Отмена').first().click();
});

test('«Запустить кампанию» добавляет в список АКТИВНУЮ кампанию (B46)', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await expect(page.getByText(/^Активные \(2\)/)).toBeVisible();
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Демо-запуск B46');
  await page.getByRole('button', { name: /Запустить кампанию/ }).click();
  await expect(page.getByText(/Шаг \d из 5/)).toBeHidden();
  // Раньше уходил черновик: «Активные» не менялись, кампания была в «Черновиках».
  await expect(page.getByText(/^Активные \(3\)/)).toBeVisible();
  await expect(page.getByText('Кампания создана и запущена')).toBeVisible();
  await page.getByText(/^Активные \(3\)/).click();
  await expect(page.getByText('Демо-запуск B46')).toBeVisible();
});

test('визард не пускает дальше при несогласованных полях (B46)', async () => {
  await openNewCampaignWizard(page);
  const next = page.getByRole('button', { name: /Далее/ });
  // Шаг 1: окончание раньше начала.
  await page.getByPlaceholder(/Летний кэшбэк/).fill('Демо-валидация');
  await page.locator('input[type="date"]').first().fill('2026-07-31');
  await page.locator('input[type="date"]').nth(1).fill('2026-07-01');
  await expect(next).toBeDisabled();
  await expect(page.getByText('Дата окончания раньше даты начала')).toBeVisible();
  await page.locator('input[type="date"]').nth(1).fill('2026-07-31'); // один день — допустимо
  await expect(next).toBeEnabled();
  await next.click();
  // Шаг 2: RFM-квантиль «от» 5 > «до» 1 (range-инпуты — с клавиатуры).
  await page.getByText('Премиум', { exact: true }).click();
  const rfmMin = page.locator('input[type="range"]').first();
  const rfmMax = page.locator('input[type="range"]').nth(1);
  await rfmMin.focus();
  await page.keyboard.press('End');
  await rfmMax.focus();
  await page.keyboard.press('Home');
  await expect(next).toBeDisabled();
  await expect(page.getByText('Квантиль «от» больше квантиля «до»')).toBeVisible();
  await page.keyboard.press('End');
  await expect(next).toBeEnabled();
  await next.click();
  // Шаг 3 → 4: дневной лимит больше бюджета.
  await page.getByText('Косметика', { exact: true }).click();
  await next.click();
  await page.locator('input[type="number"]').first().fill('100000');
  await page.locator('input[type="number"]').nth(1).fill('200000');
  await expect(next).toBeDisabled();
  await expect(page.getByText('Дневной лимит больше общего бюджета')).toBeVisible();
  await page.locator('input[type="number"]').nth(1).fill('50000');
  await expect(next).toBeEnabled();
  await page.locator('button:has-text("×")').first().click();
  await expect(page.getByText(/Шаг \d из 5/)).toBeHidden();
});

test('итог: ни одной неожиданной console-ошибки за сессию', async () => {
  expect(unexpectedErrors(errors)).toEqual([]);
});
