# Creality Filament System (CFS) Box for Klipper — Open Source Edition

[English](README.md) | Русский

Открытая замена штатного модуля Klipper `[box]` для Creality K1C с K1 Series CFS
Upgrade. Сохраняет штатную библиотеку `serial_485`, прошивку MCU CFS, экран,
Creality Print и пользовательские настройки.

Проект охватывает полный контракт `box`: инициализацию, загрузку, выгрузку, резку,
смену инструмента, продувку, жизненный цикл печати, восстановление, датчики, RFID
и данные катушек. Базовая прошивка — **1.1.3**; изученные отличия до 1.5.3 описаны
в [истории прошивок](docs/compatibility.ru.md#firmware-history).

Реализация хоста покрыта тестами на ПК. На контрольном K1C проверена предзагрузка
всех четырёх слотов и два последовательных полных цикла загрузки/выгрузки T1C
без ручной помощи. Печать и дополнительные версии прошивки ещё требуют
аппаратной проверки.

По умолчанию запуск **пассивный**: доступны идентификация и телеметрия, обычные
механические действия заблокированы. Явные
[тестовые команды](docs/installation.ru.md#material-operation-trial) разрешают
отдельные операции без изменения режима по умолчанию.

## Структура проекта

```text
src/cfs/        Реализация расширения
klipper/box.py  Точка входа Klipper
tests/          Проверки на ПК
docs/           Архитектура, протокол и совместимость
```

- [Установка и откат](docs/installation.ru.md)
- [Архитектура](docs/architecture.ru.md)
- [Протокол](docs/protocol.ru.md)
- [Совместимость](docs/compatibility.ru.md)
- [План реализации](docs/implementation-plan.ru.md)
- [Матрица контракта](docs/contract-matrix.ru.md)

<a id="быстрый-старт"></a>

## Запуск тестов

**Python 3.8** — версия проекта по умолчанию, указанная в
[.python-version](.python-version). Тесты используют стандартный `unittest`
и Jinja2 из [requirements-test.txt](requirements-test.txt). Сборка
и установка расширения как пакета не нужны.

Windows / PowerShell:

```powershell
py -3.8 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-test.txt
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Linux / macOS:

```sh
python3.8 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

[CI](.github/workflows/ci.yml) запускает эти проверки на Python 3.8, включая
установку и откат во временных каталогах. Тесты не управляют принтером.

Правила участия — в [CONTRIBUTING.ru.md](CONTRIBUTING.ru.md). В сообщении
об ошибке укажите коммит, версии хоста/MCU и шаги воспроизведения, удалив
частные данные.

## Поддержать проект

<a href="https://nowpayments.io/donation?api_key=608dff38-32c9-4d19-8f85-0c8a46408f3f" target="_blank" rel="noreferrer noopener">
   <img src="https://nowpayments.io/images/embeds/donation-button-white.svg" alt="Cryptocurrency & Bitcoin donation button by NOWPayments">
</a>
