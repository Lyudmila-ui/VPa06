# Генератор чеков VPa06

Программа собирает демонстрационные чеки: берёт HTML-шаблон и строки из CSV, подставляет значения в поля `{{название_колонки}}` и сохраняет на каждую выбранную запись отдельный HTML и PDF.

Это учебный макет. Создание HTML и PDF не выполняет фискализацию и не формирует кассовый чек.

## Структура папок

```
VPa06/
  receipt_generator.py      # программа
  requirements.txt          # зависимость для PDF
  README.md
  settings.json             # появляется после запуска, в git не входит
  fonts/
    Roboto-Regular.ttf
    Roboto-Bold.ttf
    LICENSE-Roboto.txt
  templates/
    demo_receipt.html       # пример шаблона
  data/
    demo_receipts.csv       # пример данных с кириллицей и длинными строками
  output/                   # сюда по умолчанию попадают HTML и PDF
```

Папки можно хранить и в других местах. Программа просматривает только те каталоги, которые вы добавили, включая вложенные. Весь диск она не сканирует.

## Установка

Нужен Python 3.11 или новее. PDF делает библиотека [WeasyPrint](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html). Остальной код использует стандартную библиотеку Python.

Интерфейс — tkinter, он входит в обычную поставку Python для Windows и macOS. Если tkinter отсутствует, поставьте Python с официального установщика и не отключайте Tcl/Tk.

### Windows

1. Установите Python через [Python Install Manager](https://apps.microsoft.com/detail/9NQ7512CXL7T) из Microsoft Store либо с [python.org](https://www.python.org/downloads/windows/). При установке с python.org включите tcl/tk и добавление Python в PATH.
2. Установите [MSYS2](https://www.msys2.org/), оставив путь по умолчанию.
3. Откройте оболочку **MSYS2 UCRT64** и выполните:

```bash
pacman -S mingw-w64-ucrt-x86_64-pango
```

4. Закройте MSYS2. В обычной командной строке Windows, в папке проекта:

```bat
cd /d C:\Zerocoder\VPa06
python -m venv venv
venv\Scripts\activate.bat
python -m pip install -r requirements.txt
```

5. Проверка библиотек:

```bat
set WEASYPRINT_DLL_DIRECTORIES=C:\msys64\ucrt64\bin
python -m weasyprint --info
```

Если сообщение говорит, что библиотека не найдена (`cannot load library`), оставьте переменную `WEASYPRINT_DLL_DIRECTORIES` заданной на `C:\msys64\ucrt64\bin` и в том окне, где запускаете программу. В PowerShell:

```powershell
$env:WEASYPRINT_DLL_DIRECTORIES = "C:\msys64\ucrt64\bin"
```

Некоторые антивирусы ошибочно помечают компоненты WeasyPrint. Это известная ложная реакция; сверяйтесь с [документацией WeasyPrint](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html).

### macOS

Команда `brew install weasyprint` ставит отдельную программу со своим Python. Этому скрипту нужен модуль `weasyprint` в том же интерпретаторе, которым вы запускаете `receipt_generator.py`. Поэтому Pango ставится через Homebrew, а сам пакет — через pip.

1. Установите [Homebrew](https://brew.sh/), затем зависимости:

```bash
brew install pango libffi
```

2. В папке проекта:

```bash
cd /path/to/VPa06
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
python -m weasyprint --info
```

3. Если библиотеки не находятся, перед запуском укажите каталог Homebrew.

Apple Silicon:

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib:${DYLD_FALLBACK_LIBRARY_PATH}"
```

Intel:

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/usr/local/lib:${DYLD_FALLBACK_LIBRARY_PATH}"
```

Запасной путь через MacPorts, если Homebrew не используется:

```bash
sudo port install py-pip pango libffi
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
```

## Шрифт

В шаблоне кириллица берётся из локальных файлов, без загрузки из интернета:

- `fonts/Roboto-Regular.ttf`
- `fonts/Roboto-Bold.ttf`

Roboto распространяется по лицензии Apache 2.0, текст лицензии лежит в `fonts/LICENSE-Roboto.txt`.

В `templates/demo_receipt.html` шрифт подключён так:

```css
@font-face {
  font-family: "ReceiptSans";
  src: url("../fonts/Roboto-Regular.ttf") format("truetype");
  font-weight: 400;
}
```

Путь считается от папки шаблона. Перед созданием PDF программа проверяет файлы из `url(...)`. Если файла нет, появляется сообщение с ожидаемым путём, и PDF не создаётся.

Чтобы использовать Liberation Sans вместо Roboto:

1. Скачайте Liberation Sans (лицензия SIL Open Font License), например с [страницы проекта](https://github.com/liberationfonts/liberation-fonts/releases).
2. Положите `LiberationSans-Regular.ttf` и `LiberationSans-Bold.ttf` в папку `fonts`.
3. В шаблоне замените пути в обоих правилах `@font-face` на `../fonts/LiberationSans-Regular.ttf` и `../fonts/LiberationSans-Bold.ttf`.

## Запуск

Windows, после активации окружения:

```bat
cd /d C:\Zerocoder\VPa06
venv\Scripts\activate.bat
python receipt_generator.py
```

macOS:

```bash
cd /path/to/VPa06
source venv/bin/activate
python receipt_generator.py
```

При первом запуске в списках уже стоят папки `templates` и `data`, а папка результата — `output` рядом со скриптом. Выбранные папки и последние файлы сохраняются в `settings.json`.

## Как собрать чеки

1. При необходимости добавьте ещё папки кнопкой «Добавить папку» или укажите файл кнопкой «Выбрать файл». «Обновить» перечитывает выбранные папки, включая вложенные. Поиск фильтрует список по названию файла; путь показан рядом, чтобы различать одинаковые имена.
2. Выберите HTML-шаблон и CSV. В шаблоне поля записываются как `{{название_колонки}}`, без Jinja2 и без исполняемого кода.
3. Если файл не читается как UTF-8, программа предложит UTF-8, UTF-8 с BOM или Windows-1251. Разделитель определяется автоматически; его можно сменить на запятую, точку с запятой или табуляцию и нажать «Перечитать».
4. В таблице записей видны номер, дата, клиент и сумма, если такие колонки узнаются по названию. Через «Колонки списка» можно назначить колонки вручную или оставить номер строки.
5. Отметьте одну запись, несколько или нажмите «Выбрать все».
6. «Предпросмотр HTML» открывает одну запись в браузере и не создаёт PDF.
7. «Создать чеки» сохраняет HTML и PDF в выбранную папку. Существующие файлы не перезаписываются: к имени добавляется суффикс `_2`, `_3` и дальше. Переключатель «Открыть PDF после создания» включён по умолчанию: на Windows файл открывается через `os.startfile`, на macOS — командой `open`.
8. В конце показано, сколько чеков создано и какие записи завершились ошибкой. Кнопка «Открыть папку результата» открывает каталог с файлами.

Если название в шаблоне не совпало с колонкой CSV, перед генерацией откроется сопоставление полей. Пустое обязательное значение останавливает только эту запись и сообщает номер записи и имя поля.

Значения из CSV вставляются как текст: символы `<`, `>` и `&` экранируются и не читаются как HTML.

Долгая генерация идёт в фоне. Окно остаётся доступным, ход работы обновляется в главном потоке. Кнопка «Остановить» завершает работу после текущего чека.
