# Текущий план

## Отказ bootstrap при разборе signing identity

- [x] Воспроизвести реальный формат macOS, исправить парсер без смены сертификата и проверить read-only prerequisites.

После пользовательского запуска найден баг `_named_identity`: статус `(CSSMERR_TP_NOT_TRUSTED)` после имени исключал реально существующий configured certificate, а один SHA в секциях Matching/Valid ошибочно считался двумя identities. Регрессии с обоими форматами сначала падали; парсер теперь принимает status suffix и считает уникальные SHA только для точного configured name. Подпись установленного dev-app проходит codesign strict; её leaf совпадает с выбранным существующим сертификатом, реальные layout/serve/Python environment также прошли read-only bootstrap preflight. Trust store, сертификаты, signing method, Info.plist, конфиг и установленный app не менялись; bootstrap/sign/build/launch не запускались.

По отдельному подтверждению пользователя неверный ambiguity fixture (тот же SHA дважды) заменён на два разных SHA; assertions отказа при missing/distinct ambiguous/mismatched/ad-hoc identities сохранены. 139 связанных build/platform/runtime tests прошли; scoped Ruff check/format и diff --check прошли. Независимое ревью парсера: существенных замечаний нет, 21 focused тест и 6 синтетических probes прошли. Предупреждение legacy config `model` — отдельное и не менялось. При повторе bootstrap/watch использовать fish `and`, чтобы watcher не запускался после failed bootstrap.

Пользователь после повторного запуска и инструкции включить Background Process/дать Microphone access подтвердил: «да все работает». Это ручное пользовательское подтверждение работы приложения/диктовки, не выполненный агентом live smoke и не отдельная проверка автоматического handoff при следующем изменении Swift. Затем явной командой «запушь» разрешил commit/push этого исправления в main. Перед публикацией повторены 139 связанных тестов, полный Ruff check/format (97 файлов) и diff --check; всё прошло. В commit входят только build.py, два файла тестов и эта запись; research/watchdog исключены. SHA удалённой ветки и CI проверяются после отправки.

## Финальная проверка проекта и push

Согласовано 9 ноября 2026: проверить весь текущий код на ошибки, дублирование и лишнюю сложность, затем отправить весь проверенный код/тесты/документацию и два прежних локальных коммита в `origin/main`. `research/` и `WATCHDOG.yml` оставить локально. Это явное разрешение на commit/push для данного шага; deploy и изменения пользовательских данных не разрешены.

Проверка: полный pytest/coverage и Ruff, четыре Swift typecheck, ограниченный независимый проход по core Python и native/build/watch; структурный поиск повторов с ручной проверкой реальных контрактов. Исправлять только подтверждённые существенные проблемы с регрессией до кода; не устраивать новый массовый рефакторинг. Перед push — проверить точный staged состав, отсутствие личных/секретных артефактов, remote/base и сохранность остальных локальных файлов; после push сверить SHA удалённой ветки. Живые API/микрофон/записи/установленный bundle не использовать; рабочие acceptance-сценарии без такого запуска не выдавать за проверенные.

- [x] Выполнить финальную проверку и подготовить согласованный набор к публикации.

Финальный аудит нашёл и закрыл четыре P2 с RED→GREEN регрессиями: падение обычного native client от SIGPIPE (checked SO_NOSIGPIPE на своём сокете); Apply до первого PCM/завершения native callbacks (синхронный preflight veto до configure); вечное ожидание dictation/jobs после startup failure (отдельный failure event, ready по-прежнему означает успех); RuntimeError optional chapters, отменявший публикацию готового transcript/summary (обрабатывается только в необязательной ветке, без повторного ASR/LLM). Изменения Python и native прошли отдельное независимое review1 без существенных замечаний.

После fetch локальные два старых коммита оказались уже включены в origin/main; удалённая ветка содержит ещё четыре коммита до `45cdca8`. Создан локальный checkpoint `9ce6dbf`, затем интегрирован upstream fix SINGLE_REMOTE/приоритета явных имён/cache9 с сохранением текущих cloud/local/backend/windows/terms. Входящие тесты сохранили все assertions; изменён только старый fake `.asr["ru"].calls` на эквивалентный текущий `.call_asr.calls`. Разрешение конфликтов независимо проверено по diff/AST, 37 calls tests прошли. Публикация — обычный push main после merge-коммита, без force; фактический remote SHA проверяется отдельно после отправки.

Финальная объединённая версия: полный pytest/coverage — 743 passed, 84.23%; Ruff check/format --check (97 файлов), четыре Swift typecheck, git diff и staged diff --check — exit 0. `uv lock --check --offline` и `uv pip check` прошли. Дополнительно собраны изолированный настоящий Swift app executable (Mach-O arm64) и AppIcon без запуска/подписи/установки. Поиск AST не выявил точных дубликатов Python function body от 12 строк/3 statements; это ограниченный detector, не доказательство отсутствия семантических повторов. Два независимых scoped ревью проверили основные Python/native потоки; helper-долг в conversation и legacy chapters chunker отмечены в ARCHITECTURE, не удалялись/не переписывались.

Состав публикации проверен по allowlist кода/тестов/docs и сканирован на характерные секреты/бинарные артефакты: совпадений нет; research/watchdog остаются локально. Живые providers, ASR quality, разрешения macOS, установленный app и пользовательские файлы не проверялись и не трогались. Deploy, реальные retention/storage-операции и API uploads не выполнялись; сверх принятого upstream fix форматы хранения не менялись. CI на GitHub не выдаётся за пройденный до фактического завершения run.

## Dev-watch Swift — один шаг

Согласовано: отдельный opt-in режим для dev-приложения `~/Applications/Wav2Sum.app`; пользователь один раз вручную переходит на него. Изменять `build.py` разрешено, bundle id, способ подписи и Info.plist не менять. Bootstrap отказывается от standalone и несовпадающей/временной подписи; отдельный подписанный ресурс содержит отпечаток исходников. Новые Swift-исходники собираются автоматически, работающая диктовка завершается до переключения; Python-демон, записи и задачи не останавливаются. Между версиями горячая клавиша ненадолго недоступна. Никаких изменений standalone, хранения данных и daemon-протокола; без новых зависимостей. Не устанавливать и не перезапускать рабочее приложение во время разработки.

Текущий шаг после ревью — упрощение: один stage-only сборщик для обычной установки/bootstrap/watch, один отпечаток с именами и границами файлов, watcher без откатов из-за обычного editor-save после начала handoff, один поток выполнения Swift с узкими подменами внешних эффектов. После подтверждённого успешного handoff удаляются только собственные временные результаты нового запуска; при неопределённом исходе пара bundle сохраняется, старые каталоги до сессии не очищаются. Согласовано отдельно: Ctrl-C ждёт окончания диктовки/исправлений и закрывает только dev-app, не daemon, чтобы новый watcher можно было запустить без переподключения.

Проверка до кода: регрессии boundary/rename digest, immutable build snapshot, editor-save во время handoff, завершение dev-сессии/неопределённый exit и ограниченное число принадлежащих watcher staging; native harness той же логики без живого mic/AX/clipboard/daemon. Затем изолированный pipe/atomic-swap smoke, полный pytest/Ruff, Swift typecheck, граф модулей и независимое ревью. Никаких deployment, пользовательских данных, Keychain/API и commit/push.

- [x] Упростить и проверить безопасное автоматическое обновление dev-приложения.

Результат реализации: убран `devwatch_platform.py`; общий `build.source_snapshot/source_digest/build_app/verify_dev_app` используется установщиком и watcher. Отпечаток v2 разделяет argv, signer, относительные имена и длины файлов. После native ready watcher завершает установку своего снимка, следующие editor-save обрабатывает новым проходом. Собственный scratch ограничен одной попыткой и освобождается после ACK; pre-existing каталоги не трогаются. Native условные тестовые ветки заменены узкими Effects с реальными defaults; исправлены Apply после Turn Off, явная повторная попытка старта и защита неопределённого Settings ACK. Новая семантика Ctrl-C проверена через настоящий CLI entrypoint и дочерний synthetic Swift process.

Измерено относительно сохранённого scoped baseline: рабочий код −345 строк, один Python-модуль удалён, Swift `#if` 29→1 (только entrypoint). Итоговый `uv run --offline --no-sync pytest --cov -q` — 727 passed, coverage 84.21%; Ruff check, format --check (96 файлов), git diff --check — exit 0. Все четыре Swift typecheck прошли, прежние audio deprecations остаются. Фактический pydeps — 39 модулей, 112 внутренних edges, ацикличен. Независимое review0 native: существенных замечаний нет. Python review0 выявило P2: Ctrl-C при незавершённом prepare reply оставлял dev-app запущенным. После отдельной RED-регрессии fix1 откладывает первый SIGINT до завершения control reply, второй допускает прерывание; проверены initial protocol/prepare, graceful exit, второй abort и восстановление handler. Независимое Python review1: P2 закрыто, существенных замечаний нет; повторены 60 watcher/runtime/integration tests и 7 изолированных timing probes (все прошли). Проверенная версия исходников совпадает с итоговой. Шаг завершён без установки рабочего приложения.

Граница проверки: только fake/synthetic app/mic/AX/clipboard/daemon/signing и isolated atomic-swap. Установленный bundle, настоящий daemon, разрешения macOS, Keychain, реальные записи/история не трогались, live handoff с настоящей подписью не проверялся; bootstrap/переход на watcher пользователь выполняет отдельно в idle. Commit/push отсутствуют. Лишних строк вне текущего шага нет.

## Cloud: компактная панель диктовки

Согласовано: вернуть для Cloud прежнюю плашку 190×40 по центру снизу, не меняя локальный preview. Пользователь подтвердил, что Option работает, а панель оказалась на другом мониторе; обработку клавиши не меняли.

- [x] Вернуть прежнюю Cloud-плашку, сохранив Local и проверить поведение.

Проверки: новый AppKit harness сначала не компилировался без Cloud-режима HUD, затем прошёл проверку геометрии и состояний на доступном экране; Swift app typecheck прошёл с прежними предупреждениями audio API, pytest — 560 passed, coverage 82.47%, Ruff check/format прошли. Независимое ревью обнаружило потерю экрана Local при переключении режимов; исправлено выбором экрана по сохранённой позиции, повторное ревью существенных замечаний к изменению HUD не выявило. Реальные несколько экранов, drag, физическое нажатие, визуальный результат и установленный bundle не проверялись; приложение и демон не перезапускались, аудио не отправлялось. Все изменения относятся к шагу.

Реализация private-beta credentials, проверки и запуск новой standalone-версии у автора завершены. Для рабочего friend DMG остаётся локальный скрытый ввод новых выделенных ключей через `--private-beta`; ключи не предоставлены, старые Keychain/env для персонализации не использовались.

## Private beta с готовым Cloud

Контракт: обычный build по-прежнему без ключей. Отдельный `--private-beta` запрашивает оба выделенных ключа скрыто в interactive terminal, не из argv/env/Keychain; только копия package в новом signed artifact получает credentials resource. Runtime precedence: explicit env → собственный Keychain → включённые beta-ключи; отказ Keychain не обходить. Режим остаётся явным, Local не обращается к cloud credentials, auto uploads по-прежнему требуют отдельного согласия. Settings сообщает о включённом beta-доступе, не возвращая секреты через status.

Проверка до реализации: фактическое получение включённых ключей при пустом fake Keychain; env/свои ключи выше beta, malformed resource и отказ Keychain fail closed; redirected/небезопасный ввод не допускается, invalid/empty keys не создают artifact. Затем actual private bundle/DMG с заведомо синтетическими ключами, isolated interpreter lookup/status без Keychain/provider запросов, codesign/DMG verify. Реальные beta-ключи пользователь вводит сам локально; demo artifact не выдаётся за рабочий Cloud.

Запуск у автора выполнен: свежий status подтвердил ready/cloud без recording/queued/running jobs, старый app штатно закрыт, idle daemon подтвердил shutdown. `computer.apps.open` запустил `friends-internal-2026-11-03-final/Wav2Sum.app`; running path проверен, окно первого запуска появилось, daemon не стартовал до человеческого Apply. Установленный `~/Applications/Wav2Sum.app`, identity/signing и Keychain не менялись. AX window lookup и screen capture недоступны host; разрешения не менялись. Пользователь предупреждён о штатном seven-day prune после Apply.

Доказательства private-beta реализации:
- До реализации: четыре ожидаемых failure на отсутствии beta lookup и нового аргумента сборщика; после — **560 passed, coverage 82.47%**, Ruff check/format (86 файлов), Swift app typecheck. Прежние Swig/macOS 27 audio deprecations остаются.
- Настоящий CLI builder в PTY запросил оба синтетических ключа без echo, собрал и подписал private app/DMG, прошёл relocation imports/decode и `codesign --verify --deep --strict`/`hdiutil verify`. Встроенный interpreter прочитал beta resource при пустом fake Keychain; status не раскрыл ключи и сохранил first-run Local. Реальный Keychain и провайдеры не вызывались.
- Actual SetupWindow в изолированном native harness показал ordinary/beta notice, пустые secure fields, отказ Apply без cloud consent и успешный Apply после него. Owned-view bitmap `friends-private-beta-proof/settings-beta.png` просмотрен; это не screenshot пользовательского desktop.
- Новые tests покрывают precedence env/свои ключи/beta, malformed resource, отсутствие denial bypass, input/invalid-key failure без output, passive status без секретов и implicit Cloud. `ARCHITECTURE.md` обновлён по actual pydeps: 37 модулей, 108 внутренних edges, ацикличен; новый edge distribution→credentials.
- `README.md`/`SPEC.md` описывают скрытый ввод, private resource вместо автоматического Keychain import, приватную раздачу и человеческий Cloud/auto-upload consent. Секретный resource и private DMG добавлены в ignore rules; зависимости/протокол daemon/хранилище записей не менялись.
- После proof удалены synthetic-only app/DMG и временные smoke drivers; durable evidence — `~/Library/Caches/wav2sum/friends-private-beta-proof/verification.json` и PNG. Реальные beta-ключи не читались/не сохранялись, рабочий friend DMG с ними ещё не создан; paid API/audio-text uploads, commit/push не выполнялись. Изменений, не нужных шагу, нет.

## Закрытое распространение для друзей

Предположения: один Apple Silicon app, существующий bundle id и способ подписи не меняются; сборка в отдельный output, никогда не поверх ~/Applications. Runtime и native helpers включены в bundle, проектная venv и Xcode на машине друга не нужны. Developer ID/notarization требуют отдельной доступной подписи; не выдавать внутреннюю сборку за публично notarized release. Новые зависимости не добавлять без согласования.

Первый запуск: явный local/cloud, объяснение передачи/стоимости cloud, secure Keychain entry по действию пользователя, отдельная подготовка local-моделей и разрешений. Выбранный пользователем режим хранится в app preferences, не в TOML и не выбирается по наличию ключей. Переключение режима/моделей только idle, без потери активной записи/диктовки/jobs. Auto-process выключен по умолчанию; local только transcript, cloud transcript+summary с отдельным подтверждением автоматической отправки.

Local: существующие Qwen 1.7B/Gemma E4B и Nemotron; лёгкий local call backend на Qwen, без скрытого cloud fallback. Профили по фактическому чипу/RAM/диску с ручным override; 16/24/32/48 GiB — классы, не выдуманные результаты измерений. Текущая машина M5 Max/48 GiB; M4/Pro/Max нужно измерять на реальном железе друзей тем же runner. Новые веса загружаются только явным действием. Gated HF terms не принимать автоматически.

LoRA: read-only просмотр собственных текстовых примеров, отбор/фильтр, «выбрать все показанные»/«снять выбор», редактирование target и отдельное подтверждение экспорта для обучения. Экспорт в выбранный новый файл, без перезаписи, только allowlisted текстовые поля; никаких audio/calls, ключей, путей, app/title, автоматической отправки или новой бессрочной копилки истории. Retention/edits/vocabulary не менять.

Проверки до реализации: режим/автоматическая обработка на fake recorder и socket, отсутствие обработки по умолчанию и отказ busy-switch; экспорт только выбранных подтверждённых текстов без изменения источников/overwrite; аппаратные границы и настоящие local inference benchmarks; перенос bundle в другой путь и запуск встроенного Python/helpers с изолированными HOME/config и без проектных путей. Native UI smoke, Python/Ruff/Swift gate один раз после интеграции. Реальные calls/dictations в smoke не читать.

- [x] Переносимый внутренний bundle и DMG; проверка relocation и отсутствия пользовательских файлов/секретов.
- [x] Native setup/settings, явные режимы и отдельный opt-in auto-process.
- [x] Просмотр/выбор/редактирование/экспорт LoRA-примеров с согласием.
- [x] Local-профили, лёгкий call backend, подготовка моделей и воспроизводимые реальные замеры.
- [x] Интеграционные smoke, полный gate и инструкции для друзей с честными ограничениями доступного железа/подписи.

### Результат и доказательства

- Финальная внутренняя сборка: `~/Library/Caches/wav2sum/friends-internal-2026-11-03-final/Wav2Sum.app` и `Wav2Sum-Internal.dmg` (410 723 045 байт). DMG SHA-256: `f867e7845694c03a9030608f92c836fe2023afd1930b6a69e1322e0c3a5aeef6`. `codesign --verify --deep --strict` и `hdiutil verify` прошли. Это не notarized release; текущий MLX требует macOS 26.2+, Xcode/uv/checkout на машине друга не нужны.
- Полный Python gate: **549 passed, coverage 82.19%** при пороге 80%; Ruff check/format прошли (86 файлов). После изоляции macOS memory lookup в startup-failure test этот тест повторно прошёл; два прежних Swig deprecation warnings остаются. Все четыре Swift typecheck прошли, финальный builder также скомпилировал actual app/capture sources.
- Relocated interpreter прошёл isolated-home imports, helper resolution и FFmpeg/TorchCodec decode. Настоящий встроенный `setup` в новом HOME вернул local/uncompleted без daemon socket. Отдельная anonymous-загрузка публичного Qwen `config.json` в временный cache прошла без токена: это metadata, не пользовательское аудио/текст.
- Настоящие daemon/socket и CLI setup на изолированных fixtures подтвердили idle-only reconfiguration, refusal busy-switch, manual default, explicit local auto transcript, сохранение перед остановкой и consent/selected-only/no-overwrite export без изменения источников. Failed warm-up виден также новым клиентам как failed, а не вечный loading.
- Native AppKit harness проверил mode/auto-cloud consent gates, Apply через JSON pipe, фильтр/Select All Shown, сброс подтверждения после правки и экспорт одного подтверждённого target. Исправлена найденная runtime-ошибка активации constraints до общего ancestor. `friends-native-proof/settings.png` и `sharing.png` — bitmap renders настоящих owned views, не desktop captures: разрешение screen capture для host отсутствует, AX window lookup не сработал; разрешения не менялись.
- Реальный packaged-runtime inference: RU/EN/mixed × Qwen/Nemotron, три warm repeats ASR и Gemma cleanup, **6/6 прошли** с HF offline и изолированным HOME; существующий cache весов использовался read-only. Рядом с DMG сохранены `embedded-model-results.json` и `Wav2Sum-Benchmark-Corpus.zip` с WAV/reference/hash manifest для одинаковых входов на Mac друзей. Cleanup WER без отдельного edited reference теперь null, не оценка семантического качества.
- Отдельный полный local call smoke держал Qwen/Gemma dictation resident и выполнил stereo VAD/pyannote/ASR обоими backend: transcript без summary, call ASR освобождён. Результаты — `friends-local-benchmark-20261103-122752/full-workload-*.json`. У Nemotron наблюдался рост system-wide swap; чужие процессы не контролировались, RSS не равен полной unified memory. M4/Pro/Max и 16/24/32 GiB не измерялись: профили provisional, runner и общий corpus передаются друзьям.
- Packaging потребовал совместимый pinned FFmpeg 8.1.3 вместо установленного 9, все три TorchCodec 8 families, сохранение нужного Torch `_inductor/test_operators.py` и declared setuptools, различение LC_ID_DYLIB от зависимости и удаление только мёртвых build rpaths. LGPL source/license/reproduction metadata включены. Существующий системный FFmpeg и bundle identity/signing mechanism не изменялись.
- Regression до/после выявила небезопасные linked lock files: общий `exclusive` теперь не truncates, открывает `O_NOFOLLOW|O_NONBLOCK`, проверяет regular file/single hardlink; sharing не resolve-ит финальное lock name. На fixtures подтверждены отказ symlink/hardlink и неизменность источников; форматы/пути/retention реальных данных не менялись.
- `README.md`, `SPEC.md`, `ARCHITECTURE.md` обновлены; graph получен actual pydeps: 37 модулей без root, 107 внутренних edges, без cycles. Temporary smoke drivers и synthetic native app directories удалены; результаты/visual proof сохранены. `verification.json` рядом с финальным DMG содержит сводку доказательств.
- Не выполнялись live install/restart, платные API-проверки, выдача новых ключей, реальные exports/pruning, upload recordings/text, LoRA training/server, commit/push. Экспорт для LoRA ручной, без фоновой отправки. Изменений, не нужных этому шагу, нет.

## Последний звонок и retention диктовок

Предположения/контракт: кнопка работает только ready/cloud, не во время записи. Последний собственный завершённый звонок выбирается по started_at sidecar (fallback — время файла/имя записи при недоступном audio), не по времени изменения summary/output. Готовый summary только открыть; существующий transcript использовать без ASR; raw запись обработать прежним pipeline один раз. Partial/ambiguous output не перезаписывать, повторный запрос возвращает уже активную job. Local summary остаётся запрещённой, остановка записи по-прежнему ничего не ставит в очередь.

RPC `summarize_last_meeting` с существующим cloud-флагом: готовый результат `{out_dir, already_done:true}` либо стандартная `{job}`; Python/Swift меняются вместе, существующие клиенты и job-поля сохраняются. Новая summary-only обработка добавляет только summary.md, не переписывает transcript/audio/meta. Проверки — временные записи, grounded summary fixtures, локальный fake HTTP/Unix daemon и настоящий AppKit menu; никаких real last-call API uploads.

Retention: fixed 7*24h, strict older-than cutoff, startup + hourly background maintenance вне dictation/paste path. Существующий history lock и atomic replace, сохранение retained bytes/order/mode; неизвестные/naive/future timestamps сохраняются, malformed JSON/UTF8 или I/O failure оставляет файл целиком. Успешная очистка шлёт count-only `history_pruned`, TUI перечитывает список; словарь и edits не затрагиваются. Policy без новых knobs/dependencies. Применение к накопившимся рабочим данным — только после запуска обновлённого daemon пользователем, не при проверках агента.

Тесты до кода: native runtime harness сначала упал на отсутствии menu item; retention tests — на отсутствующем prune_dictations. Затем backend-регрессии выбора/повтора/existing summary/no overwrite и reuse transcript, periodic cleanup на temp history. После интеграции — pytest/cov/Ruff, все Swift typecheck, изолированный RPC→ASR/summary smoke, runtime native menu и доступная визуальная проверка; не устанавливать app и не перезапускать рабочий daemon, так как это активирует реальные удаления.

- [x] Menu bar action и backend последней встречи, без повторного ASR и перезаписи.
- [x] Seven-day retention с явным согласием для старых/новых, без потери vocabulary/edits/calls.
- [x] Реальные изолированные smoke, полный gate, docs/architecture и инструкции активации пользователем.

Проверено: Ruff check/format; полный pytest — 469 passed, coverage 87.63% (порог 80%); все четыре Swift typecheck прошли с существующими предупреждениями deprecated audio API macOS 27. Native AppKit runtime harness проверил состояния menu item, RPC, повторные запросы, открытие готового результата и reset после disconnect: fake daemon получил ровно 5 cloud-запросов. В harness исправлено закрытие stream/socket fake peer, без изменения продуктового кода или ослабления assertions.

Изолированный настоящий Daemon.run/RPC → локальные HTTP fixtures: raw звонок распознан ровно один раз, дубликат возвращает ту же job, готовая summary не перезаписывается, сохранённый transcript обработан без audio/повторного ASR; startup retention удалил только просроченную fixture, сохранил current/vocabulary/edits. Всего 1 ASR + 4 summary HTTP-запроса к localhost, 0 внешних API-запросов. Настоящий TUI в PTY после history_pruned показывает только CURRENT; screenshot проверен. Снимок установленного menu bar недоступен: host capture denied/0 displays; права не менялись, вместо этого выполнен native runtime harness.

Доказательства: ~/Library/Caches/wav2sum/meeting-menu-retention-2026-11-03/verification.json и retention-tui.svg. ARCHITECTURE.md обновлён по настоящему pydeps: 32 модуля, 91 внутренняя связь. Временные smoke scripts удалены после проверок. Для активации пользователь закрывает старое menu-bar приложение и запускает `uv run --offline --no-sync wav2sum app --cloud`; startup сразу применит необратимую семидневную очистку к накопленной истории диктовок. Коммита, установки, перезапуска рабочего daemon и удаления реальной истории не выполнялось.

## Завершённый предыдущий шаг

## OpenAI preconnect во время речи

Evaluator: `env PYTHONPATH=. uv run --offline --no-sync python ~/Library/Caches/wav2sum/insertion-benchmark-2026-11-02/luna-preconnect.py`. Три прежних synthetic raw текста, точный Dictator cleanup payload, новые cold connections против TLS-only connect в 3.2-секундном окне речи; два alternating AB/BA повтора на текст, 12 LLM requests, никакого paid warmup/HTTP/ключа внутри preconnect.

Baseline/candidate измерены до product edits: 5 из 6 paired complete times лучше с заранее подключённым socket, в одной long паре хуже на 0.088 с. Все шесть preconnected sockets реально использованы POST, complete stop outputs попарно совпали, model/tier gpt-6-luna/fast. Медиана парного выигрыша 1.072 с в этой маленькой выборке, не чистая оценка TLS и не SLA: provider variability также влияет. Артефакт `luna-preconnect-1793651641208220000.json`, 12 полных responses, 9236 observed tokens. Предварительный TLS-only probe не делал HTTP requests; существующий stale-peer check этот socket не отбрасывал.

Принятый минимальный дизайн: nonblocking preconnect с непустого cloud audio, только когда включена LLM cleanup; продолжающееся аудио обновляет обнаруженный закрытый socket, никаких запросов/ключей для прогрева и никакого reconnect timer в простое. Network connect вне transport lock, один worker на клиент. Foreground POST не ждёт warming и не replay-ится; если прогрев не успел/упал, работает обычный прежний путь. Unload/origin/proxy change инвалидирует незавершённый connect, поздний socket не может восстановить закрытый клиент или заменить foreground connection. Настроенный proxy не обходится: preconnect пропускается, HTTP остаётся на прежнем proxy-aware urllib. DNS/connect cancellation best-effort, без обещания мгновенного завершения.

Проверки до кода: реальный локальный HTTP server подтверждает отсутствие preconnect HTTP и повторное использование того же socket; stalled/failed warm не блокирует chat; unload/foreground race отбрасывает поздний socket; single-flight и proxy skip. Затем реальный paired smoke через новую реализацию, полный pytest/cov/Ruff, docs/pydeps, idle reload. Числовой latency SLA/допуск не задан: не выдумывать CI budget и не гарантировать выигрыш на каждом запросе.

- [x] Проверить cold vs preconnect на исходных payloads.
- [x] Реализовать только доказанную оптимизацию, сохранив lifecycle/proxy/no replay.
- [x] Проверить реальный путь, полный gate, docs и runtime.

### Реальное поведение и проверка продлённого preconnect

Тесты до кода: 5 новых lifecycle/proxy сценариев падали на отсутствующем OpenAILLM.preconnect. После реализации 91 целевая проверка прошла. Первый actual DictationStream smoke (`preconnect-integrated-1793652254603889000.json`) остановился на проверке reuse длинного входа: два полных ASR/Luna ответа, но warming socket не использован. Выигрыш этой пары не приписывается preconnect, failed artifact сохранён.

TLS-only `tls-idle-probe.py` не читал ключи и не делал HTTP: socket не readable в 3.2/11.6 с, readable в 23.6 с, recv подтвердил peer_closed (`tls-idle-probe-1793652458119442000.json`). Это не ошибка TLS stale-check: сервер действительно закрыл ожидающее соединение. Разовый connect на первом чанке недостаточен для длинной диктовки.

Добавлена отдельная real-HTTP регрессия continued speech после закрытого preconnect: сначала failed, затем passed после проверки/обновления connection на следующих непустых audio chunks. Все 6 новых preconnect-проверок прошли: warm не читает ключи/не делает HTTP, foreground использует тот же socket, configured proxy не обходится, stalled/failed connect не блокирует chat, concurrent warm single-flight, unload/foreground race не публикует поздний socket. Штатный POST и отказ от неоднозначного replay не менялись.

Новый actual DictationStream/ASR/Luna smoke с paced PCM и свежим клиентом на каждую ветку (`preconnect-renewal-1793652753475472000.json`) завершился: 3 пары, 6 полных ответов, prepared socket reused во всех warm ветках, no paid warmup. Raw и cleaned тексты совпали попарно, контрольный captured tail сохранён. На long потребовались два последовательных TLS workers из-за peer timeout, на short/mixed по одному; одновременно не более одного.

| Вход | Luna cold, с | Luna preconnect, с | Весь cold путь, с | Весь preconnect путь, с |
| --- | ---: | ---: | ---: | ---: |
| captured long | 2.217 | 1.471 | 2.478 | 1.956 |
| short | 2.071 | 1.708 | 2.284 | 1.924 |
| mixed RU/EN | 1.791 | 1.475 | 2.874 | 1.711 |

По одной паре на вход, после речи до готового текста Python, без UI/AX и без p95/SLA. Luna-выигрыш 0.317–0.745 с, медиана 0.362 с; не гарантируется каждому запросу. В mixed одновременно различалась ASR-пауза (1.083 против 0.236 с), поэтому весь total-выигрыш нельзя приписать preconnect. Максимальный add в warm ветках 3.54–7.80 мс, не нулевая цена и не установленный CI budget. Model/tier gpt-6-luna/fast, stop, token budget/prompt/ответы не изменены.

Финальный gate: `ruff check`, `ruff format --check`, `pytest --cov -q` — **449 passed**, **87.98% coverage** при пороге 80%; только прежние Swig deprecation warnings. Swift/typecheck не запускались, native не менялся. Реальный pydeps — **32 модуля / 91 внутренняя связь, DAG**. README/SPEC/ARCHITECTURE обновлены; зависимостей, конфигурационных параметров, storage/signing/protocol изменений нет. Оба временных integration smoke scripts удалены; cache evaluator, TLS probe и все JSON сохранены. Commit не делался; изменений вне шага нет.

Idle ready/cloud daemon **51937** (recording=null/jobs=[]) остановлен после gate; уже открытая native app сама запустила **55887**, свежий status — **ready/cloud**, 384 MB, recording=null/jobs=[]. Отдельный serve/rebuild/permission grant не запускался. Старое TOML `unknown setting model` осталось, config не трогали. Нового физического ⌥→paste/AX прогона нет: реальный backend/API и runtime readiness проверены раздельно.

Учёт этого шага: **20 LLM requests / 15516 observed OpenAI tokens** (12/9236 первоначальные пары, 2/1662 остановленный first-only эксперимент, 6/4618 финальный smoke), **8 ASR requests**; все provider responses полные, неоднозначных POST replay не было. Два TLS-only probes и все preconnect handshakes не делали HTTP/не читали ключи/не расходовали model tokens. Денежная стоимость не наблюдалась.

## Завершённый предыдущий шаг

## Cloud latency: Luna и полный финал после речи

Baseline и workload: три сохранённых синтетических входа из `~/Library/Caches/wav2sum/insertion-benchmark-2026-11-02/`, включая captured PCM с известным SHA-256 и пропадавшим окончанием. Последний реальный полный Dictator smoke: long ASR 1.461 / Luna 6.039 / total 7.500 с; short 1.088 / 1.723 / 2.810 с; mixed 2.196 / 1.434 / 3.630 с. Это n=1 и не UI/AX latency. Личные history/vocabulary/config не читать; внешние запросы только разрешённым Deepgram/OpenAI на этих синтетических данных. Без новых зависимостей, смены модели/промпта и скрытых повторов.

Evaluator Luna: `env PYTHONPATH=. uv run --offline --no-sync python ~/Library/Caches/wav2sum/insertion-benchmark-2026-11-02/luna-profile.py`; `--cap-study` сравнивает прежний output budget с щедрым input-dependent cap, `--clean-endtoend` измеряет actual Dictator.clean/credential/HTTP. Точный production cleanup payload, alternating paired requests, время headers/первого непустого delta/последнего content/full completion, model/tier/usage и все synthetic outputs. Эти три исследования завершены; SSE и cap не приняты как оптимизации.

Evaluator ASR: `env PYTHONPATH=. uv run --offline --no-sync python ~/Library/Caches/wav2sum/insertion-benchmark-2026-11-02/asr-overlap.py`. Сравнить один paced chunked HTTP request во время речи с полным обычным WAV HTTP после неё; тот же Nova-3/RU-EN/PCM, финал только из полной HTTP response. До cutover подтвердить captured tail в повторных прогонах, exact PCM hash/order и меньшую post-release паузу; отдельно учесть перенесённые во время речи TLS/upload. Интерим-текста и доказательства внутреннего inference до EOF этот API не даёт.

Приёмка: не вводить числовой SLA/CI latency budget без согласованного допуска; полный контрольный хвост обязателен, ни одного потерянного/повторно отправленного чанка или автоматического платного replay. Изменение — только при воспроизводимом выигрыше на сопоставимых полных результатах; SSE без улучшения полного времени или неповторяемую разницу отклонить. Для принятого ASR изменения сначала регрессии, затем код, локальный HTTP smoke lifecycle/cancel/finalization, настоящий provider smoke, полный pytest/cov/Ruff и docs/pydeps по факту; Swift не менять без необходимости. Сохранить все попытки, ошибки и неизвестный remote outcome, не выдавать маленькую выборку за гарантию WER/p95.

- [x] Luna: TTFT/generation, output-cap A/B и actual cleanup overhead; принять только подтверждённую оптимизацию.
- [x] ASR: сравнить прежний batch с одним HTTP upload во время речи, проверить полный финал и lifecycle.
- [x] Интегрировать доказанное улучшение либо явно отклонить неподтверждённое; записать реальные проверки, ограничения и runtime состояние.

### Результат Luna

`luna-profile-results.json`: 18 попыток, 17 полных ответов, 8 успешных SSE-пар. Median SSE TTFT 1.016 с, first-to-last content 0.262 с; median paired SSE-minus-nonstream completion −0.031 с, изменения разнонаправленны. SSE даёт видимость первого токена, не доказанное ускорение полного текста. Один SSE сбой с неизвестной причиной/remote usage сохранён; после исправления draining evaluator продолжил только оставшиеся scheduled requests в новом процессе, автоматического replay не было.

`luna-cap-results.json`: 12 полных ответов, прежние 32768 против `min(32768,max(256,2*len(raw.encode("utf-8"))+256))`, неизменные prompt/model/tier. Median paired full-time difference −0.089 с, без первой cold пары −0.072 с, с разнонаправленными результатами; устойчивого выигрыша не доказано. Все ответы stop, outputs в сравниваемых парах совпали, но это не доказательство безопасности cap на иных входах. Оба кандидата отклонены, production Luna не менялась.

`luna-clean-endtoend.json`: 6 настоящих Dictator.clean, без paid warmup. Cleanup 1.061–4.514 с, HTTP 1.058–4.512 с; credential 1.40–2.97 мс, остальная local-работа 0.36–0.61 мс. В этих прогонах ожидание находится внутри HTTP/network/provider, не в локальной подготовке или Keychain. Сеть/очередь/prefill отдельно не разделены; состав прежних 6.039 с неизвестен. Served model/tier — gpt-6-luna/fast, cached tokens=0.

### Результат ASR и принятый код

`asr-overlap.json`: 18 начатых ASR requests, 17 полных ответов с текстом прежнего WAV reference, один captured request прерван in-flight с неизвестным remote outcome; не replay. `asr-overlap-followup.json`: 3 новые парные captured пробы, все 6 network requests HTTP 200, совпадают весь текст, PCM/body SHA и duration. Chunked release→response 0.239/0.319/0.390 с против WAV 1.540/1.936/2.107 с; выигрыш 1.300/1.617/1.716 с. TLS/startup перенесён до release; отдельный WAV connect/TLS 0.552–0.653 с и residual response wait 0.963–1.381 с. Это доказывает меньшую паузу, не момент начала inference. Локальная ошибка followup instrumentation до создания socket записана отдельно и не считается provider request. Документация binary/linear16 не даёт явной гарантии chunked buffering; риск и конечный размер выборки сохранены, Flux/другие providers не подключались.

Приняты `cloud.DeepgramUpload` и lazy `DeepgramTranscriber.start_dictation`; `DictationStream.add` немедленно передаёт owned PCM, cloud step не выдаёт preview. Один worker/stdlib urllib iterable body, default proxy/verified TLS/_NoRedirect, raw linear16/16 kHz/mono, прежние model/language/terms. Ограниченный backlog, overflow — ошибка без потери/повтора; один заранее готовый крупный вход допустим. Finish закрывает input после хвоста, ждёт полный response с release-based timeout 20 с, кэширует успех; close будит ожидание, отбрасывает результат и делает best-effort socket shutdown без ожидания DNS/connect. Весь cloud float PCM больше не накапливается.

Тесты до кода: новый контракт дал 18 failed / 6 passed на прежнем batch. После реализации 85 целевых проверок прошли. Дополнительно воспроизведён новый compatibility bug: один целый вход >8 MiB PCM ошибочно считался backlog overflow; отдельная real-HTTP регрессия сначала упала, после исправления допуска крупного единственного чанка проверяет весь PCM/хвост без нового ограничения длительности. Переполнение настоящего накопленного backlog по-прежнему ошибка. Assertions для исправления багов не менялись; старые deferral-тесты заменены именно из-за явно изменённого streaming-upload контракта.

### Настоящий интегрированный smoke

`integrated-upload-smoke-1793637762487770000.json`: actual Models/DictationStream/Dictator, paced 250 ms chunks с последующей мутацией caller buffers, затем настоящая Luna; сравнение с прежним WAV batch, alternating порядок. По одной паре на вход, это Python-финализация после речи, не физический hotkey→AX и не p95.

| Вход | ASR batch, с | ASR upload, с | Весь batch, с | Весь upload, с |
| --- | ---: | ---: | ---: | ---: |
| captured long | 1.980 | 0.493 | 3.418 | 3.415 |
| short | 1.462 | 0.250 | 2.813 | 2.070 |
| mixed RU/EN | 2.058 | 0.445 | 3.598 | 2.072 |

В каждой ветке ровно один ASR и один LLM; никаких paid warmup/preview. До release уже отправлено аудио; серверный body SHA совпал с полным фактическим body, captured raw PCM SHA сохранён. Raw и cleaned тексты совпали между ветками на всех трёх входах, контрольное окончание есть. ASR быстрее во всех трёх парах; в long более медленный Luna-вызов поглотил выигрыш, поэтому общего стабильного сокращения на каждом входе не заявляем. Ошибка mixed `margin` вместо `merging` осталась прежней; WER не измерялся.

### Gate, runtime и учёт

Финальный `ruff check`, `ruff format --check`, `pytest --cov -q`: **443 passed**, **87.81% coverage** при пороге 80%; только прежние Swig deprecation warnings. Swift/typecheck не запускались — native не менялся. Реальный pydeps: **32 модуля / 91 внутренняя связь, DAG**. README/SPEC/ARCHITECTURE обновлены, в том числе прежние stale WebSocket-формулировки Q-3/языка. Зависимостей, настроек, протокола, моделей/промпта или storage-изменений нет; commit не делался. Временный integrated smoke script удалён; cache evaluators и все исходные JSON сохранены.

Перед reload status подтвердил idle ready/cloud daemon 47811, recording=null/jobs=[]. Graceful stop, native app сама запустила **51937**; свежий status — **ready/cloud**, 383 MB, recording=null/jobs=[]. Отдельный serve/app rebuild/permission grant не требовался. Старое `unknown setting model` осталось, пользовательский TOML не трогали. Нового физического ⌥→paste/AX smoke после cutover нет; backend API и runtime readiness проверены отдельно.

За этот шаг: **30 ASR-попыток** (29 полных responses, 1 interrupted outcome unknown), **42 LLM-попытки** (41 полный response, 1 failure outcome/usage unknown); **31621 наблюдённый OpenAI token**. Первые исследования 36 LLM попыток/27003 observed tokens, integrated smoke 6/4618. Стоимость в деньгах не наблюдалась. Локальные HTTP tests и pre-network instrumentation error не считаются provider calls; автоматических повторов не было.

## Завершённый предыдущий шаг

## Потеря хвоста до LLM — один шаг

### Итог HTTP cutover

Cloud накапливает собственные PCM-копии и отправляет один полный HTTP ASR только в finish. Удалены production WebSocket-модуль/фабрика/obsolete transport tests и зависимость websockets; `uv lock --offline` удалил только websockets. Старый код сохранён вне репозитория как `archived_cloud_stream_20261102.py`, исторический evaluator явно загружает этот архив; loader проверен без сети, shim приложения нет. README/SPEC/ARCHITECTURE и заголовок latency обновлены; старые WS-записи остаются в cloud-агрегатах.

Регрессии сначала воспроизвели старый контракт: 4 падения на `.stream`/неподдержанном timeout. После реализации новая cloud fixture ошибочно наследовала default RU; по отдельному разрешению пользователя исправлена только fixture на явный `language=AUTO`, assertions и product default RU не менялись. Целевые проверки: 14 passed. Полный gate: `ruff check`, `ruff format --check`, `pytest --cov -q` — 429 passed, coverage 87.63% при пороге 80%; только существующие Swig deprecation warnings. Swift typecheck не запускался: Swift не менялся. Фактический pydeps: 32 модуля / 91 внутренняя связь, DAG; production/tests/manifest/lock больше не ссылаются на cloud_stream/DeepgramStream/websockets.

Реальный smoke использовал `Models(cloud=True)`, `DictationStream` и `Dictator.dictate` с настоящими Deepgram HTTP и `gpt-6-luna`, `fast` (модель/tier подтверждены ответами). Три заранее разрешённых синтетических входа, отдельный пустой Vocabulary без path и дефолтный in-memory config; пользовательские history/vocabulary/config не читались и не менялись. Чанки 250 мс изменялись caller после add; в фактический WAV upload ушёл byte-identical полный исходник. До finish нет provider-запросов; ровно один ASR и один cleanup на вход; повторный finish не делает ASR. На captured PCM сохранён SHA-256, «отдельного подтверждения» есть в raw и после Luna. Это полнота конкретного проблемного примера, не гарантия общего WER.

| Вход | Аудио, с | ASR, с | Luna, с | Финализация Python, с |
| --- | ---: | ---: | ---: | ---: |
| captured long | 23.597 | 1.461 | 6.039 | 7.500 |
| short | 3.217 | 1.088 | 1.723 | 2.810 |
| mixed RU/EN | 11.598 | 2.196 | 1.434 | 3.630 |

Один проход на вход, не p50/p95; long — первый запрос нового клиента. Время здесь от вызова Dictator до готового текста, без native paste/AX, не новая физическая release→insertion latency. В mixed осталась ASR-ошибка `margin into main` вместо `merging into main`; Luna её не исправила. Новых эвристик/промпта/смены модели нет. На long основная оставшаяся задержка — Luna, а не ASR; TTFT и генерация отдельно не измерены, причину её разброса эти три пробы не доказывают. Наблюдено 3 ASR и 3 LLM-запроса, OpenAI usage суммарно 2309 tokens; денежная стоимость не наблюдалась. Артефакт: `~/Library/Caches/wav2sum/insertion-benchmark-2026-11-02/http-batch-smoke-1793634796746423000.json`; временный smoke script удалён, результаты/архив benchmark сохранены.

После gate и smoke проверен idle daemon 22880: ready/cloud, recording=null, jobs=[]. Выполнен graceful stop; уже открытое приложение само запустило новый daemon **47811**, status подтверждает **ready/cloud**, 384 MB, recording=null, jobs=[]. Отдельный serve/новая app/signing/permissions не запускались. Старое предупреждение TOML `unknown setting model` осталось, пользовательский config не трогали. Новый физический hotkey→AX прогон после cutover не выполнялся; runtime readiness и настоящий API-путь проверены раздельно.

### Историческая диагностика до cutover

Baseline: две реальные long-диктовки (20.40 с captured/sent/received) обрываются в raw на «Если тесты пройдут,». Синтетический исходник long.aiff (19.279625 с) полный, это подтверждено offline GigaAM. Первичная проверка — ровно два Deepgram ASR-запроса, HTTP и рабочий WS на одном PCM, без LLM/микрофона/личных примеров; сохранить transcript и timestamps/durations/финальные Results/Metadata без ключей. Evaluator — одноразовый `tail-compare.py` в существующем cache каталоге benchmark. WS посылается чанками 250 мс в реальном темпе, финализируется production кодом. Приёмка сравнительной пробы — наличие смыслового хвоста до «отдельного подтверждения», а не только непустой ответ или полный счётчик сэмплов.

Независимые offline-проверки: NativeTailBoundary — AVAudioConverter и native take/stream/finish/socket ordering; DaemonTailBoundary — Python session add/finish/preview/PCM suffix. Parent — реальный HTTP/WS, cloud_stream.py/cloud.py, интеграция. Не менять код по догадке. Доказанный дефект сначала воспроизвести регрессией/сценарием, затем минимальный фикс и повтор полноты/latency. Gate после интеграции: pytest/Ruff, Swift typecheck при Swift-изменениях, реальный путь. Числовой SLA не задан; completeness — обязательный gate. Сначала этот шаг, не оптимизация LLM.

Промежуточный результат: два реальных ASR-запроса на идентичном PCM из long.aiff (19.279625 с) оба сохранили окончание «отдельного подтверждения». HTTP whole request 1.436707 с; WS в реальном темпе, finalize 0.243531 с, sent=19.279625 с. Последний содержательный final Result покрывает start=2.85/duration=16.429625 и содержит хвост, затем пустой zero-duration final и терминальная Metadata duration=19.279625. Артефакт `tail-compare-1793623845180236000.json` содержит PCM SHA-256. Это успешный полный исходник, не доказательство полноты реального микрофона.

Offline NativeTailBoundary: и leading-, и trailing-silence сценарий через AVAudioConverter в 1600-frame чанках сохраняет spoken tail bit-for-bit; не flushed только 6 последних samples (0.375 мс) тишины. take() восстанавливает каждый emitted sample. Offline DaemonTailBoundary: все 308474 samples и 32000-sample suffix доходят через фактические DictationStream/DeepgramStream.add/_send, Finalize/CloseStream после данных; stale preview/отмена не подменяют final. Продуктовый код по этим результатам не менялся: причина ещё не доказана.

Следующая необходимая проба — сохранить исходящий Native PCM ровно одной новой синтетической long-диктовки до ASR. Подготовлен `capture-relay.py`: прозрачный local Unix relay, audio whitelist по собственному TextEdit title/session, без изменения протокола/обычного хранения. Self-test доказал byte equality, отсутствие записи чужого session, точный PCM suffix и восстановление/reconnect исходного socket inode. Desktop approval многократно возвращает unanswered, включая правильный активный workspace. Пользователь через интерактивный ask явно выбрал «Я подержу правый ⌥» и разрешил тестовый PCM/cache/временный restart/relay. Новый `--manual` вообще не запускает driver/CGE input: только ждёт первого PCM выбранного TextEdit, проигрывает AIFF и ждёт пользовательского отпускания; waveform сохраняется по final request. Первая manual-сессия завершилась по idle timeout 90 с без события hotkey (app.log это подтвердил), PCM/облачных запросов не было; исходный socket/app восстановлены, daemon pid22880 тот же. Следующее ожидание — 300 с, инструкция отдельным финальным сообщением; без ключевого события микрофон агент не включает. На текущем шаге по-прежнему 2 дополнительных ASR-запроса, 0 LLM-запросов.

**Реальный захват получен:** manual-02 завершился exit=0, session `D4F67C92-034B-421A-AFF6-A3933312D4D4`. WAV `captured-6ada85bb-6703-4276-9f44-1dc476debdb6.wav`: 377554 samples / 23.597125 с, PCM SHA-256 `72a72a59364e6cf8376f30f1a5bb462c920f7459abec8c1320f3a533f75b9382`. Offline GigaAM слышит последнюю фразу полностью, включая «подтверждения»; live raw Deepgram заканчивается на «отдельного». Native insertion ready=3225/send=3227/AX=3227..3311 мс, asr=0.35/llm=2.83/finalize=3.19 с. Исходный socket/app восстановлены, daemon pid22880 неизменен. Источник потери локализован: слово есть в фактическом исходящем PCM, но отсутствует уже в ответе streaming-ASR, не теряется в LLM/клиентской сборке. `capture-analysis.json` содержит доказательства.

На том же captured PCM HTTP вернул полный хвост (1.421635 с whole request), WS повторил отсутствие слова (0.304952 с finalize), terminal Metadata подтверждает полные 23.597124 с. Проверены два минимальных кандидата без правок продукта: один CloseStream вместо Finalize+CloseStream, затем smart_format=false при сохранённой пунктуации. Обратный повтор опроверг первый кандидат; у обоих кандидатов по одной полной и одной неполной расшифровке. У default WS на двух offline-replay тоже одна полная/одна неполная. Это маленькая выборка, не общая оценка WER; оба кандидата **не приняты**. Предварительная тестовая модель для гипотезы double flush удалена как неподтверждённая, tests/test_cloud_stream.py восстановлен к snapshot 2D94; продуктовый код не менялся. См. `protocol-probe-summary.json` и исходные response JSON.

Дальнейший выбор — явный tradeoff источника финального текста, а не regex/дописывание/скрытый retry: (1) только HTTP batch, без live-preview, один ASR-проход; (2) WS-preview + авторитетный HTTP batch final, два ASR-прохода; (3) сохранить быстрый WS с известным риском выпадения слов. На этой записи HTTP ASR ~1.42 с, WS finalization ~0.23–0.36 с; полная новая release→paste latency ещё не измерена. Выбор стратегии требует пользователя; не переключать самовольно. За текущую диагностику наблюдено 10 ASR-запросов и 1 Luna-вызов из live-пробы; стоимость/токены биллинга не наблюдались.

**Выбор пользователя: HTTP без живого превью.** Чистый cutover только cloud-диктовки: PCM чанки по прежнему socket протоколу накапливаются до final, один HTTP ASR после отпускания, без preview или повторного ASR. Снимок контекста/порядок/последний хвост/владение изменяемыми чанками сохраняются; успешный finish кэшируется, concurrent finish не удваивает платный запрос, отмена не ждёт HTTP и отбрасывает результат. Сетевой timeout dictation — 20 с через per-request override, timeout HTTP звонков не меняется; это не новый wall-clock deadline поверх urllib. После успешного batch audio_sent отражает полностью отправленное аудио; история/формат/сокет-протокол не меняются.

Тесты до кода: exact ordered PCM + tail при разных границах/мутации caller buffers, отсутствие запросов до finish, frozen language/terms, empty/cancelled/no replay after failed finish, concurrent finish single-flight и cancellation in-flight. HTTP timeout override отдельно от call timeout. Удалить obsolete DeepgramTranscriber.stream/cloud_stream и его тесты; websockets dependency убрать, если больше нет production refs, lock обновить offline. Старый WS код сохранить только в cache как архив benchmark, не как shim приложения. Обновить README/SPEC/ARCHITECTURE и нейтральный заголовок latency pipeline, не выдавать HTTP за WS. Затем полный pytest/cov/Ruff, реальный batch smoke на captured PCM с Luna, pydeps, idle reload daemon и подтверждение ready. Local, звонки, signing/Info.plist и persistent storage modules не менять.





## Отпускание ⌥ → подтверждённая вставка — один шаг

До изменений evaluator отсутствовал: `app.log` писал release→callback, CorrectionWatcher замечал содержащий текст через 500 мс и не отличал ранее существующий текст. Evaluator — обычная диктовка в доступное через AX поле, `app.log` и read-only история. После просьбы «можешь сам?» пользователь отдельно интерактивно разрешил шесть синтетических диктовок через динамики/микрофон и cloud; без такого согласия микрофон агент не включает. Предварительная проверка: исходный field snapshot + UTF-16 selection должны давать точное ожидаемое значение; замена на тот же текст не считается подтверждением. Наблюдение только после отправки paste, 25 мс до 2 с, monotonic clock, без текста в логе. Успех — AX того же поля совпал с ожидаемым новым значением; фиксируются release→ready, release→paste send и интервал release→AX match между последней отрицательной проверкой и первым совпадением. Нет AX/selection, несовпадение или timeout — явно неполный замер, не «вставка успешна».

Проверка: typecheck Swift, compile/install штатным builder без изменения bundle id/signing/Info.plist source; готовый cloud daemon и установленное menu bar app. Реальный smoke/замер: разрешённая синтетическая речь, короткий RU, mixed RU/EN и длинный RU по два раза, исходное пустое поле, стабильный фокус до вставки. Снять все шесть записей и интервалы, отдельно после пауз/последовательные и ready/ASR/LLM/paste. AX match — подтверждение значения поля, не screen paint/photon latency. Нет заданного SLA/tolerance, поэтому числового regression budget нет. Оптимизаций до этих измерений не делать.

Подготовка выполнена: readonly insertion probe в PasteTarget, отсчёт CACurrentMediaTime с event-tap release, точное expected value после UTF-16 replacement. Реальный код класса прошёл throwaway Swift smoke с контролируемыми AX-ответами: existing substring, Unicode replacement/insertion, interval bounds, unchanged/invalid selection/missing AX/timeout. Это отдельно от реального прогона ниже. Все четыре Swift typecheck-команды прошли (прежние macOS 27 deprecation warnings), native build установлен штатно; pytest 444 passed, Ruff check/format чистые. История/протокол/модели/промпт не менялись.

Первичная блокировка: `computer` ошибочно сообщает capture/input denied и не читает AXWindows TextEdit. Фактические native Swift проверки из shell и из computer.run: AX/PostEvent/ListenEvent/ScreenCapture разрешены, AXWindows успешно читаются. Tool issue отправлен; права macOS не менялись. После ошибки инструмента использован разрешённый native fallback через уже доверенный cmux, с интерактивным desktop approval, ограничением на собственный scratch `/private/tmp/wav2sum-latency-8d12d536-dc82-4513-bbdb-a59c82201502.txt`, остановкой при пользовательском вводе/смене фокуса и гарантированным release модификатора. Общий инструмент computer не объявляем исправленным.

### Фактический автопрогон, 2 ноября

Получены **6 реальных dictation→AX вставок**, без ручной речи: локальная Milena, afplay volume 0.2 (глобальная громкость не менялась), MacBook speakers/microphone, действующее приложение/Deepgram/Luna, TextEdit. Две пробы после пауз отделены от четырёх последовательных. Первый вспомогательный detector ошибочно остановился на синтетическом ⌘V Wav2Sum, но native insertion probe уже записал результат; он восстановлен из app.log и проверен по совпадению исходного поля и истории. Детектор теперь отличает источник Wav2Sum от физического ввода; первый платный запрос не повторялся, доделаны только пять остальных.

| Текст / повтор | ASR, с | LLM, с | Release→AX interval, с |
|---|---:|---:|---:|
| short / 1, после паузы | 0.40 | 2.74 | 3.178–3.248 |
| mixed / 1, после паузы | 0.34 | 2.76 | 3.164–3.192 |
| long / 1 | 0.27 | 1.21 | 1.522–1.550 |
| short / 2 | 0.29 | 1.23 | 1.536–1.564 |
| mixed / 2 | 0.28 | 1.20 | 1.529–1.555 |
| long / 2 | 0.22 | 1.30 | 1.550–1.577 |

LLM — основной post-release участок, а не paste: у последних четырёх send→AX не более 28 мс (у первой до 70 мс); это верхняя граница подтверждения, не точное время paint. TCP/TLS/prefill/queue/generation внутри LLM этим прогоном не разделены, причина замедления после паузы не доказана. n=2/текст, не SLA.

**Важная оговорка полноты:** обе long-пробы получили raw и text только до «Если тесты пройдут,», без последнего условия/действия. В исходном `long.aiff` хвост есть: read-only GigaAM offline на том же файле распознал весь текст до «отдельного подтверждения» (19.279625 с). LLM хвост не удаляла: его нет уже в raw. Не локализовано между реальным захватом, транспортом и Deepgram; audio_sent/received полные (по 20.40 с), но это не доказательство сохранности фонетического хвоста. Поэтому long latency не является успешной приёмкой качества; следующая диагностика — потеря хвоста до LLM, не дальнейшее сокращение промпта.

Артефакты: `~/Library/Caches/wav2sum/insertion-benchmark-2026-11-02/`: исходные AIFF, `measure.swift`/binary, `live-C98FFA28-C3E0-40B3-8EC5-C78568F6D60E.jsonl` (6 samples + complete), `final.png`. Все шесть history_equals_field=true и native session подтверждены app.log; финальная фактическая вставка видна на скриншоте. Внешний computer.run дал timeout: его exit не считается успешным; завершение отдельного evaluator доказано persisted complete и отсутствием measure/afplay процессов. Desktop control released, cloud daemon 22880 ready, recording=null/jobs=[]. Шесть синтетических диктовок остались в обычной истории; ничего не удалялось. Дополнительных paid запросов для проверки хвоста не было, offline-процесс завершён.


## Задержка перед вставкой: парный multi-prompt замер — один шаг

Исходные данные до фикса: 6 реальных streaming-проб Luna Fast с production RU prompt/static examples, без личных примеров/словаря. Медианы short: total 1,891 с, first text 1,837 с, соединение 0,597 с; long: total 1,899 с, first text 1,577 с, соединение 0,517 с. Fast подтверждён API, reasoning/cache tokens 0. Рабочий urllib создаёт новое соединение каждый запрос; это выбранный измеренный участок. Streaming-профиль не считается равным non-streaming задержке вставки.

Evaluator сохранён как запрошенный воспроизводимый артефакт: `uv run --offline --no-sync python ~/Library/Caches/wav2sum/latency-benchmark-2026-11-02/measure.py`; `--self-test` использует только localhost, обычный запуск делает 54 платных запроса. Основной замер: 3 prompt-варианта (текущий с примерами / тот же без примеров / компактный) × 3 синтетических входа (short RU / mixed technical / long RU) × 3 повтора × fresh/reuse. Порядок сценариев/пар counterbalanced. Оба транспорта проходят production `OpenAILLM.chat` с одинаковым payload; сравнивается полное non-streaming время, headers TTFB не выдаётся за первый токен. Никаких личных текстов, новых paid warmups или скрытых retries.

Кандидат: ленивое single keepalive HTTP/1.1 соединение на OpenAILLM, lock/close/lifecycle, stdlib без новых зависимостей. SSL-проверка и запрет redirects/утечек прежние. Proxy settings не обходить: для proxy допустим прежний urllib без pooling. Промпты в production не сокращать; варианты только эксперимент. Перед отправкой можно отбросить закрытое peer-соединение, но POST не переигрывать. Если на парном замере нет выигрыша для текущего prompt, безопасно убрать только собственный кандидат, не наслаивать другие оптимизации.

Приёмка: одинаковые сообщения/параметры и фактический выигрыш текущего prompt на парных запросах; p95 с малой выборкой — ориентир, не SLA. Пользователь числовой tolerance не задавал, поэтому числовой regression budget не объявляется. Offline regression gate — TCP reuse и lifecycle/failures без повторных POST, не сетевые тайминги CI. После — pytest/Ruff, production smoke/full daemon path на synthetic localhost без лишних paid calls; docs/pydeps; idle daemon cutover. Владельцы: PersistentOpenAI — транспорт/owners/tests, PairedPromptBench — evaluator, parent — запуск/анализ/приёмка. Артефакт JSONL с измерениями сохранить в доступном локальном отчёте, секреты не сохранять.

### Наблюдаемые результаты и приёмка

54/54 API-вызова успешны; Fast/model Luna подтверждены во всех ответах, ошибок 0. Все 27 пар имеют одинаковый payload hash и дословно одинаковый итоговый текст. Соединений fresh/reuse: 27/1 (первый cold reuse включён). Usage: 27 648 input / 4 908 output / 0 cached / 0 reasoning tokens; стоимость не объявляется фактически списанной без billing-данных.

Медианы complete cleanup, секунды; по 3 повтора на transport/строку:

| Промпт | Вход | Fresh urllib | Keepalive |
|---|---|---:|---:|
| Текущий + примеры | Short RU | 1.691 | 0.927 |
| Текущий + примеры | Mixed technical | 1.721 | 1.241 |
| Текущий + примеры | Long RU | 2.185 | 1.766 |
| Тот же без примеров | Short RU | 2.000 | 0.895 |
| Тот же без примеров | Mixed technical | 1.741 | 1.140 |
| Тот же без примеров | Long RU | 2.386 | 1.544 |
| Компактный | Short RU | 1.534 | 0.823 |
| Компактный | Mixed technical | 1.657 | 0.974 |
| Компактный | Long RU | 2.189 | 1.507 |

Текущий prompt выиграл 9/9 пар; медиана парной экономии 0.548 с. No-examples выиграл 9/9, compact 8/9; маленькая выборка и p95=max не являются SLA. Compact в техническом примере оставил время словами, поэтому короткие варианты в production не приняты: сохранены исходный prompt/examples и model settings. Нет новой серверной cache-экономии, cached tokens 0.

Принят только stdlib keepalive без зависимостей. Один lazy/locked клиент на LLM, strict redirects/TLS/error sanitization, configured proxy остаётся fresh urllib, повторов POST нет. Daemon/CLI/cloud-factcheck закрывают свои клиенты; локальный CLI lifetime не меняется. Lifecycle cancellation-регрессия сначала воспроизведена (ValueError вместо CancelledError после отмены), исправлена с ожиданием фонового завершения и сохранением CancelledError.

Данные и evaluator: `~/Library/Caches/wav2sum/latency-benchmark-2026-11-02/` (`live-1793616232569743000.jsonl`, self-test JSONL, `measure.py`); секреты/личная история не записаны. Реальный локальный Unix daemon → Deepgram WebSocket → Luna HTTP → ready text прошёл две полные synthetic диктовки на одном LLM TCP-соединении. История/словарь для smoke изолированы во временном каталоге. Граф после изменений: 33 модуля / 93 связи, DAG. Фактическое пользовательское ⌥→вставка после фикса пока не измерено, это выигрыш фазы чистки; первый запрос/соединение после простоя остаётся cold.
Финальный gate: **444 passed / 87,88 %**, Ruff check/format чистые (74 файла). Тесты проверяют reuse/close/изоляцию/origin change, concurrent serialization, proxy path, stale peer и отсутствие POST replay, recovery после malformed/refused/truncated/HTTP-error. Отдельная cancellation-регрессия passing после failing-before. Документы обновлены, временный daemon-smoke удалён, запрошенный evaluator/сырые замеры сохранены в cache как deliverable. При idle остановлен daemon 19667, menu bar app автоматически подняла новый **22880, cloud/ready**. Swift app/подпись/разрешения, конфиг, модели, промпт, словарь и личная история не менялись. Лишних изменений вне задачи нет.



## Deepgram auto: подсказки RU/EN — один шаг

История подтверждает: `Prebien, muy bien, test.` уже в raw Deepgram, Luna только убрала точку. Auto сейчас уходит как `language=multi` без языковых подсказок. Официальный Nova-3 Language Prompting поддерживает повторяемый `language_hint` в HTTP и WebSocket; это bias, не ограничение и не гарантия точности.

Добавить `language_hint=ru&language_hint=en` только при multi в обоих Deepgram transport, явные ru/en оставить без hint. Сначала дополнить существующие HTTP/WS регрессии для auto/explicit, затем реализация. Проверка: тест падает до добавления параметров; pytest/Ruff; настоящая локальная HTTP/WS проверка запросов без платных API-вызовов; idle daemon restart. Для оценки распознавания потребуется следующая новая диктовка: старое аудио не сохранено.

Выполнено: `_listen` при multi добавляет повторяемые RU/EN hints; streaming connection URL делает то же. Явные ru/en идут без hint, смешанная речь остаётся multi. Эффективный пользовательский `language=auto` подтверждён; настройки и чистка не менялись.

Проверено: до реализации HTTP auto/None регрессии упали на отсутствующих hints, explicit en прошёл. После — **432 passed / 87,84 %**, Ruff check/format чистые. Реальный localhost HTTP+WebSocket smoke production Deepgram-клиента подтвердил auto hints и отсутствие hints при explicit ru; скрипт удалён. README/SPEC/ARCHITECTURE обновлены, graph/API signatures не менялись. Реальный Deepgram на пользовательском голосе не вызывался: это проверка параметров транспорта, не доказательство исправленной точности. Старое аудио недоступно; нужна новая диктовка. Лишних изменений вне шага нет.

Применено: при отсутствии активной записи/jobs остановлен idle daemon 17433; menu bar app автоматически подняла новый **19667, cloud/ready** с текущими HTTP/WS hints. Swift app не пересобиралась, ключи и пользовательские записи не менялись.

## Luna Fast, простая чистка и HUD — один согласованный шаг

1. Cloud-диктовка/команды/перевод: `gpt-6-luna`, `service_tier=fast`, `reasoning_effort=none`; cloud-summary остаётся `gpt-6.1-terra` без явного Fast. Проверка: production HTTP-клиент и интеграция Dictator/summary на локальном синтетическом провайдере, pytest.
2. LLM получает сырой текст без regex-подготовки; убрать semantic validators/числа/отрицания/эвристики роста, удалить ставший неиспользуемым cleanup.py и его тесты. Сохранить реальные ошибки/пустой ответ, явные snippets/словарь, стиль/курсор, отключение cleanup. Проверка: принятая моделью самокоррекция не отклоняется, нет regex-подмены или потери содержимого из-за постпроверки.
3. HUD: компактнее, стартует справа снизу, перетаскивается без активации окна, положение не сбрасывается при preview/state; границы экранов учитываются. Проверка: Swift typecheck, настоящий HUD со синтетическим текстом, screenshot/AX и перемещение.

Без новых зависимостей/настроек/fallback; не отправлять пользовательские записи повторно в API. После интеграции — один общий pytest/Ruff/Swift gate, фактическая карта pydeps и docs. Установленную app обновлять только штатной сборкой с прежним bundle id/подписью; разрешения автоматически не выдавать.

Реализовано: Luna/Fast только в cloud Dictator, Terra остаётся default summary; обе модели с none. Удалены `cleanup.py`, его obsolete-тесты, semantic/growth guards и short-text обход; успешная LLM-правка принимается, raw используется только при ошибке/пустом ответе. Явные snippets/словарь/стиль/курсор и отдельные ограничения перевода сохранены. Удалён также конструктор-only routing test: реальная маршрутизация проверяется HTTP smoke, а не копированием аргументов mock-а.

Проверка Python: **432 passed**, **87,83 %**, Ruff check/format чистые (74 файла). Production Dictator и CLI summary factory прошли localhost HTTP smoke: raw отправлен без regex-подготовки, Luna/Fast/none приняла исправленное время с сохранённым запретом, Terra/none — без явного Fast. Внешние API/реальные пользовательские записи для проверки не отправлялись. Pydeps: **33 модуля / 93 связи**, DAG; README/SPEC/ARCHITECTURE обновлены.

Проверка Swift/HUD: все четыре typecheck прошли, после финальной HUD-правки повторно проверен app typecheck и скомпилирован настоящий HUD. В исходной implicit-анимации hide→show hit-testing терял окно; `animationBehavior=none` устранил это, собственный управляемый success fade сохранён. Реальный HUD 320×80 стартует справа снизу, native drag меняет frame, preview/thinking/new сохраняют позицию, выход за экран ограничивается; после быстрых переходов окно видно, hit-testing own-window и отказ для nil/чужой точки проходят. Фокус остаётся в cmux (подтверждено computer.windows/focusedWindow). Мышиный CG-point переводится в AppKit и проверяется по frontmost window, не по ненадёжной early-tap аннотации. Физический multi-monitor drag и реальная диктовка с перетаскиванием не проверялись.

Ограничение визуальной проверки: screenshot отклонён системой — у ответственного cmux нет Screen Recording, AXWindows также недоступен. Разрешения не выдавались и не обходились; цвет/текстовый рендер не объявлены визуально проверенными. Геометрия/видимость/фокус/native hit-testing проверены на настоящем окне. Демон перезапущен при idle: **17433, cloud/ready**, effective dictation Luna и summary Terra. Финальная app обновлена штатным builder с прежним named certificate/bundle id; `codesign --verify --strict` прошёл. По свежему app.log после финального обновления Accessibility/Input Monitoring/microphone сохранены, right_option tap активен. Тестовые процессы завершены, временные скрипты и бинарник удалены. Лишних изменений вне задачи нет.

## Cloud GPT-6.1 Terra — один шаг

Заменить `gpt-4.1-mini` на официальный API ID `gpt-6.1-terra` в cloud-дефолтах диктовки и summary; локальные модели и Deepgram неизменны. Chat Completions поддерживается. Для Terra явно `reasoning_effort=none`: сохраняется прежний нерезонирующий режим и совместимость temperature; другие явно выбранные модели не получают Terra-параметр. Никаких новых зависимостей, fallback или изменений промпта.

Проверка до реализации: контракт запроса требует явный none для Terra. После — pytest/Ruff, настоящий локальный HTTP smoke production-клиента с синтетическими данными, проверка выбранной runtime-модели без раскрытия ключей. Настоящие пользовательские тексты повторно в API не отправлять. Документация отражает новый default; pinned-default assertions удалить, не перепривязывать.

Выполнено: cloud default и summary default — `gpt-6.1-terra`, Terra получает `reasoning_effort=none`; другие явно выбранные модели — без этого параметра. Официальные model/migration docs подтверждают Chat Completions и temperature при none. Промпты и local не менялись.

Проверено: контрактный тест сначала упал на отсутствующем `reasoning_effort`, после реализации **501 passed / 88,30 %**, Ruff check/format чистые (76 файлов). Настоящий локальный HTTP smoke прошёл `Dictator.clean` → запрос Terra → проверку ответа; отдельно проверен explicit custom model без Terra-параметра. Временный скрипт удалён. Эффективные модели диктовки и summary проверены без чтения ключей — обе Terra. Демон 14224 остановлен при отсутствии записи/jobs; menu bar app автоматически подняла новый **15573, cloud/ready** с текущим кодом. Попытка параллельного запуска корректно отклонена lock-ом, второго демона нет.

Не проверялось: доступ конкретного OpenAI-аккаунта к Terra, качество/задержка настоящего API и factcheck реальных звонков — внешние запросы и повторная отправка пользовательских текстов не выполнялись. Установленная Swift app и её подпись не менялись. Документация обновлена; лишних изменений вне задачи нет.

## Постоянные cloud-ключи — один шаг

Допущения: без новых зависимостей; штатный Security.framework через ctypes, секреты не передаются в argv/subprocess, конфиг или логи. Явный env override сохраняется для запуска/CI; без него cloud читает Keychain (`service=wav2sum.cloud`, accounts `DEEPGRAM_API_KEY` / `OPENAI_API_KEY`). Local не обращается к Keychain. Закрытый Keychain/отказ доступа — понятная ошибка, не молчаливый fallback.

1. Сначала регрессии приоритета env, отсутствия/отказа ключа, скрытого ввода, отмены и пустого значения; изоляция всех тестов от настоящих ключей. Затем `credentials.py`, интеграция providers и `wav2sum cloud-keys`: существующий экспорт сохраняется без повторного ввода, отсутствующее значение вводится через getpass только в интерактивном терминале. Собрать оба значения до записи; обновлять item без удаления ACL. Проверка: pytest/Ruff, настоящий CLI на изолированном backend, native Keychain smoke только на собственных временных тестовых элементах; никаких API-запросов. README/SPEC/ARCHITECTURE обновить по фактическому коду.

Реализовано: отдельный `credentials.py`, CLI `cloud-keys` без чтения пользовательского TOML, приоритет env/Keychain в обоих cloud-провайдерах. Скрытый ввод fail-closed при GetPassWarning; оба значения собираются до записи. Тесты изолированы от настоящего Keychain, включая регрессию local-прогрева без доступа к нему.

Проверено: `pytest --cov` — **501 passed**, **88,27 %**, Ruff check/format — чисто (76 файлов). Настоящий CLI на синтетическом backend сохраняет значения, после удаления env lookup берёт сохранённые ключи, секретов в stdout нет; пользовательский конфиг не читается. Security.framework/CoreFoundation ABI и нужные константы загружаются, без чтения/записи Keychain. Pydeps: **34 модуля / 94 связи**, DAG. PTY-smoke не завершился из-за зависшего тестового дочернего процесса; это не проверка native-хранилища. Временные файлы удалены.

Native-проверка завершена с явного разрешения пользователя: обе фиктивные записи в `wav2sum.cloud.test.53f1a4e5fc1d460bbaeff369e4046dbf` созданы, прочитаны и обновлены; новый Python-процесс через production `credential` получил обновлённые значения без DEEPGRAM_API_KEY/OPENAI_API_KEY в env. Тестовые записи удалены, отсутствие подтверждено повторным get; временный скрипт удалён. Разрешения/ACL не расширялись, UI-подтверждения не обходились. Настоящие API-ключи не читались/не сохранялись: пользователю остаётся один раз выполнить `wav2sum cloud-keys` и перезапустить idle cloud daemon. Daemon/app не перезапускались, платных запросов не было. Шаг реализации и доступные проверки завершены; лишних строк вне задачи нет.

## Качество local/cloud — один согласованный шаг

Допущения: модели и явная граница режимов неизменны; промежуточный текст только в HUD; одна финальная вставка. Нет новых профилей/настроек, чтения содержимого окон или скрытого контекста. Словарь подсказок собирается из существующих явных терминов и выученных написаний, фиксируется на сессию. Новая зависимость `websockets` согласована отдельно.

Реализовать и принять как один шаг:

| Критерий | Изменение и пути | Проверка |
| --- | --- | --- |
| Q-1 | Ограниченный неизменяемый контекст терминов: `context.py`, Qwen/Deepgram/Nemotron, `calls.py`, `dictation.py`; контекст входит в ключ кэша звонка | `tests/test_context.py`, `test_asr.py`, `test_nemotron.py`, `test_calls.py::test_terms_invalidate_local_cache_but_names_do_not`; настоящий inference Qwen/Nemotron с GitHub/Nemotron |
| Q-2 | Минимальная чистка и отказ от неподтверждённых изменений: `cleanup.py`, `dictation.py` | `tests/test_cleanup.py`: RU/EN, spoken numbers/время/даты/дни недели/имена, самокоррекции, валюты, отрицания с contractions, ложные подсказки, пропуск короткого предложения, snippets; настоящая Gemma |
| Q-3 | WebSocket Deepgram и incremental local-хвост: `cloud.py`, `cloud_stream.py`, `dictation.py`, `vad.py`, `daemon.py`, Swift | `tests/test_cloud_stream.py`, `test_vad.py`, `test_dictation_sessions.py`; `test_daemon.py::test_cancel_during_final_cleanup_is_responsive_and_never_persists` и `::test_pipelined_sessionless_final_cannot_capture_replacement`; настоящий Unix/WebSocket и Swift-клиент |
| Q-4 | Local-окна звонка без смешения говорящих: `calls.py`, `nemotron.py` | `tests/test_calls.py::test_local_windows_preserve_short_turns_gaps_and_duration_boundaries`, `::test_windows_do_not_bridge_a_different_speaker_hidden_in_a_vad_gap`; настоящий Nemotron на окне 25,68 с, все три повторения сохранены, cache hit без нового worker |
| Q-5 | Cloud summary с источниками и ограниченным сведением: `summarizer.py`, `calls.py`, `factcheck.py` | `tests/test_summarizer.py`: вымышленные/скрытые/чужие ссылки, recursive notes, длинная монореплика и текст без пробелов; `test_factcheck.py`; настоящий локальный HTTP smoke с тремя репликами и источником решения U2 |
| Q-6 | Замеры без нового хранилища, фактическая документация | `tests/test_latency.py`, запуск read-only CLI на синтетическом JSONL; полные pytest/покрытие ≥80 %, Ruff, Swift typecheck, lock, pydeps; структурное ревью «руль» |

Контракты: ASR получает keyword-only `terms`; словарь подсказок не разрешает автоматическую замену созвучного слова. Cloud transport имеет неблокирующие `add`/`close`, snapshot `text` и блокирующий worker-метод `finish`. Отмена закрывает транспорт при замене сессии, disconnect и shutdown. Встреча сохраняет произнесённые самокоррекции: правила чистки диктовки на транскрипт не распространяются. Источники summary проверяются по существованию, не выдаются за доказательство смыслового следования.

Проверки внешних API — только на отдельно разрешённом аудио; до этого настоящий локальный сетевой сервер с синтетическими данными. Установленную app и работающий демон не перезапускать. Приёмка субъективного качества, p50/p95 на живом микрофоне и memory pressure под рабочей нагрузкой не подменяются тестами протокола.
### Наблюдаемое поведение

- Настоящие Qwen и Nemotron распознали синтезированную Samantha фразу с Nemotron/GitHub/суммой. Qwen дал preview и финал; 25,677 с непрерывной диктовки прошли границу 20 с без потери трёх повторений. Silero-копии дали одинаковый финал при interleave с batch VAD. Gemma оставила 16:30 вместо 15:30 и сохранила сумму и отрицание; отдельный реальный вызов сохранил `can't` / `won't`.
- Настоящий `calls.process` local обработал окно 25,68 с, сохранил три повторения, не создал summary; повторный запуск не создал worker. Worker закрыт/reap-нут в отдельной реальной проверке.
- Изолированный cloud daemon + настоящие Unix/WebSocket/HTTP: preview, финальный хвост, чистка и cloud-звонок с summary/главами. Для диктовки одна WebSocket-сессия, 12 000 PCM-сэмплов отправлены ровно один раз. Ответы провайдеров синтетические: это проверка транспорта/контрактов, не live Deepgram/OpenAI.
- Скомпилированный production `Daemon.swift` получил preview и финальный текст через тот же изолированный daemon; клиент другого режима отклонён. Приложение, микрофон и права macOS не запускались/не менялись.
- Новые `timings`: `preview`, `finalize` с учётом незавершённого live-шагa, cloud `audio_sent`/`audio_received`; CLI p50/p95 проверен только на синтетической истории, без чтения пользовательских данных. Величины сетевого fixture не являются замером задержки платного API.
- Русский Milena-клип: смешанное написание `Немotron` исправлено точной транслитерацией в заданный `Nemotron`, сохранены GitHub/main, финал содержит 16:30 и 12 долларов 50 центов. Но вместо исходного «не отправляй» конвейер вернул «не отправляя» и сохранил эту форму при чистке. Проверка буквального императива **не прошла**; это не объявлено успешной оценкой качества ASR. Отрицание не потеряно. Восстановление неизвестного намерения догадкой не добавлялось.

«Руль»: pydeps дал 33 модуля / 92 внутренние связи, DAG; ARCHITECTURE.md обновлена по этому выводу. По сравнению с прежней картой добавлены только `context` (подсказки/точное mixed-script написание), `cleanup` (границы редактора), `cloud_stream` (транспорт). Таблица транслитерации переиспользована из `vocabulary`, файл словаря не читается этой операцией. Протокол JSON не заменён: первый чанк передаёт app/title, отмена работает и во время финала. Внешний tracker, signing/bundle/Info.plist, пользовательское хранилище и работающий daemon не менялись. Новая зависимость — только согласованный `websockets` 15.0.1.

Пользовательская приёмка остаётся отдельной: live streaming с настоящим Deepgram/OpenAI, микрофон/вставка в установленной app, WER на голосе пользователя, реальные p50/p95 и memory pressure при рабочей нагрузке. Код не объявляет полное сохранение смысла доказанным и не выдаёт валидную ссылку summary за semantic entailment.

### Итог локальных проверок, 02.11.2026

Источник — текущее рабочее дерево; проверки ниже выполнены после последних изменений Python/Swift, exit 0. После прогона менялись только документы и удалялись собственные временные smoke-артефакты.

| Команда / сценарий | Наблюдаемый результат | Область |
| --- | --- | --- |
| `uv run --offline pytest --cov -q` | 484 passed, покрытие 90,10 % | полный набор, включая отмену финала, conflicting/adjacent finals, контекст, сохранение источников и guards |
| `uv run --offline ruff check`; `uv run --offline ruff format --check` | все проверки прошли, 74 файла formatted | Python |
| `uv lock --check --offline` | resolved 147 packages | согласованный lock с websockets 15.0.1 |
| `swiftc -typecheck wav2sum/native/app/*.swift`; `swiftc -typecheck wav2sum/native/capture.swift`; `swiftc -typecheck wav2sum/native/vprecord.swift`; `swiftc -typecheck wav2sum/native/app/Logo.swift wav2sum/native/icon/main.swift` | все четыре команды прошли; предупреждения устаревших audio API macOS 27 остались | native app/helpers |
| `uv run --offline pydeps wav2sum --show-deps --no-output --only wav2sum` | 33 модуля, 92 внутренние связи, без циклов | фактическая архитектура |
| `uv run --offline --no-sync python /private/tmp/wav2sum-quality-local-smoke.py`; `uv run --offline --no-sync python /private/tmp/wav2sum-quality-call-smoke.py`; `uv run --offline --no-sync python /private/tmp/wav2sum-quality-long-dictation.py`; `uv run --offline --no-sync python /private/tmp/wav2sum-quality-cleanup-smoke.py`; `uv run --offline --no-sync python /private/tmp/wav2sum-quality-cloud-smoke.py` | реальные локальные модели и описанные выше Unix/HTTP/WebSocket/Swift-сценарии прошли | только синтетические данные; cloud ответы fabricated; временные файлы после проверки удалены |
| `uv run --offline --no-sync python -m wav2sum.latency /private/tmp/wav2sum-quality-latency.jsonl` | p50/p95 и sent/received показаны корректно | синтетическая история, только чтение |

Буквальная русская TTS-проверка императива не прошла, как зафиксировано выше; её не включаем в список успешных smoke. CI, установленная app/живой микрофон, платные API, WER и memory pressure не проверены. Не заявляется готовность к релизу или отсутствие модельных ошибок. Устаревшие тесты текста промптов/копирования аргументов удалены вместо перепривязки к новой формулировке; оставлены проверки поведения. Изменений вне согласованного шага нет.


## Режимы local и --cloud — текущая реализация

Согласовано пользователем: `local` по умолчанию — Qwen3-ASR для диктовки, локальная Gemma для чистки, Nemotron 3.5 Transcribe 30B IQ4_NL для транскрипции звонков, без summary. `--cloud` — Deepgram для диктовки и звонков, OpenAI API для чистки диктовки, summary и глав. Внутренний рабочий API не подключаем. Nemotron запускается только при обработке звонка и выгружается при завершении или ошибке. Cloud включается только явным флагом; ошибки не переключают провайдера или режим.

1. Зафиксировать контракты и границу передачи данных (`config.py`, CLI/daemon/socket). Проверка: local не создаёт облачных запросов, несовпадение режима клиента и демона отклоняется до обработки; флаг не сохраняется как скрытая настройка.
2. Подключить тот же speech.cpp 0.9.4 и Nemotron IQ4_NL, которые использовались в замерах; сохранить VAD, разделение говорящих и таймкоды (`nemotron.py`, `models.py`, `calls.py`). Сначала тесты результата и жизненного цикла worker. Проверка: настоящий локальный клип; процесс worker отсутствует до обработки и завершён после успеха/ошибки; local не вызывает summary/главы.
3. Реализовать Deepgram Nova-3 и OpenAI API через стандартный HTTPS без новых пакетов (`cloud.py`, `dictation.py`, `calls.py`). Ключи только из `DEEPGRAM_API_KEY` и `OPENAI_API_KEY`, без логирования. Проверка: настоящий локальный HTTP-сервер с документированными ответами, RU/EN/multi, спикеры/каналы/таймкоды, ошибки HTTP, таймаут и неполный ответ; никаких пользовательских данных в облачных smoke.
4. Провести `--cloud` через CLI, daemon, TUI и Swift (`cli.py`, `client.py`, `daemon.py`, `tui.py`, `native/app/`). Не менять bundle ID, подпись или Info.plist. Проверка: предварительный и финальный текст, отмена, локальная и облачная очередь, явный режим при запуске и видимый режим в интерфейсе. Кэш распознавания различает backend/mode в существующих params, без миграции пользовательских результатов.
5. Полные pytest/Ruff/Swift typecheck, граф модулей и документация; структурное ревью «руль» по фактическому коду и пользовательская приёмка двух режимов. Реальные облачные API проверяются только на отдельно разрешённом тестовом аудио при наличии ключей; отсутствие ключей не заменяется фальшивым успехом.

Результат реализации: оба режима подключены к CLI, daemon, TUI и Swift. Local звонок проверен настоящим Nemotron: transcript/meta созданы, новый summary отсутствует, worker завершён; повторный запуск взял кэш без worker. Cloud-диктовка (preview/финал/чистка), cloud-звонок (спикеры/summary/главы) и отказ неверного режима проверены настоящими Unix/HTTP-соединениями с локальными ответами провайдеров и синтетическими данными, без внешней передачи. Нативный Swift-клиент проверен отдельным локальным socket-harness; настоящий TUI показал `ready · cloud` и корректно вышел по q.

Проверки: 344 теста прошли, покрытие 88,36 %; Ruff check/format, все Swift typecheck и проверка lock прошли. Прямые зависимости Parakeet/SpeechBrain и ставший неиспользуемым код удалены; GigaAM оставлена только для Core ML. Старый top-level `model` не переиспользуется как OpenAI ID: новая настройка `summary_model`, старый ключ игнорируется с предупреждением без изменения пользовательского конфига.

«Руль», структурная проверка: фактический pydeps-граф — 30 модулей, 84 связи, без циклов; новые границы `cloud.py` (HTTPS) и `nemotron.py` (scoped subprocess), карта обновлена в ARCHITECTURE.md. Форматы пользовательских результатов и семантика удаления не мигрировались; основной daemon и установленная подписанная app не перезапускались. Реальные платные API, живой микрофон и memory pressure на рабочем M3 Pro здесь не проверены; облачная пользовательская приёмка требует ключей и отдельно разрешённого тестового аудио. Временные smoke-файлы удалены.

## Ручная обработка записей

1. Сохранить автоматическую запись звонка; убрать автоматическую обработку после остановки и перезапуска. В TUI показать необработанные записи и одной командой запускать транскрипт и summary. Не перезаписывать существующие результаты, не обрабатывать активную запись и не создавать дубли в очереди; сохранить текущую семантику удаления и явной CLI-обработки. Обновить контракт демона и Swift-приложения и документацию.

   Проверка: запись сохраняется, очередь пуста до команды; TUI показывает её и ставит в очередь только по запросу; активная запись, занятый путь результата и повторный запуск отклоняются, существующие файлы не меняются. Дополнительно — тесты, линтер, Swift typecheck и изолированный запуск TUI без пользовательских записей.

   Результат: 229 тестов прошли, покрытие 86,36 %, Ruff и Swift typecheck прошли; изолированный TUI поставил запись в очередь по `p` и получил результат с подменёнными ASR/summary. Отдельно проверена ошибка остановки через сокет: состояние записи очищается, частичный WAV сохраняется. При одном предыдущем запуске существующий `test_coreml.py::test_padding_to_a_fixed_shape_does_not_change_the_frames_of_the_clip` не прошёл на случайном входе; следующий полный запуск прошёл. Работающий демон не перезапускался.

## Дальнейшая работа

1. **WAV-5 — выбор локального ASR завершён.** По уточнению пользователя повторное сравнение моделей не требуется. Результаты уже выполненного сравнения четырёх моделей на открытых наборах сохранены ниже.

   Дополнительный сравнительный прогон на локальных диктовках и новая запись 46 фраз не входят в текущую работу. Следующее согласованное действие — подключить Qwen3-ASR 1.7B только для диктовки, включая предварительный текст; GigaAM + Parakeet для звонков не менять.

   Шаг интеграции Qwen:
   - Использовать официальный `Qwen/Qwen3-ASR-1.7B-hf` через установленный Transformers, с фиксированной ревизией, FP16 на MPS и без нового runtime/зависимостей. Сначала регрессионные тесты извлечения ответа, языкового режима и ошибки обрезанного ответа, затем адаптер ASR. Проверка: целевые тесты и настоящий локальный inference.
   - Переключить диктовку, её прогрев и предварительный текст на отдельную ленивую модель Qwen. В `auto` распознавать без принудительного языка; язык готового текста определяет промпт чистки, а не выбор ASR. Убрать ставшие ненужными эвристики переключения GigaAM/Parakeet в диктовке. Проверка: обычная/потоковая диктовка, RU/EN/смешанная речь, стабильные спаны и нестабильный preview, отмена; звонки продолжают использовать прежние модели.
   - Проверить русский, английский и смешанный локальный клип настоящей моделью, путь предварительного и финального текста через сокет, тесты, Ruff, Swift typecheck; обновить документацию. Это проверка интеграции, не повторное сравнение качества моделей. Сборку/подпись установленного приложения и пользовательское хранилище не менять.

   Результат интеграции: диктовка, предварительный текст, CLI-диктовка и `evalset` используют отдельный ленивый Qwen; звонки сохраняют прежний маршрут. Новый Python-пакет или внешний runtime не добавлены; минимальная версия уже используемого Transformers поднята до 5.13, lock обновлён. Официальные веса закреплённой ревизии скачаны локально. Настоящий MPS/FP16 inference прошёл на русском, английском и смешанном клипах; в изолированном Unix-сокете прошли preview, финальный Qwen, настоящая LLM-чистка и отмена следующей сессии. В памяти конвейера диктовки были только `vad` и `dictation_asr`, без старых ASR/LID; пользовательская история не записывалась. Это smoke интеграции, не повторное сравнение моделей. Установленное приложение не пересобиралось, работающий демон не перезапускался; живой микрофон и CPU-инференс в этом шаге не проверялись.

   Итог проверок интеграции: 249 тестов прошли, покрытие 86,44 %; Ruff check/format, проверка lock и все Swift typecheck прошли. Остались предупреждения существующих аудио-API macOS 27. Граф модулей пересобран; зависимости между модулями не изменились, новые интерфейсы отражены в ARCHITECTURE.md.
2. **WAV-2 — предварительный текст диктовки.** Согласовано: сырой ASR-текст показывается в отдельной нередактируемой HUD-плашке во время речи; промежуточные версии не вставляются в приложение. После отпускания клавиши выполняются финальное распознавание и чистка, затем одна вставка. Смена фокуса запрещает автовставку, но готовый текст остаётся в буфере; Esc отменяет запись или ожидающую вставку.

   Шаг реализации:
   - Сначала регрессионные тесты предварительного текста до отпускания, совпадения финального результата и изоляции отменённых/новых диктовок; затем Python-обработчик и Swift-плашка вместе. Проверка: целевые тесты и изолированный обмен через настоящий Unix-сокет.
   - Идентификатор сессии связывает предварительный текст с текущей диктовкой; отмена и поздний ответ не должны вернуть плашку или вставить текст. Проверка: отмена во время ASR и финальной обработки, новая диктовка после отмены, два клиента.
   - Проверить нативную плашку, сохранение фокуса и отсутствие промежуточной вставки; обновить описание протокола и поведения. Проверка: изолированный Swift-запуск, свежая AX/визуальная проверка, полный pytest, Ruff и Swift typecheck. Задержку UI измерять отдельно от задержки настоящих моделей; подмена ASR не доказывает реальную скорость распознавания.

   Результат: реализованы предварительный сырой текст в HUD, идентификаторы сессий, отмена и защита финальной вставки при смене поля/текста/выделения. 240 тестов прошли, покрытие 86,57 %; Ruff и все Swift typecheck прошли (остались предупреждения устаревших аудио-API macOS 27). Изолированный настоящий Unix-сокет проверил предварительный текст, финал и отмену; нативная плашка визуально и через AX показала конец длинного текста и сохранила фокус другого приложения. Отдельный Swift-harness проверил логику контроллера и PasteTarget с подменёнными внешними эффектами: отмену записи/обработки, поздний ответ после новой сессии, смену поля/текста/выделения, ошибку и disconnect. На одном локальном клипе с настоящими VAD/GigaAM первый preview получен после 1 с аудио за 113 мс ASR; следующие шаги — 48 и 45 мс, финальный ASR — 26 мс. Это не замер живого микрофона и полной LLM-задержки; установленное приложение не пересобиралось и не перезапускалось. Короткая программная смена поля и возврат между опросами AX (0,2 с) могут остаться незамеченными; при недоступном AX-поле — только буфер обмена. Следующий шаг WAV-3 не начинался.
3. **WAV-3 — точность терминов.** Сравнить распознавание и чистку терминов выбранным ASR с текущей связкой; отдельно проверить осуществимость усиления словаря GigaAM, если он остаётся в маршруте.

   Проверка: один локальный `evalset`, WER до и после чистки, точность заранее отмеченных терминов и имён, ложные замены. Изменение хранения или удаления `vocabulary.json`, записей, транскриптов и истории — только после отдельного согласования.
4. **WAV-4 — README.** Сверить документацию с итоговым поведением приложения, выбранными моделями и ручной обработкой записей.

   Проверка: команды, настройки и пользовательские сценарии соответствуют коду; описанные действия проверены в приложении.

### Результаты WAV-5

На M5 Max 48 ГБ обработаны полные тестовые наборы [FLEURS](https://huggingface.co/datasets/google/fleurs): русский — 775 записей и 344 разных предложения, английский — 647 записей и 350 предложений. Дополнительно взяты 337 русско-английских записей [CS-FLEURS Tagged](https://huggingface.co/datasets/YapayNet/cs-fleurs-tagged); для парного сравнения исключён один файл, на котором Qwen вернула неполный результат `model_limit`.

| Модель | Русский WER | Английский WER | RU+EN WER, 336 файлов | Совпадение отмеченных английских слов |
| --- | ---: | ---: | ---: | ---: |
| GigaAM-v3 e2e RNNT | 5,34 % | 73,70 % | 35,93 % | 464/2222 — 20,9 % |
| Qwen3-ASR 1.7B Q8_0 | 6,06 % | 3,81 % | 33,67 % | 1221/2222 — 55,0 % |
| Nemotron 3.5 Transcribe 30B IQ4_NL | 7,61 % | 6,28 % | 26,99 % | 1294/2222 — 58,2 % |
| Nemotron 3.5 ASR Streaming 0.6B | 12,93 % | 10,94 % | 38,45 % | 626/2222 — 28,2 % |

Один файл — один запрос, одинаковое аудио 16 кГц mono, без VAD, LLM и подсказок словаря; WER суммарный по словам, Unicode NFKC и регистр нормализованы, знаки препинания удалены, числа и `ё`/`е` не объединены. Совпадение английских слов считается по выравниванию с эталоном; транслитерация не считается правильным написанием. Streaming работал с сохранением контекста и окном 560 мс: на FLEURS язык задавался, на RU+EN использовалось `auto`. При принудительном `ru-RU` на тех же смешанных файлах WER 42,60 %. Это не замер задержки от микрофона.

Ограничения: FLEURS — начитанные предложения; смешанные фразы искусственно составлены и начитаны одним русским диктором, а английские слова не ограничены ML-терминами. Набор CS-FLEURS Tagged доступен по CC BY-NC 4.0; условия коммерческого применения требуют отдельной проверки. GigaAM предназначена для русского. Результаты не доказывают качество на живых созвонах и пользовательской диктовке.

На M5 Max файл Nemotron IQ4_NL занимает 19,0 ГБ, пик RSS процесса в прогоне — около 18,5 ГБ, без гарантии учёта всей общей памяти и Metal. На M3 Pro 36 ГБ модель, вероятно, поместится отдельно от большой LLM, но прямого замера нет. Для длинного звонка и одновременной работы моделей нужен отдельный замер; RSS и память Metal нельзя складывать как независимые расходы.

Предыдущий 30-секундный пример pyannote дал WER 2,47 % для Nemotron и 18,52 % для Parakeet относительно STM с непроверенной точностью. На русской записи 30 минут согласие Nemotron и GigaAM составило 79,61 % без эталона; это не WER.

### Отдельные решения после основного порядка

- Прежнее сравнение локальных summary-моделей отменено: local summary отключено; нового сравнения ASR не требуется.
- Прежний `--nonda` и обсуждение внутреннего API заменены согласованным `--cloud` (Deepgram/OpenAI), описанным в текущей работе выше. Local остаётся полностью локальным для аудио/текста.

# PLAN: CHU-98 — сконвертировать энкодер GigaAM-v3 в Core ML

**Исторический план.** CHU-98 выполнена, итог замеров приведён ниже. Эпик ANE закрыт без внедрения второго бэкенда; новую задачу и план определяет текущее поручение пользователя. Не запускай эти шаги автоматически.

Сейчас `Transcriber` (`asr.py`) грузит GigaAM-v3 (`e2e_rnnt`) и зовёт `model.forward(wav, lengths)`: внутри препроцессор (лог-мел из сырого wav) и Conformer-энкодер, на MPS в fp16. RNNT-голова и декодирование идут на CPU (`cpu_head`). Цель эпика CHU-97 — чтобы ASR диктовки не делил GPU с Gemma (теперь mlx-lm в том же процессе) и меньше грелся на созвонах. Эта задача только конвертирует энкодер и проверяет точность; в `Transcriber` ничего не встраиваем (это CHU-100, и только если бенчмарк CHU-99 скажет «встраиваем»).

## Допущения

- Конвертируем только энкодер. Препроцессор (STFT + мел) остаётся в torch на CPU: STFT на ANE не идёт и дешёвый, а вход-фичи трейсится чище, чем сырой wav.
- Вход модели — мел-фичи `[1, n_mels, T]` и длина `[1]`. Длина нужна, чтобы энкодер маскировал паддинг в attention: без неё короткий клип, допаженный до 20 с, даст другие выходы.
- Формы: `ct.EnumeratedShapes` по T для 5 / 10 / 20 с, батч 1. Батчи и выбор размера — в CHU-100.
- `ct.convert(convert_to="mlprogram", compute_precision=FLOAT16, compute_units=CPU_AND_NE, minimum_deployment_target=macOS15)`.
- Проверка точности — на Mac через `coremltools` `predict` (на Linux он не работает), против torch fp32 на CPU, cosine similarity по кадрам до `enc_len`.
- torch в lock — 2.11, а coremltools 9.0 официально проверен на torch до 2.7. Скорее всего сработает с предупреждением; если нет — гоняем скрипт с другим torch через `uv run --with`, основной lock не трогаем.

## Шаги

1. **Обёртка для трейса.** `nn.Module` вокруг `model.encoder`, который принимает `(features, length)` и возвращает `(encoded, enc_len)` без python-ветвлений, которые ломают трейс. Тест в `tests/test_coreml.py` на маленьком фейковом энкодере (как в `test_asr.py`): выход обёртки совпадает с прямым вызовом энкодера; клип, допаженный до формы, даёт те же кадры до `enc_len`. Проверка: `uv run pytest --cov`, ruff.
2. **Скрипт конвертации.** `torch.jit.trace` обёртки → `ct.convert` → `.mlpackage`. Модуль покрываем тестом только в части без `coremltools` (выбор формы и паддинг до ближайшей из 5/10/20 с). Проверка: тест + на Mac `uv run … --out <путь>` два раза подряд даёт одинаковый `.mlpackage` (сравнить `weight.bin` и `model.mlmodel` по хешу).
3. **Сравнение с torch fp32 на Mac.** В том же скрипте флаг проверки: несколько клипов (из `~/wav2sum/recordings/` или HF-кэша, только чтение), cosine sim по кадрам. Готово, если > 0.99 на всех клипах и всех трёх формах.
4. **Отчёт по ANE.** Xcode → Performance report на `.mlpackage` (руками, Андрей) или `MLComputePlan` из Swift/Python, если хватит coremltools 9. Записать долю операций на ANE и какие слои ушли на CPU (главный подозреваемый — позиционное кодирование в attention). Итог — комментарий в CHU-98.
5. **Документы.** README: как собрать `.mlpackage`. SPEC.md: строка в «Куда двигают архитектуру». AGENTS.md: команда конвертации.

Проверка каждого шага: `uv run pytest --cov` (≥ 80 %), `uv run ruff check && uv run ruff format --check`.

## Итог на Mac (M5 Max, macOS 27, torch 2.11, coremltools 9.0)

- `EnumeratedShapes` уводит всю модель на CPU (0 из 1001 операций на ANE). Поэтому три фиксированные формы 5/10/20 с — функции `s5`/`s10`/`s20` одного multifunction-пакета, веса общие (422 МБ).
- SDPA с bool-маской на ANE пропускает паддинг (косинус 0.88 на 3 с в форме 10 с). GigaAM-путь без SDPA (matmul + masked_fill + softmax) даёт 0.9956–0.9984 на всех клипах; на ANE 965 операций, на CPU 4 (cast длины, expand_dims, less).
- Ещё две правки под coremltools: `pe` строим до трейса, `half` считаем в Python (в fp16 `5000 + frames` округляется), op `int` переопределён (NumPy 2 не делает `int()` из массива формы (1,)).
- Воспроизводимость: `weight.bin` побайтно равен, `model.mlmodel` отличается только порядком байт (декодированный protobuf одинаковый), `Manifest.json` — случайными UUID.
- Задержка (медиана, ANE / GPU): 5 с — 5.6 / 6.3 мс, 10 с — 13.6 / 7.2 мс, 20 с — 39.9 / 9.1 мс. Загрузка функции на ANE 4–8 с, на GPU ~1 с.
- torch падает (segfault) на CPU после predict на ANE в том же процессе. В скрипте сравнение с Core ML идёт после всей работы torch. Для CHU-100 это значит: энкодер на ANE в одном процессе с torch надо проверить отдельно.
