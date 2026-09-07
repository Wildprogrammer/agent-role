from pathlib import Path

import pytest

from agent_workflow_hub.desktop_client_automation.contracts import load_request


ROOT = Path(__file__).resolve().parents[3]
REQUEST = (
    ROOT
    / "workspace"
    / "workflows"
    / "desktop-client-automation"
    / "bambu-studio"
    / "bambu-studio-favorites-search-request.json"
)


def test_bambu_studio_request_is_a_repeatable_image_replay() -> None:
    if not REQUEST.is_file():
        pytest.skip("requires the local Bambu Studio acceptance request")
    request = load_request(REQUEST)

    assert request.name == "bambu-studio-favorites-search-bbgun"
    assert len(request.apps) == 1
    assert request.apps[0].alias == "bambu_studio"
    assert request.apps[0].lifecycle == "reuse"
    assert [step.action for step in request.steps] == [
        "click-image",
        "click-image",
        "click-image",
        "click-image",
        "click-image",
        "click-image",
        "type-text",
        "press-key",
    ]
    assert [step.id for step in request.steps[:6]] == [
        "reset-to-model-library",
        "open-online-models",
        "return-to-online-home",
        "open-favorites",
        "open-favorite-search",
        "focus-favorite-search-box",
    ]
    assert request.steps[2].optional is True
    assert request.steps[6].values["text"] == "${query}"
    assert request.steps[7].values["key"] == "ENTER"
    assert request.parameters[0].name == "query"
    assert request.parameters[0].default == "BB\u67aa"
    assert request.steps[0].values["template"].endswith("model-library-nav-v001.png")
    assert request.steps[2].values["template"].endswith("return-online-home-v001.png")
    assert request.success_assertion.template.name == "verification-bb-result-v001.png"
