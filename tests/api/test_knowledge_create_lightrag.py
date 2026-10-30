"""Regression: POST /knowledge-bases must persist the requested provider.

The create route used to register the KB (status=initializing, no provider)
and then crash on an unbound ``description`` local before ``_save_config()`` —
the entry survived with the default provider (llamaindex) while the request
returned 500, so the web dialog never closed and the KB showed the wrong
engine.
"""

import asyncio
import json

from fastapi import BackgroundTasks

import deeptutor.api.routers.knowledge as knowledge_router_module
from deeptutor.knowledge.manager import KnowledgeBaseManager


def test_create_lightrag_kb_persists_provider_and_description(monkeypatch, tmp_path):
    base = tmp_path / "knowledge_bases"
    base.mkdir()
    manager = KnowledgeBaseManager(base_dir=str(base))
    monkeypatch.setattr(knowledge_router_module, "get_kb_manager", lambda: manager)
    monkeypatch.setattr(knowledge_router_module, "_current_kb_base_dir", lambda: base)
    monkeypatch.setattr(
        knowledge_router_module, "_assert_provider_ready", lambda *_a, **_k: None
    )

    class _Snapshot:
        def persisted_policy(self):
            return {"policy": "released"}

    monkeypatch.setattr(
        knowledge_router_module, "_freeze_default_indexing_llm", lambda: _Snapshot()
    )

    import deeptutor.services.rag.pipelines.lightrag.indexing_policy as policy_mod

    monkeypatch.setattr(policy_mod, "bind_target", lambda snapshot, *_a, **_k: snapshot)

    async def workflow():
        return await knowledge_router_module.create_knowledge_base(
            BackgroundTasks(),
            name="lrrepro",
            files=[],
            rag_provider="lightrag",
            pageindex_mode="",
            search_mode="",
            rel_paths=None,
            indexing_llm="",
            description="papers about graphs",
        )

    asyncio.run(workflow())

    config = json.loads((base / "kb_config.json").read_text(encoding="utf-8"))
    entry = config["knowledge_bases"]["lrrepro"]
    assert entry["rag_provider"] == "lightrag"
    assert entry["description"] == "papers about graphs"
    assert entry["status"] == "ready"
