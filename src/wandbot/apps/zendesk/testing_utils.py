# Define a dummy client class
class DummyResponse:
    def __init__(self, answer: str) -> None:
        self.answer = answer

class DummyAPIClient:
    async def query(self, question: str, chat_history: list, language: str) -> str:
        return DummyResponse(answer="Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.")