# Локальная подпись (без платного аккаунта Apple)

Для работы на **этом** Mac: ad-hoc подпись (`codesign --sign -`), без Apple ID, сертификата и нотаризации.
Системная защита (SIP, Gatekeeper, TCC) не меняется.

## Что это за приложение

| Часть | Технология | Где |
|---|---|---|
| Запускатель `AIVirtualCameraStudio` | Swift | `Contents/MacOS/AIVirtualCameraStudio` — `CFBundleExecutable`, владелец разрешения на камеру |
| Основная программа `vcstudio` | Python 3.12, PyInstaller | `Contents/MacOS/vcstudio` |
| Библиотеки | Python.framework, Qt (PySide6), PyObjC, OpenCV, NumPy/SciPy, свои `libVCSNative`/`libVCSShm` | `Contents/Frameworks/` (333 Mach-O файла) |
| Плагин OBS | C | `Contents/Resources/obs-plugin/vcstudio-obs.plugin` |
| XPC, расширения приложений, вложенные .app | нет | — |

Bundle ID `com.aivcstudio.app`. App Sandbox **не используется** и не нужен: приложение читает общую память OBS,
запускает `screencapture`, загружает модели пользователя из своей папки данных.

## Как подписывается (автоматически)

`scripts/build_app.sh` после сборки вызывает `scripts/sign_local.sh`. Вручную, для уже собранного приложения:

```bash
scripts/sign_local.sh "/путь/AI Virtual Camera Studio.app"
```

Порядок — изнутри наружу, **без `--deep`** при подписи:

1. все отдельные Mach-O (`.dylib`, `.so`), самые глубокие первыми — без entitlements;
2. фреймворки: сначала их Mach-O, затем сам `.framework` — без entitlements;
3. плагин OBS (`.plugin`) — без entitlements;
4. `vcstudio`, затем `AIVirtualCameraStudio`, затем само приложение — с `packaging/local.entitlements`
   и `--generate-entitlement-der`.

Всё — с **Hardened Runtime** (`--options runtime`); `--no-runtime` подписывает без него.

## Entitlements (`packaging/local.entitlements`) — только нужные

| Право | Зачем |
|---|---|
| `com.apple.security.device.camera` | вход с камеры через AVFoundation при включённом Hardened Runtime |
| `com.apple.security.cs.disable-library-validation` | у ad-hoc библиотек нет Team ID; без этого исключения Hardened Runtime не загрузил бы собственные библиотеки приложения |
| `com.apple.security.cs.allow-unsigned-executable-memory` | Python, PyObjC и libffi создают исполняемые переходники во время работы |

Ни одно из них не ограниченное (restricted): профиль Apple не нужен. Sandbox-прав нет, Sandbox и Hardened Runtime
не смешиваются.

## Info.plist

| Ключ | Есть | Зачем |
|---|---|---|
| `NSCameraUsageDescription` | да | текст запроса доступа к камере |
| микрофон, контакты, Apple Events и т. д. | не нужны | приложение их не использует |
| запись экрана | ключа в Info.plist не существует | macOS спросит при первом «Найти рамки автоматически» |

## Разрешения macOS (TCC) — выдаёт только пользователь

| Разрешение | Когда спросит | Без него |
|---|---|---|
| Камера | при захвате с камеры (OBS Virtual Camera / веб-камера) и в «Проверить всю цепочку» | вход с камеры не работает; вход через плагин OBS работает |
| Запись экрана | «Карточка для игры → Найти рамки автоматически» | автопоиск рамки не видит игру; рамку можно обвести вручную |

**Важно:** macOS запоминает разрешение для ad-hoc приложения по хешу его кода. После каждой новой сборки хеш
меняется, и macOS может спросить разрешение снова. Если разрешение «застряло» (приложение не спрашивает и не
работает), удалите старую запись: «Системные настройки → Конфиденциальность и безопасность → Камера / Запись экрана».

## Что ad-hoc подпись НЕ даёт

| Право | Кому нужно | Почему не работает |
|---|---|---|
| `com.apple.developer.system-extension.install` | установка своей камеры «AI Virtual Camera» (`native/HostApp`) | ограниченное: ядро разрешает его только с профилем Apple; проверено — с ним ad-hoc программа убивается при запуске ([CAMERA_LOCAL_SIGNING.md](CAMERA_LOCAL_SIGNING.md)) |
| App Group `<TeamID>.com.aivcstudio.app.camera` | расширение камеры | требует Team ID; у ad-hoc его нет |

В собранном приложении этих прав **нет** — оно им не нужно (вывод идёт в OBS Virtual Camera).

## Проверка

```bash
codesign --verify --deep --strict --verbose=2 "/Applications/AI Virtual Camera Studio.app"
codesign -d --entitlements - "/Applications/AI Virtual Camera Studio.app"
"/Applications/AI Virtual Camera Studio.app/Contents/MacOS/AIVirtualCameraStudio" --self-check
```

`spctl --assess --type execute` отвечает `rejected` для ad-hoc приложения — это ожидаемо (проверено): Gatekeeper принимает только
Developer ID. Собранное на этом Mac приложение не помечено карантином и запускается без этой проверки; скачанное
из интернета — откройте один раз правой кнопкой → «Открыть».
