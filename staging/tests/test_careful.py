import unittest
from types import SimpleNamespace as NS

from transcribe import GeminiClient


class FakeModels:
    def __init__(self, items=None, boom=False):
        self.items, self.boom = items or [], boom

    def list(self):
        if self.boom:
            raise RuntimeError("no list")
        return self.items


def client(models):
    c = GeminiClient.__new__(GeminiClient)
    c._client = NS(models=models)
    c._models = ["flash-a", "flash-b"]
    c._careful_models = None
    return c


def m(name, acts=("generateContent",)):
    return NS(name="models/" + name, supported_actions=list(acts))


class CarefulModelsTests(unittest.TestCase):
    def test_newest_pro_models_first_then_usual_ones(self):
        c = client(FakeModels([m("gemini-3.6-pro"), m("gemini-3.8-pro"), m("gemini-3.8-flash"),
                               m("gemini-3.8-pro-image"), m("gemini-3.9-pro-tts"),
                               m("gemini-2.5-pro"), m("gemini-4.0-pro", acts=("embedContent",))]))
        self.assertEqual(c._careful(), ["gemini-3.8-pro", "gemini-3.6-pro", "flash-a", "flash-b"])

    def test_research_agents_and_previews_of_other_kinds_are_not_chosen(self):
        c = client(FakeModels([m("deep-research-pro-preview-12-2025"), m("gemini-3.1-pro-preview"),
                               m("gemini-3.1-pro-preview-customtools"), m("gemini-2.5-pro")]))
        self.assertEqual(c._careful(), ["gemini-3.1-pro-preview", "gemini-2.5-pro", "flash-a", "flash-b"])

    def test_listing_failure_falls_back_to_usual_models(self):
        self.assertEqual(client(FakeModels(boom=True))._careful(), ["flash-a", "flash-b"])

    def test_listed_once(self):
        c = client(FakeModels([m("gemini-3.8-pro")]))
        first = c._careful()
        c._client.models.boom = True
        self.assertEqual(c._careful(), first)


class FakeApi:
    def __init__(self, fail_models=(), text='[{"text": "ok"}]'):
        self.fail_models, self.text, self.configs, self.models_tried = set(fail_models), text, [], []
        self.files = NS(upload=lambda file: "F")
        self.models = NS(list=lambda: [m("gemini-3.1-pro-preview")], generate_content=self.generate)

    def generate(self, model, contents, config):
        self.models_tried.append(model)
        self.configs.append(config)
        if model in self.fail_models:
            raise RuntimeError("400 INVALID_ARGUMENT bad model")
        return NS(text=self.text)


def real(api):
    c = GeminiClient.__new__(GeminiClient)
    c._client, c._models, c._careful_models = api, ["flash-a"], None
    return c


class AskTests(unittest.TestCase):
    def test_no_forced_temperature(self):
        api = FakeApi()
        real(api)._ask("a.mp3", "p")
        self.assertNotIn("temperature", api.configs[0])

    def test_failing_careful_model_falls_back_to_usual_models(self):
        api = FakeApi(fail_models={"gemini-3.1-pro-preview"})
        rows = real(api)._ask("a.mp3", "p", careful=True)
        self.assertEqual(rows, [{"text": "ok"}])
        self.assertEqual(api.models_tried[-1], "flash-a")

    def test_align_retries_once_when_answer_is_empty(self):
        api = FakeApi(text="not json")
        self.assertEqual(real(api).align_words("a.mp3", "متن"), [])
        self.assertEqual(len(api.models_tried), 2)


if __name__ == "__main__":
    unittest.main()
