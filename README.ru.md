# Creality Filament System (CFS) Box for Klipper — Open Source Edition

[English](README.md) | Русский

> [!WARNING]
> ПО предоставляется **«как есть»**, без явных или подразумеваемых гарантий.
> Вы устанавливаете и используете его на свой страх и риск. В пределах,
> допускаемых применимым законодательством, авторы и участники проекта не несут
> ответственности за претензии, убытки или ущерб, связанные с установкой,
> ошибками ПО, его использованием или невозможностью использования.

Открытая замена штатного модуля Klipper `[box]` для Creality K1C с K1 Series CFS
Upgrade. Сохраняет штатную библиотеку `serial_485`, прошивку MCU CFS, экран,
Creality Print и пользовательские настройки.

Проект охватывает полный контракт `box`: инициализацию, загрузку, выгрузку, резку,
смену инструмента, продувку, жизненный цикл печати, восстановление, датчики, RFID
и данные катушек. Базовая прошивка — **1.1.3**; изученные отличия до 1.5.3 описаны
в [истории прошивок](docs/compatibility.ru.md#firmware-history).

На контрольном K1C с MCU **1.1.3** прошли предзагрузка четырёх слотов, полные
циклы загрузки и выгрузки, смены C→A→C, двухцветная и трёхцветная печать. Пауза
и продолжение проверены во время печати. Трёхцветное задание OrcaSlicer завершилось
после восстановления оператором. Автоматическое восстановление через
**«Повторить»** после облома проверено тестами на ПК.
[Результаты на контрольном принтере](docs/installation.ru.md#reference-printer-evidence).

По умолчанию доступны обычные команды слайсера и принтера через Klipper virtual SD.
Параметр `passive: true` в `[box]` включает телеметрию и
[отдельные диагностические операции](docs/installation.ru.md#material-operation-trial).

## Установка на K1C

Подключитесь по SSH с правами root к K1C с комплектом CFS Upgrade. Сначала
завершите печать, ручные перемещения и операции с прутком.

```sh
cd /usr/data
wget -O cfs-install.sh https://raw.githubusercontent.com/truestealth/cfs_klipper/main/install.sh
```

`wget` скачивает стартовый скрипт. Архив текущей ветки `main` скрипт скачивает
через Python 3.8 или новее с проверкой сертификата по системному хранилищу.
Далее `tar` распаковывает архив, а установщик создаёт копию штатного Box и
перезапускает Klipper. Состояние установки и резервные копии сохраняются в
`/usr/data/cfs-deployment`.

В некоторых сборках BusyBox `wget` не проверяет сертификат. Если он сообщает
об этом, скачайте скрипт на ПК с проверкой сертификата и скопируйте его на
принтер по SSH или сравните скачанный файл с доверенной копией.
[Исходник `install.sh`](install.sh)

После скачивания и сверки запустите `sh cfs-install.sh`.

Для обновления и отката запустите `sh cfs-install.sh update` или
`sh cfs-install.sh rollback`. Добавьте `--check`,
например `sh cfs-install.sh install --check`, чтобы проверить условия до
остановки Klipper. Если Box установлен вручную или изменён `START_PRINT`, см.
[инструкцию по установке и откату](docs/installation.ru.md).

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
