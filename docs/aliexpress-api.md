# AliExpress API: легальность и возможности интеграции (для DealHunter)

Исследование для проекта DealHunter — Telegram-бот «радар цен» на технику. Проект некоммерческий, работает на аудиторию из России, создатель — несовершеннолетний физлицо из РФ.

Дата исследования: октябрь 2026. Все ключевые утверждения снабжены ссылками на первоисточники. Зеркала официальной документации на `open.alitrip.com` помечены там же как «已废弃» (устаревшие), это сервер-рендеренная копия документации AliExpress Open Platform, доступная без JS; содержимое совпадает с `openservice.aliexpress.com`, который является SPA и не читается без браузера.

---

## Вердикт

**Условно (CONDITIONALLY). Напрямую — НЕТ (несовершеннолетним), фактически — ДА (при 18+ / через взрослого-самозанятого).**

1. **Использовать официальное API — легально.** AliExpress сам создаёт Affiliate API именно для публичной демонстрации товаров и цен с партнёрскими ссылками; сервисы сравнения цен и площадки-каталоги со скидками — официальный разрешённый тип трафика. Ботов и Telegram прямо не запрещают: в программах для РФ запрещены только практики накрутки и соцсети, запрещённые в РФ (Telegram к ним не относится).
2. **Но зарегистрироваться самому — нельзя, пока нет 18 лет.** Условия участия прямо требуют способности заключать договоры («lawfully able to enter into contracts (e.g. you are not a minor)»), а официальная российская платформа aePlatform требует 18+, налоговое резидентство РФ и статус самозанятого/ИП/ООО.
3. **Легальный путь для проекта:** бот технически строится на официальном партнёрском API (бесплатно, без сессий и оплаты), но учётные записи оформляются на совершеннолетнего (родитель/самозанятый); до этого — контент-режим без API-монетизации (артикулы ALI..., ручные подборки).
4. **Жёсткие условия легальности:** только официальные API (скрейпинг не лицензирован), показ содержимого как рекламы с партнёрской ссылкой, без изменения содержимого, без брендовых названий («AliExpress...» в имени бота — нельзя), без накруток и искусственных кликов/обновлений, лимиты запросов добросовестные (квоты в консоли).
5. **Важно:** официальное API не отдаёт историю цен — «радар» придётся строить самостоятельно: опрос по квоте и сохранение собственной серии цен.

---

## Что доступно официально

### 1. AliExpress Open Platform (глобальный, `openservice.aliexpress.com`)

Единая точка входа для всех API AliExpress. Регистрация и консоль приложений — `console.aliexpress.com`. Полный поток подключения описан официально (заведение AliExpress-аккаунта + аккаунта Portals на одну почту → включение роли разработчика → создание приложения типа **Affiliate API** → модерация ~1–2 дня):

- https://open.alitrip.com/docs/doc.htm?treeId=674&articleId=118193&docType=1 (Overall Flow For API Access)
- https://openservice.aliexpress.com/doc/doc.htm (SPA-версия)

**Affiliate API (набор для партнёров)** — бесплатный (в зеркале документации помечен «免费 API»), без сессий и пользовательской авторизации. Официальный список и зеркала документации:

| API | Что даёт | ID / ссылка |
|---|---|---|
| `aliexpress.affiliate.product.query` | поиск товаров: ключевые слова, категории, фильтры цены, сортировка, `ship_to_country`, `target_currency` (в т.ч. RUB), `target_language` (в т.ч. RU) | https://open.alitrip.com/docs/doc.htm?apiId=45803 |
| `aliexpress.affiliate.hotproduct.query` | «горячие» товары (тренды/хиты) | https://open.alitrip.com/docs/doc.htm?apiId=45794 |
| `aliexpress.affiliate.hotproduct.download` | выгрузка спец-подборок | https://open.alitrip.com/docs/doc.htm?apiId=48598 |
| `aliexpress.affiliate.productdetail.get` | детали товара: `product_title`, изображения, **video**, `original_price`, `sale_price`, `app_sale_price`, `discount`, `evaluate_rate` (% хороших отзывов), `commission_rate`, `lastest_volume`, промокоды и т.п. | https://open.alitrip.com/docs/doc.htm?apiId=48595 |
| `aliexpress.affiliate.category.get` | дерево категорий | https://open.alitrip.com/docs/doc.htm?apiId=45801 |
| `aliexpress.affiliate.featuredpromo.info/.products.get` | промо-механики и их товары | https://open.alitrip.com/docs/doc.htm?apiId=48596 , 48597 |
| `aliexpress.affiliate.smartmatch.product.get` | умный подбор товара по контексту | https://open.alitrip.com/docs/doc.htm?apiId=46010 |
| `aliexpress.affiliate.link.generate` | генерация партнёрских ссылок/диплинков | https://open.alitrip.com/docs/doc.htm?apiId=45804 |
| отчёты по заказам/кликам (`order.*`, `link.*`) | статистика конверсий | treeId=674 |

Обзор набора: https://open.alitrip.com/docs/doc.htm?treeId=674&articleId=118192&docType=1

**Шлюзы вызова** (из официальной «How to invoke affiliate API», https://open.alitrip.com/docs/doc.htm?treeId=674&articleId=118934&docType=1): `gw.api.taobao.com/router/rest` (CN), `api-sg.aliexpress.com/sync` (Сингапур), `api-us.aliexpress.com/sync` (США), а также региональный **`ru-api.aliexpress.com/router/rest`** (Россия). SDK на Java/PHP/Python/.NET скачиваются в консоли приложений.

**Важно про историю цен:** в ответах Affiliate API **нет** полей с прошлыми ценами — только текущие/прошлые базовые `original_price`/`sale_price`/`app_sale_price` и `discount`. Мониторинг «снижений» возможен только как собственное накопление снятий (полл + локальное хранение истории).

### 2. Прочие наборы того же Open Platform

- **API для дропшипперов** (`aliexpress.ds.*`) — продукты/логистика/ «Dropshipping suite»; доступ привязан к программе дропшиппинга AliExpress (см. community SDK с официальными ссылками: `ae_sdk` на npm — https://cdn.jsdelivr.net/npm/ae_sdk/dist/index.d.ts).
- **API для селлеров** (`aliexpress.solution.*`) — для продавцов на aliexpress.com; нас не касается.

### 3. Партнёрская программа AliExpress (Portals, глобальная)

- Портал партнёра: https://portals.aliexpress.com (официальное определение — п. 1.3 соглашения ниже; SPA, читается только в браузере).
- Участие = принятие «AliExpress Affiliate Program Service Agreement» (редакция от 01.04.2025): https://cdn.contract.alibaba.com/terms/b_platform_service_agreement/20250305142526766/20250305142526766.html?lng=en — ключевые пункты:
  - п. 2.2: участник должен быть «lawfully able to enter into contracts (e.g. you are not a minor)» → **несовершеннолетним вход закрыт**;
  - п. 1.2 «AliExpress Content»: данные/картинки/тексты, доступные через Portal «or through authorized use of designated APIs» → использование именно API — лицензированный канал;
  - п. 3.1: суть программы — «advertising of AliExpress Content» с оплатой Qualifying Purchase;
  - **RU включён**: п. 1.15 «Relevant Jurisdictions» = RF и СНГ; п. 7.1 — за покупки на `aliexpress.ru` платит AliExpress CIS (через AliExpress Global);
  - п. 8.2/8.4: нельзя передавать/копировать/изменять/создавать производные от Content без письменного согласия (показ «как есть» с ссылкой — базовый разрешённый кейс);
  - п. 6.7: AliExpress может **приостановить API-доступ** участника, нарушившего правила;
  - документ упоминает «AliExpress Affiliate Program Advertising Rules and Policies» как правила, публикуемые в Portals Help Center (полный текст — только под логином).

### 4. Официальная российская партнёрка — aePlatform (`aeplatform.ru`)

- https://aeplatform.ru/ru/faq — требования к партнёру: **совершеннолетний (18+)**, налоговый резидент РФ, правовая форма: **самозанятый / ИП / ООО**; модерация площадки (ориентир: 1000+ подписчиков).
- https://aeplatform.ru/ru/partners — API доступа: только по запросу, после присвоения статуса «Партнёр».
- https://aeplatform.ru/ru/conditions — ставки, окно атрибуции cookie **15 дней**, гео СНГ; требования к трафику; из соцсетей запрещены только **соцсети, запрещённые в РФ** (Telegram разрешён).
- https://aeplatform.ru/ru/aeprules — Пользовательское соглашение: **нельзя использовать товарные знаки/бренд AliExpress в названиях сайтов/приложений/ботов** без письменного согласия (т.е. имя вида «AliExpressDealsBot» — запрещено).

### 5. CPA-сети с официальными офферами AliExpress (альтернатива прямому API)

- **Admitad (Mitgo)** — «AliExpress RU&CIS»: https://www.admitad.ru/store/offers/aliexpress-rucis/ — ставки для РФ, статистика EPC/CR, продуктовые фиды по запросу (обновление/выдача 10–14 дней — https://support.admitad.ru/article/ru/102), глубокие ссылки через редирект Admitad (переход на новую систему редиректов — 13.05.2026), готовый Telegram-бот вебмастера, инструмент «Hot Products» в программах AliExpress; сравнение WW vs RU&CIS: https://support.admitad.ru/article/ru/100.
- **ePN** — активный оффер AliExpress RU (обновления октябрь 2025: корректные партнёрские ссылки в приложение AliExpress RU, «артикулы» вида `ALI…` для упоминания в постах, window cookie 15 дней, API с обязательным `placementId`): https://epn.bz/ru/info/faq/aliexpress_improvements/
- Санкционный контроль качества трафика (антифрод-выводы за «накрутку») описан в справке Mitgo: https://support.mitgo.com/hc/en-us/articles/33313254502418 («Antispam deduction»).

### 6. Что недоступно

- **История цен** — ни одного официального эндпоинта нет.
- **Скрейпинг aliexpress.com / aliexpress.ru** — не лицензирован: лицензия программы покрывает только контент, переданный через Portal/API (п. 1.2 соглашения); DSA-обязательства AliExpress в ЕС дополнительно обещают борьбу с недобросовестными практиками (https://digital-strategy.ec.europa.eu/en/news/commission-makes-aliexpress-commitments-under-digital-services-act-binding).
- **Публичного API у aliexpress.ru нет.** У .ru есть только кабинет для продавцов (`business.aliexpress.ru`, API для селлеров: https://business.aliexpress.ru/help/article/docs) — для нашего бота неприменимо.
- **Sandbox для Affiliate API не документирован** (тестовые окружения описаны только для продавецских/Solution API).

---

## Кто может зарегистрироваться

| Канал | Возраст/статус | Что нужно |
|---|---|---|
| Portals + Affiliate API (глобальный Console: `console.aliexpress.com`, тип приложения «Affiliate API», одобрение 1–2 дня — https://open.alitrip.com/docs/doc.htm?treeId=674&articleId=118193&docType=1 и articleId=118195) | дееспособное лицо, **не несовершеннолетний** (п. 2.2 соглашения) | аккаунт AliExpress + аккаунт Portals, одна и та же почта |
| aePlatform (официальная РФ-партнёрка) | **18+, налоговый резидент РФ, самозанятый/ИП/ООО** (https://aeplatform.ru/ru/faq) | модерация площадки; API — по запросу после статуса «Партнёр» |
| Admitad / ePN (CPA) | обычно 18+ и налоговый статус для выплат (правила самих сетей; проверять в их офертах) | регистрация вебмастера, получение оффера, модерация площадки/канала |

**Для несовершеннолетнего в 2026 прямой легальной регистрации нет ни в одном канале.** Практические варианты: (а) оформление на совершеннолетнего родственника (самозанятый; выплаты — на его реквизиты; самозанятость доступна с 16 лет, а договоры до 18 лет заключаются с письменного согласия родителей — так схема «аккаунт на взрослого» остаётся обычной практикой), (б) ждать 18 лет, (в) до тех пор — «контент-режим без партнёрок»: подборки с артикулами ALI… (приём из практики ePN) без монетизации и без скрейпинга.

---

## Данные и лимиты

**Что можно показывать из API** (`product.query` / `productdetail.get`): заголовок, картинки, видео, `original_price`, `sale_price`, `app_sale_price` (в выбранной `target_currency`, цены в центах), `discount`, рейтинг (`evaluate_rate`), число заказов (`lastest_volume`), промокоды, комиссии, `ship_to_days`, ссылки на товар. Это ровно набор «AliExpress Content», предназначенный для рекламы (п. 3.1 соглашения).

**Изменения и кэш прямо не запрещены, но с ограничениями:**
- нельзя копировать/изменять/делать производные контента без письменного согласия (8.2/8.4) → показываем «как есть», со ссылкой;
- лимиты запросов официальные трёх типов — на приложение в сутки, на конкретный API, на пару «API+приложение» (https://open.alitrip.com/docs/doc.htm?treeId=502&articleId=108426&docType=1): при исчерпании квоты запросы отклоняются до 24:00 (по пекинскому времени), увеличения — по заявке через консоль. Конкретные числа (500/5000+ запросов в день на разных источниках) официально не гарантированы — смотреть реальную квоту в консоли приложения (https://console.aliexpress.com).
- **Итого для «радара»:** белая схема — поллинг по квоте своих отслеживаемых товаров, локальное накопление истории цен (собственные расчётные данные правилом не запрещены), показ цены/скидки/изображения «как есть» + официальная партнёрская ссылка.

---

## Правила по ботам и кэшу (что прямо разрешено/запрещено)

**Разрешено / нейтрально:**
- Боты и Telegram вообще: это трафик типа «Chat groups» из официального списка каналов Portals; в российской партнёрке прямо разрешены любые каналы, кроме запрещённых в РФ соцсетей (aePlatform, раздел «Условия») и запрещённых типов трафика (кликфрод, cookie stuffing, бренд-минус-слова и т.п. — https://www.admitad.ru/store/offers/aliexpress-rucis/ и https://support.mitgo.com/hc/en-us/articles/33313216721426-FAQ-on-working-with-AliExpress-programs-in-Admitad).
- Формат «диил»-/прайс-платформы: сервисы сравнения цен и каталоги со скидками — официальный разрешённый тип площадок; полный текст запрещённых практик доступен в правилах AliExpress на зеркале сети Awin: https://ui.awin.com/merchant-profile-terms/6378.
- Собственный кэш цен / слежение за изменением цены — правила не запрещают хранить и обрабатывать полученные по API данные у себя (главный запрет — на накрутку действий и на искажение/модификацию показываемого контента). Список мошенничества — п. 1.7 соглашения: «robots, frames, iframes, scripts, manual refresh», используемые для создания фиктивных кликов/заказов. Обычный бот, который показывает пользователю цены и честную партнёрскую ссылку, под это не подпадает; подпадают автоклики, скрытые iframe'ы и принудительные переобновления ради комиссий.
- Собственная накопленная история цен — ваша аналитика поверх полученных данных (запрета на собственную статистику нет).

**Запрещено:**
1. Скрейпинг сайта вместо/поверх API (не входит в лицензию «AliExpress Content», п. 1.2).
2. Изменение/переписывание/пересохранение чужих описаний/фото вне показа «как есть» без письменного согласия (8.2/8.4).
3. Бренд AliExpress в названии бота/канала/домена («AliExpress…», «алиэкспресс…») без письменного согласия (aePlatform, «aeprules»; то же — бренд-минус-слова в офферах CPA-сетей).
4. Накрутка: роботизированные клики, скрытые iframe/фреймы, принудительные переобновления, кликфрод, подмена рефереров, cookie stuffing (п. 1.7, 6.1, 5.3 соглашения; то же в правилах на зеркале Awin — https://ui.awin.com/merchant-profile-terms/7035).
5. Контекстная реклама по брендовым запросам AliExpress и «подмена» бренд-трафика (правила программ; enforcement — «Antispam deduction» Mitgo).
6. Чистый прайс-агрегатор без партнёрских ссылок: программа лицензирует контент именно для рекламы (п. 3.1), публикация цен без связи с рекламой — не легитимный кейс.

---

## Путь для нашего проекта (DealHunter)

**Вердикт для несовершеннолетнего создателя из РФ сегодня: УСЛОВНО / НЕТ-напрямую, но проект легально собирается тремя способами.**

1. **Сейчас (младше 18):** режим «без партнёрок и без скрейпинга»:
   - бот публикует подборки/алерты по артикулам `ALI…` (как советует ePN для Telegram) или обычные ссылки `aliexpress.ru/item/…`;
   - монетизации нет → правила партнёрских программ не нарушены (нет регистрации) и нет фрода; «радар» легален, пока не использует скрейпинг;
   - скрейпинг не использовать; можно опираться на артикулы и ручные публикации (использование официальных RSS/промо-страниц с указанием источника — серая зона).
2. **Легальная связка с взрослым (родитель-самозанятый/ИП):** оформить Portals-аккаунт на взрослого, создать приложение «Affiliate API», привязать бота к официальному набору `aliexpress.affiliate.*`; aePlatform (при 18+) — для повышенных ставок и API; так бот полностью «белый»: данные из API, ссылки через `link.generate`, история цен — собственные расчёты.
3. **Альтернатива с меньшим порогом входа:** CPA Admitad («AliExpress RU&CIS») или ePN — готовые фиды/диплинки/TG-бот вебмастера, вместо собственной интеграции с Open Platform; требования по возрасту те же (18+), но без консолей Alibaba.
4. **Правила гигиены для бота:**
   - имя бота/канала — без «AliExpress/алиэкспресс» (aeprules);
   - показывать товар «как есть» (фото/заголовок/цена из ответа API) + честную партнёрскую ссылку;
   - кэш — разумный (минуты), без «живого зеркала» всего каталога и без искажения цен;
   - никаких автокликов/переобновлений/iframe/fraud-механик (п. 1.7/6.1/6.7);
   - не копировать чужие описания текстом (ссылка+картинка+цена — достаточно);
   - квоты: стартовые лимиты всегда смотреть в консоли приложения (3 типа лимитов, articleId=108426).

**Итог одним предложением:** API легален и бесплатен, но доступ открыт только для правоспособных (18+ / самозанятый и т.п.) участников — пока автору нет 18, проект живёт в «контент-режиме» (артикулы + ссылки без партнёрки), а с 18 или через взрослого-самозанятого переезжает полностью на официальный Affiliate API.

### Оценка затрат

- **Деньги:** 0 ₽ (Affiliate API бесплатен, Portals бесплатна, aePlatform платы не берёт; CPA-сети для вебмастера тоже бесплатны).
- **Труд:** день-два (регистрация Portals + приложение «Affiliate API» → одобрение 1–2 дня → SDK + тест запросов).
- **Юридическое бремя для подростка:** нулевое в контент-режиме; при переходе на партнёрку — договоры от имени взрослого.

---

## Ссылки

**Первичные (официальные):**
1. AliExpress Affiliate Program Service Agreement (действ. с 01.04.2025): https://cdn.contract.alibaba.com/terms/b_platform_service_agreement/20250305142526766/20250305142526766.html?lng=en
2. AliExpress Open Platform (SPA): https://openservice.aliexpress.com/doc/api.htm и https://openservice.aliexpress.com/doc/doc.htm
3. Регистрация разработчика: https://open.alitrip.com/docs/doc.htm?treeId=674&articleId=118193&docType=1 (Overall Flow) и …articleId=118195 (AppKey)
4. Affiliate API — обзор: …treeId=674&articleId=118192; вызов шлюзов: …articleId=118934
5. Документы API: …apiId=45803 (product.query), 45794 (hotproduct), 48595 (productdetail), 45801 (category), 48596/48597 (featuredpromo), 46010 (smartmatch), 45804 (link.generate), 48598 (download)
6. Лимиты запросов (3 типа): https://open.alitrip.com/docs/doc.htm?treeId=502&articleId=108426&docType=1
7. aePlatform (официальная РФ): https://aeplatform.ru/ru/faq , https://aeplatform.ru/ru/partners , https://aeplatform.ru/ru/conditions , https://aeplatform.ru/ru/aeprules
8. EU DSA — binding commitments AliExpress (в т.ч. по партнёрской программе): https://digital-strategy.ec.europa.eu/en/news/commission-makes-aliexpress-commitments-under-digital-services-act-binding
9. Seller-API .ru (не для нас, но доказывает отсутствие публичного API .ru): https://business.aliexpress.ru/help/article/docs

**CPA-зеркала официальных программ:**
10. Admitad AliExpress RU&CIS: https://www.admitad.ru/store/offers/aliexpress-rucis/ ; фиды: https://support.admitad.ru/article/ru/102 ; сравнение программ: https://support.admitad.ru/article/ru/100 ; FAQ: https://support.mitgo.com/hc/en-us/articles/33313216721426 ; антифрод-выводы: https://support.mitgo.com/hc/en-us/articles/33313254502418
11. ePN AliExpress RU (активен в 2025): https://epn.bz/ru/info/faq/aliexpress_improvements/
12. Правила AliExpress на сети Awin (полные prohibited behaviors): https://ui.awin.com/merchant-profile-terms/6378 и https://ui.awin.com/merchant-profile-terms/7035

**Контекст по AliExpress Россия (состояние на 2026):**
13. ООО «Алибаба.ком (Ру)», 100% у AliExpress CIS Holding с 13.10.2023: https://adindex.ru/news/digital/2026/06/1/345531.phtml
14. Заморозка финансирования СП в 2022: https://meduza.io/news/2022/10/07/the-bell-vse-aktsionery-vo-glave-s-alibaba-otkazalis-finansirovat-aliekspress-rossiya ; https://www.kommersant.ru/doc/5607549
15. Финансы/работа платформы 2025–2026: https://iz.ru/2073477/2026-04-06/aliexpress-v-rossii-sokratil-ubytok-po-osnovnoi-deiatelnosti-za-2025-god ; https://www.rbc.ru/technology_and_media/10/03/2025/67cafd609a79472d173b1e1e ; https://ecomhub.ru/aliexpress-sng-staff-reduction-crossborder-ai-strategy-russia-market-china-imports-2025/

**Непригодные к цитированию источники (посчитаны клон/опечаткой):** `portals.laliexpress.com` — домен с копией лендинга Portals; не официальный.
