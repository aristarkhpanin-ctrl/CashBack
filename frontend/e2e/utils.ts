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

/**
 * Проводит открытый визард кампании до шага «Ревью» (5 из 5) с валидными
 * полями. Сегмент и категория есть и в mock-, и в reference-справочнике.
 */
export async function fillWizardToReview(page: Page, name: string): Promise<void> {
  await expect(page.getByText(/Шаг 1 из 5/)).toBeVisible();
  await page.getByPlaceholder(/Летний кэшбэк/).fill(name);
  await page.locator('input[type="date"]').first().fill('2026-07-01');
  await page.locator('input[type="date"]').nth(1).fill('2026-07-31');
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
const STUB_PATH = path.join(HERE, 'stub_backend.py');

async function stubIsUp(): Promise<boolean> {
  try {
    const r = await fetch('http://localhost:8002/health/live', {
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
  throw new Error('stub backend did not start on :8002 within 10s');
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
      'На :8002 уже что-то отвечает — demo-спек требует выключенного бэкенда. ' +
      'Остановите стаб/стек и перезапустите тесты.',
    );
  }
}
