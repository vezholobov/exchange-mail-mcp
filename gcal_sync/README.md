# Exchange → Google Calendar sync

Односторонний мост: встречи из рабочего Exchange (RenCredit) попадают в
основной Google Calendar. Обратной записи нет.

## Как это работает

1. `sync.py` забирает структурированные встречи из
   `POST http://172.17.0.1:5015/calendar_events_json` (окно −7…+60 дней,
   recurrence уже развёрнуты EWS).
2. Каждая встреча создаётся или обновляется в Google Calendar через
   сервисный аккаунт.
3. Созданные события помечаются невидимыми `extendedProperties`:
   `sync_source=exchange`, `sync_key`, `sync_hash`.
4. Повторный прогон не создаёт дублей: неизменённые события пропускаются,
   исчезнувшие из Exchange удаляются, отменённые не создаются.
5. Ручные события Boss'а не трогаются: обновляются и удаляются только
   события с меткой `sync_source=exchange`.

Ключ события: `uid` + `recurrence_id` (для повторяющихся) либо дата начала.
Это переживает переименования и переносы.

## Безопасность

- Доступ к Google ограничен одним календарём, расшаренным на сервисный
  аккаунт с правом «Вносить изменения».
- Ключ сервисного аккаунта лежит в `~/.config/exchange-gcal-sync/`,
  права `0600`.
- Приватные встречи копируются без описания и участников.
- Защита от массового удаления: `max_deletes` (по умолчанию 50) и отказ
  удалять что-либо, если Exchange вернул пустой список.

## Установка

```bash
python3 -m venv .venv
./.venv/bin/pip install google-api-python-client google-auth google-auth-httplib2

cp config.example.json ~/.config/exchange-gcal-sync/config.json
# указать calendar_id (email аккаунта), путь к ключу и прочее

./.venv/bin/python sync.py --dry-run   # проверка плана
./.venv/bin/python sync.py             # запись
```

## GCP и шаринг календаря

```bash
gcloud auth login
PROJECT_ID=exgcal-$(date +%y%m%d)-$RANDOM ./setup_gcp.sh
```

Скрипт печатает email сервисного аккаунта и путь к ключу. Затем в Google
Calendar: «Настройки календаря → Доступ для отдельных пользователей →
Добавить пользователя → <email сервисного аккаунта> → Вносить изменения».

## systemd

```bash
sudo cp deploy/exchange-gcal-sync.service deploy/exchange-gcal-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now exchange-gcal-sync.timer
```

Таймер запускает синхронизацию каждые 15 минут. Первый прогон удобно
сделать вручную: `sudo systemctl start exchange-gcal-sync.service`.

## Ограничения

- Переносы и отмены подхватываются в течение 15 минут.
- Правки, сделанные в Google вручную внутри синхронизированного события,
  будут перезаписаны при следующем прогоне.
- Удаление встречи в Google приведёт к её повторному созданию, пока она
  есть в Exchange.
