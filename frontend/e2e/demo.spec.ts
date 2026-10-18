/**
 * Демо-режим: backend выключен, UI живёт на встроенных mock-данных.
 *
 * Регрессия, от которой защищает этот спек, уже случалась: mockData.ts
 * не попал в коммит — фронтенд не собирался, а CI этого не видел.
 */
import { expect, test, type Page } from '@playwright/test';
import {
  SIDEBAR_PAGES, assertStubDown, collectErrors, fillWizardToReview, isoDay, openNewCampaignWizard,
  ruDay, unexpectedErrors,
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
  // Этап C (B22): дневной лимит всегда жёсткий — переключателя «авто-паузы» нет.
  await expect(page.getByText('По достижении дневного лимита кампания не платит до конца суток (UTC)',
                              { exact: false })).toBeVisible();
  await expect(page.getByText(/Авто-приостановка/)).toHaveCount(0);
  await next.click();
  await expect(page.getByText('без выплат до конца суток (UTC)')).toBeVisible();
  await page.locator('button:has-text("×")').first().click();
  await expect(page.getByText(/Шаг \d из 5/)).toBeHidden();
});

// ── Этап C: архив вместо удаления (B24), будущий старт (B25) ─────────────────
const deleteBtn = (p: Page) => p.getByRole('button', { name: 'Удалить кампанию' });
const archiveBtn = (p: Page) => p.getByRole('button', { name: 'В архив', exact: true });

/** Тост страницы кампаний живёт 3 с: следующий ждёт, пока уйдёт прошлый. */
async function waitCampaignToastGone(p: Page): Promise<void> {
  await expect(p.getByText(/Кампания (создана|обновлена|отправлена|активирована|запущена|удалена)/))
    .toHaveCount(0);
}

test('«Удалить» — только у черновика без истории, «В архив» — у приостановленной и завершённой (B24)', async () => {
  await page.locator('aside >> text=Дашборд').first().click();
  await page.locator('aside >> text=Кампании').first().click();
  await page.getByText('Электроника — Бизнес').first().click();          // черновик, расхода нет
  await expect(deleteBtn(page)).toBeVisible();
  await expect(archiveBtn(page)).toHaveCount(0);
  await page.getByText('Летний кэшбэк — Супермаркеты').first().click();  // активна
  await expect(deleteBtn(page)).toHaveCount(0);
  await expect(archiveBtn(page)).toHaveCount(0);
  await expect(page.getByText('Отправить в архив можно после паузы или завершения')).toBeVisible();
  for (const name of ['АЗС — Молодежь и Массовый', 'Рестораны выходного дня']) {  // пауза / завершена
    await page.getByText(name).first().click();
    await expect(archiveBtn(page)).toBeVisible();
    await expect(deleteBtn(page)).toHaveCount(0);
  }
});

test('«В архив»: кампания уходит из списка, «Показать архив» возвращает её с бейджем (B24)', async () => {
  await expect(page.getByText(/^Завершённые \(1\)/)).toBeVisible();
  await page.getByText('Рестораны выходного дня').first().click();
  await archiveBtn(page).click();
  await page.getByRole('button', { name: 'Подтвердить архивацию' }).click();
  await expect(page.getByText('Кампания отправлена в архив')).toBeVisible();
  await expect(page.getByText('Рестораны выходного дня')).toHaveCount(0);
  await expect(page.getByText(/^Завершённые \(0\)/)).toBeVisible();

  const toggle = page.getByLabel('Показать архив');
  await toggle.check();
  await expect(page.getByText(/^Завершённые \(1\)/)).toBeVisible();
  await expect(page.getByText('В архиве', { exact: true })).toHaveCount(1);  // бейдж карточки
  await page.getByText('Рестораны выходного дня').first().click();
  await expect(page.getByText('В архиве с')).toBeVisible();
  // Архивная кампания не меняется: ни правки, ни статусов, ни удаления.
  await expect(page.getByRole('button', { name: 'Редактировать', exact: true })).toHaveCount(0);
  await expect(archiveBtn(page)).toHaveCount(0);
  await toggle.uncheck();
  await expect(page.getByText('Рестораны выходного дня')).toHaveCount(0);
  await expect(page.getByText('В архиве с')).toHaveCount(0);           // карточка закрыта
});

test('запуск кампании с будущим стартом → «запустится ДД.ММ.ГГГГ» (B25)', async () => {
  await waitCampaignToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Демо-будущий-старт', { startInDays: 10 });
  await page.getByRole('button', { name: /Запустить кампанию/ }).click();
  await expect(page.getByText(`Кампания создана и активирована — запустится ${ruDay(isoDay(10))}`))
    .toBeVisible();
  await expect(page.getByText('Кампания создана и запущена')).toHaveCount(0);

  // Тот же признак при запуске черновика из карточки; черновик со стартом в
  // прошлом — обычное «запущена».
  await waitCampaignToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Демо-черновик-будущий', { startInDays: 7 });
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await waitCampaignToastGone(page);
  await page.getByText('Демо-черновик-будущий').first().click();
  await page.getByRole('button', { name: 'Запустить', exact: true }).click();
  await expect(page.getByText(`Кампания активирована — запустится ${ruDay(isoDay(7))}`)).toBeVisible();
  await waitCampaignToastGone(page);
  await page.getByText('Электроника — Бизнес').first().click();         // старт 2026-05-15
  await page.getByRole('button', { name: 'Запустить', exact: true }).click();
  await expect(page.getByText('Кампания запущена')).toBeVisible();
});

// ── Этап B: демо-режим не задет живой аутентификацией ───────────────────────
test('ни сессии, ни запросов к API — даже с refresh-токеном прошлой live-сессии', async ({ browser }) => {
  const ctx = await browser.newContext();
  try {
    await ctx.addInitScript(() => localStorage.setItem('cashback.refresh_token', 'stale-refresh-token'));
    const p = await ctx.newPage();
    const calls: string[] = [];
    p.on('request', r => {
      const u = new URL(r.url());
      if (u.pathname.startsWith('/api/')) calls.push(`${r.method()} ${u.pathname}`);
    });
    await p.goto('/', { waitUntil: 'networkidle' });
    await expect(p.locator('header').getByText('ДЕМО-ДАННЫЕ')).toBeVisible();
    await expect(p.locator('input[type="email"]')).toHaveCount(0);
    for (const [, label] of SIDEBAR_PAGES) {
      await p.locator(`aside >> text=${label}`).first().click();
    }
    await p.waitForTimeout(500);
    // Только health-пинг campaign_manager: ни /auth/*, ни /events/*, ни
    // recommendation_api (объяснения идут через campaign_manager, B27).
    expect(new Set(calls)).toEqual(new Set(['GET /api/campaigns/health/live']));
    expect(await p.evaluate(() => localStorage.getItem('cashback.refresh_token')))
      .toBe('stale-refresh-token');
  } finally {
    await ctx.close();
  }
});

test('ML-объяснения: mock-SHAP без панели live-запроса', async () => {
  await page.locator('aside >> text=ML-объяснения').first().click();
  await expect(page.getByText('Force plot — вклад факторов')).toBeVisible();
  await expect(page.getByText('Live-запрос к ML API')).toHaveCount(0);
});

test('администратор назначает любую роль и правит матрицу прав (как до этапа B)', async () => {
  await page.locator('aside >> text=Пользователи').first().click();
  const analystRow = page.locator('tr', { hasText: 'd.ivanov@bank.ru' });
  await expect(analystRow.locator('select option')).toHaveText(['Администратор', 'Маркетолог', 'Аналитик']);
  await page.getByRole('button', { name: /Добавить/ }).click();
  await expect(page.getByText('Администратор', { exact: true }).last()).toBeVisible();
  await page.getByRole('button', { name: 'Отмена' }).click();

  await page.getByText('Матрица прав').click();
  await expect(page.getByText(/Изменения матрицы прав применяются немедленно/)).toBeVisible();
  const cell = page.locator('tr', { hasText: 'Кампании — создание' })
    .locator('td').nth(3).locator('div').first();               // аналитик
  await expect(cell.locator('path[d^="M3 3"]')).toHaveCount(1);   // ✕
  await cell.click();
  await expect(cell.locator('path[d^="M2.5 7"]')).toHaveCount(1); // ✓
  await cell.click();                                             // вернуть как было
  await expect(cell.locator('path[d^="M3 3"]')).toHaveCount(1);
});

test('итог: ни одной неожиданной console-ошибки за сессию', async () => {
  expect(unexpectedErrors(errors)).toEqual([]);
});
