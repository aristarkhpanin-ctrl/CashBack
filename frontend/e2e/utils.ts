/**
 * Общие помощники E2E: сбор console-ошибок и управление стаб-бэкендом.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, type Page } from '@playwright/test';

// package.json объявляет "type": "module" → __dirname недоступен
const HERE = path.dirname(fileURLToPath(import.meta.url));

export const SIDEBAR_PAGES: Array<[string, string]> = [
  ['dashboard', 'Дашборд'],
  ['campaigns', 'Кампании'],
  ['analytics', 'Аналитика'],
  ['experiments', 'Эксперименты'],
  ['explanations', 'ML-объяснения'],
  ['ml_limits', 'ML-лимиты'],
  ['users', 'Пользователи'],
];

/** Открывает визард новой кампании на «чистой» странице кампаний. */
export async function openNewCampaignWizard(page: Page): Promise<void> {
  // Уход на дашборд и обратно перемонтирует страницу: сбрасывает фильтр и
  // открытую карточку (её «Сегменты» совпали бы с текстом шага аудитории).
  await page.locator('aside >> text=Дашборд').first().click();
  await page.locator('aside >> text=Кампании').first().click();
  await page.getByRole('button', { name: /Новая кампания/ }).click();
  await expect(page.getByText(/Шаг 1 из 5/)).toBeVisible();
}

/** День YYYY-MM-DD через `days` дней от сегодня — по UTC, как бэкенд. */
export function isoDay(days = 0): string {
  return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}

/** «2026-07-01» → «01.07.2026» — так UI пишет даты в тостах. */
export function ruDay(day: string): string {
  return day.split('-').reverse().join('.');
}

/**
 * Проводит открытый визард кампании до шага «Ревью» (5 из 5) с валидными
 * полями. Сегмент и категория есть и в mock-, и в reference-справочнике.
 * Даты — от сегодняшнего дня (по умолчанию кампания уже идёт): кампанию
 * с прошедшей end_date бэкенд не активирует (B25).
 */
export async function fillWizardToReview(
  page: Page, name: string, { startInDays = 0, days = 30 } = {},
): Promise<void> {
  await expect(page.getByText(/Шаг 1 из 5/)).toBeVisible();
  await page.getByPlaceholder(/Летний кэшбэк/).fill(name);
  await page.locator('input[type="date"]').first().fill(isoDay(startInDays));
  await page.locator('input[type="date"]').nth(1).fill(isoDay(startInDays + days));
  await page.getByRole('button', { name: /Далее/ }).click();    // → аудитория
  await page.getByText('Премиум', { exact: true }).click();
  await page.getByRole('button', { name: /Далее/ }).click();    // → категории
  await page.getByText('Косметика', { exact: true }).click();
  await page.getByRole('button', { name: /Далее/ }).click();    // → бюджет
  await page.getByRole('button', { name: /Далее/ }).click();    // → ревью
  await expect(page.getByText(/Шаг 5 из 5/)).toBeVisible();
}

/** Подписка на ошибки страницы; вызвать ДО goto. */
export function collectErrors(page: Page): string[] {
  const sink: string[] = [];
  page.on('console', msg => {
    if (msg.type() === 'error') sink.push(`console: ${msg.text().slice(0, 300)}`);
  });
  page.on('pageerror', err => sink.push(`pageerror: ${String(err).slice(0, 300)}`));
  return sink;
}

/**
 * Health-пинг без бэкенда отдаёт 500 через прокси — это ожидаемый шум
 * демо-режима, не дефект страницы.
 */
export function unexpectedErrors(sink: string[]): string[] {
  return sink.filter(e => !/Failed to load resource/.test(e));
}

// ── Стаб-бэкенд (python3, порты 8001/8002) ──────────────────────────────────
// Порты переопределяемы для локального прогона рядом с живым стеком:
// STUB_CAMPAIGN_PORT/STUB_RECOMMENDATION_PORT + CAMPAIGN_API_TARGET/
// RECOMMENDATION_API_TARGET для vite-прокси.
const STUB_PATH = path.join(HERE, 'stub_backend.py');
export const CAMPAIGN_STUB = `http://localhost:${process.env.STUB_CAMPAIGN_PORT ?? '8002'}`;

/** Учётки стаба (только E2E; встроенного admin/admin больше нет). */
export const STUB_USERS = {
  admin:     { email: 'admin@bank.ru',      password: 'Admin-e2e-Passw0rd' },
  marketer:  { email: 'm.sokolova@bank.ru', password: 'Marketer-e2e-Passw0rd' },
  analyst:   { email: 'd.ivanov@bank.ru',   password: 'Analyst-e2e-Passw0rd' },
  // must_change_password=true — как bootstrap-админ без ADMIN_BOOTSTRAP_PASSWORD
  bootstrap: { email: 'bootstrap@bank.ru',  password: 'Bootstrap-tmp-0001' },
} as const;

export type StubUser = { email: string; password: string };

/** Вход через форму логина (страница уже открыта, бэкенд онлайн). */
export async function loginAs(page: Page, user: StubUser): Promise<void> {
  await expect(page.locator('input[type="email"]')).toBeVisible();
  await page.locator('input[type="email"]').fill(user.email);
  await page.locator('input[type="password"]').fill(user.password);
  await page.getByText('Войти', { exact: true }).click();
}

/** Ручки /__stub/* (управление стабом): GET без body, иначе POST JSON. */
export async function stubControl<T = unknown>(name: string, body?: unknown): Promise<T> {
  const r = await fetch(`${CAMPAIGN_STUB}/__stub/${name}`, body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`stub control ${name}: ${r.status}`);
  return (r.status === 204 ? undefined : await r.json()) as T;
}

/** Прямой вызов API стаба campaign_manager (минуя браузер). */
export function stubApi(pathname: string, init?: RequestInit): Promise<Response> {
  return fetch(`${CAMPAIGN_STUB}${pathname}`, init);
}

async function stubIsUp(): Promise<boolean> {
  try {
    const r = await fetch(`${CAMPAIGN_STUB}/health/live`, {
      signal: AbortSignal.timeout(1500),
    });
    return r.ok;
  } catch {
    return false;
  }
}

export async function startStub(): Promise<ChildProcess> {
  const proc = spawn('python3', [STUB_PATH], { stdio: 'ignore' });
  for (let i = 0; i < 40; i++) {
    if (await stubIsUp()) return proc;
    await new Promise(r => setTimeout(r, 250));
  }
  proc.kill();
  throw new Error(`stub backend did not start on ${CAMPAIGN_STUB} within 10s`);
}

export async function stopStub(proc: ChildProcess | undefined): Promise<void> {
  proc?.kill('SIGTERM');
  for (let i = 0; i < 20; i++) {
    if (!(await stubIsUp())) return;
    await new Promise(r => setTimeout(r, 250));
  }
}

export async function assertStubDown(): Promise<void> {
  if (await stubIsUp()) {
    throw new Error(
      `На ${CAMPAIGN_STUB} уже что-то отвечает — demo-спек требует выключенного бэкенда. ` +
      'Остановите стаб/стек и перезапустите тесты.',
    );
  }
}
