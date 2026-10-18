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

    def test_listing_failure_falls_back_to_usual_models(self):
        self.assertEqual(client(FakeModels(boom=True))._careful(), ["flash-a", "flash-b"])

    def test_listed_once(self):
        c = client(FakeModels([m("gemini-3.8-pro")]))
        first = c._careful()
        c._client.models.boom = True
        self.assertEqual(c._careful(), first)


if __name__ == "__main__":
    unittest.main()
