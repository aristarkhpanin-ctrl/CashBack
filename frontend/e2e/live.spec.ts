/**
 * Live-режим против стаб-бэкенда (frontend/e2e/stub_backend.py),
 * повторяющего wire-формат реальных сервисов: JWT-логин, CRUD кампаний
 * через FSM, аналитика, SHAP, A/B-эксперименты, управление пользователями.
 */
import {
  expect, test, type Page, type Request, type Response,
} from '@playwright/test';
import type { ChildProcess } from 'node:child_process';
import {
  SIDEBAR_PAGES, STUB_USERS, collectErrors, fillWizardToReview, isoDay, loginAs,
  openNewCampaignWizard, ruDay, startStub, stopStub, stubApi, stubControl, unexpectedErrors,
} from './utils';

test.describe.configure({ mode: 'serial' });

let stub: ChildProcess;
let page: Page;
let errors: string[];
const refHits: { url: string; status: number }[] = [];
const kpiHits: { url: string; status: number }[] = [];
const apiCalls: string[] = [];   // «METHOD path» всех запросов к /api за сессию
const listQueries: string[] = [];  // query-строки GET /campaigns (список админки)

type StubState = {
  ml_limits_snapshot: { global_enabled: boolean };
  ml_limits_publishes: number;
};
const stubState = () => stubControl<StubState>('state');

test.beforeAll(async ({ browser }) => {
  stub = await startStub();
  page = await browser.newPage();
  errors = collectErrors(page);
  page.on('request', (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith('/api/')) apiCalls.push(`${r.method()} ${u.pathname}`);
    if (u.pathname === '/api/campaigns/campaigns' && r.method() === 'GET') listQueries.push(u.search);
  });
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
  await loginAs(page, STUB_USERS.admin);
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

test('кампании приходят из API (4 из стаба, архивная скрыта), карточка открывается', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await expect(page.getByText(/^Все \(4\)/)).toBeVisible();
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

// ── B39: kill switch ML-лимитов не «открыт при сбое» ────────────────────────
const ML_LIMITS_PATH = '/api/campaigns/ml-limits';
/** Глобальный kill switch ML — ToggleSwitch слева от подписи. */
const mlKillSwitch = (p: Page) => p.getByText(/ML-рекомендации (включены|отключены) глобально/)
  .locator('xpath=../preceding-sibling::div[1]');
const mlSave = (p: Page) => p.getByRole('button', { name: 'Сохранить', exact: true });
const mlDirty = (p: Page) => p.getByText('Есть несохранённые изменения');
const mlUnpublished = 'Сохранено, но ещё не применено — будет повторено автоматически';

function waitMlLimits(p: Page, method: 'GET' | 'PUT'): Promise<Response> {
  return p.waitForResponse(
    r => new URL(r.url()).pathname === ML_LIMITS_PATH && r.request().method() === method,
  );
}

test('ML-лимиты: «Сохранить» → PUT 200 → «записаны в БД», снапшот R7 обновлён', async () => {
  await mlKillSwitch(page).click();                                  // выключить ML
  await expect(page.getByText('ML-рекомендации отключены глобально')).toBeVisible();
  const put = waitMlLimits(page, 'PUT');
  await mlSave(page).click();
  const resp = await put;
  expect(resp.status()).toBe(200);
  expect(resp.request().postDataJSON().global_enabled).toBe(false);
  await expect(page.getByText(/Лимиты записаны в БД/)).toBeVisible();
  await expect(mlDirty(page)).toHaveCount(0);
  expect((await stubState()).ml_limits_snapshot.global_enabled).toBe(false);
});

test('ML-лимиты: 503 «saved but not published» → «Сохранено, но ещё не применено…», затем переиздано (B39)', async () => {
  await expect(page.getByText(/Лимиты записаны в БД/)).toHaveCount(0);  // прошлый тост ушёл
  await stubControl('config', { ml_limits_publish_down: true });
  let publishes: number;
  try {
    await mlKillSwitch(page).click();                                // включить ML обратно
    const put = waitMlLimits(page, 'PUT');
    const reread = waitMlLimits(page, 'GET');
    await mlSave(page).click();
    expect((await put).status()).toBe(503);
    await expect(page.getByText(mlUnpublished)).toBeVisible();
    await expect(page.getByText(/ошибка сервера \(503\)/)).toHaveCount(0);
    // Изменение уже в БД: форма не «грязная», лимиты перечитаны из API.
    await expect(mlDirty(page)).toHaveCount(0);
    expect((await (await reread).json()).global_enabled).toBe(true);
    await expect(page.getByText('ML-рекомендации включены глобально')).toBeVisible();
    // Serving-путь (R7) пока видит прежний снапшот.
    const st = await stubState();
    expect(st.ml_limits_snapshot.global_enabled).toBe(false);
    publishes = st.ml_limits_publishes;
  } finally {
    await stubControl('config', { ml_limits_publish_down: false });
  }
  // Снапшот доставляет фоновая задача republish — без повторного PUT.
  const puts = apiCalls.filter(c => c === `PUT ${ML_LIMITS_PATH}`).length;
  await expect.poll(async () => (await stubState()).ml_limits_snapshot.global_enabled,
    { timeout: 10_000 }).toBe(true);
  expect((await stubState()).ml_limits_publishes).toBe(publishes + 1);
  expect(apiCalls.filter(c => c === `PUT ${ML_LIMITS_PATH}`)).toHaveLength(puts);
});

test('ML-лимиты: прочий 503 (шлюз) — ошибка, изменения не сохранены', async () => {
  await expect(page.getByText(mlUnpublished)).toHaveCount(0);
  const putUrl = (u: URL) => u.pathname === ML_LIMITS_PATH;
  await page.route(putUrl, route => (route.request().method() === 'PUT'
    ? route.fulfill({
        status: 503, contentType: 'application/json',
        body: JSON.stringify({ detail: 'Service Unavailable' }),
      })
    : route.fallback()));
  try {
    await mlKillSwitch(page).click();
    await mlSave(page).click();
    await expect(page.getByText('Campaign Manager: ошибка сервера (503)')).toBeVisible();
    await expect(page.getByText(mlUnpublished)).toHaveCount(0);
    await expect(mlDirty(page)).toBeVisible();                        // повторить можно
  } finally {
    await page.unroute(putUrl);
  }
  await page.getByRole('button', { name: 'Отменить', exact: true }).click();
  await expect(page.getByText('ML-рекомендации включены глобально')).toBeVisible();
});

test('ростер пользователей из /auth/users', async () => {
  await page.locator('aside >> text=Пользователи').first().click();
  await expect(page.getByText('m.sokolova@bank.ru')).toBeVisible();
  await expect(page.getByText('d.ivanov@bank.ru')).toBeVisible();
  // ADMIN может назначить роль ADMIN (этап B: не-ADMIN — нет).
  const analystRow = page.locator('tr', { hasText: 'd.ivanov@bank.ru' });
  await expect(analystRow.locator('select option', { hasText: 'Администратор' })).toHaveCount(1);
});

test('матрица прав редактируется через /roles/:role/permissions (фаза 26)', async () => {
  await page.getByText('Матрица прав').click();
  await expect(page.getByText(/Изменения матрицы прав применяются немедленно/)).toBeVisible();
  // Тоггл ячейки «Кампании — создание» у аналитика (3-я роль-колонка) → PATCH.
  const row = page.locator('tr', { hasText: 'Кампании — создание' });
  await row.locator('td').nth(3).locator('div').first().click();
  await expect(page.getByText('Права роли обновлены')).toBeVisible();
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

// ── B46: «Запустить кампанию» реально активирует ────────────────────────────
const CAMPAIGNS_PATH = '/api/campaigns/campaigns';

/** Записывает мутации кампаний (всё, кроме GET) как «METHOD path?query». */
function recordCampaignMutations(p: Page): { calls: string[]; stop: () => void } {
  const calls: string[] = [];
  const onRequest = (r: Request) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith(CAMPAIGNS_PATH) && r.method() !== 'GET') {
      calls.push(`${r.method()} ${u.pathname}${u.search}`);
    }
  };
  p.on('request', onRequest);
  return { calls, stop: () => p.off('request', onRequest) };
}

function waitForCreate(p: Page): Promise<Response> {
  return p.waitForResponse(
    r => new URL(r.url()).pathname === CAMPAIGNS_PATH && r.request().method() === 'POST',
  );
}

/**
 * Ждёт, пока уйдёт тост прошлого действия с кампанией (живёт 3 с): иначе
 * его текст засчитался бы следующему действию.
 */
async function waitSaveToastGone(p: Page): Promise<void> {
  await expect(p.getByText(
    /Кампания (создана|обновлена|сохранена|отправлена|активирована|запущена|удалена)|Изменения сохранены/,
  )).toHaveCount(0);
}

const wizardOpen = (p: Page) => p.getByText(/Шаг \d из 5/);

test('«Запустить кампанию»: POST /campaigns → PATCH /status?action=activate, в списке активна (B46)', async () => {
  await waitSaveToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Запуск-B46');
  const rec = recordCampaignMutations(page);
  const createdResp = waitForCreate(page);
  try {
    await page.getByRole('button', { name: /Запустить кампанию/ }).click();
    const created = await (await createdResp).json();
    // Активируется именно созданная кампания и строго после создания.
    await expect.poll(() => rec.calls).toEqual([
      `POST ${CAMPAIGNS_PATH}`,
      `PATCH ${CAMPAIGNS_PATH}/${created.campaign_id}/status?action=activate`,
    ]);
  } finally {
    rec.stop();
  }
  await expect(page.getByText('Кампания создана и запущена')).toBeVisible();
  // Список перечитан из стаба: кампания в «Активных», т.е. там она ACTIVE.
  await page.getByText(/^Активные \(/).click();
  await expect(page.getByText('Запуск-B46')).toBeVisible();
});

test('двойной клик по «Запустить кампанию» → ровно один POST (B46)', async () => {
  // Медленная сеть: ответ на создание задерживаем, чтобы второй клик
  // гарантированно пришёлся на ещё открытый визард.
  const slowCreate = (u: URL) => u.pathname === CAMPAIGNS_PATH;
  await page.route(slowCreate, async route => {
    if (route.request().method() === 'POST') await new Promise(r => setTimeout(r, 600));
    await route.fallback();
  });
  await waitSaveToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Дабл-клик-B46');
  const rec = recordCampaignMutations(page);
  try {
    await page.getByRole('button', { name: /Запустить кампанию/ }).dblclick();
    // Визард закрывается не раньше ответа на (задержанный) первый POST, а
    // второй клик обработан сразу — второй POST, если бы был, уже записан.
    await expect(wizardOpen(page)).toBeHidden();
    expect(rec.calls.filter(c => c.startsWith('POST '))).toEqual([`POST ${CAMPAIGNS_PATH}`]);
    expect(rec.calls.filter(c => c.startsWith('PATCH '))).toHaveLength(1);
    await expect(page.getByText('Кампания создана и запущена')).toBeVisible();
  } finally {
    rec.stop();
    await page.unroute(slowCreate);
  }
  await expect(page.getByText('Дабл-клик-B46')).toHaveCount(1);
});

test('активация отклонена после создания → «сохранена как черновик», без ложного успеха (B46)', async () => {
  const statusUrl = (u: URL) => /^\/api\/campaigns\/campaigns\/[0-9a-f-]{36}\/status$/.test(u.pathname);
  await page.route(statusUrl, route => route.fulfill({
    status: 409,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'activation rejected (e2e)' }),
  }));
  await waitSaveToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Сбой-запуска-B46');
  const rec = recordCampaignMutations(page);
  const createdResp = waitForCreate(page);
  try {
    await page.getByRole('button', { name: /Запустить кампанию/ }).click();
    const created = await (await createdResp).json();
    await expect.poll(() => rec.calls).toEqual([
      `POST ${CAMPAIGNS_PATH}`,
      `PATCH ${CAMPAIGNS_PATH}/${created.campaign_id}/status?action=activate`,
    ]);
    await expect(page.getByText(/Кампания сохранена как черновик/)).toBeVisible();
    await expect(page.getByText(/Кампания создана/)).toHaveCount(0);
    // Визард закрыт: повторное «Запустить» создало бы дубль.
    await expect(wizardOpen(page)).toBeHidden();
  } finally {
    rec.stop();
    await page.unroute(statusUrl);
  }
  await page.getByText(/^Черновики \(/).click();
  await expect(page.getByText('Сбой-запуска-B46')).toBeVisible();
});

test('«Сохранить черновик» → только POST, без активации; кампания в черновиках (B46)', async () => {
  await waitSaveToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Черновик-B46');
  const rec = recordCampaignMutations(page);
  try {
    await page.getByRole('button', { name: /Сохранить черновик/ }).click();
    await expect(wizardOpen(page)).toBeHidden();
    expect(rec.calls).toEqual([`POST ${CAMPAIGNS_PATH}`]);
    await expect(page.getByText('Кампания создана и добавлена в список')).toBeVisible();
  } finally {
    rec.stop();
  }
  await page.getByText(/^Черновики \(/).click();
  await expect(page.getByText('Черновик-B46')).toBeVisible();
});

test('черновик → «Редактировать» → «Запустить кампанию»: PATCH полей, затем PATCH /status (B46)', async () => {
  await waitSaveToastGone(page);
  await page.locator('aside >> text=Дашборд').first().click();
  await page.locator('aside >> text=Кампании').first().click();
  await page.getByText('Черновик-B46').first().click();
  await page.getByRole('button', { name: 'Редактировать', exact: true }).click();
  await expect(page.getByText(/Шаг 1 из 5/)).toBeVisible();
  for (let i = 0; i < 4; i++) await page.getByRole('button', { name: /Далее/ }).click();
  await expect(page.getByText(/Шаг 5 из 5/)).toBeVisible();
  const rec = recordCampaignMutations(page);
  try {
    await page.getByRole('button', { name: /Запустить кампанию/ }).click();
    await expect(wizardOpen(page)).toBeHidden();
    const id = /^PATCH \/api\/campaigns\/campaigns\/([0-9a-f-]{36})$/.exec(rec.calls[0] ?? '')?.[1];
    expect(id, `первый вызов: ${rec.calls[0]}`).toBeTruthy();
    // Сначала правка полей черновика, затем запуск именно этой кампании.
    expect(rec.calls).toEqual([
      `PATCH ${CAMPAIGNS_PATH}/${id}`,
      `PATCH ${CAMPAIGNS_PATH}/${id}/status?action=activate`,
    ]);
    await expect(page.getByText('Кампания обновлена и запущена')).toBeVisible();
  } finally {
    rec.stop();
  }
  await page.getByText(/^Активные \(/).click();
  await expect(page.getByText('Черновик-B46').first()).toBeVisible();
  await page.getByText(/^Все \(/).click(); // следующие тесты ждут нефильтрованный список
});

// ── Этап C: архив вместо удаления (B24), окно дат при запуске (B25) ─────────
const ARCHIVED = 'Кинотеатры 8% (архив)';   // архивная кампания стаба
const deleteBtn = (p: Page) => p.getByRole('button', { name: 'Удалить кампанию' });
const archiveBtn = (p: Page) => p.getByRole('button', { name: 'В архив', exact: true });
const archiveToggle = (p: Page) => p.getByLabel('Показать архив');

test('архивные кампании скрыты; «Показать архив» → GET /campaigns?include_archived=true (B24)', async () => {
  await page.locator('aside >> text=Дашборд').first().click();
  await page.locator('aside >> text=Кампании').first().click();
  await expect(page.getByText('Супермаркеты 5% (live)').first()).toBeVisible();
  await expect(page.getByText(ARCHIVED)).toHaveCount(0);
  // До тумблера список ни разу не запрашивал архив.
  expect(listQueries.length).toBeGreaterThan(0);
  expect(listQueries.filter(q => q.includes('include_archived'))).toEqual([]);

  const archivedList = page.waitForRequest(r => {
    const u = new URL(r.url());
    return u.pathname === CAMPAIGNS_PATH && u.searchParams.get('include_archived') === 'true';
  });
  await archiveToggle(page).check();
  await archivedList;
  await page.getByText(ARCHIVED).first().click();
  await expect(page.getByText('В архиве с')).toBeVisible();
  await expect(page.getByText('В архиве', { exact: true })).toHaveCount(2);  // карточка + панель
  await expect(page.getByRole('button', { name: 'Редактировать', exact: true })).toHaveCount(0);
  await expect(page.locator('header').getByText('LIVE API')).toBeVisible();  // без мигания в демо
  await archiveToggle(page).uncheck();
  await expect(page.getByText(ARCHIVED)).toHaveCount(0);
});

test('с историей — «В архив» вместо «Удалить» → POST …/archive; активная — ни то ни другое (B24)', async () => {
  await waitSaveToastGone(page);
  await page.getByText('Запуск-B46').first().click();                 // ACTIVE
  await expect(deleteBtn(page)).toHaveCount(0);
  await expect(archiveBtn(page)).toHaveCount(0);
  await expect(page.getByText('Отправить в архив можно после паузы или завершения')).toBeVisible();

  await page.getByText('Кафе черновик с историей (live)').first().click();  // DRAFT, есть история
  await expect(deleteBtn(page)).toHaveCount(0);
  await expect(archiveBtn(page)).toHaveCount(0);
  await expect(page.getByText('У черновика уже есть история — удалить его нельзя')).toBeVisible();

  await page.getByText('Аптеки 6% (live)').first().click();           // PAUSED, есть история
  await expect(deleteBtn(page)).toHaveCount(0);
  const rec = recordCampaignMutations(page);
  try {
    await archiveBtn(page).click();
    await page.getByRole('button', { name: 'Подтвердить архивацию' }).click();
    await expect(page.getByText('Кампания отправлена в архив')).toBeVisible();
    expect(rec.calls).toHaveLength(1);
    expect(rec.calls[0]).toMatch(/^POST \/api\/campaigns\/campaigns\/[0-9a-f-]{36}\/archive$/);
  } finally {
    rec.stop();
  }
  await expect(page.getByText('Аптеки 6% (live)')).toHaveCount(0);
  await archiveToggle(page).check();
  await expect(page.getByText('Аптеки 6% (live)').first()).toBeVisible();
  await archiveToggle(page).uncheck();
});

test('запуск с будущим стартом: starts_in_future → «запустится ДД.ММ.ГГГГ» (B25)', async () => {
  await waitSaveToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Будущий-старт-B25', { startInDays: 10 });
  const activated = page.waitForResponse(r => /\/status\?action=activate$/.test(r.url()));
  await page.getByRole('button', { name: /Запустить кампанию/ }).click();
  expect((await (await activated).json()).starts_in_future).toBe(true);
  await expect(page.getByText(`Кампания создана и активирована — запустится ${ruDay(isoDay(10))}`))
    .toBeVisible();
  await expect(page.getByText('Кампания создана и запущена')).toHaveCount(0);

  // Черновик с будущим стартом, запуск из карточки.
  await waitSaveToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Черновик-будущий-B25', { startInDays: 7 });
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(wizardOpen(page)).toBeHidden();
  await waitSaveToastGone(page);
  await page.getByText('Черновик-будущий-B25').first().click();
  await page.getByRole('button', { name: 'Запустить', exact: true }).click();
  await expect(page.getByText(`Кампания активирована — запустится ${ruDay(isoDay(7))}`)).toBeVisible();
});

test('запуск после end_date → 409, кампания остаётся черновиком (B25)', async () => {
  await waitSaveToastGone(page);
  await openNewCampaignWizard(page);
  await fillWizardToReview(page, 'Закончилась-B25', { startInDays: -40, days: 10 });
  await page.getByRole('button', { name: /Сохранить черновик/ }).click();
  await expect(wizardOpen(page)).toBeHidden();
  await waitSaveToastGone(page);
  await page.getByText('Закончилась-B25').first().click();
  const activated = page.waitForResponse(r => /\/status\?action=activate$/.test(r.url()));
  await page.getByRole('button', { name: 'Запустить', exact: true }).click();
  expect((await activated).status()).toBe(409);
  await expect(page.getByText('campaign has already ended (end_date has passed)')).toBeVisible();
  await expect(page.getByText(/Кампания (запущена|активирована)/)).toHaveCount(0);
  await page.getByText(/^Черновики \(/).click();
  await expect(page.getByText('Закончилась-B25').first()).toBeVisible();
  await page.getByText(/^Все \(/).click();
});

test('admin удаляет черновик без истории через DELETE /campaigns/:id (фаза 23, B24)', async () => {
  await page.locator('aside >> text=Кампании').first().click();
  await page.getByText('АЗС черновик (live)').first().click();
  await expect(archiveBtn(page)).toHaveCount(0);
  await page.getByRole('button', { name: /Удалить кампанию/ }).click();
  await page.getByRole('button', { name: /Подтвердить удаление/ }).click();
  await expect(page.getByText(/Кампания удалена/)).toBeVisible();
  // список инвалидируется — карточка исчезает из ленты и detail-панели.
  await expect(page.getByText('АЗС черновик (live)')).toHaveCount(0);
});

test('стаб повторяет §3: DELETE кампании с историей → 409, журнал цел; budget/check нет', async () => {
  const login = await stubApi('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(STUB_USERS.admin),
  });
  const auth = { Authorization: `Bearer ${(await login.json()).access_token}` };
  const list = await (await stubApi('/campaigns?include_archived=true', { headers: auth })).json();
  const paused = list.find((c: { name: string }) => c.name === 'Супермаркеты 5% (live)');
  const budget = () => stubApi(`/campaigns/${paused.campaign_id}/budget`, { headers: auth });
  const before = await (await budget()).json();

  const del = await stubApi(`/campaigns/${paused.campaign_id}`, { method: 'DELETE', headers: auth });
  expect(del.status).toBe(409);
  expect((await del.json()).detail).toBe('campaign has history — archive it instead');
  const reserve = await stubApi(`/campaigns/${paused.campaign_id}/budget/check`, {
    method: 'POST', headers: { ...auth, 'Content-Type': 'application/json' },
    body: JSON.stringify({ amount: '100' }),
  });
  expect(reserve.status).toBe(404);
  expect(await (await budget()).json()).toEqual(before);  // ни удаления, ни «резерва»
  expect(Number(before.remaining)).toBe(Number(before.budget_total) - Number(before.budget_spent));

  // Черновик тоже не удаляется, если у него есть история.
  const draft = list.find((c: { name: string }) => c.name === 'Кафе черновик с историей (live)');
  const delDraft = await stubApi(`/campaigns/${draft.campaign_id}`, { method: 'DELETE', headers: auth });
  expect(delDraft.status).toBe(409);
  expect((await delDraft.json()).detail).toBe('campaign has history — archive it instead');
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

test('logout возвращает на страницу логина; в live нет демо-переключателя (фаза 27)', async () => {
  await page.locator('header').getByText('Аристарх').click();
  // В live профиль-меню (Выйти), а не демо-переключатель ролей.
  await expect(page.getByText('Сменить роль (демо)')).toHaveCount(0);
  await page.getByText('Выйти').click();
  await expect(page.locator('input[type="email"]')).toBeVisible();
});

test('итог: ни одной console-ошибки за live-сессию', async () => {
  // «Failed to load resource» (напр. намеренный 404-lookup) — ожидаемый
  // сетевой шум, не дефект страницы.
  expect(unexpectedErrors(errors)).toEqual([]);
  // B23: «резерв» бюджета убран — UI его не вызывает.
  expect(apiCalls.filter(c => c.includes('/budget/check'))).toEqual([]);
});
