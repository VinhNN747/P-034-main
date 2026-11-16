"""Reviewed public documentation links; the model cannot supply arbitrary URLs."""

DOCS_BASE_URL = "https://minminhmin.github.io/taskway.github.io"
DOCUMENTATION_CHAPTERS = {
    "quickstart": ("quickstart/", "Bắt đầu sử dụng Taskway", "Taskway quickstart"),
    "authentication": ("authentication/", "Xác thực API", "API authentication"),
    "api-key": ("authentication/#create-an-application-key", "Tạo application API key", "Create an application API key"),
    "sdk-configuration": ("authentication/#configure-the-sdk", "Cấu hình SDK", "Configure the SDK"),
    "sdk-installation": ("quickstart/#install-the-sdk", "Cài đặt SDK", "Install the SDK"),
    "sdk-request": ("quickstart/#send-a-request", "Gửi request đầu tiên bằng SDK", "Send your first SDK request"),
    "chat-output": ("chat-completions/#output-and-usage", "Đọc kết quả và usage", "Read output and usage"),
    "responses-output": ("responses/#create-a-response", "Đầu ra Responses API", "Responses API output"),
    "streaming": ("tools-and-streaming/", "Function tools và streaming", "Function tools and streaming"),
}


def documentation_links(chapters: tuple[str, ...], *, vietnamese: bool) -> list[tuple[str, str]]:
    language = "vi" if vietnamese else "en"
    return [
        (title_vi if vietnamese else title_en, f"{DOCS_BASE_URL}/{language}/docs/{path}")
        for path, title_vi, title_en in (DOCUMENTATION_CHAPTERS[chapter] for chapter in chapters)
    ]
