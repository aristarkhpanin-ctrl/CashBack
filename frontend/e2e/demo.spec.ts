/**
 * Демо-режим: backend выключен, UI живёт на встроенных mock-данных.
 *
 * Регрессия, от которой защищает этот спек, уже случалась: mockData.ts
 * не попал в коммит — фронтенд не собирался, а CI этого не видел.
 */
import { expect, test, type Page } from '@playwright/test';
import {
  SIDEBAR_PAGES, assertStubDown, collectErrors, unexpectedErrors,
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

test('итог: ни одной неожиданной console-ошибки за сессию', async () => {
  expect(unexpectedErrors(errors)).toEqual([]);
});
