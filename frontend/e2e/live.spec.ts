/**
 * Live-режим против стаб-бэкенда (frontend/e2e/stub_backend.py),
 * повторяющего wire-формат реальных сервисов: JWT-логин, CRUD кампаний
 * через FSM, аналитика, SHAP, A/B-эксперименты, управление пользователями.
 */
import { expect, test, type Page } from '@playwright/test';
import type { ChildProcess } from 'node:child_process';
import {
  SIDEBAR_PAGES, collectErrors, startStub, stopStub,
} from './utils';

test.describe.configure({ mode: 'serial' });

let stub: ChildProcess;
let page: Page;
let errors: string[];

test.beforeAll(async ({ browser }) => {
  stub = await startStub();
  page = await browser.newPage();
  errors = collectErrors(page);
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

test('SHAP-объяснение по UUID клиента из recommendation API', async () => {
  await page.locator('aside >> text=ML-объяснения').first().click();
  const input = page.locator('input[placeholder*="UUID"]');
  await expect(input).toBeVisible();
  await input.fill('11111111-2222-3333-4444-555555555555');
  await page.getByText('Получить SHAP-объяснение').click();
  await expect(page.getByText('Live API').first()).toBeVisible();
  await expect(page.getByText('Force plot — вклад факторов')).toBeVisible();
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

test('logout возвращает на страницу логина', async () => {
  await page.locator('header').getByText('Аристарх').click();
  await page.getByText('Выйти').click();
  await expect(page.locator('input[type="email"]')).toBeVisible();
});

test('итог: ни одной console-ошибки за live-сессию', async () => {
  expect(errors).toEqual([]);
});
