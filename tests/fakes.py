from core.llm import _Provider


class FakeProvider(_Provider):
    """Stands in for Gemini/Groq. Replies can be strings or exceptions, or a function of the prompt."""

    def __init__(self, name="gemini", replies=None, respond=None):
        super().__init__(model=f"{name}-model", min_interval=0)
        self.name = name
        self.replies = list(replies or [])
        self.respond = respond
        self.calls = 0

    def call(self, prompt, system):
        self.calls += 1
        if self.respond:
            return self.respond(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
