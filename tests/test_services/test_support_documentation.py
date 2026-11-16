import pytest

from src.backend.services.support_knowledge_service import SUPPORT_ARTICLES, SupportKnowledgeService
from src.shared.services.support_documentation import load_public_documentation
from src.shared.services.support_safety import redact_support_secrets, validate_support_faq_answer


def test_corpus_contains_real_sdk_sections_examples_and_public_source_urls():
    docs = [article for article in SUPPORT_ARTICLES if article.kind == "documentation"]
    assert len(docs) > 100
    assert {article.id.split(":")[1] for article in docs} == {"vi", "en"}
    assert any("1536" in article.answer and "3072" in article.answer for article in docs)
    assert any("client.chat.completions.create" in article.answer and "os.environ" in article.answer for article in docs)
    assert all(article.url.startswith("https://minminhmin.github.io/taskway.github.io/") for article in docs)
    assert all(len(doc.text) <= 6000 for doc in SupportKnowledgeService().documents(""))


def test_only_public_docs_and_whitelisted_snippets_are_loaded(tmp_path):
    content = tmp_path / "src" / "content" / "docs" / "vi"
    content.mkdir(parents=True)
    examples = tmp_path / "examples" / "python"
    examples.mkdir(parents=True)
    (examples / "demo.py").write_text('api_key=os.environ["TASKWAY_API_KEY"]\nprint("hello")', encoding="utf-8")
    (tmp_path / "internal").mkdir()
    (tmp_path / "internal" / "secret.md").write_text("INTERNAL_DOC_MUST_NOT_BE_INDEXED", encoding="utf-8")
    (content / "quickstart.md").write_text('''---
title: Quickstart
---
<h2 id="send-a-request">Send a request</h2>
```python snippet=python/demo.py
```
''', encoding="utf-8")
    chunks = load_public_documentation(tmp_path)
    assert len(chunks) == 1
    assert "os.environ" in chunks[0].text
    assert "print" in chunks[0].text
    assert "INTERNAL_DOC" not in chunks[0].text
    assert chunks[0].url.endswith("/vi/docs/quickstart/#send-a-request")
    (content / "quickstart.md").write_text('''---
title: Unsafe
---
<h2 id="request">Request</h2>
```python snippet=../internal/secret.md
```
''', encoding="utf-8")
    with pytest.raises(ValueError, match="public example"):
        load_public_documentation(tmp_path)


def test_public_sdk_code_can_be_generated_but_real_keys_and_invented_urls_are_rejected():
    evidence = {"sdk": 'Use base_url="https://taskway-ai-be.online/v1" and application API keys stored in os.environ.'}
    sample = '''```python
client = OpenAI(api_key=os.environ["TASKWAY_API_KEY"], base_url="https://taskway-ai-be.online/v1")
```'''
    assert validate_support_faq_answer(sample, ["sdk"], evidence)
    assert validate_support_faq_answer('api_key="YOUR_APPLICATION_KEY"', ["sdk"], evidence)
    assert not validate_support_faq_answer(sample.replace("taskway-ai-be.online", "evil.example"), ["sdk"], evidence)
    assert not validate_support_faq_answer('api_key="sk-proj-' + "x" * 24 + '"', ["sdk"], evidence)
    assert redact_support_secrets('password="actual-password"') == "password=[REDACTED]"
