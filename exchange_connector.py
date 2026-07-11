import requests
import re
from datetime import datetime

class Tools:
    def __init__(self):
        # Настройки для нативного запуска (Windows)
        self.exchange_url = "http://127.0.0.1:5015"
        self.imap_url = "http://127.0.0.1:5012"

    def _format_emails(self, emails: list) -> str:
        if not emails: return "Нет писем."
        result = ""
        for mail in emails:
            sender_name = mail.get('sender', 'Неизвестно')
            sender_email = mail.get('sender_email')
            sender = f"{sender_name} <{sender_email}>" if sender_email else sender_name
            
            message_id = mail.get('id')
            id_str = f"ID: {message_id}\n" if message_id else ""

            subject = mail.get('subject', 'Без темы')
            
            # Получаем тело письма
            body = mail.get('body', '')

            # 🛠️ МОЩНАЯ ФИЛЬТРАЦИЯ БАННЕРОВ
            # Список шаблонов мусора, который надо вырезать
            junk_patterns = [
                # 1. Стандартный баннер "Phishing"
                r"(?:📝\s*)?ВНЕШНЕЕ ПИСЬМО[\s\S]*?# Phishing\.?",
                # 2. Русский текст про безопасность (как на скрине)
                r"ВНЕШНЕЕ ПИСЬМО[\s\S]{0,400}?сомневаетесь в безопасности письма[\s\S]*?\.",
                r"Данное письмо от внешнего отправителя[\s\S]{0,300}?безопасности письма",
                # 3. Английские варианты
                r"CAUTION: This email originated from outside[\s\S]{0,300}?\."
            ]

            for pattern in junk_patterns:
                body = re.sub(pattern, "", body, flags=re.IGNORECASE).strip()

            # Обрезаем тело письма до 1000 символов
            body = body[:1000] 
            
            time_val = mail.get('time', '')
            result += f"{id_str}TIME: {time_val}\nFROM: {sender}\nSUBJ: {subject}\nBODY: {body}\n{'-'*20}\n"
        return result

    def check_work_email(self, minutes: int = 60) -> str:
        """
        Проверяет рабочую почту (Exchange) за последний час.
        :param minutes: Период проверки в минутах (по умолчанию 60).
        """
        try:
            res = requests.post(f"{self.exchange_url}/check_archive_new", json={"minutes": minutes}, timeout=30)
            data = res.json()
            if data.get("status") == "new_mail":
                return f"Найдено {data['count']} писем в рабочем архиве:\n" + self._format_emails(data.get("emails", []))
            elif data.get("status") == "empty":
                return "Новых писем в рабочем архиве нет."
            else:
                return f"Ответ сервера: {data}"
        except Exception as e:
            return f"Ошибка соединения с Exchange (5015): {e}"

    def check_personal_email(self, limit: int = 5) -> str:
        """
        Проверяет личную почту (IMAP). Возвращает непрочитанные.
        :param limit: Лимит писем (по умолчанию 5).
        """
        try:
            res = requests.post(f"{self.imap_url}/check_new_emails", json={"limit": limit}, timeout=30)
            data = res.json()
            if data.get("status") == "new_mail":
                return f"Найдено {data['count']} личных писем:\n" + self._format_emails(data.get("emails", []))
            elif data.get("status") == "empty":
                return "Личный ящик пуст (нет новых непрочитанных)."
            else:
                return f"Ответ сервера: {data}"
        except Exception as e:
            return f"Ошибка соединения с IMAP (5012): {e}"

    def get_calendar_overview(self, date: str = "today") -> str:
        """
        Показывает расписание встреч на день.
        :param date: 'today' (сегодня) или 'tomorrow' (завтра).
        """
        try:
            res = requests.post(f"{self.exchange_url}/get_calendar", json={"date": date}, timeout=30)
            return res.json().get("result", "Календарь пуст")
        except Exception as e:
            return f"Ошибка календаря: {e}"

    def search_calendar_event(self, query: str) -> str:
        """
        Ищет конкретную встречу в календаре по названию или участнику.
        :param query: Текст для поиска (например 'Бюджет' или 'Иванов').
        """
        try:
            res = requests.post(f"{self.exchange_url}/search_calendar", json={"query": query, "days": 30}, timeout=30)
            return res.json().get("result", "Ничего не найдено")
        except Exception as e:
            return f"Ошибка поиска: {e}"

    def send_work_email(self, to_email: str, subject: str, body: str) -> str:
        """
        Отправляет письмо с РАБОЧЕЙ почты.
        :param to_email: Email получателя.
        :param subject: Тема.
        :param body: Текст.
        """
        try:
            res = requests.post(
                f"{self.exchange_url}/send_work_email", 
                json={"to_email": to_email, "subject": subject, "body": body}, 
                timeout=30
            )
            return res.json().get("message", "Результат неизвестен")
        except Exception as e:
            return f"Ошибка отправки: {e}"

    def reply_work_email(self, message_id: str, body: str, subject: str = None) -> str:
        """
        Отвечает на письмо по его ID (Exchange).
        :param message_id: ID письма (из check_work_email).
        :param body: Текст ответа.
        :param subject: Тема ответа (необязательно).
        """
        try:
            payload = {"message_id": message_id, "body": body}
            if subject:
                payload["subject"] = subject
            res = requests.post(
                f"{self.exchange_url}/reply_work_email", 
                json=payload, 
                timeout=30
            )
            return res.json().get("message", "Результат неизвестен")
        except Exception as e:
            return f"Ошибка ответа: {e}"