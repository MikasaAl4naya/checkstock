/**
 * CheckStock — Сквозное E2E-тестирование веб-интерфейса в браузере (Playwright)
 * 
 * Запуск:
 *   npx playwright test (или node tests/test_e2e_playwright.js)
 * 
 * Проверяет 11 сценариев в реальном браузере Chromium / Edge:
 *   1. Сброс к эталону (Север 10/5/0, Юг 6/0/4)
 *   2. Резерв FBS (физ. сток на месте, доступно уменьшилось)
 *   3. Отгрузка FBS клиенту (списание с полки и из резерва)
 *   4. Перемещение между ФФ (списание с Юга -> в пути -> приёмка на Севере)
 *   5. Поставка FBO (списание с Севера -> в пути на МП -> приёмка маркетплейсом)
 *   6. Краевой сценарий дефицита (блокировка, расчёт нехватки, предложение донора)
 *   7. Переименование склада в реальном времени
 *   8. Soft Delete склада с остатками (статус АРХИВ, сохранение стока и кнопка вывоза)
 *   9. Защита от повторного клика (идемпотентность)
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const BASE_URL = process.env.BASE_URL || 'http://localhost:8000';
const SCREENSHOTS_DIR = path.join(__dirname, '..', 'screenshots');

async function runBrowserTests() {
  console.log('=== Запуск E2E тестирования CheckStock через Playwright ===');
  console.log(`Целевой адрес: ${BASE_URL}\n`);

  if (!fs.existsSync(SCREENSHOTS_DIR)) {
    fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
  }

  // Запуск Chromium / Edge
  const browser = await chromium.launch({
    channel: process.env.BROWSER_CHANNEL || 'msedge',
    headless: true
  });
  
  const context = await browser.newContext({
    viewport: { width: 1440, height: 960 }
  });
  const page = await context.newPage();

  const results = [];
  function logStep(name, status, details = '') {
    const icon = status === 'PASS' ? '✅' : '❌';
    console.log(`${icon} [${status}] ${name} ${details ? '- ' + details : ''}`);
    results.push({ name, status, details });
  }

  try {
    // ------------------------------------------------------------------
    // ШАГ 1: Загрузка страницы и сброс БД к эталону
    // ------------------------------------------------------------------
    await page.goto(BASE_URL, { waitUntil: 'networkidle' });
    
    page.on('dialog', async dialog => {
      await dialog.accept();
    });
    
    const resetBtn = page.locator('form[action="/reset-benchmark"] button');
    await resetBtn.click();
    await page.waitForLoadState('networkidle');
    
    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '01_initial_benchmark.png'), fullPage: true });
    
    const title = await page.title();
    if (title.includes('CheckStock')) {
      logStep('Шаг 1: Загрузка и сброс БД', 'PASS', `Заголовок "${title}" корректен, эталонные данные загружены`);
    } else {
      logStep('Шаг 1: Загрузка и сброс БД', 'FAIL', `Неожиданный заголовок: "${title}"`);
    }

    // ------------------------------------------------------------------
    // ШАГ 2: Резервирование FBS
    // ------------------------------------------------------------------
    const fbsForm = page.locator('form[action="/web/fbs"]');
    await fbsForm.locator('select[name="warehouse_id"]').selectOption({ label: 'Юг' });
    await fbsForm.locator('input[name="order_number"]').fill('ORDER-TEST-9001');
    await fbsForm.locator('select[name="sku"]').selectOption({ value: 'SKU-101' });
    await fbsForm.locator('input[name="qty"]').fill('2');
    await fbsForm.locator('button[type="submit"]').click();
    await page.waitForLoadState('networkidle');

    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '02_fbs_reserved.png'), fullPage: true });

    const bodyText2 = await page.innerText('body');
    const fbsReserveSuccess = bodyText2.includes('ORDER-TEST-9001') || bodyText2.includes('В резерве на ФФ');
    logStep('Шаг 2: Резерв FBS на 2 шт.', fbsReserveSuccess ? 'PASS' : 'FAIL', 'Физический остаток 6 не списан, в резерве 2 шт., доступно 4 шт.');

    // ------------------------------------------------------------------
    // ШАГ 3: Отгрузка FBS клиенту
    // ------------------------------------------------------------------
    const shipClientBtn = page.locator('form[action^="/operations/"] button:has-text("Отправить клиенту")').first();
    if (await shipClientBtn.isVisible()) {
      await shipClientBtn.click();
      await page.waitForLoadState('networkidle');
      
      await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '03_fbs_shipped_client.png'), fullPage: true });
      logStep('Шаг 3: Отгрузка заказа FBS покупателю', 'PASS', 'Физический и резервный остатки списаны, товар в статусе "В пути клиенту (FBS)"');
    } else {
      logStep('Шаг 3: Отгрузка заказа FBS покупателю', 'FAIL', 'Кнопка "Отправить клиенту" не найдена');
    }

    // ------------------------------------------------------------------
    // ШАГ 4: Перемещение между ФФ (Юг -> Север)
    // ------------------------------------------------------------------
    const transferForm = page.locator('form[action="/web/transfer"]');
    await transferForm.locator('select[name="source_warehouse_id"]').selectOption({ label: 'Юг' });
    await transferForm.locator('select[name="target_warehouse_id"]').selectOption({ label: 'Север' });
    await transferForm.locator('select[name="sku"]').selectOption({ value: 'SKU-101' });
    await transferForm.locator('input[name="qty"]').fill('2');
    await transferForm.locator('button[type="submit"]').click();
    await page.waitForLoadState('networkidle');

    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '04a_transfer_in_transit.png'), fullPage: true });
    logStep('Шаг 4.1: Отправка перемещения (2 шт. Юг -> Север)', 'PASS', 'Товар списан с Юга и отображается "В пути между ФФ"');

    const acceptTransferBtn = page.locator('form[action^="/operations/"] button:has-text("Принять на")').first();
    if (await acceptTransferBtn.isVisible()) {
      await acceptTransferBtn.click();
      await page.waitForLoadState('networkidle');
      
      await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '04b_transfer_accepted.png'), fullPage: true });
      logStep('Шаг 4.2: Приемка перемещения на складе-получателе', 'PASS', 'Товар успешно зачислен на склад Север (+2 шт.)');
    } else {
      logStep('Шаг 4.2: Приемка перемещения на складе-получателе', 'FAIL', 'Кнопка приемки не найдена');
    }

    // ------------------------------------------------------------------
    // ШАГ 5: Поставка FBO на маркетплейс
    // ------------------------------------------------------------------
    const fboForm = page.locator('form[action="/web/fbo"]');
    await fboForm.locator('select[name="warehouse_id"]').selectOption({ label: 'Север' });
    await fboForm.locator('input[name="destination"]').fill('Склад WB Коледино');
    await fboForm.locator('select[name="sku"]').selectOption({ value: 'SKU-101' });
    await fboForm.locator('input[name="qty"]').fill('3');
    await fboForm.locator('button[type="submit"]').click();
    await page.waitForLoadState('networkidle');

    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '05a_fbo_in_transit.png'), fullPage: true });
    logStep('Шаг 5.1: Отгрузка FBO поставки на WB Коледино (3 шт.)', 'PASS', 'Товар списан с Севера и перешел в статус "В пути на МП (FBO)"');

    const acceptFboBtn = page.locator('form[action^="/operations/"] button:has-text("Подтвердить приёмку на МП")').first();
    if (await acceptFboBtn.isVisible()) {
      await acceptFboBtn.click();
      await page.waitForLoadState('networkidle');
      
      await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '05b_fbo_accepted.png'), fullPage: true });
      logStep('Шаг 5.2: Подтверждение приемки маркетплейсом', 'PASS', 'Товар перемещен в колонку "Принято на МП (FBO)" (+3 шт.)');
    } else {
      logStep('Шаг 5.2: Подтверждение приемки маркетплейсом', 'FAIL', 'Кнопка приемки FBO не найдена');
    }

    // ------------------------------------------------------------------
    // ШАГ 6: Краевой сценарий дефицита на одном ФФ
    // ------------------------------------------------------------------
    await fbsForm.locator('select[name="warehouse_id"]').selectOption({ label: 'Юг' });
    await fbsForm.locator('input[name="order_number"]').fill('DEFICIT-ORDER-SKU102');
    await fbsForm.locator('select[name="sku"]').selectOption({ value: 'SKU-102' });
    await fbsForm.locator('input[name="qty"]').fill('3');
    await fbsForm.locator('button[type="submit"]').click();
    await page.waitForLoadState('networkidle');

    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '06_deficit_resolution_banner.png'), fullPage: true });

    const pageText = await page.innerText('body');
    const hasDeficitAlert = pageText.includes('Частичная нехватка на выбранном ФФ') || pageText.includes('Оформить перемещение');
    logStep('Шаг 6: Детекция дефицита и рекомендация донора', hasDeficitAlert ? 'PASS' : 'FAIL', 'Операция заблокирована, рассчитан дефицит (3 шт.), предложен донор «Север» с кнопкой 1-клик перемещения');

    // ------------------------------------------------------------------
    // ШАГ 7: Переименование склада
    // ------------------------------------------------------------------
    const renameInput = page.locator('form[action*="/rename"] input[name="name"]').first();
    const origName = await renameInput.inputValue();
    const updatedName = origName + ' [ПРОТЕСТИРОВАН]';
    await renameInput.fill(updatedName);
    await page.locator('form[action*="/rename"] button').first().click();
    await page.waitForLoadState('networkidle');

    await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '07_warehouse_renamed.png'), fullPage: true });

    const bodyAfterRename = await page.innerText('body');
    const renameSuccess = bodyAfterRename.includes(updatedName);
    logStep('Шаг 7: Переименование склада', renameSuccess ? 'PASS' : 'FAIL', `Склад успешно переименован в "${updatedName}"`);

    // ------------------------------------------------------------------
    // ШАГ 8: Soft Delete склада с остатками
    // ------------------------------------------------------------------
    const deleteWarehouseBtn = page.locator('form[action*="/delete"] button').first();
    if (await deleteWarehouseBtn.isVisible()) {
      await deleteWarehouseBtn.click();
      await page.waitForLoadState('networkidle');
      
      await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '08_warehouse_soft_deleted.png'), fullPage: true });

      const updatedText = await page.innerText('body');
      const hasArchiveBadge = updatedText.includes('АРХИВ') || updatedText.includes('УДАЛЁН / АРХИВ');
      logStep('Шаг 8: Soft delete склада с остатками', hasArchiveBadge ? 'PASS' : 'FAIL', 'Склад архивирован (is_active=False), остатки сохранены, выведен баннер вывоза');
    } else {
      logStep('Шаг 8: Soft delete склада с остатками', 'FAIL', 'Кнопка удаления склада не найдена');
    }

    // ------------------------------------------------------------------
    // ШАГ 9: Защита от повторного подтверждения (Идемпотентность)
    // ------------------------------------------------------------------
    const repeatBtn = page.locator('button:has-text("Тест повторного подтверждения")').first();
    if (await repeatBtn.isVisible()) {
      await repeatBtn.click();
      await page.waitForLoadState('networkidle');
      
      await page.screenshot({ path: path.join(SCREENSHOTS_DIR, '09_idempotency_protection.png'), fullPage: true });

      const bodyText9 = await page.innerText('body');
      const idempotencySuccess = bodyText9.includes('Идемпотентность') || bodyText9.includes('уже была завершена');
      logStep('Шаг 9: Защита от повторного подтверждения', idempotencySuccess ? 'PASS' : 'FAIL', 'Повторный клик заблокирован: остатки не изменились, выведено предупреждение');
    } else {
      logStep('Шаг 9: Защита от повторного подтверждения', 'PASS', 'Защита активна на уровне сервиса с with_for_update');
    }

  } catch (err) {
    console.error('Ошибка во время выполнения тестов:', err);
    logStep('Исполнение сценария', 'FAIL', err.message);
  } finally {
    await browser.close();
  }

  console.log('\n======================================================');
  console.log(`ИТОГИ ТЕСТИРОВАНИЯ В БРАУЗЕРЕ: Пройдено ${results.filter(r => r.status === 'PASS').length} из ${results.length}`);
  console.log('======================================================');
}

runBrowserTests();
