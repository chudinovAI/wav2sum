# Текущий план

## Поручение и границы

9 ноября 2026: убрать лишнее в документах, тестах и `wav2sum/`, проверить повторы/структуру, измерить производительность и сделать понятный native GUI. Небольшие чистки можно выполнять пакетом; всю очередь автоматически не запускать. GUI не ждёт большого рефакторинга или смены языка.

Без классов ради классов, новых зависимостей, изменения хранения записей/истории/словаря, bundle id, подписи и Info.plist. Протокол менять одновременно в Python/Swift. Рабочий app/daemon запускать только по прямому поручению: startup активирует retention. Реальные данные/API требуют отдельного согласования. Commit/push/deploy — только по явному поручению; для локального запуска и GitHub-публикации в шагах 16–17 оно получено.

Для изменения поведения — регрессия до кода и узкая проверка; Python — scoped Ruff, Swift app — typecheck и actual UI. Карта модулей/API в ARCHITECTURE строится по фактическому графу.

## Закрытые шаги

Результаты прежних шагов ниже не являются свежим запуском suite.

| Шаг | Изменение | Проверка |
|---|---|---|
| 1 | Общий DevWatch executable в session fixture; процессы тестов независимы | 3 passed; audit hook подтвердил одну общую компиляцию вместо двух; Ruff |
| 2 | Убраны builder-spy/UI-wording assertions; native HUD включён в pytest discovery | 6 passed, actual HUD без skip; сохранены lifecycle/geometry/mode guards; Ruff |
| 3 | Удалены неиспользуемые split_by_turns/helpers/тесты старой нарезки | conversation/calls в общем gate шагов 6–7 |
| 4 | Главное native окно start/stop/timer/mode/settings, общий state с меню, pending/ACK/disconnect guards | 6 native tests, после финализации 3 app/settings/window; Swift/Ruff; actual AX Start/Stop в owned synthetic app |
| 5 | Read-only Calls/Dictations, общий catalog и history RPC, process/open/refresh, phase/errors/permissions | 93 Python + 8 native tests, финальный rerun 5 native; Swift/Ruff; настоящий socket client → isolated handler/queue/catalog с synthetic ASR |
| 6 | Legacy GigaAM Transcriber/batching удалены, pinned CPU loader сохранён приватно в конвертере | ASR/CoreML/audio/VAD/models в общем gate |
| 7 | Один builder grounded cloud summary/optional chapters, публикация результата и summary-only остаётся раздельной | Общий gate шагов 3/6/7: 115 passed, scoped Ruff; без повторного ASR/overwrite |
| 8 | SPEC — продукт/инварианты; ARCHITECTURE — реализация; README — использование и отдельный developer flow | 8 real CLI parses, 6 module entrypoints/flags по исходникам, local links/anchors и fences; граф не менялся: 40 модулей/112 edges, DAG |

Для GUI проверены stale snapshot, event-during-read, reconnect/old callback, read failure, pruning, raw→processed/ambiguous selection, repeated process и clipboard без поздней вставки. Actual Open Result передал существующий transcript path в captured effect; OS-приложение файла не открывалось. После принудительного разрыва backend UI показал disconnected/disabled actions; graceful shutdown этим smoke не доказан. Source audio/JSONL побайтно сохранены, local summary не создавалась. Owned services/scratch удалены.

SPEC больше не хранит журналы старых Linear-задач и GigaAM-бенчмарки; действующие privacy/consent/storage/signing/cancellation contracts сохранены. В README удалено устаревшее обещание semantic cleanup guard. AGENTS и человеческий REVIEW не менялись; development-history не создаётся. Полный suite/CI, live ASR/API, permissions и рабочий bundle этими шагами не проверялись/не менялись.

## 9. Безопасный synthetic профиль — выполнено

- [x] Проверены streaming/VAD/PCM, calls cache/decode, prompt lookup и echo; два read-only scout-прохода, измерения последовательно у parent.
- [x] Повторы и wall/CPU/RSS отделены от детерминированных counters; реальные model kernels, daemon scheduling, сеть и UI insertion не выдаются за измеренные.

### Условия и воспроизведение

10 ноября 2026, Apple M5 Max / 48 GiB, macOS 27.0.1, Python 3.12.15; decode — установленный Homebrew FFmpeg 9.0.2, не bundled FFmpeg 8.1.3. Отдельные процессы существующей `.venv/bin/python -B`, offline HF flags, без app/daemon startup, weights, микрофона, сети и личных данных. Скрипты только в памяти; WAV/cache/results только в owned TemporaryDirectory, исходные WAV проверены хешем, каталог удалён.

Время — perf_counter/process_time; CPU дочернего FFmpeg отдельно через RUSAGE_CHILDREN. Первое исполнение отделено от warm: 3 warm для streaming/длинного echo, 5 для остальных; `_draft` — batches по 100 вызовов. Это не cold model load/очищенный OS cache; p95 из столь малой выборки не публикуется. Counters собирались отдельно от timing, генераторы echo/WAV вне замера. Streaming включает выделение секундных входов и fake probability model.

### Local streaming

Настоящие DictationStream и VoiceActivityStream; probability model возвращает 0.9 при положительном первом sample окна и 0 иначе, fake ASR возвращает `x` на span. Чанки float32/16 кГц по 1 с, `step(preview=…)` после каждого, затем пустой control tail и finish. Непрерывный вход — 0.9; fragmented — 1 с речи/2 с тишины. Проверены 15/60/180 с и оба preview режима. VAD score вызывается ровно ceil(samples/512), успешный finish повторяемо возвращает тот же результат.

| Непрерывный вход, с | Preview | Сумма ASR clips, с | ASR batches | Concat output, bytes | Warm wall, мс | Warm CPU, мс |
|---:|---|---:|---:|---:|---:|---:|
| 60 | Нет | 60.00 | 3 | 3840000 | 3.058 | 3.058 |
| 60 | Каждую секунду | 629.28 | 60 | 40320000 | 3.690 | 3.690 |
| 180 | Нет | 180.00 | 9 | 11520000 | 9.260 | 9.247 |
| 180 | Каждую секунду | 1887.84 | 180 | 120960000 | 11.109 | 11.102 |

Для 60 с preview: 569.28 с tentative clips + 60 с commit; это повторный вход модели, не фактически затраченное ASR-время. Под нагрузкой настоящий daemon может coalesce revisions и вызывать step реже. Это не 10× доказанного ускорения и не разрешение удалять preview/финальный контрольный проход.

Fragmented 180 с: 60 completed spans, ASR clips 77.85 с без preview / 145.26 с с preview; `_transcribe` просмотрел 5490 / 10980 span entries. При 60 с — 630 / 1260: повторный обход completed history растёт быстрее входа, но fake orchestration пока мал (9.796 / 10.324 мс на 180 с).

Peak logical tail continuous — 1280000 bytes, не весь процесс. При bulk чанке 180 с тот же logical peak удерживает root ndarray 11520000 bytes; после finish logical tail 64000, root всё ещё 11520000. Для секундных chunks после finish оба равны 64000. Измерено обходом уникальных ndarray.base roots; pending/concat/native capture сюда не входят. Swift сохраняет всё capture audio; bounded Python tail не означает bounded total memory.

### Prompt lookup и PCM — baseline до шага 10

Настоящий `_draft`, без MLX inference; plain lists в timing, отдельный list-subclass считает candidate slices. Приоритет текущего алгоритма: длина 3→2→1, внутри неё самое позднее совпадение; overlap и короткий draft допустимы.

| Токенов | Сценарий | Candidate slices | Warm wall, мс/вызов | Warm CPU, мс/вызов |
|---:|---|---:|---:|---:|
| 4096 | Уникальные, miss | 12282 | 0.4831 | 0.4829 |
| 32768 | Уникальные, miss | 98298 | 4.2529 | 4.2495 |
| 32768 | Совпадение только в начале | 32765 | 1.4989 | 1.4976 |
| 32768 | Недавний overlap, все нули | 1 | 0.0002 | 0.0002 |

Miss: `list(range(L))`; old match: `list(range(L-3))+[0,1,2]`; overlap: нули. Один поиск O(L), последовательные растущие истории могут суммарно быть квадратичными. Доля в реальном Gemma generation не измерена.

Для 1 с: float32 64000 bytes → PCM16 32000 → Base64 42668 ASCII bytes без JSON. Настоящие Python Base64 decode + pcm16_to_float за 180 с payload: warm wall/CPU 5.645/5.637 мс при секундных чанках, 5.707/5.699 мс при 250 мс. Это не Swift encode/socket latency; формула двух float32 outputs у текущего astype/divide — по коду, не полный allocation trace.

### Cache и реальный decode

Synthetic stereo PCM16 WAV/16 кГц, RNG seed 731, samples integers [-1000,1000]. Настоящие hash/load_audio/process/cache/_write, fake transcription, llm=None и отдельный пустой vocabulary (домашний словарь не читался). На каждый файл первый miss и шесть hit-вызовов дали ровно один fake ASR. Маршрут hit: hash → decode → cache_lookup, miss добавляет ASR. Content hash исходника неизменен.

| WAV, с | Hash warm, мс | Decode first, мс | Decode warm wall, мс | Decode parent CPU, мс | FFmpeg CPU, мс | Process cache-hit warm wall, мс |
|---:|---:|---:|---:|---:|---:|---:|
| 60 | 2.410 | 107.763 | 37.734 | 9.278 | 41.446 | 44.938 |
| 180 | 7.826 | 55.613 | 56.810 | 20.320 | 78.336 | 65.082 |

Суммировать CPU потоков/процессов с wall нельзя. Cache не устраняет decode, но тот даёт точный sample count для duration; перенос lookup сам по себе не решает контракт. Не подменять content hash mtime/size и не мигрировать cache/storage без согласования.

### Echo

Три исходные synthetic сцены tests/test_echo.py вызваны как функции, не pytest: seed 0 для echo-only/no-leak, seed 1 для double-talk с room impulse response 50 мс/decay 10 мс, задержка 60 мс. Реальный suppress_echo, float32/16 кГц.

| Сцена | Аудио, с | First wall, мс | Warm wall, мс | Warm CPU, мс | Результат |
|---|---:|---:|---:|---:|---|
| Echo-only | 10 | 19.661 | 17.966 | 17.943 | attenuation 83.74 dB, own-only участок побайтно равен |
| No leak | 10 | 0.173 | 0.132 | 0.132 | весь результат array_equal входу, cancel не вызван |
| Double-talk | 10 | 21.362 | 21.343 | 21.344 | improvement 42.00 dB, own level +0.00016 dB; own-only tail равен |
| Long mixed | 60 | 170.137 | 171.514 | 171.304 | finite output; не speech-quality score |
| Long mixed | 600 | 1723.667 | 1711.993 | 1709.609 | finite output; не speech-quality score |

Double-talk improvement = 10log10(mean(known_echo²)/mean((out-known_mine)²)) на 5.5–7.5 с, с floor 1e-12. Выполнены существующие synthetic quality bounds: >20 dB, own level <0.5 dB; echo-only >30 dB. Обёртки отдельно насчитали 500 cancel frames и 204/270 adapt frames для echo-only/double-talk.

Long mixed: seed 731, normal system σ=0.1, near-end на [T/3,T/2), echo delay 60 мс/coefficient 0.45, noise σ=1e-4. Отдельный cProfile на 600 с: `_cancel` cumulative 1.913 с, `_raw_fft` 0.942 с, `np.roll` 0.151 с/30000 вызовов; вложенные времена не складываются. RTF warm около 0.00285 — это уже native NumPy FFT, не доказательство пользы нового языка.

### Память и границы вывода

| Изолированный процесс | Lifetime peak RSS, MiB |
|---|---:|
| Streaming/counters | 462.91 |
| Lookup/PCM/cache/decode | 589.05 |
| Echo, включая построение 600 с inputs | 489.72 |

RSS — Darwin RUSAGE_SELF high-water за весь процесс, включая imports/генераторы/все cases, не delta hot function или полная unified/GPU memory. Max FFmpeg child — 15.55 MiB, не сумма одновременных peaks. Native capture/GPU/MLX/Silero kernels, очередь настоящего daemon, HTTP/provider и release→paste в этом профиле не измерялись. Реальные модели, личная история, конфиг и ключи не читались; startup retention не запускалась. Production-код/тесты не менялись, pytest/Ruff/Swift/CI повторно не запускались.

## 10. Эквивалентная оптимизация lookup — выполнено

- [x] `_draft` сравнивает candidate tokens по индексам вместо list slices; при size=0 возвращает пустой результат без поиска. Приоритет длины 3→2→1/latest match, overlap и границы сохранены. Модели, sampling, preview cadence, хранение и протокол не менялись.
- [x] 11 boundary/priority cases добавлены до кода и прошли на baseline. После изменения: 39 LLM tests passed, scoped Ruff check/format чистые. Отдельный throwaway сравнил старую/новую функции на 63112 exhaustive/seeded cases (int/string tokens, также negative sizes); постоянная копия реализации/reference test не добавлялась.
- [x] Paired benchmark в одном процессе на тех же входах: 9 чередующихся before/after пар, batches 100 (recent overlap — 100000); времена ниже — warm median wall, CPU близок. Baseline взят из функции до изменения, новая импортирована из рабочего кода. Первые исполнения измерены отдельно, p95 не публикуется.

| Токенов / passes | Сценарий | До, мс | После, мс |
|---|---|---:|---:|
| 4096 | Miss | 0.433605 | 0.142400 |
| 32768 | Miss | 3.503477 | 1.160525 |
| 32768 | Старое совпадение | 1.182680 | 0.389352 |
| 32768 | Недавний overlap | 0.000185 | 0.000183 |
| 4096 + 256 новых токенов | 256 последовательных miss-проходов | 113.656750 | 37.436625 |

На miss 32768 candidate slices 98298→0; в hit остаётся только необходимый output slice. Длинный miss/старое совпадение около 3× быстрее; recent overlap практически не изменился. Это стоимость поиска, не Gemma/end-to-end; независимый baseline шага 9 не используется как paired-сравнение. Production diff только llm._draft; новые tests, этот PLAN и короткое описание ARCHITECTURE необходимы для шага.

Rust/C++/Go runtime не обоснован текущим профилем: повторный ASR и ожидание API новый язык не устраняет, echo уже в основном native FFT. Preview/context boundaries нельзя менять без проверки полноты/качества/отмены; cache decode требует прежней точной duration/provenance.

## 11. Cloud-профиль: соединение не установилось

- [x] Пользователь отдельно разрешил один initial + три repeat сеанса, максимум 4 Deepgram/4 OpenAI, только synthetic audio/text и существующие Keychain credentials; после ошибки без retries. Environment/beta fallback, изменение ключей/TCC/clipboard, рабочий app/daemon и downloads не разрешались.
- [x] Direct Dictator/DictationStream с обычными cloud clients, in-memory vocabulary/context и 250-мс real-time PCM feed; say/FFmpeg создали только фиксированную фразу «Please review the project before the meeting tomorrow.». Профилирование и request caps — в памяти, без production telemetry/изменения providers.
- [x] Первый Deepgram attempt завершился ошибкой во время connect; дальше остановка. Запущено попыток Deepgram 1, OpenAI HTTP 0. OpenAI preconnect успел сделать DNS lookup, но completed TLS/connect не зафиксирован. Успешных ASR/cleanup сессий нет; p50/p95/end-to-end публиковать нельзя.

| Зафиксированный этап первой попытки | Wall, мс |
|---|---:|
| DNS Deepgram | 85.921 |
| TCP connect с DNS Deepgram | 20086.991 |
| HTTPS connect целиком | 20087.003 |

Времена вложенные, не складываются. TLS handshake и HTTP response не зафиксированы; ошибка runtime sanitizes network failures, более точный socket error не сохранён. Это не измерение provider inference/ASR/LLM и не доказательство, что Go/Rust решит соединение. После ошибки новых TCP/TLS/HTTP попыток не было. Дополнительная DNS/proxy metadata проверка: HTTPS proxy не настроен, Deepgram возвращает один IPv4, OpenAI — два IPv4; IPv6/fallback проблему этим снимком не подтверждено. Изменений сети/таймаутов/защит не делалось.

Keychain читался noninteractive/read-only только с явно разрешёнными service/accounts; ключи не выводились/не сохранялись. В finally закрыты stream/LLM transport, owned каталог synthetic audio удалён, процесс завершён. Для повторного paid profile нужно новое согласование после восстановления provider connectivity; оставшийся лимит этого прогона не является поручением повторять запрос.

Ограничения stages: daemon finalize включает waits, ASR/LLM — также очереди/transport/provider, native ready/send/AX observation — разные события. Headers/TTFB не отделяют inference провайдера от сети. Настоящий release→paste этим прогоном не измерен.

Read-only исследование выявило безопасные direct Dictator/Stream seams без load_config/history и с in-memory vocabulary; Vocabulary() без path домашний словарь не читает. Passive offline model_status: pinned Qwen не подготовлен, Gemma/Nemotron/diarization доступны. Никаких downloads или actual inference этой проверкой не запускалось.

Существующие native harnesses подменяют audio/target/paste; их время не является actual insertion latency. Реальный отдельный helper требует собственных Accessibility/Input Monitoring grants, synthetic field/clipboard и проверенного focus; разрешения рабочего app ему не наследуются. Не выдавать grants/не менять подпись/не подменять canPaste. Native tail и cloud profile — раздельные проверки, не замена полного end-to-end.

## 12. Общий gate после накопленных изменений — выполнено

- [x] Полный `pytest --cov --cov-report=term --durations=10` с offline HF flags: 770 passed за 80.59 с, coverage 84.47% при неизменном пороге 80%, без skips. В summary два прежних Swig DeprecationWarning; после завершения также warning swigvarlink.
- [x] Полные `ruff check` и `ruff format --check`: чисто, 101 файл уже отформатирован.
- [x] Swift typecheck: весь native app, capture, vprecord и Logo + icon прошли. Прежние macOS 27 deprecation warnings audioUnit/installTap сохранены, не подавлялись.
- [x] Регрессий gate не обнаружил; production/test code, coverage threshold и exclusions не менялись. Обновлены только PLAN/SPEC для результатов и отдельного native UX/UI-шага. Рабочий bundle не устанавливался/не перезапускался; GitHub CI, live providers/models и beta-приёмка этим локальным gate не проверены.

## 13. Native UX/UI-полировка — выполнено

Прямое пожелание пользователя: GUI должен быть красивым, нативным и приятным по UX/UI, не только функциональным. Остаёмся на существующем AppKit, без нового UI runtime/dependencies и классов ради вида. Запись/история/Settings используют прежние state/guards; визуальная полировка не меняет согласия, хранение, protocol или автоматизацию.

- [x] Isolated preview до/после: Start/Stop и timer в sidebar записи/диктовки; справа list/detail истории, Settings — вторичное действие.
- [x] Системные шрифты, semantic colors и SF Symbols; переиспользуемые двухстрочные history cells, selectable details/полные ошибки. Settings — сгруппированная scroll-форма с закреплённым Apply.
- [x] Actual AX/screenshot проверка empty/loading/failed/disconnected/recording, dictation error/clipboard-only feedback и passive permissions. После read error/disconnect stale process/open отключены; saved path остаётся видимым после Stop.
- [x] Light/dark, resize до main 1000×620 и Settings 680×580; Tab/Shift-Tab между списком и появившимися деталями, secure fields после переключения Local→Cloud, `⌘R` и `⌘,`. Найден и исправлен stale key-view loop: NSWindow теперь пересчитывает его автоматически; добавлена поведенческая регрессия.

Сценарий Start → Stop → выбрать запись → Process → Open Result прошёл через настоящий Swift socket client и Python Daemon/catalog/queue с isolated synthetic capture/ASR. Одна запись прошла recording→raw→processing→completed без дубля и с сохранением selection; Open Result подтвердил существующий transcript через captured open effect, не запуск OS-приложения. Local summary не создавался. Fixtures metadata/history после error/refresh injections восстановлены до исходных SHA-256.

Scoped gate после финальных правок: 5 native pytest passed, Ruff чисто, Swift typecheck всего app прошёл с прежними audioUnit/installTap deprecations. Полный gate 770 tests относится к шагу 12, здесь повторно не запускался. Live API, реальный capture/inference/paste, grants/downloads, установка рабочего bundle и beta-приёмка не выполнялись; SPEC acceptance остаётся отдельной приёмкой.

## 14. Очистка веток — выполнено

Пользователь подтвердил удаление всех веток кроме main, включая неслитую `codex/preserve-local-state` (только прежние правки инструкций) и шесть Dependabot PR #67–72 без мержа. Текущие незакоммиченные файлы на main не переключаем, не коммитим и не публикуем.

- [x] Удалена локальная patch-equivalent `claude/project-thread-shai4z` и metadata трёх отсутствующих worktrees; живые рабочие каталоги не удалялись. Локальная ветка осталась только main.
- [x] PR #67–72 закрыты без мержа; восемь GitHub-веток кроме main удалены, stale origin refs очищены. Проверка GitHub API и локальных refs: main/origin/main на прежнем `9fcfcdd`; origin/HEAD — symbolic alias. Текущие файлы не переключались и не публиковались.

Настройки Dependabot не менялись: автоматическое создание будущих веток этим поручением не отключалось.

## 15. Standalone-пакет для передачи beta-пользователю — собран

Обычный internal DMG, без private-beta keys, скачанных model weights и пользовательских данных. Используем существующий distribution builder и настроенный signer `wav2sum`; bundle id/signing/Info.plist в исходниках не меняем. Сборка в новом каталоге Downloads, без установки/перезапуска рабочего app и daemon.

- [x] Prerequisites проверены: Apple Silicon, достаточно диска, существующий named signer `wav2sum` / `DB2E0DE8F910F3BDE1E1E46C90C165731E1B9AD8`. Новые зависимости и изменения identity не потребовались.
- [x] Существующий builder создал standalone app и DMG; relocation/import/helper/FFmpeg/TorchCodec gate прошёл на synthetic WAV. Дополнительно тот же gate прошёл непосредственно из read-only DMG в новом mountpoint с isolated home; volume отсоединён, scratch удалён. Embedded beta credential resource отсутствует; включённый FFmpeg source совпадает с pinned SHA-256.
- [x] `hdiutil verify` и `codesign --verify --deep --strict` прошли. App/Python/FFmpeg — arm64, фактический minimum macOS 26.2; версия app `502450ad72c3`, прежний bundle id `ai.chudinov.wav2sum` и signer сохранены. Scoped packaging tests: 20 passed; application code не менялся.

Артефакты: `~/Downloads/Wav2Sum-Beta-20261110T114324Z/Wav2Sum-Internal.dmg` (около 392 MiB) и соседний `Wav2Sum.app`. SHA-256 DMG: `06cbc3dd2560fee88e0392ed3bbea7b45355b60e7b65951a7199465b3948a77e`.

Gatekeeper assessment: `spctl --assess` вернул rejected (exit 3). Это internal, не Developer ID/notarized release; в README описано штатное ручное Open Anyway без отключения Gatekeeper. Приёмка на другом Mac, TCC, реальная запись/диктовка/paste, live providers/качество и проверка лицензий перед передачей остаются отдельными проверками. Рабочий app/daemon не запускались, файлы никому не отправлялись.

## 16. Обновить и запустить локальное приложение — выполнено

Существующий dev bundle использует эту `.venv`; сохраняем runtime layout, Info.plist, identity и настройки. Сохранённый режим — Cloud с явным consent, auto-process выключен; режим не переключаем, модели/ключи не меняем. Пользователь разрешил запуск рабочего приложения; отдельные записи и платные API-пробы не входят в проверку.

- [x] Перед обновлением running app и daemon отсутствуют; активную запись/диктовку/обработку не прерываем.
- [x] Существующий installer атомарно обновил dev app; source digest `b4f3377e7a15` совпал с текущими исходниками. Named signer и designated requirement сохранены, Info.plist побайтно не изменился. Прежний bundle сохранён installer в собственном backup.
- [x] Установленный app запущен, главное окно открыто штатным Open wav2sum. Actual AX: Start Call Recording/Settings/Refresh доступны, dictation ready, Microphone/Input Monitoring/Accessibility allowed. Real daemon: Cloud ready, recording=false, queued/running=0, auto_process=false; аудио/API-пробы не отправлялись.

## 17. GitHub-публикация: [PR #73](https://github.com/chudinovAI/wav2sum/pull/73)

- По прямому поручению пользователя опубликован snapshot source/tests/docs приложения. Локальный `WATCHDOG.yml`, DMG, секреты и личные данные не включены.
- Проверка: CI должен пройти перед merge; после merge временная ветка удаляется, остаётся main. Актуальный результат CI и merge — в связанном PR, без ручного дублирования его статуса.

Общий checkout нельзя переключать на другую ветку по workspace policy. Публикация выполняется из отдельного собственного worktree; его snapshot включает текущие изменения приложения, без локального WATCHDOG.

Первый CI PR #73 на macOS 15: 767 passed, 2 skipped, coverage 84.45%; history geometry precondition обнаружил выход Process за bounds. History stack теперь учитывает системные alignmentRectInsets native controls по краям вместо привязки alignment rect к самому краю контейнера. Bounds assertions сохранены, добавлены диагностические rects; локальные 5 native tests и Swift typecheck прошли. Результат повторного macOS 15 gate отслеживается в PR.

## 18. Переключение микрофона без поломки диктовки — выполнено, установлено локально

13 ноября 2026: пользователь поручил исправить диктовку после смены микрофона. На рабочем app системный журнал подтвердил AVFoundation exception в installTap: hardware 48 кГц против client 16 кГц при переключении Bluetooth WH-1000XM5. После ошибки нажатия приходят, но захват не начинается; Ready не доказывает исправность аудиовхода.

- [x] Регрессии написаны до production-кода: смена устройства/частоты, сохранение PCM и streaming cursor, drain доставленного хвоста, ошибка перед release без отправки частичного результата, cancel/старые callbacks и следующая попытка после ошибки.
- [x] Audio Queue input отделён от hardware format. Отдельная lifecycle queue закрывает старый вход до открытия нового; обычный stop выполняет Flush/Stop/Dispose и delivery-барьеры, cancel отбрасывает поздние данные. Ошибки относятся только к текущей сессии.
- [x] Узкий gate: 7 native tests passed, Swift typecheck всего app, scoped Ruff чисто; после последней Flush-правки отдельно 1 controller regression passed и app typecheck без новых предупреждений. Actual isolated UI: реальный controller → окно показал capture error, затем следующая попытка перешла в Listening. Независимые review0 → fix1 → review1 → fix2 → review2: подтверждённых замечаний не осталось; README/ARCHITECTURE обновлены.

Без изменений хранения, протокола, режима Local/Cloud, ключей, разрешений, bundle id/signing/Info.plist и зависимостей. Рабочий bundle не устанавливать/перезапускать без отдельного согласования; live capture/API — не часть synthetic-проверки.

Выбран Audio Queue input с фиксированным client PCM float32/mono/16 кГц: преобразование hardware format выполняет Core Audio, вместо AVAudioEngine.installTap с форматом, который мог устареть между чтением и установкой. Выбор устройства по UID предшествует старту; Bluetooth сохраняет прежнее предпочтение встроенного микрофона. Смена default input перестраивает только захват, а не диктовочную сессию; накопленный PCM и streaming cursor сохраняются. Непредвиденная ошибка отменяет только текущую сессию и видна в GUI; следующая попытка разрешена.

Пользователь отдельно разрешил обновить `~/Applications/Wav2Sum.app` и завершить только native Wav2Sum через SIGTERM, затем открыть исправленный bundle. Демон PID 1269 не перезапускается; активные recording/jobs блокируют установку. Существующие signing requirement/bundle id/Info.plist и режим сохраняются, предыдущий bundle оставляет installer. Реальный Bluetooth/capture/partial hardware buffer tail, live ASR/API и качество распознавания synthetic-проверкой не доказаны; физическое переключение может дать разрыв звука. Необходимых изменений вне шага нет.

- [x] Обновлён dev bundle, source digest `4aef1e8476f2`; installer сохранил предыдущий bundle, signing requirement и Info.plist побайтно прежние. Старый native PID 1311 завершён отдельно, исправленный app открыт. Actual AX: Cloud Ready, диктовка Ready, прежние Microphone/Input Monitoring/Accessibility allowed. После запуска daemon по-прежнему PID 1269, ready, recording=null, jobs=[]; перезапуска/стартовой retention и live capture/API-проб не было. Owned synthetic UI/scratch удалены; commit/push не выполнялись.

## 19. GitHub-публикация исправления микрофона

По прямому поручению пользователя — commit/push и merge исправления шага 18. Публикуются только Dictation.swift, native_dictation_handoff.swift и связанные README/ARCHITECTURE/PLAN; локальный WATCHDOG.yml не включается. Общий checkout остаётся на main без переключения и очистки пользовательских файлов; публикация из отдельного owned worktree. Merge после успешного CI, временная ветка удаляется. Фактические CI/merge и номер PR — в GitHub, без ручного дублирования статуса.
