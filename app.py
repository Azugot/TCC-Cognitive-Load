"""Application entrypoint assembling Gradio layouts from modular pages."""

from __future__ import annotations

import gradio as gr

from services.vertex_client import VERTEX_CFG, _vertex_err

from app.pages.admin import build_admin_views, _sync_domain_after_auth
from app.pages.auth import (
    AuthViews,
    _back_home,
    _doLogout,
    _route_home,
    _studentUi,
    _teacherUi,
    build_auth_views,
    doLogin,
    doRegister,
    listStudents,
)
from app.pages.chat import build_studio_page
from app.pages.student import (
    StudentViews,
    build_student_views,
    student_auto_open_rooms,
    student_history_dropdown,
    student_go_rooms,
    student_rooms_back,
    student_set_exit_label,
    student_rooms_refresh,
)
from app.pages.teacher import (
    TeacherView,
    build_teacher_view,
    teacher_history_dropdown,
)


APP_CSS = """
.history-box,
.history-preview {
    max-height: 24rem;
    overflow-y: auto;
    padding: 0.75rem 1rem;
    border: 1px solid var(--block-border-color, rgba(148, 163, 184, 0.35));
    border-radius: var(--radius-lg, 0.75rem);
    background-color: var(--block-background-fill, rgba(255, 255, 255, 0.6));
}

.history-box::-webkit-scrollbar,
.history-preview::-webkit-scrollbar {
    width: 8px;
}

.history-box::-webkit-scrollbar-thumb,
.history-preview::-webkit-scrollbar-thumb {
    background-color: rgba(100, 116, 139, 0.45);
    border-radius: 9999px;
}

.history-box:hover::-webkit-scrollbar-thumb,
.history-preview:hover::-webkit-scrollbar-thumb {
    background-color: rgba(100, 116, 139, 0.75);
}
"""


def _logout_cleanup():
    return (
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(value="Info: Selecione uma sala para visualizar os materiais."),
        gr.update(choices=[], value=None),
        [],
        gr.update(visible=False, value=None, label="Baixar material", file_name=None),
        gr.update(value="Info: Nenhum material disponível para esta sala."),
    )


def build_app() -> gr.Blocks:
    with gr.Blocks(theme=gr.themes.Default(), fill_height=True, css=APP_CSS) as demo:
        auth_state = gr.State(
            {
                "isAuth": False,
                "username": None,
                "full_name": None,
                "display_name": None,
                "role": None,
                "user_id": None,
            }
        )
        docs_state = gr.State({})
        script_state = gr.State("Você é um assistente pedagógico. Aguarde configuração do usuário.")
        adv_state = gr.State({"temperature": 0.7, "top_p": 0.95, "top_k": 40, "max_tokens": 1024})
        classrooms_state = gr.State([])
        subjects_state = gr.State({})
        chats_state = gr.State({})
        current_chat_id = gr.State(None)
        admin_nav_state = gr.State({"page": "home"})
        student_selected_class = gr.State(None)

        auth_views: AuthViews = build_auth_views(
            blocks=demo, vertex_cfg=VERTEX_CFG, vertex_err=_vertex_err
        )

        studio_view = build_studio_page(
            blocks=demo,
            script_state=script_state,
            adv_state=adv_state,
            docs_state=docs_state,
            auth_state=auth_state,
            current_chat_id=current_chat_id,
            chats_state=chats_state,
        )

        student_views: StudentViews = build_student_views(
            blocks=demo,
            auth_state=auth_state,
            classrooms_state=classrooms_state,
            subjects_state=subjects_state,
            docs_state=docs_state,
            script_state=script_state,
            adv_state=adv_state,
            current_chat_id=current_chat_id,
            chats_state=chats_state,
            student_selected_class=student_selected_class,
        )

        teacher_view: TeacherView = build_teacher_view(
            blocks=demo,
            auth_state=auth_state,
            classrooms_state=classrooms_state,
            subjects_state=subjects_state,
            home_view=auth_views.view_home,
        )

        admin_views = build_admin_views(
            blocks=demo,
            auth_state=auth_state,
            classrooms_state=classrooms_state,
            subjects_state=subjects_state,
            chats_state=chats_state,
            admin_nav_state=admin_nav_state,
            studio_container=studio_view.container,
        )

        # Navigation hooks --------------------------------------------------
        auth_views.btn_go_customize.click(
            lambda: (gr.update(visible=False), gr.update(visible=True)),
            inputs=None,
            outputs=[auth_views.view_home, studio_view.container],
        )

        studio_view.back_button.click(
            _back_home,
            inputs=auth_state,
            outputs=[studio_view.container, admin_views.home, auth_views.view_home],
        )

        auth_views.btn_teacher_classrooms.click(
            lambda: (gr.update(visible=False), gr.update(visible=True)),
            inputs=None,
            outputs=[auth_views.view_home, teacher_view.container],
        ).then(
            teacher_history_dropdown,
            inputs=[auth_state, classrooms_state, teacher_view.history_class_dropdown],
            outputs=teacher_view.history_class_dropdown,
        )

        auth_views.btn_student_rooms.click(
            student_go_rooms,
            outputs=[auth_views.view_home, student_views.rooms_view],
        ).then(
            lambda: gr.update(visible=False),
            outputs=student_views.setup_view,
        ).then(
            student_rooms_refresh,
            inputs=[auth_state, classrooms_state, subjects_state],
            outputs=[
                student_views.rooms_dropdown,
                student_views.rooms_info,
                student_selected_class,
                student_views.documents_markdown,
                student_views.documents_dropdown,
                student_views.documents_state,
                student_views.documents_download_button,
                student_views.documents_notice,
            ],
        ).then(
            student_history_dropdown,
            inputs=[auth_state, classrooms_state, student_views.history_class_dropdown],
            outputs=student_views.history_class_dropdown,
        ).then(
            student_set_exit_label,
            outputs=[student_views.rooms_back_button],
        )

        student_views.rooms_back_button.click(
            _doLogout,
            outputs=[
                auth_state,
                auth_views.header,
                auth_views.view_login,
                auth_views.view_home,
                admin_views.home,
                studio_view.container,
                admin_views.classrooms,
                admin_views.history,
                admin_views.evaluate,
                admin_views.progress,
                admin_views.admin_page,
            ],
        ).then(
            _logout_cleanup,
            outputs=[
                teacher_view.container,
                student_views.rooms_view,
                student_views.setup_view,
                student_views.documents_markdown,
                student_views.documents_dropdown,
                student_views.documents_state,
                student_views.documents_download_button,
                student_views.documents_notice,
            ],
        )

        auth_views.btn_view_students.click(
            listStudents,
            inputs=auth_state,
            outputs=[auth_views.students_out],
        )

        admin_views.btn_admin_list_students.click(
            listStudents,
            inputs=auth_state,
            outputs=[auth_views.students_out],
        )

        # Authentication flows ---------------------------------------------
        auth_views.btn_login.click(
            doLogin,
            inputs=[auth_views.username, auth_views.password, auth_state],
            outputs=[auth_views.login_msg, auth_state],
        ).then(
            _route_home,
            inputs=auth_state,
            outputs=[
                auth_views.header,
                auth_views.view_login,
                auth_views.view_home,
                admin_views.home,
                auth_views.home_greet,
            ],
        ).then(
            _teacherUi,
            inputs=auth_state,
            outputs=[auth_views.teacher_row, auth_views.students_out],
        ).then(
            _studentUi,
            inputs=auth_state,
            outputs=[auth_views.student_row],
        ).then(
            _sync_domain_after_auth,
            inputs=[auth_state, classrooms_state, subjects_state],
            outputs=[classrooms_state, subjects_state],
        ).then(
            student_auto_open_rooms,
            inputs=[
                auth_state,
                classrooms_state,
                subjects_state,
                student_views.history_class_dropdown,
                student_selected_class,
                student_views.documents_state,
            ],
            outputs=[
                auth_views.view_home,
                student_views.rooms_view,
                student_views.setup_view,
                student_views.rooms_dropdown,
                student_views.rooms_info,
                student_selected_class,
                student_views.documents_markdown,
                student_views.documents_dropdown,
                student_views.documents_state,
                student_views.documents_download_button,
                student_views.documents_notice,
                student_views.history_class_dropdown,
            ],
        ).then(
            teacher_history_dropdown,
            inputs=[auth_state, classrooms_state, teacher_view.history_class_dropdown],
            outputs=teacher_view.history_class_dropdown,
        ).then(
            student_set_exit_label,
            outputs=[student_views.rooms_back_button],
        )

        auth_views.btn_register.click(
            doRegister,
            inputs=[
                auth_views.username,
                auth_views.password,
                auth_views.confirm_password,
                auth_views.email,
                auth_views.full_name,
                auth_views.role_radio,
                auth_state,
            ],
            outputs=[auth_views.login_msg, auth_state],
        ).then(
            _route_home,
            inputs=auth_state,
            outputs=[
                auth_views.header,
                auth_views.view_login,
                auth_views.view_home,
                admin_views.home,
                auth_views.home_greet,
            ],
        ).then(
            _teacherUi,
            inputs=auth_state,
            outputs=[auth_views.teacher_row, auth_views.students_out],
        ).then(
            _studentUi,
            inputs=auth_state,
            outputs=[auth_views.student_row],
        ).then(
            _sync_domain_after_auth,
            inputs=[auth_state, classrooms_state, subjects_state],
            outputs=[classrooms_state, subjects_state],
        ).then(
            student_auto_open_rooms,
            inputs=[
                auth_state,
                classrooms_state,
                subjects_state,
                student_views.history_class_dropdown,
                student_selected_class,
                student_views.documents_state,
            ],
            outputs=[
                auth_views.view_home,
                student_views.rooms_view,
                student_views.setup_view,
                student_views.rooms_dropdown,
                student_views.rooms_info,
                student_selected_class,
                student_views.documents_markdown,
                student_views.documents_dropdown,
                student_views.documents_state,
                student_views.documents_download_button,
                student_views.documents_notice,
                student_views.history_class_dropdown,
            ],
        ).then(
            teacher_history_dropdown,
            inputs=[auth_state, classrooms_state, teacher_view.history_class_dropdown],
            outputs=teacher_view.history_class_dropdown,
        ).then(
            student_set_exit_label,
            outputs=[student_views.rooms_back_button],
        )

        # Logout handling ---------------------------------------------------
        auth_views.btn_logout_home.click(
            _doLogout,
            outputs=[
                auth_state,
                auth_views.header,
                auth_views.view_login,
                auth_views.view_home,
                admin_views.home,
                studio_view.container,
                admin_views.classrooms,
                admin_views.history,
                admin_views.evaluate,
                admin_views.progress,
                admin_views.admin_page,
            ],
        ).then(
            _logout_cleanup,
            outputs=[
                teacher_view.container,
                student_views.rooms_view,
                student_views.setup_view,
                student_views.documents_markdown,
                student_views.documents_dropdown,
                student_views.documents_state,
                student_views.documents_download_button,
                student_views.documents_notice,
            ],
        )

        admin_views.btn_logout.click(
            _doLogout,
            outputs=[
                auth_state,
                auth_views.header,
                auth_views.view_login,
                auth_views.view_home,
                admin_views.home,
                studio_view.container,
                admin_views.classrooms,
                admin_views.history,
                admin_views.evaluate,
                admin_views.progress,
                admin_views.admin_page,
            ],
        ).then(
            _logout_cleanup,
            outputs=[
                teacher_view.container,
                student_views.rooms_view,
                student_views.setup_view,
                student_views.documents_markdown,
                student_views.documents_dropdown,
                student_views.documents_state,
                student_views.documents_download_button,
                student_views.documents_notice,
            ],
        )

        demo.queue()

    return demo


def launch():
    app = build_app()
    app.launch(debug=True)


if __name__ == "__main__":
        launch()
