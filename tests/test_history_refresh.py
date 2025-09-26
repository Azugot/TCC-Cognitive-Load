import copy
import unittest
from unittest.mock import patch

import gradio as gr

from app.pages.history_shared import (
    HISTORY_TABLE_HEADERS,
    _format_timestamp,
    _history_table_data,
    prepare_history_listing,
)
from app.pages.student import student_history_refresh
from app.pages.teacher import teacher_history_refresh


SAMPLE_CHATS = [
    {
        "id": "chat-1",
        "student_name": "Ana",
        "student_login": "ana",
        "classroom_name": "Sala 1",
        "classroom_id": "1",
        "subjects": ["Matemática"],
        "summary_preview": "Resumo 1",
        "grade": 8.5,
        "started_at": "2024-03-01T10:00:00",
    },
    {
        "id": "chat-2",
        "student_name": "Ana",
        "student_login": "ana",
        "classroom_name": "Sala 1",
        "classroom_id": "1",
        "subjects": ["Português"],
        "summary": "Resumo 2",
        "grade": None,
        "teacher_comments": [
            {"score": 9.0, "created_at": "2024-03-02T09:00:00", "author_name": "Prof."}
        ],
        "started_at": "2024-03-02T08:30:00Z",
    },
    {
        "id": "chat-3",
        "student_name": "Bruno",
        "student_login": "bruno",
        "classroom_name": "Sala 2",
        "classroom_id": "2",
        "subjects": [],
        "subject_free_text": "História",
        "summary_preview": "Resumo 3",
        "grade": 7,
        "started_at": 1710000000,
    },
]


class HistoryRefreshHelperTests(unittest.TestCase):
    def test_prepare_history_listing_custom_messages(self):
        chats = copy.deepcopy(SAMPLE_CHATS[:1])

        table_update, filtered, dropdown_update, message, default_id = prepare_history_listing(
            chats,
            column_labels=HISTORY_TABLE_HEADERS,
            filter_fn=None,
            dropdown_label=lambda chat: chat["id"],
            empty_message="Nada encontrado",
            found_message=lambda count: f"{count} chat(s) localizados",
        )

        self.assertEqual(filtered, chats)
        self.assertEqual(table_update, gr.update(value=_history_table_data(chats)))
        self.assertEqual(dropdown_update, gr.update(choices=[("chat-1", "chat-1")], value="chat-1"))
        self.assertEqual(message, "1 chat(s) localizados")
        self.assertEqual(default_id, "chat-1")

        (
            empty_table,
            empty_filtered,
            empty_dropdown,
            empty_message,
            empty_default,
        ) = prepare_history_listing(
            chats,
            column_labels=HISTORY_TABLE_HEADERS,
            filter_fn=lambda _: False,
            dropdown_label=lambda chat: chat["id"],
            empty_message="Sem resultados",
            found_message="Ignorado",
        )

        self.assertEqual(empty_table, gr.update(value=[]))
        self.assertEqual(empty_filtered, [])
        self.assertEqual(empty_dropdown, gr.update(choices=[], value=None))
        self.assertEqual(empty_message, "Sem resultados")
        self.assertIsNone(empty_default)

    @patch("app.pages.student.list_student_chats")
    def test_student_history_refresh_matches_previous_updates(self, mock_list_chats):
        mock_list_chats.return_value = copy.deepcopy(SAMPLE_CHATS)

        result = student_history_refresh({"user_id": "student-1"}, "1")

        expected_filtered = [
            chat for chat in mock_list_chats.return_value if str(chat.get("classroom_id")) == "1"
        ]
        expected_table = _history_table_data(expected_filtered)
        expected_dropdown = []
        for chat in expected_filtered:
            chat_id = chat.get("id")
            if not chat_id:
                continue
            classroom = chat.get("classroom_name") or chat.get("classroom_id") or "Sala"
            started = _format_timestamp(chat.get("started_at"))
            expected_dropdown.append((f"{classroom} — {started}", chat_id))

        default_id = expected_dropdown[0][1] if expected_dropdown else None
        expected_message = (
            f"✅ {len(expected_filtered)} chat(s) encontrados."
            if expected_filtered
            else "ℹ️ Nenhum chat para o filtro selecionado."
        )

        expected = (
            gr.update(value=expected_table),
            expected_filtered,
            gr.update(choices=expected_dropdown, value=default_id),
            expected_message,
            default_id,
        )

        self.assertEqual(result, expected)

    @patch("app.pages.teacher.list_teacher_classroom_chats")
    def test_teacher_history_refresh_matches_previous_updates(self, mock_list_chats):
        mock_list_chats.return_value = copy.deepcopy(SAMPLE_CHATS)

        result = teacher_history_refresh({"user_id": "teacher-1"}, "1")

        expected_filtered = [
            chat for chat in mock_list_chats.return_value if str(chat.get("classroom_id")) == "1"
        ]
        expected_table = _history_table_data(expected_filtered)
        expected_dropdown = []
        for chat in expected_filtered:
            chat_id = chat.get("id")
            if not chat_id:
                continue
            student = chat.get("student_name") or chat.get("student_login") or "Aluno"
            started = _format_timestamp(chat.get("started_at"))
            expected_dropdown.append((f"{student} — {started}", chat_id))

        default_id = expected_dropdown[0][1] if expected_dropdown else None
        expected_message = (
            f"✅ {len(expected_filtered)} chat(s) encontrados."
            if expected_filtered
            else "ℹ️ Nenhum chat para o filtro."
        )

        expected = (
            gr.update(value=expected_table),
            expected_filtered,
            gr.update(choices=expected_dropdown, value=default_id),
            expected_message,
            default_id,
        )

        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
