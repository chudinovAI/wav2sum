# PLAN: CHU-98 — сконвертировать энкодер GigaAM-v3 в Core ML

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
5. **Документы.** README: как собрать `.mlpackage`. SPEC.md: строка в «Куда двигают архитектуру». CLAUDE.md: команда конвертации.

Проверка каждого шага: `uv run pytest --cov` (≥ 80 %), `uv run ruff check && uv run ruff format --check`.
